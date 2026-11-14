import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_paths import resolve_adb


class RuntimePathsTests(unittest.TestCase):
    def test_explicit_and_environment_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "adb.exe"
            executable.touch()
            with patch.dict(os.environ, {"MINI_PILOT_ADB_BIN": str(executable)}):
                self.assertEqual(resolve_adb(), str(executable.resolve()))
                with self.assertRaises(FileNotFoundError):
                    resolve_adb(str(Path(directory) / "missing.exe"))

    def test_no_configured_adb_fails_without_silent_device_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"MINI_PILOT_ADB_BIN": ""}), patch("runtime_paths.shutil.which", return_value=None):
                with self.assertRaises(FileNotFoundError):
                    resolve_adb(bundled_dir=directory)
