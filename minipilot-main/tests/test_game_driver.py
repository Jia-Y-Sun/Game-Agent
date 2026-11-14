from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from integration.game_driver import GameDriverConfig, GameDriverProcess


DRIVER_SOURCE = textwrap.dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    event_file = Path(sys.argv[1])
    stop_file = Path(sys.argv[2])

    def emit(seq, event, **extra):
        record = {
            "schema_version": 1,
            "run_id": "driver-test",
            "seq": seq,
            "ts": "2026-09-29T21:00:00+08:00",
            "event": event,
            **extra,
        }
        with event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\\n")

    emit(1, "SCRIPT_STARTED")
    emit(2, "DEVICE_READY")
    emit(3, "WAITING_MATCH_READY")
    while not stop_file.exists():
        time.sleep(0.05)
    emit(4, "SCRIPT_STOPPED", reason="stop_file")
    raise SystemExit(3)
    """
)


class GameDriverTests(unittest.TestCase):
    def test_stop_file_controls_driver_and_preserves_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "driver.py"
            script.write_text(DRIVER_SOURCE, encoding="utf-8")
            event_file = root / "events.jsonl"
            stop_file = root / "stop.game"
            log_file = root / "driver.log"

            driver = GameDriverProcess(
                GameDriverConfig(
                    command=[
                        sys.executable,
                        str(script),
                        str(event_file),
                        str(stop_file),
                    ],
                    cwd=root,
                    event_file=event_file,
                    stop_file=stop_file,
                    log_file=log_file,
                )
            )
            driver.start()
            self.assertTrue(driver.is_running)
            driver.request_stop()
            self.assertEqual(driver.wait(timeout=5), 3)
            self.assertFalse(driver.is_running)
            self.assertIn("SCRIPT_STOPPED", event_file.read_text(encoding="utf-8"))
            self.assertTrue(log_file.exists())


if __name__ == "__main__":
    unittest.main()

