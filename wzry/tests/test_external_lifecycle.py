import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecycle import ExternalLifecycle, RunAbort, EXIT_STOPPED, EXIT_TIMEOUT


class ExternalLifecycleTests(unittest.TestCase):
    def test_event_order_identifier_and_finish_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "events.jsonl"
            lifecycle = ExternalLifecycle(enabled=True, event_file=str(target), run_id="fixture")
            lifecycle.emit_started(device_id="emulator-5554", mode="external")
            lifecycle.emit_match_ready()
            lifecycle.emit_match_ready()
            lifecycle.emit_match_finished(event="practice")
            lifecycle.emit_match_finished(event="practice")
            lifecycle.emit_stopped(reason="match_finished")
            events = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["event"] for row in events], ["SCRIPT_STARTED", "MATCH_READY", "MATCH_FINISHED", "SCRIPT_STOPPED"])
            self.assertEqual([row["seq"] for row in events], [1, 2, 3, 4])
            self.assertTrue(all(row["run_id"] == "fixture" for row in events))
            self.assertEqual(events[2]["result"], "practice")

    def test_stop_file_and_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            stop_file = Path(directory) / "stop"
            for stop, timeout, ready_timeout, reason, code in (
                (True, 30, 30, "stop_file", EXIT_STOPPED),
                (False, 5, 30, "timeout", EXIT_TIMEOUT),
                (False, 30, 5, "match_ready_timeout", EXIT_TIMEOUT),
            ):
                with self.subTest(reason=reason), patch("lifecycle.time.monotonic", return_value=100):
                    lifecycle = ExternalLifecycle(enabled=True, stop_file=str(stop_file), timeout=timeout, match_ready_timeout=ready_timeout)
                    if stop:
                        stop_file.touch()
                    elif stop_file.exists():
                        stop_file.unlink()
                    with patch("lifecycle.time.monotonic", return_value=110), self.assertRaises(RunAbort) as error:
                        lifecycle.check()
                    self.assertEqual((error.exception.reason, error.exception.code), (reason, code))
