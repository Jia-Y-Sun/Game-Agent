"""行为克隆训练。

原版只能读「一次离线采集、每帧都有图」的 `samples.jsonl`，
也就是只能学“开局那几十秒”。本版改成读整场人为辅助数据集：

* 新格式 `assist_samples.jsonl`（`train.py` 里实时采的，
  含“哪几步是人操作、AI 当时想干什么、处于什么阶段”）；
* 旧格式 `samples.jsonl` 仍然兼容；
* `--bc_samples_path` 支持文件 / 目录 / 逗号分隔多份数据，
  可以把多次人为辅助的对局合并成一个训练集；
* `--bc_include_ai_steps` 可以把 AI 自己打的步也加进来（默认只用人操作的那些步）。

同时把输入分辨率降到 `--obs_size`、默认开启 AMP，
显存占用比原版（640x640 fp32 + 1.99 亿参数网络）低两个数量级。
"""

import os

import cv2
import numpy as np
import torch
from torch import optim
from torch.utils.data import DataLoader, Dataset

import assist_dataset as ads
import vram_manager
from argparses import args, device
from dqnAgent import action_bc_loss
from net_actor import NetDQN, build_model, detect_arch_from_state_dict, load_state_dict_flexible


ACTION_SIZES = (2, 360, 9, 11, 3, 360, 100, 5)


def resolve_source():
    if args.bc_samples_path:
        return args.bc_samples_path

    latest = ads.latest_episode_dir(args.human_data_dir, prefer_frames=True)
    if latest:
        return latest
    return args.human_data_dir


class HumanActionDataset(Dataset):
    """把“一帧 + 一个动作标签”读成训练样本。

    标签来源优先级：`human_act`（人这一步真的做了什么）>
    `act`（这一步实际执行了什么）。因此同一条数据既可以用于纯模仿，
    也可以把 AI 自己的决策一起学。
    """

    def __init__(self, source, obs_size=160, include_ai_steps=False, limit=0):
        self.obs_size = max(64, int(obs_size))
        self.records = []
        self.human_samples = 0
        self.ai_samples = 0

        for record in ads.iter_records(source, require_frame=True, limit=limit):
            label = record.get("human_act")
            is_human = label is not None
            if label is None:
                if not include_ai_steps:
                    continue
                label = record["acts"]
                if ads.is_zero_action(label) and not record.get("human_present"):
                    # AI 的空动作对模仿学习没有信息量，默认丢掉
                    continue
            frame_path = record.get("frame_path")
            if not frame_path or not os.path.exists(frame_path):
                continue
            self.records.append((frame_path, ads.clamp_action(label), is_human))
            if is_human:
                self.human_samples += 1
            else:
                self.ai_samples += 1

        if not self.records:
            raise RuntimeError(
                "没有可用的行为克隆样本。请确认：\n"
                "  1) --assist_mode 不是 off，且 --assist_record_enabled true；\n"
                "  2) --assist_frame_policy 不是 none（行为克隆需要图片帧）；\n"
                f"  3) 数据目录里有带 frame_path 的 jsonl：{source}"
            )

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        frame_path, action, is_human = self.records[index]
        image = cv2.imread(frame_path)
        if image is None:
            raise RuntimeError(f"Failed to read image: {frame_path}")
        if image.shape[0] != self.obs_size or image.shape[1] != self.obs_size:
            image = cv2.resize(image, (self.obs_size, self.obs_size), interpolation=cv2.INTER_AREA)
        image = torch.from_numpy(np.ascontiguousarray(image)).float().permute(2, 0, 1) / 255.0
        return image, torch.LongTensor(action), torch.tensor(1 if is_human else 0, dtype=torch.float32)

    def stats(self):
        return {
            "samples": len(self.records),
            "human_samples": self.human_samples,
            "ai_samples": self.ai_samples,
        }


def branch_accuracy(outputs, actions):
    with torch.no_grad():
        predictions = [torch.argmax(output, dim=1) for output in outputs]
        move_acc = (predictions[0] == actions[:, 0]).float().mean().item()
        angle_acc = (predictions[1] == actions[:, 1]).float().mean().item()
        info_acc = (predictions[2] == actions[:, 2]).float().mean().item()
        attack_acc = (predictions[3] == actions[:, 3]).float().mean().item()
        coarse_acc = (
            (predictions[0] == actions[:, 0])
            & (predictions[2] == actions[:, 2])
            & (predictions[3] == actions[:, 3])
        ).float().mean().item()
    return move_acc, angle_acc, info_acc, attack_acc, coarse_acc


def train_behavior_clone():
    source = resolve_source()
    arch = _resolve_arch()
    obs_size = 640 if arch == "legacy" else args.obs_size
    if obs_size != args.obs_size:
        print(f"[bc] legacy 结构要求 640x640 输入，已把 obs_size 从 {args.obs_size} 调整为 {obs_size}")

    dataset = HumanActionDataset(
        source,
        obs_size=obs_size,
        include_ai_steps=args.bc_include_ai_steps,
        limit=args.bc_max_samples,
    )
    dataloader = DataLoader(
        dataset,
        batch_size=max(1, args.bc_batch_size),
        shuffle=True,
        num_workers=max(0, args.bc_num_workers),
        pin_memory=torch.cuda.is_available(),
        drop_last=False,
    )

    model = build_model(arch=arch, input_size=obs_size, device=device)
    if args.model_path and os.path.exists(args.model_path):
        state_dict = torch.load(args.model_path, map_location=device)
        if detect_arch_from_state_dict(state_dict) == arch:
            try:
                model.load_state_dict(state_dict, strict=True)
                print(f"Loaded initial model from {args.model_path}")
            except RuntimeError:
                load_state_dict_flexible(model, state_dict)
        else:
            load_state_dict_flexible(model, state_dict)
        del state_dict
    vram_manager.log_vram("bc_after_load")

    optimizer = optim.Adam(model.parameters(), lr=args.bc_learning_rate)
    scaler = vram_manager.build_grad_scaler(True)
    amp_enabled = vram_manager.amp_supported(device)

    print(f"Behavior cloning samples: {dataset.stats()}")
    print(f"Samples source: {source}")
    print(f"obs_size={obs_size} arch={arch} amp={amp_enabled} device={device}")

    for epoch in range(1, args.bc_epochs + 1):
        model.train()
        totals = {"loss": 0.0, "move": 0.0, "angle": 0.0, "info": 0.0, "attack": 0.0, "coarse": 0.0}
        batch_count = 0

        for images, actions, _is_human in dataloader:
            images = images.to(device, non_blocking=True)
            actions = actions.to(device)

            with vram_manager.autocast_context(amp_enabled):
                outputs = model(images)
                loss = action_bc_loss(outputs, actions)

            optimizer.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            if args.grad_clip_norm > 0:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip_norm)
            scaler.step(optimizer)
            scaler.update()

            move_acc, angle_acc, info_acc, attack_acc, coarse_acc = branch_accuracy(outputs, actions)
            totals["loss"] += float(loss.item())
            totals["move"] += move_acc
            totals["angle"] += angle_acc
            totals["info"] += info_acc
            totals["attack"] += attack_acc
            totals["coarse"] += coarse_acc
            batch_count += 1

            del images, actions, outputs, loss
            vram_manager.maybe_empty_cache(batch_count, args.vram_empty_cache_interval_steps, tag="bc", log=False)

        batch_count = max(1, batch_count)
        print(
            f"epoch={epoch} "
            f"loss={totals['loss'] / batch_count:.4f} "
            f"move_acc={totals['move'] / batch_count:.3f} "
            f"angle_acc={totals['angle'] / batch_count:.3f} "
            f"info_acc={totals['info'] / batch_count:.3f} "
            f"attack_acc={totals['attack'] / batch_count:.3f} "
            f"coarse_acc={totals['coarse'] / batch_count:.3f} "
            f"{vram_manager.format_vram()}"
        )

    output_dir = os.path.dirname(args.bc_model_out)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    torch.save(model.state_dict(), args.bc_model_out)
    print(f"Behavior cloning model saved to {args.bc_model_out}")
    print("下一步：python train.py --model_path " + str(args.bc_model_out) + " --assist_mode assist")


def _resolve_arch():
    """与 `dqnAgent` 保持一致：`auto` 默认用 compact，旧 checkpoint 做部分迁移。"""
    requested = str(getattr(args, "net_arch", "auto") or "auto").lower()
    if requested in ("compact", "legacy"):
        return requested
    model_path = getattr(args, "model_path", "") or ""
    if model_path and os.path.exists(model_path):
        try:
            state_dict = torch.load(model_path, map_location="cpu")
            arch = detect_arch_from_state_dict(state_dict)
            del state_dict
            if arch == "legacy":
                print("[bc] 检测到 legacy checkpoint，将把 conv/动作头迁移到 compact 结构")
        except (OSError, RuntimeError, ValueError):
            pass
    return "compact"


if __name__ == "__main__":
    train_behavior_clone()
