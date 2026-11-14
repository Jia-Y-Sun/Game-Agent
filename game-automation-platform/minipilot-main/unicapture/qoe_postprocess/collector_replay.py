"""Replay the online QoE monitor against an MP4 at one-second intervals."""

from __future__ import annotations

import csv
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import cv2

from ..qoe_monitor import QoEMonitor
from .media import FrameProvider
from .sample import Sample
from .schema import CSV_FIELDS


def _format_time(sample: Sample, second: int) -> str:
    value = sample.start_time + timedelta(seconds=second)
    centiseconds = value.microsecond // 10_000
    return f"{value.strftime('%Y-%m-%d %H:%M:%S')}.{centiseconds:02d}{value.strftime('%z')}"


def _business_start(sample: Sample) -> tuple[int, str]:
    if sample.timing_business_start_second is not None:
        return sample.timing_business_start_second, sample.timing_business_start_source or "runtime_monitor"
    if sample.vlm_business_start_second is not None:
        return sample.vlm_business_start_second, "cached_vlm"
    if sample.activate_start_second is not None:
        return sample.activate_start_second, "activate_fallback"
    return 0, "video_start_fallback"


def replay_collector_qoe(
    sample: Sample,
    output_path: Path,
    *,
    resolution_every: int = 1,
) -> dict[str, Any]:
    """Run the same classifiers and state windows used during live capture."""
    started = time.perf_counter()
    business_start, business_source = _business_start(sample)
    business_start = max(0, min(business_start, sample.duration_seconds))
    target = sample.target_business_duration
    business_end = min(
        sample.duration_seconds,
        business_start + target if target is not None else sample.duration_seconds,
    )

    print("[collector-replay] 阶段 1/2：按1秒时间轴提取视频帧", flush=True)
    frame_started = time.perf_counter()

    def frame_progress(ratio: float) -> None:
        current = int(round(ratio * sample.duration_seconds))
        if current in {0, sample.duration_seconds} or current % 10 == 0:
            print(
                f"[collector-replay] 抽帧 {ratio * 100:6.2f}% "
                f"{current}/{sample.duration_seconds}s "
                f"已用 {time.perf_counter() - frame_started:.1f}s",
                flush=True,
            )

    provider = FrameProvider(
        sample,
        video_usable=sample.video_path is not None,
        progress_callback=frame_progress,
    )
    print("[collector-replay] 阶段 2/2：复放采集期QoE检测器", flush=True)
    monitor = QoEMonitor(
        interval=1.0,
        output_dir=str(sample.root / "repair" / "collector_replay_runtime"),
        pcap_name=sample.pcap_name,
        app_type=sample.app_type,
        app_name=sample.app_name,
        scene=sample.scene,
        warmup_seconds=0,
        register_signal=False,
    )
    monitor.business_started = False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    missing_frames = 0
    resolution_every = max(1, int(resolution_every))

    try:
        with tempfile.TemporaryDirectory(prefix="collector_replay_") as directory:
            frame_path = Path(directory) / "current.jpg"
            previous_path = Path(directory) / "previous.jpg"
            with output_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
                writer.writeheader()
                for second in range(sample.duration_seconds):
                    if second % 10 == 0 or second == sample.duration_seconds - 1:
                        elapsed = time.perf_counter() - started
                        ratio = (second + 1) / max(1, sample.duration_seconds)
                        eta = elapsed / ratio - elapsed
                        print(
                            f"[collector-replay] 分析 {ratio * 100:6.2f}% "
                            f"{second + 1}/{sample.duration_seconds}s "
                            f"已用 {elapsed:.1f}s 预计剩余 {eta:.1f}s",
                            flush=True,
                        )
                    image, _source = provider.get(second)
                    if image is None:
                        missing_frames += 1
                        if business_start <= second < business_end:
                            writer.writerow({
                                "file_name": sample.pcap_name,
                                "time": _format_time(sample, second),
                                "rtt": -4,
                                "trust_resolution": -128,
                                "trust_stall": -128,
                                "loading_reason": "missing_frame",
                            })
                            row_count += 1
                        continue

                    if second == business_start:
                        monitor.business_started = True
                        monitor.similarity_history.clear()
                        monitor.pixel_diff_history.clear()
                        monitor.resolution_history.clear()

                    if not cv2.imwrite(str(frame_path), image):
                        raise RuntimeError(f"无法写入临时视频帧: {second}s")
                    similarity = 0.0
                    pixel_diff = 0.0
                    if previous_path.exists():
                        similarity, pixel_diff = monitor.image_analyzer.calculate_similarity(
                            str(previous_path), str(frame_path)
                        )
                    page_type = "unknown"
                    if sample.app_name == "douyin":
                        try:
                            page_type, _details = monitor.page_classifier.classify(str(frame_path))
                        except Exception:
                            page_type = "unknown"
                    if second % resolution_every == 0:
                        resolution = monitor.detect_resolution(
                            str(frame_path), (image.shape[1], image.shape[0]), page_type=page_type
                        )
                    else:
                        valid = [value for value in monitor.resolution_history if value > 0]
                        resolution = valid[-1] if valid else (-4 if monitor.app_type != "video" else -128)
                    stall, reason = monitor.detect_stall(
                        similarity, pixel_diff, str(frame_path), page_type=page_type
                    )
                    if business_start <= second < business_end:
                        writer.writerow({
                            "file_name": sample.pcap_name,
                            "time": _format_time(sample, second),
                            "rtt": -4,
                            "trust_resolution": resolution,
                            "trust_stall": stall,
                            "loading_reason": reason if stall == 1 else "",
                        })
                        row_count += 1
                    frame_path.replace(previous_path)
    finally:
        provider.close()

    target_ok = target is None or row_count >= target
    warnings: list[str] = []
    if not target_ok:
        warnings.append(f"业务时长不足：实际 {row_count}s，目标 {target}s")
    if missing_frames:
        warnings.append(f"缺失视频帧：{missing_frames}s")
    return {
        "output": str(output_path),
        "rows": row_count,
        "business_duration_seconds": row_count,
        "business_duration_ok": target_ok,
        "business_duration_shortfall_seconds": max(0, target - row_count) if target is not None else 0,
        "warnings": warnings,
        "app_type": sample.app_type,
        "business_start_second": business_start,
        "activate_start_second": sample.activate_start_second,
        "target_business_duration": target,
        "business_start_source": business_source,
        "video_usable": missing_frames < sample.duration_seconds,
        "video_reason": "ok" if not missing_frames else "missing_frames",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "detector": "qoe_monitor_collector_replay",
        "resolution_every_seconds": resolution_every,
    }
