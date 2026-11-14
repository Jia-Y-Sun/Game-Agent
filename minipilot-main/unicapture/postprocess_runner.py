"""Run all post-capture annotations for one completed Unicapture sample."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

try:
    from .postprocess.annotate_business_times import (
        atomic_write,
        format_yaml_time,
        main as annotate_business_times,
        optional_section_value,
        parse_yaml_time,
        read_text_preserving,
        remove_top_level_block,
    )
    from .qoe_postprocess.processor import process_sample
    from .qoe_postprocess.sample import load_sample
    from .postprocess.flow_labeler import label_sample_flows
except ImportError:  # app_collector.py is also supported as a direct script.
    from postprocess.annotate_business_times import (
        atomic_write,
        format_yaml_time,
        main as annotate_business_times,
        optional_section_value,
        parse_yaml_time,
        read_text_preserving,
        remove_top_level_block,
    )
    from qoe_postprocess.processor import process_sample
    from qoe_postprocess.sample import load_sample
    from postprocess.flow_labeler import label_sample_flows


def run_sample_postprocess(
    sample_dir: str | Path,
    *,
    enable_vlm: bool = True,
    vlm_every: int = 5,
    vlm_max_calls: int = 20,
    ocr_every: int = 1,
    enable_flow_labeling: bool = True,
    flow_app_type: str | None = None,
    flow_scene: str | None = None,
    flow_rules_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run independent activation, QoE and flow-labeling stages.

    Each stage is isolated. A failed annotator is recorded in the summary and never
    invalidates the raw YAML, logcat, video or packet capture that already exists.
    """
    started = time.perf_counter()
    root = Path(sample_dir).resolve()
    report_root = root / "postprocess"
    report_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "sample_dir": str(root),
        "video_timing": {"status": "not_run"},
        "activate_time": {"status": "not_run"},
        "qoe": {"status": "not_run"},
        "flow_labeling": {"status": "not_run"},
    }

    print("[postprocess] 阶段 1/4：校准视频真实时间", flush=True)
    try:
        summary["video_timing"] = _sync_video_record_metadata(root, report_root)
    except Exception as exc:
        summary["video_timing"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    print("[postprocess] 阶段 2/4：识别业务边界", flush=True)
    try:
        profile = Path(__file__).parent / "postprocess" / "business_time_annotation_profile.json"
        exit_code = annotate_business_times([
            "--root", str(root),
            "--write",
            "--min-confidence", "low",
            "--only-missing-activate",
            "--require-mp4",
            "--fill-missing-from-video-record",
            "--profile-file", str(profile),
            "--report-dir", str(report_root / "activate_time_report"),
            "--backup-dir", str(report_root / "activate_time_backups"),
        ])
        summary["activate_time"] = {
            "status": "ok" if exit_code == 0 else "error",
            "exit_code": exit_code,
            "report_dir": str(report_root / "activate_time_report"),
        }
    except Exception as exc:
        summary["activate_time"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    print("[postprocess] 阶段 3/4：生成逐秒QoE", flush=True)
    try:
        if enable_vlm:
            os.environ["QOE_USE_VLM"] = "1"
        else:
            os.environ.pop("QOE_USE_VLM", None)
        sample = load_sample(root)
        output = root / f"{sample.name}_qoe.csv"
        if output.exists():
            backup_dir = report_root / "qoe_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, backup_dir / output.name)
        qoe_summary = process_sample(
            sample,
            output,
            vlm_every=vlm_every,
            vlm_max_calls=vlm_max_calls,
            ocr_every=max(1, ocr_every),
        )
        business_start_second = qoe_summary.get("business_start_second")
        if business_start_second is not None:
            _write_business_record(
                sample,
                int(business_start_second),
                str(qoe_summary.get("business_start_source") or "unknown"),
                actual_duration_seconds=int(
                    qoe_summary.get("business_duration_seconds") or 0
                ),
            )
        legacy_v2 = root / f"{sample.name}_qoe_v2.csv"
        if legacy_v2.exists():
            try:
                legacy_v2.unlink()
                qoe_summary["legacy_v2_cleanup"] = "deleted"
            except PermissionError:
                qoe_summary["legacy_v2_cleanup"] = "file_in_use"
                qoe_summary["legacy_v2_cleanup_path"] = str(legacy_v2)
        qoe_status = "ok" if qoe_summary.get("business_duration_ok", True) else "warning"
        summary["qoe"] = {"status": qoe_status, **qoe_summary}
    except Exception as exc:
        summary["qoe"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    print("[postprocess] 阶段 4/4：标注业务流", flush=True)
    if enable_flow_labeling:
        try:
            summary["flow_labeling"] = label_sample_flows(
                root,
                app_type=flow_app_type,
                scene=flow_scene,
                rules_path=flow_rules_path,
            )
        except Exception as exc:
            summary["flow_labeling"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        summary["flow_labeling"] = {"status": "disabled"}

    stages = (
        summary["video_timing"]["status"],
        summary["activate_time"]["status"],
        summary["qoe"]["status"],
        summary["flow_labeling"]["status"],
    )
    summary["status"] = (
        "partial_error" if "error" in stages
        else "warning" if "warning" in stages
        else "ok"
    )
    summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    summary_path = report_root / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _write_business_record(
    sample: Any,
    start_second: int,
    source: str,
    actual_duration_seconds: int | None = None,
) -> None:
    """Persist actual business start separately from logcat activation."""
    text, newline, has_bom = read_text_preserving(sample.yaml_path)
    start_time = sample.start_time + timedelta(seconds=start_second)
    target_duration = getattr(sample, "target_business_duration", None)
    if actual_duration_seconds is not None:
        actual_duration_seconds = max(0, int(actual_duration_seconds))
        end_time = start_time + timedelta(seconds=actual_duration_seconds)
        target_met = (
            target_duration is None
            or actual_duration_seconds >= int(target_duration)
        )
        end_event = (
            "target_business_duration_end"
            if target_met
            else "available_business_data_end"
        )
    elif target_duration is not None and int(target_duration) > 0:
        end_time = start_time + timedelta(seconds=int(target_duration))
        end_event = "target_business_duration_end"
    else:
        activate_end_text = optional_section_value(
            text, "activate_record", "end_time", ""
        )
        try:
            end_time = parse_yaml_time(activate_end_text)
            end_event = "activate_record_end"
        except (TypeError, ValueError):
            end_time = sample.start_time + timedelta(seconds=sample.duration_seconds)
            end_event = "video_record_end"
    if end_time <= start_time:
        end_time = start_time + timedelta(seconds=1)
        end_event = "minimum_interval_fallback"
    video_end = sample.start_time + timedelta(
        seconds=(
            getattr(sample, "duration_exact_seconds", 0.0)
            or float(sample.duration_seconds)
        )
    )
    if end_time > video_end:
        end_time = video_end
        end_event = "available_business_data_end"

    updated = remove_top_level_block(text, "business_record")
    if updated and not updated.endswith(("\n", "\r")):
        updated += newline
    updated += newline.join([
        "business_record:",
        f"  start_time: {format_yaml_time(start_time)}",
        f"  end_time: {format_yaml_time(end_time)}",
        f"  start_event: {source}_business_confirmed",
        f"  end_event: {end_event}",
        f"  source: {source}",
        f"  confidence: {'high' if 'vlm' in source else 'medium'}",
    ]) + newline
    atomic_write(sample.yaml_path, updated, has_bom)


def _sync_video_record_metadata(root: Path, report_root: Path) -> dict[str, Any]:
    """Make the MP4 container duration authoritative for YAML video timing."""
    sample = load_sample(root)
    duration_source = getattr(sample, "duration_source", "unavailable")
    if duration_source != "ffprobe":
        return {
            "status": "skipped",
            "reason": "ffprobe_duration_unavailable",
            "duration_source": duration_source,
        }

    text, newline, has_bom = read_text_preserving(sample.yaml_path)
    video_start_text = optional_section_value(text, "video_record", "start_time", "")
    try:
        video_start = parse_yaml_time(video_start_text)
    except (TypeError, ValueError):
        video_start = sample.start_time
    video_end = video_start + timedelta(seconds=sample.duration_exact_seconds)
    replacements = {
        "duration": f"{sample.duration_exact_seconds:.3f}".rstrip("0").rstrip("."),
        "end_time": format_yaml_time(video_end),
        "duration_source": "ffprobe",
    }
    updated = text
    for key, value in replacements.items():
        updated = _upsert_section_value(updated, "video_record", key, value, newline)

    changed = updated != text
    if changed:
        backup_dir = report_root / "video_timing_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / sample.yaml_path.name
        if not backup_path.exists():
            shutil.copy2(sample.yaml_path, backup_path)
        atomic_write(sample.yaml_path, updated, has_bom)
    return {
        "status": "ok",
        "changed": changed,
        "duration_source": "ffprobe",
        "declared_duration_seconds": sample.declared_duration_seconds,
        "actual_duration_seconds": round(sample.duration_exact_seconds, 3),
        "video_end_time": format_yaml_time(video_end),
    }


def _upsert_section_value(
    text: str,
    section: str,
    key: str,
    value: str,
    newline: str,
) -> str:
    section_match = re.search(
        rf"(?m)^{re.escape(section)}:[^\r\n]*(?:\r?\n|$)",
        text,
    )
    if not section_match:
        suffix = "" if not text or text.endswith(("\n", "\r")) else newline
        return text + suffix + f"{section}:{newline}  {key}: {value}{newline}"
    section_start = section_match.end()
    next_section = re.search(r"(?m)^\S[^:\r\n]*:[^\r\n]*(?:\r?\n|$)", text[section_start:])
    section_end = section_start + next_section.start() if next_section else len(text)
    block = text[section_start:section_end]
    key_match = re.search(rf"(?m)^(\s+){re.escape(key)}:\s*.*$", block)
    if key_match:
        new_block = block[:key_match.start()] + f"{key_match.group(1)}{key}: {value}" + block[key_match.end():]
    else:
        new_block = f"  {key}: {value}{newline}" + block
    return text[:section_start] + new_block + text[section_end:]
