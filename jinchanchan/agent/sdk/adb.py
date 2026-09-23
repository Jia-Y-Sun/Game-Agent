# -*- coding: utf-8 -*-
"""ADB 连接 + OCR 识别层(移植自 Sunflower adb.py, 适配雷电模拟器/easyocr)

改动点(相对 Sunflower):
  - OCR 引擎 PaddleOCR -> easyocr(与 jinchanchan 现有 ocr_helper 一致, 免装 paddle)
  - easyocr 为同步接口, 通过 asyncio.to_thread 包装避免阻塞事件循环
  - 默认端口 5555(雷电模拟器), 连接失败自动扫描本地 adb 端口(5555 起, 兼容多开)
  - get_screen_size 解析兼容 "Physical size / Override size" 两种输出
"""

import asyncio
import re
import random
import threading

import cv2
import numpy
import psutil
from numpy import ndarray
from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.adb_device_async import AdbDeviceTcpAsync

from agent.sdk.base import BoundingBox, OcrResult
from utils.logger import logger

# easyocr 置信度阈值(paddle 默认 0.85 对 easyocr 偏高, 下调后靠下游名字表兜底)
DEFAULT_OCR_CONFIDENCE = 0.5


class EasyOcrEngine:
    """easyocr 引擎封装: 后台线程加载模型, 提供同步 ocr 接口"""

    def __init__(self, languages=("ch_sim", "en")):
        self._reader = None
        self._ready = threading.Event()
        self._load_error = None
        self._lock = threading.Lock()   # easyocr 非线程安全, 串行化并发识别
        self._thread = threading.Thread(
            target=self._load, args=(languages,), daemon=True)
        self._thread.start()

    def _load(self, languages):
        try:
            import easyocr
            logger.info("[SDK] 正在初始化 easyocr 模型(首次运行需下载, 约 100MB)...")
            self._reader = easyocr.Reader(list(languages), gpu=False, verbose=False)
            logger.info("[SDK] easyocr 就绪")
        except Exception as e:  # noqa: BLE001
            self._load_error = e
            logger.error(f"[SDK] easyocr 初始化失败: {e}")
        finally:
            self._ready.set()

    def wait_ready(self, timeout=None):
        self._ready.wait(timeout)
        if self._load_error:
            raise self._load_error
        return self._ready.is_set() and self._reader is not None

    def ocr(self, image, confidence=DEFAULT_OCR_CONFIDENCE):
        """识别图片文字, 返回 [OcrResult] 或 None"""
        if not self.wait_ready():
            return None
        if image is None or image.size == 0:
            return None

        # easyocr 单图输入返回 [(box, text, conf), ...]
        with self._lock:
            results = self._reader.readtext(image)
        out = []
        for box, text, conf in results:
            if conf < confidence:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append(OcrResult(
                x=int(min(xs)), y=int(min(ys)),
                width=int(max(xs) - min(xs)), height=int(max(ys) - min(ys)),
                # 去除全部空白: easyocr 常在中文间插空格(如"注 销"),
                # 而名字表/关键词表都是无空格中文, 必须归一化才能匹配
                text="".join(str(text).split()),
            ))
        return out or None


class AdbOCR:
    """ADB 连接 + 截图 + 点击 + OCR 基础能力"""

    def __init__(self):
        self._device = None
        self.ocr_engine = None
        self._random = random.Random()
        self.connected_host = None
        self.connected_port = None
        self._adb_lock = asyncio.Lock()  # 串行化所有 ADB 操作(截图/点击), 防止并发任务互相干扰

    async def load(self, port, host="localhost", scan_if_fail=True, languages=("ch_sim", "en")):
        """连接设备(后台) + 初始化 OCR 引擎(后台), 两者并行"""
        task1 = asyncio.create_task(asyncio.to_thread(self.ensure_ocr_engine, languages))
        task2 = asyncio.create_task(self.connect_only(port=port, host=host, scan_if_fail=scan_if_fail))

        await task1
        await task2

    async def connect_only(self, port, host="localhost", scan_if_fail=True):
        """只做设备连接, 失败抛 ConnectionError(供启动重试场景使用)"""
        await self._connect_device(port=port, host=host, scan_if_fail=scan_if_fail)
        if self.connected_port is None:
            raise ConnectionError(
                f"无法连接设备 {host}:{port}, 请确认模拟器已启动且开启了 ADB 调试")

    def ensure_ocr_engine(self, languages=("ch_sim", "en")):
        """确保 OCR 引擎已初始化(重复调用不重复加载模型)"""
        if self.ocr_engine is None:
            self.ocr_engine = EasyOcrEngine(languages)

    async def _connect_device(self, port, host="localhost", scan_if_fail=True):
        if self._device is not None:
            return

        device = AdbDeviceTcpAsync(host=host, port=port, default_transport_timeout_s=9)
        logger.debug(f"[SDK] 尝试连接设备 {host}:{port} ...")

        try:
            if await device.connect():
                self._device = device
                self.connected_host, self.connected_port = host, port
                logger.info(f"[SDK] 已连接设备 {host}:{port}")
                return
        except OSError:
            if scan_if_fail:
                logger.warning(f"[SDK] 连接 {host}:{port} 失败, 尝试扫描本地设备")
            else:
                logger.error(f"[SDK] 连接 {host}:{port} 失败")

        # 连接失败时扫描本地 adb 端口(雷电多开: 5555/5557/5559...)
        if host == "localhost" and scan_if_fail:
            port_found = await asyncio.to_thread(self._scan_local_devices)
            if port_found:
                try:
                    device = AdbDeviceTcpAsync(host=host, port=port_found, default_transport_timeout_s=9)
                    if await device.connect():
                        self._device = device
                        self.connected_host, self.connected_port = host, port_found
                        logger.info(f"[SDK] 已连接扫描到的设备 {host}:{port_found}")
                        return
                except OSError:
                    pass
                logger.error("[SDK] 连接失败, 请确认模拟器已开启 ADB 调试")

    @staticmethod
    def _scan_local_devices():
        """扫描本机 LISTEN 的 adb 端口(>=5555), 返回第一个可连接的端口"""
        logger.info("[SDK] 扫描本地 adb 端口...")
        try:
            connections = [
                c for c in psutil.net_connections("tcp4")
                if c.laddr.port >= 5555 and c.status == "LISTEN"
            ]
        except psutil.AccessDenied:
            logger.error("[SDK] 端口扫描权限不足, 请以管理员身份运行")
            return None

        for conn in sorted(connections, key=lambda c: c.laddr.port):
            port = conn.laddr.port
            try:
                adb_device = AdbDeviceTcp("localhost", port=port, default_transport_timeout_s=0.5)
                if adb_device.connect(read_timeout_s=0.5):
                    adb_device.close()
                    return port
            except Exception:  # noqa: BLE001
                continue

        logger.warning("[SDK] 未发现本地设备")
        return None

    def _init_ocr_engine(self, languages):
        self.ensure_ocr_engine(languages)

    def is_connected(self):
        return self._device is not None and self._device.available

    async def close(self):
        if self._device is not None:
            try:
                await self._device.close()
            except Exception:  # noqa: BLE001
                pass
            self._device = None

    # ---------- 设备信息 ----------

    async def get_screen_size(self):
        """返回 (width, height) 设备分辨率"""
        if not self.is_connected():
            return 0, 0
        output = await self._device.shell("wm size")
        matches = re.findall(r"(\d+)x(\d+)", output)
        if matches:
            w, h = matches[-1]
            return int(w), int(h)
        return 0, 0

    async def get_screen_density(self):
        output = await self._device.shell("wm density")
        matches = re.findall(r"(\d+)", output)
        return int(matches[-1]) if matches else 0

    async def set_screen_size(self, width, height):
        await self._device.shell(f"wm size {width}x{height}")

    async def set_screen_density(self, density):
        await self._device.shell(f"wm density {density}")

    async def get_memory(self):
        """设备总内存, 单位 MB"""
        output = await self._device.shell("cat /proc/meminfo | grep MemTotal")
        return int(output.split()[1]) // 1024

    # ---------- 截图 / 输入 ----------

    async def get_screen(self):
        """截取设备屏幕(灰度图), 失败自动重连一次, 仍失败返回 None"""
        if not self.is_connected():
            await self._try_reconnect()
            return None
        async with self._adb_lock:
            try:
                image_bytes = await self._device.exec_out("screencap -p", decode=False)
                raw_image = numpy.frombuffer(image_bytes, dtype=numpy.uint8)
                return cv2.imdecode(raw_image, cv2.IMREAD_GRAYSCALE)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[SDK] 截图失败: {e}, 尝试重连")
                self._device = None
                await self._try_reconnect()
                return None

    async def _try_reconnect(self):
        """断线后尝试重建连接(模拟器重启/游戏重开后自动恢复)"""
        if not self.connected_port:
            return
        try:
            device = AdbDeviceTcpAsync(host=self.connected_host or "localhost",
                                       port=self.connected_port,
                                       default_transport_timeout_s=9)
            if await device.connect():
                self._device = device
                logger.info(f"[SDK] 已重连设备 {self.connected_host}:{self.connected_port}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[SDK] 重连失败: {e}")

    async def click(self, x, y):
        """点击(短 swipe 模拟, 带随机时长), 失败仅告警不抛异常"""
        try:
            async with self._adb_lock:
                await self._device.shell(
                    f"input swipe {x} {y} {x} {y} {self._random.randint(60, 120)}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SDK] 点击失败: {e}")
            self._device = None
            await self._try_reconnect()

    async def swipe(self, x1, y1, x2, y2, duration=None):
        try:
            async with self._adb_lock:
                await self._device.shell(
                    f"input swipe {x1} {y1} {x2} {y2} "
                    f"{self._random.randint(60, 120) if not duration else duration}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SDK] 滑动失败: {e}")
            self._device = None
            await self._try_reconnect()

    async def go_back(self):
        try:
            async with self._adb_lock:
                await self._device.shell("input keyevent 4")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SDK] 返回键失败: {e}")
            self._device = None
            await self._try_reconnect()

    # ---------- OCR ----------

    async def get_screen_text(self, detect_area=None, confidence=DEFAULT_OCR_CONFIDENCE):
        """截屏并识别文字(可选限定区域), 返回 [OcrResult] 或 None"""
        if not self.is_connected():
            logger.warning("[SDK] 未连接设备, 无法 OCR")
            return None

        screen = await self.get_screen()
        if screen is None:
            return None
        return await self.get_image_text(screen, detect_area, confidence)

    async def get_image_text(self, image, detect_area=None, confidence=DEFAULT_OCR_CONFIDENCE):
        """识别图片文字(可选限定区域), 返回 [OcrResult] 或 None

        detect_area 未缩放的基准坐标需由调用方处理, 这里按设备像素直接裁剪。
        """
        if image is None or image.size == 0:
            return None

        if detect_area is not None:
            image = image[detect_area.y:detect_area.y + detect_area.height,
                          detect_area.x:detect_area.x + detect_area.width]

        if self.ocr_engine is None:
            logger.warning("[SDK] OCR 引擎未初始化")
            return None

        results = await asyncio.to_thread(self.ocr_engine.ocr, image, confidence)
        if not results:
            logger.debug("[SDK] 未识别到文字")
            return None

        # 区域裁剪时把坐标平移到全屏坐标系
        if detect_area is not None:
            for r in results:
                r.x += detect_area.x
                r.y += detect_area.y
        return results
