# -*- coding: utf-8 -*-
"""External 对局执行器的生命周期与数据契约。"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime


SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_BUSINESS_ERROR = 1
EXIT_ENV_ERROR = 2
EXIT_STOPPED = 3
EXIT_TIMEOUT = 4

MATCH_READY_TIMEOUT = 180.0
MATCH_READY_SAMPLES = 2
MATCH_READY_INTERVAL = 1.5


class RunAbort(Exception):
    """携带标准退出码和事件信息的中止信号。"""

    def __init__(self, code, reason, stage=None, message=None):
        super().__init__(message or reason)
        self.code = code
        self.reason = reason
        self.stage = stage
        self.message = message or reason


@dataclass(frozen=True)
class DeviceEndpoint:
    host: str
    port: int

    @property
    def device_id(self):
        return f"{self.host}:{self.port}"


def parse_device_id(value):
    """把 <host>:<port> 转为 ADB TCP 端点。"""
    if not value or ":" not in value:
        raise ValueError("--device-id 必须是 <host>:<port> 格式")
    host, port_text = value.rsplit(":", 1)
    if not host:
        raise ValueError("--device-id 缺少 host")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("--device-id 的 port 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("--device-id 的 port 必须在 1-65535 之间")
    return DeviceEndpoint(host, port)


def resolve_device_endpoint(device_id=None, host=None, port=None):
    """解析设备来源; --device-id 与 --host/--port 不允许混用。"""
    if device_id is not None and (host is not None or port is not None):
        raise ValueError("--device-id 不能与 --host/--port 同时使用")
    if device_id is not None:
        return parse_device_id(device_id)
    resolved_port = 5555 if port is None else port
    if not 1 <= resolved_port <= 65535:
        raise ValueError("--port 必须在 1-65535 之间")
    return DeviceEndpoint(host or "localhost", resolved_port)


class EventWriter:
    """按 JSONL 契约写事件; 每轮原子替换, 避免读到半行。"""

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


class LifecycleGuard:
    """轮询 stop-file 和全局超时。"""

    def __init__(self, stop_file=None, timeout=None, poll_interval=0.5):
        self.stop_file = os.path.abspath(stop_file) if stop_file else None
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.started_at = time.monotonic()

    async def wait_for_abort(self):
        while True:
            if self.stop_file and os.path.exists(self.stop_file):
                return RunAbort(EXIT_STOPPED, "stop_file")
            if self.timeout is not None:
                elapsed = time.monotonic() - self.started_at
                if elapsed >= self.timeout:
                    return RunAbort(
                        EXIT_TIMEOUT,
                        "timeout",
                        stage="timeout",
                        message=f"global timeout after {self.timeout:.1f}s",
                    )
            await asyncio.sleep(self.poll_interval)


def is_valid_round(round_str):
    """只接受 1-1 到 7-7 的回合号。"""
    if not round_str:
        return False
    match = re.fullmatch(r"(\d+)-(\d+)", round_str)
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    return 1 <= major <= 7 and 1 <= minor <= 7


class StableMatchReadyGate:
    """连续多次识别到合法回合和局内阶段后放行。"""

    def __init__(
        self,
        banner,
        samples=MATCH_READY_SAMPLES,
        interval=MATCH_READY_INTERVAL,
        timeout=MATCH_READY_TIMEOUT,
    ):
        self.banner = banner
        self.samples = samples
        self.interval = interval
        self.timeout = timeout

    async def wait(self):
        deadline = time.monotonic() + self.timeout
        stable = 0
        last_round = None
        last_kind = None

        while time.monotonic() < deadline:
            kind, round_str = await self.banner.detect()
            if kind and is_valid_round(round_str):
                stable += 1
                last_round = round_str
                last_kind = kind
                if stable >= self.samples:
                    return kind, round_str
            else:
                stable = 0
            await asyncio.sleep(self.interval)

        raise RunAbort(
            EXIT_TIMEOUT,
            "match_ready_timeout",
            stage="match_ready",
            message=(
                f"MATCH_READY timeout: last_kind={last_kind}, "
                f"last_round={last_round}"
            ),
        )


PLACEMENTS = {
    "第一名": (1, "win"),
    "第二名": (2, "loss"),
    "第三名": (3, "loss"),
    "第四名": (4, "loss"),
    "第五名": (5, "loss"),
    "第六名": (6, "loss"),
    "第七名": (7, "loss"),
    "第八名": (8, "loss"),
}


def parse_match_result(texts):
    """从结算 OCR 文本提取 result/placement; 不确定时返回 unknown。"""
    for text in texts or []:
        compact = "".join(str(text).split())
        for marker, (placement, result) in PLACEMENTS.items():
            if marker in compact:
                return result, placement
    return "unknown", None
