from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from ..video_metadata import probe_video_duration
except ImportError:  # Direct execution from the unicapture directory.
    from video_metadata import probe_video_duration


DATASET_TIME_RE = re.compile(r"(?P<date>\d{8})T(?P<time>\d{6})(?P<fraction>\d{0,6})(?P<zone>[+-]\d{4})?")
SCREEN_TIME_RE = re.compile(r"screen_(\d{8})_(\d{6})", re.IGNORECASE)


def parse_dataset_time(value: Any, default_zone: str = "+0800") -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    try:
        parsed_iso = datetime.fromisoformat(text)
        if parsed_iso.tzinfo is None:
            parsed_iso = parsed_iso.replace(
                tzinfo=timezone(timedelta(hours=8))
            )
        return parsed_iso
    except ValueError:
        pass
    match = DATASET_TIME_RE.search(text)
    if match:
        fraction = match.group("fraction").ljust(6, "0")
        zone = match.group("zone") or default_zone
        normalized = f"{match.group('date')}T{match.group('time')}{fraction}{zone}"
        return datetime.strptime(normalized, "%Y%m%dT%H%M%S%f%z")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            return parsed
        except ValueError:
            continue
    return None


def screenshot_time(path: Path, zone: timezone) -> Optional[datetime]:
    match = SCREEN_TIME_RE.search(path.stem)
    if not match:
        return None
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=zone)


@dataclass(frozen=True)
class Sample:
    root: Path
    yaml_path: Path
    video_path: Optional[Path]
    pcap_path: Optional[Path]
    screenshots: tuple[tuple[datetime, Path], ...]
    name: str
    app_type: str
    app_name: str
    package: str
    scene: str
    activate_start_second: Optional[int]
    vlm_business_start_second: Optional[int]
    timing_business_start_second: Optional[int]
    timing_business_start_source: str
    target_business_duration: Optional[int]
    initial_resolution: Optional[int]
    source_resolution_series: tuple[tuple[int, int], ...]
    source_stall_series: tuple[tuple[int, int], ...]
    start_time: datetime
    duration_seconds: int
    duration_exact_seconds: float = 0.0
    declared_duration_seconds: float = 0.0
    duration_source: str = "yaml"

    @property
    def pcap_name(self) -> str:
        if self.pcap_path:
            return self.pcap_path.name
        return f"{self.name}.pcap"


def _first(root: Path, patterns: tuple[str, ...]) -> Optional[Path]:
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _first_pcap(root: Path) -> Optional[Path]:
    """Prefer current PCAP names, but keep reading legacy cut_* samples."""
    matches = sorted((*root.glob("*.pcap"), *root.glob("*.pcapng")))
    for path in matches:
        if not path.name.startswith("cut_"):
            return path
    return matches[0] if matches else None


def load_sample(root: Path) -> Sample:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"样本目录不存在: {root}")
    yaml_path = _first(root, ("*.yaml", "*.yml"))
    if not yaml_path:
        raise FileNotFoundError(f"样本目录中没有 YAML: {root}")
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    task = data.get("task_info") or {}
    app = data.get("app_info") or {}
    video = data.get("video_record") or {}
    zone_text = str(data.get("time_zone") or "+0800")
    start = (
        parse_dataset_time(video.get("start_time"), zone_text)
        or parse_dataset_time(task.get("start_time"), zone_text)
    )
    if not start:
        raise ValueError(f"YAML 中缺少有效开始时间: {yaml_path}")

    video_path = _first(root, ("*.mp4",))
    declared_duration = float(video.get("duration") or 0)
    probed_duration = probe_video_duration(video_path)
    duration_exact = probed_duration or declared_duration
    duration_source = "ffprobe" if probed_duration is not None else "yaml"
    if duration_exact <= 0:
        end = parse_dataset_time(video.get("end_time") or task.get("end_time"), zone_text)
        duration_exact = max(1.0, (end - start).total_seconds()) if end else 1.0
        duration_source = "yaml_end_time"
    duration = max(1, int(math.ceil(duration_exact)))

    app_type = str(app.get("app_type") or "unknown")
    typed_app = app.get(app_type) if isinstance(app.get(app_type), dict) else {}
    if not typed_app:
        typed_app = next(
            (
                value for value in app.values()
                if isinstance(value, dict) and value.get("scene")
            ),
            {},
        )
    scene = str(typed_app.get("scene") or app.get("scene") or "unknown")
    image_files = []
    for extension in ("*.png", "*.jpg", "*.jpeg"):
        image_files.extend(root.rglob(extension))
    timed_images = []
    for path in sorted(set(image_files)):
        timestamp = screenshot_time(path, start.tzinfo or timezone(timedelta(hours=8)))
        if timestamp:
            timed_images.append((timestamp, path))

    activate = data.get("activate_record") or {}
    activate_start = parse_dataset_time(activate.get("start_time"), zone_text)
    activate_start_second = (
        max(0, int(round((activate_start - start).total_seconds())))
        if activate_start is not None
        else None
    )
    business = data.get("business_record") or {}
    business_source = str(business.get("source") or "").lower()
    business_start = parse_dataset_time(business.get("start_time"), zone_text)
    vlm_business_start_second = (
        max(0, int(round((business_start - start).total_seconds())))
        if business_start is not None and "vlm" in business_source
        else None
    )
    timing_business_start_second: Optional[int] = None
    timing_business_start_source = ""
    configured_business_duration = int(task.get("business_duration") or 0)
    target_business_duration: Optional[int] = (
        configured_business_duration if configured_business_duration > 0 else None
    )
    timing_path = root / "business_timing.json"
    if not timing_path.exists():
        timing_path = root.parent / "business_timing.json"
    if timing_path.exists():
        try:
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            timing_start = parse_dataset_time(
                timing.get("business_start_time"), zone_text
            )
            if timing_start is not None:
                timing_business_start_second = max(
                    0, int(round((timing_start - start).total_seconds()))
                )
            timing_business_start_source = str(timing.get("source") or "")
            configured_duration = int(timing.get("target_duration_seconds") or 0)
            if configured_duration > 0:
                target_business_duration = configured_duration
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    initial_resolution: Optional[int] = None
    source_resolution_series: list[tuple[int, int]] = []
    source_stall_series: list[tuple[int, int]] = []
    source_qoe_files = [
        path for path in sorted(root.glob("*_qoe.csv"))
        if "_qoe_v2" not in path.stem
    ]
    if source_qoe_files:
        try:
            with source_qoe_files[0].open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        resolution = int(row.get("trust_resolution") or -4)
                    except (TypeError, ValueError):
                        resolution = -4
                    if resolution > 0 and initial_resolution is None:
                        initial_resolution = resolution
                    row_time = parse_dataset_time(row.get("time"), zone_text)
                    if row_time:
                        offset = max(0, int(round((row_time - start).total_seconds())))
                        source_resolution_series.append((offset, resolution))
                        try:
                            source_stall_series.append((offset, int(row.get("trust_stall") or -4)))
                        except (TypeError, ValueError):
                            pass
        except (OSError, csv.Error):
            pass

    return Sample(
        root=root,
        yaml_path=yaml_path,
        video_path=video_path,
        pcap_path=_first_pcap(root),
        screenshots=tuple(sorted(timed_images)),
        name=str(data.get("name") or yaml_path.stem),
        app_type=app_type,
        app_name=str(app.get("app_name") or "unknown"),
        package=str(app.get("app_package") or ""),
        scene=scene,
        activate_start_second=activate_start_second,
        vlm_business_start_second=vlm_business_start_second,
        timing_business_start_second=timing_business_start_second,
        timing_business_start_source=timing_business_start_source,
        target_business_duration=target_business_duration,
        initial_resolution=initial_resolution,
        source_resolution_series=tuple(source_resolution_series),
        source_stall_series=tuple(source_stall_series),
        start_time=start,
        duration_seconds=duration,
        duration_exact_seconds=duration_exact,
        declared_duration_seconds=declared_duration,
        duration_source=duration_source,
    )

