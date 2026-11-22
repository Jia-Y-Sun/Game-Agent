"""设备端（双端模式）：本机只负责「截屏 + 注入动作 + 转发触摸/流量」。

它把 `AndroidTool` 的能力通过 TCP 暴露给另一台机器上的 `train.py`，所以：

* 游戏画面、adb、模拟器都留在这台机器；
* 视觉、策略、奖励、训练都在另一台机器；
* 训练用的张量不过网，只有「一帧图」和「一个动作」在传。

启动：

```bat
python device_agent.py --iphone_id 127.0.0.1:5555 --device_listen_port 5599
```

安全与兜底：

* 程序端断开、或 `--device_watchdog_sec` 秒内没有任何请求，会自动
  `stop_persistent_move()`，避免程序端崩了角色一直朝一个方向跑；
* `--device_token` 两端一致才接受连接（同网段广播/误连的简单防护）；
* 同一时刻只服务一个程序端。
"""

import socket
import subprocess
import threading
import time

import cv2
import numpy as np

from argparses import args
from remote_protocol import PROTOCOL_VERSION, recv_message, send_message


def _lan_ip():
    """取本机对外网卡的地址，只用来打印提示。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class DeviceServer:
    """把 AndroidTool 包装成一个请求-响应 + 触摸推送的服务。"""

    def __init__(self, tool, host, port, token="", watchdog_sec=10.0,
                 frame_format="jpeg", frame_quality=82, getevent_enabled=True,
                 procnet_reader=None):
        self.tool = tool
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.watchdog_sec = max(0.0, float(watchdog_sec))
        self.frame_format = str(frame_format or "jpeg").lower()
        self.frame_quality = int(frame_quality)
        self.getevent_enabled = bool(getevent_enabled)
        #: 读 `/proc/net/dev` 的方式；测试里可以注入一个假实现
        self.procnet_reader = procnet_reader

        self.ready = threading.Event()
        self.bound_port = self.port
        self._client = None
        self._client_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._last_request = time.time()
        self._watchdog_fired = False
        self._stop = threading.Event()

        self._touch_device = ""
        self._pump_thread = None
        self._pump_stop = threading.Event()
        self._pump_proc = None

        self.stats = {"requests": 0, "frames": 0, "actions": 0, "bytes_out": 0, "clients": 0}

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def serve_forever(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(1)
        server.settimeout(0.5)
        self.bound_port = server.getsockname()[1]
        self.ready.set()

        print("=" * 68)
        print(f"[device] 监听 {self.host}:{self.bound_port}")
        print(f"[device] 分辨率 {self.tool.actual_width}x{self.tool.actual_height}，"
              f"帧 {self.frame_format}@{self.frame_quality}，"
              f"getevent={'on' if self.getevent_enabled else 'off'}")
        print(f"[device] 程序端启动参数：--device_source remote "
              f"--device_endpoint {_lan_ip()}:{self.bound_port}")
        if not self.token:
            print("[device] 提示：没有设置 --device_token，同网段任何人都能连；"
                  "只在可信网络里这样用")
        print("[device] Ctrl+C 停止")
        print("=" * 68)

        watchdog = threading.Thread(target=self._watchdog_loop, name="device-watchdog", daemon=True)
        watchdog.start()

        try:
            while not self._stop.is_set():
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._serve_client(conn, addr)
        finally:
            self._stop.set()
            self._stop_touch_pump()
            try:
                self.tool.stop_persistent_move()
            except Exception:  # noqa: BLE001 - 退出阶段不打断
                pass
            server.close()
            print(f"[device] 已停止，统计 {self.stats}")

    def start_background(self, timeout_sec=5.0):
        """在后台线程里起服务，返回实际监听端口（测试/嵌入式用）。"""
        thread = threading.Thread(target=self.serve_forever, name="device-server", daemon=True)
        thread.start()
        self.ready.wait(timeout_sec)
        return self.bound_port

    def shutdown(self):
        self._stop.set()
        self.ready.wait(0.1)

    def _serve_client(self, conn, addr):
        with self._client_lock:
            if self._client is not None:
                try:
                    send_message(conn, {"kind": "busy", "ok": False,
                                        "error": "已有程序端连接，拒绝第二个"})
                except OSError:
                    pass
                conn.close()
                print(f"[device] 拒绝第二个连接 {addr}")
                return
            self._client = conn

        self.stats["clients"] += 1
        self._last_request = time.time()
        self._watchdog_fired = False
        print(f"[device] 程序端已连接 {addr}")

        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        try:
            while not self._stop.is_set():
                try:
                    message = recv_message(conn)
                except (OSError, ValueError) as exc:
                    print(f"[device] 读请求失败: {exc}")
                    break
                if message is None:
                    break
                self._last_request = time.time()
                self._watchdog_fired = False
                self.stats["requests"] += 1
                kind = message.get("kind")
                self._handle(conn, message)
                if kind == "close":
                    break
        finally:
            with self._client_lock:
                if self._client is conn:
                    self._client = None
            self._stop_touch_pump()
            try:
                self.tool.stop_persistent_move()
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.close()
            except OSError:
                pass
            print(f"[device] 程序端断开 {addr}，已松摇杆")

    # ------------------------------------------------------------------ #
    # 请求分发
    # ------------------------------------------------------------------ #
    def _reply(self, conn, request, meta=None, blob=b""):
        payload = dict(meta or {})
        payload["id"] = request.get("id")
        payload.setdefault("kind", f"{request.get('kind')}_res")
        try:
            send_message(conn, payload, blob, lock=self._send_lock)
            self.stats["bytes_out"] += len(blob)
        except OSError as exc:
            print(f"[device] 回包失败: {exc}")

    def _handle(self, conn, request):
        kind = str(request.get("kind") or "")
        handler = {
            "hello": self._on_hello,
            "shot": self._on_shot,
            "act": self._on_act,
            "stop": self._on_stop,
            "exec": self._on_exec,
            "procnet": self._on_procnet,
            "touch_sub": self._on_touch_sub,
            "touch_unsub": self._on_touch_unsub,
            "ping": self._on_ping,
        }.get(kind)

        if handler is None:
            if kind != "close":
                self._reply(conn, request, {"ok": False, "error": f"未知请求 {kind}"})
            else:
                self._reply(conn, request, {"ok": True})
            return

        try:
            handler(conn, request)
        except Exception as exc:  # noqa: BLE001 - 单条请求失败不能拖垮服务
            print(f"[device] 处理 {kind} 出错: {exc}")
            self._reply(conn, request, {"ok": False, "error": str(exc)[:200]})

    def _on_hello(self, conn, request):
        if self.token and str(request.get("token") or "") != self.token:
            self._reply(conn, request, {"ok": False, "error": "token 不匹配"})
            print("[device] 拒绝连接：token 不匹配")
            return
        self._reply(conn, request, {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "width": self.tool.actual_width,
            "height": self.tool.actual_height,
            "scrcpy_dir": getattr(self.tool, "scrcpy_dir", ""),
            "device_serial": getattr(self.tool, "device_serial", ""),
            "touch_device": getattr(args, "touch_device", "") or "",
            "frame_format": self.frame_format,
            "frame_quality": self.frame_quality,
        })

    def _on_shot(self, conn, request):
        frame_format = str(request.get("format") or self.frame_format).lower()
        quality = int(request.get("quality") or self.frame_quality)
        started = time.perf_counter()
        image = self.tool.screenshot_window()
        grab_ms = (time.perf_counter() - started) * 1000
        if image is None or getattr(image, "size", 0) == 0:
            self._reply(conn, request, {"ok": False, "error": "截图失败", "grab_ms": round(grab_ms, 2)})
            return

        started = time.perf_counter()
        meta = {"ok": True, "format": frame_format, "grab_ms": round(grab_ms, 2)}
        if frame_format == "raw":
            array = np.ascontiguousarray(image)
            meta["shape"] = list(array.shape)
            blob = array.tobytes()
        elif frame_format == "png":
            ok, buffer = cv2.imencode(".png", image)
            blob = buffer.tobytes() if ok else b""
        else:
            ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            blob = buffer.tobytes() if ok else b""

        meta["encode_ms"] = round((time.perf_counter() - started) * 1000, 2)
        meta["bytes"] = len(blob)
        self.stats["frames"] += 1
        self._reply(conn, request, meta, blob)

    def _on_act(self, conn, request):
        action = [int(v) for v in (request.get("action") or [0] * 8)]
        started = time.perf_counter()
        result = self.tool.execute_action_burst(
            action,
            suppress_attack=bool(request.get("suppress_attack")),
            suppress_info=bool(request.get("suppress_info")),
            burst_duration_ms=request.get("burst_ms"),
            human_control=bool(request.get("human_control")),
        )
        try:
            result = [bool(v) for v in result]
        except TypeError:
            result = [False, False, False]
        self.stats["actions"] += 1
        self._reply(conn, request, {
            "ok": True,
            "result": result,
            "ms": round((time.perf_counter() - started) * 1000, 2),
        })

    def _on_stop(self, conn, request):
        self.tool.stop_persistent_move()
        self._reply(conn, request, {"ok": True})

    def _on_exec(self, conn, request):
        op = str(request.get("op") or "")
        params = request.get("params") or {}
        caller = {
            "move": self.tool.action_move,
            "attack": self.tool.action_attack,
            "info": self.tool.action_info,
        }.get(op)
        if caller is None:
            self._reply(conn, request, {"ok": False, "error": f"未知操作 {op}"})
            return
        future = caller(params)
        value = bool(future.result(timeout=5)) if future is not None else False
        self._reply(conn, request, {"ok": True, "value": value})

    def _on_procnet(self, conn, request):
        text = ""
        error = ""
        try:
            if self.procnet_reader is not None:
                text = self.procnet_reader() or ""
            else:
                completed = subprocess.run(
                    [f"{self.tool.scrcpy_dir}/adb", "-s", self.tool.device_serial,
                     "shell", "cat", "/proc/net/dev"],
                    capture_output=True,
                    text=True,
                    timeout=float(getattr(args, "traffic_probe_timeout_sec", 1.0) or 1.0),
                )
                if completed.returncode == 0:
                    text = completed.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)[:120]
        self._reply(conn, request, {"ok": bool(text), "text": text, "error": error})

    def _on_touch_sub(self, conn, request):
        if not self.getevent_enabled:
            self._reply(conn, request, {"ok": False, "error": "设备端已关闭 getevent 转发"})
            return
        self._touch_device = str(request.get("device") or getattr(args, "touch_device", "") or "")
        ok = self._start_touch_pump()
        self._reply(conn, request, {"ok": ok, "device": self._touch_device})

    def _on_touch_unsub(self, conn, request):
        self._stop_touch_pump()
        self._reply(conn, request, {"ok": True})

    def _on_ping(self, conn, request):
        self._reply(conn, request, {"ok": True, "ts": time.time()})

    # ------------------------------------------------------------------ #
    # 触摸转发
    # ------------------------------------------------------------------ #
    def _start_touch_pump(self):
        if self._pump_thread is not None and self._pump_thread.is_alive():
            return True
        command = [f"{self.tool.scrcpy_dir}/adb", "-s", self.tool.device_serial,
                   "shell", "getevent", "-lt"]
        if self._touch_device:
            command.append(self._touch_device)
        try:
            self._pump_proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except (OSError, ValueError) as exc:
            print(f"[device] 启动 getevent 失败: {exc}")
            return False

        self._pump_stop.clear()
        self._pump_thread = threading.Thread(target=self._touch_pump_loop, name="getevent-pump", daemon=True)
        self._pump_thread.start()
        time.sleep(0.2)
        if self._pump_proc.poll() is not None:
            print(f"[device] getevent 立即退出 returncode={self._pump_proc.poll()}；"
                  f"请先跑 touch_event_diagnose.py 确认触摸设备")
            self._pump_thread = None
            return False
        print(f"[device] 开始转发触摸事件 {self._touch_device or '(所有设备)'}")
        return True

    def _stop_touch_pump(self):
        self._pump_stop.set()
        process, self._pump_proc = self._pump_proc, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass
        thread, self._pump_thread = self._pump_thread, None
        if thread is not None:
            thread.join(timeout=1)

    def _touch_pump_loop(self):
        """按 ~30ms 或 8 行一批发送，避免每行一个小包。"""
        process = self._pump_proc
        if process is None or process.stdout is None:
            return
        batch = []
        last_flush = time.perf_counter()
        while not self._pump_stop.is_set():
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.01)
                continue
            batch.append([time.time(), line.rstrip("\r\n")])
            now = time.perf_counter()
            if len(batch) >= 8 or (now - last_flush) >= 0.03:
                self.push_touch_lines(batch)
                batch = []
                last_flush = now
        if batch:
            self.push_touch_lines(batch)

    def push_touch_lines(self, lines):
        """把一批 `[时间戳, getevent 原始行]` 推给程序端（触摸泵与测试共用）。"""
        with self._client_lock:
            conn = self._client
        if conn is None or not lines:
            return False
        try:
            send_message(conn, {"kind": "touch", "lines": list(lines)}, lock=self._send_lock)
        except OSError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # 看门狗
    # ------------------------------------------------------------------ #
    def _watchdog_loop(self):
        while not self._stop.is_set():
            time.sleep(0.5)
            if self.watchdog_sec <= 0:
                continue
            with self._client_lock:
                connected = self._client is not None
            if not connected or self._watchdog_fired:
                continue
            idle = time.time() - self._last_request
            if idle < self.watchdog_sec:
                continue
            self._watchdog_fired = True
            print(f"[device] 已 {idle:.0f}s 没收到请求，自动松摇杆（程序端可能卡住/断线）")
            try:
                self.tool.stop_persistent_move()
            except Exception as exc:  # noqa: BLE001
                print(f"[device] 松摇杆失败: {exc}")


def main():
    # 延迟导入：`AndroidTool` 依赖 win32gui / PyQt5，只在真正当设备端时加载
    from android_tool import AndroidTool

    print(f"[device] 设备 {getattr(args, 'iphone_id', '?')}，scrcpy 目录 {getattr(args, 'scrcpy_dir', '?')}")
    try:
        tool = AndroidTool()
    except Exception as exc:  # noqa: BLE001 - 设备没起来时给出可读信息
        print(f"[device] 初始化 AndroidTool 失败: {exc}")
        print("[device] 先确认 adb devices 能看到设备（状态必须是 device）")
        return 1

    if getattr(args, "device_show_scrcpy", True):
        # 双端模式下程序端不会启动投屏，截图要靠这个窗口
        print("[device] 启动 scrcpy 投屏窗口（截图源）…")
        tool.show_scrcpy()
        time.sleep(2.0)

    server = DeviceServer(
        tool,
        host=args.device_listen_host,
        port=args.device_listen_port,
        token=args.device_token,
        watchdog_sec=args.device_watchdog_sec,
        frame_format=args.device_frame_format,
        frame_quality=args.device_frame_quality,
        getevent_enabled=args.device_getevent_enabled,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[device] 收到 Ctrl+C，正在退出")
    finally:
        try:
            tool.stop_persistent_move()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
