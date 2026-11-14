from __future__ import annotations

import sys
import unittest

from integration.multi_round_orchestrator import (
    _build_round_command,
    _replace_option_value,
)


class MultiRoundOrchestratorTests(unittest.TestCase):
    def test_round_one_uses_cold_start_command(self) -> None:
        command = _build_round_command(
            base_args=["--config", "config.json"],
            round_index=1,
            session_id="session",
            pregame_goal="cold start goal",
        )
        self.assertEqual(command[:3], [sys.executable, "-m", "integration.game_probe_orchestrator"])
        self.assertIn("--run-id", command)
        self.assertIn("session-r1", command)
        self.assertIn("cold start goal", command)
        self.assertNotIn("--skip-launch-game", command)

    def test_later_round_reuses_running_game(self) -> None:
        command = _build_round_command(
            base_args=["--config", "config.json"],
            round_index=2,
            session_id="session",
            pregame_goal="next match goal",
        )
        self.assertIn("--skip-launch-game", command)
        self.assertIn("next match goal", command)

    def test_retry_can_use_unique_run_id(self) -> None:
        command = _replace_option_value(
            ["--run-id", "session-r1"],
            "--run-id",
            "session-r1-retry2",
        )
        self.assertEqual(command.count("--run-id"), 1)
        self.assertIn("session-r1-retry2", command)


if __name__ == "__main__":
    unittest.main()
