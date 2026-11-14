from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from statistics import median
from typing import Optional

import numpy as np

from .media import FrameProvider, motion_gray, motion_metrics, validate_video
from .perception import OCRPerception, Perception, VLMPerception, is_business_evidence
from .sample import Sample
from .schema import CSV_FIELDS, NOT_APPLICABLE, UNKNOWN, metric_policy


@dataclass
class InternalSecond:
    second: int
    perception: Perception
    has_frame: bool
    source: str
    low_motion: Optional[bool] = None
    business_evidence: bool = False


def _format_time(sample: Sample, second: int) -> str:
    timestamp = sample.start_time + timedelta(seconds=second)
    centiseconds = timestamp.microsecond // 10_000
    return f"{timestamp.strftime('%Y-%m-%d %H:%M:%S')}.{centiseconds:02d}{timestamp.strftime('%z')}"


def _print_progress(
    label: str,
    current: int,
    total: int,
    started: float,
    *,
    force: bool = False,
) -> None:
    """Show a time-axis, percentage, elapsed time and ETA without looking hung."""
    total = max(1, total)
    current = min(max(0, current), total)
    if not force and current not in {0, total} and current % 10:
        return
    ratio = current / total
    width = 24
    filled = min(width, int(round(width * ratio)))
    bar = "█" * filled + "·" * (width - filled)
    elapsed = max(0.0, time.perf_counter() - started)
    eta = (elapsed / ratio - elapsed) if ratio > 0 else 0.0
    message = (
        f"[qoe-postprocess] {label} [{bar}] {ratio * 100:6.2f}% "
        f"时间轴 {current:>3}/{total}s  已用 {elapsed:5.1f}s"
    )
    if ratio > 0 and ratio < 1:
        message += f"  预计剩余 {eta:5.1f}s"
    if sys.stdout.isatty():
        print("\r" + message.ljust(120), end="\n" if current >= total else "", flush=True)
    else:
        print(message, flush=True)


def _find_business_start(records: list[InternalSecond], confirmations: int = 2) -> Optional[int]:
    candidates = [record for record in records if record.business_evidence]
    if not candidates:
        return None
    # Evidence does not need to be adjacent when screenshot fallback is sparse,
    # but it must repeat in a small wall-clock window.
    for index in range(len(candidates) - confirmations + 1):
        window = candidates[index:index + confirmations]
        if window[-1].second - window[0].second <= 10:
            return window[0].second
    return candidates[0].second if confirmations <= 1 else None


def _validated_latency(current: Optional[int], previous: Optional[int]) -> Optional[int]:
    """Reject a common occlusion OCR failure such as 44ms being read as 4ms."""
    if current is None:
        return None
    if previous is not None and previous >= 20 and current < 10:
        return None
    return current


def _has_trusted_runtime_business_timing(sample: Sample) -> bool:
    """Return whether runtime already confirmed the user-visible business start."""
    if sample.timing_business_start_second is None:
        return False
    source = (sample.timing_business_start_source or "").strip().lower()
    if not source:
        return False
    # Runtime/offline/manual VLM confirmation is sufficient. In video scenes,
    # advertisements are part of the requested business and remain valid.
    return source in {
        "vlm",
        "vlm_scene_confirmed",
        "offline_video_vlm",
        "manual_business_confirmed",
        "game_script_match_ready",
    }


def _mark_stalls(records: list[InternalSecond], start_second: Optional[int]) -> set[int]:
    if start_second is None:
        return set()
    stalls: set[int] = set()
    run: list[int] = []
    for record in records:
        if record.second < start_second or record.low_motion is None:
            run = []
            continue
        if record.low_motion:
            run.append(record.second)
            if len(run) >= 3:
                stalls.update(run)
        else:
            run = []
    return stalls


def _select_business_records(
    records: list[InternalSecond],
    start_second: Optional[int],
    target_duration: Optional[int],
) -> list[InternalSecond]:
    """Return only the effective business interval, starting with its first second."""
    if start_second is None:
        return []
    selected = [record for record in records if record.second >= start_second]
    if target_duration is not None:
        selected = selected[:max(0, target_duration)]
    return selected


def process_sample(
    sample: Sample,
    output_path: Path,
    *,
    max_seconds: int = 0,
    validate: bool = True,
    vlm_every: int = 2,
    screenshots_only: bool = False,
    use_source_stall: bool = True,
    vlm_max_calls: int = 20,
    ocr_every: int = 1,
) -> dict[str, object]:
    analysis_started = time.perf_counter()
    policy = metric_policy(sample.app_type)
    scene_policy = metric_policy(sample.scene)
    if not (policy.latency or policy.resolution or policy.stall):
        policy = scene_policy
    print("[qoe-postprocess] 阶段 1/3：校验视频文件", flush=True)
    validation = validate_video(sample.video_path) if validate and not screenshots_only else None
    video_usable = (bool(sample.video_path) if validation is None else validation.usable) and not screenshots_only
    frame_prepare_started = time.perf_counter()
    print("[qoe-postprocess] 阶段 2/3：按时间轴预提取视频帧", flush=True)

    def frame_progress(ratio: float) -> None:
        _print_progress(
            "提取帧",
            int(round(max(0.0, min(1.0, ratio)) * sample.duration_seconds)),
            sample.duration_seconds,
            frame_prepare_started,
            force=ratio >= 1.0,
        )

    provider = FrameProvider(
        sample,
        video_usable=video_usable,
        progress_callback=frame_progress,
    )
    print("[qoe-postprocess] 阶段 3/3：逐秒识别并生成QoE", flush=True)
    ocr = OCRPerception(app_name=sample.app_name, scene=sample.scene)
    # Post-processing may use a later native screenshot to learn a stable OCR
    # region. Only its geometry is retained; its numeric value is not copied to
    # any other second.
    if policy.latency and sample.screenshots:
        try:
            import cv2

            raw = np.fromfile(str(sample.screenshots[0][1]), dtype=np.uint8)
            calibration_image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if calibration_image is not None:
                ocr.analyze(calibration_image)
                if ocr.has_latency_roi:
                    ocr.tracked_only = True
        except (OSError, ValueError):
            pass
    trusted_runtime_timing = _has_trusted_runtime_business_timing(sample)
    # Re-running the VLM is both redundant and potentially very slow when the
    # online controller already persisted a trusted business marker. Numeric
    # QoE still gets recomputed from every video second below.
    vlm = None if trusted_runtime_timing else VLMPerception.from_environment()
    records: list[InternalSecond] = []
    previous_gray: Optional[np.ndarray] = None
    provisional_evidence: list[int] = []
    vlm_business_confirmed = False
    vlm_calls = 0
    vlm_active_seconds: list[int] = []
    vlm_observations: list[dict[str, object]] = []
    limit = sample.duration_seconds if max_seconds <= 0 else min(sample.duration_seconds, max_seconds)

    try:
        for second in range(limit):
            _print_progress("分析", second, limit, analysis_started)
            image, source = provider.get(second)
            if image is None:
                records.append(InternalSecond(second, Perception(), False, source))
                previous_gray = None
                continue
            # Until a latency display is discovered, full-screen OCR every two
            # seconds is enough. Afterwards recognition tracks the learned ROI
            # every second and is orders of magnitude faster.
            if (
                policy.latency
                and not ocr.has_latency_roi
                and second % max(2, ocr_every)
                and source == "video"
            ):
                perceived = Perception()
            elif policy.resolution and sample.source_resolution_series:
                # A complete collector time series is stronger than unconstrained
                # full-screen OCR, which can mistake numbers inside video content
                # for a quality label. Resample/fill that series instead.
                perceived = Perception()
            elif policy.resolution and sample.initial_resolution is not None and second % 30:
                # Preserve the previous/collector resolution prior and only scan
                # periodically for an explicit quality change on screen.
                perceived = Perception()
            elif policy.resolution and second % max(1, ocr_every):
                # Offline repair can sample expensive full-screen OCR at a
                # stable interval while motion/stall analysis remains 1 Hz.
                perceived = Perception()
            else:
                perceived = ocr.analyze(image)
            if (
                vlm
                and not vlm_business_confirmed
                and vlm_calls < max(1, vlm_max_calls)
                and second % max(1, vlm_every) == 0
            ):
                try:
                    vlm_calls += 1
                    print(
                        f"[qoe-postprocess] VLM识别 {vlm_calls}/{max(1, vlm_max_calls)}，"
                        f"视频第 {second}s",
                        flush=True,
                    )
                    perceived = replace(perceived, vlm_state=vlm.classify(image, sample.app_type, sample.scene))
                    vlm_observations.append({"second": second, "state": perceived.vlm_state})
                    if perceived.vlm_state in {"GAMEPLAY", "PLAYING", "LIVE", "MEETING", "CALL"}:
                        vlm_active_seconds.append(second)
                        recent_vlm = [value for value in vlm_active_seconds if second - value <= 10]
                        vlm_business_confirmed = len(recent_vlm) >= 2
                except Exception as exc:
                    print(f"[qoe-postprocess] VLM 第 {second} 秒失败，降级为 OCR/时序规则: {exc}")
            if policy.stall:
                mad, changed_ratio = motion_metrics(previous_gray, image)
                previous_gray = motion_gray(image)
                low_motion = mad <= 1.8 and changed_ratio <= 0.006
            else:
                mad = 0.0
                changed_ratio = 0.0
                previous_gray = None
                low_motion = None
            evidence = is_business_evidence(sample.app_type, perceived)
            if (
                not evidence
                and policy.stall
                and second >= 3
                and not perceived.loading
                and mad >= 3.0
                and changed_ratio >= 0.02
            ):
                evidence = True
            if evidence:
                provisional_evidence.append(second)
                provisional_evidence = [value for value in provisional_evidence if second - value <= 10]
            records.append(InternalSecond(
                second=second,
                perception=perceived,
                has_frame=True,
                source=source,
                low_motion=low_motion,
                business_evidence=evidence,
            ))
    finally:
        provider.close()
    _print_progress("分析", limit, limit, analysis_started, force=True)

    vlm_records = [
        InternalSecond(second=value, perception=Perception(vlm_state="PLAYING"), has_frame=True, source="vlm", business_evidence=True)
        for value in vlm_active_seconds
    ]
    vlm_start = _find_business_start(vlm_records)
    # Business start is intentionally VLM-only. Logcat marks app activation,
    # while OCR/motion and the collector's original QoE are not allowed to
    # masquerade as the actual start of user-visible business content.
    business_start = (
        sample.timing_business_start_second
        if sample.timing_business_start_second is not None
        else vlm_start
        if vlm_start is not None
        else sample.vlm_business_start_second
        if sample.vlm_business_start_second is not None
        else sample.activate_start_second
    )
    if sample.timing_business_start_second is not None:
        business_start_source = sample.timing_business_start_source or "runtime_monitor"
    elif vlm_start is not None:
        business_start_source = "vlm"
    elif sample.vlm_business_start_second is not None:
        business_start_source = "cached_vlm"
    elif sample.activate_start_second is not None:
        business_start_source = "activate_fallback"
    else:
        business_start_source = "unknown"
    stalls = _mark_stalls(records, business_start) if policy.stall else set()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    latency_values: list[int] = []
    last_rtt: Optional[int] = None
    last_resolution: Optional[int] = sample.initial_resolution
    last_stall: Optional[int] = None
    last_loading_reason = ""
    source_resolution = dict(sample.source_resolution_series)
    source_stall = dict(sample.source_stall_series)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        selected_records = _select_business_records(
            records, business_start, sample.target_business_duration
        )
        for record in selected_records:
            started = True
            if started:
                if policy.latency:
                    current_rtt = _validated_latency(record.perception.latency_ms, last_rtt)
                    rtt = current_rtt if current_rtt is not None else (last_rtt if last_rtt is not None else UNKNOWN)
                    if rtt >= 0:
                        latency_values.append(rtt)
                        last_rtt = rtt
                else:
                    rtt = NOT_APPLICABLE
                if policy.resolution:
                    prior_resolution = source_resolution.get(record.second)
                    if prior_resolution is not None and prior_resolution <= 0:
                        prior_resolution = None
                    current_resolution = record.perception.resolution or prior_resolution
                    resolution = (
                        current_resolution if current_resolution is not None
                        else (last_resolution if last_resolution is not None else UNKNOWN)
                    )
                    if resolution >= 0:
                        last_resolution = resolution
                else:
                    resolution = NOT_APPLICABLE
                if policy.stall:
                    current_stall = 1 if record.second in stalls else (0 if record.has_frame else None)
                    if use_source_stall and source_stall.get(record.second) == 1:
                        current_stall = 1
                    stall = current_stall if current_stall is not None else (last_stall if last_stall is not None else UNKNOWN)
                    if stall in {0, 1}:
                        last_stall = stall
                    if current_stall == 1:
                        reason = "frozen_frame"
                        last_loading_reason = reason
                    elif current_stall is None and stall == 1:
                        reason = last_loading_reason
                    else:
                        reason = ""
                        if stall == 0:
                            last_loading_reason = ""
                else:
                    stall = NOT_APPLICABLE
                    reason = ""

            row = {
                "file_name": sample.pcap_name,
                "time": _format_time(sample, record.second),
                "rtt": rtt,
                "trust_resolution": resolution,
                "trust_stall": stall,
                "loading_reason": reason,
            }
            writer.writerow(row)
            row_count += 1

    target_duration = sample.target_business_duration
    business_duration_ok = (
        target_duration is None or row_count >= int(target_duration)
    )
    shortfall = (
        max(0, int(target_duration) - row_count)
        if target_duration is not None
        else 0
    )
    warnings: list[str] = []
    if not business_duration_ok:
        warning = (
            f"业务时长不足：实际 {row_count}s，目标 {int(target_duration)}s，"
            f"缺少 {shortfall}s"
        )
        warnings.append(warning)
        print(f"[qoe-postprocess][警告] {warning}", flush=True)
    else:
        expected = int(target_duration) if target_duration is not None else row_count
        print(f"[qoe-postprocess] 业务时长校验通过：{row_count}/{expected}s", flush=True)

    return {
        "output": str(output_path),
        "rows": row_count,
        "business_duration_seconds": row_count,
        "business_duration_ok": business_duration_ok,
        "business_duration_shortfall_seconds": shortfall,
        "warnings": warnings,
        "app_type": sample.app_type,
        "business_start_second": business_start,
        "activate_start_second": sample.activate_start_second,
        "target_business_duration": sample.target_business_duration,
        "latency_samples": len(latency_values),
        "median_latency": int(median(latency_values)) if latency_values else None,
        "video_usable": video_usable,
        "video_reason": validation.reason if validation else ("screenshots_only" if screenshots_only else "validation_skipped"),
        "elapsed_seconds": round(time.perf_counter() - analysis_started, 3),
        "source_stall_fusion": use_source_stall,
        "vlm_enabled": vlm is not None,
        "vlm_skipped_for_runtime_timing": trusted_runtime_timing,
        "vlm_calls": vlm_calls,
        "business_start_source": business_start_source,
        "vlm_observations": vlm_observations,
    }
