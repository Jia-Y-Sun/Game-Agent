from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from integration.pregame_agent import PregameAgentConfig, PregameAgentProcess


AGENT_SOURCE = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    command_file = Path(sys.argv[1])
    while True:
        if command_file.exists() and "/stop" in command_file.read_text(encoding="utf-8"):
            raise SystemExit(0)
        time.sleep(0.05)
    """
)


class PregameAgentTests(unittest.TestCase):
    def test_live_command_stops_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "agent.py"
            script.write_text(AGENT_SOURCE, encoding="utf-8")
            live_file = root / "live.jsonl"
            log_file = root / "agent.log"

            process = PregameAgentProcess(
                PregameAgentConfig(
                    command=[sys.executable, str(script), str(live_file)],
                    cwd=root,
                    live_command_file=live_file,
                    log_file=log_file,
                )
            )
            process.start()
            self.assertTrue(process.is_running)
            process.request_stop("MATCH_READY reached")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertFalse(process.is_running)
            self.assertTrue(log_file.exists())


if __name__ == "__main__":
    unittest.main()
