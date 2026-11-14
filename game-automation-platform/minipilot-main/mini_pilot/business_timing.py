"""Runtime business-start detection and effective-duration control."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


ACTIVE_VLM_STATES = {"GAMEPLAY", "PLAYING", "LIVE", "MEETING", "CALL"}


def create_vlm_probe(
    *,
    device_id: str | None,
    app_type: str,
    scene: str,
    base_url: str,
    api_key: str,
    model: str,
) -> Callable[[], str] | None:
    """Build a screenshot classifier without exposing credentials."""
    key = (api_key or "").strip()
    if key in {"", "1111", "EMPTY", "YOUR_API_KEY", "CHANGE_ME"}:
        return None
    try:
        import cv2
        import numpy as np

        from mini_pilot.device import run_adb_bytes
        from unicapture.qoe_postprocess.perception import VLMPerception
    except ImportError:
        return None

    lowered = f"{base_url} {model}".lower()
    provider = "qwen" if "qwen" in lowered or "dashscope" in lowered else "openai"
    classifier = VLMPerception(provider, base_url, key, model)

    def probe() -> str:
        result = run_adb_bytes(
            ["exec-out", "screencap", "-p"],
            device_id=device_id,
            timeout=15,
        )
        raw = np.frombuffer(result.stdout, dtype=np.uint8)
        image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if image is None:
            return "UNKNOWN"
        return classifier.classify(image, app_type, scene)

    return probe


@dataclass(frozen=True)
class BusinessTimingResult:
    source: str
    business_start_monotonic: float
    business_start_time: datetime
    target_duration_seconds: int


class BusinessTimingController:
    """Detect business start, then complete after the requested effective time."""

    def __init__(
        self,
        *,
        target_duration_seconds: int,
        startup_timeout_seconds: int,
        activation_fallback_delay_seconds: int,
        marker_path: Path,
        activation_probe: Callable[[], str | None],
        vlm_probe: Callable[[], str] | None = None,
        vlm_interval_seconds: float = 5.0,
    ) -> None:
        self.target_duration_seconds = max(1, target_duration_seconds)
        self.startup_timeout_seconds = max(0, startup_timeout_seconds)
        self.activation_fallback_delay_seconds = max(
            0, activation_fallback_delay_seconds
        )
        self.marker_path = marker_path
        self.activation_probe = activation_probe
        self.vlm_probe = vlm_probe
        self.vlm_interval_seconds = max(0.1, vlm_interval_seconds)
        self._result: BusinessTimingResult | None = None
        self._complete = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_state = "waiting"

    @property
    def result(self) -> BusinessTimingResult | None:
        with self._lock:
            return self._result

    @property
    def last_state(self) -> str:
        with self._lock:
            return self._last_state

    def duration_started_at(self) -> float | None:
        result = self.result
        return result.business_start_monotonic if result else None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def wait(self, timeout: float | None = None) -> bool:
        return self._complete.wait(timeout)

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._last_state = state

    def _set_business_start(
        self,
        monotonic_value: float,
        wall_time: datetime,
        source: str,
    ) -> None:
        result = BusinessTimingResult(
            source=source,
            business_start_monotonic=monotonic_value,
            business_start_time=wall_time,
            target_duration_seconds=self.target_duration_seconds,
        )
        with self._lock:
            if self._result is not None:
                return
            self._result = result
            self._last_state = f"business_started:{source}"
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(
            json.dumps(
                {
                    "business_start_time": wall_time.astimezone().isoformat(),
                    "source": source,
                    "target_duration_seconds": self.target_duration_seconds,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _run(self) -> None:
        monitor_started = time.monotonic()
        deadline = monitor_started + self.startup_timeout_seconds
        activation_at: float | None = None
        activation_wall: datetime | None = None
        activation_source = ""
        positive_vlm: list[tuple[float, datetime]] = []
        next_vlm_at = monitor_started

        while not self._cancel.is_set() and self.result is None:
            now = time.monotonic()
            if activation_at is None:
                try:
                    detected_source = self.activation_probe()
                except Exception:
                    detected_source = None
                if detected_source:
                    activation_at = now
                    activation_wall = datetime.now().astimezone()
                    activation_source = detected_source
                    self._set_state(f"activated:{detected_source}")

            if self.vlm_probe is not None and now >= next_vlm_at:
                next_vlm_at = now + self.vlm_interval_seconds
                try:
                    state = self.vlm_probe().upper()
                except Exception:
                    state = "UNKNOWN"
                # The VLM call may take several seconds.  Keep the monotonic
                # and wall-clock anchors from the same instant *after* the
                # response; pairing the pre-call monotonic value with a
                # post-call wall time makes exported business windows shorter
                # than the duration enforced by the runtime clock.
                observed_mono = time.monotonic()
                observed_wall = datetime.now().astimezone()
                self._set_state(f"vlm:{state}")
                if state in ACTIVE_VLM_STATES:
                    positive_vlm.append((observed_mono, observed_wall))
                    positive_vlm = [
                        item
                        for item in positive_vlm
                        if observed_mono - item[0] <= 10
                    ]
                    if len(positive_vlm) >= 2:
                        first_mono, first_wall = positive_vlm[0]
                        self._set_business_start(first_mono, first_wall, "vlm")
                        break

            if activation_at is not None:
                delay = (
                    0
                    if self.vlm_probe is None
                    else self.activation_fallback_delay_seconds
                )
                if now - activation_at >= delay:
                    self._set_business_start(
                        activation_at,
                        activation_wall or datetime.now().astimezone(),
                        f"{activation_source or 'activate'}_fallback",
                    )
                    break

            if now >= deadline:
                self._set_business_start(
                    now,
                    datetime.now().astimezone(),
                    "startup_timeout_fallback",
                )
                break
            self._cancel.wait(0.25)

        result = self.result
        if result is None:
            return
        finish_at = (
            result.business_start_monotonic + result.target_duration_seconds
        )
        while not self._cancel.is_set():
            remaining = finish_at - time.monotonic()
            if remaining <= 0:
                self._set_state("complete")
                self._complete.set()
                return
            self._cancel.wait(min(0.5, remaining))
