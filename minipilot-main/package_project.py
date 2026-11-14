#!/usr/bin/env python3
"""Create a clean, reproducible MiniPilot source distribution ZIP."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "dist"
ARCHIVE_ROOT = "minipilot"

REQUIRED_FILES = (
    "README.md",
    "requirements.txt",
    "mini_pilot.config.example.json",
    "mini_pilot/main.py",
    "unicapture/app_collector.py",
    "安卓端侧APP样本采集方案.md",
    "极简操作手册.md",
)

EXCLUDED_DIRECTORY_NAMES = {
    ".agents",
    ".codex",
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "venv",
    "env",
    "runs",
    "backups",
    "memories",
    "dist",
    "build",
    "qoe_data",
}

EXCLUDED_EXACT_FILES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}

EXCLUDED_INTERNAL_DOCUMENTS = {
    "mini_pilot.runtime_preferences.md",
    "unicapture/README.md",
    "unicapture/SAMPLE_FORMAT.md",
    "unicapture/batch_config_sample.yaml",
    "unicapture/postprocess/README_BUSINESS_TIME.md",
    "unicapture/qoe_postprocess/README.md",
    "unicapture/样本示例说明.txt",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
    ".pcap",
    ".pcapng",
    ".mp4",
    ".wmv",
    ".tmp",
    ".swp",
    ".swo",
    ".pem",
    ".key",
}


def parse_args() -> argparse.Namespace:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(
        description=(
            "Package MiniPilot without Git metadata, virtual environments, "
            "runtime data, caches or local secrets."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / f"minipilot-{timestamp}.zip",
        help="Output ZIP path (default: dist/minipilot-<timestamp>.zip).",
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Include the tests directory in the distribution.",
    )
    parser.add_argument(
        "--include-samples",
        action="store_true",
        help="Include unicapture/数据样本样例 (large media may be included).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output ZIP.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected files without creating a ZIP.",
    )
    return parser.parse_args()


def is_virtualenv_directory(name: str) -> bool:
    lowered = name.lower()
    return lowered == ".venv" or lowered.startswith(".venv")


def is_local_config(path: Path) -> bool:
    name = path.name.lower()
    if name == "mini_pilot.config.example.json":
        return False
    return name.startswith("mini_pilot") and name.endswith(".config.json")


def is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or is_local_config(path)
    )


def is_backup_file(path: Path) -> bool:
    lowered = path.name.lower()
    return (
        lowered.endswith(".bak")
        or ".bak." in lowered
        or lowered.endswith(".backup")
        or lowered.endswith("~")
        or "副本" in path.name
    )


def should_exclude(path: Path, *, include_tests: bool, include_samples: bool) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    parts = relative.parts
    relative_text = relative.as_posix()
    in_sample_directory = parts[:2] == ("unicapture", "数据样本样例")

    for part in parts[:-1]:
        if part in EXCLUDED_DIRECTORY_NAMES or is_virtualenv_directory(part):
            return True
    if not include_tests and parts and parts[0] == "tests":
        return True
    if not include_samples and in_sample_directory:
        return True
    if any(part in {"data", "test_data"} for part in parts[:-1]):
        return True

    if path.name in EXCLUDED_EXACT_FILES:
        return True
    if relative_text in EXCLUDED_INTERNAL_DOCUMENTS:
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES and not (
        include_samples and in_sample_directory
    ):
        return True
    if path.suffix.lower() == ".zip":
        return True
    if is_secret_file(path) or is_backup_file(path):
        return True
    return False


def collect_files(*, include_tests: bool, include_samples: bool) -> list[Path]:
    selected: list[Path] = []
    for directory, directory_names, file_names in os.walk(PROJECT_ROOT, followlinks=False):
        current = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not should_exclude(
                current / name / ".directory_probe",
                include_tests=include_tests,
                include_samples=include_samples,
            )
        )
        for file_name in sorted(file_names):
            path = current / file_name
            if path.is_symlink():
                print(f"[skip symlink] {path.relative_to(PROJECT_ROOT)}", file=sys.stderr)
                continue
            if should_exclude(
                path,
                include_tests=include_tests,
                include_samples=include_samples,
            ):
                continue
            selected.append(path)
    return sorted(selected, key=lambda item: item.relative_to(PROJECT_ROOT).as_posix())


def validate_project(files: list[Path]) -> None:
    selected = {path.relative_to(PROJECT_ROOT).as_posix() for path in files}
    missing = [name for name in REQUIRED_FILES if name not in selected]
    if missing:
        raise RuntimeError("Required package files are missing: " + ", ".join(missing))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(files: list[Path]) -> str:
    lines = [
        "MiniPilot package contents",
        f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Files: {len(files)}",
        "",
        "SHA256                                                            Path",
    ]
    for path in files:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        lines.append(f"{sha256(path)}  {relative}")
    return "\n".join(lines) + "\n"


def write_zip(files: list[Path], output: Path, *, force: bool) -> None:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise FileExistsError(f"Output already exists: {output}; pass --force to replace it.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()

    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in files:
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                archive.write(path, PurePosixPath(ARCHIVE_ROOT, relative).as_posix())
            archive.writestr(
                PurePosixPath(ARCHIVE_ROOT, "PACKAGE_CONTENTS.txt").as_posix(),
                build_manifest(files),
            )
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    print(f"Created: {output}")
    print(f"Files: {len(files)}")
    print(f"Size: {output.stat().st_size / (1024 * 1024):.2f} MiB")


def main() -> int:
    args = parse_args()
    try:
        files = collect_files(
            include_tests=args.include_tests,
            include_samples=args.include_samples,
        )
        validate_project(files)
        if args.dry_run:
            for path in files:
                print(path.relative_to(PROJECT_ROOT).as_posix())
            print(f"Files: {len(files)}")
            return 0
        write_zip(files, args.output, force=args.force)
        return 0
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"Packaging failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
