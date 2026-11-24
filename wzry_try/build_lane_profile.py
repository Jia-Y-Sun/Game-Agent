import argparse
import json
import math
import os
from collections import Counter


ZERO_ACTION = [0, 0, 0, 0, 0, 0, 0, 0]
DEFAULT_MOVE_CENTER_RATIO = (0.173, 0.784)


def parse_args():
    parser = argparse.ArgumentParser(description="Build early-lane macro profile from human samples.")
    parser.add_argument("--samples_path", default="", help="Path to samples.jsonl; empty means latest episode")
    parser.add_argument("--human_data_dir", default="human_data")
    parser.add_argument("--out", default="src/lane_profile.json")
    parser.add_argument("--max_steps", type=int, default=90)
    parser.add_argument("--opening_steps", type=int, default=42)
    parser.add_argument("--profile_step_sec", type=float, default=0.5,
                        help="Seconds represented by one generated macro step; use 0 to keep raw sample rate")
    parser.add_argument("--angle_tolerance", type=int, default=18)
    parser.add_argument("--min_segment_steps", type=int, default=2)
    parser.add_argument("--route_outlier_max_turn", type=int, default=100,
                        help="Remove short route segments that turn more than this many degrees from neighbors")
    parser.add_argument("--route_outlier_max_steps", type=int, default=2,
                        help="Maximum steps for a short reverse route segment to be treated as jitter")
    parser.add_argument("--min_patrol_angles", type=int, default=4,
                        help="Minimum number of patrol angles; backfill from the opening route if needed")
    parser.add_argument("--info_min_interval_steps", type=int, default=6,
                        help="Minimum interval between buy/upgrade info actions in the generated profile")
    parser.add_argument("--opening_move_only_profile", action="store_true",
                        help="Build a pure movement opening profile from a no-attack demonstration")
    parser.add_argument("--opening_route_max_segments", type=int, default=2,
                        help="Maximum number of movement route segments for pure opening profiles")
    parser.add_argument("--opening_min_move_ratio", type=float, default=0.35,
                        help="Minimum move-action ratio required to trust a pure movement profile")
    parser.add_argument("--opening_touch_route", action="store_true",
                        help="Infer opening movement directly from touch samples instead of parsed actions")
    parser.add_argument("--opening_touch_zone_ratio", type=float, default=0.50,
                        help="Broad joystick zone ratio used when inferring pure movement from touches")
    parser.add_argument("--opening_touch_deadzone_ratio", type=float, default=0.015,
                        help="Deadzone ratio used when inferring pure movement from touches")
    parser.add_argument("--move_center_x", type=float, default=DEFAULT_MOVE_CENTER_RATIO[0],
                        help="Fallback screen-relative joystick center x used for touch-route inference")
    parser.add_argument("--move_center_y", type=float, default=DEFAULT_MOVE_CENTER_RATIO[1],
                        help="Fallback screen-relative joystick center y used for touch-route inference")
    return parser.parse_args()


def resolve_samples_path(args):
    if args.samples_path:
        return os.path.abspath(args.samples_path)

    latest_path = os.path.join(args.human_data_dir, "latest_action_episode.txt")
    if not os.path.exists(latest_path):
        latest_path = os.path.join(args.human_data_dir, "latest_episode.txt")
    if not os.path.exists(latest_path):
        raise RuntimeError("No samples_path and no human_data/latest_action_episode.txt")

    with open(latest_path, "r", encoding="utf-8") as file:
        episode_dir = file.read().strip()
    if not episode_dir:
        raise RuntimeError("human_data/latest_episode.txt is empty")
    return os.path.join(episode_dir, "samples.jsonl")


def load_records(samples_path):
    records = []
    with open(samples_path, "r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            record = json.loads(line)
            action = record.get("parsed_action") or ZERO_ACTION
            if len(action) < 8:
                action = list(action) + [0] * (8 - len(action))
            record["parsed_action"] = [int(value) for value in action[:8]]
            records.append(record)
    if not records:
        raise RuntimeError(f"No valid samples in {samples_path}")
    return records


def load_metadata(samples_path):
    metadata_path = os.path.join(os.path.dirname(os.path.abspath(samples_path)), "metadata.json")
    if not os.path.exists(metadata_path):
        return {}
    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError, TypeError):
        return {}


def _record_ts(record, fallback_index):
    try:
        return float(record.get("timestamp", fallback_index))
    except (TypeError, ValueError):
        return float(fallback_index)


def _best_attack(actions):
    priority = {1: 1, 2: 2, 3: 3, 8: 4, 9: 5, 10: 6}
    best = ZERO_ACTION
    best_score = -1
    for action in actions:
        attack_action = int(action[3])
        if attack_action == 0:
            continue
        score = priority.get(attack_action, 1)
        if score >= best_score:
            best = action
            best_score = score
    return best


def _merge_bin_records(records):
    merged = dict(records[-1])
    actions = [record["parsed_action"] for record in records]
    touch_samples = []
    for record in records:
        touch_samples.extend(record.get("touch_samples") or [])
    action = [0, 0, 0, 0, 0, 0, 0, 0]

    for item in actions:
        if item[0] != 0:
            action[0] = item[0]
            action[1] = item[1]
        if item[2] != 0:
            action[2] = item[2]

    attack_action = _best_attack(actions)
    action[3:] = attack_action[3:]
    merged["parsed_action"] = [int(value) for value in action]
    merged["timestamp"] = _record_ts(records[0], 0)
    merged["source_record_count"] = len(records)
    merged["touch_samples"] = touch_samples
    return merged


def resample_records(records, step_sec, max_steps):
    if step_sec <= 0:
        return records[:max_steps]

    first_ts = _record_ts(records[0], 0)
    bins = []
    current_bin = []
    current_index = None

    for fallback_index, record in enumerate(records):
        timestamp = _record_ts(record, fallback_index)
        bin_index = int(max(0.0, timestamp - first_ts) / step_sec)
        if current_index is None:
            current_index = bin_index
        if bin_index != current_index and current_bin:
            bins.append(_merge_bin_records(current_bin))
            current_bin = []
            current_index = bin_index
            if len(bins) >= max_steps:
                break
        current_bin.append(record)

    if current_bin and len(bins) < max_steps:
        bins.append(_merge_bin_records(current_bin))

    return bins[:max_steps]


def angular_distance(first, second):
    return abs((first - second + 180) % 360 - 180)


def circular_mean(angles):
    if not angles:
        return 0
    sin_sum = sum(math.sin(math.radians(angle)) for angle in angles)
    cos_sum = sum(math.cos(math.radians(angle)) for angle in angles)
    return int(round(math.degrees(math.atan2(sin_sum, cos_sum)))) % 360


def _transform_touch(sample, metadata):
    screen_width = int(metadata.get("screen_width", 2800))
    screen_height = int(metadata.get("screen_height", 1260))
    transform = metadata.get("touch_transform") or {}
    raw_x = float(sample.get("raw_x", 0))
    raw_y = float(sample.get("raw_y", 0))
    raw_width = float(transform.get("raw_width") or screen_width)
    raw_height = float(transform.get("raw_height") or screen_height)

    if transform.get("swap_xy"):
        raw_x, raw_y = raw_y, raw_x
        raw_width, raw_height = raw_height, raw_width

    x = raw_x / max(1.0, raw_width) * screen_width
    y = raw_y / max(1.0, raw_height) * screen_height

    if transform.get("invert_x"):
        x = screen_width - x
    if transform.get("invert_y"):
        y = screen_height - y

    return x, y, screen_width, screen_height


def _move_center_ratio(metadata, args):
    center = metadata.get("move_joystick_center") or {}
    try:
        x_ratio = float(center.get("x", args.move_center_x))
        y_ratio = float(center.get("y", args.move_center_y))
    except (TypeError, ValueError):
        x_ratio, y_ratio = DEFAULT_MOVE_CENTER_RATIO
    return (
        max(0.0, min(1.0, x_ratio)),
        max(0.0, min(1.0, y_ratio)),
    )


def infer_moves_from_touch_samples(records, metadata, args):
    screen_width = int(metadata.get("screen_width", 2800))
    screen_height = int(metadata.get("screen_height", 1260))
    center_ratio = _move_center_ratio(metadata, args)
    center = (screen_width * center_ratio[0], screen_height * center_ratio[1])
    short_side = min(screen_width, screen_height)
    broad_zone = short_side * float(args.opening_touch_zone_ratio)
    deadzone = short_side * float(args.opening_touch_deadzone_ratio)
    inferred_count = 0

    for record in records:
        samples = record.get("touch_samples") or []
        candidates = []
        for sample in samples:
            if sample.get("raw_x") is None or sample.get("raw_y") is None:
                continue
            x, y, _, _ = _transform_touch(sample, metadata)
            dx = x - center[0]
            dy = y - center[1]
            distance = math.hypot(dx, dy)
            if deadzone <= distance <= broad_zone:
                candidates.append((distance, dx, dy))

        if not candidates:
            continue

        _, dx, dy = max(candidates, key=lambda item: item[0])
        angle = int(round(math.degrees(math.atan2(dy, dx)))) % 360
        action = list(record.get("parsed_action") or ZERO_ACTION)
        action[0] = 1
        action[1] = angle
        action[2] = 0
        action[3] = 0
        action[4] = 0
        action[5] = 0
        action[6] = 0
        action[7] = 0
        record["parsed_action"] = action
        inferred_count += 1

    return inferred_count


def compress_angles(records, angle_tolerance, min_segment_steps):
    segments = []
    current_angles = []
    current_steps = 0

    for record in records:
        action = record["parsed_action"]
        if action[0] == 0:
            continue

        angle = int(action[1])
        if not current_angles:
            current_angles = [angle]
            current_steps = 1
            continue

        mean_angle = circular_mean(current_angles)
        if angular_distance(angle, mean_angle) <= angle_tolerance:
            current_angles.append(angle)
            current_steps += 1
        else:
            segments.append({"angle": circular_mean(current_angles), "steps": current_steps})
            current_angles = [angle]
            current_steps = 1

    if current_angles:
        segments.append({"angle": circular_mean(current_angles), "steps": current_steps})

    if not segments:
        return [{"angle": 195, "steps": 12}]

    merged = []
    for segment in segments:
        if segment["steps"] >= min_segment_steps or not merged:
            merged.append(segment)
        else:
            previous = merged[-1]
            combined_angles = [previous["angle"]] * previous["steps"] + [segment["angle"]] * segment["steps"]
            previous["angle"] = circular_mean(combined_angles)
            previous["steps"] += segment["steps"]
    return merged


def _merge_close_segments(segments, angle_tolerance):
    merged = []
    for segment in segments:
        if not merged:
            merged.append(dict(segment))
            continue

        previous = merged[-1]
        if angular_distance(previous["angle"], segment["angle"]) <= angle_tolerance:
            combined_angles = [previous["angle"]] * previous["steps"] + [segment["angle"]] * segment["steps"]
            previous["angle"] = circular_mean(combined_angles)
            previous["steps"] += segment["steps"]
        else:
            merged.append(dict(segment))
    return merged


def clean_route_segments(segments, args):
    if len(segments) < 3:
        return segments, []

    cleaned = []
    removed = []
    max_steps = max(1, int(args.route_outlier_max_steps))
    max_turn = max(45, int(args.route_outlier_max_turn))

    for index, segment in enumerate(segments):
        previous_segment = cleaned[-1] if cleaned else None
        next_segment = segments[index + 1] if index + 1 < len(segments) else None
        is_short = int(segment["steps"]) <= max_steps
        is_reverse = False

        if is_short and previous_segment is not None and next_segment is not None:
            previous_turn = angular_distance(segment["angle"], previous_segment["angle"])
            next_turn = angular_distance(segment["angle"], next_segment["angle"])
            neighbor_turn = angular_distance(previous_segment["angle"], next_segment["angle"])
            is_reverse = previous_turn >= max_turn and next_turn >= max_turn and neighbor_turn <= max_turn

        if is_reverse:
            previous_segment["steps"] += int(segment["steps"])
            removed.append(dict(segment))
            continue

        cleaned.append(dict(segment))

    return _merge_close_segments(cleaned, args.angle_tolerance), removed


def build_patrol_angles(patrol_segments, route_segments, min_count):
    angles = [item["angle"] for item in patrol_segments]
    route_tail = [item["angle"] for item in route_segments[-max(1, min_count):]]
    defaults = [175, 190, 205, 190]

    for angle in route_tail + defaults:
        if len(angles) >= min_count:
            break
        if not angles or angular_distance(angle, angles[-1]) > 8:
            angles.append(angle)

    return angles[:max(1, min_count)]


def simplify_route_segments(segments, max_segments):
    if not segments:
        return [{"angle": 195, "steps": 12}]
    max_segments = max(1, int(max_segments))
    if len(segments) <= max_segments:
        return segments

    if max_segments == 1:
        angles = []
        for segment in segments:
            angles.extend([segment["angle"]] * int(segment["steps"]))
        return [{"angle": circular_mean(angles), "steps": sum(int(item["steps"]) for item in segments)}]

    while len(segments) > max_segments:
        best_index = 0
        best_score = None
        for index in range(len(segments) - 1):
            first = segments[index]
            second = segments[index + 1]
            score = angular_distance(first["angle"], second["angle"]) * min(first["steps"], second["steps"])
            if best_score is None or score < best_score:
                best_index = index
                best_score = score
        first = segments[best_index]
        second = segments[best_index + 1]
        merged = {
            "angle": circular_mean([first["angle"]] * int(first["steps"]) + [second["angle"]] * int(second["steps"])),
            "steps": int(first["steps"]) + int(second["steps"]),
        }
        segments = segments[:best_index] + [merged] + segments[best_index + 2:]
    return segments


def estimate_interval(indices, default_value):
    if len(indices) < 2:
        return default_value
    deltas = [b - a for a, b in zip(indices, indices[1:]) if b > a]
    if not deltas:
        return default_value
    return max(2, int(round(sum(deltas) / len(deltas))))


def build_profile(records, args, samples_path, metadata=None):
    metadata = metadata or {}
    move_center_ratio = _move_center_ratio(metadata, args)
    touch_inferred_move_count = 0
    if args.opening_move_only_profile and args.opening_touch_route:
        touch_inferred_move_count = infer_moves_from_touch_samples(records, metadata, args)

    opening_records = records[:min(args.opening_steps, len(records))]
    laning_records = records[min(args.opening_steps, len(records)):] or records
    route_segments = compress_angles(opening_records, args.angle_tolerance, args.min_segment_steps)
    route_segments, removed_route_segments = clean_route_segments(route_segments, args)
    if args.opening_move_only_profile:
        route_segments = simplify_route_segments(route_segments, args.opening_route_max_segments)
    patrol_segments = compress_angles(laning_records, args.angle_tolerance, args.min_segment_steps)
    patrol_angles = build_patrol_angles(patrol_segments, route_segments, args.min_patrol_angles)

    info_schedule = []
    attack_schedule = []
    skill_steps = []
    last_hit_steps = []
    push_tower_steps = []
    attack_counter = Counter()
    info_counter = Counter()
    move_counter = Counter()
    last_info_step = -max(1, int(args.info_min_interval_steps))

    for step, record in enumerate(records):
        action = record["parsed_action"]
        move_counter.update([action[0]])
        info_counter.update([action[2]])
        attack_counter.update([action[3]])

        if action[2] in [1, 2, 6, 7, 8] and step - last_info_step >= max(1, int(args.info_min_interval_steps)):
            info_schedule.append({"step": step, "action": action[2]})
            last_info_step = step
        if action[3] != 0:
            attack_schedule.append({
                "step": step,
                "action": action[3],
                "action_type": action[4],
                "arg1": action[5],
                "arg2": action[6],
                "arg3": action[7],
            })
        if action[3] in [8, 9, 10]:
            skill_steps.append(step)
        elif action[3] == 2:
            last_hit_steps.append(step)
        elif action[3] == 3:
            push_tower_steps.append(step)

    first_attack_step = next((item["step"] for item in attack_schedule if item["action"] in [1, 2, 3, 8, 9, 10]), 8)
    first_skill_step = next((item["step"] for item in attack_schedule if item["action"] in [8, 9, 10]), 16)

    profile = {
        "version": 1,
        "source_samples_path": os.path.abspath(samples_path),
        "profile_step_sec": float(args.profile_step_sec),
        "record_count": len(records),
        "opening_move_only_profile": bool(args.opening_move_only_profile),
        "opening_touch_route": bool(args.opening_touch_route),
        "move_joystick_center": {
            "x": round(float(move_center_ratio[0]), 4),
            "y": round(float(move_center_ratio[1]), 4),
        },
        "touch_inferred_move_count": int(touch_inferred_move_count),
        "opening_move_ratio": round(
            sum(1 for record in opening_records if record["parsed_action"][0] != 0) / max(1, len(opening_records)),
            4,
        ),
        "route_angles": [item["angle"] for item in route_segments],
        "route_steps": [item["steps"] for item in route_segments],
        "patrol_angles": patrol_angles,
        "removed_route_segments": removed_route_segments,
        "info_schedule": [] if args.opening_move_only_profile else info_schedule[:32],
        "attack_schedule": [] if args.opening_move_only_profile else attack_schedule[:64],
        "opening_attack_start_step": int(sum(int(item["steps"]) for item in route_segments)) if args.opening_move_only_profile else int(first_attack_step),
        "opening_skill_start_step": int(sum(int(item["steps"]) for item in route_segments)) if args.opening_move_only_profile else int(first_skill_step),
        "skill_interval_steps": estimate_interval(skill_steps, 7),
        "last_hit_interval_steps": estimate_interval(last_hit_steps, 4),
        "push_tower_interval_steps": estimate_interval(push_tower_steps, 13),
        "summary": {
            "move": dict(move_counter),
            "info": dict(info_counter),
            "attack": dict(attack_counter),
        },
    }
    return profile


def main():
    args = parse_args()
    samples_path = resolve_samples_path(args)
    metadata = load_metadata(samples_path)
    raw_records = load_records(samples_path)
    records = resample_records(raw_records, args.profile_step_sec, args.max_steps)
    if args.opening_move_only_profile:
        args.opening_touch_route = True
    profile = build_profile(records, args, samples_path, metadata=metadata)
    profile["source_record_count"] = len(raw_records)

    output_dir = os.path.dirname(args.out)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as file:
        json.dump(profile, file, ensure_ascii=False, indent=2)

    print(f"samples={samples_path}")
    print(f"out={args.out}")
    print(f"source_records={len(raw_records)}")
    print(f"profile_records={len(records)}")
    print(f"profile_step_sec={args.profile_step_sec}")
    print(f"route_angles={profile['route_angles']}")
    print(f"route_steps={profile['route_steps']}")
    print(f"opening_move_only_profile={profile.get('opening_move_only_profile')}")
    print(f"opening_touch_route={profile.get('opening_touch_route')}")
    print(f"move_joystick_center={profile.get('move_joystick_center')}")
    print(f"touch_inferred_move_count={profile.get('touch_inferred_move_count')}")
    print(f"opening_move_ratio={profile.get('opening_move_ratio')}")
    print(f"patrol_angles={profile['patrol_angles']}")
    print(f"info_schedule={profile['info_schedule'][:8]}")
    print(f"attack_summary={profile['summary']['attack']}")


if __name__ == "__main__":
    main()
