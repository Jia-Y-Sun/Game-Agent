"""Capture integration for running Unicapture beside MiniPilot."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import importlib.util
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from mini_pilot.device import DeviceError, run_adb
from mini_pilot.text_repair import recover_mojibake


class CaptureError(RuntimeError):
    """Raised when a capture session cannot be started or stopped."""


PCAPDROID_PACKAGES = (
    "com.emanuelef.remote_capture.debug",
    "com.emanuelef.remote_capture",
)
PCAPDROID_API_KEY_VALIDATION_RECEIVER = (
    "com.emanuelef.remote_capture.ApiKeyValidationReceiver"
)
PCAPDROID_API_KEY_VALIDATION_ACTION = (
    "com.emanuelef.remote_capture.action.VALIDATE_API_KEY"
)
PCAPDROID_API_KEY_VALID_RESULT = 100
PCAPDROID_API_KEY_VALID_TOKEN = "PCAPDROID_API_KEY_VALID"
_PCAPDROID_API_KEY_PLACEHOLDERS = {
    "EMPTY",
    "YOUR_API_KEY",
    "CHANGE_ME",
}


@dataclass(frozen=True)
class UnicaptureConfig:
    """Configuration for a Unicapture sidecar process."""

    app_name: str
    scene: str
    device_id: str | None = None
    output_dir: str | Path = "capture"
    duration: int | None = None
    target_business_duration: int | None = None
    business_start_timeout: int = 100
    activation_fallback_delay: int = 30
    resolution: str | None = None
    location: str = "default"
    capture_mode: str = "auto"
    pcap_mode: str = "standard"
    pcap_app_package: str | None = None
    pcapdroid_api_key: str | None = None
    pcapdroid_preflight: bool = True
    enable_pcap: bool = True
    enable_qoe: bool = True
    custom_app_type: str | None = None
    custom_app_scene: str | None = None
    enable_postprocess: bool = True
    enable_flow_labeling: bool = True
    postprocess_enable_vlm: bool = True
    postprocess_vlm_base_url: str | None = None
    postprocess_vlm_api_key: str | None = None
    postprocess_vlm_model: str | None = None
    postprocess_vlm_every: int = 5
    postprocess_vlm_max_calls: int = 20
    postprocess_ocr_every: int = 1
    unicapture_dir: str | Path | None = None
    python_executable: str | Path | None = None
    start_timeout: float = 120.0
    stop_timeout: float = 3600.0


class UnicaptureSession:
    """Start and stop Unicapture in external-control mode."""

    def __init__(self, config: UnicaptureConfig):
        self.config = config
        self.unicapture_dir = (
            Path(config.unicapture_dir)
            if config.unicapture_dir
            else _default_unicapture_dir()
        )
        self.python_executable = (
            Path(config.python_executable)
            if config.python_executable
            else _default_unicapture_python(self.unicapture_dir)
        )
        self.output_dir = Path(config.output_dir).resolve()
        self.stop_file = self.output_dir / "unicapture.stop"
        self.ready_file = self.output_dir / "unicapture.ready"
        self.process: subprocess.Popen[str] | None = None
        self.output_log_handle = None
        self.output_thread: threading.Thread | None = None
        self._last_status_message: str | None = None
        self._last_raw_output: str | None = None
        self._latest_loop_output: str | None = None
        self._output_lock = threading.Lock()
        self._last_heartbeat_at = 0.0
        self._last_raw_output_at = 0.0
        self._capture_started_at = 0.0
        self._raw_output_lines = 0

    @property
    def capture_started_at(self) -> float | None:
        """Monotonic timestamp for when the capture sidecar was started."""
        if self._capture_started_at <= 0:
            return None
        return self._capture_started_at

    @property
    def target_app_package(self) -> str | None:
        """Package selected for the captured target app."""
        package = (self.config.pcap_app_package or "").strip()
        return package or None

    @property
    def business_timing_path(self) -> Path:
        """Shared runtime marker consumed by post-processing."""
        return self.output_dir / "business_timing.json"

    def logcat_activation_detected(self) -> bool:
        """Return whether the live sample log contains a target activation event."""
        package = self.target_app_package
        if not package:
            return False
        markers = (
            "Displayed ",
            "START u",
            "wm_on_resume_called",
            "ResumedActivity",
            "setResumedActivity",
            "mCurrentFocus",
        )
        for log_path in sorted(self.output_dir.glob("*/*.log"), reverse=True):
            try:
                with log_path.open("rb") as handle:
                    size = handle.seek(0, 2)
                    handle.seek(max(0, size - 4 * 1024 * 1024))
                    text = handle.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            if package in text and any(marker in text for marker in markers):
                return True
        return False

    def start(self) -> None:
        """Start the sidecar collector process."""
        if self.process is not None:
            raise CaptureError("Unicapture session is already running.")
        if not self.unicapture_dir.exists():
            raise CaptureError(
                f"Unicapture directory not found: {self.unicapture_dir}"
            )
        if not (self.unicapture_dir / "app_collector.py").exists():
            raise CaptureError(
                f"Unicapture app_collector.py not found in: {self.unicapture_dir}"
            )
        if not self.python_executable.exists():
            raise CaptureError(f"Python executable not found: {self.python_executable}")

        if (
            self.config.enable_pcap
            and self.config.pcapdroid_preflight
            and self.config.capture_mode.strip().lower() != "root"
        ):
            package = validate_pcapdroid_api_key(
                self.config.pcapdroid_api_key,
                device_id=self.config.device_id,
            )
            print(f"PCAPdroid API Key validated ({package}).")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.stop_file.exists():
            self.stop_file.unlink()
        if self.ready_file.exists():
            self.ready_file.unlink()

        cmd = self._build_command()
        self._write_command_file(cmd)
        self.output_log_handle = (self.output_dir / "unicapture_stdout.log").open("ab")
        self._capture_started_at = time.monotonic()
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        if self.config.postprocess_enable_vlm:
            env.setdefault("QOE_USE_VLM", "1")
            env.setdefault("QOE_VLM_PROVIDER", "openai")
            if self.config.postprocess_vlm_base_url:
                env.setdefault("QOE_VLM_BASE_URL", self.config.postprocess_vlm_base_url)
            if self.config.postprocess_vlm_api_key:
                env.setdefault("QOE_VLM_API_KEY", self.config.postprocess_vlm_api_key)
            if self.config.postprocess_vlm_model:
                env.setdefault("QOE_VLM_MODEL", self.config.postprocess_vlm_model)
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.unicapture_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.output_thread = threading.Thread(
            target=self._forward_output,
            args=(self.process,),
            daemon=True,
        )
        self.output_thread.start()
        try:
            self._wait_until_ready()
        except Exception:
            self._cleanup_output_stream()
            raise

    def stop(self) -> None:
        """Signal the collector to stop and wait for artifacts to be collected."""
        if self.process is None:
            return

        self.stop_file.write_text(
            f"stop requested at {datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
        try:
            self.process.wait(timeout=self.config.stop_timeout)
        except subprocess.TimeoutExpired as exc:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            raise CaptureError("Unicapture did not stop before timeout.") from exc
        finally:
            self.process = None
            self._cleanup_output_stream()

    def _build_command(self) -> list[str]:
        cfg = self.config
        cmd = [
            str(self.python_executable),
            "app_collector.py",
            "--app-name",
            cfg.app_name,
            "--scene",
            cfg.scene,
            "--output",
            str(self.output_dir),
            "--location",
            cfg.location,
            "--capture-mode",
            cfg.capture_mode,
            "--pcap-mode",
            cfg.pcap_mode,
            "--external-control",
            "--stop-file",
            str(self.stop_file),
            "--ready-file",
            str(self.ready_file),
        ]
        if cfg.device_id:
            cmd.extend(["--device", cfg.device_id])
        if cfg.duration is not None:
            cmd.extend(["--duration", str(cfg.duration)])
        if cfg.target_business_duration is not None:
            cmd.extend(
                ["--target-business-duration", str(cfg.target_business_duration)]
            )
        if cfg.resolution:
            cmd.extend(["--resolution", cfg.resolution])
        if cfg.pcap_app_package:
            cmd.extend(["--pcap-app-package", cfg.pcap_app_package])
        if cfg.pcapdroid_api_key:
            cmd.extend(["--pcapdroid-api-key", cfg.pcapdroid_api_key])
        if cfg.custom_app_type:
            cmd.extend(["--custom-app-type", cfg.custom_app_type])
        if cfg.custom_app_scene:
            cmd.extend(["--custom-app-scene", cfg.custom_app_scene])
        if not cfg.enable_pcap:
            cmd.append("--no-pcap")
        if not cfg.enable_qoe:
            cmd.append("--disable-qoe")
        if not cfg.enable_postprocess:
            cmd.append("--disable-postprocess")
        if not cfg.enable_flow_labeling:
            cmd.append("--disable-flow-labeling")
        if not cfg.postprocess_enable_vlm:
            cmd.append("--postprocess-disable-vlm")
        cmd.extend(["--postprocess-vlm-every", str(cfg.postprocess_vlm_every)])
        cmd.extend(["--postprocess-vlm-max-calls", str(cfg.postprocess_vlm_max_calls)])
        cmd.extend(["--postprocess-ocr-every", str(max(1, cfg.postprocess_ocr_every))])
        return cmd

    def _write_command_file(self, cmd: list[str]) -> None:
        command_file = self.output_dir / "unicapture_command.txt"
        redacted = list(cmd)
        for index, item in enumerate(redacted[:-1]):
            if item in {"--pcapdroid-api-key"}:
                redacted[index + 1] = _redact_secret(redacted[index + 1])
        command_file.write_text(" ".join(redacted) + "\n", encoding="utf-8")

    def _forward_output(self, process: subprocess.Popen[str]) -> None:
        """Write all Unicapture output to file and forward selected lines."""
        if process.stdout is None or self.output_log_handle is None:
            return
        for line in process.stdout:
            safe_line = _redact_value_in_text(line, self.config.pcapdroid_api_key)
            encoded = safe_line.encode("utf-8", errors="replace")
            self.output_log_handle.write(encoded)
            self.output_log_handle.flush()
            text = safe_line.rstrip()
            if not text:
                continue
            status = self._status_message_from_output(text)
            if status:
                self._print_status(status)
            else:
                if not self._print_sampled_output(text):
                    self._maybe_print_heartbeat()

    def _cleanup_output_stream(self) -> None:
        """Join output forwarding and close the Unicapture stdout log."""
        if self.output_thread is not None:
            self.output_thread.join(timeout=5)
            self.output_thread = None
        if self.output_log_handle is not None:
            self.output_log_handle.close()
            self.output_log_handle = None

    def _status_message_from_output(self, line: str) -> str | None:
        """Map noisy sidecar output to stable console status messages."""
        lowered = line.lower()
        if "cv::finddecoder" in lowered or "imread_(" in lowered:
            return None

        if any(marker in lowered for marker in ("error", "failed", "exception")):
            return f"warning/error reported; see {self.output_dir / 'unicapture_stdout.log'}"
        if "warning" in lowered or "warn:" in lowered:
            return f"warning reported; see {self.output_dir / 'unicapture_stdout.log'}"
        if "pcapdroid" in lowered and any(
            marker in lowered for marker in ("start", "started", "vpn", "capture")
        ):
            return "PCAPdroid capture started"
        if "pcapdroid" in lowered and any(
            marker in lowered for marker in ("stop", "stopped")
        ):
            return "PCAPdroid capture stopping"
        if "logcat" in lowered and any(
            marker in lowered for marker in ("start", "started", "stream")
        ):
            return "logcat stream started"
        if "screenrecord" in lowered and any(
            marker in lowered for marker in ("start", "started")
        ):
            return "screen recording started"
        if "screenrecord" in lowered and any(
            marker in lowered for marker in ("stop", "stopped", "final")
        ):
            return "screen recording finalizing"
        if any(marker in line for marker in ("错误", "失败", "警告", "异常")):
            return f"warning/error reported; see {self.output_dir / 'unicapture_stdout.log'}"
        if "PCAPdroid" in line and any(
            marker in line for marker in ("启动", "开始", "抓包")
        ):
            return "PCAPdroid capture started"
        if "PCAPdroid" in line and any(marker in line for marker in ("停止", "结束")):
            return "PCAPdroid capture stopping"
        if "logcat" in lowered and any(marker in line for marker in ("启动", "开始")):
            return "logcat stream started"
        if any(marker in line for marker in ("录屏启动", "开始录屏")):
            return "screen recording started"
        if any(marker in line for marker in ("录屏停止", "录屏完成", "保存录屏")):
            return "screen recording finalizing"
        if any(marker in line for marker in ("就绪", "ready")):
            return "capture sidecar ready"
        if any(marker in line for marker in ("停止", "结束")):
            return "capture stop requested"
        if any(marker in line for marker in ("启动", "开始", "业务采样", "抓包")):
            return "capture started/running"
        if "ready" in lowered:
            return "capture sidecar ready"
        if "stop" in lowered:
            return "capture stop requested"

        # Some Windows consoles surface UTF-8 Chinese output as mojibake.
        # Do not print the raw line; classify common lifecycle messages instead.
        mojibake_start = ("鍚", "寮", "宸插", "鐠", "閸")
        mojibake_stop = ("鍋", "宸插仠", "瀹屾", "缁撴")
        mojibake_error = ("閿", "澶辫", "璀", "寮傚")
        if any(marker in line for marker in mojibake_error):
            return f"warning/error reported; see {self.output_dir / 'unicapture_stdout.log'}"
        if any(marker in line for marker in mojibake_stop):
            return "capture stopping/completing"
        if any(marker in line for marker in mojibake_start):
            return "capture started/running"

        return None

    def _print_status(self, message: str) -> None:
        if message == self._last_status_message:
            return
        self._last_status_message = message
        self._last_heartbeat_at = time.monotonic()
        print(f"[Unicapture] {message}")

    def _maybe_print_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_at < 10.0:
            return
        elapsed = max(0, int(now - self._capture_started_at))
        self._last_heartbeat_at = now
        print(
            f"[Unicapture] running (elapsed={elapsed}s, "
            f"log={self.output_dir / 'unicapture_stdout.log'})"
        )

    def _print_sampled_output(self, line: str) -> bool:
        if self._is_noisy_output_line(line):
            return False

        repaired = _compact_console_line(recover_mojibake(line), limit=220)
        if not repaired or repaired == self._last_raw_output:
            return False

        self._raw_output_lines += 1
        now = time.monotonic()
        self._last_raw_output = repaired
        self._last_raw_output_at = now
        self._last_heartbeat_at = now
        with self._output_lock:
            self._latest_loop_output = repaired
        return True

    def latest_loop_output(self) -> str | None:
        """Return the latest non-noisy sidecar output for loop-aligned logging."""
        with self._output_lock:
            return self._latest_loop_output

    def _is_noisy_output_line(self, line: str) -> bool:
        lowered = line.lower()
        noisy_markers = (
            "cv::finddecoder",
            "imread_(",
            "deprecated",
            "matplotlib",
            "font manager",
        )
        return any(marker in lowered for marker in noisy_markers)

    def _should_forward_output_line(self, line: str) -> bool:
        """Forward important Unicapture lines without flooding the console."""
        if "cv::findDecoder" in line or "imread_(" in line:
            return False

        important_markers = (
            "error",
            "failed",
            "warning",
            "exception",
            "PCAPdroid",
            "logcat",
            "screenrecord",
            "ready",
            "stop",
            "错误",
            "失败",
            "警告",
            "异常",
            "启动",
            "已启动",
            "开始",
            "停止",
            "已停止",
            "完成",
            "就绪",
            "閿欒",
            "澶辫触",
            "璀﹀憡",
            "寮€濮",
            "鍚姩",
            "宸插惎鍔",
            "鍋滄",
            "瀹屾垚",
        )
        lowered = line.lower()
        if any(marker.lower() in lowered for marker in important_markers):
            return True

        if not line.startswith("["):
            return False

        self._forwarded_output_lines += 1
        if self._forwarded_output_lines <= 12:
            return True

        now = time.monotonic()
        if now - self._last_forwarded_output_at >= 5.0:
            self._last_forwarded_output_at = now
            return True
        return False

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.config.start_timeout
        while time.monotonic() < deadline:
            if self.ready_file.exists():
                return
            if self.process and self.process.poll() is not None:
                raise CaptureError(
                    f"Unicapture exited during startup with code {self.process.returncode}."
                )
            time.sleep(0.5)

        raise CaptureError(
            "Unicapture did not become ready before timeout. "
            "If PCAPdroid is showing an authorization dialog, approve it on the phone "
            "or pass --capture-pcapdroid-api-key."
        )


def _default_unicapture_dir() -> Path:
    """Return the v4-local Unicapture directory: <project_root>/unicapture."""
    return Path(__file__).resolve().parents[1] / "unicapture"


def _default_unicapture_python(unicapture_dir: Path) -> Path:
    candidates = [
        unicapture_dir / ".venv313" / "Scripts" / "python.exe",
        unicapture_dir / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists() and _python_executable_works(candidate):
            return candidate
    return Path(sys.executable)


def validate_pcapdroid_api_key(
    api_key: str | None,
    *,
    device_id: str | None = None,
) -> str:
    """Validate the configured PCAPdroid API Key without starting a capture."""
    key = (api_key or "").strip()
    if not key or key.upper() in _PCAPDROID_API_KEY_PLACEHOLDERS:
        raise CaptureError(
            "PCAPdroid API Key is not configured. Open PCAPdroid > Settings > "
            "Control Permissions, generate an API Key, and set "
            "capture.pcapdroid_api_key in mini_pilot.config.json."
        )

    try:
        packages_result = run_adb(
            ["shell", "pm", "list", "packages"],
            device_id=device_id,
            timeout=10,
            check=True,
        )
    except (DeviceError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(
            "Could not query installed apps before validating the PCAPdroid API Key. "
            "Check the ADB connection and selected device."
        ) from exc

    installed = {
        line.removeprefix("package:").strip()
        for line in packages_result.stdout.splitlines()
        if line.strip().startswith("package:")
    }
    package = next((item for item in PCAPDROID_PACKAGES if item in installed), None)
    if package is None:
        raise CaptureError(
            "PCAPdroid is not installed. Install the customized PCAPdroid APK "
            "before starting a capture task."
        )

    component = f"{package}/{PCAPDROID_API_KEY_VALIDATION_RECEIVER}"
    try:
        result = run_adb(
            [
                "shell",
                "am",
                "broadcast",
                "--user",
                "current",
                "--receiver-foreground",
                "-a",
                PCAPDROID_API_KEY_VALIDATION_ACTION,
                "-n",
                component,
                "--es",
                "api_key",
                key,
            ],
            device_id=device_id,
            timeout=10,
            check=False,
        )
    except (DeviceError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(
            "PCAPdroid API Key validation could not be sent. Check the ADB "
            "connection and selected device."
        ) from exc

    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        raise CaptureError(
            "PCAPdroid API Key validation request failed. Check the ADB connection "
            "and install the latest customized PCAPdroid APK."
        )

    result_match = re.search(r"Broadcast completed:\s*result=(-?\d+)", output)
    result_code = int(result_match.group(1)) if result_match else None
    token_match = re.search(r'\bdata=(?:"([^"]*)"|(\S+))', output)
    data_token = (
        (token_match.group(1) or token_match.group(2)).rstrip(",")
        if token_match
        else None
    )
    protocol_match = re.search(r"\bprotocol_version=(\d+)\b", output)
    protocol_version = int(protocol_match.group(1)) if protocol_match else None

    if protocol_version is not None and protocol_version != 1:
        raise CaptureError(
            "PCAPdroid API Key validation protocol is unsupported. Upgrade "
            "MiniPilot and the customized PCAPdroid APK."
        )

    if (
        result_code == PCAPDROID_API_KEY_VALID_RESULT
        and data_token == PCAPDROID_API_KEY_VALID_TOKEN
    ):
        return package

    if data_token == "PCAPDROID_API_KEY_INVALID" or result_code == 101:
        raise CaptureError(
            "PCAPdroid API Key is incorrect. Copy the current Key from PCAPdroid > "
            "Settings > Control Permissions and update mini_pilot.config.json."
        )
    if data_token == "PCAPDROID_API_KEY_NOT_CONFIGURED" or result_code == 102:
        raise CaptureError(
            "PCAPdroid has no API Key configured. Generate one in PCAPdroid > "
            "Settings > Control Permissions first."
        )
    if data_token == "PCAPDROID_API_KEY_MISSING" or result_code == 103:
        raise CaptureError("PCAPdroid API Key validation request did not contain a Key.")
    if data_token == "PCAPDROID_API_KEY_BAD_REQUEST" or result_code == 104:
        raise CaptureError(
            "PCAPdroid API Key validation request has an invalid format. Upgrade "
            "MiniPilot and the customized PCAPdroid APK."
        )

    raise CaptureError(
        "The installed PCAPdroid does not support API Key preflight validation. "
        "Install the latest customized PCAPdroid APK."
    )


def _redact_value_in_text(text: str, secret: str | None) -> str:
    """Remove a configured secret from diagnostic text."""
    value = (secret or "").strip()
    if not value:
        return text
    return text.replace(value, _redact_secret(value))


def _python_executable_works(path: Path) -> bool:
    """Return whether a copied virtualenv Python can actually start."""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _redact_secret(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _compact_console_line(text: str, *, limit: int) -> str:
    one_line = " ".join(text.strip().split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3].rstrip() + "..."


def infer_unicapture_selection(
    text: str,
    unicapture_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Infer Unicapture app/scene/duration from a user goal or task."""
    clean_text = text.strip()
    if not clean_text:
        return {}

    module = _load_unicapture_app_configs(unicapture_dir)
    app_name = _infer_capture_app(clean_text, module)
    if not app_name:
        return {}

    app_config = module.get_app_config(app_name)
    if not app_config:
        return {}

    scene = _infer_capture_scene(clean_text, app_config)
    duration = _infer_duration_seconds(clean_text)
    result: dict[str, Any] = {"app": app_name}
    if getattr(app_config, "package", None):
        result["package"] = app_config.package
    if scene:
        result["scene"] = scene
        if duration is None:
            scene_config = app_config.get_scene(scene)
            if scene_config:
                duration = scene_config.duration
    if duration is not None:
        result["duration"] = duration
    return result


def get_unicapture_catalog(
    unicapture_dir: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return capture apps and their user-selectable scenes."""
    module = _load_unicapture_app_configs(unicapture_dir)
    catalog: dict[str, list[dict[str, Any]]] = {}
    for app_name in sorted(module.list_all_apps()):
        app_config = module.get_app_config(app_name)
        if not app_config:
            continue
        catalog[app_name] = [
            {
                "name": scene.name,
                "description": scene.description,
                "duration": scene.duration,
            }
            for scene in app_config.scenes
        ]
    return catalog


def _load_unicapture_app_configs(unicapture_dir: str | Path | None = None):
    root = Path(unicapture_dir) if unicapture_dir else _default_unicapture_dir()
    configs_path = root / "app_configs.py"
    if not configs_path.exists():
        raise CaptureError(f"Unicapture app_configs.py not found in: {root}")

    spec = importlib.util.spec_from_file_location(
        "_unicapture_app_configs", configs_path
    )
    if spec is None or spec.loader is None:
        raise CaptureError(f"Could not load Unicapture app config from: {configs_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _infer_capture_app(text: str, module) -> str | None:
    unified_infer = getattr(module, "infer_app_key_from_text", None)
    if callable(unified_infer):
        app_name = unified_infer(text)
        if app_name:
            return str(app_name)

    # Compatibility fallback for an older external Unicapture directory.
    aliases = {
        "douyin": ("抖音", "douyin", "tiktok"),
        "bilibili": ("哔哩哔哩", "哔哩", "bilibili", "b站"),
        "kuaishou": ("快手", "kuaishou"),
        "xiaohongshu": ("小红书", "xiaohongshu", "rednote"),
        "mango": ("芒果tv", "芒果", "mango"),
        "iqiyi": ("爱奇艺", "iqiyi"),
        "tencent_video": ("腾讯视频", "tencent video"),
        "wechat": ("微信", "wechat"),
        "qq": ("qq",),
        "taobao": ("淘宝", "taobao"),
        "jd": ("京东", "jd"),
        "lanren_tingshu": ("懒人听书", "lanren", "lazy audio"),
        "deepseek": ("deepseek", "deep seek", "深度求索"),
    }
    lowered = text.lower()
    supported = set(module.list_all_apps())
    for app_name, names in aliases.items():
        if app_name not in supported:
            continue
        if any(alias.lower() in lowered for alias in names):
            return app_name
    return None


def _infer_capture_scene(text: str, app_config) -> str | None:
    scene_names = {scene.name for scene in app_config.scenes}
    lowered = text.lower()

    candidates: list[str] = []
    if any(keyword in text for keyword in ("直播", "live")):
        candidates.extend(["live"])
    if any(keyword in text for keyword in ("搜索", "search")):
        candidates.extend(["search"])
    if any(keyword in text for keyword in ("商城", "商品", "购物", "shop")):
        candidates.extend(["shop", "browse"])
    if any(keyword in text for keyword in ("短视频", "视频流", "推荐流")):
        candidates.extend(["short_video", "video_feed", "feed"])
    if any(keyword in text for keyword in ("视频", "观看", "播放", "看")):
        candidates.extend(["video", "short_video", "feed", "movie"])
    if any(keyword in text for keyword in ("听书", "听", "有声", "音频", "播放")):
        candidates.extend(["audio_playback", "browse", "feed"])
    if any(keyword in text for keyword in ("刷", "浏览", "逛", "browse", "feed")):
        candidates.extend(["feed", "browse", "short_video", "video_feed"])
    if any(keyword in text for keyword in ("对话", "聊天", "讨论", "chat", "conversation")):
        candidates.extend(["chat"])

    for scene_name in scene_names:
        if scene_name.lower() in lowered:
            candidates.insert(0, scene_name)

    for candidate in candidates:
        if candidate in scene_names:
            return candidate
    return app_config.scenes[0].name if app_config.scenes else None


def _infer_duration_seconds(text: str) -> int | None:
    patterns = (
        (r"(\d+)\s*小时", 3600),
        (r"(\d+)\s*分钟", 60),
        (r"(\d+)\s*分\b", 60),
        (r"(\d+)\s*秒", 1),
        (r"(\d+)\s*min", 60),
        (r"(\d+)\s*s\b", 1),
    )
    for pattern, multiplier in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)) * multiplier)

    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    match = re.search(r"([一二两三四五六七八九十])\s*分钟", text)
    if match:
        return chinese_digits[match.group(1)] * 60
    match = re.search(r"([一二两三四五六七八九十])\s*秒", text)
    if match:
        return chinese_digits[match.group(1)]
    return None


def validate_unicapture_selection(
    app_name: str,
    scene: str,
    unicapture_dir: str | Path | None = None,
) -> None:
    """Validate that Unicapture knows the requested app and scene."""
    module = _load_unicapture_app_configs(unicapture_dir)

    app_config = module.get_app_config(app_name)
    if not app_config:
        supported = ", ".join(module.list_all_apps())
        raise CaptureError(
            f"Unicapture does not support app '{app_name}'. Supported apps: {supported}"
        )

    if not app_config.get_scene(scene):
        scenes = ", ".join(item.name for item in app_config.scenes)
        raise CaptureError(
            f"Unicapture app '{app_name}' does not support scene '{scene}'. "
            f"Supported scenes: {scenes}"
        )
