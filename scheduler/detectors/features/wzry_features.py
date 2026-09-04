# -*- coding: utf-8 -*-
# ---- 王者荣耀界面特征判定(占位/可扩展) ----
# 现状: 王者尚未迁到雷电模拟器(现为真机 + scrcpy 投屏窗口), 也没有现成"界面特征模板图"。
# 后续迁移后的建议(与金铲铲同套路):
#   在 wzry/templates/ 下放置王者"大厅/开始匹配/对局已开"的按钮或特征图, 参考
#   jinchanchan_features 的重用逻辑, 命中后返回 Scene.WZRY_LOBBY / WZRY_INGAME。
#
# 本模块现在只做两件事:
#   1) 若配置的模板目录 wzry/templates 存在且有特征文件, 按模板匹配给场景;
#   2) 否则返回 None, 并在首次给出"待补充王者特征模板"的提示, 不崩溃。
import glob
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

_hinted = False


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
    global _hinted
    try:
        base = config.WZRY_TEMPLATES
    except AttributeError:
        base = os.path.join(config.WZRY_DIR, "templates")

    # 图片存在性判定: 大厅入口 vs 对局中的命名约定(需你自己按上面说明命名)
    tpls = []
    if os.path.isdir(base):
        tpls = sorted(glob.glob(os.path.join(base, "*.png")) +
                      glob.glob(os.path.join(base, "*.jpg")))
    if not tpls:
        if not _hinted:
            _hinted = True
            print("[wzry_features] 未部署王者特征模板 -> 返回 None。"
                  "迁到雷电后请在 %s 放特征图(见 README/讲解)。" % base)
        return None

    hit_lobby = any(_match_one(frame, t, config.CONFIDENCE) for t in tpls
                    if "ingame" not in os.path.basename(t).lower()
                    and "start" in os.path.basename(t).lower())
    hit_ingame = any(_match_one(frame, t, config.CONFIDENCE) for t in tpls
                     if "ingame" in os.path.basename(t).lower())
    if hit_ingame:
        return Scene.WZRY_INGAME
    if hit_lobby:
        return Scene.WZRY_LOBBY
    return None
