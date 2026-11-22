"""单局训练效果统计。

读取 `src/step_metrics.jsonl`（`train.py` 每步写一行），输出一局的：

* 时长/步数/奖励总量与奖励构成
* **探索度**：ε 探索占比（policy_source 分布）、动作多样性（熵）、阶段覆盖
* 动作分布：移动比例、角度直方图、普攻/技能/信息动作占比
* 事件与异常：死亡次数、无画面变化的无效动作占比
* 时延与显存

用法：

```bat
python analyze_episode.py
python analyze_episode.py --metrics src/step_metrics.jsonl --json out.json
```

想只看最近一局，可以在开新局前把旧的 `step_metrics.jsonl` 挪走（脚本本身也会按
`done=1` 或时间间隔自动切分「局」，用 `--max-gap-sec` 控制）。
"""

import argparse
import json
import math
import os
import statistics
from collections import Counter, defaultdict


def load_records(path):
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def split_episodes(records, max_gap_sec=90.0):
    """按 done=1 或「相邻两步时间断层」切局。"""
    episodes = []
    current = []
    for record in records:
        if current:
            gap = float(record.get("step_latency_ms") or 0) / 1000.0
            if gap > max_gap_sec:
                episodes.append(current)
                current = []
        current.append(record)
        if int(record.get("done") or 0) == 1:
            episodes.append(current)
            current = []
    if current:
        episodes.append(current)
    return episodes


def entropy(counter):
    total = sum(counter.values())
    if total <= 1:
        return 0.0
    value = 0.0
    for count in counter.values():
        p = count / total
        if p > 0:
            value -= p * math.log(p, 2)
    return round(value, 3)


def normalized_entropy(counter, bins):
    if bins <= 1:
        return 0.0
    top = math.log(bins, 2)
    return round(entropy(counter) / top, 3) if top else 0.0


def action_key(record):
    raw = record.get("effective_action") or []
    if len(raw) < 8:
        return "?"
    move, angle, info, attack = int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3])
    if move:
        angle_bucket = int(angle) % 360 // 30
        return f"move:{angle_bucket}"
    if attack >= 7:
        return f"skill:{attack}"
    if attack:
        return f"attack:{attack}"
    if info:
        return f"info:{info}"
    return "idle"


def _mean(values, default=0.0):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else default


def summarize_episode(records):
    n = len(records)
    steps = []
    rewards = []
    reward_parts = defaultdict(list)
    phases = Counter()
    macros = Counter()
    sources = Counter()
    events = Counter()
    actions = Counter()
    angles = Counter()
    move_ratio = 0
    attack_counter = Counter()
    info_counter = Counter()
    frame_change = []
    ineffective = 0
    deaths = 0
    latency = []
    select_ms = []
    hp = []

    for record in records:
        steps.append(record)
        rewards.append(float(record.get("reward") or 0.0))
        for key in ("probe_reward", "gameplay_reward", "traffic_reward", "target_reward",
                    "phase_reward", "event_reward"):
            if record.get(key) is not None:
                reward_parts[key].append(float(record[key]))
        phases[record.get("phase") or "unknown"] += 1
        macros[record.get("lane_macro_name") or "-"] += 1
        sources[record.get("policy_source") or "?"] += 1
        events[record.get("event") or "none"] += 1
        if record.get("event") == "death":
            deaths += 1
        actions[action_key(record)] += 1
        raw = record.get("effective_action") or []
        if len(raw) >= 8:
            if int(raw[0]) != 0:
                move_ratio += 1
                angles[int(raw[1]) % 360 // 30] += 1
            attack_counter[int(raw[3])] += 1
            info_counter[int(raw[2])] += 1
        fc = record.get("frame_change")
        if fc is not None:
            frame_change.append(float(fc))
        if record.get("ineffective_action"):
            ineffective += 1
        if record.get("step_latency_ms") is not None:
            latency.append(float(record["step_latency_ms"]))
        if record.get("select_action_ms") is not None:
            select_ms.append(float(record["select_action_ms"]))
        if record.get("phase_own_hp_ratio") is not None:
            hp.append(float(record["phase_own_hp_ratio"]))

    model_steps = sources.get("model", 0)
    explore_steps = n - model_steps
    total_reward = round(sum(rewards), 3)
    per_attack = {k: v for k, v in sorted(attack_counter.items())}

    return {
        "steps": n,
        "total_reward": total_reward,
        "mean_reward": round(total_reward / n, 4) if n else 0.0,
        "reward_median": round(statistics.median(rewards), 4) if rewards else 0.0,
        "reward_parts": {k: round(sum(v), 3) for k, v in reward_parts.items()},
        "reward_part_mean": {k: _mean(v) for k, v in reward_parts.items()},
        "exploration": {
            "model_ratio": round(model_steps / n, 4) if n else 0.0,
            "explore_ratio": round(explore_steps / n, 4) if n else 0.0,
            "policy_sources": dict(sources),
            "action_kinds": len(actions),
            "action_entropy": entropy(actions),
            "action_entropy_norm": round(entropy(actions) / math.log(max(2, len(actions)), 2), 3),
            "angle_entropy_norm": normalized_entropy(angles, 12),
            "angle_bins_used": len(angles),
            "phases_visited": len([p for p in phases if p != "unknown"]),
        },
        "actions": {
            "move_ratio": round(move_ratio / n, 4) if n else 0.0,
            "top_actions": actions.most_common(10),
            "attack_hist": per_attack,
            "info_hist": {k: v for k, v in sorted(info_counter.items())},
            "angle_bucket_hist": {k: v for k, v in sorted(angles.items())},
        },
        "phases": dict(phases),
        "macros": macros.most_common(10),
        "events": dict(events),
        "deaths": deaths,
        "frame_change_mean": _mean(frame_change),
        "ineffective_ratio": round(ineffective / n, 4) if n else 0.0,
        "hp_ratio_mean": _mean(hp) if hp else None,
        "latency_ms_mean": _mean(latency),
        "select_ms_mean": _mean(select_ms),
        "duration_min": round(sum(latency) / 1000.0 / 60.0, 2) if latency else None,
    }


def print_report(summary, title="单局统计"):
    line = "=" * 68
    print(line)
    print(f"{title}")
    print(line)
    print(f"步数            : {summary['steps']}")
    print(f"时长            : {summary['duration_min']} 分钟"
          f"（平均步时延 {summary['latency_ms_mean']} ms，其中决策 {summary['select_ms_mean']} ms）")
    print(f"总奖励          : {summary['total_reward']}   平均 {summary['mean_reward']}   中位数 {summary['reward_median']}")
    print("")
    print("奖励构成（总和 / 每步平均）：")
    for key in ("probe_reward", "gameplay_reward", "target_reward", "phase_reward", "event_reward", "traffic_reward"):
        print(f"  {key:<18}: {summary['reward_parts'].get(key, 0):>10}  /  {summary['reward_part_mean'].get(key, 0):>8}")
    print("")
    exp = summary["exploration"]
    print("探索度：")
    print(f"  模型决策占比    : {exp['model_ratio']:.1%}")
    print(f"  ε/先验探索占比  : {exp['explore_ratio']:.1%}   {exp['policy_sources']}")
    print(f"  动作种类        : {exp['action_kinds']}   动作熵 {exp['action_entropy']} bit")
    print(f"  移动角度熵(归一): {exp['angle_entropy_norm']}（用了 {exp['angle_bins_used']}/12 个 30° 区间）")
    print(f"  访问阶段数      : {exp['phases_visited']}")
    print("")
    print("动作分布：")
    print(f"  移动占比        : {summary['actions']['move_ratio']:.1%}")
    print(f"  Top 动作        : {summary['actions']['top_actions']}")
    print(f"  攻击动作直方图  : {summary['actions']['attack_hist']}")
    print(f"  信息动作直方图  : {summary['actions']['info_hist']}")
    print("")
    print(f"阶段分布        : {summary['phases']}")
    print(f"宏动作 Top      : {summary['macros']}")
    print(f"事件            : {summary['events']}   死亡 {summary['deaths']} 次")
    print(f"画面变化均值    : {summary['frame_change_mean']}   无效动作占比 {summary['ineffective_ratio']:.1%}")
    print(f"血量估计均值    : {summary['hp_ratio_mean']}")


def main():
    parser = argparse.ArgumentParser(description="Summarize one training episode from step_metrics.jsonl")
    parser.add_argument("--metrics", default="src/step_metrics.jsonl")
    parser.add_argument("--json", default="", help="把结果写成 JSON")
    parser.add_argument("--all", action="store_true", help="打印每一局")
    parser.add_argument("--max-gap-sec", type=float, default=90.0)
    parser.add_argument("--range", default="", dest="step_range",
                        help="只统计指定步区间，例如 0:1065（对局结束后程序可能还在别的界面上乱点）")
    args = parser.parse_args()

    if not os.path.exists(args.metrics):
        print(f"找不到 {args.metrics}，先跑一局 train.py（默认就会写这个文件）")
        return 2

    records = load_records(args.metrics)
    if not records:
        print(f"{args.metrics} 是空的")
        return 2

    if args.step_range:
        start_text, _, end_text = args.step_range.partition(":")
        start = int(start_text or 0)
        end = int(end_text) if end_text.strip() else len(records)
        records = records[start:end]
        print(f"按 --range {args.step_range} 取样：{len(records)} 步\n")

    episodes = split_episodes(records, args.max_gap_sec)
    print(f"共 {len(records)} 步，切出 {len(episodes)} 局\n")

    summaries = []
    if args.all:
        for index, episode in enumerate(episodes, 1):
            summary = summarize_episode(episode)
            summaries.append(summary)
            print_report(summary, title=f"第 {index} 局（{len(episode)} 步）")
            print("")
    else:
        summary = summarize_episode(episodes[-1])
        summaries.append(summary)
        print_report(summary, title=f"最近一局（{len(episodes[-1])} 步，共 {len(episodes)} 局）")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as file:
            json.dump(summaries if args.all else summaries[-1], file, ensure_ascii=False, indent=2)
        print(f"\njson -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
