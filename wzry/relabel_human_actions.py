import json
import os
from collections import Counter

from argparses import args
from touch_event_parser import TouchActionMapper


ZERO_ACTION = (0, 0, 0, 0, 0, 0, 0, 0)


def resolve_episode_dir():
    if args.relabel_episode_dir:
        return os.path.abspath(args.relabel_episode_dir)

    latest_path = os.path.join(args.human_data_dir, "latest_episode.txt")
    if not os.path.exists(latest_path):
        raise RuntimeError("No relabel_episode_dir and no human_data/latest_episode.txt")

    with open(latest_path, "r", encoding="utf-8") as file:
        episode_dir = file.read().strip()
    if not episode_dir:
        raise RuntimeError("human_data/latest_episode.txt is empty")
    return os.path.abspath(episode_dir)


def load_metadata(episode_dir):
    metadata_path = os.path.join(episode_dir, "metadata.json")
    if not os.path.exists(metadata_path):
        raise RuntimeError(f"metadata.json not found: {metadata_path}")
    with open(metadata_path, "r", encoding="utf-8") as file:
        return json.load(file)


def resolve_paths(episode_dir):
    samples_path = os.path.join(episode_dir, "samples.jsonl")
    if not os.path.exists(samples_path):
        raise RuntimeError(f"samples.jsonl not found: {samples_path}")

    if args.relabel_in_place:
        output_path = samples_path + ".tmp"
    elif args.relabel_output_path:
        output_path = os.path.abspath(args.relabel_output_path)
    else:
        output_path = os.path.join(episode_dir, "samples_relabel.jsonl")

    return samples_path, output_path


def relabel_human_actions():
    episode_dir = resolve_episode_dir()
    metadata = load_metadata(episode_dir)
    samples_path, output_path = resolve_paths(episode_dir)

    mapper = TouchActionMapper(
        screen_width=int(metadata["screen_width"]),
        screen_height=int(metadata["screen_height"]),
    )

    action_counter = Counter()
    move_counter = Counter()
    info_counter = Counter()
    attack_counter = Counter()
    total = 0
    nonzero = 0
    touch_frames = 0

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(samples_path, "r", encoding="utf-8") as source, open(output_path, "w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue

            record = json.loads(line)
            touch_samples = record.get("touch_samples") or []
            action, diagnostics = mapper.samples_to_action(touch_samples)
            record["parsed_action"] = action
            record["action_diagnostics"] = diagnostics
            record["relabel_transform"] = {
                "raw_width": args.touch_raw_width,
                "raw_height": args.touch_raw_height,
                "swap_xy": args.touch_swap_xy,
                "invert_x": args.touch_invert_x,
                "invert_y": args.touch_invert_y,
                "hit_radius_ratio": args.touch_hit_radius_ratio,
                "move_deadzone_ratio": args.touch_move_deadzone_ratio,
            }
            target.write(json.dumps(record, ensure_ascii=False) + "\n")

            total += 1
            action_tuple = tuple(action)
            action_counter.update([action_tuple])
            if touch_samples:
                touch_frames += 1
            if action_tuple != ZERO_ACTION:
                nonzero += 1
            move_counter.update([action[0]])
            info_counter.update([action[2]])
            attack_counter.update([action[3]])

    if args.relabel_in_place:
        os.replace(output_path, samples_path)
        output_path = samples_path

    print(f"episode_dir={episode_dir}")
    print(f"input={samples_path}")
    print(f"output={output_path}")
    print(f"total_frames={total}, touch_frames={touch_frames}, nonzero_actions={nonzero}")
    print(f"move={move_counter}")
    print(f"info={info_counter}")
    print(f"attack={attack_counter}")
    print(f"top_actions={action_counter.most_common(max(1, args.relabel_preview_limit))}")


if __name__ == "__main__":
    relabel_human_actions()
