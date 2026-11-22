"""DQN 智能体。

相对原版的主要改动都围绕两件事：**整场人为辅助训练** 和 **显存/内存占用**。

显存相关
--------
1. 观测先在 CPU 上缩到 `--obs_size`（默认 160）再入库，网络只在
   小图上做卷积；原版直接推理 640x640，激活值和计算量都大一个数量级。
2. 网络默认用 `compact` 结构（见 `net_actor.py`），参数量 1.99 亿 -> 约 110 万。
3. 训练默认开启 AMP（`--amp true`），卷积激活值再减半。
4. 整个 batch 先拼成一个 uint8 numpy 数组，再**一次性**搬到 GPU，
   避免原版「逐张图像单独 `.to(device)` 再 `torch.stack`」造成的碎片与重复拷贝。
5. target 网络前向包在 `torch.no_grad()` 里，并且只为非终止样本计算。
6. 反向传播前做梯度裁剪，避免 fp16 下的偶发爆炸；周期性按需 `empty_cache`。

人为辅助相关
------------
* 经验里额外记录 `human_action`（人这一步做了什么）。人为接管的经验会按
  `--assist_human_sample_fraction` 的比例被优先采样；
* `--human_aux_bc_weight` 会在同一批次上追加一个“模仿人为动作”的交叉熵辅助损失，
  让 DQN 的策略在被人工纠正过的状态上直接向人的动作靠拢。
"""

import json
import os

import cv2
import numpy as np
import torch
from torch import optim, nn

from argparses import device, args, globalInfo
from gameplay_policy import GameplayBehaviorPrior
from memory import Transition
from net_actor import (
    ARCH_CHOICES,
    NetDQN,
    build_model,
    detect_arch_from_state_dict,
    load_state_dict_flexible,
)
import vram_manager


ACTION_SIZES = (2, 360, 9, 11, 3, 360, 100, 5)


def _masked_cross_entropy(logits, labels, mask):
    if not bool(mask.any()):
        return logits.sum() * 0.0
    return nn.functional.cross_entropy(logits[mask], labels[mask])


def action_bc_loss(outputs, actions):
    """从 8 个动作头的 logits 计算行为克隆损失（只在有意义的样本上算）。"""
    move_logits, angle_logits, info_logits, attack_logits, action_type_logits, arg1_logits, arg2_logits, arg3_logits = outputs

    move_mask = actions[:, 0] != 0
    skill_mask = actions[:, 3] >= 8
    directional_skill_mask = skill_mask & (actions[:, 4] == 1)
    long_press_skill_mask = skill_mask & (actions[:, 4] == 2)

    loss = nn.functional.cross_entropy(move_logits, actions[:, 0])
    loss = loss + _masked_cross_entropy(angle_logits, actions[:, 1], move_mask)
    loss = loss + nn.functional.cross_entropy(info_logits, actions[:, 2])
    loss = loss + nn.functional.cross_entropy(attack_logits, actions[:, 3])
    loss = loss + _masked_cross_entropy(action_type_logits, actions[:, 4], skill_mask)
    loss = loss + _masked_cross_entropy(arg1_logits, actions[:, 5], directional_skill_mask)
    loss = loss + _masked_cross_entropy(arg2_logits, actions[:, 6], directional_skill_mask)
    loss = loss + _masked_cross_entropy(arg3_logits, actions[:, 7], long_press_skill_mask)
    return loss


class DQNAgent:
    def __init__(self):
        self.action_sizes = list(ACTION_SIZES)
        self.device = device
        self.batch_size = args.batch_size
        self.gamma = args.gamma
        self.epsilon = args.epsilon
        self.epsilon_decay = args.epsilon_decay
        self.epsilon_min = args.epsilon_min
        self.learning_rate = args.learning_rate

        self.steps_done = 0
        self.target_update = max(1, args.target_update)
        self.epsilon_decay_mode = str(getattr(args, "epsilon_decay_mode", "step") or "step").lower()
        self.target_update_mode = str(getattr(args, "target_update_mode", "step") or "step").lower()
        self.replay_count = 0
        self.env_steps = 0
        self.behavior_prior = GameplayBehaviorPrior()
        self.last_policy_info = {"source": "init", "macro": None}

        self.obs_size = max(64, int(getattr(args, "obs_size", 160)))
        self.obs_interpolation = (
            cv2.INTER_AREA if str(getattr(args, "obs_resize_method", "area")).lower() == "area" else cv2.INTER_LINEAR
        )
        self.amp_enabled = bool(getattr(args, "amp", True)) and vram_manager.amp_supported(self.device)
        self.amp_inference = bool(getattr(args, "amp_inference", True)) and self.amp_enabled
        self.grad_clip_norm = float(getattr(args, "grad_clip_norm", 10.0) or 0.0)
        self.td_target_clip = float(getattr(args, "td_target_clip", 100.0) or 0.0)
        self.pin_memory = bool(getattr(args, "pin_memory", True)) and self.device.type == "cuda"
        self.human_sample_fraction = float(getattr(args, "assist_human_sample_fraction", 0.35) or 0.0)
        self.human_sample_boost = float(getattr(args, "assist_human_sample_boost", 1.0) or 1.0)
        self.human_aux_bc_weight = float(getattr(args, "human_aux_bc_weight", 0.5) or 0.0)
        self.vram_log_interval = int(getattr(args, "vram_log_interval_steps", 0) or 0)
        self.vram_empty_cache_interval = int(getattr(args, "vram_empty_cache_interval_steps", 0) or 0)

        self.arch = self._resolve_arch()
        if self.arch == "legacy" and self.obs_size != 640:
            print(f"[dqn] legacy 结构要求 640x640 输入，已把 obs_size 从 {self.obs_size} 调整为 640")
            self.obs_size = 640
        self.policy_net = build_model(arch=self.arch, input_size=self.obs_size, device=self.device)
        self.target_net = build_model(arch=self.arch, input_size=self.obs_size, device=self.device)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()
        for parameter in self.target_net.parameters():
            parameter.requires_grad_(False)

        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=self.learning_rate)
        self.criterion = nn.SmoothL1Loss()
        self.scaler = vram_manager.build_grad_scaler(self.amp_enabled)

        self.load_report = {}
        if args.model_path and os.path.exists(args.model_path):
            self.load_model(args.model_path)
        else:
            print(f"[dqn] 未找到初始模型 {args.model_path}，从随机初始化开始")

        self.last_replay_info = {}
        vram_manager.report_startup(self.device)

    # ------------------------------------------------------------------ #
    # 模型结构与加载
    # ------------------------------------------------------------------ #
    def _resolve_arch(self):
        """决定用哪种网络结构。

        `--net_arch auto`（默认）= `compact`。旧 checkpoint 会被自动识别成 `legacy`
        并做**部分权重迁移**（conv 主干 + 动作头），而不是把 1.99 亿参数的旧结构
        原样搬回来——否则显存优化就失效了。
        """
        requested = str(getattr(args, "net_arch", "auto") or "auto").lower()
        if requested in ARCH_CHOICES:
            return requested

        model_path = getattr(args, "model_path", "") or ""
        if model_path and os.path.exists(model_path):
            try:
                state_dict = torch.load(model_path, map_location="cpu")
                detected = detect_arch_from_state_dict(state_dict)
                del state_dict
                if detected == "legacy":
                    print(
                        "[dqn] 检测到旧版 legacy checkpoint，将把 conv 主干与动作头"
                        "迁移到 compact 结构（fc 层无法迁移，重新初始化）"
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"[dqn] 检测 checkpoint 结构失败({exc})，仍使用 compact")
        return "compact"

    @staticmethod
    def _check_reward_scale_meta(path):
        """checkpoint 的奖励尺度与当前不一致时给出提示。

        Q 值大小直接跟着奖励尺度走：用 `--reward_scale 1.0` 训出来的模型（Q≈100）
        再用 `--reward_scale 0.1` 继续训练，会顶在 TD 裁剪上限上下不来。
        """
        meta_path = path + ".meta.json"
        if not os.path.exists(meta_path):
            return
        try:
            with open(meta_path, "r", encoding="utf-8") as file:
                meta = json.load(file)
        except (OSError, ValueError):
            return
        saved_scale = meta.get("reward_scale")
        current_scale = float(getattr(args, "reward_scale", 1.0) or 1.0)
        if saved_scale is None:
            return
        try:
            saved_scale = float(saved_scale)
        except (TypeError, ValueError):
            return
        if abs(saved_scale - current_scale) < 1e-6:
            return
        print("")
        print(f"[dqn] 警告：checkpoint 的奖励尺度是 {saved_scale}，当前是 {current_scale}。")
        print("       Q 值量级会不匹配（可能一直顶在 TD 裁剪上限），建议二选一：")
        print(f"         1) 换新模型从头训：--model_path src/none.pt")
        print(f"         2) 沿用旧尺度：--reward_scale {saved_scale}")
        print("")

    def load_model(self, path):
        # 注意：先在 CPU 上读 checkpoint，再把张量逐个拷进显存里的参数。
        # 原版 `map_location=self.device` 会把 762MB 的旧权重整块塞进显存，
        # 峰值直接多占 0.7GB；CPU 中转后峰值只剩模型本身的大小。
        self._check_reward_scale_meta(path)
        try:
            state_dict = torch.load(path, map_location="cpu")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"[dqn] 模型加载失败: {exc}")
            return
        if not isinstance(state_dict, dict):
            print(f"[dqn] 模型格式异常: {path}")
            return

        source_arch = detect_arch_from_state_dict(state_dict)
        if source_arch != self.arch:
            self.load_report = load_state_dict_flexible(self.policy_net, state_dict)
            print(
                f"[dqn] 权重跨结构迁移完成，迁移比例 "
                f"{self.load_report.get('transferred_ratio', 0):.1%}；"
                "被跳过的层保持随机初始化"
            )
            print(
                f"[dqn] 提示：checkpoint 是 {source_arch}，当前是 {self.arch}，这不是无缝续训。"
                f"想要干净地从零开始请用 --model_path src/none.pt"
            )
        else:
            try:
                self.policy_net.load_state_dict(state_dict, strict=True)
                print(f"[dqn] 模型已加载（严格匹配）: {path}")
            except RuntimeError:
                self.load_report = load_state_dict_flexible(self.policy_net, state_dict)

        self.target_net.load_state_dict(self.policy_net.state_dict())
        del state_dict
        vram_manager.release_unused()
        vram_manager.log_vram("after_load_model")

    def save_model(self, path):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        torch.save(self.policy_net.state_dict(), path)
        meta = {
            "arch": self.arch,
            "obs_size": self.obs_size,
            "steps_done": self.steps_done,
            "epsilon": round(float(self.epsilon), 6),
            "reward_scale": float(getattr(args, "reward_scale", 1.0) or 1.0),
            "td_target_clip": float(getattr(args, "td_target_clip", 0.0) or 0.0),
            "params": sum(parameter.numel() for parameter in self.policy_net.parameters()),
        }
        try:
            with open(path + ".meta.json", "w", encoding="utf-8") as file:
                json.dump(meta, file, ensure_ascii=False, indent=2)
        except OSError:
            pass
        print(f"Model saved to {path} ({meta})")

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def to_observation(self, frame):
        """把整屏截图压成 `obs_size x obs_size x 3` 的 uint8 观测；入库只存这一份。"""
        if frame is None:
            return np.zeros((self.obs_size, self.obs_size, 3), dtype=np.uint8)
        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8, copy=False)
        if frame.shape[0] == self.obs_size and frame.shape[1] == self.obs_size:
            return frame
        return cv2.resize(frame, (self.obs_size, self.obs_size), interpolation=self.obs_interpolation)

    def _obs_batch_tensor(self, observations):
        """把一批 uint8 HWC 观测拼成 float NCHW 张量，只做一次主机到设备拷贝。"""
        if isinstance(observations, np.ndarray) and observations.ndim == 4:
            stacked = np.ascontiguousarray(observations)
        else:
            stacked = np.ascontiguousarray(np.stack(observations))

        tensor = torch.from_numpy(stacked)
        if self.pin_memory:
            try:
                tensor = tensor.pin_memory()
            except (RuntimeError, ValueError):
                pass
        tensor = tensor.to(self.device, non_blocking=self.pin_memory)
        tensor = tensor.permute(0, 3, 1, 2).float().div_(255.0)
        return tensor

    # ------------------------------------------------------------------ #
    # 动作选择
    # ------------------------------------------------------------------ #
    def reset_episode(self):
        self.behavior_prior.reset_episode()
        self.last_policy_info = {"source": "episode_reset", "macro": None}

    # ------------------------------------------------------------------ #
    # 按环境步调度的探索率与目标网络（P2）
    # ------------------------------------------------------------------ #
    def decay_epsilon(self):
        if self.epsilon > self.epsilon_min:
            self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)

    def sync_target(self):
        self.target_net.load_state_dict(self.policy_net.state_dict())

    def on_env_step(self):
        """每个环境步调用一次：推进 epsilon 衰减与目标网络同步。"""
        self.env_steps += 1
        if self.epsilon_decay_mode == "step":
            self.decay_epsilon()
        if self.target_update_mode == "step" and self.env_steps % self.target_update == 0:
            self.sync_target()

    def select_action(self, state, obs=None):
        if np.random.rand() <= self.epsilon:
            return self._sample_exploration_action()

        observation = obs if obs is not None else self.to_observation(state)
        batch = self._obs_batch_tensor(observation[None, ...])
        self.policy_net.eval()
        # 用 no_grad 而不是 inference_mode：AMP 推理时 autocast 会把 fp16 权重缓存到模块/算子缓存里，
        # 若这些缓存是「inference tensor」，另一个线程（训练线程）的 forward 再复用它就会报
        # "Inference tensors cannot be saved for backward"。no_grad 同样不建图，但张量是普通张量。
        with torch.no_grad():
            with vram_manager.autocast_context(self.amp_inference):
                q_values = self.policy_net(batch)
        self.last_policy_info = {"source": "model", "macro": None}
        action = [int(np.argmax(q[0].detach().cpu().numpy())) for q in q_values]
        del q_values, batch
        return self._normalize_action(action)

    def _sample_exploration_action(self):
        if args.behavior_prior_mode == "smart":
            action, macro = self.behavior_prior.sample()
            self.last_policy_info = {"source": "smart_prior", "macro": macro}
            return self._normalize_action(action)

        if args.behavior_prior_mode == "uniform":
            self.last_policy_info = {"source": "uniform", "macro": None}
            return self._normalize_action([np.random.randint(size) for size in self.sizes])

        self.last_policy_info = {"source": "weighted", "macro": None}
        move_action = int(np.random.choice([0, 1], p=[0.25, 0.75]))
        angle = int(np.random.randint(360)) if move_action == 1 else 0

        info_action = int(np.random.choice(
            list(range(9)),
            p=[0.70, 0.02, 0.02, 0.02, 0.02, 0.02, 0.07, 0.07, 0.06],
        ))
        attack_action = int(np.random.choice(
            list(range(11)),
            p=[0.10, 0.35, 0.10, 0.05, 0.02, 0.02, 0.04, 0.06, 0.10, 0.08, 0.08],
        ))

        action_type = 0
        arg1 = 0
        arg2 = 0
        arg3 = 0
        if attack_action >= 7:
            action_type = int(np.random.choice([0, 1, 2], p=[0.70, 0.25, 0.05]))
            arg1 = int(np.random.randint(360))
            arg2 = int(np.random.randint(100))
            arg3 = int(np.random.randint(5))

        return self._normalize_action([
            move_action,
            angle,
            info_action,
            attack_action,
            action_type,
            arg1,
            arg2,
            arg3,
        ])

    @property
    def sizes(self):
        return self.action_sizes

    def _normalize_action(self, action):
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = [int(v) for v in action]
        if move_action == 0:
            angle = 0
        if attack_action == 0 or attack_action < 7:
            action_type = 0
            arg1 = 0
            arg2 = 0
            arg3 = 0
        return [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]

    # ------------------------------------------------------------------ #
    # 训练
    # ------------------------------------------------------------------ #
    def replay(self):
        transitions = globalInfo.random_batch_size_memory_dqn(
            human_fraction=self.human_sample_fraction,
            human_boost=self.human_sample_boost,
        )
        if not transitions:
            return None

        batch = Transition(*zip(*transitions))
        batch_size = len(transitions)

        batch_state = self._obs_batch_tensor(list(batch.state))
        batch_action = torch.as_tensor(
            np.asarray(batch.action, dtype=np.int64), device=self.device
        )
        batch_reward = torch.as_tensor(
            np.asarray(batch.reward, dtype=np.float32), device=self.device
        )
        batch_done = torch.as_tensor(
            np.asarray(batch.done, dtype=np.float32), device=self.device
        )

        self.policy_net.train()
        with vram_manager.autocast_context(self.amp_enabled):
            outputs = self.policy_net(batch_state)
            # AMP 下 8 个头输出是 fp16；统一转 fp32 再算 TD，
            # 否则 index_put / SmoothL1 会因 dtype 不一致直接报错。
            state_action_q_values = self._gather_action_q(outputs, batch_action).float()

            next_state_values = torch.zeros(batch_size, device=self.device, dtype=torch.float32)
            non_final_mask = batch_done == 0
            non_final_count = int(non_final_mask.sum().item())
            if non_final_count > 0:
                non_final_observations = [
                    observation for observation, keep in zip(batch.next_state, batch_done.tolist()) if keep == 0
                ]
                batch_next_state = self._obs_batch_tensor(non_final_observations)
                with torch.no_grad():
                    next_outputs = self.target_net(batch_next_state)
                    next_state_values[non_final_mask] = self._max_action_q(next_outputs).float()
                del batch_next_state, next_outputs

            expected_state_action_values = batch_reward + self.gamma * next_state_values * (1 - batch_done)
            # 【稳定性】8 个动作头的 Q 相加、每头又对 360 类取 max，
            # 天然容易过估计；自举一旦跑飞会把 Q 推到几千、loss 到 1e7。
            # 这里给 TD 目标加上限，切断正反馈（--td_target_clip，0 表示关闭）。
            if self.td_target_clip and self.td_target_clip > 0:
                expected_state_action_values = expected_state_action_values.clamp(
                    -self.td_target_clip, self.td_target_clip
                )
            td_loss = self.criterion(state_action_q_values, expected_state_action_values.unsqueeze(1))

            aux_loss, human_count = self._human_auxiliary_loss(outputs, transitions)
            loss = td_loss + aux_loss

        self.optimizer.zero_grad(set_to_none=True)
        self.scaler.scale(loss).backward()
        if self.grad_clip_norm > 0:
            self.scaler.unscale_(self.optimizer)
            torch.nn.utils.clip_grad_norm_(self.policy_net.parameters(), self.grad_clip_norm)
        self.scaler.step(self.optimizer)
        self.scaler.update()

        # 【P2】epsilon 与目标网络默认改为「按环境步」调度（见 train.py 的 step 回调）。
        # 旧行为按训练迭代衰减，训练线程满速时会在一分钟内把 epsilon 打到下限。
        if self.epsilon_decay_mode == "replay":
            self.decay_epsilon()

        if self.target_update_mode == "replay" and self.replay_count % self.target_update == 0:
            self.sync_target()

        self.steps_done += 1
        self.replay_count += 1

        info = {
            "loss": float(td_loss.detach().item()),
            "aux_loss": float(aux_loss.detach().item()),
            "q_absmax": float(state_action_q_values.detach().abs().max().item()),
            "target_absmax": float(expected_state_action_values.detach().abs().max().item()),
            "human_in_batch": int(human_count),
            "batch_size": int(batch_size),
            "epsilon": round(float(self.epsilon), 5),
            "amp": bool(self.amp_enabled),
            "arch": self.arch,
        }
        self.last_replay_info = info
        del batch_state, outputs, state_action_q_values, expected_state_action_values, next_state_values
        del batch_action, batch_reward, batch_done, td_loss, aux_loss, loss
        vram_manager.maybe_empty_cache(
            self.steps_done,
            self.vram_empty_cache_interval,
            tag="replay",
            log=False,
        )
        return info

    def _gather_action_q(self, outputs, batch_action):
        move_q, angle_q, info_q, attack_q, action_type_q, arg1_q, arg2_q, arg3_q = outputs
        return (
            move_q.gather(1, batch_action[:, 0].unsqueeze(1))
            + angle_q.gather(1, batch_action[:, 1].unsqueeze(1))
            + info_q.gather(1, batch_action[:, 2].unsqueeze(1))
            + attack_q.gather(1, batch_action[:, 3].unsqueeze(1))
            + action_type_q.gather(1, batch_action[:, 4].unsqueeze(1))
            + arg1_q.gather(1, batch_action[:, 5].unsqueeze(1))
            + arg2_q.gather(1, batch_action[:, 6].unsqueeze(1))
            + arg3_q.gather(1, batch_action[:, 7].unsqueeze(1))
        )

    @staticmethod
    def _max_action_q(outputs):
        total = None
        for head in outputs:
            value = torch.max(head, 1)[0]
            total = value if total is None else total + value
        return total

    def _human_auxiliary_loss(self, outputs, transitions):
        """人为示范样本上的辅助模仿损失。"""
        if self.human_aux_bc_weight <= 0:
            return torch.zeros((), device=self.device), 0

        indices = [index for index, item in enumerate(transitions) if item.human_action is not None]
        if not indices:
            return torch.zeros((), device=self.device), 0

        labels = torch.as_tensor(
            np.asarray([transitions[index].human_action for index in indices], dtype=np.int64),
            device=self.device,
        )
        index_tensor = torch.as_tensor(indices, device=self.device)
        selected = tuple(head.index_select(0, index_tensor) for head in outputs)
        loss = action_bc_loss(selected, labels) * self.human_aux_bc_weight
        return loss, len(indices)

    # ------------------------------------------------------------------ #
    # 观测上报
    # ------------------------------------------------------------------ #
    def report(self):
        memory_stats = globalInfo.dqn_memory_stats()
        return {
            "arch": self.arch,
            "obs_size": self.obs_size,
            "amp": bool(self.amp_enabled),
            "epsilon": round(float(self.epsilon), 5),
            "steps_done": int(self.steps_done),
            "memory": memory_stats,
            "last_replay": dict(self.last_replay_info),
            **vram_manager.vram_stats(),
        }
