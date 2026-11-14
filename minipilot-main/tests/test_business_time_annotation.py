from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from unicapture.postprocess import annotate_business_times as business_time


YAML_TEXT = """version: 2.0.0
time_zone: +0800
name: fixture
task_info:
  start_time: 20260101T10000000+0800
  end_time: 20260101T10050000+0800
app_info:
  app_type: video
  app_name: fixture_app
  app_package: com.example.fixture
video_record:
  start_time: 20260101T10000000+0800
  end_time: 20260101T10050000+0800
  duration: 300
  record_format: mp4
"""


class BusinessTimeTests(unittest.TestCase):
    def test_timestamp_round_trip(self) -> None:
        value = "20260707T00350866+0800"
        self.assertEqual(
            business_time.format_yaml_time(business_time.parse_yaml_time(value)),
            value,
        )

    def test_profile_aware_event_classification(self) -> None:
        displayed = (
            "01-01 10:00:01.000  100  200 I ActivityTaskManager: "
            "Displayed com.example.fixture/.MainActivity"
        )
        task_back = (
            "01-01 10:05:00.000  100  200 I ActivityTaskManager: "
            "moveTaskToBack com.example.fixture"
        )
        capture = (
            "01-01 10:05:01.000  100  200 I ActivityTaskManager: "
            "START u0 {cmp=com.capture.custom/.ControlActivity}"
        )
        self.assertEqual(
            business_time.classify_event(displayed, "com.example.fixture"),
            "activity_displayed",
        )
        self.assertEqual(
            business_time.classify_event(task_back, "com.example.fixture"),
            "task_to_back",
        )
        self.assertEqual(
            business_time.classify_event(
                capture,
                "com.example.fixture",
                "com.capture.custom",
                "ControlActivity",
            ),
            "capture_control_start",
        )

    def test_full_video_fallback_write_preserves_video_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "App" / "run" / "capture" / "sample"
            sample.mkdir(parents=True)
            yaml_path = sample / "fixture.yaml"
            yaml_path.write_text(YAML_TEXT, encoding="utf-8")
            (sample / "fixture.mp4").write_bytes(b"fixture")
            (sample / "fixture.log").write_text(
                "# logcat stream started at 2026-01-01T10:00:00+08:00\n"
                "01-01 10:05:00.000  100  200 I Fixture: no lifecycle event\n"
                "# logcat stream stopped at 2026-01-01T10:05:00+08:00\n",
                encoding="utf-8",
            )
            original = yaml_path.read_text(encoding="utf-8")
            report = root / "business_time_reports" / "test"
            backup = root / "business_time_backups" / "test"
            code = business_time.main([
                "--root", str(root),
                "--write",
                "--min-confidence", "low",
                "--fill-missing-from-video-record",
                "--require-mp4",
                "--report-dir", str(report),
                "--backup-dir", str(backup),
            ])
            self.assertEqual(code, 0)
            updated = yaml_path.read_text(encoding="utf-8")
            self.assertEqual(
                business_time.section_block(original, "video_record"),
                business_time.section_block(updated, "video_record"),
            )
            self.assertIn("activate_record:", updated)
            self.assertNotIn("business_record:", updated)
            self.assertIn("source: video_record_fallback", updated)
            self.assertIn("confidence: low", updated)
            self.assertTrue((backup / yaml_path.relative_to(root)).is_file())
            with (report / "activate_time_exceptions.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "written_needs_review")
            self.assertEqual(rows[0]["boundary_rule"], "video_record_full_fallback")

    def test_only_missing_activate_skips_before_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "App" / "run" / "capture" / "sample"
            sample.mkdir(parents=True)
            yaml_path = sample / "fixture.yaml"
            yaml_path.write_text(
                YAML_TEXT
                + "activate_record:\n"
                + "  start_time: 20260101T10000000+0800\n"
                + "  end_time: 20260101T10050000+0800\n"
                + "  start_event: app_launch\n"
                + "  end_event: app_exit_foreground\n"
                + "  source: logcat\n"
                + "  confidence: high\n",
                encoding="utf-8",
            )
            report = root / "business_time_reports" / "skip-test"
            code = business_time.main([
                "--root", str(root),
                "--only-missing-activate",
                "--require-mp4",
                "--report-dir", str(report),
            ])
            self.assertEqual(code, 0)
            with (report / "activate_time_candidates.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["status"], "skipped_existing_activate_record")

    def test_non_sample_yaml_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "batch_config.yaml").write_text(
                "devices:\n  - id: fixture\n", encoding="utf-8"
            )
            report = root / "business_time_reports" / "ignore-test"
            code = business_time.main([
                "--root", str(root),
                "--report-dir", str(report),
            ])
            self.assertEqual(code, 0)
            with (report / "activate_time_candidates.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["status"], "ignored_non_sample_yaml")


if __name__ == "__main__":
    unittest.main()



