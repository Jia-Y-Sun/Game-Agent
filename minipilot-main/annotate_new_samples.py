"""Batch/retry entry point for MiniPilot logcat application-time annotation."""

from __future__ import annotations

import sys
from pathlib import Path

from unicapture.postprocess.annotate_business_times import main as annotate_main


def main(argv: list[str] | None = None) -> int:
    profile = (
        Path(__file__).parent
        / "unicapture"
        / "postprocess"
        / "business_time_annotation_profile.json"
    )
    defaults = [
        "--root", "runs",
        "--min-confidence", "low",
        "--only-missing-activate",
        "--require-mp4",
        "--fill-missing-from-video-record",
        "--profile-file", str(profile),
    ]
    return annotate_main(defaults + (list(argv) if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
