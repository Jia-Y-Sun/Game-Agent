from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integration.game_events import GameEvent
from integration.game_probe_orchestrator import (
    _build_capture_args,
    _business_marker,
    _write_marker,
    parse_args,
    run,
)


class GameProbeOrchestratorTests(unittest.TestCase):
    def test_capture_args_include_optional_app_fields(self) -> None:
        args = parse_args(["--game-driver", "driver.py"])
        capture_args = _build_capture_args(args)
        self.assertIsNone(capture_args.custom_app_type)
        self.assertIsNone(capture_args.custom_app_scene)
        self.assertEqual(capture_args.capture_app, "jcc")
        self.assertGreater(capture_args.capture_duration, args.game_timeout)
        self.assertEqual(capture_args.capture_postprocess_ocr_every, 10)

    def test_business_marker_uses_match_events_for_duration(self) -> None:
        start = GameEvent(
            event="MATCH_READY",
            seq=4,
            ts="2026-09-29T21:00:00+08:00",
            run_id="run-1",
            schema_version=1,
            payload={},
        )
        end = GameEvent(
            event="MATCH_FINISHED",
            seq=9,
            ts="2026-09-29T21:03:20+08:00",
            run_id="run-1",
            schema_version=1,
            payload={"result": "unknown"},
        )
        marker = _business_marker(start, end, run_id="run-1", result="unknown")
        self.assertEqual(marker["source"], "game_script_match_ready")
        self.assertEqual(marker["match_ready_seq"], 4)
        self.assertEqual(marker["match_finished_seq"], 9)
        self.assertEqual(marker["target_duration_seconds"], 200)
        self.assertEqual(marker["match_result"], "unknown")

    def test_write_marker_is_complete_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "business_timing.json"
            _write_marker(path, {"source": "game_script_match_ready"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "game_script_match_ready")
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_orchestrator_run_uses_events_to_finish_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver_script = root / "driver.py"
            driver_script.write_text("# fake driver\n", encoding="utf-8")

            class FakeCapture:
                def __init__(self) -> None:
                    self.stopped = False

                def stop(self) -> None:
                    self.stopped = True

            class FakeDriver:
                def __init__(self, config) -> None:
                    self.config = config
                    self.returncode = 0
                    self._events_written = False

                @property
                def is_running(self) -> bool:
                    return False

                def start(self) -> None:
                    records = [
                        {"seq": 1, "event": "SCRIPT_STARTED", "ts": "2026-09-29T21:00:00+08:00"},
                        {"seq": 2, "event": "DEVICE_READY", "ts": "2026-09-29T21:00:01+08:00"},
                        {"seq": 3, "event": "MATCH_READY", "ts": "2026-09-29T21:00:02+08:00"},
                        {
                            "seq": 4,
                            "event": "MATCH_FINISHED",
                            "ts": "2026-09-29T21:01:02+08:00",
                            "result": "unknown",
                        },
                    ]
                    with self.config.event_file.open("a", encoding="utf-8") as handle:
                        for record in records:
                            handle.write(json.dumps(record) + "\n")

                def request_stop(self) -> None:
                    pass

                def wait(self, timeout=None) -> int:
                    return 0

                def terminate(self, grace_seconds: float = 5.0) -> int:
                    return 0

            capture = FakeCapture()
            args = parse_args(
                [
                    "--game-driver",
                    str(driver_script),
                    "--runs-dir",
                    str(root / "runs"),
                    "--run-id",
                    "orchestrator-test",
                    "--skip-launch-game",
                    "--game-timeout",
                    "30",
                ]
            )
            with (
                patch(
                    "integration.game_probe_orchestrator._start_capture",
                    return_value=capture,
                ),
                patch("integration.game_probe_orchestrator._launch_game"),
                patch(
                    "integration.game_probe_orchestrator.GameDriverProcess",
                    FakeDriver,
                ),
            ):
                result = run(args)

            self.assertEqual(result, 0)
            self.assertTrue(capture.stopped)
            manifest = json.loads(
                (
                    root
                    / "runs"
                    / "orchestrator-test-jcc-external"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "ok")
            self.assertEqual(manifest["postprocess_status"], "summary_not_found")
            marker = json.loads(
                (
                    root
                    / "runs"
                    / "orchestrator-test-jcc-external"
                    / "capture"
                    / "business_timing.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(marker["target_duration_seconds"], 60)
            self.assertEqual(marker["match_result"], "unknown")
            self.assertEqual(marker["match_finish_source"], "MATCH_FINISHED")
            self.assertEqual(marker["match_result_source"], "unknown")


if __name__ == "__main__":
    unittest.main()
