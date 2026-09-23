# -*- coding: utf-8 -*-
"""游戏信息读取层(移植自 Sunflower sunflower_catcher.py)

通过 ADB 截图 + OCR 读取细粒度对局信息:
  金币/等级/回合/商店英雄名/血量/装备/强化/进化/棋子(名字/星级/位置/装备)/界面状态
"""

import asyncio

from agent.sdk.base import BoundingBox
from agent.sdk.regions import GameState, SpecificArea, SpecificButton
from agent.sdk.utils import SdkUtils
from agent.sdk.datatypes import BasicGameInfo, Chess
from utils.logger import logger

# 备战区中的特殊道具(锻造器/金蛋等), 出现在备战席时会被识别为特殊棋子
SPECIAL_CANDIDATES = {
    "基础装备锻造器",
    "成装锻造器",
    "神器锻造器",
    "辅助装备锻造器",
    "金蛋",
}

INTERVAL = 0.3  # 每次操作后的等待间隔


class Reader:
    """依赖 Sdk(AdbOCR + BoxScaler) 实例工作, 所有区域自动缩放"""

    def __init__(self, sdk):
        self.sdk = sdk

    # ---------- 内部工具 ----------

    def area(self, box):
        return self.sdk.scaler.scale(box)

    async def screen_box(self, box):
        """截屏并裁剪指定区域"""
        screen = await self.sdk.get_screen()
        if screen is None:
            return None
        b = self.area(box)
        return screen[b.y:b.y + b.height, b.x:b.x + b.width]

    async def click_box(self, box, delay=INTERVAL):
        x, y = self.area(box).get_middle_coordinate()
        await self.sdk.click(x, y)
        await asyncio.sleep(delay)

    # ---------- 界面状态 ----------

    async def get_game_state(self):
        """识别当前所在界面(主菜单/模式选择/房间/对局中/结算)"""
        ocr_results = await self.sdk.get_screen_text()
        if not ocr_results:
            return None
        ocr_text = {r.text for r in ocr_results}

        # 关键词随赛季更新, 同时保留旧赛季(双城之战)的词兜底
        # 不用"开始游戏"作证据: 该词可能出现在模式选择等其他界面
        if {"商城", "事件", "魔典", "注销"} & ocr_text:
            return GameState.MAIN_MENU
        if {"排位赛", "匹配对战", "限时模式"} & ocr_text:
            return GameState.MODE_CHOOSE_MENU
        if {"房间语音", "战备", "招募", "对局已找到"} & ocr_text:
            return GameState.ROOM
        if {"备战环节", "购买经验", "刷新", "战斗环节"} & ocr_text:
            return GameState.IN_GAME
        if {"您获得了", "第一名", "第二名", "第三名", "第四名",
             "第五名", "第六名", "第七名", "第八名"} & ocr_text:
            return GameState.RESULT_MENU
        return None

    # ---------- 基础对局信息 ----------

    async def get_user_name(self, game_state):
        """主菜单读取昵称(会点开头像再返回)"""
        if game_state != GameState.MAIN_MENU:
            logger.warning("[SDK] 不在主菜单, 无法读取昵称")
            return None

        await self.click_box(SpecificButton.PROFILE)
        await asyncio.sleep(1)
        name = await self.sdk.get_screen_text(self.area(SpecificArea.NAME))
        await self.sdk.go_back()
        return name[0].text if name else None

    async def get_coin(self, game_state):
        if game_state != GameState.IN_GAME:
            return None
        coin = await self.sdk.get_screen_text(self.area(SpecificArea.COIN))
        if not coin:
            return None
        # 区域旁有胜率"0%"等干扰文字, 只取纯数字文本
        for r in coin:
            if r.text.isdigit():
                return int(r.text)
        # 兜底: 从第一个结果中提取数字
        digits = "".join(c for c in coin[0].text if c.isdigit())
        if digits:
            return int(digits)
        logger.warning(f"[SDK] 金币 OCR 异常: {coin[0].text}")
        return None

    async def get_level(self, game_state):
        if game_state != GameState.IN_GAME:
            return None
        level = await self.sdk.get_screen_text(self.area(SpecificArea.LEVEL))
        if not level:
            return None
        try:
            return int(level[0].text.replace("级", ""))
        except ValueError:
            logger.warning(f"[SDK] 等级 OCR 异常: {level[0].text}")
            return None

    async def get_period(self, game_state):
        """回合数, 返回 (x, y) 元组, 如 (2, 1) 表示 2-1"""
        if game_state != GameState.IN_GAME:
            return None
        period = await self.sdk.get_screen_text(self.area(SpecificArea.PERIOD))
        if not period:
            return None

        text = period[0].text
        digits = [c for c in text if c.isdigit()]
        if len(digits) == 2:
            return int(digits[0]), int(digits[1])
        logger.warning(f"[SDK] 回合 OCR 异常: {text}")
        return None

    async def get_hp(self, game_state, user_name):
        """读取 8 名玩家血量, 返回 {玩家名: 血量}(会点开血量面板)"""
        if game_state != GameState.IN_GAME:
            return None

        await self.click_box(SpecificButton.CHOOSE_HP)
        hp = {}
        for i in range(8):
            ocr_results = await self.sdk.get_screen_text(self.area(SpecificArea.PLAYERS[i]))
            if not ocr_results:
                continue
            if len(ocr_results) == 1 and ocr_results[0].text.isnumeric():
                hp[user_name] = int(ocr_results[0].text)
            elif len(ocr_results) >= 2 and ocr_results[1].text.isnumeric():
                hp[ocr_results[0].text] = int(ocr_results[1].text)
        return hp or None

    async def get_equipment(self, game_state):
        """读取持有装备列表(会点开装备栏逐件识别)"""
        if game_state != GameState.IN_GAME:
            return None

        await self.click_box(SpecificButton.CHOOSE_EQUIPMENT)
        equipments = []
        for i in range(10):
            await self.click_box(SpecificButton.EQUIPMENT[i])
            if equipment := await self.get_equipment_name(game_state):
                equipments.append(equipment)
            else:
                break
        return equipments or None

    async def get_equipment_name(self, game_state, left=True):
        """读取装备弹窗中的装备名(left=True 看左半屏, False 看右半屏)"""
        if game_state != GameState.IN_GAME:
            return None

        ocr_results = await self.sdk.get_screen_text(
            self.area(BoundingBox(0 if left else 512, 0, 512, 500)))
        if not ocr_results:
            return None

        equipment_results = [r for r in ocr_results if r.text in SdkUtils.get_equipments()]
        if equipment_results:
            return min(equipment_results, key=lambda x: x.y).text
        return None

    async def get_store(self, game_state):
        """商店 5 个位置当前出售的英雄名列表(无英雄的位置可能识别成噪声, 由 is_hero 兜底)"""
        if game_state != GameState.IN_GAME:
            return None

        heroes = []
        for i in range(5):
            ocr_results = await self.sdk.get_screen_text(self.area(SpecificButton.STORE_HERO[i]))
            heroes.append(SdkUtils.is_hero(ocr_results))
        return heroes

    async def get_basic_game_info(self, game_state):
        """金币/等级/回合/商店, 并行读取"""
        if game_state != GameState.IN_GAME:
            logger.warning("[SDK] 不在对局中, 无法读取基础信息")
            return None

        coin, level, period, store = await asyncio.gather(
            self.get_coin(game_state),
            self.get_level(game_state),
            self.get_period(game_state),
            self.get_store(game_state),
        )
        return BasicGameInfo(coin, level, period, store)

    # ---------- 强化 / 进化 ----------

    async def get_augments(self, game_state):
        """读取 3 个强化名称(仅在强化选择界面有效)"""
        if game_state != GameState.IN_GAME:
            return None

        augments = []
        for i in range(3):
            ocr_results = await self.sdk.get_screen_text(self.area(SpecificArea.AUGMENTS[i]))
            augments.append(SdkUtils.is_augments(ocr_results) if ocr_results else None)
        return augments or None

    async def get_evolution(self, game_state):
        """读取当前进化名称(仅在进化选择界面有效)"""
        if game_state != GameState.IN_GAME:
            return None
        ocr_results = await self.sdk.get_screen_text(self.area(SpecificArea.EVOLVE))
        return SdkUtils.is_evolution(ocr_results)

    # ---------- 棋子 ----------

    async def _read_chess_panel(self):
        """点中棋子后, 并行读取信息面板中的名字与星级

        注意: 不能用 `await t1, t2` 这种写法(只等第一个任务, 原 Sunflower 的 bug),
        必须用 asyncio.gather 等齐两个任务。
        """
        chess_name_task = asyncio.create_task(
            self.sdk.get_image_text(await self.screen_box(SpecificArea.CHESS_NAME)))
        chess_star_task = asyncio.create_task(
            asyncio.to_thread(SdkUtils.get_chess_star, await self.screen_box(SpecificArea.CHESS_STAR)))
        await asyncio.gather(chess_name_task, chess_star_task)
        return SdkUtils.is_hero(chess_name_task.result()), chess_star_task.result()

    async def get_chess_info(self, game_state, location, check_equipment=False):
        """读取棋盘棋子信息

        location: (行, 列) 或 (备战区索引,) 或 (商店索引,)
        check_equipment: 是否逐件读取棋子携带装备(较慢)
        """
        if game_state != GameState.IN_GAME:
            logger.warning("[SDK] 不在对局中, 无法读取棋子信息")
            return None

        is_candidate = len(location) == 1
        if is_candidate:
            await self.click_box(SpecificArea.CANDIDATES[location[0]])
        else:
            await self.click_box(SpecificArea.BOARD[location[0]][location[1]])

        chess_equipments = []
        if check_equipment:
            for i in range(3):
                await self.click_box(SpecificArea.CHESS_EQUIPMENTS[i])
                equipment_name = await self.get_equipment_name(game_state, left=False)
                if not equipment_name:
                    break
                chess_equipments.append(equipment_name)
        chess_equipments.extend([None] * (3 - len(chess_equipments)))

        chess_name, chess_star = await self._read_chess_panel()
        return Chess(chess_name, chess_star, location, is_candidate, chess_equipments)

    async def get_candidate_info(self, game_state, order, check_equipment=False):
        """读取备战区棋子信息(同 get_chess_info 的一维坐标版本)"""
        return await self.get_chess_info(game_state, (order,), check_equipment)
