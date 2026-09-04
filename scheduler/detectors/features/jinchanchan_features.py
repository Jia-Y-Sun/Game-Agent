# -*- coding: utf-8 -*-
# ---- 金铲铲界面特征判定 ----
# 思路与现有金铲铲本体一致(OpenCV 模板匹配), 这里"复用"其本体 images/buttons
# 下的按钮模板作为特征源(只读引用, 不改其代码/图片)。
#   - 命中 start_game/ranked 等大厅按钮 -> JCC_LOBBY
#   - 命中 game_start / prepare_phase / battle_phase -> JCC_INGAME
# 模板目录/依赖缺失时返回 None(交给上层走其它游戏/UNKNOWN), 不崩溃。
import os

import config
from state import Scene

_has_cv2 = False
try:
    import cv2
    import numpy as np
    _has_cv2 = True
except Exception:
    pass


def _match_one(frame, tpl_path, threshold):
    if not _has_cv2 or not os.path.exists(tpl_path):
        return False
    tpl = cv2.imread(tpl_path, cv2.IMREAD_COLOR)
    if tpl is None:
        return False
    if frame.shape[0] < tpl.shape[0] or frame.shape[1] < tpl.shape[1]:
        return False
    try:
        res = cv2.matchTemplate(frame, tpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, _ = cv2.minMaxLoc(res)
        return mx >= threshold
    except Exception:
        return False


def detect(frame):
    """返回 Scene. 若金铲铲特征命中则给出对应场景, 否则 None。"""
    base = config.JCC_TEMPLATES
    th = config.CONFIDENCE
    if not os.path.isdir(base):
        return None

    def hit(name):
        return _match_one(frame, os.path.join(base, name), th)

    # 对局中标志的按钮存在性(进入对局后/回合中)
    if any(hit(n) for n in ("game_start.png", "prepare_phase.png", "battle_phase.png")):
        return Scene.JCC_INGAME
    # 大厅/操作入口
    if any(hit(n) for n in ("start_game_1.png", "start_game_2.png",
                            "start_game_3.png", "ranked_tab.png",
                            "accept_match.png")):
        return Scene.JCC_LOBBY
    return None
