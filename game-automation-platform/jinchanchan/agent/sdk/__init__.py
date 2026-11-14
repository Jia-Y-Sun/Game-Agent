# -*- coding: utf-8 -*-
"""Sunflower SDK 移植包(适配雷电模拟器 + easyocr)

来源: github.com/Carey8175/Sunflower (原作者已停止维护, SDK 功能完整)
用法示例见项目根目录 test_sdk.py。
"""

from agent.sdk.base import BoundingBox, OcrResult, BoxScaler, BASE_RESOLUTION
from agent.sdk.regions import GameState
from agent.sdk.sdk import Sdk
from agent.sdk.bridge import SdkBridge

__all__ = [
    "BoundingBox", "OcrResult", "BoxScaler", "BASE_RESOLUTION",
    "GameState", "Sdk", "SdkBridge",
]
