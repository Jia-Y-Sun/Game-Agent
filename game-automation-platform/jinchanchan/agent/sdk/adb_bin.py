# -*- coding: utf-8 -*-
"""ADB 二进制传输层 — 王者荣耀(wzry)的 PC 端连接安卓端形式

与 agent/sdk/adb.py 的 adb_shell(TCP 直连模拟器 5555)不同, 此形式:
  - 调用 adb 可执行文件(subprocess), 与 wzry/android_tool.py 完全一致
  - 用设备序列号(serial)标识设备: USB 真机 / 模拟器(如 emulator-5554)通用
  - 截图: `adb exec-out screencap -p` 直读 stdout,
          失败降级 远程文件+pull, 并含 Windows CRLF 修复(参考 wzry _decode_screenshot_bytes)
  - 输入: `adb shell input swipe`(点击=同点短 swipe) / keyevent
  - 分辨率: `wm size` + 横竖屏归一化(宽=较大值, 与 wzry get_device_resolution 一致)
"""

import os
import random
import re
import subprocess
import tempfile

import cv2
import numpy

from utils.logger import logger


class AdbBinaryTransport:
    def __init__(self, adb_path="adb", serial=None, timeout=10):
        self.adb_path = adb_path
        self.serial = serial          # 指定设备; None 时自动选 adb devices 里第一个
        self.timeout = timeout
        self._random = random.Random()
        self._connected_serial = None

    # ---------- 发现 / 连接 ----------

    @staticmethod
    def list_devices(adb_path="adb"):
        """`adb devices` 返回在线设备 [(serial, state), ...]"""
        try:
            out = subprocess.check_output([adb_path, "devices"], timeout=8) \
                .decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
        devices = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append((parts[0], parts[1]))
        return devices

    def connect(self):
        """确认目标设备在线; 未指定 serial 时自动选第一个在线设备"""
        devices = self.list_devices(self.adb_path)
        if self.serial:
            if any(d[0] == self.serial for d in devices):
                self._connected_serial = self.serial
                return True
            return False
        if devices:
            self._connected_serial = devices[0][0]
            return True
        return False

    def is_connected(self):
        if not self._connected_serial:
            return False
        return any(d[0] == self._connected_serial for d in self.list_devices(self.adb_path))

    @property
    def connected_serial(self):
        return self._connected_serial

    # ---------- 基础 adb 调用 ----------

    def _cmd(self, *args):
        cmd = [self.adb_path]
        if self._connected_serial:
            cmd += ["-s", self._connected_serial]
        cmd += list(args)
        return cmd

    def shell(self, shell_cmd, timeout=None):
        try:
            result = subprocess.run(
                self._cmd("shell", shell_cmd),
                capture_output=True, timeout=timeout or self.timeout)
            return result.returncode == 0, result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"[ADB-BIN] shell 执行失败: {exc}")
            return False, b""

    def get_screen_size(self):
        """返回横屏序 (width, height): wzry 形式, 部分设备 wm size 报竖屏, 取 max 为宽"""
        ok, out = self.shell("wm size")
        if not ok:
            return 0, 0
        text = out.decode("utf-8", "replace")
        matches = re.findall(r"(\d+)x(\d+)", text)
        if not matches:
            return 0, 0
        w, h = map(int, matches[-1])
        return max(w, h), min(w, h)

    # ---------- 截图(参考 wzry 的降级链) ----------

    @staticmethod
    def _decode(data, source):
        """解码 PNG 字节流: 截断到 PNG 头 / 修复 Windows CRLF 后重试(参考 wzry)"""
        candidates = [data]
        idx = data.find(b"\x89PNG")
        if idx > 0:
            candidates.append(data[idx:])
        candidates.append(data.replace(b"\r\r\n", b"\r\n"))
        candidates.append(data.replace(b"\r\n", b"\n"))
        for c in candidates:
            img = cv2.imdecode(numpy.frombuffer(c, numpy.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                return img
        logger.warning(f"[ADB-BIN] 截图解码失败({source}), bytes={len(data)}")
        return None

    def screenshot(self):
        """截取设备屏幕(灰度), 失败返回 None"""
        # 方式 1: exec-out 直读 stdout
        try:
            result = subprocess.run(self._cmd("exec-out", "screencap", "-p"),
                                    capture_output=True, timeout=self.timeout)
            if result.returncode == 0 and result.stdout:
                img = self._decode(result.stdout, "exec-out")
                if img is not None:
                    return img
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"[ADB-BIN] exec-out 截图失败: {exc}")

        # 方式 2: 远程文件 + pull
        remote = "/sdcard/jcc_ai_screen.png"
        local = None
        try:
            ok, _ = self.shell(f"screencap -p {remote}")
            if ok:
                fd, local = tempfile.mkstemp(prefix="jcc_ai_screen_", suffix=".png")
                os.close(fd)
                r = subprocess.run(self._cmd("pull", remote, local),
                                   capture_output=True, timeout=self.timeout)
                if r.returncode == 0:
                    with open(local, "rb") as f:
                        img = self._decode(f.read(), "remote-file")
                    if img is not None:
                        return img
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"[ADB-BIN] 远程文件截图失败: {exc}")
        finally:
            if local:
                try:
                    os.remove(local)
                except OSError:
                    pass
        return None

    # ---------- 输入(参考 wzry: 点击=同点短 swipe) ----------

    def tap(self, x, y):
        self.shell(f"input swipe {x} {y} {x} {y} {self._random.randint(60, 120)}")

    def swipe(self, x1, y1, x2, y2, duration=None):
        d = duration if duration else self._random.randint(60, 120)
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {d}")

    def keyevent(self, code):
        self.shell(f"input keyevent {code}")
