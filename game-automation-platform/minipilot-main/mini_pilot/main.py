"""Command line entry point for MiniPilot v5.

v5 keeps the single-model executor and adds live command-line corrections.
One vision-language
model receives the original user goal, current screenshot, runtime progress,
and recent action history, then chooses the next phone action directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import queue
from datetime import datetime
from pathlib import Path

from mini_pilot.agent import AgentConfig, AgentError, AgentRunResult, MiniPilotAgent
from mini_pilot.apps import get_package, list_supported_apps, register_custom_app
from mini_pilot.business_timing import BusinessTimingController, create_vlm_probe
from mini_pilot.capture import (
    CaptureError,
    UnicaptureConfig,
    UnicaptureSession,
    get_unicapture_catalog,
    infer_unicapture_selection,
    validate_unicapture_selection,
)
from mini_pilot.device import (
    DeviceError,
    close_app_by_package,
    get_current_app,
    list_devices,
    require_device,
)
from mini_pilot.model import ModelClient, ModelConfig, ModelError
from mini_pilot.settings import RoleSettings, SettingsError, load_global_settings
from mini_pilot.text_repair import recover_mojibake


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="mini-pilot-v5",
        description="MiniPilot v5: single-model Android phone automation with live corrections.",
    )
    parser.add_argument("task", nargs="?", help="Task or goal to run.")
    parser.add_argument("--goal", default=None, help="Task or goal to run.")
    parser.add_argument(
        "--goal-file",
        default=None,
        help="UTF-8 text file containing the goal.",
    )

    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL.")
    parser.add_argument("--model", default=None, help="Model name.")
    parser.add_argument("--api-key", default=None, help="API key.")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--image-max-side", type=int, default=None)
    parser.add_argument(
        "--profile",
        default=None,
        help="Executor profile name from mini_pilot.config.json.",
    )

    parser.add_argument("--max-loops", "--max-steps", dest="max_loops", type=int, default=None)
    parser.add_argument(
        "--run-duration",
        type=int,
        default=None,
        help="Runtime duration in seconds. The timer starts after the target app/content is reached.",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=float,
        default=1.5,
        help="Maximum seconds to execute for a model-requested Wait action.",
    )
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--screenshot-dir", default=None)
    parser.add_argument("--history-steps", type=int, default=3)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--no-live-input",
        action="store_true",
        help="Disable runtime command-line corrections.",
    )
    parser.add_argument(
        "--live-command-file",
        default=None,
        help="JSONL file used for live commands from an external UI.",
    )
    parser.add_argument(
        "--no-live-console",
        action="store_true",
        help="Do not open the separate live input console.",
    )

    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--check-device", action="store_true")
    parser.add_argument("--current-app", action="store_true")
    parser.add_argument(
        "--list-apps",
        action="store_true",
        help="List supported apps with their capture scene names.",
    )
    parser.add_argument(
        "--list-scenes",
        metavar="APP",
        help="List detailed capture scenes for one app and exit.",
    )

    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--config", default="mini_pilot.config.json")
    parser.add_argument(
        "--capture",
        default=None,
        help="Run sidecar capture: none or unicapture. Default: config or none.",
    )
    parser.add_argument("--capture-app", default=None)
    parser.add_argument("--capture-scene", default=None)
    parser.add_argument("--capture-duration", type=int, default=None)
    parser.add_argument(
        "--business-start-timeout",
        type=int,
        default=None,
        help="Extra seconds allowed for detecting business start (default: 100).",
    )
    parser.add_argument(
        "--activate-fallback-delay",
        type=int,
        default=None,
        help="Seconds to wait for VLM after activate/logcat detection (default: 30).",
    )
    parser.add_argument("--capture-resolution", default=None)
    parser.add_argument("--capture-location", default=None)
    parser.add_argument("--capture-mode", choices=["auto", "root", "noroot"], default=None)
    parser.add_argument(
        "--capture-pcap-mode",
        choices=["minimal", "standard", "full"],
        default=None,
    )
    parser.add_argument("--capture-pcap-app-package", default=None)
    parser.add_argument("--capture-pcapdroid-api-key", default=None)
    parser.add_argument(
        "--capture-skip-pcapdroid-preflight",
        action="store_true",
        help="Explicitly allow a legacy PCAPdroid build without API Key preflight support.",
    )
    parser.add_argument("--capture-no-pcap", action="store_true")
    parser.add_argument("--capture-disable-qoe", action="store_true")
    parser.add_argument("--capture-disable-postprocess", action="store_true")
    parser.add_argument("--capture-disable-flow-labeling", action="store_true")
    parser.add_argument("--capture-postprocess-disable-vlm", action="store_true")
    parser.add_argument("--capture-postprocess-ocr-every", type=int, default=None)
    parser.add_argument(
        "--operation-mode",
        choices=("agent", "manual"),
        default=None,
        help="Who operates the phone while Unicapture records (default: config or agent).",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Shortcut for --operation-mode manual.",
    )
    parser.add_argument("--custom-app-package", default=None)
    parser.add_argument("--custom-app-type", default=None)
    parser.add_argument("--custom-app-scene", default=None)
    parser.add_argument(
        "--no-close-app",
        action="store_true",
        help="Do not force-stop the target app after the task finishes.",
    )
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=1,
        help="Run the same task N times (default: 1).",
    )
    parser.add_argument(
        "--repeat-delay",
        type=float,
        default=10.0,
        help="Seconds between repeated runs (default: 10).",
    )
    parser.add_argument("--unicapture-dir", default=None)
    parser.add_argument("--unicapture-python", default=None)
    return parser.parse_args()


def main() -> int:
    """Run the CLI."""
    args = parse_args()

    if args.list_devices:
        return _list_devices(args.device_id)
    if args.check_device:
        return _check_device(args.device_id)
    if args.current_app:
        return _print_current_app(args.device_id)
    if args.list_apps:
        return _list_apps(args.unicapture_dir)
    if args.list_scenes:
        return _list_scenes(args.list_scenes, args.unicapture_dir)

    try:
        settings = load_global_settings(args.config)
    except SettingsError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    operation_mode = _resolve_operation_mode(args, settings.capture)
    goal = _resolve_goal(args)
    if not goal and operation_mode == "manual":
        app = args.capture_app or str(settings.capture.get("app") or "app")
        scene = args.capture_scene or str(settings.capture.get("scene") or "session")
        goal = f"manual-{app}-{scene}"
    if not goal:
        print("Error: provide a task, --goal, or --goal-file.", file=sys.stderr)
        return 2
    if args.run_duration is not None and args.run_duration <= 0:
        print("Error: --run-duration must be greater than 0.", file=sys.stderr)
        return 2
    if args.business_start_timeout is not None and args.business_start_timeout < 0:
        print("Error: --business-start-timeout cannot be negative.", file=sys.stderr)
        return 2
    if args.activate_fallback_delay is not None and args.activate_fallback_delay < 0:
        print("Error: --activate-fallback-delay cannot be negative.", file=sys.stderr)
        return 2
    repeat_error = _validate_repeat_options(args.repeat_count, args.repeat_delay)
    if repeat_error:
        print(f"Error: {repeat_error}", file=sys.stderr)
        return 2

    _maybe_register_custom_app(args, goal)
    try:
        args.device_id = _resolve_device_id(args.device_id)
        require_device(args.device_id)
    except DeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 1

    model_config = _build_model_config(
        args,
        settings.executor,
        settings.executor_active_profile,
        settings.executor_profiles,
    )
    image_max_side = args.image_max_side or model_config.image_max_side
    max_loops = (
        args.max_loops
        if args.max_loops is not None
        else _optional_int(settings.runtime.get("max_loops"))
        if settings.runtime.get("max_loops") is not None
        else 100
    )

    _print_header("MiniPilot v5 single-model config")
    print(f"Operation mode: {operation_mode}")
    print(f"Device: {args.device_id}")
    if operation_mode == "manual":
        print("Agent operation: disabled")
    else:
        print(f"Executor base URL: {model_config.base_url}")
        print(f"Executor model: {model_config.model_name}")
        print(f"Executor image max side: {image_max_side}")
        print(f"Max loops: {max_loops}")
    if args.run_duration is not None:
        print(f"Run duration: {args.run_duration}s")
    elif settings.capture.get("business_duration") is not None:
        print(
            "Run duration: "
            f"{_optional_int(settings.capture.get('business_duration'))}s (from config)"
        )
    if args.repeat_count > 1:
        print(f"Repeat mode: {args.repeat_count} run(s), {args.repeat_delay:g}s delay")

    overall_success = True
    last_message = ""
    for run_index in range(1, args.repeat_count + 1):
        if args.repeat_count > 1:
            _print_header(f"Repeat run {run_index}/{args.repeat_count}")
        success, last_message = _run_single(
            args,
            settings,
            goal,
            model_config,
            image_max_side,
            max_loops,
            operation_mode,
        )
        if not success:
            overall_success = False
            break
        if run_index < args.repeat_count:
            time.sleep(args.repeat_delay)

    _print_header("MiniPilot v5 execution")
    print(f"TASK_STATUS: {'SUCCESS' if overall_success else 'FAILED'}")
    print(f"TASK_COMPLETED: {str(overall_success).lower()}")
    print(f"Success: {overall_success}")
    print(f"Message: {last_message}")
    return 0 if overall_success else 1


def _maybe_register_custom_app(args: argparse.Namespace, goal: str) -> None:
    package = (args.custom_app_package or "").strip()
    if not package:
        return
    name = args.capture_app or _extract_app_name_from_goal(goal) or "custom_app"
    register_custom_app(name, package)
    print(f"[CustomApp] Registered '{name}' -> {package}")


def _validate_repeat_options(repeat_count: int, repeat_delay: float) -> str | None:
    if repeat_count < 1:
        return "--repeat-count must be at least 1."
    if repeat_delay < 0:
        return "--repeat-delay cannot be negative."
    return None


def _extract_app_name_from_goal(goal: str) -> str | None:
    for marker in ("打开", "使用", "启动", "进入"):
        if marker not in goal:
            continue
        remainder = goal.split(marker, 1)[1].strip()
        for separator in (" ", "，", ",", "。", "刷", "看", "玩", "搜"):
            if separator in remainder:
                remainder = remainder.split(separator, 1)[0]
        if remainder.strip():
            return remainder.strip()
    tokens = goal.strip().split()
    return tokens[0] if tokens else None


def _resolve_device_id(device_id: str | None) -> str | None:
    """Preserve an explicit device or auto-select the only ready device."""
    if device_id:
        return device_id
    devices = [device for device in list_devices() if device.status == "device"]
    if len(devices) == 1:
        selected = devices[0].device_id
        print(f"[Device] Auto-selected the only connected device: {selected}")
        return selected
    return None


def _run_single(
    args: argparse.Namespace,
    settings: object,
    goal: str,
    model_config: ModelConfig,
    image_max_side: int,
    max_loops: int,
    operation_mode: str,
) -> tuple[bool, str]:
    """Execute one complete capture and automation run."""
    run_dir = _create_run_dir(Path(settings.runs_dir or args.runs_dir), goal)
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "goal.txt").write_text(goal, encoding="utf-8")
    live_input = LiveCommandController(
        enabled=operation_mode == "agent" and not args.no_live_input,
        log_path=input_dir / "live_commands.txt",
        queue_path=Path(args.live_command_file)
        if args.live_command_file
        else input_dir / "live_commands.queue.jsonl",
        verbose=not args.quiet,
        open_console=not args.no_live_console and not args.live_command_file,
    )
    live_input.start()
    print(f"Run dir: {run_dir}")

    capture_session: UnicaptureSession | None = None
    timing_controller: BusinessTimingController | None = None
    try:
        capture_session = _maybe_start_capture(
            args, settings.capture, goal, run_dir, model_config
        )
        if capture_session is not None:
            timing_controller = _start_business_timing(capture_session)
    except CaptureError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False, str(exc)

    result: AgentRunResult
    try:
        if operation_mode == "manual":
            if capture_session is None:
                raise CaptureError("Manual mode requires Unicapture capture to be enabled.")
            success, message = _run_manual_operation(
                args, capture_session, timing_controller
            )
        else:
            duration_budget = _resolve_duration_budget(args, capture_session)
            model_client = ModelClient(model_config)
            agent = MiniPilotAgent(
                model_client=model_client,
                config=AgentConfig(
                    max_loops=max_loops,
                    device_id=args.device_id,
                    screenshot_dir=str(run_dir / "execution" / "screenshots"),
                    verbose=not args.quiet,
                    history_steps=args.history_steps,
                    image_max_side=image_max_side,
                    duration_budget_seconds=duration_budget,
                    duration_started_at=None,
                    duration_started_at_provider=(
                        timing_controller.duration_started_at
                        if timing_controller is not None
                        else None
                    ),
                    target_app_package=(
                        capture_session.target_app_package
                        if capture_session is not None
                        else None
                    ),
                    max_wait_seconds=args.max_wait_seconds,
                    loop_status_provider=(
                        capture_session.latest_loop_output
                        if capture_session is not None
                        else None
                    ),
                    operation_style_text=_build_operation_style_text(
                        settings.operation_style
                    ),
                    live_command_provider=live_input.snapshot,
                ),
            )
            result = agent.run(goal)
            success, message = result.success, result.message
    except (AgentError, ModelError, CaptureError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        success, message = False, str(exc)
    finally:
        if capture_session is not None:
            try:
                capture_session.stop()
            except CaptureError as exc:
                print(f"Capture stop error: {exc}", file=sys.stderr)
        if timing_controller is not None:
            timing_controller.cancel()
        target_package = _resolve_target_package(
            args,
            settings.capture,
            goal,
            capture_session,
        )
        if target_package and not args.no_close_app:
            try:
                close_app_by_package(target_package, device_id=args.device_id)
            except DeviceError as exc:
                print(f"App close error: {exc}", file=sys.stderr)

    _write_execution_summary(run_dir, success, message)
    print(f"Execution dir: {run_dir / 'execution'}")
    return success, message


def _resolve_target_package(
    args: argparse.Namespace,
    capture_settings: dict[str, object],
    goal: str,
    capture_session: UnicaptureSession | None,
) -> str | None:
    if capture_session is not None and capture_session.target_app_package:
        return capture_session.target_app_package
    explicit = (args.custom_app_package or args.capture_pcap_app_package or "").strip()
    if explicit:
        return explicit
    inferred = infer_unicapture_selection(goal, args.unicapture_dir)
    app_name = (
        args.capture_app
        or _optional_str(capture_settings.get("app"))
        or _optional_str(inferred.get("app"))
        or _extract_app_name_from_goal(goal)
    )
    return get_package(app_name) if app_name else None


def _resolve_goal(args: argparse.Namespace) -> str:
    if args.goal_file:
        return recover_mojibake(Path(args.goal_file).read_text(encoding="utf-8").strip())
    if args.goal:
        return recover_mojibake(args.goal.strip())
    return recover_mojibake((args.task or "").strip())


def _resolve_operation_mode(
    args: argparse.Namespace,
    capture_settings: dict[str, object],
) -> str:
    if args.manual:
        return "manual"
    configured = str(capture_settings.get("operation_mode") or "agent").strip().lower()
    selected = (args.operation_mode or configured).strip().lower()
    return selected if selected in {"agent", "manual"} else "agent"


def _run_manual_operation(
    args: argparse.Namespace,
    capture_session: UnicaptureSession,
    timing_controller: BusinessTimingController | None = None,
) -> tuple[bool, str]:
    duration = (
        args.run_duration
        if args.run_duration is not None
        else getattr(capture_session.config, "target_business_duration", None)
        if getattr(capture_session.config, "target_business_duration", None) is not None
        else args.capture_duration
        if args.capture_duration is not None
        else capture_session.config.duration
    )
    _print_header("Manual phone operation")
    print("Unicapture is ready. Operate the phone directly; no Agent calls will be made.")
    if capture_session.target_app_package:
        print(f"Target package: {capture_session.target_app_package}")
    if duration is None:
        print("No duration was set. Press Ctrl+C when manual operation is complete.")
    else:
        print(f"Manual duration: {duration}s")

    if timing_controller is not None:
        print(
            "Waiting for business start; effective-duration countdown begins "
            "after VLM/activate confirmation."
        )
        last_state = ""
        last_remaining: int | None = None
        try:
            while not timing_controller.wait(0.25):
                state = timing_controller.last_state
                if state != last_state:
                    print(f"[Business timing] {state}", flush=True)
                    last_state = state
                result = timing_controller.result
                if result is not None:
                    remaining = max(
                        0,
                        int(
                            result.business_start_monotonic
                            + result.target_duration_seconds
                            - time.monotonic()
                            + 0.999
                        ),
                    )
                    if remaining != last_remaining and remaining % 10 == 0:
                        print(
                            f"[Manual] effective business remaining: {remaining}s",
                            flush=True,
                        )
                        last_remaining = remaining
        except KeyboardInterrupt:
            return True, "Manual operation stopped by user."
        result = timing_controller.result
        source = result.source if result else "unknown"
        return True, (
            f"Manual operation completed after {duration}s effective business "
            f"time (start source: {source})."
        )

    started = time.monotonic()
    last_reported: int | None = None
    try:
        while True:
            if duration is not None:
                remaining = max(0, int(duration - (time.monotonic() - started) + 0.999))
                if remaining <= 0:
                    break
                if remaining != last_reported and (remaining == duration or remaining % 10 == 0):
                    print(f"[Manual] remaining: {remaining}s", flush=True)
                    last_reported = remaining
            time.sleep(0.25)
    except KeyboardInterrupt:
        elapsed = int(time.monotonic() - started)
        return True, f"Manual operation stopped by user after {elapsed}s."
    return True, f"Manual operation completed after {duration}s."


def _resolve_duration_budget(
    args: argparse.Namespace,
    capture_session: UnicaptureSession | None,
) -> int | None:
    """Resolve one effective business duration for capture and Agent runtime."""
    if args.run_duration is not None:
        return args.run_duration
    if capture_session is not None:
        return getattr(capture_session.config, "target_business_duration", None)
    return None


def _start_business_timing(
    capture_session: UnicaptureSession,
) -> BusinessTimingController | None:
    cfg = capture_session.config
    target_duration = cfg.target_business_duration
    if target_duration is None:
        return None
    package = capture_session.target_app_package

    def activation_probe() -> str | None:
        if not package:
            return None
        current = get_current_app(cfg.device_id)
        if package not in current:
            return None
        if capture_session.logcat_activation_detected():
            return "logcat_activate"
        return "foreground_activate"

    vlm_probe = None
    if cfg.postprocess_enable_vlm:
        vlm_probe = create_vlm_probe(
            device_id=cfg.device_id,
            app_type=cfg.custom_app_type or cfg.app_name,
            scene=cfg.scene,
            base_url=(
                os.getenv("QOE_VLM_BASE_URL")
                or cfg.postprocess_vlm_base_url
                or ""
            ),
            api_key=(
                os.getenv("QOE_VLM_API_KEY")
                or os.getenv("DASHSCOPE_API_KEY")
                or cfg.postprocess_vlm_api_key
                or ""
            ),
            model=(
                os.getenv("QOE_VLM_MODEL")
                or cfg.postprocess_vlm_model
                or ""
            ),
        )
    controller = BusinessTimingController(
        target_duration_seconds=target_duration,
        startup_timeout_seconds=cfg.business_start_timeout,
        activation_fallback_delay_seconds=cfg.activation_fallback_delay,
        marker_path=capture_session.business_timing_path,
        activation_probe=activation_probe,
        vlm_probe=vlm_probe,
        vlm_interval_seconds=cfg.postprocess_vlm_every,
    )
    controller.start()
    return controller


class LiveCommandController:
    """Collect live user corrections without sharing the main logging console."""

    def __init__(
        self,
        *,
        enabled: bool,
        log_path: Path,
        queue_path: Path,
        verbose: bool,
        open_console: bool,
    ):
        self.enabled = enabled
        self.log_path = log_path
        self.queue_path = queue_path
        self.verbose = verbose
        self.open_console = open_console
        self._queue: queue.Queue[str] = queue.Queue()
        self._queue_position = 0
        self._latest_mission: str | None = None
        self._constraints: list[str] = []
        self._superseded: list[str] = []
        self._once: list[str] = []
        self._stop_message: str | None = None
        self._started = False
        self._console_process: subprocess.Popen[object] | None = None
        self._stdin_fallback = False

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.touch(exist_ok=True)
        self._started = True
        if self.open_console and self._start_separate_console():
            self._log_instructions(separate_console=True)
            return
        if not self.open_console:
            if self.verbose:
                print(f"[LiveInput] command file enabled: {self.queue_path}")
            return
        if not sys.stdin or not sys.stdin.isatty():
            self.enabled = False
            return
        self._stdin_fallback = True
        self._log_instructions(separate_console=False)
        thread = threading.Thread(target=self._read_stdin, daemon=True)
        thread.start()

    def snapshot(self) -> dict[str, object]:
        if not self.enabled:
            return {}
        self._drain_command_file()
        self._drain_queue()
        once = list(self._once)
        self._once.clear()
        persistent = []
        if self._latest_mission:
            persistent.append(self._latest_mission)
        persistent.extend(self._constraints[-4:])
        return {
            "latest": self._latest_mission,
            "constraints": list(self._constraints[-4:]),
            "superseded": list(self._superseded[-5:]),
            "persistent": persistent,
            "once": once,
            "stop": self._stop_message is not None,
            "stop_message": self._stop_message,
        }

    def _read_stdin(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            text = recover_mojibake(line.strip())
            if text:
                self._queue.put(text)

    def _start_separate_console(self) -> bool:
        if os.name != "nt":
            return False
        try:
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE")
            self._console_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mini_pilot.live_input_console",
                    str(self.queue_path),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                creationflags=creationflags,
                close_fds=False,
            )
        except Exception as exc:
            if self.verbose:
                print(f"[LiveInput] failed to open separate console; fallback to stdin: {exc}")
            return False
        return True

    def _drain_command_file(self) -> None:
        if not self.queue_path.exists():
            return
        try:
            with self.queue_path.open("r", encoding="utf-8") as file:
                file.seek(self._queue_position)
                lines = file.readlines()
                self._queue_position = file.tell()
        except OSError as exc:
            if self.verbose:
                print(f"[LiveInput] ignored command file read error: {exc}")
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                self._handle_command(recover_mojibake(text.strip()))

    def _drain_queue(self) -> None:
        while True:
            try:
                text = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_command(text)

    def _handle_command(self, text: str) -> None:
        lowered = text.lower()
        if lowered == "/clear":
            self._latest_mission = None
            self._constraints.clear()
            self._superseded.clear()
            self._once.clear()
            self._append_log("CLEAR", text)
            if self.verbose:
                print("[LiveInput] cleared live corrections")
            return
        if lowered.startswith("/stop"):
            message = text[5:].strip() or "Stopped by live user command."
            self._stop_message = message
            self._append_log("STOP", text)
            if self.verbose:
                print(f"[LiveInput] stop requested: {message}")
            return
        if lowered.startswith("/send "):
            correction = text[6:].strip()
            if correction:
                self._set_latest_mission(correction)
                self._append_log("MISSION", correction)
                if self.verbose:
                    print(f"[LiveInput] latest live mission set: {correction}")
            return
        if lowered.startswith("/also "):
            constraint = text[6:].strip()
            if constraint:
                self._constraints.append(constraint)
                self._append_log("CONSTRAINT", constraint)
                if self.verbose:
                    print(f"[LiveInput] compatible live constraint added: {constraint}")
            return
        if lowered.startswith("/keep "):
            constraint = text[6:].strip()
            if constraint:
                self._constraints.append(constraint)
                self._append_log("CONSTRAINT", constraint)
                if self.verbose:
                    print(f"[LiveInput] compatible live constraint added: {constraint}")
            return
        if lowered.startswith("/once "):
            correction = text[6:].strip()
            if correction:
                self._once.append(correction)
                self._append_log("ONCE", correction)
                if self.verbose:
                    print(f"[LiveInput] one-shot correction queued: {correction}")
            return

        correction = text.strip()
        if correction:
            self._set_latest_mission(correction)
            self._append_log("MISSION", correction)
            if self.verbose:
                print(f"[LiveInput] latest live mission set: {correction}")
            return

        self._append_log("IGNORED", text)
        if self.verbose:
            print(f"[LiveInput] ignored input without command prefix: {text}")

    def _set_latest_mission(self, mission: str) -> None:
        if self._latest_mission:
            self._superseded.append(self._latest_mission)
        self._latest_mission = mission

    def _append_log(self, kind: str, text: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(f"{timestamp}\t{kind}\t{text}\n")

    def _log_instructions(self, *, separate_console: bool) -> None:
        if not self.verbose:
            return
        if separate_console:
            print(
                "[LiveInput] separate input console opened. Plain text or /send "
                "replaces the latest live mission. Use /also or /keep to add "
                "compatible constraints. Also supports /once, /clear, and /stop."
            )
        else:
            print(
                "[LiveInput] stdin fallback enabled. Plain text or /send replaces "
                "the latest live mission. Use /also or /keep to add compatible "
                "constraints. Also supports /once, /clear, and /stop."
            )


def _build_model_config(
    args: argparse.Namespace,
    executor_settings: RoleSettings,
    active_profile: str | None,
    profiles: dict[str, RoleSettings],
) -> ModelConfig:
    env_config = ModelConfig.from_env()
    profile_name = args.profile or active_profile
    profile = profiles.get(profile_name or "") if profile_name else None
    role = profile or executor_settings
    return ModelConfig(
        base_url=(
            args.base_url
            or role.base_url
            or env_config.base_url
        ),
        model_name=args.model or role.model or env_config.model_name,
        api_key=args.api_key or role.api_key or env_config.api_key,
        temperature=(
            args.temperature
            if args.temperature is not None
            else role.temperature
            if role.temperature is not None
            else env_config.temperature
        ),
        max_tokens=(
            args.max_tokens
            if args.max_tokens is not None
            else role.max_tokens
            if role.max_tokens is not None
            else env_config.max_tokens
        ),
        image_max_side=(
            args.image_max_side
            if args.image_max_side is not None
            else role.image_max_side
            if role.image_max_side is not None
            else env_config.image_max_side
        ),
        extra_body=role.extra_body,
    )


def _build_operation_style_text(style: dict[str, object]) -> str | None:
    """Render operation_style config as prompt text while preserving priority."""
    if not style or style.get("enabled") is False:
        return None

    lines = [
        "These style preferences affect how to operate only when they do not conflict with the immutable original user goal, explicit user constraints, runtime gates, or safety boundaries.",
    ]

    persona = style.get("persona")
    if isinstance(persona, str) and persona.strip():
        lines.append(f"Persona: {persona.strip()}")

    _append_style_list(lines, "Principles", style.get("principles"))
    _append_style_list(lines, "Priority rules", style.get("priority_rules"))
    _append_style_list(lines, "Safety boundaries", style.get("safety_boundaries"))

    freeform = style.get("freeform")
    if isinstance(freeform, str) and freeform.strip():
        lines.append("Additional style notes:")
        lines.append(freeform.strip())

    return "\n".join(lines)


def _append_style_list(lines: list[str], title: str, value: object) -> None:
    if not isinstance(value, list):
        return
    items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if not items:
        return
    lines.append(f"{title}:")
    for item in items:
        lines.append(f"- {item}")


def _maybe_start_capture(
    args: argparse.Namespace,
    capture_settings: dict[str, object],
    goal: str,
    run_dir: Path,
    model_config: ModelConfig,
) -> UnicaptureSession | None:
    capture_backend = _capture_backend(args.capture, capture_settings)
    if capture_backend in {"", "none", "off", "false", "0"}:
        return None
    if capture_backend != "unicapture":
        raise CaptureError("Only --capture unicapture or --capture none is supported.")

    unicapture_dir = args.unicapture_dir or _optional_str(
        capture_settings.get("unicapture_dir")
    )
    inferred = infer_unicapture_selection(goal, unicapture_dir)
    custom_package = (args.custom_app_package or "").strip()
    if custom_package:
        app_name = (
            args.capture_app
            or _extract_app_name_from_goal(goal)
            or "custom_app"
        )
        scene = args.capture_scene or args.custom_app_scene or "live"
    else:
        app_name = args.capture_app or str(
            capture_settings.get("app") or inferred.get("app") or ""
        )
        scene = args.capture_scene or str(
            capture_settings.get("scene") or inferred.get("scene") or ""
        )
    if not app_name or not scene:
        raise CaptureError(
            "Unicapture needs --capture-app and --capture-scene, or an inferable goal."
        )

    validation = (
        {}
        if custom_package
        else validate_unicapture_selection(app_name, scene, unicapture_dir) or {}
    )
    target_business_duration = (
        args.run_duration
        if args.run_duration is not None
        else _optional_int(capture_settings.get("business_duration"))
        if capture_settings.get("business_duration") is not None
        else _optional_int(capture_settings.get("duration"))
        if capture_settings.get("duration") is not None
        else _optional_int(inferred.get("duration"))
    )
    business_start_timeout = (
        args.business_start_timeout
        if args.business_start_timeout is not None
        else _optional_int(capture_settings.get("business_start_timeout"))
        if capture_settings.get("business_start_timeout") is not None
        else 100
    )
    activation_fallback_delay = (
        args.activate_fallback_delay
        if args.activate_fallback_delay is not None
        else _optional_int(capture_settings.get("activation_fallback_delay"))
        if capture_settings.get("activation_fallback_delay") is not None
        else 30
    )
    duration = (
        args.capture_duration
        if args.capture_duration is not None
        else target_business_duration + business_start_timeout
        if target_business_duration is not None
        else None
    )
    pcap_package = (
        args.capture_pcap_app_package
        or custom_package
        or str(capture_settings.get("pcap_app_package") or "")
        or (get_package(app_name) if args.capture_app else "")
        or str(inferred.get("package") or "")
        or get_package(app_name)
    )

    _print_header("Unicapture")
    print(f"Capture app: {app_name}")
    print(f"Capture scene: {scene}")
    if target_business_duration is not None:
        print(
            f"Business duration: {target_business_duration}s "
            f"(capture ceiling: {duration}s)"
        )
    print(f"Capture output: {run_dir / 'capture'}")

    session = UnicaptureSession(
        UnicaptureConfig(
            app_name=app_name,
            scene=scene,
            device_id=args.device_id,
            output_dir=run_dir / "capture",
            duration=duration,
            target_business_duration=target_business_duration,
            business_start_timeout=business_start_timeout,
            activation_fallback_delay=activation_fallback_delay,
            resolution=args.capture_resolution
            or validation.get("default_resolution")
            or _optional_str(capture_settings.get("resolution")),
            location=args.capture_location
            or _optional_str(capture_settings.get("location"))
            or "default",
            capture_mode=args.capture_mode
            or _optional_str(capture_settings.get("capture_mode"))
            or _optional_str(capture_settings.get("mode"))
            or "auto",
            pcap_mode=args.capture_pcap_mode
            or _optional_str(capture_settings.get("pcap_mode"))
            or "standard",
            pcap_app_package=pcap_package,
            pcapdroid_api_key=_resolve_pcapdroid_api_key(
                args.capture_pcapdroid_api_key,
                capture_settings,
                args.device_id,
            ),
            pcapdroid_preflight=(
                not args.capture_skip_pcapdroid_preflight
                and bool(capture_settings.get("pcapdroid_preflight", True))
            ),
            enable_pcap=not args.capture_no_pcap
            and bool(capture_settings.get("enable_pcap", True)),
            enable_qoe=not args.capture_disable_qoe
            and bool(capture_settings.get("enable_qoe", True)),
            custom_app_type=args.custom_app_type,
            custom_app_scene=scene if custom_package else args.custom_app_scene,
            enable_postprocess=not args.capture_disable_postprocess
            and bool(capture_settings.get("enable_postprocess", True)),
            enable_flow_labeling=not args.capture_disable_flow_labeling
            and bool(capture_settings.get("enable_flow_labeling", True)),
            postprocess_enable_vlm=not args.capture_postprocess_disable_vlm
            and bool(capture_settings.get("postprocess_enable_vlm", True)),
            postprocess_vlm_base_url=(
                _optional_str(capture_settings.get("postprocess_vlm_base_url"))
                or model_config.base_url
            ),
            postprocess_vlm_api_key=(
                _optional_str(capture_settings.get("postprocess_vlm_api_key"))
                or (
                    model_config.api_key
                    if model_config.api_key.strip() not in {"", "1111"}
                    else None
                )
            ),
            postprocess_vlm_model=(
                _optional_str(capture_settings.get("postprocess_vlm_model"))
                or model_config.model_name
            ),
            postprocess_vlm_every=_optional_int(
                capture_settings.get("postprocess_vlm_every")
            ) or 5,
            postprocess_vlm_max_calls=_optional_int(
                capture_settings.get("postprocess_vlm_max_calls")
            ) or 20,
            postprocess_ocr_every=(
                args.capture_postprocess_ocr_every
                if getattr(args, "capture_postprocess_ocr_every", None) is not None
                else _optional_int(capture_settings.get("postprocess_ocr_every"))
                or 1
            ),
            unicapture_dir=unicapture_dir,
            python_executable=args.unicapture_python
            or _optional_str(capture_settings.get("python")),
        )
    )
    session.start()
    return session


def _resolve_pcapdroid_api_key(
    cli_key: str | None,
    capture_settings: dict[str, object],
    device_id: str | None,
) -> str | None:
    """Resolve CLI, per-device, then legacy PCAPdroid credentials."""
    if cli_key and cli_key.strip():
        return cli_key.strip()
    api_keys = capture_settings.get("pcapdroid_api_keys")
    if isinstance(api_keys, dict):
        key = api_keys.get(device_id) if device_id else None
        if isinstance(key, str) and key.strip():
            return key.strip()
        # Do not borrow another device's key. The capture preflight will
        # fail closed with its normal missing-key message.
        if api_keys:
            return None
    return _optional_str(capture_settings.get("pcapdroid_api_key"))


def _capture_backend(
    cli_capture: str | None,
    capture_settings: dict[str, object],
) -> str:
    """Resolve whether sidecar capture should run."""
    if cli_capture is not None:
        return cli_capture.strip().lower()
    enabled = bool(capture_settings.get("enabled", False))
    if not enabled:
        return "none"
    backend = capture_settings.get("backend", capture_settings.get("type"))
    if isinstance(backend, str) and backend.strip():
        return backend.strip().lower()
    return "unicapture"


def _create_run_dir(base_dir: Path, goal: str) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    suffix = _goal_suffix(goal)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for counter in range(1, 10_000):
        ending = "" if counter == 1 else f"-{counter}"
        run_dir = base_dir / f"{timestamp}-{suffix}{ending}"
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        return run_dir
    raise RuntimeError("Could not allocate a unique run directory.")


def _goal_suffix(goal: str) -> str:
    for token in goal.split():
        clean = "".join(ch for ch in token if ch.isascii() and ch.isalnum())
        if clean:
            return clean[:16]
    digest = hashlib.sha1(goal.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"task-{digest}"


def _write_execution_summary(run_dir: Path, success: bool, message: str) -> None:
    output = {
        "success": success,
        "message": message,
        "mode": "single_model",
    }
    execution_dir = run_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    (execution_dir / "summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _list_devices(device_id: str | None) -> int:
    try:
        for device in list_devices():
            marker = " *" if device.device_id == device_id else ""
            print(f"{device.device_id}\t{device.status}{marker}")
    except DeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 1
    return 0


def _check_device(device_id: str | None) -> int:
    try:
        selected = require_device(device_id)
        if selected is None:
            selected = next(
                device.device_id
                for device in list_devices()
                if device.status == "device"
            )
    except DeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 1
    print(f"Device ready: {selected}")
    return 0


def _print_current_app(device_id: str | None) -> int:
    try:
        print(get_current_app(device_id))
    except DeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 1
    return 0


def _list_apps(unicapture_dir: str | None = None) -> int:
    try:
        catalog = get_unicapture_catalog(unicapture_dir)
    except CaptureError as exc:
        print(f"Capture config error: {exc}", file=sys.stderr)
        return 2

    for name in list_supported_apps():
        scenes = catalog.get(name, [])
        if scenes:
            scene_names = ", ".join(str(scene["name"]) for scene in scenes)
            print(f"{name}: {scene_names}")
        else:
            print(name)
    print("\nUse --list-scenes APP for descriptions and default durations.")
    return 0


def _list_scenes(app_name: str, unicapture_dir: str | None = None) -> int:
    try:
        catalog = get_unicapture_catalog(unicapture_dir)
    except CaptureError as exc:
        print(f"Capture config error: {exc}", file=sys.stderr)
        return 2

    scenes = catalog.get(app_name)
    if scenes is None:
        print(
            f"Unknown capture app '{app_name}'. Use --list-apps to view valid names.",
            file=sys.stderr,
        )
        return 2

    print(f"{app_name} capture scenes:")
    for scene in scenes:
        print(
            f"  - {scene['name']}: {scene['description']} "
            f"(default {scene['duration']}s)"
        )
    return 0


def _print_header(title: str) -> None:
    print()
    print("=" * 50)
    print(title)
    print("=" * 50)


if __name__ == "__main__":
    raise SystemExit(main())
