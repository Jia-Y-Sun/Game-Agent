"""Process adapter for an external Jin Chan Chan game driver."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class GameDriverError(RuntimeError):
    """Raised when the external game driver cannot be controlled."""


@dataclass(frozen=True)
class GameDriverConfig:
    """Configuration for one external game-driver process."""

    command: Sequence[str]
    cwd: str | Path
    event_file: str | Path
    stop_file: str | Path
    log_file: str | Path
    env: Mapping[str, str] | None = None


class GameDriverProcess:
    """Own the lifecycle of one external game-driver process."""

    def __init__(self, config: GameDriverConfig) -> None:
        self.config = config
        self.cwd = Path(config.cwd).resolve()
        self.event_file = Path(config.event_file).resolve()
        self.stop_file = Path(config.stop_file).resolve()
        self.log_file = Path(config.log_file).resolve()
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self.started_at = 0.0

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.process is None else self.process.returncode

    def start(self) -> None:
        """Clear stale control files and start the game driver."""
        if self.process is not None:
            raise GameDriverError("Game driver is already started.")
        if not self.cwd.is_dir():
            raise GameDriverError(f"Game driver directory does not exist: {self.cwd}")

        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.event_file.unlink(missing_ok=True)
        self.stop_file.unlink(missing_ok=True)

        env = dict(os.environ)
        if self.config.env:
            env.update(self.config.env)

        self._log_handle = self.log_file.open("ab")
        self.process = subprocess.Popen(
            list(self.config.command),
            cwd=str(self.cwd),
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0
            ),
        )
        self.started_at = time.monotonic()

    def request_stop(self) -> None:
        """Create the stop file consumed by the external game driver."""
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(
            "stop requested by integration orchestrator\n",
            encoding="utf-8",
        )

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process exit and return its exit code."""
        if self.process is None:
            raise GameDriverError("Game driver has not been started.")
        returncode = self.process.wait(timeout=timeout)
        self._close_log()
        return returncode

    def terminate(self, grace_seconds: float = 5.0) -> int | None:
        """Request an external stop, then terminate the process if needed."""
        if self.process is None:
            return None
        self.request_stop()
        try:
            return self.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                return self.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                return self.wait(timeout=5.0)

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def __enter__(self) -> "GameDriverProcess":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.is_running:
            self.terminate()
        self._close_log()

