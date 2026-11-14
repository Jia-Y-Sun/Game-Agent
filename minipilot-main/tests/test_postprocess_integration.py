from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mini_pilot.capture import UnicaptureConfig, UnicaptureSession
from mini_pilot.business_timing import BusinessTimingController
from mini_pilot.main import (
    _resolve_duration_budget,
    _resolve_operation_mode,
    _run_manual_operation,
)
from unicapture.postprocess_runner import (
    _write_business_record,
    run_sample_postprocess,
)


class PostprocessIntegrationTests(unittest.TestCase):
    def test_runtime_timing_uses_activate_when_vlm_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "business_timing.json"
            controller = BusinessTimingController(
                target_duration_seconds=1,
                startup_timeout_seconds=2,
                activation_fallback_delay_seconds=30,
                marker_path=marker,
                activation_probe=lambda: "logcat_activate",
                vlm_probe=None,
                vlm_interval_seconds=0.1,
            )
            controller.start()
            self.assertTrue(controller.wait(2))
            result = controller.result
            controller.cancel()
            self.assertIsNotNone(result)
            self.assertEqual(result.source, "logcat_activate_fallback")
            saved = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(saved["target_duration_seconds"], 1)

    def test_manual_mode_overrides_configured_agent(self) -> None:
        args = SimpleNamespace(manual=True, operation_mode=None)
        self.assertEqual(
            _resolve_operation_mode(args, {"operation_mode": "agent"}),
            "manual",
        )

    def test_manual_operation_uses_timer_without_agent(self) -> None:
        args = SimpleNamespace(run_duration=1, capture_duration=None)
        session = SimpleNamespace(
            config=SimpleNamespace(duration=120),
            target_app_package="tv.danmaku.bili",
        )
        with (
            patch("mini_pilot.main.time.monotonic", side_effect=[0.0, 0.0, 1.1]),
            patch("mini_pilot.main.time.sleep"),
        ):
            success, message = _run_manual_operation(args, session)
        self.assertTrue(success)
        self.assertIn("1s", message)

    def test_capture_postprocess_is_enabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UnicaptureSession(UnicaptureConfig(
                app_name="fixture",
                scene="video",
                output_dir=directory,
            ))
            command = session._build_command()
        self.assertNotIn("--disable-postprocess", command)
        self.assertNotIn("--disable-flow-labeling", command)
        self.assertNotIn("--postprocess-disable-vlm", command)
        self.assertIn("--postprocess-vlm-every", command)
        self.assertIn("--postprocess-ocr-every", command)

    def test_capture_command_separates_ceiling_from_business_duration(self) -> None:
        session = UnicaptureSession(
            UnicaptureConfig(
                app_name="bilibili",
                scene="video",
                duration=400,
                target_business_duration=300,
            )
        )
        command = session._build_command()
        self.assertEqual(command[command.index("--duration") + 1], "400")
        self.assertEqual(
            command[command.index("--target-business-duration") + 1], "300"
        )

    def test_configured_business_duration_reaches_agent_budget(self) -> None:
        args = SimpleNamespace(run_duration=None)
        session = SimpleNamespace(
            config=SimpleNamespace(target_business_duration=300)
        )
        self.assertEqual(_resolve_duration_budget(args, session), 300)

    def test_cli_duration_overrides_configured_agent_budget(self) -> None:
        args = SimpleNamespace(run_duration=120)
        session = SimpleNamespace(
            config=SimpleNamespace(target_business_duration=300)
        )
        self.assertEqual(_resolve_duration_budget(args, session), 120)

    def test_capture_postprocess_can_be_explicitly_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UnicaptureSession(UnicaptureConfig(
                app_name="fixture",
                scene="video",
                output_dir=directory,
                enable_postprocess=False,
                enable_flow_labeling=False,
                postprocess_enable_vlm=False,
            ))
            command = session._build_command()
        self.assertIn("--disable-postprocess", command)
        self.assertIn("--disable-flow-labeling", command)
        self.assertIn("--postprocess-disable-vlm", command)

    def test_runner_writes_summary_and_isolates_qoe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("unicapture.postprocess_runner.annotate_business_times", return_value=0),
                patch(
                    "unicapture.postprocess_runner.load_sample",
                    return_value=SimpleNamespace(root=root, name="fixture"),
                ),
                patch(
                    "unicapture.postprocess_runner.process_sample",
                    side_effect=RuntimeError("fixture failure"),
                ),
            ):
                result = run_sample_postprocess(root, enable_vlm=False)
            saved = json.loads(
                (root / "postprocess" / "summary.json").read_text(encoding="utf-8")
            )
        self.assertEqual(result["status"], "partial_error")
        self.assertEqual(result["activate_time"]["status"], "ok")
        self.assertEqual(result["qoe"]["status"], "error")
        self.assertEqual(saved["status"], "partial_error")
        self.assertNotIn("QOE_USE_VLM", os.environ)

    def test_runner_overwrites_qoe_and_removes_legacy_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "fixture_qoe.csv"
            original.write_text("old\n", encoding="utf-8")
            legacy_v2 = root / "fixture_qoe_v2.csv"
            legacy_v2.write_text("legacy\n", encoding="utf-8")
            sample = SimpleNamespace(root=root, name="fixture")

            def write_qoe(_sample, output, **_kwargs):
                output.write_text(
                    "file_name,time,rtt,trust_resolution,trust_stall,"
                    "loading_reason\n",
                    encoding="utf-8",
                )
                return {"business_start_second": None, "rows": 0}

            with (
                patch("unicapture.postprocess_runner.annotate_business_times", return_value=0),
                patch("unicapture.postprocess_runner.load_sample", return_value=sample),
                patch("unicapture.postprocess_runner.process_sample", side_effect=write_qoe),
            ):
                result = run_sample_postprocess(root, enable_vlm=False)

            self.assertEqual(result["qoe"]["status"], "ok")
            self.assertIn("loading_reason", original.read_text(encoding="utf-8"))
            self.assertEqual(
                (root / "postprocess" / "qoe_backups" / original.name).read_text(
                    encoding="utf-8"
                ),
                "old\n",
            )
            self.assertFalse(legacy_v2.exists())

    def test_vlm_business_record_is_separate_from_activate_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            yaml_path = Path(directory) / "fixture.yaml"
            yaml_path.write_text(
                "activate_record:\n"
                "  start_time: 20260101T10000000+0000\n"
                "  end_time: 20260101T10020000+0000\n"
                "  source: logcat\n",
                encoding="utf-8",
            )
            sample = SimpleNamespace(
                yaml_path=yaml_path,
                start_time=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
                duration_seconds=120,
                target_business_duration=60,
            )
            _write_business_record(sample, 15, "vlm")
            updated = yaml_path.read_text(encoding="utf-8")
            self.assertIn("activate_record:", updated)
            self.assertIn("business_record:", updated)
            self.assertIn("start_event: vlm_business_confirmed", updated)
            self.assertIn("end_time: 20260101T10011500+0000", updated)
            self.assertIn("end_event: target_business_duration_end", updated)
            self.assertIn("source: vlm", updated)

    def test_short_business_record_does_not_claim_target_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            yaml_path = Path(directory) / "fixture.yaml"
            yaml_path.write_text("name: fixture\n", encoding="utf-8")
            sample = SimpleNamespace(
                yaml_path=yaml_path,
                start_time=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
                duration_seconds=50,
                target_business_duration=300,
            )
            _write_business_record(
                sample,
                10,
                "vlm",
                actual_duration_seconds=40,
            )
            updated = yaml_path.read_text(encoding="utf-8")
            self.assertIn("end_time: 20260101T10005000+0000", updated)
            self.assertIn("end_event: available_business_data_end", updated)


if __name__ == "__main__":
    unittest.main()
