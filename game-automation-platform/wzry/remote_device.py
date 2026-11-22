"""程序端（双端模式）：用网络里的设备替代本机 adb。

设备端跑 `device_agent.py`，本模块提供三个替身，让 `train.py` 里的其它代码完全不用改：

* `RemoteAndroidTool`：替代 `AndroidTool`，把「截图 / 注入动作 / 松摇杆」变成 RPC；
* `RemoteProcNetDevTrafficProbe`：替代 `AdbProcNetDevTrafficProbe`，流量计数由设备端读；
* `RemoteTouchStream`：替代 `HumanTouchStream`，触摸行由设备端转发。

设计要点：

1. **数据不过网**：设备端只发它本来就要发的画面，程序端拿到帧自己算观测/奖励/存经验池，
   所以训练用的张量一张都不用传；
2. **每步只发一帧**：jpeg 默认质量 82，2400×1080 约 250~400KB；
3. **失败即降级**：任何 RPC 失败都返回「空动作/空帧」而不是抛异常，让主循环继续，
   设备端另有看门狗负责松摇杆，避免程序端崩了人物一直跑。
"""

import concurrent.futures
import threading
import time

import cv2
import numpy as np

from live_human_assist import HumanTouchStream
from remote_protocol import (
    PROTOCOL_VERSION,
    Message,
    make_connection,
    parse_endpoint,
    recv_message,
    send_message,
)
from traffic_probe import AdbProcNetDevTrafficProbe, parse_proc_net_dev


class RemoteDeviceError(RuntimeError):
    """设备端不可用（超时/断线/拒绝）。"""


class RemoteAndroidTool:
    """`AndroidTool` 的网络替身，接口按 `wzry_env` 实际调用面实现。"""

    #: 供 `LiveHumanAssist` / `build_traffic_probe` 识别双端模式
    is_remote_device = True

    def __init__(self, endpoint, token="", timeout_ms=3000, frame_format="jpeg",
                 frame_quality=82, reconnect_attempts=2, verbose=True):
        self.host, self.port = parse_endpoint(endpoint, default_port=5599)
        self.endpoint = f"{self.host}:{self.port}"
        self.token = token or ""
        self.timeout_sec = max(0.2, float(timeout_ms) / 1000.0)
        self.frame_format = str(frame_format or "jpeg").lower()
        self.frame_quality = int(frame_quality)
        self.reconnect_attempts = max(0, int(reconnect_attempts))
        self.verbose = bool(verbose)

        self.actual_width = 0
        self.actual_height = 0
        self.scrcpy_dir = ""
        self.device_serial = ""
        self.protocol = 0
        self.device_touch_device = ""

        self.sock = None
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Condition()
        self._next_id = 1
        self._reader_thread = None
        self._closed = False
        self._connected = threading.Event()
        self.touch_stream = None
        self.hello = {}

        self.stats = {
            "requests": 0,
            "errors": 0,
            "reconnects": 0,
            "frames": 0,
            "bytes_in": 0,
            "bytes_out": 0,
        }

        self._connect(raise_on_error=True)
        if self.verbose:
            print(
                f"[remote] 已连接设备端 {self.endpoint} "
                f"(分辨率 {self.actual_width}x{self.actual_height}, "
                f"帧格式 {self.frame_format}@{self.frame_quality})"
            )

    # ------------------------------------------------------------------ #
    # 连接与收发
    # ------------------------------------------------------------------ #
    def _connect(self, raise_on_error=False):
        try:
            sock = make_connection(self.host, self.port, timeout_sec=max(1.0, self.timeout_sec))
        except OSError as exc:
            if raise_on_error:
                raise RemoteDeviceError(f"连不上设备端 {self.endpoint}: {exc}") from exc
            return False

        self.sock = sock
        self._closed = False
        self._connected.set()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="remote-device", daemon=True
        )
        self._reader_thread.start()

        try:
            meta, _ = self._request("hello", {"protocol": PROTOCOL_VERSION, "token": self.token})
        except RemoteDeviceError:
            self._shutdown_socket()
            if raise_on_error:
                raise
            return False

        if not meta.get("ok"):
            reason = meta.get("error") or "握手被拒绝"
            self._shutdown_socket()
            if raise_on_error:
                raise RemoteDeviceError(f"设备端拒绝: {reason}")
            return False

        if int(meta.get("protocol", 0)) != PROTOCOL_VERSION:
            self._shutdown_socket()
            message = f"协议版本不一致: 设备端 {meta.get('protocol')} / 程序端 {PROTOCOL_VERSION}"
            if raise_on_error:
                raise RemoteDeviceError(message)
            print(f"[remote] {message}")
            return False

        self.hello = dict(meta)
        self.protocol = int(meta.get("protocol", 0))
        self.actual_width = int(meta.get("width") or 0)
        self.actual_height = int(meta.get("height") or 0)
        self.scrcpy_dir = meta.get("scrcpy_dir") or ""
        self.device_serial = meta.get("device_serial") or ""
        self.device_touch_device = meta.get("touch_device") or ""
        return True

    def _ensure_connected(self):
        if self._closed:
            return False
        if self._connected.is_set() and self.sock is not None:
            return True
        for _ in range(self.reconnect_attempts):
            self.stats["reconnects"] += 1
            if self._connect():
                if self.verbose:
                    print(f"[remote] 已重连设备端 {self.endpoint}")
                if self.touch_stream is not None and self.touch_stream.started:
                    self.touch_stream.started = False  # 触发重新订阅
                return True
            time.sleep(0.2)
        return False

    def _reader_loop(self):
        sock = self.sock
        while not self._closed and sock is not None:
            try:
                message = recv_message(sock)
            except (OSError, ValueError) as exc:
                if not self._closed:
                    print(f"[remote] 读设备端失败: {exc}")
                break
            if message is None:
                break
            self._dispatch(message)
        self._connected.clear()
        self._fail_pending()

    def _dispatch(self, message):
        kind = message.get("kind")
        if kind == "touch":
            if self.touch_stream is not None:
                for item in message.get("lines") or []:
                    try:
                        host_ts, line = item[0], item[1]
                    except (TypeError, IndexError):
                        continue
                    self.touch_stream.feed_line(line, float(host_ts))
            return

        request_id = message.get("id")
        if request_id is None:
            return
        with self._pending_lock:
            entry = self._pending.get(request_id)
            if entry is not None:
                entry["message"] = message
                entry["event"].set()
            self._pending_lock.notify_all()

    def _fail_pending(self):
        with self._pending_lock:
            for entry in self._pending.values():
                entry["error"] = "connection lost"
                entry["event"].set()
            self._pending_lock.notify_all()

    def _request(self, kind, meta=None, blob=b"", timeout=None):
        """发一条请求并等对应响应；失败抛 `RemoteDeviceError`。"""
        if self.sock is None:
            raise RemoteDeviceError("未连接设备端")

        timeout = self.timeout_sec if timeout is None else timeout
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            entry = {"event": threading.Event(), "message": None, "error": None}
            with self._pending_lock:
                self._pending[request_id] = entry

            payload = dict(meta or {})
            payload["id"] = request_id
            payload["kind"] = kind
            try:
                send_message(self.sock, payload, blob, lock=self._send_lock)
            except OSError as exc:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise RemoteDeviceError(f"发送 {kind} 失败: {exc}") from exc

            self.stats["requests"] += 1
            self.stats["bytes_out"] += len(blob)
            finished = entry["event"].wait(timeout)
            with self._pending_lock:
                self._pending.pop(request_id, None)

            if not finished:
                self.stats["errors"] += 1
                raise RemoteDeviceError(f"{kind} 超时（{timeout:.1f}s）")
            if entry["error"]:
                self.stats["errors"] += 1
                raise RemoteDeviceError(f"{kind} 失败: {entry['error']}")

            message = entry["message"]
            if message is None:
                self.stats["errors"] += 1
                raise RemoteDeviceError(f"{kind} 没有响应")
            self.stats["bytes_in"] += len(message.blob)
            return message.meta, message.blob

    def _shutdown_socket(self):
        sock, self.sock = self.sock, None
        self._connected.clear()
        if sock is not None:
            try:
                sock.shutdown(2)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # AndroidTool 接口
    # ------------------------------------------------------------------ #
    def show_scrcpy(self):
        if self.verbose:
            print(f"[remote] scrcpy 由设备端负责，本机不启动投屏窗口")
        return True

    def show_action_log(self):
        return None

    def hidden_action_log(self):
        return None

    def get_device_resolution(self):
        return self.actual_height, self.actual_width

    def screenshot_window(self):
        """取一帧（BGR ndarray）；失败返回 None，让 env 自己重试。"""
        if not self._ensure_connected():
            return None
        try:
            meta, blob = self._request(
                "shot", {"format": self.frame_format, "quality": self.frame_quality}
            )
        except RemoteDeviceError:
            return None
        if not meta.get("ok") or not blob:
            return None
        frame = self._decode_frame(meta, blob)
        if frame is not None:
            self.stats["frames"] += 1
        return frame

    def _decode_frame(self, meta, blob):
        frame_format = str(meta.get("format") or self.frame_format).lower()
        if frame_format == "raw":
            shape = meta.get("shape") or []
            try:
                height, width, channels = (int(shape[0]), int(shape[1]), int(shape[2]))
            except (IndexError, TypeError, ValueError):
                return None
            expected = height * width * channels
            if len(blob) != expected:
                print(f"[remote] raw 帧长度异常: {len(blob)} != {expected}")
                return None
            return np.frombuffer(blob, dtype=np.uint8).reshape(height, width, channels).copy()

        buffer = np.frombuffer(blob, dtype=np.uint8)
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)

    def execute_action_burst(self, action, suppress_attack=False, suppress_info=False,
                             burst_duration_ms=None, human_control=False):
        """把一步动作交给设备端执行，返回 `(注入普攻, 注入信息, 注入移动)`。"""
        if not self._ensure_connected():
            return False, False, False
        try:
            meta, _ = self._request("act", {
                "action": [int(v) for v in action],
                "suppress_attack": bool(suppress_attack),
                "suppress_info": bool(suppress_info),
                "burst_ms": int(burst_duration_ms) if burst_duration_ms is not None else None,
                "human_control": bool(human_control),
            })
        except RemoteDeviceError:
            return False, False, False
        result = meta.get("result") or [False, False, False]
        return bool(result[0]), bool(result[1]), bool(result[2])

    def stop_persistent_move(self):
        if not self._ensure_connected():
            return False
        try:
            self._request("stop", timeout=min(1.0, self.timeout_sec))
        except RemoteDeviceError:
            return False
        return True

    def _exec_legacy(self, op, params):
        """`--action_burst_enabled false` 时 env 会直接调 action_*，这里转成同步 RPC。"""
        future = concurrent.futures.Future()
        ok = False
        if self._ensure_connected():
            try:
                meta, _ = self._request("exec", {"op": op, "params": params or {}})
                ok = bool(meta.get("value", meta.get("ok")))
            except RemoteDeviceError:
                ok = False
        future.set_result(ok)
        return future

    def action_move(self, params):
        return self._exec_legacy("move", params)

    def action_attack(self, params):
        return self._exec_legacy("attack", params)

    def action_info(self, params):
        return self._exec_legacy("info", params)

    # ------------------------------------------------------------------ #
    # 触摸与流量
    # ------------------------------------------------------------------ #
    def subscribe_touch(self, touch_device=""):
        if not self._ensure_connected():
            return False
        try:
            meta, _ = self._request(
                "touch_sub", {"device": touch_device or self.device_touch_device}
            )
        except RemoteDeviceError:
            return False
        return bool(meta.get("ok"))

    def unsubscribe_touch(self):
        if self.sock is None:
            return False
        try:
            self._request("touch_unsub", timeout=min(1.0, self.timeout_sec))
        except RemoteDeviceError:
            return False
        return True

    def create_touch_stream(self, debug=False, debug_limit=40, touch_device=""):
        """给 `LiveHumanAssist` 用：返回一个由设备端喂数据的触摸流。"""
        stream = RemoteTouchStream(
            self,
            touch_device=touch_device or self.device_touch_device,
            debug=debug,
            debug_limit=debug_limit,
        )
        self.touch_stream = stream
        return stream

    def read_proc_net_dev(self):
        if not self._ensure_connected():
            return ""
        try:
            meta, _ = self._request("procnet")
        except RemoteDeviceError:
            return ""
        return meta.get("text") or ""

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.touch_stream is not None:
            try:
                self.touch_stream.stop()
            except Exception:  # noqa: BLE001 - 关闭阶段不打断
                pass
        else:
            try:
                self._request("touch_unsub", timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._request("close", timeout=0.5)
        except Exception:  # noqa: BLE001
            pass
        self._shutdown_socket()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)

    stop = close

    def __repr__(self):
        return (
            f"RemoteAndroidTool({self.endpoint}, connected={self._connected.is_set()}, "
            f"frames={self.stats['frames']}, errors={self.stats['errors']})"
        )


class RemoteTouchStream(HumanTouchStream):
    """`HumanTouchStream` 的网络版：解析/缓冲逻辑复用，只换数据来源。"""

    def __init__(self, tool, touch_device="", debug=False, debug_limit=40, capacity=4000):
        super().__init__(
            adb_path="",
            device_serial=getattr(tool, "device_serial", ""),
            touch_device=touch_device,
            capacity=capacity,
            auto_start=False,
            debug=bool(debug),
            debug_limit=int(debug_limit),
        )
        self.tool = tool

    def start(self):
        ok = self.tool.subscribe_touch(self.touch_device)
        self.started = bool(ok)
        if not ok:
            self.last_error = self.tool.stats.get("last_touch_error") or "设备端拒绝订阅触摸"
        return self.started

    def _read_loop(self):
        return None

    def ensure_alive(self):
        if self.closed:
            return False
        if not self.started:
            return self.start()
        return True

    def stop(self):
        try:
            self.tool.unsubscribe_touch()
        except Exception:  # noqa: BLE001 - 关闭阶段不打断
            pass
        super().stop()


class RemoteProcNetDevTrafficProbe(AdbProcNetDevTrafficProbe):
    """流量探针的网络版：设备端读 `/proc/net/dev`，程序端用同一套解析。"""

    def __init__(self, tool, interfaces=None, timeout_sec=1.0):
        super().__init__(
            adb_path="",
            device_serial=getattr(tool, "device_serial", ""),
            interfaces=interfaces,
            timeout_sec=timeout_sec,
        )
        self.tool = tool

    def _read_proc_net_dev_text(self):
        return self.tool.read_proc_net_dev() or ""

    def _read_proc_net_dev(self):
        return parse_proc_net_dev(self._read_proc_net_dev_text())
