# -*- coding: utf-8 -*-
"""游戏界面区域/按钮坐标定义(1024x720 基准, 移植自 Sunflower datatype.py)"""

from enum import auto, StrEnum

from agent.sdk.base import BoundingBox


class GameState(StrEnum):
    """当前所处的游戏界面"""
    MAIN_MENU = auto()          # 主菜单
    MODE_CHOOSE_MENU = auto()   # 模式选择
    ROOM = auto()               # 房间(排队中)
    IN_GAME = auto()            # 对局中
    RESULT_MENU = auto()        # 结算界面


class SpecificArea:
    """特定信息展示区域(只读)

    2026-09-17 实机校准(雷电 1920x1080 画面, 按 OCR 文字坐标换算回 1024x720 基准):
    已校准: PERIOD / COIN / LEVEL / AUGMENTS
    待校准: BOARD / CANDIDATES / CHESS_* / PLAYERS(右侧血量列位置与实机基本吻合)
    """

    # 主菜单 ------------------------------------------------------------------
    NAME = BoundingBox(700, 15, 210, 30)

    # 对局中 ------------------------------------------------------------------
    # [已校准] 回合数实测中心(780,27)@1920x1080 -> 基准(416,18)
    PERIOD = BoundingBox(330, 0, 170, 36)
    # [已校准] 金币实测中心(678,840)@1920x1080 -> 基准(362,560)
    # 收窄区域: 旁边就是玩家胜率"0%", 区域太宽会把胜率当金币
    COIN = BoundingBox(348, 552, 30, 18)
    # [已校准] 等级"2级"实测中心(300,828)@1920x1080 -> 基准(160,552)
    LEVEL = BoundingBox(120, 530, 80, 44)
    EVOLVE = BoundingBox(240, 600, 250, 30)         # 进化文字(待校准)
    CIRCLE = BoundingBox(500, 410, 10, 10)          # 传送门圈(待校准)

    PLAYERS = [
        BoundingBox(850, 55 + i * 53, 120, 55) for i in range(8)
    ]

    # [已校准] 强化卡片名实测中心 x=453/956/1461, y=386 @1920x1080 -> 基准 y=257
    AUGMENTS = [
        BoundingBox(192, 245, 100, 25),     # 强化 1
        BoundingBox(460, 245, 100, 25),     # 强化 2
        BoundingBox(730, 245, 100, 25),     # 强化 3
    ]

    # 棋盘 4 行 x 7 列(待校准)
    BOARD = [
        [BoundingBox(290 + i * 65, 265, 50, 25) for i in range(7)],
        [BoundingBox(320 + i * 65, 310, 50, 25) for i in range(7)],
        [BoundingBox(275 + i * 70, 355, 50, 25) for i in range(7)],
        [BoundingBox(300 + i * 77, 405, 50, 25) for i in range(7)],
    ]

    # 备战区 9 个位置(待校准)
    CANDIDATES = [
        BoundingBox(205 + i * 72, 490, 40, 40) for i in range(9)
    ]

    # 棋子信息面板(点中棋子后, 待校准)
    CHESS_PRICE = BoundingBox(850, 100, 50, 20)
    CHESS_NAME = BoundingBox(830, 75, 120, 25)
    CHESS_STAR = BoundingBox(860, 40, 70, 30)
    CIRCLE_PLACE = BoundingBox(510, 425, 10, 10)

    CHESS_EQUIPMENTS = [
        BoundingBox(790 + i * 80, 410, 50, 50) for i in range(3)
    ]


class SpecificButton:
    """可点击的按钮区域"""

    # 主菜单 ------------------------------------------------------------------
    PROFILE = BoundingBox(5, 630, 70, 70)           # 左下角头像
    START_MAIN = BoundingBox(880, 560, 150, 150)    # 右下角开始按钮
    SETTING_MAIN = BoundingBox(960, 20, 20, 20)     # 左上角设置

    # 模式选择 ----------------------------------------------------------------
    START_MODE = BoundingBox(820, 640, 195, 70)     # 右下角开始按钮

    # 房间 --------------------------------------------------------------------
    START_ROOM = START_MODE
    ACCEPT_COMBAT = BoundingBox(440, 460, 140, 50)  # 接受对局
    RETURN_BOARD = START_MODE

    # 对局中 ------------------------------------------------------------------
    # [已校准] 购买经验文字实测中心(332,905)@1920x1080 -> 基准(177,603)
    BUY_XP = BoundingBox(135, 585, 85, 40)
    # [已校准] 刷新文字实测中心(314,1003)@1920x1080 -> 基准(167,669)
    ROLL = BoundingBox(130, 652, 75, 34)
    CHOOSE_HP = BoundingBox(950, 480, 50, 50)       # 查看血量(待校准)
    CHESS_SELL = BoundingBox(830, 480, 120, 30)     # 出售棋子(待校准)
    SETTING_IN_GAME = BoundingBox(970, 0, 50, 50)   # 局内设置(待校准)

    ROLL_AUGMENTS = [
        BoundingBox(207, 468, 90, 30),              # 刷新强化 1(待校准)
        BoundingBox(475, 468, 90, 30),              # 刷新强化 2(待校准)
        BoundingBox(740, 468, 90, 30),              # 刷新强化 3(待校准)
    ]

    # [已校准] 商店 5 格英雄名实测中心 x=512/746/980/1214/1448, y=1040 @1920x1080
    #          -> 基准中心 x=273/398/523/647/772, y=693
    STORE_HERO = [
        BoundingBox(216 + i * 125, 655, 115, 75) for i in range(5)
    ]

    CHOOSE_EQUIPMENT = BoundingBox(10, 160, 50, 100)    # 打开装备栏
    EQUIPMENT = [
        BoundingBox(65, 55, 60, 60),
        BoundingBox(65, 117, 60, 60),
        BoundingBox(65, 179, 60, 60),
        BoundingBox(65, 241, 60, 60),
        BoundingBox(65, 303, 60, 60),
        BoundingBox(140, 55, 60, 60),
        BoundingBox(140, 117, 60, 60),
        BoundingBox(140, 179, 60, 60),
        BoundingBox(140, 241, 60, 60),
        BoundingBox(140, 303, 60, 60),
    ]

    EVOLVE = BoundingBox(780, 600, 110, 50)         # 进化按钮
    EVOLVE_ROLL = BoundingBox(780, 655, 110, 50)    # 刷新进化

    EXIT_NOW = BoundingBox(320, 610, 150, 50)       # 立即退出

    # 结算界面 ----------------------------------------------------------------
    NEXT_STEP = BoundingBox(810, 660, 120, 50)      # 下一步
