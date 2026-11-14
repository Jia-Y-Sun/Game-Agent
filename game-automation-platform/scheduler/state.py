# -*- coding: utf-8 -*-
# ---- 串行状态/场景枚举 ----
# 识别层把"同一片雷电屏幕"归到某个 Scene, 再由 SCENE_TO_GAME 决定应前台跑哪款。
# 本模块只做最简单映射, 不做业务推断, 便于调度器最小切换。
from enum import Enum


class Scene(Enum):
    """识别层可能给出的场景结论。"""
    NONE       = "none"         # 无游戏画面/空闲桌面
    WZRY_LOBBY = "wzry_lobby"   # 王者 - 大厅
    WZRY_INGAME= "wzry_ingame"  # 王者 - 对局中
    JCC_LOBBY  = "jcc_lobby"    # 金铲铲 - 大厅
    JCC_INGAME = "jcc_ingame"   # 金铲铲 - 对局中
    UNKNOWN    = "unknown"      # 识别不清


# Scene -> 应前台运行的游戏名(与 config.GAMES 的 key 对应); None=无需启动
SCENE_TO_GAME = {
    Scene.WZRY_LOBBY:  "wzry",
    Scene.WZRY_INGAME: "wzry",
    Scene.JCC_LOBBY:   "jinchanchan",
    Scene.JCC_INGAME:  "jinchanchan",
}


class SchedulerState:
    """串行调度状态: 仅记录"当前希望前台运行的游戏"。"""
    def __init__(self):
        self.desired = None

    def desired_from(self, scene):
        return SCENE_TO_GAME.get(scene)
