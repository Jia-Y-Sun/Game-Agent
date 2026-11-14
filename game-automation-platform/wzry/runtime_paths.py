import os
import shutil
from pathlib import Path


def resolve_adb(explicit=None, bundled_dir=None):
    configured = explicit or os.environ.get("MINI_PILOT_ADB_BIN")
    if configured:
        found = shutil.which(str(configured))
        if found:
            return str(Path(found).resolve())
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise FileNotFoundError("Configured ADB executable does not exist: " + str(configured))
    found = shutil.which("adb")
    if found:
        return str(Path(found).resolve())
    directory = Path(bundled_dir or Path(__file__).parent / "scrcpy-win64-v2.0")
    for name in ("adb.exe", "adb"):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError("Set MINI_PILOT_ADB_BIN or add adb to PATH.")
