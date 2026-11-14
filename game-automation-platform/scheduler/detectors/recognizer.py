# -*- coding: utf-8 -*-
# ---- 特征判定器 ----
# 输入: 一帧"同一片屏幕"截图(可选; 为省资源也可以直接在各 features 内各自截图)。
# 输出: state.Scene 枚举中的某一个。
#
# 组成: 依次询问各游戏 features 模块 -> 谁先给出非 None/非 UNKNOWN 的场景就采纳。
# 好处: 增加新游戏只需新增一个 features 模块并挂到下面列表。
#
# NOTE: 无三引号 docstring, 用注释。所有文本便于原样落盘。
import config
from detectors.capture import capture_fullscreen
from detectors.features import wzry_features, jinchanchan_features
from state import Scene


def detect_screen_scene(frame=None) -> Scene:
    """截(或复用传入)一帧,"同屏"归到一个 Scene。"""
    if frame is None:
        frame = capture_fullscreen()
    if frame is None:
        # 无法截图 -> 维持 NONE, 不冒然切换游戏
        return Scene.NONE

    # 依次尝试各游戏特征识别(先铲子后王者顺序可调; 谁是首要由现场定)
    for name, feature_fn in (
        ("jinchanchan", jinchanchan_features.detect),
        ("wzry",        wzry_features.detect),
    ):
        try:
            got = feature_fn(frame)
        except Exception as e:
            print("[recognizer] %s.features.detect 异常:", name, e)
            got = None
        if got is not None and got not in (Scene.UNKNOWN,):
            # 命中确定场景
            return got
    return Scene.UNKNOWN
