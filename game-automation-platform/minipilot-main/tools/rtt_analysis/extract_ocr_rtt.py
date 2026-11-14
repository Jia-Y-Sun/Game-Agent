"""Extract an on-screen ``NN ms`` RTT series from a fixed video ROI.

The video is decoded sequentially with FFmpeg.  Frames are rescaled to the
logical display size, cropped to a fixed latency-label rectangle, and passed
to RapidOCR in recognition-only mode.  Recognition-only mode is fast enough
for sub-second sampling when the game's RTT label stays in one position.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from rapidocr_onnxruntime import RapidOCR


RTT_RE = re.compile(r"(?<!\d)(\d{1,4})\s*m\s*s\b", re.IGNORECASE)


def _size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(item) for item in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("display dimensions must be positive")
    return width, height


def _roi(value: str) -> tuple[int, int, int, int]:
    try:
        x, y, width, height = (int(item) for item in value.split(","))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected X,Y,WIDTH,HEIGHT") from exc
    if min(x, y) < 0 or width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("ROI must be non-negative with positive size")
    return x, y, width, height


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--fps", type=float, default=5.0, help="OCR samples per second")
    parser.add_argument("--display-size", type=_size, required=True)
    parser.add_argument("--roi", type=_roi, required=True)
    parser.add_argument("--upscale", type=int, default=5)
    parser.add_argument("--min-confidence", type=float, default=0.45)
    parser.add_argument("--min-rtt-ms", type=int, default=1)
    parser.add_argument("--max-rtt-ms", type=int, default=9999)
    return parser


def _resolve_executable(value: str) -> str:
    candidate = Path(value)
    if candidate.exists():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError(f"FFmpeg not found: {value}")
    return resolved


def _recognize(engine: RapidOCR, frame: np.ndarray) -> tuple[str, float, int | None]:
    result, _ = engine(frame, use_det=False, use_cls=False)
    if not result:
        return "", 0.0, None
    first = result[0]
    text = str(first[0]).strip()
    confidence = float(first[1])
    normalized = text.replace("毫秒", "ms")
    match = RTT_RE.search(normalized)
    return text, confidence, int(match.group(1)) if match else None


def extract(args: argparse.Namespace) -> dict[str, object]:
    video = args.video.resolve()
    if not video.is_file():
        raise FileNotFoundError(video)
    if args.fps <= 0:
        raise ValueError("--fps must be positive")
    ffmpeg = _resolve_executable(args.ffmpeg)
    display_width, display_height = args.display_size
    x, y, roi_width, roi_height = args.roi
    output_width = roi_width * args.upscale
    output_height = roi_height * args.upscale
    filters = (
        f"fps={args.fps},scale={display_width}:{display_height},"
        f"crop={roi_width}:{roi_height}:{x}:{y},"
        f"scale={output_width}:{output_height}:flags=lanczos"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        filters,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    frame_bytes = output_width * output_height * 3
    engine = RapidOCR()
    rows: list[dict[str, object]] = []
    valid_count = 0
    index = 0
    while True:
        payload = process.stdout.read(frame_bytes)
        if len(payload) < frame_bytes:
            break
        frame = np.frombuffer(payload, dtype=np.uint8).reshape(output_height, output_width, 3)
        text, confidence, value = _recognize(engine, frame)
        valid = (
            value is not None
            and confidence >= args.min_confidence
            and args.min_rtt_ms <= value <= args.max_rtt_ms
        )
        rows.append(
            {
                "time_s": round(index / args.fps, 6),
                "rtt_ms": value if valid else "",
                "confidence": round(confidence, 6),
                "raw_text": text,
                "valid": 1 if valid else 0,
            }
        )
        valid_count += int(valid)
        index += 1
    stderr = (process.stderr.read() if process.stderr else b"").decode("utf-8", errors="replace")
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"FFmpeg failed ({return_code}): {stderr[-2000:]}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("time_s", "rtt_ms", "confidence", "raw_text", "valid"),
        )
        writer.writeheader()
        writer.writerows(rows)
    return {
        "input": str(video),
        "output": str(args.output.resolve()),
        "fps": args.fps,
        "interval_s": 1.0 / args.fps,
        "frames": len(rows),
        "valid_frames": valid_count,
        "coverage": round(valid_count / len(rows), 6) if rows else 0.0,
        "display_size": args.display_size,
        "roi": args.roi,
    }


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(extract(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
