"""ADB device operations for MiniPilot.

This module is the low-level device layer. It does not understand model output
or user tasks; it only sends concrete ADB commands to an Android device.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from mini_pilot.apps import get_package
from mini_pilot.timing import TIMING_CONFIG


ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADB_KEYBOARD_PACKAGE = "com.android.adbkeyboard"
ADB_KEYBOARD_APK_URL = "https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk"


@dataclass(frozen=True)
class DeviceInfo:
    """Basic information about a connected Android device."""

    device_id: str
    status: str


@dataclass(frozen=True)
class ScreenSize:
    """Physical screen size in pixels."""

    width: int
    height: int


class DeviceError(RuntimeError):
    """Raised when an ADB/device operation fails."""


def adb_binary() -> str:
    """Return the configured ADB executable path or command name."""
    configured = os.getenv("MINI_PILOT_ADB_BIN") or os.getenv("ADB_BIN")
    return configured.strip() if configured and configured.strip() else "adb"


def ensure_adb_available() -> None:
    """Raise DeviceError if the configured adb executable is unavailable."""
    binary = adb_binary()
    has_path_separator = os.path.sep in binary or (
        os.path.altsep and os.path.altsep in binary
    )
    available = (
        os.path.isfile(binary)
        if has_path_separator or os.path.isabs(binary)
        else shutil.which(binary) is not None
    )
    if not available:
        raise DeviceError(
            f"adb was not found: {binary}. Configure MINI_PILOT_ADB_BIN or PATH."
        )


def _adb_prefix(device_id: str | None = None) -> list[str]:
    """Build the adb command prefix, optionally targeting a specific device."""
    ensure_adb_available()
    binary = adb_binary()
    if device_id:
        return [binary, "-s", device_id]
    return [binary]


def run_adb(
    args: list[str],
    device_id: str | None = None,
    timeout: int = 15,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an adb command and return the completed process."""
    cmd = _adb_prefix(device_id) + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )

    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown adb error"
        raise DeviceError(f"ADB command failed: {' '.join(cmd)}\n{message}")

    return result


def run_adb_bytes(
    args: list[str],
    device_id: str | None = None,
    timeout: int = 15,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run an adb command that returns binary stdout."""
    cmd = _adb_prefix(device_id) + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
    )

    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        message = message or result.stdout.decode("utf-8", errors="replace").strip()
        message = message or "unknown adb error"
        raise DeviceError(f"ADB command failed: {' '.join(cmd)}\n{message}")

    return result


def list_devices() -> list[DeviceInfo]:
    """List devices reported by `adb devices`."""
    result = run_adb(["devices"], check=True)
    devices: list[DeviceInfo] = []

    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) >= 2:
            devices.append(DeviceInfo(device_id=parts[0], status=parts[1]))

    return devices


def require_device(device_id: str | None = None) -> str | None:
    """Validate that a usable device is connected.

    Returns the selected device id. If only one device is connected and no
    device_id was provided, adb can auto-select it, so this returns None.
    """
    devices = [device for device in list_devices() if device.status == "device"]

    if device_id:
        if any(device.device_id == device_id for device in devices):
            return device_id
        raise DeviceError(f"Device not found or not ready: {device_id}")

    if not devices:
        raise DeviceError("No ready Android device found. Check `adb devices`.")

    if len(devices) > 1:
        ids = ", ".join(device.device_id for device in devices)
        raise DeviceError(f"Multiple devices found. Please specify one: {ids}")

    return None


def get_screen_size(device_id: str | None = None) -> ScreenSize:
    """Get the device screen size using `wm size`."""
    result = run_adb(["shell", "wm", "size"], device_id=device_id)
    output = result.stdout.strip()

    # Common output: "Physical size: 1080x2400"
    for token in output.replace(":", " ").split():
        if "x" not in token:
            continue

        width_text, height_text = token.lower().split("x", 1)
        if width_text.isdigit() and height_text.isdigit():
            return ScreenSize(width=int(width_text), height=int(height_text))

    raise DeviceError(f"Could not parse screen size from: {output}")


def get_current_app(device_id: str | None = None) -> str:
    """Return the currently focused Android package name."""
    result = run_adb(["shell", "dumpsys", "window"], device_id=device_id, timeout=10)
    output = result.stdout

    markers = ("mCurrentFocus=", "mFocusedApp=", "topResumedActivity=")
    for line in output.splitlines():
        if not any(marker in line for marker in markers):
            continue

        package = _extract_package_from_window_line(line)
        if package:
            return package

    raise DeviceError("Could not detect current foreground app.")


def _extract_package_from_window_line(line: str) -> str | None:
    """Extract a package id from a dumpsys window line."""
    for token in line.replace("/", " ").split():
        if "." not in token:
            continue

        cleaned = token.strip("{}[](),")
        if cleaned.startswith("u0"):
            cleaned = cleaned[2:]

        if "." in cleaned and not cleaned.startswith("."):
            return cleaned

    return None


def launch_app(
    app_name: str,
    device_id: str | None = None,
    delay: float | None = None,
    verify_timeout: float | None = None,
    verify_interval: float | None = None,
) -> None:
    """Launch an app by friendly name or package id."""
    if delay is None:
        delay = TIMING_CONFIG.device.launch_delay
    if verify_timeout is None:
        verify_timeout = TIMING_CONFIG.device.launch_verify_timeout
    if verify_interval is None:
        verify_interval = TIMING_CONFIG.device.launch_verify_interval

    package = get_package(app_name)
    if not package:
        raise DeviceError(f"Unknown app: {app_name}. Add it to mini_pilot/apps.py.")

    run_adb(
        [
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        device_id=device_id,
        timeout=15,
    )
    time.sleep(delay)

    deadline = time.time() + max(0.0, verify_timeout)
    last_seen_package: str | None = None
    while time.time() <= deadline:
        try:
            current_package = get_current_app(device_id=device_id)
        except DeviceError:
            current_package = None

        if current_package:
            last_seen_package = current_package
            if current_package == package:
                return

        time.sleep(max(0.1, verify_interval))

    if last_seen_package is not None and last_seen_package != package:
        raise DeviceError(
            f"App launch did not reach expected foreground package. "
            f"Expected: {package}, current: {last_seen_package}"
        )


def close_app(
    app_name: str,
    *,
    device_id: str | None = None,
    method: str = "force-stop",
) -> None:
    """Close an app by friendly name without clearing its data."""
    package = get_package(app_name)
    if not package:
        raise DeviceError(f"Unknown app: {app_name}. Add it to mini_pilot/apps.py.")
    close_app_by_package(package, device_id=device_id, method=method)


def close_app_by_package(
    package: str,
    *,
    device_id: str | None = None,
    method: str = "force-stop",
) -> None:
    """Close an app package without clearing its persistent data."""
    package = package.strip()
    if not package:
        raise DeviceError("App package cannot be empty.")
    if method == "force-stop":
        args = ["shell", "am", "force-stop", package]
    elif method == "kill-background":
        args = ["shell", "am", "kill-background-process", package]
    else:
        raise DeviceError(f"Unsupported close method: {method}")
    run_adb(args, device_id=device_id, timeout=10)


def wake_screen(device_id: str | None = None) -> None:
    """Wake the device screen and try to reveal an interactive surface.

    This is intentionally conservative: KEYCODE_WAKEUP does not toggle the
    screen off when it is already awake, unlike POWER. The swipe only attempts
    to reveal the lock screen/home surface; secure locks still require user
    intervention and will remain visible for the model or caller to handle.
    """
    run_adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"], device_id=device_id, check=False)
    time.sleep(0.8)
    run_adb(["shell", "input", "keyevent", "KEYCODE_MENU"], device_id=device_id, check=False)
    time.sleep(0.3)
    try:
        size = get_screen_size(device_id=device_id)
    except DeviceError:
        return
    run_adb(
        [
            "shell",
            "input",
            "swipe",
            str(size.width // 2),
            str(int(size.height * 0.82)),
            str(size.width // 2),
            str(int(size.height * 0.28)),
            "350",
        ],
        device_id=device_id,
        check=False,
    )
    time.sleep(0.8)


def tap(x: int, y: int, device_id: str | None = None, delay: float | None = None) -> None:
    """Tap an absolute screen coordinate."""
    if delay is None:
        delay = TIMING_CONFIG.device.tap_delay
    run_adb(["shell", "input", "tap", str(x), str(y)], device_id=device_id)
    time.sleep(delay)


def double_tap(
    x: int,
    y: int,
    device_id: str | None = None,
    delay: float | None = None,
) -> None:
    """Double tap an absolute screen coordinate."""
    if delay is None:
        delay = TIMING_CONFIG.device.tap_delay
    run_adb(["shell", "input", "tap", str(x), str(y)], device_id=device_id)
    time.sleep(0.1)
    run_adb(["shell", "input", "tap", str(x), str(y)], device_id=device_id)
    time.sleep(delay)


def long_press(
    x: int,
    y: int,
    duration_ms: int = 3000,
    device_id: str | None = None,
    delay: float | None = None,
) -> None:
    """Long press an absolute screen coordinate."""
    if delay is None:
        delay = TIMING_CONFIG.device.tap_delay
    run_adb(
        [
            "shell",
            "input",
            "swipe",
            str(x),
            str(y),
            str(x),
            str(y),
            str(max(1, duration_ms)),
        ],
        device_id=device_id,
    )
    time.sleep(delay)


def swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration_ms: int = 500,
    device_id: str | None = None,
    delay: float | None = None,
) -> None:
    """Swipe between two absolute screen coordinates."""
    if delay is None:
        delay = TIMING_CONFIG.device.swipe_delay
    run_adb(
        [
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ],
        device_id=device_id,
    )
    time.sleep(delay)


def type_text(
    text: str,
    device_id: str | None = None,
    delay: float | None = None,
    auto_keyboard: bool = True,
    clear_before_input: bool = True,
) -> str:
    """Type text.

    MiniPilot uses ADB Keyboard by default because it handles Chinese, spaces,
    punctuation, and long text better than `adb shell input text`. Set
    MINI_PILOT_FORCE_ADB_KEYBOARD=0 only when fallback input is intentional.
    """
    if delay is None:
        delay = TIMING_CONFIG.action.text_input_delay

    force_adb_keyboard = os.getenv("MINI_PILOT_FORCE_ADB_KEYBOARD", "1") != "0"
    adb_keyboard_ime = find_adb_keyboard_ime(device_id) if auto_keyboard else None
    use_adb_keyboard = adb_keyboard_ime is not None
    if force_adb_keyboard and auto_keyboard and not adb_keyboard_ime:
        raise DeviceError(
            "ADB Keyboard is required for Type, but it is not installed or not visible. "
            f"Install and enable ADB Keyboard first: {ADB_KEYBOARD_APK_URL}"
        )
    if not use_adb_keyboard and _requires_adb_keyboard(text):
        raise DeviceError(
            "Text contains non-ASCII characters or spaces, but ADB Keyboard is not installed or not visible. "
            f"Install and enable ADB Keyboard first: {ADB_KEYBOARD_APK_URL}"
        )

    input_method = "adb shell input text"
    original_ime: str | None = None
    try:
        if use_adb_keyboard:
            original_ime = get_current_input_method(device_id)
            enable_adb_keyboard(adb_keyboard_ime, device_id=device_id)
            set_input_method(adb_keyboard_ime, device_id)
            time.sleep(TIMING_CONFIG.action.keyboard_switch_delay)
            if clear_before_input:
                clear_text(device_id=device_id, use_adb_keyboard=True)
                time.sleep(TIMING_CONFIG.action.text_clear_delay)
            adb_keyboard_text(text, device_id)
            input_method = f"ADBKeyboard({adb_keyboard_ime})"
        else:
            if clear_before_input:
                clear_text(device_id=device_id, use_adb_keyboard=False)
                time.sleep(TIMING_CONFIG.action.text_clear_delay)
            input_text_simple(text, device_id)
    finally:
        if original_ime:
            set_input_method(original_ime, device_id)
            time.sleep(TIMING_CONFIG.action.keyboard_restore_delay)

    time.sleep(delay)
    return input_method


def input_text_simple(text: str, device_id: str | None = None) -> None:
    """Type text using Android's built-in input command."""
    escaped = text.replace("%", "%25").replace(" ", "%s")
    run_adb(["shell", "input", "text", escaped], device_id=device_id)


def clear_text(
    device_id: str | None = None,
    use_adb_keyboard: bool | None = None,
    max_backspaces: int = 80,
) -> None:
    """Clear text in the currently focused input field."""
    if use_adb_keyboard is None:
        use_adb_keyboard = is_adb_keyboard_installed(device_id)

    if use_adb_keyboard:
        adb_keyboard_clear_text(device_id)
        return

    clear_text_simple(device_id=device_id, max_backspaces=max_backspaces)


def clear_text_simple(
    device_id: str | None = None,
    max_backspaces: int = 80,
) -> None:
    """Clear text with repeated key events when ADB Keyboard is unavailable."""
    press_key("MOVE_END", device_id=device_id, delay=0.1)
    for _ in range(max(1, max_backspaces)):
        run_adb(["shell", "input", "keyevent", "DEL"], device_id=device_id)


def adb_keyboard_clear_text(device_id: str | None = None) -> None:
    """Clear text through ADB Keyboard broadcast."""
    run_adb(
        ["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"],
        device_id=device_id,
    )


def adb_keyboard_text(text: str, device_id: str | None = None) -> None:
    """Send text through ADB Keyboard broadcast."""
    encoded_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
    run_adb(
        [
            "shell",
            "am",
            "broadcast",
            "-a",
            "ADB_INPUT_B64",
            "--es",
            "msg",
            encoded_text,
        ],
        device_id=device_id,
    )


def is_adb_keyboard_installed(device_id: str | None = None) -> bool:
    """Return whether ADB Keyboard IME is installed on the device."""
    if find_adb_keyboard_ime(device_id):
        return True

    package_result = run_adb(
        ["shell", "pm", "list", "packages", ADB_KEYBOARD_PACKAGE],
        device_id=device_id,
        check=False,
    )
    return ADB_KEYBOARD_PACKAGE in package_result.stdout


def find_adb_keyboard_ime(device_id: str | None = None) -> str | None:
    """Return the concrete ADB Keyboard IME id available on the device."""
    for args in (["shell", "ime", "list", "-s"], ["shell", "ime", "list", "-a"]):
        result = run_adb(args, device_id=device_id, check=False)
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if ADB_KEYBOARD_IME in stripped:
                return ADB_KEYBOARD_IME
            if ADB_KEYBOARD_PACKAGE in stripped and "/" in stripped:
                return stripped.split()[0].lstrip("*").strip()
    return None


def enable_adb_keyboard(ime: str = ADB_KEYBOARD_IME, device_id: str | None = None) -> None:
    """Enable ADB Keyboard before selecting it as the active IME."""
    run_adb(["shell", "ime", "enable", ime], device_id=device_id)


def get_current_input_method(device_id: str | None = None) -> str | None:
    """Return the current Android input method id."""
    result = run_adb(["shell", "settings", "get", "secure", "default_input_method"], device_id=device_id)
    ime = result.stdout.strip()
    return ime or None


def set_input_method(ime: str, device_id: str | None = None) -> None:
    """Set the current Android input method."""
    run_adb(["shell", "ime", "set", ime], device_id=device_id)


def _requires_adb_keyboard(text: str) -> bool:
    """Return whether text is unsafe for `adb shell input text`."""
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return True
    return any(char.isspace() for char in text)


def press_key(
    keycode: str | int, device_id: str | None = None, delay: float | None = None
) -> None:
    """Send an Android key event."""
    if delay is None:
        delay = TIMING_CONFIG.device.enter_delay
    run_adb(["shell", "input", "keyevent", str(keycode)], device_id=device_id)
    time.sleep(delay)


def back(device_id: str | None = None, delay: float | None = None) -> None:
    """Press Android Back."""
    if delay is None:
        delay = TIMING_CONFIG.device.back_delay
    press_key("BACK", device_id=device_id, delay=delay)


def home(device_id: str | None = None, delay: float | None = None) -> None:
    """Press Android Home."""
    if delay is None:
        delay = TIMING_CONFIG.device.home_delay
    press_key("HOME", device_id=device_id, delay=delay)


def enter(device_id: str | None = None, delay: float | None = None) -> None:
    """Press Android Enter."""
    if delay is None:
        delay = TIMING_CONFIG.device.enter_delay
    press_key("ENTER", device_id=device_id, delay=delay)


def wait(seconds: float) -> None:
    """Wait without sending input to the device."""
    time.sleep(seconds)
