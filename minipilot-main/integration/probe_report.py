"""Build a compact Chinese Markdown report from one probe run directory."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from integration.game_profiles import GAME_PROFILES


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def _load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _first(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_size(path: Path | None) -> str:
    if path is None or not path.exists():
        return "缺失"
    size = path.stat().st_size
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.2f} KB"
    return f"{size} B"


def _event_ts(events: list[dict[str, Any]], name: str) -> str:
    for event in events:
        if event.get("event") == name:
            return str(event.get("ts") or "")
    return "未记录"


def _event_value(events: list[dict[str, Any]], name: str, key: str) -> Any:
    for event in events:
        if event.get("event") == name:
            return event.get(key)
    return None


def _duration_from_events(events: list[dict[str, Any]]) -> int | None:
    ready = _event_ts(events, "MATCH_READY")
    finished = _event_ts(events, "MATCH_FINISHED")
    if not ready or not finished:
        return None
    try:
        start = datetime.fromisoformat(ready)
        end = datetime.fromisoformat(finished)
    except ValueError:
        return None
    return max(0, int(round((end - start).total_seconds())))


def _latency_stats(rows: list[dict[str, str]]) -> tuple[int, int | None, int | None, int | None]:
    values = [
        value
        for value in (_as_int(row.get("rtt")) for row in rows)
        if value is not None and value >= 0
    ]
    if not values:
        return 0, None, None, None
    return len(values), min(values), int(statistics.median(values)), max(values)


def _stall_stats(rows: list[dict[str, str]]) -> tuple[int, dict[str, int]]:
    reasons: dict[str, int] = {}
    count = 0
    for row in rows:
        if _as_int(row.get("trust_stall")) != 1:
            continue
        count += 1
        reason = (row.get("loading_reason") or "unknown").strip() or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    return count, reasons


def _flow_stats(path: Path | None) -> tuple[int, dict[str, int]]:
    rows = _load_rows(path) if path else []
    counts: dict[str, int] = {}
    for row in rows:
        label = (row.get("label") or row.get("flow_label") or "unknown").strip()
        counts[label] = counts.get(label, 0) + 1
    return len(rows), counts


def build_report(run_dir: str | Path, manifest: dict[str, Any] | None = None) -> str:
    root = Path(run_dir).resolve()
    manifest = manifest or _load_json(root / "manifest.json")
    events = _load_events(root / "events" / "game_driver.jsonl")
    qoe_path = _first(root.glob("capture/*/*_qoe.csv"))
    flow_path = _first(root.glob("capture/*/*_flow_labeled.csv"))
    summary_path = _first(root.glob("capture/*/postprocess/summary.json"))
    summary = _load_json(summary_path) if summary_path else {}
    qoe_summary = summary.get("qoe") if isinstance(summary.get("qoe"), dict) else {}
    video_timing = (
        summary.get("video_timing")
        if isinstance(summary.get("video_timing"), dict)
        else {}
    )
    qoe_rows = _load_rows(qoe_path) if qoe_path else []
    latency_samples, latency_min, latency_median, latency_max = _latency_stats(qoe_rows)
    stall_rows, stall_reasons = _stall_stats(qoe_rows)
    flow_count, flow_labels = _flow_stats(flow_path)
    video_path = _first(root.glob("capture/*/*.mp4"))
    pcap_path = _first(root.glob("capture/*/*.pcap"))
    log_path = _first(root.glob("capture/*/*.log"))
    postprocess_status = str(
        manifest.get("postprocess_status")
        or summary.get("status")
        or "unknown"
    )
    business_duration = qoe_summary.get("business_duration_seconds")
    if business_duration is None:
        business_duration = _duration_from_events(events)

    lines: list[str] = []
    game_id = str(manifest.get("game_id") or "unknown")
    game_name = GAME_PROFILES.get(game_id, {}).get("name", game_id)
    lines.append(f"# {game_name}自动拨测报告")
    lines.append("")
    lines.append(f"- 运行标识：`{manifest.get('run_id', root.name)}`")
    lines.append(f"- 游戏标识：`{game_id}`")
    lines.append(f"- 设备：`{manifest.get('device_id', 'unknown')}`")
    lines.append(f"- 状态：`{manifest.get('status', 'unknown')}`")
    lines.append(f"- 采集状态：`{manifest.get('capture_status', 'unknown')}`")
    lines.append(f"- 后处理状态：`{postprocess_status}`")
    lines.append(f"- 报告生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append("")

    lines.append("## 业务时间轴")
    lines.append("")
    lines.append(f"- `SCRIPT_STARTED`：{_event_ts(events, 'SCRIPT_STARTED')}")
    lines.append(f"- `MATCH_READY`：{_event_ts(events, 'MATCH_READY')}")
    lines.append(f"- `MATCH_FINISHED`：{_event_ts(events, 'MATCH_FINISHED')}")
    lines.append(
        f"- 业务时长：{business_duration if business_duration is not None else '未知'} 秒"
    )
    lines.append(
        f"- 结果：`{manifest.get('match_result', 'unknown')}`，"
        f"结果来源：`{manifest.get('match_result_source', 'unknown')}`"
    )
    lines.append(
        f"- 结束来源：`{manifest.get('match_finish_source', 'unknown')}`"
    )
    lines.append("")

    lines.append("## 采集产物")
    lines.append("")
    lines.append(
        f"- 视频：{_format_size(video_path)}，时长 "
        f"{video_timing.get('actual_duration_seconds', '未知')} 秒"
    )
    lines.append(f"- PCAP：{_format_size(pcap_path)}")
    lines.append(f"- 日志：{_format_size(log_path)}")
    lines.append(f"- QoE CSV：{_format_size(qoe_path)}")
    lines.append(f"- 流级 CSV：{_format_size(flow_path)}")
    lines.append("")

    lines.append("## QoE 指标")
    lines.append("")
    lines.append(f"- QoE 行数：{len(qoe_rows)}")
    lines.append(f"- RTT 样本：{latency_samples}")
    if latency_samples:
        lines.append(
            f"- RTT 最小值 / 中位数 / 最大值："
            f"{latency_min} / {latency_median} / {latency_max} ms"
        )
    else:
        lines.append("- RTT：本样本没有可信数值")
    lines.append(f"- 卡顿行数：{stall_rows}")
    if stall_reasons:
        lines.append(
            "- 卡顿原因："
            + "，".join(f"{key} {value}" for key, value in sorted(stall_reasons.items()))
        )
    warnings = list(summary.get("warnings") or []) + list(qoe_summary.get("warnings") or [])
    lines.append(f"- 后处理告警：{'无' if not warnings else '；'.join(str(item) for item in warnings)}")
    lines.append("")

    lines.append("## 流级分类")
    lines.append("")
    lines.append(f"- 流数量：{flow_count}")
    if flow_labels:
        lines.append(
            "- 分类分布："
            + "，".join(f"{key} {value}" for key, value in sorted(flow_labels.items()))
        )
    else:
        lines.append("- 分类分布：无")
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    status = str(manifest.get("status") or "unknown")
    postprocess_ok = postprocess_status == "ok"
    if status == "ok" and not postprocess_ok:
        lines.append("- 对局流程已结束，后处理缺失或未全部成功，请检查 summary 和日志。")
    elif status == "ok" and any(path is None for path in (video_path, pcap_path, qoe_path, flow_path)):
        lines.append("- 对局流程已结束，但部分视频、抓包、QoE 或流级产物缺失，不能据此认定采集完整。")
    elif status == "ok" and latency_samples and stall_rows == 0:
        lines.append("- 本轮流程正常结束，已找到非空视频、抓包、QoE 和流级文件；数据质量仍需复核。")
        lines.append("- 已输出的 QoE 行未标记卡顿；该统计不等同于人工画面质量验收。")
    elif status == "ok":
        lines.append("- 本轮流程正常结束，已找到非空产物；指标有效性和卡顿情况仍需进一步分析。")
    else:
        lines.append("- 本轮运行未正常完成，请检查 manifest、游戏日志和采集日志。")
    lines.append("")
    return "\n".join(lines)


def write_report(
    run_dir: str | Path,
    manifest: dict[str, Any] | None = None,
    output: str | Path | None = None,
) -> Path:
    root = Path(run_dir).resolve()
    target = Path(output).resolve() if output else root / "report.md"
    target.write_text(build_report(root, manifest), encoding="utf-8")
    return target


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Chinese probe report.")
    parser.add_argument("run_dir")
    parser.add_argument("--output", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_report(args.run_dir, output=args.output)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
