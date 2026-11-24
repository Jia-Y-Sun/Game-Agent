"""把整场人为示范压成 `src/game_profile.json`。

用法（最常用）：

```powershell
python build_game_profile.py --assist_source human_data --out src/game_profile.json
```

输入既可以是新的 `assist_samples.jsonl`（`live_human_assist.py` 实时写的），
也可以是旧的 `human_data_collector.py` 产出的 `samples.jsonl`，两者会被自动识别。

输出是 `lane_profile.json` 的超集：

* 顶层保留 `route_angles` / `route_steps` / `patrol_angles` / `info_schedule` /
  `attack_schedule` 等字段，`lane_macro.py` 可以直接拿它当开局 profile 用；
* 新增 `phases` / `groups` / `global`，按对局阶段统计走位角度分布、
  普攻/技能/补刀/推塔节奏和买装升级节奏，供 `game_profile.GameProfilePrior`
  在整场对局里回放人的节奏。
"""

import argparse
import datetime
import json
import os
import statistics
import sys
from collections import Counter

import assist_dataset as ads
import build_lane_profile as blp
from game_profile import PHASE_GROUPS, angle_histogram, circular_mean, phase_group


DEFAULT_MOVE_CENTER = (0.173, 0.784)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a whole-game behaviour profile from human demonstrations.",
    )
    parser.add_argument("--assist_source", default="",
                        help="assist_samples.jsonl / samples.jsonl / episode dir / human_data dir; empty means latest episode")
    parser.add_argument("--human_data_dir", default="human_data")
    parser.add_argument("--out", default="src/game_profile.json")
    parser.add_argument("--profile_step_sec", type=float, default=0.0,
                        help="Time bin for resampling; 0 keeps the recorded decision rate")
    parser.add_argument("--max_steps", type=int, default=4000, help="Max resampled steps per episode")
    parser.add_argument("--opening_steps", type=int, default=120,
                        help="Steps treated as the opening phase when records carry no phase label")
    parser.add_argument("--angle_tolerance", type=int, default=18)
    parser.add_argument("--min_segment_steps", type=int, default=2)
    parser.add_argument("--route_outlier_max_turn", type=int, default=100)
    parser.add_argument("--route_outlier_max_steps", type=int, default=2)
    parser.add_argument("--opening_route_max_segments", type=int, default=3)
    parser.add_argument("--min_patrol_angles", type=int, default=4)
    parser.add_argument("--top_angle_count", type=int, default=6)
    parser.add_argument("--min_phase_steps", type=int, default=6)
    parser.add_argument("--move_center_x", type=float, default=DEFAULT_MOVE_CENTER[0])
    parser.add_argument("--move_center_y", type=float, default=DEFAULT_MOVE_CENTER[1])
    parser.add_argument("--dry_run", action="store_true", help="Print the summary without writing the file")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# 数据准备
# --------------------------------------------------------------------------- #
def resolve_source(args):
    if args.assist_source:
        return args.assist_source
    latest = ads.latest_episode_dir(args.human_data_dir, prefer_frames=False)
    if latest:
        return latest
    return args.human_data_dir


def group_by_episode(records):
    episodes = {}
    for record in records:
        key = record.get("source") or record.get("episode_id") or "default"
        episodes.setdefault(key, []).append(record)
    for key, items in episodes.items():
        items.sort(key=lambda item: (item.get("step", -1), item.get("timestamp", 0.0)))
    return episodes


def _merge_bin(records):
    """把一个时间桶里的多帧合成一个决策步，规则与 build_lane_profile 保持一致。"""
    merged = dict(records[-1])
    actions = [record.get("human_act") or record["acts"] for record in records]
    action = [0] * ads.ACTION_LEN
    for item in actions:
        if item[0] != 0:
            action[0] = item[0]
            action[1] = item[1]
        if item[2] != 0:
            action[2] = item[2]
    best = blp._best_attack(actions)
    action[3:] = list(best[3:])
    merged["acts"] = [int(value) for value in action]
    if any(record.get("human_present") for record in records):
        merged["human_act"] = [int(value) for value in action]
        merged["human_present"] = True
    merged["obs"] = records[-1].get("obs") or {}
    merged["timestamp"] = records[0].get("timestamp", 0.0)
    return merged


def resample_episode(records, step_sec, max_steps):
    if step_sec <= 0 or not records:
        return records[:max_steps] if max_steps else records

    first_ts = records[0].get("timestamp", 0.0) or 0.0
    bins = []
    current = []
    current_index = None
    for record in records:
        timestamp = record.get("timestamp", 0.0) or 0.0
        index = int(max(0.0, timestamp - first_ts) / step_sec)
        if current_index is None:
            current_index = index
        if index != current_index and current:
            bins.append(_merge_bin(current))
            current = []
            current_index = index
            if max_steps and len(bins) >= max_steps:
                break
        current.append(record)
    if current and (not max_steps or len(bins) < max_steps):
        bins.append(_merge_bin(current))
    return bins[:max_steps] if max_steps else bins


def derive_phase(record, index, opening_steps):
    phase = str(record.get("phase") or "").strip()
    if phase and phase != "unknown":
        return phase
    lane_phase = str(record.get("lane_phase") or "").strip()
    if lane_phase in ("opening", "laning"):
        return lane_phase
    obs = record.get("obs") or {}
    if obs.get("dead"):
        return "recover"
    if index < opening_steps:
        return "opening"
    return "laning"


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def _cadence(steps, default):
    if len(steps) < 2:
        return default
    deltas = [b - a for a, b in zip(steps, steps[1:]) if b > a]
    if not deltas:
        return default
    value = int(round(statistics.median(deltas)))
    return max(2, value)


def _mean(values, default=None):
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return default
    return round(sum(cleaned) / len(cleaned), 4)


def _ratio(part, total):
    return round(float(part) / float(total), 4) if total else 0.0


def phase_stats(entries, top_angle_count=6):
    """`entries` 是 (index, action, observation) 三元组。"""
    step_count = len(entries)
    if not step_count:
        return {}

    move_actions = 0
    angles = []
    attack_hist = Counter()
    info_hist = Counter()
    attack_steps = []
    skill_steps = []
    last_hit_steps = []
    push_tower_steps = []
    buy_steps = []
    upgrade_steps = []
    directional_skills = 0
    skill_total = 0
    hps = []
    pressures = []
    distances = []

    for index, action, observation in entries:
        if action[0] != 0:
            move_actions += 1
            angles.append(int(action[1]) % 360)
        attack = int(action[3])
        info = int(action[2])
        if attack:
            attack_hist[attack] += 1
            attack_steps.append(index)
            if attack >= 8:
                skill_steps.append(index)
                skill_total += 1
                if int(action[4]) == 1:
                    directional_skills += 1
            elif attack == 2:
                last_hit_steps.append(index)
            elif attack == 3:
                push_tower_steps.append(index)
        if info:
            info_hist[info] += 1
            if info in (1, 2):
                buy_steps.append(index)
            elif info in (6, 7, 8):
                upgrade_steps.append(index)
        observation = observation or {}
        hps.append(observation.get("hp"))
        pressures.append(observation.get("pressure"))
        distances.append(observation.get("target_dist"))

    histogram = angle_histogram(angles, bins=36)
    total_angles = sum(histogram.values()) or 1
    top_angles = [
        {
            "angle": int((int(bucket) + 0.5) * 10) % 360,
            "count": int(count),
            "ratio": round(count / total_angles, 4),
        }
        for bucket, count in sorted(histogram.items(), key=lambda item: (-item[1], item[0]))[:max(1, top_angle_count)]
    ]

    return {
        "step_count": int(step_count),
        "move_ratio": _ratio(move_actions, step_count),
        "angle_histogram": histogram,
        "top_angles": top_angles,
        "mean_angle": int(circular_mean(angles)) if angles else None,
        "attack_hist": {int(key): int(value) for key, value in sorted(attack_hist.items())},
        "info_hist": {int(key): int(value) for key, value in sorted(info_hist.items())},
        "attack_cadence": {
            "all": _cadence(attack_steps, 3),
            "skill": _cadence(skill_steps, 7),
            "last_hit": _cadence(last_hit_steps, 4),
            "push_tower": _cadence(push_tower_steps, 13),
        },
        "info_cadence": {
            "buy": _cadence(buy_steps, 45),
            "upgrade": _cadence(upgrade_steps, 18),
        },
        "directional_skill_ratio": _ratio(directional_skills, skill_total),
        "mean_hp": _mean(hps),
        "mean_pressure": _mean(pressures),
        "mean_target_distance": _mean(distances),
        "attack_ratio": _ratio(sum(attack_hist.values()), step_count),
        "info_ratio": _ratio(sum(info_hist.values()), step_count),
    }


def aggregate_stats(stats_list):
    """把多个阶段的统计合并成上层统计（按步数近似加权）。"""
    merged = Counter()
    angles = []
    attack_hist = Counter()
    info_hist = Counter()
    skill_total = 0
    directional = 0
    total_steps = 0
    move_steps = 0
    attack_steps = 0
    info_steps = 0
    hps = []
    pressures = []
    distances = []

    for stats in stats_list:
        steps = int(stats.get("step_count", 0) or 0)
        total_steps += steps
        move_steps += int(round(float(stats.get("move_ratio", 0.0)) * steps))
        attack_steps += int(round(float(stats.get("attack_ratio", 0.0)) * steps))
        info_steps += int(round(float(stats.get("info_ratio", 0.0)) * steps))
        for bucket, count in (stats.get("angle_histogram") or {}).items():
            merged[int(bucket)] += int(count)
        for key, value in (stats.get("attack_hist") or {}).items():
            attack_hist[int(key)] += int(value)
        for key, value in (stats.get("info_hist") or {}).items():
            info_hist[int(key)] += int(value)
        # 每个阶段自己的技能样本数 × 它自己的定向比例，再累加
        phase_skill_total = sum(
            int(value) for key, value in (stats.get("attack_hist") or {}).items() if int(key) >= 8
        )
        skill_total += phase_skill_total
        directional += int(round(float(stats.get("directional_skill_ratio", 0.0)) * phase_skill_total))
        for name, bucket in (("mean_hp", hps), ("mean_pressure", pressures), ("mean_target_distance", distances)):
            if stats.get(name) is not None:
                bucket.append(stats[name])

    if not total_steps:
        return {}

    histogram = {int(key): int(value) for key, value in sorted(merged.items())}
    total_angles = sum(histogram.values()) or 1
    top_angles = [
        {
            "angle": int((int(bucket) + 0.5) * 10) % 360,
            "count": int(count),
            "ratio": round(count / total_angles, 4),
        }
        for bucket, count in sorted(histogram.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]
    for bucket in histogram:
        angles.extend([(bucket + 0.5) * 10] * histogram[bucket])

    return {
        "step_count": int(total_steps),
        "move_ratio": _ratio(move_steps, total_steps),
        "angle_histogram": histogram,
        "top_angles": top_angles,
        "mean_angle": int(circular_mean(angles)) if angles else None,
        "attack_hist": {int(key): int(value) for key, value in sorted(attack_hist.items())},
        "info_hist": {int(key): int(value) for key, value in sorted(info_hist.items())},
        "attack_cadence": stats_list[0].get("attack_cadence", {}) if stats_list else {},
        "info_cadence": stats_list[0].get("info_cadence", {}) if stats_list else {},
        "directional_skill_ratio": _ratio(directional, skill_total),
        "mean_hp": _mean(hps),
        "mean_pressure": _mean(pressures),
        "mean_target_distance": _mean(distances),
        "attack_ratio": _ratio(attack_steps, total_steps),
        "info_ratio": _ratio(info_steps, total_steps),
    }


# --------------------------------------------------------------------------- #
# 开局兼容字段
# --------------------------------------------------------------------------- #
def build_opening_route(episodes, args):
    """从第一段示范的开局部分提取 `lane_macro.py` 能直接用的路线。"""
    first = next(iter(episodes.values()), [])
    if not first:
        return {}, []

    opening = first[: args.opening_steps] if args.opening_steps > 0 else first
    proxied = []
    for record in opening:
        action = record.get("human_act") or record["acts"]
        proxied.append({"parsed_action": [int(value) for value in action]})

    segments = blp.compress_angles(proxied, args.angle_tolerance, args.min_segment_steps)
    segments, removed = blp.clean_route_segments(segments, args)
    segments = blp.simplify_route_segments(segments, args.opening_route_max_segments)

    laning_proxy = [
        {"parsed_action": [int(value) for value in (record.get("human_act") or record["acts"])]}
        for record in first
    ]
    patrol_segments = blp.compress_angles(laning_proxy, args.angle_tolerance, args.min_segment_steps)
    patrol_angles = blp.build_patrol_angles(patrol_segments, segments, args.min_patrol_angles)

    move_count = sum(1 for record in opening if (record.get("human_act") or record["acts"])[0] != 0)
    payload = {
        "opening_move_only_profile": True,
        "opening_touch_route": True,
        "opening_move_ratio": _ratio(move_count, len(opening)),
        "route_angles": [int(item["angle"]) for item in segments],
        "route_steps": [int(item["steps"]) for item in segments],
        "patrol_angles": [int(value) for value in patrol_angles],
        "removed_route_segments": removed,
        "move_joystick_center": {
            "x": round(float(args.move_center_x), 4),
            "y": round(float(args.move_center_y), 4),
        },
        "opening_attack_start_step": int(sum(int(item["steps"]) for item in segments)),
        "opening_skill_start_step": int(sum(int(item["steps"]) for item in segments)),
    }
    return payload, segments


def build_schedules(episodes, args):
    """生成与 `lane_profile.json` 兼容的 info/attack 回放表。"""
    first = next(iter(episodes.values()), [])
    info_schedule = []
    attack_schedule = []
    last_info_step = -999
    for index, record in enumerate(first[: max(1, args.opening_steps or 120)]):
        action = record.get("human_act") or record["acts"]
        if int(action[2]) != 0 and index - last_info_step >= 6:
            info_schedule.append({"step": int(index), "action": int(action[2])})
            last_info_step = index
        if int(action[3]) != 0:
            attack_schedule.append({
                "step": int(index),
                "action": int(action[3]),
                "action_type": int(action[4]),
                "arg1": int(action[5]),
                "arg2": int(action[6]),
                "arg3": int(action[7]),
            })
    return info_schedule[:32], attack_schedule[:64]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_profile(args, records):
    episodes = group_by_episode(records)
    resampled = {}
    for key, items in episodes.items():
        resampled[key] = resample_episode(items, args.profile_step_sec, args.max_steps)

    phase_entries = {}
    phase_steps = Counter()
    for items in resampled.values():
        for index, record in enumerate(items):
            phase = derive_phase(record, index, args.opening_steps)
            action = record.get("human_act") or record["acts"]
            phase_entries.setdefault(phase, []).append((index, action, record.get("obs") or {}))

    phases = {}
    for phase, entries in phase_entries.items():
        stats = phase_stats(entries, args.top_angle_count)
        if not stats:
            continue
        stats["human_step_count"] = int(sum(1 for entry in entries))
        phases[phase] = stats
        phase_steps[phase] = stats["step_count"]

    groups = {}
    for group, members in PHASE_GROUPS.items():
        collected = [phases[name] for name in members if name in phases]
        if not collected:
            continue
        stats = aggregate_stats(collected)
        if stats:
            stats["phases"] = [name for name in members if name in phases]
            groups[group] = stats

    global_stats = aggregate_stats(list(phases.values()))

    opening_payload, segments = build_opening_route(resampled, args)
    info_schedule, attack_schedule = build_schedules(resampled, args)

    profile = {
        "version": 2,
        "kind": "game_profile",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "profile_step_sec": float(args.profile_step_sec),
        "source_record_count": len(records),
        "episode_count": len(resampled),
        "episode_dirs": sorted(resampled.keys()),
        "source_samples_paths": sorted({record.get("source", "") for record in records if record.get("source")}),
        "minutes_covered": round(
            sum(
                max(0.0, (items[-1].get("timestamp", 0.0) - items[0].get("timestamp", 0.0)))
                for items in resampled.values() if items
            ) / 60.0,
            2,
        ),
        **opening_payload,
        "info_schedule": info_schedule,
        "attack_schedule": attack_schedule,
        "skill_interval_steps": int((phases.get("laning") or {}).get("attack_cadence", {}).get("skill", 7)),
        "last_hit_interval_steps": int((phases.get("laning") or {}).get("attack_cadence", {}).get("last_hit", 4)),
        "push_tower_interval_steps": int((phases.get("laning") or {}).get("attack_cadence", {}).get("push_tower", 13)),
        "phases": phases,
        "groups": groups,
        "global": global_stats,
        "phase_step_counts": dict(phase_steps),
        "opening_segments": segments,
    }
    return profile


def main(argv=None):
    args = parse_args(argv)
    source = resolve_source(args)
    sample_files = ads.resolve_sample_files(source)
    if not sample_files:
        print(f"未找到可用的示范数据: {source}")
        print("提示: 先跑 train.py 采集（--assist_enabled true），或用 human_data_collector.py 采旧格式样本。")
        return 2

    records = ads.load_records(sample_files)
    if not records:
        print(f"示范数据为空: {sample_files}")
        return 2

    summary = ads.summarize_records(records)
    print(f"source={source}")
    print(f"sample_files={len(sample_files)}")
    for path in sample_files:
        print(f"  - {path}")
    print(
        "records={total_steps} human_steps={human_steps} "
        "({human_ratio:.2%}) saved_frames={saved_frames} duration_sec={duration_sec}".format(**summary)
    )
    print(f"phases={summary['phases']}")
    print(f"attack={summary['attack']}")
    print(f"info={summary['info']}")

    profile = build_profile(args, records)

    if args.out and not args.dry_run:
        directory = os.path.dirname(args.out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as file:
            json.dump(profile, file, ensure_ascii=False, indent=2)
        print(f"out={args.out}")

    print("")
    print("== 整场行为 profile ==")
    print(f"episode_count={profile['episode_count']} source_records={profile['source_record_count']} "
          f"minutes_covered={profile['minutes_covered']}")
    print(f"phase_step_counts={profile['phase_step_counts']}")
    for name, stats in profile["phases"].items():
        print(
            f"  phase={name:<12} steps={stats['step_count']:<5} "
            f"move_ratio={stats['move_ratio']:.2f} "
            f"attack_ratio={stats['attack_ratio']:.2f} "
            f"skill_ival={stats['attack_cadence'].get('skill')} "
            f"last_hit_ival={stats['attack_cadence'].get('last_hit')} "
            f"top_angles={[item['angle'] for item in stats['top_angles'][:3]]}"
        )
    for name, stats in profile["groups"].items():
        print(
            f"  group={name:<12} steps={stats['step_count']:<5} "
            f"move_ratio={stats['move_ratio']:.2f} attack_ratio={stats['attack_ratio']:.2f}"
        )
    print("")
    print(f"route_angles={profile['route_angles']}")
    print(f"route_steps={profile['route_steps']}")
    print(f"patrol_angles={profile['patrol_angles']}")
    print(f"opening_move_ratio={profile['opening_move_ratio']}")
    print(f"move_joystick_center={profile['move_joystick_center']}")
    print("")
    print("下一步：")
    print("  python train.py --assist_enabled true --assist_mode assist "
          "--game_profile_enabled true --game_profile_prior_mode fill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
