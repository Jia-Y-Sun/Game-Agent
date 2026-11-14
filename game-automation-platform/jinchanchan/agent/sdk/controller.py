# -*- coding: utf-8 -*-
"""游戏操作层(移植自 Sunflower sunflower_control.py)"""

import asyncio

from agent.sdk.base import BoundingBox as SdkBox
from agent.sdk.regions import SpecificArea, SpecificButton
from utils.logger import logger

INTERVAL = 0.3


class Control:
    """依赖 Sdk 实例工作, 所有坐标自动缩放"""

    def __init__(self, sdk):
        self.sdk = sdk

    async def click_box(self, box, delay=INTERVAL):
        x, y = self.sdk.scaler.scale(box).get_middle_coordinate()
        await self.sdk.click(x, y)
        await asyncio.sleep(delay)

    # ---------- 各界面通用 ----------

    async def start_game(self):
        """主菜单/模式选择/房间界面的开始按钮"""
        await self.click_box(SpecificButton.START_MAIN)

    async def accept_combat(self):
        """接受对局"""
        await self.click_box(SpecificButton.ACCEPT_COMBAT)

    async def next_step(self):
        """结算界面下一步"""
        await self.click_box(SpecificButton.NEXT_STEP)

    # ---------- 对局内 ----------

    async def buy_xp(self):
        await self.click_box(SpecificButton.BUY_XP)

    async def roll(self):
        await self.click_box(SpecificButton.ROLL)

    async def buy_chess(self, index):
        """购买商店第 index 格(0-4)的英雄"""
        await self.click_box(SpecificButton.STORE_HERO[index])

    async def sell_chess(self, location):
        """出售棋子: 先点棋子(棋盘/备战区), 再点出售按钮"""
        if len(location) == 2:
            await self.click_box(SpecificArea.BOARD[location[0]][location[1]])
        else:
            await self.click_box(SpecificArea.CANDIDATES[location[0]])
        await self.click_box(SpecificButton.CHESS_SELL)

    async def move_chess(self, location, target):
        """移动棋子(棋盘<->备战区), 滑动 600ms"""
        if len(location) == 2:
            loc = self.sdk.scaler.scale(SpecificArea.BOARD[location[0]][location[1]])
        else:
            loc = self.sdk.scaler.scale(SpecificArea.CANDIDATES[location[0]])

        if len(target) == 2:
            tgt = self.sdk.scaler.scale(SpecificArea.BOARD[target[0]][target[1]])
        else:
            tgt = self.sdk.scaler.scale(SpecificArea.CANDIDATES[target[0]])

        await self.sdk.swipe(*loc.get_middle_coordinate(),
                             *tgt.get_middle_coordinate(), duration=600)

    async def roll_augment(self, index):
        await self.click_box(SpecificButton.ROLL_AUGMENTS[index])

    async def choose_augment(self, index):
        await self.click_box(SpecificArea.AUGMENTS[index])

    async def evolve(self):
        await self.click_box(SpecificButton.EVOLVE)

    async def roll_evolve(self):
        await self.click_box(SpecificButton.EVOLVE_ROLL)

    async def move_to_circle(self):
        """走向传送门圈(选秀轮)"""
        center = self.sdk.scaler.scale(SdkBox(500, 500, 1, 1))
        await self.sdk.click(center.x, center.y)
        await asyncio.sleep(INTERVAL)
        await self.click_box(SpecificArea.CIRCLE)
