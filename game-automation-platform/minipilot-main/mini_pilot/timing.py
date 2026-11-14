"""Timing configuration for MiniPilot runtime waits."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class DeviceTimingConfig:
    """Delays used by low-level device actions."""

    tap_delay: float = 0.25
    swipe_delay: float = 0.25
    back_delay: float = 0.3
    home_delay: float = 0.3
    enter_delay: float = 0.3
    launch_delay: float = 2.0
    launch_verify_timeout: float = 6.0
    launch_verify_interval: float = 0.5

    def __post_init__(self) -> None:
        self.tap_delay = float(os.getenv("MINI_PILOT_TAP_DELAY", self.tap_delay))
        self.swipe_delay = float(
            os.getenv("MINI_PILOT_SWIPE_DELAY", self.swipe_delay)
        )
        self.back_delay = float(os.getenv("MINI_PILOT_BACK_DELAY", self.back_delay))
        self.home_delay = float(os.getenv("MINI_PILOT_HOME_DELAY", self.home_delay))
        self.enter_delay = float(
            os.getenv("MINI_PILOT_ENTER_DELAY", self.enter_delay)
        )
        self.launch_delay = float(
            os.getenv("MINI_PILOT_LAUNCH_DELAY", self.launch_delay)
        )
        self.launch_verify_timeout = float(
            os.getenv(
                "MINI_PILOT_LAUNCH_VERIFY_TIMEOUT",
                self.launch_verify_timeout,
            )
        )
        self.launch_verify_interval = float(
            os.getenv(
                "MINI_PILOT_LAUNCH_VERIFY_INTERVAL",
                self.launch_verify_interval,
            )
        )


@dataclass
class ActionTimingConfig:
    """Delays used by composite actions such as text input."""

    keyboard_switch_delay: float = 0.25
    text_clear_delay: float = 0.3
    text_input_delay: float = 0.25
    keyboard_restore_delay: float = 0.2

    def __post_init__(self) -> None:
        self.keyboard_switch_delay = float(
            os.getenv(
                "MINI_PILOT_KEYBOARD_SWITCH_DELAY",
                self.keyboard_switch_delay,
            )
        )
        self.text_clear_delay = float(
            os.getenv("MINI_PILOT_TEXT_CLEAR_DELAY", self.text_clear_delay)
        )
        self.text_input_delay = float(
            os.getenv("MINI_PILOT_TEXT_INPUT_DELAY", self.text_input_delay)
        )
        self.keyboard_restore_delay = float(
            os.getenv(
                "MINI_PILOT_KEYBOARD_RESTORE_DELAY",
                self.keyboard_restore_delay,
            )
        )


@dataclass
class ScreenshotTimingConfig:
    """Delays used while retrying screenshots."""

    black_screen_retry_delay: float = 1.0

    def __post_init__(self) -> None:
        self.black_screen_retry_delay = float(
            os.getenv(
                "MINI_PILOT_BLACK_SCREEN_RETRY_DELAY",
                self.black_screen_retry_delay,
            )
        )


@dataclass
class TimingConfig:
    """Top-level runtime timing configuration."""

    device: DeviceTimingConfig
    action: ActionTimingConfig
    screenshot: ScreenshotTimingConfig

    def __init__(self) -> None:
        self.device = DeviceTimingConfig()
        self.action = ActionTimingConfig()
        self.screenshot = ScreenshotTimingConfig()


TIMING_CONFIG = TimingConfig()


def get_timing_config() -> TimingConfig:
    """Return the global MiniPilot timing configuration."""
    return TIMING_CONFIG
