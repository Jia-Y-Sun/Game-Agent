from __future__ import annotations

import io
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


UNICAPTURE_DIR = Path(__file__).resolve().parents[1] / "unicapture"
if str(UNICAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(UNICAPTURE_DIR))

from app_collector import ADBHelper  # noqa: E402


WINDOWS_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 1)


class FakeScrcpyProcess:
    def __init__(self, exit_code: int = 0, stderr: str = "") -> None:
        self.returncode: int | None = None
        self.exit_code = exit_code
        self.stderr = io.StringIO(stderr)
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sent_signal: int) -> None:
        self.signals.append(sent_signal)

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self.exit_code
        return self.exit_code

    def terminate(self) -> None:
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1


class ScrcpyDiscoveryTests(unittest.TestCase):
    def test_adb_helper_uses_configured_adb_binary(self) -> None:
        with patch.dict(
            os.environ,
            {"MINI_PILOT_ADB_BIN": r"D:\leidian\LDPlayer14\adb.exe"},
            clear=False,
        ):
            command = ADBHelper._build_base_cmd("127.0.0.1:5555")
        self.assertEqual(
            command,
            [r"D:\leidian\LDPlayer14\adb.exe", "-s", "127.0.0.1:5555"],
        )

    def test_windows_uses_bundled_executable(self) -> None:
        with (
            patch("app_collector.sys.platform", "win32"),
            patch("app_collector.Path.is_file", return_value=True),
            patch("app_collector.shutil.which") as which,
        ):
            result = ADBHelper.find_scrcpy()

        self.assertIsNotNone(result)
        self.assertEqual(Path(result or "").name, "scrcpy.exe")
        which.assert_not_called()

    def test_windows_falls_back_to_path_without_bundled_binary(self) -> None:
        native_scrcpy = r"C:\tools\scrcpy.exe"
        with (
            patch("app_collector.sys.platform", "win32"),
            patch("app_collector.Path.is_file", return_value=False),
            patch("app_collector.shutil.which", return_value=native_scrcpy) as which,
        ):
            result = ADBHelper.find_scrcpy()
        self.assertEqual(result, native_scrcpy)
        which.assert_called_once_with("scrcpy.exe")

    def test_macos_ignores_bundled_windows_executable(self) -> None:
        native_scrcpy = "/opt/homebrew/bin/scrcpy"
        with (
            patch("app_collector.sys.platform", "darwin"),
            patch("app_collector.shutil.which", return_value=native_scrcpy) as which,
        ):
            result = ADBHelper.find_scrcpy()

        self.assertEqual(result, native_scrcpy)
        which.assert_called_once_with("scrcpy")


class ScrcpyPosixWorkflowTests(unittest.TestCase):
    def test_scrcpy_inherits_selected_helper_device(self) -> None:
        process = FakeScrcpyProcess()
        helper = ADBHelper("phone-b")
        with tempfile.TemporaryDirectory() as directory:
            save_path = Path(directory) / "recording.mp4"
            with (
                patch("app_collector.sys.platform", "darwin"),
                patch.object(ADBHelper, "find_scrcpy", return_value="/usr/bin/scrcpy"),
                patch("app_collector.subprocess.Popen", return_value=process) as popen,
                patch("app_collector.time.sleep"),
            ):
                result = helper.start_scrcpy_record(str(save_path), 30)

        self.assertIs(result, process)
        command = popen.call_args.args[0]
        serial_index = command.index("-s")
        self.assertEqual("phone-b", command[serial_index + 1])

    def test_macos_records_directly_to_mp4(self) -> None:
        process = FakeScrcpyProcess()
        helper = ADBHelper.__new__(ADBHelper)
        with tempfile.TemporaryDirectory() as directory:
            save_path = Path(directory) / "recording.mp4"
            with (
                patch("app_collector.sys.platform", "darwin"),
                patch.object(ADBHelper, "find_scrcpy", return_value="/usr/bin/scrcpy"),
                patch("app_collector.subprocess.Popen", return_value=process) as popen,
                patch("app_collector.time.sleep"),
            ):
                result = helper.start_scrcpy_record(str(save_path), 30)

        self.assertIs(result, process)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--record") + 1], str(save_path))
        self.assertNotIn("--record-format", command)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0)

    def test_macos_uses_sigint_and_does_not_remux(self) -> None:
        process = FakeScrcpyProcess()
        helper = ADBHelper.__new__(ADBHelper)
        with tempfile.TemporaryDirectory() as directory:
            save_path = Path(directory) / "recording.mp4"
            save_path.write_bytes(b"recorded-video")
            with (
                patch("app_collector.sys.platform", "darwin"),
                patch.object(ADBHelper, "_validate_video_file", return_value=True) as validate,
                patch.object(ADBHelper, "_remux_video_to_mp4") as remux,
            ):
                valid = helper.stop_scrcpy_record(process, str(save_path))

        self.assertTrue(valid)
        self.assertEqual(process.signals, [signal.SIGINT])
        validate.assert_called_once_with(save_path, label="MP4")
        remux.assert_not_called()

    def test_macos_prefers_scrcpy_natural_exit(self) -> None:
        process = FakeScrcpyProcess()
        process._scrcpy_natural_deadline = 0.5
        helper = ADBHelper.__new__(ADBHelper)
        with tempfile.TemporaryDirectory() as directory:
            save_path = Path(directory) / "recording.mp4"
            save_path.write_bytes(b"recorded-video")
            with (
                patch("app_collector.sys.platform", "darwin"),
                patch("app_collector.time.monotonic", return_value=0),
                patch.object(ADBHelper, "_validate_video_file", return_value=True),
            ):
                valid = helper.stop_scrcpy_record(process, str(save_path))

        self.assertTrue(valid)
        self.assertEqual(process.signals, [])


class ScrcpyRecordingStopTests(unittest.TestCase):
    def test_windows_uses_ctrl_break_and_remuxes_valid_mkv(self) -> None:
        process = FakeScrcpyProcess()
        helper = ADBHelper.__new__(ADBHelper)
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "recording.scrcpy.mkv"
            final_path = Path(directory) / "recording.mp4"
            record_path.write_bytes(b"recorded-video")
            with (
                patch("app_collector.sys.platform", "win32"),
                patch(
                    "app_collector.signal.CTRL_BREAK_EVENT",
                    WINDOWS_CTRL_BREAK_EVENT,
                    create=True,
                ),
                patch("app_collector.time.sleep"),
                patch.object(ADBHelper, "_validate_video_file", return_value=True),
                patch.object(ADBHelper, "_remux_video_to_mp4", return_value=True) as remux,
            ):
                valid = helper.stop_scrcpy_record(
                    process, str(record_path), str(final_path)
                )

        self.assertTrue(valid)
        self.assertEqual(process.signals, [WINDOWS_CTRL_BREAK_EVENT])
        remux.assert_called_once_with(record_path, final_path)

    def test_invalid_mkv_is_not_remuxed(self) -> None:
        process = FakeScrcpyProcess(exit_code=1, stderr="recording failed")
        helper = ADBHelper.__new__(ADBHelper)
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "recording.scrcpy.mkv"
            final_path = Path(directory) / "recording.mp4"
            record_path.write_bytes(b"incomplete-video")
            with (
                patch("app_collector.sys.platform", "win32"),
                patch(
                    "app_collector.signal.CTRL_BREAK_EVENT",
                    WINDOWS_CTRL_BREAK_EVENT,
                    create=True,
                ),
                patch("app_collector.time.sleep"),
                patch.object(ADBHelper, "_validate_video_file", return_value=False),
                patch.object(ADBHelper, "_remux_video_to_mp4") as remux,
            ):
                valid = helper.stop_scrcpy_record(
                    process, str(record_path), str(final_path)
                )

        self.assertFalse(valid)
        self.assertEqual(process.signals, [WINDOWS_CTRL_BREAK_EVENT])
        remux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
