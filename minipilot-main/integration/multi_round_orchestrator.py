"""Run multiple sequential game-probe rounds through the base orchestrator."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from integration.game_profiles import GAME_PROFILES


DEFAULT_FIRST_ROUND_GOAL = GAME_PROFILES["jcc"]["first_goal"]
DEFAULT_POSTGAME_GOAL = GAME_PROFILES["jcc"]["postgame_goal"]
DEFAULT_RECOVERY_GOAL = GAME_PROFILES["jcc"]["recovery_goal"]


class MultiRoundError(RuntimeError):
    """Raised when the multi-round session cannot continue."""


def parse_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="multi-round-orchestrator",
        description=(
            "Run sequential game-probe rounds. Wrapper options are parsed "
            "here; all unknown arguments are passed to game_probe_orchestrator."
        ),
    )
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--round-delay", type=float, default=10.0)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--game-id", default="jcc", choices=tuple(GAME_PROFILES))
    parser.add_argument("--pregame-goal", default=None)
    parser.add_argument("--postgame-goal", default=None)
    parser.add_argument("--recovery-goal", default=None)
    parser.add_argument("--max-round-retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_known_args(argv)


def _remove_option(arguments: list[str], option: str) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for index, item in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        if item == option:
            skip_next = True
            continue
        if item.startswith(option + "="):
            continue
        cleaned.append(item)
    return cleaned


def _build_round_command(
    *,
    base_args: list[str],
    round_index: int,
    session_id: str,
    pregame_goal: str,
    game_id: str = "jcc",
) -> list[str]:
    cleaned_args = _remove_option(base_args, "--run-id")
    cleaned_args = _remove_option(cleaned_args, "--pregame-goal")
    cleaned_args = _remove_option(cleaned_args, "--game-id")
    command = [
        sys.executable,
        "-m",
        "integration.game_probe_orchestrator",
        *cleaned_args,
    ]
    command.extend(["--game-id", game_id, "--run-id", f"{session_id}-r{round_index}"])
    command.extend(["--pregame-goal", pregame_goal])
    if round_index > 1:
        command.append("--skip-launch-game")
    return command


def _replace_option_value(
    arguments: list[str],
    option: str,
    value: str,
) -> list[str]:
    cleaned = _remove_option(arguments, option)
    cleaned.extend([option, value])
    return cleaned


def _write_session_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    if args.rounds < 1:
        raise SystemExit("--rounds must be at least 1")
    if args.round_delay < 0:
        raise SystemExit("--round-delay cannot be negative")
    if args.max_round_retries < 0:
        raise SystemExit("--max-round-retries cannot be negative")

    profile = GAME_PROFILES[args.game_id]
    passthrough = _replace_option_value(
        passthrough, "--runs-dir", str(Path(args.runs_dir).resolve())
    )

    session_id = args.session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = Path(args.runs_dir) / f"{session_id}-multi-round"
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = session_dir / "session_manifest.json"
    manifest = {
        "session_id": session_id,
        "game_id": args.game_id,
        "rounds_requested": args.rounds,
        "rounds": [],
        "status": "running",
    }
    _write_session_manifest(manifest_path, manifest)

    for round_index in range(1, args.rounds + 1):
        pregame_goal = (
            (args.pregame_goal or profile["first_goal"])
            if round_index == 1
            else (args.postgame_goal or profile["postgame_goal"])
        )
        command = _build_round_command(
            base_args=passthrough,
            round_index=round_index,
            session_id=session_id,
            pregame_goal=pregame_goal,
            game_id=args.game_id,
        )
        round_log = session_dir / f"round-{round_index:02d}.log"
        round_record = {
            "round": round_index,
            "command": command,
            "log": str(round_log),
            "status": "running",
        }
        manifest["rounds"].append(round_record)
        _write_session_manifest(manifest_path, manifest)

        if args.dry_run:
            print(" ".join(command))
            round_record["status"] = "dry_run"
            continue

        attempts = args.max_round_retries + 1
        for attempt in range(1, attempts + 1):
            attempt_command = list(command)
            if attempt > 1:
                attempt_command = [
                    item for item in attempt_command
                    if item != "--skip-launch-game"
                ]
                attempt_command = _replace_option_value(
                    attempt_command,
                    "--run-id",
                    f"{session_id}-r{round_index}-retry{attempt}",
                )
                attempt_command = _remove_option(
                    attempt_command,
                    "--pregame-goal",
                )
                recovery_goal = args.recovery_goal or profile["recovery_goal"]
                attempt_command.extend(
                    ["--skip-launch-game", "--pregame-goal", recovery_goal]
                )
            round_record["attempt"] = attempt
            _write_session_manifest(manifest_path, manifest)
            with round_log.open("ab") as log_handle:
                log_handle.write(
                    f"\n=== round {round_index} attempt {attempt} ===\n".encode(
                        "utf-8"
                    )
                )
                result = subprocess.run(
                    attempt_command,
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                log_handle.write(result.stdout.encode("utf-8"))
                log_handle.flush()
            if result.returncode == 0:
                round_record["status"] = "ok"
                break
            round_record["status"] = "failed"
            round_record["exit_code"] = result.returncode
        if round_record["status"] != "ok":
            manifest["status"] = "failed"
            _write_session_manifest(manifest_path, manifest)
            return 1

        _write_session_manifest(manifest_path, manifest)
        if round_index < args.rounds:
            time.sleep(args.round_delay)

    manifest["status"] = "dry_run" if args.dry_run else "ok"
    _write_session_manifest(manifest_path, manifest)
    print(f"Session manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
