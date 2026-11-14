from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from integration.game_events import (
    EventFileTailer,
    EventProtocolError,
    EventWaitTimeout,
    parse_event_line,
)


class GameEventTests(unittest.TestCase):
    def test_parse_valid_event(self) -> None:
        event = parse_event_line(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-1",
                    "seq": 2,
                    "ts": "2026-09-29T21:00:00+08:00",
                    "event": "MATCH_READY",
                }
            )
        )
        self.assertEqual(event.event, "MATCH_READY")
        self.assertEqual(event.seq, 2)
        self.assertEqual(event.run_id, "run-1")
        self.assertEqual(event.schema_version, 1)

    def test_parse_rejects_missing_event(self) -> None:
        with self.assertRaises(EventProtocolError):
            parse_event_line(json.dumps({"seq": 1}))

    def test_tailer_handles_partial_and_appended_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            tailer = EventFileTailer(path)
            self.assertEqual(tailer.poll(), [])

            first = {
                "schema_version": 1,
                "seq": 1,
                "ts": "2026-09-29T21:00:00+08:00",
                "event": "SCRIPT_STARTED",
            }
            encoded = json.dumps(first).encode("utf-8")
            path.write_bytes(encoded[:10])
            self.assertEqual(tailer.poll(), [])

            with path.open("ab") as handle:
                handle.write(encoded[10:] + b"\n")

            events = tailer.poll()
            self.assertEqual([event.event for event in events], ["SCRIPT_STARTED"])

            second = {
                "schema_version": 1,
                "seq": 2,
                "ts": "2026-09-29T21:00:01+08:00",
                "event": "MATCH_READY",
            }
            with path.open("ab") as handle:
                handle.write(json.dumps(second).encode("utf-8") + b"\n")
            self.assertEqual([event.event for event in tailer.poll()], ["MATCH_READY"])

    def test_tailer_rejects_non_increasing_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"seq": 1, "event": "SCRIPT_STARTED"}),
                        json.dumps({"seq": 1, "event": "DEVICE_READY"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(EventProtocolError):
                EventFileTailer(path).poll()

    def test_tailer_wait_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.touch()
            with self.assertRaises(EventWaitTimeout):
                EventFileTailer(path).wait_for("MATCH_READY", timeout=0.01)


if __name__ == "__main__":
    unittest.main()

