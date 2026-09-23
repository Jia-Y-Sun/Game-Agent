# -*- coding: utf-8 -*-
"""问号标志识别 — 对局内掉落物/宝箱"?"圆圈检测

用用户提供的两张问号截图(images/buttons/qm_new_1.png, qm_new_2.png)做
多尺度模板匹配, 识别到返回设备坐标, 供主循环点击抓取。

匹配策略:
  - 模板带透明通道, 用 alpha 作掩码参与匹配
  - 多尺度 [0.6, 0.8, 1.0, 1.25, 1.5, 1.75] 适配未知截图来源的分辨率
  - 全屏匹配, 阈值默认 0.62
"""

import os

import cv2
import numpy

from utils.logger import logger

QM_TEMPLATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "images", "buttons", "qm_new_1.png"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "images", "buttons", "qm_new_2.png"),
]

QM_SCALES = [0.6, 0.8, 1.0, 1.25, 1.5, 1.75]
QM_THRESHOLD = 0.62


class QuestionMarkDetector:
    def __init__(self):
        self._templates = []
        for path in QM_TEMPLATES:
            if not os.path.exists(path):
                logger.warning(f"[问号] 模板不存在: {path}")
                continue
            data = numpy.fromfile(path, dtype=numpy.uint8)   # 兼容中文路径
            img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
            if img is None:
                logger.warning(f"[问号] 模板读取失败: {path}")
                continue
            gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY) \
                if img.ndim == 3 and img.shape[2] >= 3 else img
            mask = None
            if img.ndim == 3 and img.shape[2] == 4:
                alpha = img[:, :, 3]
                # 只有"部分透明"的模板才用掩码: OpenCV 对全 255 掩码做
                # TM_CCOEFF_NORMED 会返回 NaN(实测), 导致满屏假阳性
                if alpha.min() < 255 and alpha.max() > 0:
                    mask = alpha
            self._templates.append((os.path.basename(path), gray, mask))

    @property
    def ready(self):
        return len(self._templates) > 0

    async def find(self, sdk):
        """截屏并匹配问号, 返回 (x, y, 模板名, 置信度) 或 None"""
        if not self.ready:
            return None
        screen = await sdk.get_screen()
        if screen is None or screen.size == 0:
            return None
        best = None  # (conf, x, y, name)
        for name, template, mask in self._templates:
            t_h, t_w = template.shape[:2]
            for sc in QM_SCALES:
                tw, th = max(int(t_w * sc), 5), max(int(t_h * sc), 5)
                if tw > screen.shape[1] or th > screen.shape[0]:
                    continue
                tt = cv2.resize(template, (tw, th), interpolation=cv2.INTER_LINEAR)
                mm = cv2.resize(mask, (tw, th), interpolation=cv2.INTER_NEAREST) \
                    if mask is not None else None
                result = cv2.matchTemplate(screen, tt, cv2.TM_CCOEFF_NORMED, mask=mm)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                # 过滤 NaN/inf(掩码匹配数值怪癖), 只信有限值
                if not numpy.isfinite(max_val):
                    continue
                if max_val >= QM_THRESHOLD and (best is None or max_val > best[0]):
                    cx = max_loc[0] + tw // 2
                    cy = max_loc[1] + th // 2
                    best = (max_val, cx, cy, name)
        if best:
            logger.info(f"[问号] 识别到 {best[3]} 置信度={best[0]:.2f} 坐标=({best[1]},{best[2]})")
            return best[1], best[2], best[3], best[0]
        return None
