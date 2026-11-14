import json
import os
import time
from datetime import datetime


SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_BUSINESS_ERROR = 1
EXIT_ENV_ERROR = 2
EXIT_STOPPED = 3
EXIT_TIMEOUT = 4

MATCH_READY_TIMEOUT = 180.0


class RunAbort(Exception):
    """Signal a controlled external stop or timeout."""

    def __init__(self, code, reason, stage=None, message=None):
        super().__init__(message or reason)
        self.code = code
        self.reason = reason
        self.stage = stage
        self.message = message or reason


class EventWriter:
    """Write the JSONL lifecycle contract consumed by the capture orchestrator."""

    def __init__(self, event_file=None, run_id=None):
        self.event_file = os.path.abspath(event_file) if event_file else None
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.seq = 0
        self._events = []
        self._once = set()
        if self.event_file:
            os.makedirs(os.path.dirname(self.event_file) or ".", exist_ok=True)

    def emit(self, event, once=False, **fields):
        if once and event in self._once:
            return False

        self.seq += 1
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
        }
        record.update(fields)
        self._events.append(record)
        if once:
            self._once.add(event)
        if self.event_file:
            self._write_atomic()
        return True

    def _write_atomic(self):
        temp_file = f"{self.event_file}.{os.getpid()}.tmp"
        with open(temp_file, "w", encoding="utf-8", newline="\n") as handle:
            for record in self._events:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file, self.event_file)


class ExternalLifecycle:
    """Manage external mode events, stop-file checks, and global timeouts."""

    def __init__(
        self,
        *,
        enabled=False,
        event_file=None,
        stop_file=None,
        timeout=None,
        run_id=None,
        match_ready_timeout=MATCH_READY_TIMEOUT,
        poll_interval=0.5,
    ):
        self.enabled = bool(enabled)
        self.events = EventWriter(event_file, run_id=run_id)
        self.stop_file = os.path.abspath(stop_file) if stop_file else None
        self.timeout = timeout
        self.match_ready_timeout = match_ready_timeout
        self.poll_interval = max(0.05, float(poll_interval))
        self.started_at = time.monotonic()
        self.match_ready_at = None
        self.match_finished = False
        self._last_check = 0.0

    def emit_started(self, *, device_id, mode):
        if not self.enabled:
            return
        self.events.emit(
            "SCRIPT_STARTED",
            once=True,
            mode=mode,
            device_id=device_id,
            binding_mode="adb_serial",
        )

    def emit_device_ready(self, *, device_id, resolution):
        if not self.enabled:
            return
        self.events.emit(
            "DEVICE_READY",
            once=True,
            device_id=device_id,
            binding_mode="adb_serial",
            resolution=resolution,
        )

    def emit_waiting(self):
        if not self.enabled:
            return
        self.events.emit("WAITING_MATCH_READY", once=True)

    def emit_match_ready(self, *, source="start_model"):
        if not self.enabled:
            return False
        self.match_ready_at = time.monotonic()
        return self.events.emit(
            "MATCH_READY",
            once=True,
            source=source,
        )

    def emit_match_finished(self, *, event=None, result=None, result_source=None):
        if not self.enabled:
            return False
        if result is None:
            result = {
                "successes": "win",
                "failed": "loss",
                "practice": "practice",
            }.get(event, "unknown")
        self.match_finished = True
        return self.events.emit(
            "MATCH_FINISHED",
            once=True,
            result=result,
            source_event=event,
            result_source=result_source or "event_mapping",
        )

    def emit_error(self, *, stage, message):
        if not self.enabled:
            return False
        return self.events.emit(
            "ERROR",
            stage=stage,
            message=str(message),
        )

    def emit_stopped(self, *, reason):
        if not self.enabled:
            return False
        return self.events.emit(
            "SCRIPT_STOPPED",
            once=True,
            reason=reason,
        )

    def check(self):
        if not self.enabled:
            return

        now = time.monotonic()
        if now - self._last_check < self.poll_interval:
            return
        self._last_check = now

        if self.stop_file and os.path.exists(self.stop_file):
            raise RunAbort(EXIT_STOPPED, "stop_file")

        elapsed = now - self.started_at
        if self.timeout is not None and elapsed >= self.timeout:
            raise RunAbort(
                EXIT_TIMEOUT,
                "timeout",
                stage="timeout",
                message=f"global timeout after {self.timeout:.1f}s",
            )

        if (
            self.match_ready_at is None
            and self.match_ready_timeout is not None
            and elapsed >= self.match_ready_timeout
        ):
            raise RunAbort(
                EXIT_TIMEOUT,
                "match_ready_timeout",
                stage="match_ready",
                message=(
                    "MATCH_READY timeout after "
                    f"{self.match_ready_timeout:.1f}s"
                ),
            )
