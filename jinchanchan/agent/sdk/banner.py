# -*- coding: utf-8 -*-
"""窄区域阶段识别 — 缩小 OCR 识别区(用户要求)

只识别两个窄条区域, 速度快(单条 OCR ~1 秒), 供对局内主循环使用:

  横幅条(顶部居中): x 30%~70%, y 0~30%
    - "准备阶段" / "战斗开始" 两个标志字样(屏幕靠上且居中)
    - 回合数 "X-Y" (选秀轮 X-4 推导) / 强化选择横幅"点击卡片!选择强化效果!"

  HUD 条(左下角): x 10%~28%, y 78%~96%
    - "购买经验"/"刷新"按钮文字 —— 备战环节全程可见, 横幅闪过没抓到时的兜底证据

识别不到横幅时若仍有回合号, 默认按战斗环节处理(战斗横幅一闪而过, 常错过)。
"""

import re

from agent.sdk.base import BoundingBox
from agent.sdk.utils import SdkUtils
from utils.logger import logger

ROUND_PATTERN = re.compile(r"(\d+)-(\d+)")

KIND_PREPARE = "prepare"
KIND_BATTLE = "battle"
KIND_AUGMENT = "augment"
KIND_CAROUSEL = "carousel"


class BannerDetector:
    def __init__(self, sdk):
        self.sdk = sdk

    def _banner_box(self):
        w, h = self.sdk.scaler.device_size
        return BoundingBox(int(w * 0.30), 0, int(w * 0.40), int(h * 0.30))

    def _hud_box(self):
        w, h = self.sdk.scaler.device_size
        return BoundingBox(int(w * 0.10), int(h * 0.78), int(w * 0.18), int(h * 0.18))

    @staticmethod
    def _find_round(texts):
        for t in texts:
            m = ROUND_PATTERN.search(t)
            if m:
                return m.group(0)
        return None

    async def detect(self):
        """识别当前对局内阶段, 返回 (kind, round_str)

        kind: prepare / battle / augment / carousel / None(不在对局内或过渡画面)
        """
        # 1) 顶部横幅窄条
        results = await self.sdk.get_screen_text(self._banner_box(), confidence=0.35)
        texts = [r.text for r in results] if results else []
        round_str = self._find_round(texts)

        # 强化选择横幅(含标点, 用子串判断)
        if any("点击卡片" in t for t in texts):
            return KIND_AUGMENT, round_str
        for t in texts:
            if SdkUtils.text_match(t, "战斗开始"):
                return KIND_BATTLE, round_str
        for t in texts:
            if SdkUtils.text_match(t, "准备阶段"):
                return KIND_PREPARE, round_str
        if round_str and round_str.split("-")[1] == "4":
            return KIND_CAROUSEL, round_str

        # 2) 左下 HUD 窄条: 备战全程可见的"购买经验/刷新"按钮
        hud = await self.sdk.get_screen_text(self._hud_box(), confidence=0.35)
        hud_texts = [r.text for r in hud] if hud else []
        for t in hud_texts:
            if SdkUtils.text_match(t, "购买经验") or SdkUtils.text_match(t, "刷新"):
                return KIND_PREPARE, round_str

        # 3) 局内但横幅闪过没抓到: 默认战斗环节(回合号兜底)
        if round_str:
            return KIND_BATTLE, round_str
        return None, None
