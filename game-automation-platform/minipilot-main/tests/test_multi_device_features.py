from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mini_pilot.capture import UnicaptureConfig, UnicaptureSession
from mini_pilot.device import DeviceInfo, _adb_prefix, close_app_by_package
from mini_pilot.gui import _initial_device_selection
from mini_pilot.main import (
    _create_run_dir,
    _resolve_device_id,
    _resolve_pcapdroid_api_key,
    _resolve_target_package,
    _validate_repeat_options,
)


UNICAPTURE_DIR = Path(__file__).resolve().parents[1] / "unicapture"
if str(UNICAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(UNICAPTURE_DIR))

from app_collector import ADBHelper as CollectorADBHelper  # noqa: E402
from check_large_files import ADBHelper as LargeFileADBHelper  # noqa: E402
from qoe_monitor import ADBHelper as QoEADBHelper  # noqa: E402


def _adb_devices_result(*lines: str) -> subprocess.CompletedProcess[str]:
    output = "List of devices attached\n" + "\n".join(lines) + "\n"
    return subprocess.CompletedProcess(["adb", "devices"], 0, output, "")


class MultiDeviceKeyTest(unittest.TestCase):
    def test_cli_key_has_priority(self) -> None:
        self.assertEqual(
            "cli-key",
            _resolve_pcapdroid_api_key(
                " cli-key ",
                {"pcapdroid_api_keys": {"phone-a": "mapped-key"}},
                "phone-a",
            ),
        )

    def test_selects_key_for_exact_device(self) -> None:
        settings = {
            "pcapdroid_api_keys": {
                "phone-a": "key-a",
                "phone-b": "key-b",
            }
        }
        self.assertEqual(
            "key-b",
            _resolve_pcapdroid_api_key(None, settings, "phone-b"),
        )

    def test_missing_device_does_not_borrow_legacy_or_other_device_key(self) -> None:
        settings = {
            "pcapdroid_api_keys": {"phone-a": "key-a"},
            "pcapdroid_api_key": "legacy-key",
        }
        self.assertIsNone(
            _resolve_pcapdroid_api_key(None, settings, "phone-b")
        )

    def test_legacy_key_remains_compatible_without_mapping(self) -> None:
        self.assertEqual(
            "legacy-key",
            _resolve_pcapdroid_api_key(
                None,
                {"pcapdroid_api_key": "legacy-key"},
                "phone-a",
            ),
        )


class DeviceSelectionTest(unittest.TestCase):
    def test_explicit_cli_device_is_preserved(self) -> None:
        with patch("mini_pilot.main.list_devices") as list_devices:
            self.assertEqual("offline-phone", _resolve_device_id("offline-phone"))
            list_devices.assert_not_called()

    def test_auto_selects_only_ready_device(self) -> None:
        with patch(
            "mini_pilot.main.list_devices",
            return_value=[
                DeviceInfo(device_id="phone-a", status="device"),
                DeviceInfo(device_id="phone-b", status="offline"),
            ],
        ):
            self.assertEqual("phone-a", _resolve_device_id(None))

    def test_gui_never_replaces_requested_device(self) -> None:
        selected, choices = _initial_device_selection(
            "requested-phone",
            ["another-phone"],
        )
        self.assertEqual("requested-phone", selected)
        self.assertEqual(["requested-phone", "another-phone"], choices)

    def test_gui_does_not_guess_when_multiple_devices_are_ready(self) -> None:
        selected, _ = _initial_device_selection(None, ["phone-a", "phone-b"])
        self.assertIsNone(selected)

    def test_minipilot_adb_prefix_targets_selected_device(self) -> None:
        with patch("mini_pilot.device.ensure_adb_available"):
            self.assertEqual(["adb", "-s", "phone-b"], _adb_prefix("phone-b"))

    def test_unicapture_helpers_pin_the_only_ready_device(self) -> None:
        helper_specs = (
            (CollectorADBHelper, "app_collector.subprocess.run"),
            (QoEADBHelper, "qoe_monitor.subprocess.run"),
            (LargeFileADBHelper, "check_large_files.subprocess.run"),
        )
        for helper_class, subprocess_path in helper_specs:
            with self.subTest(helper=helper_class.__module__), patch(
                subprocess_path,
                return_value=_adb_devices_result("phone-a\tdevice"),
            ):
                helper = helper_class()
                self.assertTrue(helper.check_connection())
                self.assertEqual("phone-a", helper.device_id)
                self.assertEqual(["adb", "-s", "phone-a"], helper.base_cmd)

    def test_unicapture_helpers_reject_ambiguous_device_selection(self) -> None:
        helper_specs = (
            (CollectorADBHelper, "app_collector.subprocess.run"),
            (QoEADBHelper, "qoe_monitor.subprocess.run"),
            (LargeFileADBHelper, "check_large_files.subprocess.run"),
        )
        result = _adb_devices_result("phone-a\tdevice", "phone-b\tdevice")
        for helper_class, subprocess_path in helper_specs:
            with self.subTest(helper=helper_class.__module__), patch(
                subprocess_path,
                return_value=result,
            ):
                helper = helper_class()
                self.assertFalse(helper.check_connection())
                self.assertIn("--device", helper.connection_error)
                self.assertIn("phone-a", helper.connection_error)
                self.assertIn("phone-b", helper.connection_error)

    def test_unicapture_helpers_validate_explicit_device(self) -> None:
        helper_specs = (
            (CollectorADBHelper, "app_collector.subprocess.run"),
            (QoEADBHelper, "qoe_monitor.subprocess.run"),
            (LargeFileADBHelper, "check_large_files.subprocess.run"),
        )
        result = _adb_devices_result("phone-a\tdevice", "phone-b\tdevice")
        for helper_class, subprocess_path in helper_specs:
            with self.subTest(helper=helper_class.__module__), patch(
                subprocess_path,
                return_value=result,
            ):
                helper = helper_class("phone-b")
                self.assertTrue(helper.check_connection())
                self.assertEqual(["adb", "-s", "phone-b"], helper.base_cmd)


class RepeatAndRunDirectoryTest(unittest.TestCase):
    def test_repeat_options_reject_invalid_values(self) -> None:
        self.assertIn("at least 1", _validate_repeat_options(0, 10))
        self.assertIn("at least 1", _validate_repeat_options(-1, 10))
        self.assertIn("cannot be negative", _validate_repeat_options(1, -0.1))
        self.assertIsNone(_validate_repeat_options(1, 0))

    def test_parallel_runs_allocate_unique_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with ThreadPoolExecutor(max_workers=12) as executor:
                paths = list(
                    executor.map(
                        lambda _index: _create_run_dir(base, "same goal"),
                        range(24),
                    )
                )
            self.assertEqual(24, len({path.name for path in paths}))
            self.assertTrue(all(path.is_dir() for path in paths))


class CleanupAndCustomCaptureTest(unittest.TestCase):
    def test_capture_command_forwards_custom_app_fields(self) -> None:
        session = UnicaptureSession(
            UnicaptureConfig(
                app_name="custom",
                scene="feed",
                pcap_app_package="com.example.custom",
                custom_app_type="social",
                custom_app_scene="feed",
            )
        )
        command = session._build_command()
        self.assertIn("--custom-app-type", command)
        self.assertIn("social", command)
        self.assertIn("--custom-app-scene", command)
        self.assertIn("feed", command)

    def test_capture_command_forwards_selected_device(self) -> None:
        session = UnicaptureSession(
            UnicaptureConfig(
                app_name="douyin",
                scene="feed",
                device_id="phone-b",
            )
        )
        command = session._build_command()
        device_index = command.index("--device")
        self.assertEqual("phone-b", command[device_index + 1])

    def test_capture_none_still_resolves_custom_package_for_cleanup(self) -> None:
        args = argparse.Namespace(
            custom_app_package="com.example.custom",
            capture_pcap_app_package=None,
            capture_app=None,
            unicapture_dir=None,
        )
        self.assertEqual(
            "com.example.custom",
            _resolve_target_package(args, {}, "use custom", None),
        )

    def test_force_stop_targets_selected_device(self) -> None:
        with patch("mini_pilot.device.run_adb") as run_adb:
            close_app_by_package(
                "com.example.custom",
                device_id="phone-b",
            )
        run_adb.assert_called_once_with(
            ["shell", "am", "force-stop", "com.example.custom"],
            device_id="phone-b",
            timeout=10,
        )


if __name__ == "__main__":
    unittest.main()
