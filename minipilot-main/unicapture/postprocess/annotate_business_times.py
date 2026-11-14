#!/usr/bin/env python3
"""Locate app foreground intervals in logcat files and annotate sample YAML files.

The script is dry-run by default. It treats the target app entering the foreground
as the business start and the target app leaving the foreground/being stopped as
the business end. For an app already resident in the background, a target-app
``TopResumedActivity`` followed by ``CaptureCtrl START`` or target-app
``TaskToBack`` is also accepted as a complete high-confidence boundary pair when
no target-app Activity START exists. A target-app ``Activity Displayed`` may be a
high-confidence start only under the stricter displayed-event timing and exit-pair
rules implemented below.
Existing ``video_record`` fields are never modified.

Examples:
    python3 annotate_business_times.py --root .
    python3 annotate_business_times.py --root . --path-contains '短视频/抖音' --limit 2
    python3 annotate_business_times.py --root . --write --min-confidence high
    python3 annotate_business_times.py --root . --write --min-confidence low \
        --fill-missing-from-video-record
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


LOG_TS_RE = re.compile(
    r"^(?P<month>\d{2})-(?P<day>\d{2})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\.(?P<millis>\d{3})\s+"
)
STREAM_START_RE = re.compile(r"^# logcat stream started at (?P<value>\S+)")
STREAM_STOP_RE = re.compile(r"^# logcat stream stopped at (?P<value>\S+)")
YAML_TIME_RE = re.compile(
    r"^(?P<base>\d{8}T\d{6})(?P<fraction>\d*)(?P<offset>[+-]\d{4})$"
)

START_KINDS = {
    "shell_launch_command",
    "activity_start",
    "monkey_launch",
    "task_to_front",
    "activity_resumed",
    "window_focus",
    "activity_displayed",
    "process_start",
}
STRONG_START_KINDS = {
    "shell_launch_command",
    "activity_start",
    "task_to_front",
    "activity_resumed",
    "monkey_launch",
}
END_KINDS = {
    "capture_stop_command",
    "force_stop",
    "task_removed",
    "process_killed",
    "activity_stopped",
    "activity_paused",
    "task_to_back",
    "window_focus_lost",
    "capture_control_start",
    "capture_control_displayed",
}

START_PRIORITY = {
    "shell_launch_command": 99,
    "activity_start": 100,
    "task_to_front": 98,
    "activity_resumed": 94,
    "monkey_launch": 92,
    "window_focus": 86,
    "activity_displayed": 80,
    "process_start": 72,
}
END_PRIORITY = {
    "capture_stop_command": 99,
    "force_stop": 100,
    "task_removed": 98,
    "process_killed": 95,
    "task_to_back": 92,
    "activity_stopped": 90,
    "capture_control_start": 88,
    "capture_control_displayed": 86,
    "activity_paused": 78,
    "window_focus_lost": 70,
}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
EXCLUDED_DIRS = {
    ".git",
    ".agents",
    ".codex",
    "business_time_backups",
    "business_time_reports",
    "activate_time_backups",
    "activate_time_reports",
    "activate_time_report",
    "qoe_backups",
    "repair",
    "postprocess",
    "__pycache__",
}
DEFAULT_CAPTURE_CONTROL_PACKAGE = "com.emanuelef.remote_capture"
DEFAULT_CAPTURE_CONTROL_ACTIVITY = "CaptureCtrl"


@dataclass
class Event:
    kind: str
    raw_time: datetime
    line_no: int
    line: str
    corrected_time: Optional[datetime] = None


@dataclass
class SampleResult:
    sample: str
    category: str
    app: str
    package: str
    yaml_path: str
    log_path: str
    video_start_time: str
    video_end_time: str
    video_path: str = ""
    raw_start_time: str = ""
    raw_end_time: str = ""
    activate_start_time: str = ""
    activate_end_time: str = ""
    start_event: str = ""
    end_event: str = ""
    boundary_rule: str = ""
    boundary_source: str = ""
    start_fallback: bool = False
    end_fallback: bool = False
    start_line: int = 0
    end_line: int = 0
    alignment_method: str = ""
    clock_offset_seconds: str = ""
    confidence: str = "low"
    status: str = "candidate"
    reason: str = ""
    yaml_written: bool = False


def parse_timezone(value: str) -> timezone:
    match = re.fullmatch(r"([+-])(\d{2})(\d{2})", value.strip().strip("'\""))
    if not match:
        raise ValueError(f"unsupported time_zone: {value!r}")
    sign = 1 if match.group(1) == "+" else -1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(sign * delta)


def parse_yaml_time(value: str) -> datetime:
    value = value.strip().strip("'\"")
    match = YAML_TIME_RE.fullmatch(value)
    if not match:
        raise ValueError(f"unsupported YAML time: {value!r}")
    base = datetime.strptime(match.group("base"), "%Y%m%dT%H%M%S")
    fraction = match.group("fraction")
    micros = int((fraction + "000000")[:6]) if fraction else 0
    tz = parse_timezone(match.group("offset"))
    return base.replace(microsecond=micros, tzinfo=tz)


def format_yaml_time(value: datetime) -> str:
    centiseconds = value.microsecond // 10_000
    offset = value.strftime("%z")
    return f"{value.strftime('%Y%m%dT%H%M%S')}{centiseconds:02d}{offset}"


def format_iso(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return value.isoformat(timespec="milliseconds")


def parse_marker_time(value: str, tz: timezone) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def parse_log_time(match: re.Match[str], reference: datetime, tz: timezone) -> datetime:
    values = {
        "month": int(match.group("month")),
        "day": int(match.group("day")),
        "hour": int(match.group("hour")),
        "minute": int(match.group("minute")),
        "second": int(match.group("second")),
        "microsecond": int(match.group("millis")) * 1000,
        "tzinfo": tz,
    }
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(datetime(year=year, **values))
        except ValueError:
            pass
    if not candidates:
        raise ValueError("invalid logcat timestamp")
    return min(candidates, key=lambda item: abs(item - reference))


def read_text_preserving(path: Path) -> tuple[str, str, bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    if has_bom:
        raw = raw[3:]
    text = raw.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, newline, has_bom


def section_block(text: str, name: str) -> str:
    match = re.search(rf"(?m)^{re.escape(name)}:\s*(?:\r?\n|$)", text)
    if not match:
        return ""
    start = match.start()
    pos = match.end()
    for line in text[pos:].splitlines(keepends=True):
        if line.strip() and not line[0].isspace():
            break
        pos += len(line)
    return text[start:pos]


def section_value(text: str, section: str, key: str) -> str:
    block = section_block(text, section)
    match = re.search(rf"(?m)^\s+{re.escape(key)}:\s*(.*?)\s*$", block)
    if not match:
        raise ValueError(f"missing {section}.{key}")
    return match.group(1).strip().strip("'\"")


def optional_section_value(text: str, section: str, key: str, default: str = "") -> str:
    try:
        return section_value(text, section, key)
    except ValueError:
        return default


def global_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(.*?)\s*$", text)
    if not match:
        raise ValueError(f"missing {key}")
    return match.group(1).strip().strip("'\"")


def remove_top_level_block(text: str, name: str) -> str:
    block = section_block(text, name)
    if not block:
        return text
    start = text.index(block)
    return text[:start] + text[start + len(block):]


def classify_event(
    line: str,
    package: str,
    capture_control_package: str = DEFAULT_CAPTURE_CONTROL_PACKAGE,
    capture_control_activity: str = DEFAULT_CAPTURE_CONTROL_ACTIVITY,
) -> Optional[str]:
    lower = line.lower()
    package_lower = package.lower()
    has_target = package_lower in lower
    has_capture = capture_control_package.lower() in lower
    capture_activity_lower = capture_control_activity.lower()

    # Some vendor builds omit ActivityTaskManager lifecycle messages but retain
    # the adb shell commands in the adbd log. These commands are strong boundary
    # signals: monkey brings the target package forward, while the capture stop
    # command brings CaptureCtrl forward and therefore backgrounds the target.
    if (
        has_capture
        and "am start" in lower
        and "action stop" in lower
        and capture_activity_lower in lower
    ):
        return "capture_stop_command"
    if has_target and f"monkey -p {package_lower}" in lower:
        return "shell_launch_command"

    if has_target:
        if "activitytaskmanager" in lower and re.search(r"\bstart\s+u\d+\b", lower):
            if "cmp=" in lower or f"{package_lower}/" in lower:
                return "activity_start"
        if " monkey " in lower and "args:" in lower and "-p" in lower:
            return "monkey_launch"
        if any(token in lower for token in (
            "movetasktofront", "move task to front", "to_front", "task to front"
        )):
            return "task_to_front"
        if any(token in lower for token in (
            "resumedactivity", "topresumedactivity", "setresumedactivity",
            "wm_set_resumed_activity", "wm_on_resume_called", "onresumed",
            "activityresuming("
        )):
            return "activity_resumed"
        if "displayed " in lower and f"{package_lower}/" in lower:
            return "activity_displayed"
        if "start proc " in lower:
            main_process = re.search(
                rf"start proc\s+\d+:{re.escape(package_lower)}(?:/|\s)", lower
            )
            if main_process:
                return "process_start"
        if any(token in lower for token in (
            "focus entering", "focus gained", "window gained focus", "hasfocus=true"
        )):
            return "window_focus"

        if "force stopping" in lower:
            return "force_stop"
        if any(token in lower for token in (
            "removing task", "remove task", "task removed", "removetask"
        )):
            return "task_removed"
        if "killing " in lower and any(token in lower for token in (
            "remove task", "stop", "user requested", "force", "user stopped"
        )):
            return "process_killed"
        if any(token in lower for token in (
            "movetasktoback", "move task to back", "task to back", "to_back"
        )):
            return "task_to_back"
        if any(token in lower for token in (
            "wm_on_stop_called", "onstop", "activity stopped", "stopping activity"
        )):
            return "activity_stopped"
        if any(token in lower for token in (
            "wm_on_paused_called", "onpause", "activity paused", "pausing activity"
        )):
            return "activity_paused"
        if any(token in lower for token in (
            "focus leaving", "focus lost", "window lost focus", "hasfocus=false"
        )):
            return "window_focus_lost"

    if has_capture:
        if "activitytaskmanager" in lower and re.search(r"\bstart\s+u\d+\b", lower):
            return "capture_control_start"
        if "displayed " in lower and capture_activity_lower in lower:
            return "capture_control_displayed"
    return None


def scan_log(
    log_path: Path,
    package: str,
    task_start: datetime,
    tz: timezone,
    capture_control_package: str = DEFAULT_CAPTURE_CONTROL_PACKAGE,
    capture_control_activity: str = DEFAULT_CAPTURE_CONTROL_ACTIVITY,
) -> tuple[list[Event], Optional[datetime], Optional[datetime], Optional[datetime]]:
    events: list[Event] = []
    marker_start: Optional[datetime] = None
    marker_stop: Optional[datetime] = None
    last_log_time: Optional[datetime] = None

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\r\n")
            start_match = STREAM_START_RE.match(line)
            if start_match:
                try:
                    marker_start = parse_marker_time(start_match.group("value"), tz)
                except ValueError:
                    pass
                continue
            stop_match = STREAM_STOP_RE.match(line)
            if stop_match:
                try:
                    marker_stop = parse_marker_time(stop_match.group("value"), tz)
                except ValueError:
                    pass
                continue

            time_match = LOG_TS_RE.match(line)
            if not time_match:
                continue
            try:
                raw_time = parse_log_time(time_match, task_start, tz)
            except ValueError:
                continue
            last_log_time = raw_time
            kind = classify_event(
                line,
                package,
                capture_control_package,
                capture_control_activity,
            )
            if kind:
                events.append(Event(kind, raw_time, line_no, line[:2000]))
    return events, marker_start, marker_stop, last_log_time


def align_events(
    events: list[Event],
    marker_start: Optional[datetime],
    marker_stop: Optional[datetime],
    last_log_time: Optional[datetime],
    task_start: datetime,
    task_end: datetime,
) -> tuple[str, timedelta, str]:
    if marker_stop and last_log_time:
        offset = last_log_time - marker_stop
        method = "log_stream_stop_anchor"
        quality = "high" if abs(offset.total_seconds()) < 86400 else "medium"
    elif last_log_time:
        offset = last_log_time - task_end
        method = "yaml_end_fallback"
        quality = "medium"
    else:
        return "unavailable", timedelta(0), "low"

    for event in events:
        event.corrected_time = event.raw_time - offset

    if marker_start and marker_stop and marker_stop <= marker_start:
        quality = "low"
    return method, offset, quality


def choose_event(
    events: Iterable[Event],
    kinds: set[str],
    priorities: dict[str, int],
    expected: datetime,
    lower: datetime,
    upper: datetime,
) -> Optional[Event]:
    candidates = [
        event for event in events
        if event.kind in kinds
        and event.corrected_time is not None
        and lower <= event.corrected_time <= upper
    ]
    if not candidates:
        return None

    def score(event: Event) -> tuple[float, int, int]:
        distance = abs((event.corrected_time - expected).total_seconds())
        priority_penalty = (100 - priorities.get(event.kind, 0)) * 0.08
        return distance + priority_penalty, -priorities.get(event.kind, 0), event.line_no

    return min(candidates, key=score)


def nearby_support(events: Iterable[Event], selected: Event, kinds: set[str], seconds: float = 10) -> int:
    supported = set()
    for event in events:
        if event.kind not in kinds or event.corrected_time is None:
            continue
        if abs((event.corrected_time - selected.corrected_time).total_seconds()) <= seconds:
            supported.add(event.kind)
    return len(supported)


def start_event_name(kind: str) -> str:
    if kind in {"task_to_front", "activity_resumed"}:
        return "app_warm_resume"
    if kind == "window_focus":
        return "app_foreground"
    return "app_launch"


def end_event_name(kind: str) -> str:
    return {
        "capture_stop_command": "app_exit_foreground",
        "force_stop": "app_force_stop",
        "task_removed": "app_task_removed",
        "process_killed": "app_process_killed",
        "task_to_back": "app_exit_foreground",
        "activity_stopped": "app_exit_foreground",
        "activity_paused": "app_exit_foreground",
        "window_focus_lost": "app_exit_foreground",
        "capture_control_start": "app_exit_foreground",
        "capture_control_displayed": "app_exit_foreground",
    }.get(kind, "app_exit")


def confidence_for(
    alignment_quality: str,
    start_event: Optional[Event],
    end_event: Optional[Event],
    start_support: int,
    end_support: int,
    task_start: datetime,
    task_end: datetime,
    target_activity_start_present: bool,
    stronger_start_near_task_start: bool,
    standard_start_distance_seconds: float = 90,
    displayed_start_distance_seconds: float = 30,
    end_distance_seconds: float = 15,
    minimum_business_duration_seconds: float = 30,
) -> tuple[str, str]:
    reasons = []
    if not start_event:
        reasons.append("no foreground-entry event")
    if not end_event:
        reasons.append("no foreground-exit event")
    if not start_event or not end_event:
        return "low", "; ".join(reasons)
    if start_event.corrected_time >= end_event.corrected_time:
        return "low", "start is not earlier than end"

    start_distance = abs((start_event.corrected_time - task_start).total_seconds())
    end_distance = abs((end_event.corrected_time - task_end).total_seconds())
    if start_distance > standard_start_distance_seconds:
        reasons.append(f"start differs from task start by {start_distance:.1f}s")
    if end_distance > max(90, end_distance_seconds):
        reasons.append(f"end differs from task end by {end_distance:.1f}s")
    elif end_distance > end_distance_seconds:
        reasons.append(f"end differs from task end by {end_distance:.1f}s")
    if alignment_quality == "low" or start_distance > 180 or end_distance > 180:
        return "low", "; ".join(reasons or ["weak time alignment"])

    # Approved warm-start rule: the app was already alive in the background, so
    # Android may emit TopResumedActivity without a new ActivityTaskManager START.
    # CaptureCtrl entering the foreground is the corresponding exit boundary.
    background_resume_capture_pair = (
        not target_activity_start_present
        and start_event.kind == "activity_resumed"
        and end_event.kind in {"capture_control_start", "task_to_back"}
    )
    if (
        alignment_quality == "high"
        and background_resume_capture_pair
        and start_distance <= standard_start_distance_seconds
        and end_distance <= end_distance_seconds
    ):
        return "high", ""

    displayed_complete_pair = (
        start_event.kind == "activity_displayed"
        and end_event.kind in {
            "task_to_back",
            "capture_stop_command",
            "capture_control_start",
            "activity_stopped",
        }
    )
    displayed_duration = (end_event.corrected_time - start_event.corrected_time).total_seconds()
    if (
        alignment_quality == "high"
        and displayed_complete_pair
        and not stronger_start_near_task_start
        and start_distance <= displayed_start_distance_seconds
        and end_distance <= end_distance_seconds
        and displayed_duration >= minimum_business_duration_seconds
    ):
        return "high", ""

    strong_start = start_event.kind in STRONG_START_KINDS
    strong_end = end_event.kind in {
        "capture_stop_command", "force_stop", "task_removed", "process_killed", "task_to_back",
        "activity_stopped", "capture_control_start", "capture_control_displayed",
    }
    if (
        alignment_quality == "high"
        and strong_start
        and strong_end
        and start_support >= 2
        and end_support >= 1
        and start_distance <= standard_start_distance_seconds
        and end_distance <= end_distance_seconds
    ):
        return "high", ""
    return "medium", "; ".join(reasons or ["only one independent boundary signal"])


def boundary_rule_for(
    start_event: Optional[Event],
    end_event: Optional[Event],
    target_activity_start_present: bool,
) -> str:
    if not start_event or not end_event:
        return ""
    if not target_activity_start_present and start_event.kind == "activity_resumed":
        if end_event.kind == "capture_control_start":
            return "background_resume_capture_control"
        if end_event.kind == "task_to_back":
            return "background_resume_task_to_back"
    if start_event.kind == "activity_displayed":
        if end_event.kind == "task_to_back":
            return "activity_displayed_task_to_back"
        if end_event.kind in {"capture_stop_command", "capture_control_start"}:
            return "activity_displayed_capture_control"
        if end_event.kind == "activity_stopped":
            return "activity_displayed_activity_stopped"
    return "standard_multi_signal"


def discover_yaml(root: Path, path_contains: str) -> list[Path]:
    found = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        current_path = Path(current)
        for name in files:
            if not name.endswith(".yaml"):
                continue
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path_contains and path_contains not in relative:
                continue
            found.append(path)
    return sorted(found)


def matching_log(yaml_path: Path) -> Optional[Path]:
    exact = yaml_path.with_suffix(".log")
    if exact.is_file():
        return exact
    candidates = sorted(yaml_path.parent.glob("*.log"))
    return candidates[0] if len(candidates) == 1 else None


def matching_video(yaml_path: Path) -> Optional[Path]:
    exact = yaml_path.with_suffix(".mp4")
    if exact.is_file():
        return exact
    candidates = sorted(yaml_path.parent.glob("*.mp4"))
    return candidates[0] if len(candidates) == 1 else None


def build_activate_block(
    start_time: datetime,
    end_time: datetime,
    start_event: str,
    end_event: str,
    source: str,
    confidence: str,
    newline: str,
) -> str:
    lines = [
        "activate_record:",
        f"  start_time: {format_yaml_time(start_time)}",
        f"  end_time: {format_yaml_time(end_time)}",
        f"  start_event: {start_event}",
        f"  end_event: {end_event}",
        f"  source: {source}",
        f"  confidence: {confidence}",
    ]
    return newline.join(lines) + newline


def validate_annotated_text(original: str, updated: str) -> None:
    original_video = section_block(original, "video_record")
    updated_video = section_block(updated, "video_record")
    if not original_video or original_video != updated_video:
        raise ValueError("video_record block changed")
    activate = section_block(updated, "activate_record")
    if not activate:
        raise ValueError("activate_record block missing after update")
    start = section_value(updated, "activate_record", "start_time")
    end = section_value(updated, "activate_record", "end_time")
    if parse_yaml_time(start) >= parse_yaml_time(end):
        raise ValueError("invalid activate_record interval")


def atomic_write(path: Path, text: str, has_bom: bool) -> None:
    data = text.encode("utf-8")
    if has_bom:
        data = b"\xef\xbb\xbf" + data
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def process_sample(
    root: Path,
    yaml_path: Path,
    args: argparse.Namespace,
    backup_root: Path,
) -> tuple[SampleResult, list[dict[str, object]]]:
    relative = yaml_path.relative_to(root)
    parts = relative.parts
    category = parts[0] if len(parts) >= 1 else ""
    app = parts[1] if len(parts) >= 2 else ""
    sample = parts[-2] if len(parts) >= 2 else yaml_path.stem
    log_path = matching_log(yaml_path)
    video_path = matching_video(yaml_path)
    result = SampleResult(
        sample=sample,
        category=category,
        app=app,
        package="",
        yaml_path=relative.as_posix(),
        log_path=log_path.relative_to(root).as_posix() if log_path else "",
        video_start_time="",
        video_end_time="",
        video_path=video_path.relative_to(root).as_posix() if video_path else "",
    )
    evidence: list[dict[str, object]] = []

    try:
        text, newline, has_bom = read_text_preserving(yaml_path)
        if not section_block(text, "app_info") or not section_block(text, "video_record"):
            result.status = "ignored_non_sample_yaml"
            result.reason = "missing app_info or video_record sample sections"
            return result, evidence
        result.category = optional_section_value(text, "app_info", "app_type", category)
        result.app = optional_section_value(text, "app_info", "app_name", app)
        result.package = global_value(text, "app_package")
        existing_activate = bool(section_block(text, "activate_record"))
        if (
            existing_activate
            and getattr(args, "only_missing_activate", False)
            and not args.overwrite_activate
        ):
            result.activate_start_time = optional_section_value(
                text, "activate_record", "start_time"
            )
            result.activate_end_time = optional_section_value(
                text, "activate_record", "end_time"
            )
            result.start_event = optional_section_value(
                text, "activate_record", "start_event"
            )
            result.end_event = optional_section_value(
                text, "activate_record", "end_event"
            )
            result.boundary_source = optional_section_value(
                text, "activate_record", "source"
            )
            result.confidence = optional_section_value(
                text, "activate_record", "confidence", "low"
            )
            result.status = "skipped_existing_activate_record"
            return result, evidence
        missing_files = []
        if not log_path:
            missing_files.append("logcat")
        if getattr(args, "require_mp4", False) and not video_path:
            missing_files.append("mp4")
        if missing_files:
            result.status = "incomplete_sample"
            result.confidence = "low"
            result.reason = "missing required file(s): " + ", ".join(missing_files)
            return result, evidence
        timezone_value = global_value(text, "time_zone")
        tz = parse_timezone(timezone_value)
        task_start_text = section_value(text, "task_info", "start_time")
        task_end_text = section_value(text, "task_info", "end_time")
        result.video_start_time = section_value(text, "video_record", "start_time")
        result.video_end_time = section_value(text, "video_record", "end_time")
        task_start = parse_yaml_time(task_start_text)
        task_end = parse_yaml_time(task_end_text)
        video_start = parse_yaml_time(result.video_start_time)
        video_end = parse_yaml_time(result.video_end_time)
        if not log_path:
            raise ValueError("matching log file not found")

        events, marker_start, marker_stop, last_log_time = scan_log(
            log_path,
            result.package,
            task_start,
            tz,
            getattr(args, "capture_control_package", DEFAULT_CAPTURE_CONTROL_PACKAGE),
            getattr(args, "capture_control_activity", DEFAULT_CAPTURE_CONTROL_ACTIVITY),
        )
        method, offset, alignment_quality = align_events(
            events, marker_start, marker_stop, last_log_time, task_start, task_end
        )
        result.alignment_method = method
        result.clock_offset_seconds = f"{offset.total_seconds():.3f}"

        lower = task_start - timedelta(seconds=args.boundary_window_seconds)
        upper = task_end + timedelta(seconds=args.boundary_window_seconds)
        if marker_start and method == "log_stream_stop_anchor":
            lower = max(lower, marker_start - timedelta(seconds=5))
        if marker_stop and method == "log_stream_stop_anchor":
            upper = min(upper, marker_stop + timedelta(seconds=5))

        selected_start = choose_event(
            events, START_KINDS, START_PRIORITY, task_start, lower, upper
        )
        if selected_start and selected_start.kind == "activity_displayed":
            displayed_window = getattr(args, "displayed_start_distance_seconds", 30)
            stronger_lower = max(
                lower, task_start - timedelta(seconds=displayed_window)
            )
            stronger_upper = min(
                upper, task_start + timedelta(seconds=displayed_window)
            )
            stronger_start = choose_event(
                events,
                STRONG_START_KINDS,
                START_PRIORITY,
                task_start,
                stronger_lower,
                stronger_upper,
            )
            if stronger_start:
                selected_start = stronger_start
        end_lower = (
            selected_start.corrected_time + timedelta(seconds=1)
            if selected_start and selected_start.corrected_time
            else task_start
        )
        selected_end = choose_event(
            events, END_KINDS, END_PRIORITY, task_end, end_lower, upper
        )
        support_window = getattr(args, "support_window_seconds", 10)
        start_support = (
            nearby_support(events, selected_start, START_KINDS, support_window)
            if selected_start else 0
        )
        end_support = (
            nearby_support(events, selected_end, END_KINDS, support_window)
            if selected_end else 0
        )
        target_activity_start_present = any(
            event.kind == "activity_start" for event in events
        )
        stronger_start_near_task_start = any(
            event.kind in STRONG_START_KINDS
            and event.corrected_time is not None
            and abs((event.corrected_time - task_start).total_seconds())
            <= getattr(args, "displayed_start_distance_seconds", 30)
            for event in events
        )
        result.boundary_rule = boundary_rule_for(
            selected_start,
            selected_end,
            target_activity_start_present,
        )
        confidence, reason = confidence_for(
            alignment_quality,
            selected_start,
            selected_end,
            start_support,
            end_support,
            task_start,
            task_end,
            target_activity_start_present,
            stronger_start_near_task_start,
            getattr(args, "standard_start_distance_seconds", 90),
            getattr(args, "displayed_start_distance_seconds", 30),
            getattr(args, "end_distance_seconds", 15),
            getattr(args, "minimum_business_duration_seconds", 30),
        )
        result.confidence = confidence
        result.reason = reason

        if selected_start and selected_start.corrected_time:
            result.raw_start_time = format_iso(selected_start.raw_time)
            result.activate_start_time = format_yaml_time(selected_start.corrected_time)
            result.start_event = start_event_name(selected_start.kind)
            result.start_line = selected_start.line_no
            evidence.append({
                "sample": sample,
                "boundary": "start",
                "event_kind": selected_start.kind,
                "raw_logcat_time": format_iso(selected_start.raw_time),
                "corrected_time": format_iso(selected_start.corrected_time),
                "line_no": selected_start.line_no,
                "log_line": selected_start.line,
            })
        if selected_end and selected_end.corrected_time:
            result.raw_end_time = format_iso(selected_end.raw_time)
            result.activate_end_time = format_yaml_time(selected_end.corrected_time)
            result.end_event = end_event_name(selected_end.kind)
            result.end_line = selected_end.line_no
            evidence.append({
                "sample": sample,
                "boundary": "end",
                "event_kind": selected_end.kind,
                "raw_logcat_time": format_iso(selected_end.raw_time),
                "corrected_time": format_iso(selected_end.corrected_time),
                "line_no": selected_end.line_no,
                "log_line": selected_end.line,
            })

        effective_start = (
            selected_start.corrected_time
            if selected_start and selected_start.corrected_time
            else None
        )
        effective_end = (
            selected_end.corrected_time
            if selected_end and selected_end.corrected_time
            else None
        )
        fill_missing = getattr(args, "fill_missing_from_video_record", False)
        fallback_notes = []
        if fill_missing and effective_start is None:
            effective_start = video_start
            result.start_fallback = True
            result.start_event = "video_record_start_fallback"
            fallback_notes.append("start reused video_record.start_time")
        if fill_missing and effective_end is None:
            effective_end = video_end
            result.end_fallback = True
            result.end_event = "video_record_end_fallback"
            fallback_notes.append("end reused video_record.end_time")
        if (
            fill_missing
            and effective_start is not None
            and effective_end is not None
            and effective_start >= effective_end
        ):
            effective_start = video_start
            effective_end = video_end
            result.start_fallback = True
            result.end_fallback = True
            result.start_event = "video_record_start_fallback"
            result.end_event = "video_record_end_fallback"
            fallback_notes.append("invalid candidate interval replaced by full video_record interval")

        if result.start_fallback and result.end_fallback:
            result.boundary_rule = "video_record_full_fallback"
            result.boundary_source = "video_record_fallback"
        elif result.start_fallback:
            result.boundary_rule = "video_record_start_fallback"
            result.boundary_source = "logcat+video_record_fallback"
        elif result.end_fallback:
            result.boundary_rule = "video_record_end_fallback"
            result.boundary_source = "logcat+video_record_fallback"
        else:
            result.boundary_source = "logcat"
        if effective_start is not None:
            result.activate_start_time = format_yaml_time(effective_start)
        if effective_end is not None:
            result.activate_end_time = format_yaml_time(effective_end)
        if fallback_notes:
            fallback_reason = "; ".join(fallback_notes)
            result.reason = "; ".join(
                item for item in (result.reason, fallback_reason) if item
            )

        eligible = (
            effective_start is not None
            and effective_end is not None
            and effective_start < effective_end
            and CONFIDENCE_RANK[confidence] >= CONFIDENCE_RANK[args.min_confidence]
        )
        if existing_activate and not args.overwrite_activate:
            result.status = "skipped_existing_activate_record"
            return result, evidence
        if not eligible:
            result.status = "needs_review"
            return result, evidence
        if not args.write:
            result.status = (
                "dry_run_eligible"
                if confidence == "high" and not fallback_notes
                else "dry_run_eligible_needs_review"
            )
            return result, evidence

        updated = remove_top_level_block(text, "activate_record")
        # Migrate records produced by older versions where logcat activation
        # was incorrectly stored as business time. Preserve genuine VLM data.
        legacy_business_source = optional_section_value(
            updated, "business_record", "source", ""
        ).lower()
        if legacy_business_source in {
            "logcat", "logcat+video_record_fallback", "video_record_fallback"
        }:
            updated = remove_top_level_block(updated, "business_record")
        if updated and not updated.endswith(("\n", "\r")):
            updated += newline
        updated += build_activate_block(
            effective_start,
            effective_end,
            result.start_event,
            result.end_event,
            result.boundary_source,
            confidence,
            newline,
        )
        validate_annotated_text(text, updated)

        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(yaml_path, backup_path)
        atomic_write(yaml_path, updated, has_bom)

        written_text, _, _ = read_text_preserving(yaml_path)
        validate_annotated_text(text, written_text)
        result.yaml_written = True
        result.status = (
            "written"
            if confidence == "high" and not fallback_notes
            else "written_needs_review"
        )
        return result, evidence
    except Exception as exc:
        result.status = "error"
        result.reason = f"{type(exc).__name__}: {exc}"
        return result, evidence


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="dataset root")
    parser.add_argument("--write", action="store_true", help="write eligible YAML files")
    parser.add_argument(
        "--min-confidence", choices=("high", "medium", "low"), default="high",
        help="minimum confidence required for writing (default: high)",
    )
    parser.add_argument(
        "--overwrite-activate", action="store_true",
        help="replace an existing top-level activate_record block",
    )
    parser.add_argument(
        "--only-missing-activate", action="store_true",
        help="skip an existing activate_record before scanning its large logcat",
    )
    parser.add_argument(
        "--require-mp4", action="store_true",
        help="treat a sample without a matching MP4 as incomplete",
    )
    parser.add_argument(
        "--fill-missing-from-video-record", action="store_true",
        help=(
            "reuse video_record start/end for a missing boundary; fallback records "
            "remain low-confidence and are retained in the review report"
        ),
    )
    parser.add_argument("--path-contains", default="", help="only process matching paths")
    parser.add_argument("--limit", type=int, default=0, help="process at most N samples")
    parser.add_argument(
        "--boundary-window-seconds", type=int, default=120,
        help="candidate search padding around task interval",
    )
    parser.add_argument(
        "--standard-start-distance-seconds", type=float, default=90,
        help="maximum task-start distance for ordinary/resume high confidence",
    )
    parser.add_argument(
        "--displayed-start-distance-seconds", type=float, default=30,
        help="maximum task-start distance for Activity Displayed high confidence",
    )
    parser.add_argument(
        "--end-distance-seconds", type=float, default=15,
        help="maximum task-end distance for high confidence",
    )
    parser.add_argument(
        "--support-window-seconds", type=float, default=10,
        help="window for independent supporting event kinds",
    )
    parser.add_argument(
        "--minimum-business-duration-seconds", type=float, default=30,
        help="minimum duration for a Displayed-based complete interval",
    )
    parser.add_argument(
        "--capture-control-package", default=DEFAULT_CAPTURE_CONTROL_PACKAGE,
        help="capture-control Android package used as an exit signal",
    )
    parser.add_argument(
        "--capture-control-activity", default=DEFAULT_CAPTURE_CONTROL_ACTIVITY,
        help="capture-control Activity name used as an exit signal",
    )
    parser.add_argument(
        "--profile-file", type=Path,
        help="profile file recorded in the summary for traceability",
    )
    parser.add_argument("--report-dir", type=Path, help="custom report directory")
    parser.add_argument("--backup-dir", type=Path, help="custom YAML backup directory")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"root is not a directory: {root}")
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    report_dir = (args.report_dir or root / "activate_time_reports" / run_id).resolve()
    backup_dir = (args.backup_dir or root / "activate_time_backups" / run_id).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    yaml_paths = discover_yaml(root, args.path_contains)
    if args.limit > 0:
        yaml_paths = yaml_paths[:args.limit]

    results: list[SampleResult] = []
    evidence_rows: list[dict[str, object]] = []
    for index, yaml_path in enumerate(yaml_paths, 1):
        result, evidence = process_sample(root, yaml_path, args, backup_dir)
        results.append(result)
        evidence_rows.extend(evidence)
        print(
            f"[{index}/{len(yaml_paths)}] {result.status:32s} "
            f"{result.confidence:6s} {result.yaml_path}",
            file=sys.stderr,
        )

    candidate_rows = [asdict(result) for result in results]
    candidate_fields = list(asdict(SampleResult("", "", "", "", "", "", "", "")).keys())
    write_csv(report_dir / "activate_time_candidates.csv", candidate_rows, candidate_fields)
    write_csv(
        report_dir / "activate_time_evidence.csv",
        evidence_rows,
        [
            "sample", "boundary", "event_kind", "raw_logcat_time",
            "corrected_time", "line_no", "log_line",
        ],
    )
    exception_rows = [
        row for row in candidate_rows
        if row["status"] in {
            "needs_review", "written_needs_review", "incomplete_sample", "error"
        }
        or row["confidence"] in {"medium", "low"}
        or row["start_fallback"]
        or row["end_fallback"]
    ]
    write_csv(
        report_dir / "activate_time_exceptions.csv",
        exception_rows,
        candidate_fields,
    )

    status_counts: dict[str, int] = {}
    confidence_counts: dict[str, int] = {}
    for result in results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        confidence_counts[result.confidence] = confidence_counts.get(result.confidence, 0) + 1
    profile_path = args.profile_file.resolve() if args.profile_file else None
    profile_sha256 = ""
    if profile_path and profile_path.is_file():
        profile_sha256 = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    summary = {
        "root": str(root),
        "mode": "write" if args.write else "dry-run",
        "processed": len(results),
        "minimum_write_confidence": args.min_confidence,
        "status_counts": status_counts,
        "confidence_counts": confidence_counts,
        "fallback_counts": {
            "start": sum(1 for result in results if result.start_fallback),
            "end": sum(1 for result in results if result.end_fallback),
            "full_interval": sum(
                1 for result in results
                if result.start_fallback and result.end_fallback
            ),
        },
        "report_dir": str(report_dir),
        "backup_dir": str(backup_dir) if args.write else "",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "profile_file": str(profile_path) if profile_path else "",
        "profile_sha256": profile_sha256,
    }
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not status_counts.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())


