from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from .sample import Sample


@dataclass(frozen=True)
class VideoValidation:
    usable: bool
    reason: str


def validate_video(path: Optional[Path], timeout_seconds: int = 120) -> VideoValidation:
    if not path or not path.exists():
        return VideoValidation(False, "video_missing")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        cap = cv2.VideoCapture(str(path))
        usable = cap.isOpened()
        cap.release()
        return VideoValidation(usable, "ffmpeg_missing" if usable else "video_open_failed")
    command = [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return VideoValidation(False, "decode_validation_timeout")
    error_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    decoder_markers = (
        "error while decoding", "invalid data found", "corrupt", "concealing",
        "missing reference", "reference picture missing", "decode_slice_header",
        "mmco:", "no frame!", "hardware accelerator failed",
    )
    decoder_errors = [
        line for line in error_lines
        if any(marker in line.lower() for marker in decoder_markers)
    ]
    # Timestamp warnings from the null muxer alone do not prove visual damage.
    # Recoverable H.264 decoder errors do: for labeling, such frames are unsafe.
    if result.returncode != 0 or decoder_errors:
        first_error = decoder_errors[0] if decoder_errors else (error_lines[0] if error_lines else "decode_error")
        return VideoValidation(False, first_error[:180])
    return VideoValidation(True, "ok")


class FrameProvider:
    """Return a trustworthy frame for an exact dataset second.

    Screenshot fallback is intentionally strict: an image from another second is
    not carried forward because that would fabricate a second-level label.
    """

    def __init__(
        self,
        sample: Sample,
        video_usable: bool,
        progress_callback: Optional[Callable[[float], None]] = None,
    ):
        self.sample = sample
        self.video_usable = video_usable and sample.video_path is not None
        self._cap = cv2.VideoCapture(str(sample.video_path)) if self.video_usable else None
        self._fps = (self._cap.get(cv2.CAP_PROP_FPS) if self._cap else 0.0) or 30.0
        self._frame_index = -1
        self._reference_size: Optional[tuple[int, int]] = None
        self._screens = {
            int(round((stamp - sample.start_time).total_seconds())): path
            for stamp, path in sample.screenshots
            if 0 <= (stamp - sample.start_time).total_seconds() < sample.duration_seconds
        }
        if sample.screenshots:
            try:
                raw = np.fromfile(str(sample.screenshots[0][1]), dtype=np.uint8)
                reference = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                if reference is not None:
                    self._reference_size = (reference.shape[1], reference.shape[0])
            except (OSError, ValueError):
                pass
        self._frame_temp: Optional[tempfile.TemporaryDirectory] = None
        self._extracted_frames: Optional[Path] = None
        if self.video_usable and sample.video_path and shutil.which("ffmpeg"):
            self._prepare_ffmpeg_frames(sample.video_path, progress_callback)

    def _prepare_ffmpeg_frames(
        self,
        video_path: Path,
        progress_callback: Optional[Callable[[float], None]],
    ) -> None:
        temp = tempfile.TemporaryDirectory(prefix="qoe_frames_")
        output_dir = Path(temp.name)
        filters = ["fps=1"]
        if self._reference_size:
            width, height = self._reference_size
            filters.append(f"scale={width}:{height}")
        command = [
            shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-v", "error",
            "-i", str(video_path), "-vf", ",".join(filters), "-q:v", "4",
            "-progress", "pipe:1", "-nostats",
            str(output_dir / "frame_%06d.jpg"),
        ]
        if progress_callback:
            progress_callback(0.0)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            expected = max(
                0.001,
                self.sample.duration_exact_seconds or float(self.sample.duration_seconds),
            )
            for line in process.stdout:
                key, separator, value = line.strip().partition("=")
                if not separator or not progress_callback:
                    continue
                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        # ffmpeg historically names this field out_time_ms even
                        # though its value is expressed in microseconds.
                        seconds = float(value) / 1_000_000.0
                    except ValueError:
                        continue
                    progress_callback(min(1.0, seconds / expected))
            return_code = process.wait(timeout=180)
            stderr = process.stderr.read() if process.stderr else ""
        except (OSError, subprocess.TimeoutExpired):
            if "process" in locals() and process.poll() is None:
                process.kill()
            temp.cleanup()
            return
        if return_code == 0 and any(output_dir.glob("frame_*.jpg")):
            if progress_callback:
                progress_callback(1.0)
            self._frame_temp = temp
            self._extracted_frames = output_dir
            if self._cap:
                self._cap.release()
                self._cap = None
        else:
            if stderr:
                print(f"[qoe-postprocess][警告] ffmpeg提取帧失败: {stderr.strip()[:180]}")
            temp.cleanup()

    def close(self) -> None:
        if self._cap:
            self._cap.release()
        if self._frame_temp:
            self._frame_temp.cleanup()

    def get(self, second: int) -> tuple[Optional[np.ndarray], str]:
        screenshot = self._screens.get(second)
        if screenshot:
            # cv2.imread on Windows cannot reliably open non-ASCII paths.
            try:
                raw = np.fromfile(str(screenshot), dtype=np.uint8)
                image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            except (OSError, ValueError):
                image = None
            if image is not None:
                return image, "screenshot"
        if self._extracted_frames:
            frame_path = self._extracted_frames / f"frame_{second + 1:06d}.jpg"
            if frame_path.exists():
                image = cv2.imread(str(frame_path), cv2.IMREAD_COLOR)
                if image is not None:
                    return image, "video"
        if self._cap:
            # Calls are monotonic. Decode forward instead of random seeking: many
            # Android screen recordings have sparse/broken keyframe seek points.
            target_index = max(0, int(round(second * self._fps)))
            while self._frame_index < target_index:
                ok = self._cap.grab()
                if not ok:
                    return None, "missing"
                self._frame_index += 1
            ok, image = self._cap.retrieve()
            if ok and image is not None:
                if self._reference_size:
                    ref_width, ref_height = self._reference_size
                    image_is_portrait = image.shape[0] > image.shape[1]
                    reference_is_portrait = ref_height > ref_width
                    if image_is_portrait != reference_is_portrait:
                        # Some Android recorders keep the initial portrait canvas
                        # and squeeze later landscape frames into it. A native
                        # screenshot supplies the correct display aspect ratio.
                        image = cv2.resize(image, (ref_width, ref_height), interpolation=cv2.INTER_LINEAR)
                return image, "video"
        return None, "missing"


def motion_metrics(previous: Optional[np.ndarray], current: np.ndarray) -> tuple[float, float]:
    gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    scale = 320 / max(1, gray.shape[1])
    gray = cv2.resize(gray, (320, max(1, int(gray.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    if previous is None:
        return 999.0, 1.0
    if previous.shape != gray.shape:
        previous = cv2.resize(previous, (gray.shape[1], gray.shape[0]))
    diff = cv2.absdiff(previous, gray)
    return float(np.mean(diff)), float(np.mean(diff > 12))


def motion_gray(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scale = 320 / max(1, gray.shape[1])
    return cv2.resize(gray, (320, max(1, int(gray.shape[0] * scale))), interpolation=cv2.INTER_AREA)
