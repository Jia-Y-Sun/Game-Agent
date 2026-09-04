# -*- coding: utf-8 -*-
# ---- 统一截图抽象 ----
# 依据 decision-1(B)/Q2: 两套程序都假定跑在雷电模拟器、同屏可见,
# 因此这里截"整片当前桌面/屏幕"作为唯一观测源。
#
# 为降低依赖(架构阶段不想真的装 pyautogui 也能跑通 import), 截图用惰性导入:
#   - 优先 pyautogui.screenshot
#   - 其次 mss
# 两者都不可用时返回 (None, 原因字符串), 上层据此走 NONE/UNKNOWN 降级。
import os

try:
    import pyautogui as _pg
    _HAS_PG = True
except Exception:
    _HAS_PG = False
    _pg = None


def capture_fullscreen():
    """截取当前整屏, 返回 BGR numpy 数组(BGR->RGB 不确定, 识别主要依赖模板匹配,
    通道差异影响有限, 架构阶段放宽)。失败返回 None。"""
    if not _HAS_PG:
        return _try_mss()
    try:
        shot = _pg.screenshot()          # PIL Image, RGB
        import numpy as np
        return np.array(shot)[..., ::-1].copy()  # 转成 BGR(与 opencv 模板习惯一致)
    except Exception as e:
        print("[capture] pyautogui 截图失败:", e)
        return _try_mss()


def _try_mss():
    try:
        import mss
        import numpy as np
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[0])   # 主屏全区域
            img = np.asarray(raw)[..., :3].copy()
            return img
    except Exception as e:
        print("[capture] mss 截图不可用:", e)
        return None
