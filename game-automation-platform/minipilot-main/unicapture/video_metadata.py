"""Utilities for reading authoritative timing from recorded video files."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def probe_video_duration(path: str | Path | None, timeout_seconds: int = 20) -> Optional[float]:
    """Return the container duration in seconds, or ``None`` when unavailable."""
    if not path:
        return None
    video_path = Path(path)
    if not video_path.is_file():
        return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float(result.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None
    return duration if duration > 0 else None
