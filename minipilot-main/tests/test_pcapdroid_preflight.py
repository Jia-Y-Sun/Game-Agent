from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_pilot.capture import (
    CaptureError,
    UnicaptureConfig,
    UnicaptureSession,
    validate_pcapdroid_api_key,
)
from mini_pilot.device import DeviceError


def completed(
    stdout: str = "",
    *,
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["adb"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def package_output(*packages: str) -> str:
    return "".join(f"package:{package}\n" for package in packages)


def broadcast(result: int, token: str | None, protocol: int | None = 1) -> str:
    data = f', data="{token}"' if token is not None else ""
    version = f", protocol_version={protocol}" if protocol is not None else ""
    return f"Broadcast completed: result={result}{data}{version}\n"


class PcapdroidPreflightTest(unittest.TestCase):
    def test_rejects_missing_and_placeholder_keys_before_adb(self) -> None:
        for key in (None, "", "   ", "EMPTY", "YOUR_API_KEY", "change_me"):
            with self.subTest(key=key), patch(
                "mini_pilot.capture.run_adb"
            ) as run_adb:
                with self.assertRaisesRegex(CaptureError, "not configured"):
                    validate_pcapdroid_api_key(key)
                run_adb.assert_not_called()

    def test_accepts_debug_package_and_builds_explicit_component(self) -> None:
        with patch(
            "mini_pilot.capture.run_adb",
            side_effect=[
                completed(package_output("com.emanuelef.remote_capture.debug")),
                completed(broadcast(100, "PCAPDROID_API_KEY_VALID")),
            ],
        ) as run_adb:
            package = validate_pcapdroid_api_key(" valid-key ", device_id="phone")

        self.assertEqual("com.emanuelef.remote_capture.debug", package)
        command = run_adb.call_args_list[1].args[0]
        self.assertIn(
            "com.emanuelef.remote_capture.debug/"
            "com.emanuelef.remote_capture.ApiKeyValidationReceiver",
            command,
        )
        self.assertEqual("valid-key", command[-1])
        self.assertEqual("phone", run_adb.call_args_list[1].kwargs["device_id"])

    def test_accepts_release_package(self) -> None:
        with patch(
            "mini_pilot.capture.run_adb",
            side_effect=[
                completed(package_output("com.emanuelef.remote_capture")),
                completed(broadcast(100, "PCAPDROID_API_KEY_VALID")),
            ],
        ):
            self.assertEqual(
                "com.emanuelef.remote_capture",
                validate_pcapdroid_api_key("valid-key"),
            )

    def test_prefers_debug_when_both_packages_are_installed(self) -> None:
        with patch(
            "mini_pilot.capture.run_adb",
            side_effect=[
                completed(
                    package_output(
                        "com.emanuelef.remote_capture",
                        "com.emanuelef.remote_capture.debug",
                    )
                ),
                completed(broadcast(100, "PCAPDROID_API_KEY_VALID")),
            ],
        ):
            self.assertEqual(
                "com.emanuelef.remote_capture.debug",
                validate_pcapdroid_api_key("valid-key"),
            )

    def test_maps_known_result_codes(self) -> None:
        cases = (
            (101, "PCAPDROID_API_KEY_INVALID", "incorrect"),
            (102, "PCAPDROID_API_KEY_NOT_CONFIGURED", "no API Key"),
            (103, "PCAPDROID_API_KEY_MISSING", "did not contain"),
            (104, "PCAPDROID_API_KEY_BAD_REQUEST", "invalid format"),
        )
        for code, token, message in cases:
            with self.subTest(code=code), patch(
                "mini_pilot.capture.run_adb",
                side_effect=[
                    completed(package_output("com.emanuelef.remote_capture")),
                    completed(broadcast(code, token)),
                ],
            ):
                with self.assertRaisesRegex(CaptureError, message):
                    validate_pcapdroid_api_key("key-value")

    def test_success_requires_both_result_and_exact_token(self) -> None:
        failures = (
            broadcast(0, "PCAPDROID_API_KEY_VALID"),
            broadcast(100, None),
            broadcast(100, "PCAPDROID_API_KEY_INVALID"),
            "result=100 data=PCAPDROID_API_KEY_VALID",
        )
        for output in failures:
            with self.subTest(output=output), patch(
                "mini_pilot.capture.run_adb",
                side_effect=[
                    completed(package_output("com.emanuelef.remote_capture")),
                    completed(output),
                ],
            ):
                with self.assertRaises(CaptureError):
                    validate_pcapdroid_api_key("key-value")

    def test_rejects_unknown_protocol_version(self) -> None:
        with patch(
            "mini_pilot.capture.run_adb",
            side_effect=[
                completed(package_output("com.emanuelef.remote_capture")),
                completed(broadcast(100, "PCAPDROID_API_KEY_VALID", protocol=2)),
            ],
        ):
            with self.assertRaisesRegex(CaptureError, "protocol is unsupported"):
                validate_pcapdroid_api_key("key-value")

    def test_rejects_missing_package(self) -> None:
        with patch(
            "mini_pilot.capture.run_adb",
            return_value=completed(package_output("another.package")),
        ):
            with self.assertRaisesRegex(CaptureError, "not installed"):
                validate_pcapdroid_api_key("key-value")

    def test_rejects_adb_failure_timeout_and_receiver_error(self) -> None:
        failures = (
            DeviceError("offline"),
            subprocess.TimeoutExpired(["adb"], 10),
            completed(stderr="Error type 3", returncode=1),
        )
        for failure in failures:
            with self.subTest(failure=failure), patch(
                "mini_pilot.capture.run_adb",
                side_effect=[
                    completed(package_output("com.emanuelef.remote_capture")),
                    failure,
                ],
            ):
                with self.assertRaises(CaptureError):
                    validate_pcapdroid_api_key("private-key")

    def test_failed_preflight_does_not_start_unicapture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app_collector.py").touch()
            session = UnicaptureSession(
                UnicaptureConfig(
                    app_name="douyin",
                    scene="feed",
                    unicapture_dir=root,
                    python_executable=sys.executable,
                    output_dir=root / "output",
                    pcapdroid_api_key="wrong-key",
                )
            )
            with patch(
                "mini_pilot.capture.validate_pcapdroid_api_key",
                side_effect=CaptureError("incorrect"),
            ), patch("mini_pilot.capture.subprocess.Popen") as popen:
                with self.assertRaisesRegex(CaptureError, "incorrect"):
                    session.start()
                popen.assert_not_called()
                self.assertFalse((root / "output").exists())

    def test_root_or_disabled_pcap_skips_preflight(self) -> None:
        for capture_mode, enable_pcap in (("root", True), ("noroot", False)):
            with self.subTest(
                capture_mode=capture_mode, enable_pcap=enable_pcap
            ), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "app_collector.py").touch()
                session = UnicaptureSession(
                    UnicaptureConfig(
                        app_name="douyin",
                        scene="feed",
                        unicapture_dir=root,
                        python_executable=sys.executable,
                        output_dir=root / "output",
                        capture_mode=capture_mode,
                        enable_pcap=enable_pcap,
                    )
                )
                with patch(
                    "mini_pilot.capture.validate_pcapdroid_api_key"
                ) as validate, patch.object(
                    session, "_wait_until_ready"
                ), patch.object(
                    session, "_forward_output"
                ), patch(
                    "mini_pilot.capture.subprocess.Popen"
                ) as popen:
                    process = popen.return_value
                    process.stdout = None
                    session.start()
                    validate.assert_not_called()
                    session._cleanup_output_stream()

    def test_explicit_legacy_compatibility_skips_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app_collector.py").touch()
            session = UnicaptureSession(
                UnicaptureConfig(
                    app_name="bilibili",
                    scene="video",
                    unicapture_dir=root,
                    python_executable=sys.executable,
                    output_dir=root / "output",
                    capture_mode="noroot",
                    enable_pcap=True,
                    pcapdroid_preflight=False,
                )
            )
            with patch(
                "mini_pilot.capture.validate_pcapdroid_api_key"
            ) as validate, patch.object(
                session, "_wait_until_ready"
            ), patch.object(
                session, "_forward_output"
            ), patch(
                "mini_pilot.capture.subprocess.Popen"
            ) as popen:
                process = popen.return_value
                process.stdout = None
                session.start()
                validate.assert_not_called()
                session._cleanup_output_stream()


if __name__ == "__main__":
    unittest.main()
