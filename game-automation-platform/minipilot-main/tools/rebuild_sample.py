#!/usr/bin/env python3
"""Rebuild and validate one or more Unicapture samples from MP4 and PCAP.

The command accepts either a MiniPilot run directory or a concrete sample
directory. Existing YAML/QoE files are backed up before replacement. Repair is
atomic at the individual-file level and a JSON report is always written.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from unicapture.app_configs import get_app_config
from unicapture.postprocess.annotate_business_times import (
    format_yaml_time,
    main as annotate_business_times,
)
from unicapture.qoe_postprocess.collector_replay import replay_collector_qoe
from unicapture.qoe_postprocess.sample import load_sample, parse_dataset_time
from unicapture.video_metadata import probe_video_duration


REQUIRED_QOE_FIELDS = {
    "file_name", "time", "rtt", "trust_resolution", "trust_stall",
    "loading_reason",
}
CAPTURE_TIME_RE = re.compile(r"(?P<stamp>\d{8}T\d{6})(?P<fraction>\d{0,6})")
TARGET_RE = re.compile(r"-(?P<duration>\d+)-\d{8}T\d{6}")


class RepairError(RuntimeError):
    """Raised when safe reconstruction is impossible."""


@dataclass(frozen=True)
class Artifacts:
    root: Path
    video: Path
    pcap: Path
    yaml_path: Path
    qoe_path: Path
    prefix: str


def discover_samples(path: Path) -> list[Artifacts]:
    """Find direct MP4+PCAP pairs below a run or sample directory."""
    path = path.resolve()
    if not path.is_dir():
        raise RepairError(f"目录不存在: {path}")
    candidates = [path, *(item for item in path.rglob("*") if item.is_dir())]
    found: list[Artifacts] = []
    for directory in candidates:
        if "postprocess" in {part.lower() for part in directory.parts}:
            continue
        videos = sorted(directory.glob("*.mp4"))
        pcaps = sorted((*directory.glob("*.pcap"), *directory.glob("*.pcapng")))
        if not videos and not pcaps:
            continue
        if len(videos) != 1:
            raise RepairError(f"{directory} 必须且只能有一个MP4，实际 {len(videos)} 个")
        canonical_pcaps = [item for item in pcaps if not item.name.startswith("cut_")]
        selected_pcaps = canonical_pcaps or pcaps
        if len(selected_pcaps) != 1:
            raise RepairError(f"{directory} 必须且只能有一个主PCAP，实际 {len(selected_pcaps)} 个")
        video = videos[0]
        pcap = selected_pcaps[0]
        prefix = video.stem
        if pcap.stem != prefix and not pcap.stem.endswith(prefix):
            raise RepairError(f"MP4与PCAP前缀不一致: {video.name} / {pcap.name}")
        yamls = sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")))
        yaml_path = yamls[0] if len(yamls) == 1 else directory / f"{prefix}.yaml"
        if len(yamls) > 1:
            raise RepairError(f"{directory} 有多个YAML，拒绝猜测")
        found.append(Artifacts(
            root=directory,
            video=video,
            pcap=pcap,
            yaml_path=yaml_path,
            qoe_path=directory / f"{prefix}_qoe.csv",
            prefix=prefix,
        ))
    unique = {item.root: item for item in found}
    return [unique[key] for key in sorted(unique)]


def _video_stream_info(video: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RepairError("找不到 ffprobe，请先安装 FFmpeg")
    import subprocess
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate",
            "-of", "json", str(video),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise RepairError(f"ffprobe无法读取视频: {result.stderr.strip()[:200]}")
    streams = (json.loads(result.stdout or "{}").get("streams") or [])
    if not streams:
        raise RepairError(f"视频中没有可用画面流: {video}")
    stream = streams[0]
    rate = str(stream.get("avg_frame_rate") or "0/1")
    try:
        numerator, denominator = rate.split("/", 1)
        fps = round(float(numerator) / max(float(denominator), 1.0), 3)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": fps,
    }


def _classic_pcap_bounds(path: Path, zone: timezone) -> tuple[datetime, datetime] | None:
    """Read timestamps from classic pcap without loading packets into memory."""
    formats = {
        b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),
        b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
        b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),
        b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
    }
    with path.open("rb") as handle:
        magic = handle.read(4)
        if magic not in formats:
            return None
        endian, divisor = formats[magic]
        if len(handle.read(20)) != 20:
            raise RepairError(f"PCAP全局头不完整: {path}")
        first: float | None = None
        last: float | None = None
        while True:
            header = handle.read(16)
            if not header:
                break
            if len(header) != 16:
                raise RepairError(f"PCAP数据包头被截断: {path}")
            seconds, fraction, captured, _original = struct.unpack(
                f"{endian}IIII", header
            )
            if captured > 128 * 1024 * 1024:
                raise RepairError(f"PCAP记录长度异常: {captured}")
            stamp = seconds + fraction / divisor
            first = stamp if first is None else first
            last = stamp
            handle.seek(captured, 1)
        if first is None or last is None:
            raise RepairError(f"PCAP中没有数据包: {path}")
    return (
        datetime.fromtimestamp(first, tz=timezone.utc).astimezone(zone),
        datetime.fromtimestamp(last, tz=timezone.utc).astimezone(zone),
    )


def _infer_start(artifacts: Artifacts, old: dict[str, Any], zone_text: str) -> datetime:
    video = old.get("video_record") or {}
    task = old.get("task_info") or {}
    existing = (
        parse_dataset_time(video.get("start_time"), zone_text)
        or parse_dataset_time(task.get("start_time"), zone_text)
    )
    if existing:
        return existing
    match = CAPTURE_TIME_RE.search(artifacts.prefix)
    if not match:
        raise RepairError("YAML缺失且无法从文件名推断录制开始时间")
    fraction = match.group("fraction").ljust(6, "0")
    zone = datetime.strptime(f"20000101T000000000000{zone_text}", "%Y%m%dT%H%M%S%f%z").tzinfo
    return datetime.strptime(
        f"{match.group('stamp')}{fraction}", "%Y%m%dT%H%M%S%f"
    ).replace(tzinfo=zone)


def _infer_app_scene(artifacts: Artifacts, old: dict[str, Any]) -> tuple[str, str, str, str]:
    app_info = old.get("app_info") or {}
    app_name = str(app_info.get("app_name") or "").strip()
    if not app_name:
        directory_match = re.match(r"(.+?)-\d+-\d{8}T", artifacts.root.name)
        app_name = directory_match.group(1) if directory_match else artifacts.prefix.split("_")[0]
    config = get_app_config(app_name)
    app_type = str(app_info.get("app_type") or (config.app_type if config else "unknown"))
    package = str(app_info.get("app_package") or (config.package if config else ""))
    typed = app_info.get(app_type) if isinstance(app_info.get(app_type), dict) else {}
    scene = str(typed.get("scene") or app_info.get("scene") or "")
    if not scene and config:
        matches = [item.name for item in config.scenes if item.name in artifacts.prefix]
        scene = max(matches, key=len) if matches else config.scenes[0].name
    return app_name, app_type, package, scene or "unknown"


def rebuild_yaml(artifacts: Artifacts, target_override: int | None) -> dict[str, Any]:
    old: dict[str, Any] = {}
    if artifacts.yaml_path.exists():
        old = yaml.safe_load(artifacts.yaml_path.read_text(encoding="utf-8")) or {}
        if not isinstance(old, dict):
            raise RepairError(f"YAML根节点不是对象: {artifacts.yaml_path}")
    zone_text = str(old.get("time_zone") or "+0800")
    start = _infer_start(artifacts, old, zone_text)
    duration = probe_video_duration(artifacts.video)
    if duration is None:
        raise RepairError(f"无法读取MP4真实时长: {artifacts.video}")
    end = start + timedelta(seconds=duration)
    stream = _video_stream_info(artifacts.video)
    app_name, app_type, package, scene = _infer_app_scene(artifacts, old)
    target_match = TARGET_RE.search(artifacts.root.name)
    old_target = int(((old.get("task_info") or {}).get("business_duration") or 0))
    target = target_override or old_target or (
        int(target_match.group("duration")) if target_match else 0
    )

    data = old
    data["version"] = str(data.get("version") or "2.0.0")
    data["time_zone"] = zone_text
    data["name"] = artifacts.prefix
    task = data.setdefault("task_info", {})
    task["start_time"] = format_yaml_time(start)
    task["end_time"] = format_yaml_time(end)
    if target > 0:
        task["business_duration"] = target
    app = data.setdefault("app_info", {})
    app.update({
        "app_type": app_type,
        "app_name": app_name,
        "app_package": package,
        "app_version": str(app.get("app_version") or ""),
    })
    typed = app.setdefault(app_type, {})
    if isinstance(typed, dict):
        typed["scene"] = scene
    data.setdefault("qoe_info", {})["qoe_start_time"] = format_yaml_time(start)
    packet = data.setdefault("packet_capture", {})
    existing_packet_start = parse_dataset_time(packet.get("start_time"), zone_text)
    existing_packet_end = parse_dataset_time(packet.get("end_time"), zone_text)
    try:
        pcap_bounds = _classic_pcap_bounds(artifacts.pcap, start.tzinfo or timezone(timedelta(hours=8)))
    except OSError as exc:
        raise RepairError(f"无法读取PCAP: {exc}") from exc
    raw_packet_start, raw_packet_end = pcap_bounds or (None, None)
    if (
        existing_packet_start is not None
        and existing_packet_end is not None
        and existing_packet_end >= existing_packet_start
    ):
        packet_start, packet_end = existing_packet_start, existing_packet_end
        packet_time_source = "collector_wall_clock"
    else:
        # VPN capture timestamps can come from a different clock domain. With
        # no trusted collector boundary, video time is the safe canonical axis;
        # raw PCAP times remain available below for later alignment.
        packet_start, packet_end = start, end
        packet_time_source = "video_record_fallback"
    packet["start_time"] = format_yaml_time(packet_start)
    packet["end_time"] = format_yaml_time(packet_end)
    packet["time_source"] = packet_time_source
    if raw_packet_start is not None and raw_packet_end is not None:
        packet["raw_first_packet_time"] = raw_packet_start.isoformat()
        packet["raw_last_packet_time"] = raw_packet_end.isoformat()
        packet["raw_clock_offset_seconds"] = round(
            (raw_packet_start - packet_start).total_seconds(), 6
        )
    packet["capture_file"] = artifacts.pcap.name
    packet["format"] = artifacts.pcap.suffix.lstrip(".").lower()
    video = data.setdefault("video_record", {})
    video.update({
        "start_time": format_yaml_time(start),
        "end_time": format_yaml_time(end),
        "duration": round(duration, 3),
        "duration_source": "ffprobe",
        "width": stream["width"],
        "height": stream["height"],
        "fps": stream["fps"],
        "record_format": "mp4",
        "record_file": artifacts.video.name,
    })
    return data


def _atomic_yaml(path: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent,
        suffix=".tmp",
    ) as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _backup(artifacts: Artifacts) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = artifacts.root / "repair" / stamp / "before"
    backup.mkdir(parents=True, exist_ok=True)
    for path in (artifacts.yaml_path, artifacts.qoe_path):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    return backup


def _clamp_records(yaml_path: Path, qoe_summary: dict[str, Any]) -> None:
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    zone = str(data.get("time_zone") or "+0800")
    video = data.get("video_record") or {}
    video_start = parse_dataset_time(video.get("start_time"), zone)
    video_end = parse_dataset_time(video.get("end_time"), zone)
    if not video_start or not video_end:
        raise RepairError("重建后YAML缺少视频时间边界")
    activate = data.get("activate_record")
    if isinstance(activate, dict):
        activate_start = parse_dataset_time(activate.get("start_time"), zone) or video_start
        activate_start = min(max(activate_start, video_start), video_end)
        activate_end = parse_dataset_time(activate.get("end_time"), zone) or video_end
        activate_end = min(max(activate_end, activate_start), video_end)
        activate["start_time"] = format_yaml_time(activate_start)
        activate["end_time"] = format_yaml_time(activate_end)
        if str(activate.get("end_event") or "") and activate_end == video_end:
            activate["end_event"] = "video_record_end_fallback"
    start_second = qoe_summary.get("business_start_second")
    if start_second is not None:
        business_start = video_start + timedelta(seconds=int(start_second))
        rows = int(qoe_summary.get("business_duration_seconds") or 0)
        business_end = min(video_end, business_start + timedelta(seconds=rows))
        target = int(((data.get("task_info") or {}).get("business_duration") or 0))
        source = str(qoe_summary.get("business_start_source") or "unknown")
        data["business_record"] = {
            "start_time": format_yaml_time(business_start),
            "end_time": format_yaml_time(business_end),
            "start_event": f"{source}_business_confirmed",
            "end_event": (
                "target_business_duration_end"
                if target > 0 and rows >= target and business_end < video_end
                else "available_business_data_end"
            ),
            "source": source,
            "confidence": "high" if "vlm" in source else "medium",
        }
    _atomic_yaml(yaml_path, data)


def validate_sample(artifacts: Artifacts) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    duration = probe_video_duration(artifacts.video)
    if duration is None:
        errors.append("MP4无法由ffprobe读取")
    else:
        details["video_duration_seconds"] = round(duration, 3)
    if artifacts.pcap.stat().st_size <= 24:
        errors.append("PCAP为空或过小")
    data: dict[str, Any] = {}
    if not artifacts.yaml_path.exists():
        errors.append("YAML不存在")
    else:
        try:
            data = yaml.safe_load(artifacts.yaml_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"YAML无法解析: {exc}")
    zone = str(data.get("time_zone") or "+0800")
    video = data.get("video_record") or {}
    video_start = parse_dataset_time(video.get("start_time"), zone)
    video_end = parse_dataset_time(video.get("end_time"), zone)
    if not video_start or not video_end or video_end <= video_start:
        errors.append("video_record时间边界无效")
    elif duration is not None:
        declared = float(video.get("duration") or 0)
        if abs(declared - duration) > 0.1:
            errors.append(f"YAML视频时长与MP4不一致: {declared} vs {duration:.3f}")
    for section in ("activate_record", "business_record"):
        value = data.get(section)
        if not isinstance(value, dict):
            warnings.append(f"缺少{section}")
            continue
        start = parse_dataset_time(value.get("start_time"), zone)
        end = parse_dataset_time(value.get("end_time"), zone)
        if not start or not end or end < start:
            errors.append(f"{section}时间边界无效")
        elif video_start and video_end and (start < video_start or end > video_end):
            errors.append(f"{section}越过视频边界")
    packet = data.get("packet_capture") or {}
    try:
        raw_offset = abs(float(packet.get("raw_clock_offset_seconds") or 0.0))
    except (TypeError, ValueError):
        raw_offset = 0.0
    if raw_offset > 5.0:
        warnings.append(
            f"PCAP原始时钟与采集时间轴偏移 {raw_offset:.3f}s，已保留原始时间并使用采集墙钟"
        )
        details["pcap_raw_clock_offset_seconds"] = round(raw_offset, 6)
    rows: list[dict[str, str]] = []
    if not artifacts.qoe_path.exists():
        errors.append("QoE CSV不存在")
    else:
        try:
            with artifacts.qoe_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if not REQUIRED_QOE_FIELDS.issubset(reader.fieldnames or []):
                    errors.append("QoE CSV表头不完整")
                rows = list(reader)
        except (OSError, csv.Error) as exc:
            errors.append(f"QoE CSV无法读取: {exc}")
    details["qoe_rows"] = len(rows)
    target = int(((data.get("task_info") or {}).get("business_duration") or 0))
    details["target_business_duration"] = target or None
    if target and len(rows) < target:
        warnings.append(f"业务时长不足: {len(rows)}/{target}s")
    wrong_names = {row.get("file_name") for row in rows if row.get("file_name") != artifacts.pcap.name}
    if wrong_names:
        errors.append("QoE file_name与PCAP文件名不一致")
    status = "error" if errors else "warning" if warnings else "ok"
    return {"status": status, "errors": errors, "warnings": warnings, **details}


def repair_one(
    artifacts: Artifacts,
    *,
    check_only: bool,
    enable_vlm: bool,
    target_duration: int | None,
    ocr_every: int,
) -> dict[str, Any]:
    print(f"[repair] 样本: {artifacts.root}", flush=True)
    if check_only:
        return {"sample_dir": str(artifacts.root), "check": validate_sample(artifacts)}
    backup = _backup(artifacts)
    rebuilt = rebuild_yaml(artifacts, target_duration)
    _atomic_yaml(artifacts.yaml_path, rebuilt)

    profile = REPO_ROOT / "unicapture" / "postprocess" / "business_time_annotation_profile.json"
    report_dir = artifacts.root / "repair" / "activate_time_report"
    annotate_business_times([
        "--root", str(artifacts.root), "--write", "--min-confidence", "low",
        "--only-missing-activate", "--require-mp4",
        "--fill-missing-from-video-record", "--profile-file", str(profile),
        "--report-dir", str(report_dir),
        "--backup-dir", str(backup / "annotator"),
    ])

    hidden_qoe: Path | None = None
    try:
        if enable_vlm:
            os.environ["QOE_USE_VLM"] = "1"
        else:
            os.environ.pop("QOE_USE_VLM", None)
        # Read timing/app metadata, then hide the old QoE before replay. Old
        # annotations are never used as recognition input.
        sample = load_sample(artifacts.root)
        if artifacts.qoe_path.exists():
            hidden_qoe = artifacts.qoe_path.with_suffix(".csv.rebuild-old")
            if hidden_qoe.exists():
                hidden_qoe.unlink()
            artifacts.qoe_path.replace(hidden_qoe)
        qoe_summary = replay_collector_qoe(
            sample,
            artifacts.qoe_path,
            resolution_every=max(1, ocr_every),
        )
    except BaseException:
        if artifacts.qoe_path.exists():
            artifacts.qoe_path.unlink()
        if hidden_qoe and hidden_qoe.exists():
            hidden_qoe.replace(artifacts.qoe_path)
        raise
    else:
        if hidden_qoe and hidden_qoe.exists():
            hidden_qoe.unlink()
    _clamp_records(artifacts.yaml_path, qoe_summary)
    check = validate_sample(artifacts)
    return {
        "sample_dir": str(artifacts.root),
        "backup_dir": str(backup),
        "yaml": str(artifacts.yaml_path),
        "qoe": str(artifacts.qoe_path),
        "qoe_summary": qoe_summary,
        "check": check,
    }


def _write_report(base: Path, report: dict[str, Any]) -> Path:
    report_dir = base / "repair"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "latest_report.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从MP4和PCAP重建Unicapture YAML/QoE并执行边界检查",
    )
    parser.add_argument("path", type=Path, help="MiniPilot run目录或具体样本目录")
    parser.add_argument("--check-only", action="store_true", help="只检查，不修改文件")
    parser.add_argument("--all", action="store_true", help="允许一次处理多个样本目录")
    parser.add_argument("--enable-vlm", action="store_true", help="缺少可信业务起点时允许调用VLM")
    parser.add_argument("--target-duration", type=int, default=None, help="覆盖目标业务时长")
    parser.add_argument(
        "--ocr-every", type=int, default=1,
        help="复放采集逻辑时的分辨率识别间隔；1表示与在线采集一致（默认1）",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        samples = discover_samples(args.path)
        if not samples:
            raise RepairError(f"未发现包含MP4和PCAP的样本: {args.path}")
        if len(samples) > 1 and not args.all:
            raise RepairError(f"发现 {len(samples)} 个样本；确认批量处理请添加 --all")
        results = [
            repair_one(
                sample,
                check_only=args.check_only,
                enable_vlm=args.enable_vlm,
                target_duration=args.target_duration,
                ocr_every=args.ocr_every,
            )
            for sample in samples
        ]
        statuses = [item["check"]["status"] for item in results]
        report = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "mode": "check_only" if args.check_only else "rebuild",
            "status": "error" if "error" in statuses else "warning" if "warning" in statuses else "ok",
            "samples": results,
        }
        report_path = _write_report(args.path.resolve(), report)
        print(f"[repair] 状态: {report['status']}", flush=True)
        print(f"[repair] 报告: {report_path}", flush=True)
        return 2 if report["status"] == "error" else 0
    except (RepairError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"[repair][错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
