from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mini_pilot import main as minipilot_main


UNICAPTURE_DIR = Path(__file__).resolve().parents[1] / "unicapture"
if str(UNICAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(UNICAPTURE_DIR))

from app_collector import main as collector_main  # noqa: E402


class CaptureCatalogCliTests(unittest.TestCase):
    def test_minipilot_app_list_includes_scene_names(self) -> None:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["mini-pilot-v5", "--list-apps"]),
            redirect_stdout(output),
        ):
            code = minipilot_main.main()

        self.assertEqual(code, 0)
        self.assertIn("douyin: feed, short_video, live", output.getvalue())
        self.assertIn("Use --list-scenes APP", output.getvalue())

    def test_minipilot_lists_scene_details_for_app(self) -> None:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["mini-pilot-v5", "--list-scenes", "douyin"]),
            redirect_stdout(output),
        ):
            code = minipilot_main.main()

        self.assertEqual(code, 0)
        self.assertIn("douyin capture scenes:", output.getvalue())
        self.assertIn("- feed:", output.getvalue())
        self.assertIn("default 300s", output.getvalue())

    def test_minipilot_reports_unknown_scene_app(self) -> None:
        error = io.StringIO()
        with (
            patch.object(sys, "argv", ["mini-pilot-v5", "--list-scenes", "missing"]),
            redirect_stderr(error),
        ):
            code = minipilot_main.main()

        self.assertEqual(code, 2)
        self.assertIn("Use --list-apps", error.getvalue())

    def test_unicapture_lists_scenes_without_scene_argument(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["app_collector.py", "--app-name", "douyin", "--list-scenes"],
            ),
            redirect_stdout(output),
        ):
            code = collector_main()

        self.assertEqual(code, 0)
        self.assertIn("'douyin' 支持的采集场景", output.getvalue())
        self.assertIn("- live:", output.getvalue())

    def test_unicapture_app_list_includes_scene_names(self) -> None:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["app_collector.py", "--list-apps"]),
            redirect_stdout(output),
        ):
            code = collector_main()

        self.assertEqual(code, 0)
        self.assertIn("- douyin: feed, short_video, live", output.getvalue())
        self.assertIn("--app-name APP --list-scenes", output.getvalue())


if __name__ == "__main__":
    unittest.main()
