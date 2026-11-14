"""Process adapter for the MiniPilot pre-game navigation agent."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class PregameAgentError(RuntimeError):
    """Raised when the pre-game agent process cannot be controlled."""


@dataclass(frozen=True)
class PregameAgentConfig:
    """Configuration for one pre-game MiniPilot process."""

    command: Sequence[str]
    cwd: str | Path
    live_command_file: str | Path
    log_file: str | Path
    env: Mapping[str, str] | None = None


class PregameAgentProcess:
    """Own the lifecycle of the pre-game MiniPilot process."""

    def __init__(self, config: PregameAgentConfig) -> None:
        self.config = config
        self.cwd = Path(config.cwd).resolve()
        self.live_command_file = Path(config.live_command_file).resolve()
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
        if self.process is not None:
            raise PregameAgentError("Pre-game agent is already started.")
        if not self.cwd.is_dir():
            raise PregameAgentError(
                f"Pre-game agent directory does not exist: {self.cwd}"
            )

        self.live_command_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.live_command_file.unlink(missing_ok=True)
        self.live_command_file.touch()

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

    def request_stop(self, reason: str) -> None:
        """Write a /stop live command consumed by MiniPilot."""
        self.live_command_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"text": f"/stop {reason}"}
        with self.live_command_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def wait(self, timeout: float | None = None) -> int:
        if self.process is None:
            raise PregameAgentError("Pre-game agent has not been started.")
        returncode = self.process.wait(timeout=timeout)
        self._close_log()
        return returncode

    def terminate(self, reason: str, grace_seconds: float = 10.0) -> int | None:
        if self.process is None:
            return None
        self.request_stop(reason)
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

