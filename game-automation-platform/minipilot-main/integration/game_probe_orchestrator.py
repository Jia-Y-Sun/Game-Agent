"""Run Unicapture around an external game driver."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from integration.game_driver import GameDriverConfig, GameDriverProcess
from integration.game_profiles import GAME_PROFILES
from integration.pregame_agent import PregameAgentConfig, PregameAgentProcess
from integration.game_events import (
    EventFileTailer,
    EventProtocolError,
    EventWaitTimeout,
    GameEvent,
)
from mini_pilot.capture import CaptureError, UnicaptureSession
from mini_pilot.device import run_adb
from mini_pilot.model import ModelConfig
from mini_pilot.settings import SettingsError, load_global_settings


class OrchestratorError(RuntimeError):
    """Raised when the orchestration flow cannot complete."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="game-probe-orchestrator",
        description=(
            "Start Unicapture, run an external game driver, and use JSONL "
            "MATCH_READY/MATCH_FINISHED events as business boundaries."
        ),
    )
    parser.add_argument("--config", default="mini_pilot.config.json")
    parser.add_argument("--runs-dir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--game-id",
        default="jcc",
        choices=tuple(GAME_PROFILES),
        help="Game identifier used in the run directory and manifest.",
    )
    parser.add_argument("--device-id", default="127.0.0.1:5555")
    parser.add_argument(
        "--adb",
        default=None,
        help="Explicit ADB executable path, for example LDPlayer14\\adb.exe.",
    )
    parser.add_argument("--game-python", default=sys.executable)
    parser.add_argument("--game-driver", required=True)
    parser.add_argument("--game-directory", default=None)
    parser.add_argument("--game-timeout", type=int, default=3600)
    parser.add_argument("--match-ready-timeout", type=float, default=180.0)
    parser.add_argument("--business-duration", type=int, default=None)
    parser.add_argument(
        "--postprocess-ocr-every",
        type=int,
        default=10,
        help="Run postprocess OCR every N seconds when no latency ROI is known.",
    )
    parser.add_argument(
        "--capture-ceiling",
        type=int,
        default=None,
        help=(
            "Maximum capture duration in seconds. Defaults to "
            "game-timeout + 300 for external-control runs."
        ),
    )
    parser.add_argument("--capture-app", default=None)
    parser.add_argument("--capture-scene", default="battle")
    parser.add_argument("--capture-mode", default=None)
    parser.add_argument("--capture-pcap-mode", default=None)
    parser.add_argument("--capture-pcap-app-package", default=None)
    parser.add_argument("--capture-pcapdroid-api-key", default=None)
    parser.add_argument("--capture-resolution", default=None)
    parser.add_argument("--capture-location", default=None)
    parser.add_argument("--capture-skip-pcapdroid-preflight", action="store_true")
    parser.add_argument("--capture-no-pcap", action="store_true")
    parser.add_argument("--capture-disable-qoe", action="store_true")
    parser.add_argument("--capture-disable-postprocess", action="store_true")
    parser.add_argument("--capture-disable-flow-labeling", action="store_true")
    parser.add_argument("--capture-postprocess-disable-vlm", action="store_true")
    parser.add_argument("--unicapture-dir", default=None)
    parser.add_argument("--unicapture-python", default=None)
    parser.add_argument("--game-package", default=None)
    parser.add_argument(
        "--game-launch-activity",
        default=None,
    )
    parser.add_argument("--skip-launch-game", action="store_true")
    parser.add_argument(
        "--disable-pregame",
        action="store_true",
        help="Do not run the MiniPilot pre-game navigation agent.",
    )
    parser.add_argument(
        "--pregame-goal",
        default=None,
    )
    parser.add_argument("--pregame-max-loops", type=int, default=40)
    parser.add_argument("--pregame-timeout", type=float, default=600.0)
    parser.add_argument("--pregame-stop-timeout", type=float, default=30.0)
    parser.add_argument(
        "--pregame-driver",
        default=None,
        help="Optional deterministic pre-game driver script.",
    )
    parser.add_argument("--pregame-python", default=sys.executable)
    parser.add_argument("--pregame-directory", default=None)
    parser.add_argument(
        "--pregame-arg",
        action="append",
        default=[],
        help="Extra argument appended to the custom pre-game driver command.",
    )
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--game-arg",
        action="append",
        default=[],
        help="Extra argument appended to the external game-driver command.",
    )
    args = parser.parse_args(argv)
    profile = GAME_PROFILES[args.game_id]
    args.capture_app = args.capture_app or profile["capture_app"]
    args.game_package = args.game_package or profile["package"]
    args.game_launch_activity = args.game_launch_activity or profile["activity"]
    args.pregame_goal = args.pregame_goal or profile["first_goal"]
    if args.game_id == "wzry" and not args.pregame_driver:
        args.pregame_directory = args.pregame_directory or str(
            Path(args.game_directory or Path(args.game_driver).parent).resolve()
        )
        args.pregame_driver = str(Path(args.pregame_directory) / "pregame_driver.py")
        args.pregame_python = args.game_python
    return args


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text)
    return "-".join(part for part in cleaned.split("-") if part)[:48] or "jcc"


def _create_run_dir(base_dir: Path, run_id: str | None, game_id: str) -> Path:
    identifier = _slug(run_id or datetime.now().strftime("%Y%m%d-%H%M%S"))
    path = base_dir / f"{identifier}-{_slug(game_id)}-external"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def _absolute_time(value: str | None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    return datetime.now().astimezone()


def _business_marker(
    start: GameEvent,
    end: GameEvent | None = None,
    *,
    run_id: str | None = None,
    result: str | None = None,
    finish_source: str | None = None,
    result_source: str | None = None,
) -> dict[str, Any]:
    start_time = _absolute_time(start.ts)
    marker: dict[str, Any] = {
        "business_start_time": start_time.isoformat(),
        "source": "game_script_match_ready",
        "run_id": run_id,
        "match_ready_seq": start.seq,
    }
    if end is not None:
        end_time = _absolute_time(end.ts)
        duration = max(1, int(round((end_time - start_time).total_seconds())))
        marker.update(
            {
                "business_end_time": end_time.isoformat(),
                "target_duration_seconds": duration,
                "match_finished_seq": end.seq,
                "match_result": result or "unknown",
                "match_finish_source": finish_source or end.event,
                "match_result_source": result_source or "unknown",
            }
        )
    return marker


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _build_capture_args(args: argparse.Namespace) -> SimpleNamespace:
    capture_ceiling = args.capture_ceiling or max(300, args.game_timeout + 300)
    return SimpleNamespace(
        capture="unicapture",
        unicapture_dir=args.unicapture_dir,
        unicapture_python=args.unicapture_python,
        custom_app_package=None,
        capture_app=args.capture_app,
        capture_scene=args.capture_scene,
        custom_app_type=None,
        custom_app_scene=None,
        run_duration=args.business_duration,
        business_start_timeout=180,
        activate_fallback_delay=30,
        capture_duration=capture_ceiling,
        capture_pcap_app_package=args.capture_pcap_app_package,
        capture_resolution=args.capture_resolution,
        capture_location=args.capture_location,
        capture_mode=args.capture_mode,
        capture_pcap_mode=args.capture_pcap_mode,
        capture_pcapdroid_api_key=args.capture_pcapdroid_api_key,
        capture_skip_pcapdroid_preflight=args.capture_skip_pcapdroid_preflight,
        capture_no_pcap=args.capture_no_pcap,
        capture_disable_qoe=args.capture_disable_qoe,
        capture_disable_postprocess=args.capture_disable_postprocess,
        capture_disable_flow_labeling=args.capture_disable_flow_labeling,
        capture_postprocess_disable_vlm=args.capture_postprocess_disable_vlm,
        capture_postprocess_ocr_every=args.postprocess_ocr_every,
        device_id=args.device_id,
    )


def _start_capture(args: argparse.Namespace, run_dir: Path) -> UnicaptureSession:
    from mini_pilot.main import _maybe_start_capture

    settings = load_global_settings(args.config)
    capture_args = _build_capture_args(args)
    capture_goal = f"capture {args.capture_app} {args.capture_scene}".strip()
    session = _maybe_start_capture(
        capture_args,
        settings.capture,
        capture_goal,
        run_dir,
        ModelConfig(base_url="", model_name="", api_key=""),
    )
    if session is None:
        raise OrchestratorError("Unicapture was not started.")
    return session


def _launch_game(args: argparse.Namespace) -> None:
    if args.skip_launch_game:
        return
    component = f"{args.game_package}/{args.game_launch_activity}"
    run_adb(
        ["shell", "am", "start", "-n", component],
        device_id=args.device_id,
    )


def _build_game_command(
    args: argparse.Namespace,
    *,
    event_file: Path,
    stop_file: Path,
) -> list[str]:
    return [
        str(args.game_python),
        str(Path(args.game_driver).resolve()),
        "--mode",
        "external",
        "--device-id",
        args.device_id,
        "--event-file",
        str(event_file),
        "--stop-file",
        str(stop_file),
        "--timeout",
        str(args.game_timeout),
        "--match-ready-timeout",
        str(args.match_ready_timeout),
        *(["--run-id", args.run_id] if args.run_id else []),
        *args.game_arg,
    ]


def _build_pregame_command(
    args: argparse.Namespace,
    *,
    live_command_file: Path,
    pregame_runs_dir: Path,
) -> list[str]:
    if args.pregame_driver:
        command = [
            str(args.pregame_python),
            str(Path(args.pregame_driver).resolve()),
            "--device-id",
            args.device_id,
            "--live-command-file",
            str(live_command_file),
            "--timeout",
            str(max(1.0, args.pregame_timeout)),
            "--goal",
            args.pregame_goal,
        ]
        if args.adb:
            command.extend(["--adb", str(Path(args.adb).resolve())])
        if args.game_id == "wzry":
            command.append("--skip-launch-game")
        command.extend(args.pregame_arg)
        return command

    command = [
        sys.executable,
        "-m",
        "mini_pilot.main",
        "--config",
        str(Path(args.config).resolve()),
        "--device-id",
        args.device_id,
        "--goal",
        args.pregame_goal,
        "--capture",
        "none",
        "--no-live-console",
        "--live-command-file",
        str(live_command_file),
        "--no-close-app",
        "--runs-dir",
        str(pregame_runs_dir),
        "--max-loops",
        str(max(1, args.pregame_max_loops)),
    ]
    if args.profile:
        command.extend(["--profile", args.profile])
    return command


def _wait_for_terminal_event(
    tailer: EventFileTailer,
    driver: GameDriverProcess,
    *,
    timeout: float,
) -> GameEvent | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for event in tailer.events:
            if event.event in {"MATCH_FINISHED", "ERROR"}:
                return event
        for event in tailer.poll():
            if event.event in {"MATCH_FINISHED", "ERROR"}:
                return event
        if not driver.is_running:
            for event in tailer.events:
                if event.event in {"MATCH_FINISHED", "ERROR"}:
                    return event
            for event in tailer.poll():
                if event.event in {"MATCH_FINISHED", "ERROR"}:
                    return event
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.2, remaining))


def _wait_for_match_ready(
    tailer: EventFileTailer,
    driver: GameDriverProcess,
    *,
    timeout: float,
    pregame: PregameAgentProcess | None = None,
) -> GameEvent:
    """Wait for MATCH_READY while detecting an early driver exit."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for event in tailer.events:
            if event.event == "MATCH_READY":
                return event
            if event.event == "ERROR":
                raise OrchestratorError(
                    f"Game driver reported ERROR before MATCH_READY: "
                    f"{event.get('message', '')}"
                )
        for event in tailer.poll():
            if event.event == "MATCH_READY":
                return event
            if event.event == "ERROR":
                raise OrchestratorError(
                    f"Game driver reported ERROR before MATCH_READY: "
                    f"{event.get('message', '')}"
                )
        if not driver.is_running:
            raise OrchestratorError(
                "Game driver exited before MATCH_READY."
            )
        if pregame is not None and not pregame.is_running:
            pregame_code = pregame.returncode
            if pregame_code not in (None, 0):
                raise OrchestratorError(
                    "Pre-game driver exited before MATCH_READY "
                    f"(code={pregame_code}). See {pregame.log_file}"
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EventWaitTimeout("Timed out waiting for MATCH_READY.")
        time.sleep(min(0.2, remaining))


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _finalize_manifest(
    path: Path,
    payload: dict[str, Any],
    run_dir: Path,
) -> None:
    _write_manifest(path, payload)
    try:
        from integration.probe_report import write_report

        report_path = write_report(run_dir, payload)
        payload["report_status"] = "ok"
        payload["report_file"] = str(report_path)
    except Exception as exc:  # noqa: BLE001
        payload["report_status"] = f"failed: {exc}"
    _write_manifest(path, payload)


def _postprocess_status(capture_dir: Path) -> str:
    summaries = sorted(
        capture_dir.glob("*/postprocess/summary.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not summaries:
        return "summary_not_found"
    try:
        payload = json.loads(summaries[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "summary_unreadable"
    return str(payload.get("status") or "unknown")


def run(args: argparse.Namespace) -> int:
    if args.adb:
        os.environ["MINI_PILOT_ADB_BIN"] = str(Path(args.adb).resolve())

    base_dir = Path(args.runs_dir or "runs")
    run_dir = _create_run_dir(base_dir, args.run_id, args.game_id)
    effective_run_id = args.run_id or run_dir.name
    args.run_id = effective_run_id
    events_dir = run_dir / "events"
    control_dir = run_dir / "control"
    events_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)

    event_file = events_dir / "game_driver.jsonl"
    stop_file = control_dir / "stop.game"
    driver_log = run_dir / "game_driver.log"
    manifest_path = run_dir / "manifest.json"

    print(f"Run dir: {run_dir}")
    print(f"Event file: {event_file}")
    print(f"Stop file: {stop_file}")

    capture: UnicaptureSession | None = None
    driver: GameDriverProcess | None = None
    pregame: PregameAgentProcess | None = None
    pregame_exit_code: int | None = None
    tailer = EventFileTailer(event_file)
    marker_path = run_dir / "capture" / "business_timing.json"
    manifest: dict[str, Any] = {
        "run_id": effective_run_id,
        "game_id": args.game_id,
        "device_id": args.device_id,
        "binding_mode": "adb_tcp" if ":" in args.device_id else "adb_serial",
        "game_package": args.game_package,
        "game_mode": "external",
        "event_file": str(event_file),
        "stop_file": str(stop_file),
        "pregame_enabled": not args.disable_pregame,
    }

    try:
        capture = _start_capture(args, run_dir)
        _launch_game(args)
        driver = GameDriverProcess(
            GameDriverConfig(
                command=_build_game_command(
                    args,
                    event_file=event_file,
                    stop_file=stop_file,
                ),
                cwd=(
                    Path(args.game_directory).resolve()
                    if args.game_directory
                    else Path(args.game_driver).resolve().parent
                ),
                event_file=event_file,
                stop_file=stop_file,
                log_file=driver_log,
            )
        )
        driver.start()

        if not args.disable_pregame:
            pregame = PregameAgentProcess(
                PregameAgentConfig(
                    command=_build_pregame_command(
                        args,
                        live_command_file=run_dir / "pregame" / "live_commands.jsonl",
                        pregame_runs_dir=run_dir / "pregame" / "runs",
                    ),
                    cwd=(
                        Path(args.pregame_directory).resolve()
                        if args.pregame_directory
                        else Path(__file__).resolve().parents[1]
                    ),
                    live_command_file=run_dir / "pregame" / "live_commands.jsonl",
                    log_file=run_dir / "pregame_agent.log",
                )
            )
            pregame.start()

        match_ready = _wait_for_match_ready(
            tailer,
            driver,
            timeout=args.match_ready_timeout,
            pregame=pregame,
        )
        if pregame is not None and pregame.is_running:
            pregame.request_stop("MATCH_READY reached")
            try:
                pregame_exit_code = pregame.wait(
                    timeout=max(1.0, args.pregame_stop_timeout)
                )
            except subprocess.TimeoutExpired:
                pregame.terminate("MATCH_READY stop timeout")
        _write_marker(
            marker_path,
            _business_marker(
                match_ready,
                run_id=effective_run_id,
            ),
        )
        print(f"MATCH_READY received: {match_ready.ts}")

        terminal = _wait_for_terminal_event(
            tailer,
            driver,
            timeout=args.game_timeout,
        )
        if terminal is None:
            raise OrchestratorError(
                "Game driver exited or timed out before MATCH_FINISHED."
            )
        if terminal.event == "ERROR":
            raise OrchestratorError(
                f"Game driver reported ERROR: {terminal.get('message', '')}"
            )

        _write_marker(
            marker_path,
            _business_marker(
                match_ready,
                terminal,
                run_id=args.run_id or run_dir.name,
                result=str(terminal.get("result") or "unknown"),
                finish_source=str(
                    terminal.get("source_event")
                    or terminal.get("finish_source")
                    or terminal.event
                ),
                result_source=str(terminal.get("result_source") or "unknown"),
            ),
        )
        print(f"MATCH_FINISHED received: {terminal.ts}")

        if driver.is_running:
            driver.request_stop()
        try:
            exit_code = (
                driver.wait(timeout=10.0)
                if driver.is_running
                else driver.returncode
            )
        except subprocess.TimeoutExpired as exc:
            driver.terminate()
            raise OrchestratorError(
                "Game driver did not exit after MATCH_FINISHED."
            ) from exc
        manifest["game_exit_code"] = exit_code

        if capture is not None:
            capture.stop()
            capture = None

        postprocess_status = _postprocess_status(run_dir / "capture")
        manifest.update(
            {
                "match_ready_time": match_ready.ts,
                "match_finished_time": terminal.ts,
                "match_result": terminal.get("result", "unknown"),
                "match_finish_source": terminal.get(
                    "source_event",
                    terminal.event,
                ),
                "match_result_source": terminal.get("result_source", "unknown"),
                "pregame_exit_code": pregame_exit_code,
                "capture_status": "ok",
                "postprocess_status": postprocess_status,
                "status": "ok",
            }
        )
        _finalize_manifest(manifest_path, manifest, run_dir)
        print(f"TASK_STATUS: SUCCESS")
        print(f"Manifest: {manifest_path}")
        return 0
    except (
        EventProtocolError,
        EventWaitTimeout,
        CaptureError,
        OrchestratorError,
        SettingsError,
    ) as exc:
        manifest.update({"status": "failed", "error": str(exc)})
        _finalize_manifest(manifest_path, manifest, run_dir)
        print(f"TASK_STATUS: FAILED: {exc}", file=sys.stderr)
        if driver is not None and driver.is_running:
            driver.terminate()
        if capture is not None:
            try:
                capture.stop()
            except CaptureError as stop_exc:
                print(f"Capture stop error: {stop_exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        manifest.update({"status": "interrupted"})
        _finalize_manifest(manifest_path, manifest, run_dir)
        if driver is not None and driver.is_running:
            driver.terminate()
        if capture is not None:
            try:
                capture.stop()
            except CaptureError as stop_exc:
                print(f"Capture stop error: {stop_exc}", file=sys.stderr)
        return 130
    finally:
        if driver is not None and driver.is_running:
            driver.terminate()
        if pregame is not None and pregame.is_running:
            pregame.terminate("orchestrator shutdown")


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
