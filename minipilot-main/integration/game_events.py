"""Parse and tail the JSONL events emitted by an external game driver."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class EventProtocolError(RuntimeError):
    """Raised when the game driver event stream violates its contract."""


class EventWaitTimeout(TimeoutError):
    """Raised when a requested event does not arrive before the deadline."""


@dataclass(frozen=True)
class GameEvent:
    """One validated game-driver event."""

    event: str
    seq: int
    ts: str
    run_id: str | None
    schema_version: int | None
    payload: Mapping[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def parse_event_line(line: str, *, require_seq: bool = True) -> GameEvent:
    """Parse one JSONL record and validate the minimum event contract."""
    text = line.strip()
    if not text:
        raise EventProtocolError("Event line is empty.")
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EventProtocolError(f"Event is not valid JSON: {text[:200]!r}") from exc
    if not isinstance(record, dict):
        raise EventProtocolError("Event JSON root must be an object.")

    event_name = record.get("event")
    if not isinstance(event_name, str) or not event_name.strip():
        raise EventProtocolError("Event JSON is missing a non-empty 'event' field.")

    raw_seq = record.get("seq")
    if require_seq:
        if not isinstance(raw_seq, int):
            raise EventProtocolError("Event JSON is missing an integer 'seq'.")
        seq = raw_seq
    else:
        seq = raw_seq if isinstance(raw_seq, int) else 0

    ts = record.get("ts")
    if ts is not None and not isinstance(ts, str):
        raise EventProtocolError("Event 'ts' must be a string when present.")

    run_id = record.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise EventProtocolError("Event 'run_id' must be a string when present.")

    schema_version = record.get("schema_version")
    if schema_version is not None and not isinstance(schema_version, int):
        raise EventProtocolError("Event 'schema_version' must be an integer when present.")

    return GameEvent(
        event=event_name.strip(),
        seq=seq,
        ts=ts or "",
        run_id=run_id,
        schema_version=schema_version,
        payload=record,
    )


class EventFileTailer:
    """Incrementally read complete JSONL records from a game event file."""

    def __init__(self, path: str | Path, *, require_seq: bool = True) -> None:
        self.path = Path(path)
        self.require_seq = require_seq
        self._offset = 0
        self._buffer = b""
        self._last_seq = 0
        self._events: list[GameEvent] = []

    @property
    def events(self) -> tuple[GameEvent, ...]:
        return tuple(self._events)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def poll(self) -> list[GameEvent]:
        """Read only complete appended lines and return newly parsed events."""
        if not self.path.exists():
            return []

        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()

        if not chunk:
            return []

        self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()

        parsed: list[GameEvent] = []
        for raw_line in lines:
            if not raw_line.strip():
                continue
            text = raw_line.decode("utf-8-sig").strip()
            event = parse_event_line(text, require_seq=self.require_seq)
            if self.require_seq and event.seq <= self._last_seq:
                raise EventProtocolError(
                    f"Event sequence is not strictly increasing: "
                    f"{event.seq} after {self._last_seq}."
                )
            self._last_seq = event.seq
            self._events.append(event)
            parsed.append(event)
        return parsed

    def wait_for(
        self,
        event_names: str | Iterable[str],
        *,
        timeout: float,
        poll_interval: float = 0.1,
    ) -> GameEvent:
        """Wait for one of the requested event names."""
        wanted = (
            {event_names}
            if isinstance(event_names, str)
            else set(event_names)
        )
        if not wanted:
            raise ValueError("At least one event name is required.")

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            for event in self.poll():
                if event.event in wanted:
                    return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EventWaitTimeout(
                    f"Timed out waiting for event(s): {', '.join(sorted(wanted))}"
                )
            time.sleep(min(poll_interval, remaining))

