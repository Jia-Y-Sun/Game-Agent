import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from integration.game_probe_orchestrator import (
    parse_args, _build_game_command, _build_pregame_command,
)
from integration.multi_round_orchestrator import main


class MultiGameIntegrationTests(unittest.TestCase):
    def test_game_defaults_and_navigation_backends(self):
        for game_id, package, capture in (
            ("jcc", "com.tencent.jkchess", "jcc"),
            ("wzry", "com.tencent.tmgp.sgame", "honorofkings"),
        ):
            with self.subTest(game_id=game_id):
                args = parse_args(["--game-driver", "driver.py", "--game-id", game_id,
                                   "--game-python", "game-python", "--run-id", "shared-run"])
                self.assertEqual(args.game_package, package)
                self.assertEqual(args.capture_app, capture)
                command = _build_game_command(args, event_file=Path("events"), stop_file=Path("stop"))
                self.assertEqual(command[command.index("--run-id") + 1], "shared-run")
                pregame = _build_pregame_command(args, live_command_file=Path("live"),
                                                 pregame_runs_dir=Path("runs"))
                if game_id == "wzry":
                    self.assertEqual(pregame[0], "game-python")
                    self.assertTrue(pregame[1].endswith("pregame_driver.py"))
                    self.assertIn("--skip-launch-game", pregame)
                else:
                    self.assertIn("mini_pilot.main", pregame)

    def test_profile_defaults_do_not_override_explicit_values(self):
        args = parse_args(["--game-driver", "driver.py", "--game-id", "wzry",
                           "--game-package", "test.package", "--capture-app", "custom",
                           "--pregame-goal", "test goal"])
        self.assertEqual(args.game_package, "test.package")
        self.assertEqual(args.capture_app, "custom")
        self.assertEqual(args.pregame_goal, "test goal")

    def test_two_rounds_retry_and_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_results = [SimpleNamespace(returncode=0, stdout="round one"),
                            SimpleNamespace(returncode=1, stdout="retry needed"),
                            SimpleNamespace(returncode=0, stdout="round two")]
            with patch("integration.multi_round_orchestrator.subprocess.run", side_effect=fake_results) as runner:
                result = main(["--game-id", "wzry", "--rounds", "2", "--round-delay", "0",
                               "--session-id", "test", "--runs-dir", directory,
                               "--pregame-goal", "first custom", "--postgame-goal", "second custom",
                               "--recovery-goal", "recover custom", "--game-driver", "driver.py"])
            self.assertEqual(result, 0)
            self.assertEqual(runner.call_count, 3)
            for index, expected_goal in enumerate(("first custom", "second custom", "recover custom")):
                command = runner.call_args_list[index].args[0]
                self.assertEqual(command[command.index("--runs-dir") + 1], str(Path(directory).resolve()))
                self.assertEqual(command[command.index("--game-id") + 1], "wzry")
                self.assertEqual(command[command.index("--pregame-goal") + 1], expected_goal)
                self.assertEqual("--skip-launch-game" in command, index > 0)
                parse_args(command[3:])
            manifest = json.loads((Path(directory) / "test-multi-round/session_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ok")
            self.assertEqual(len(manifest["rounds"]), 2)

    def test_dry_run_never_starts_process_and_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("integration.multi_round_orchestrator.subprocess.run") as runner:
                main(["--game-id", "wzry", "--rounds", "2", "--dry-run",
                      "--session-id", "preview", "--runs-dir", directory, "--game-driver", "driver.py"])
                runner.assert_not_called()
            manifest = json.loads((Path(directory) / "preview-multi-round/session_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "dry_run")
