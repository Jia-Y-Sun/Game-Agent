import json
import os

import cv2
import torch
import torch.nn.functional as F
from torch import optim
from torch.utils.data import DataLoader, Dataset

from argparses import args, device
from net_actor import NetDQN


ACTION_SIZES = [2, 360, 9, 11, 3, 360, 100, 5]


def resolve_samples_path():
    if args.bc_samples_path:
        return args.bc_samples_path

    for latest_name in ["latest_frame_episode.txt", "latest_episode.txt"]:
        latest_path = os.path.join(args.human_data_dir, latest_name)
        if os.path.exists(latest_path):
            with open(latest_path, "r", encoding="utf-8") as file:
                episode_dir = file.read().strip()
            if episode_dir:
                return os.path.join(episode_dir, "samples.jsonl")

    return os.path.join(args.human_data_dir, "samples.jsonl")


class HumanActionDataset(Dataset):
    def __init__(self, samples_path):
        self.samples_path = samples_path
        self.records = []
        with open(samples_path, "r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                record = json.loads(line)
                frame_path = record.get("frame_path")
                action = record.get("parsed_action")
                if frame_path and action and os.path.exists(frame_path):
                    self.records.append((frame_path, self._clamp_action(action)))

        if not self.records:
            raise RuntimeError(f"No valid behavior-cloning samples found in {samples_path}")

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        frame_path, action = self.records[index]
        image = cv2.imread(frame_path)
        if image is None:
            raise RuntimeError(f"Failed to read image: {frame_path}")
        image = cv2.resize(image, (640, 640))
        image = torch.from_numpy(image).float().permute(2, 0, 1) / 255.0
        return image, torch.LongTensor(action)

    @staticmethod
    def _clamp_action(action):
        values = [int(value) for value in action[:8]]
        while len(values) < 8:
            values.append(0)
        return [
            max(0, min(size - 1, value))
            for value, size in zip(values, ACTION_SIZES)
        ]


def masked_ce(logits, labels, mask):
    if not bool(mask.any()):
        return logits.sum() * 0.0
    return F.cross_entropy(logits[mask], labels[mask])


def behavior_clone_loss(outputs, actions):
    move_logits, angle_logits, info_logits, attack_logits, action_type_logits, arg1_logits, arg2_logits, arg3_logits = outputs

    move_mask = actions[:, 0] != 0
    skill_mask = actions[:, 3] >= 8
    directional_skill_mask = skill_mask & (actions[:, 4] == 1)
    long_press_skill_mask = skill_mask & (actions[:, 4] == 2)

    loss = F.cross_entropy(move_logits, actions[:, 0])
    loss = loss + masked_ce(angle_logits, actions[:, 1], move_mask)
    loss = loss + F.cross_entropy(info_logits, actions[:, 2])
    loss = loss + F.cross_entropy(attack_logits, actions[:, 3])
    loss = loss + masked_ce(action_type_logits, actions[:, 4], skill_mask)
    loss = loss + masked_ce(arg1_logits, actions[:, 5], directional_skill_mask)
    loss = loss + masked_ce(arg2_logits, actions[:, 6], directional_skill_mask)
    loss = loss + masked_ce(arg3_logits, actions[:, 7], long_press_skill_mask)
    return loss


def branch_accuracy(outputs, actions):
    with torch.no_grad():
        predictions = [torch.argmax(output, dim=1) for output in outputs]
        move_acc = (predictions[0] == actions[:, 0]).float().mean().item()
        info_acc = (predictions[2] == actions[:, 2]).float().mean().item()
        attack_acc = (predictions[3] == actions[:, 3]).float().mean().item()
        coarse_acc = (
            (predictions[0] == actions[:, 0])
            & (predictions[2] == actions[:, 2])
            & (predictions[3] == actions[:, 3])
        ).float().mean().item()
    return move_acc, info_acc, attack_acc, coarse_acc


def train_behavior_clone():
    samples_path = resolve_samples_path()
    dataset = HumanActionDataset(samples_path)
    dataloader = DataLoader(
        dataset,
        batch_size=max(1, args.bc_batch_size),
        shuffle=True,
        num_workers=max(0, args.bc_num_workers),
        pin_memory=torch.cuda.is_available(),
    )

    model = NetDQN().to(device)
    if args.model_path and os.path.exists(args.model_path):
        model.load_state_dict(torch.load(args.model_path, map_location=device), strict=False)
        print(f"Loaded initial model from {args.model_path}")

    optimizer = optim.Adam(model.parameters(), lr=args.bc_learning_rate)
    print(f"Behavior cloning samples: {len(dataset)}")
    print(f"Samples file: {samples_path}")

    for epoch in range(1, args.bc_epochs + 1):
        model.train()
        total_loss = 0.0
        total_move_acc = 0.0
        total_info_acc = 0.0
        total_attack_acc = 0.0
        total_coarse_acc = 0.0
        batch_count = 0

        for images, actions in dataloader:
            images = images.to(device)
            actions = actions.to(device)

            outputs = model(images)
            loss = behavior_clone_loss(outputs, actions)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            move_acc, info_acc, attack_acc, coarse_acc = branch_accuracy(outputs, actions)
            total_loss += float(loss.item())
            total_move_acc += move_acc
            total_info_acc += info_acc
            total_attack_acc += attack_acc
            total_coarse_acc += coarse_acc
            batch_count += 1

        print(
            f"epoch={epoch} "
            f"loss={total_loss / batch_count:.4f} "
            f"move_acc={total_move_acc / batch_count:.3f} "
            f"info_acc={total_info_acc / batch_count:.3f} "
            f"attack_acc={total_attack_acc / batch_count:.3f} "
            f"coarse_acc={total_coarse_acc / batch_count:.3f}"
        )

    output_dir = os.path.dirname(args.bc_model_out)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    torch.save(model.state_dict(), args.bc_model_out)
    print(f"Behavior cloning model saved to {args.bc_model_out}")


if __name__ == "__main__":
    train_behavior_clone()
