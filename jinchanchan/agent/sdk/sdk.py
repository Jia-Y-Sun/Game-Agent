# -*- coding: utf-8 -*-
"""SDK 门面: ADB 连接 + 分辨率缩放 + 信息读取 + 游戏操作

用法(异步):
    sdk = Sdk()
    await sdk.load(port=5555)            # 连接雷电模拟器(默认 5555), 自动适配分辨率
    state = await sdk.get_game_state()   # 识别当前界面
    if state == GameState.IN_GAME:
        info = await sdk.get_basic_game_info()   # 金币/等级/回合/商店
    await sdk.buy_chess(4)               # 购买商店第 5 格英雄

同步代码(如 game_flow 主循环)请用 SdkBridge。
"""

from agent.sdk.adb import AdbOCR
from agent.sdk.base import BoxScaler
from agent.sdk.reader import Reader
from agent.sdk.controller import Control
from utils.logger import logger


class Sdk(AdbOCR):
    def __init__(self):
        super().__init__()
        self.scaler = None
        self.reader = None
        self.control = None

    async def load(self, port, host="localhost", scan_if_fail=True):
        await super().load(port=port, host=host, scan_if_fail=scan_if_fail)
        await self.finish_init()

    async def connect_only(self, port, host="localhost", scan_if_fail=True):
        """只连接(不初始化 OCR), 供启动重试场景使用"""
        await super().connect_only(port, host, scan_if_fail)

    async def finish_init(self):
        """连接成功后的收尾初始化: OCR 引擎 + 分辨率缩放 + 读取/操作层"""
        self.ensure_ocr_engine()

        # 分辨率以"实际截图像素"为准:
        # 雷电模拟器横屏游戏时 wm size 仍报物理竖屏(如 1080x1920),
        # 但 screencap 返回的是旋转后的真实画面(如 1920x1080), 必须用后者做缩放基准。
        size = (0, 0)
        screen = await self.get_screen()
        if screen is not None and screen.size > 0:
            size = (screen.shape[1], screen.shape[0])
        if size == (0, 0):
            size = await self.get_screen_size()
        if size == (0, 0):
            raise ConnectionError("读取设备分辨率失败")
        self.scaler = BoxScaler(size)
        self.reader = Reader(self)
        self.control = Control(self)
        logger.info(
            f"[SDK] 截图分辨率 {size[0]}x{size[1]}, "
            f"坐标缩放 x{self.scaler.sx:.3f} / y{self.scaler.sy:.3f}")

    # 便捷代理: 信息读取
    async def get_game_state(self):
        return await self.reader.get_game_state()

    async def get_basic_game_info(self):
        state = await self.reader.get_game_state()
        return await self.reader.get_basic_game_info(state)

    async def get_store(self):
        state = await self.reader.get_game_state()
        return await self.reader.get_store(state)

    async def get_chess_info(self, location, check_equipment=False):
        state = await self.reader.get_game_state()
        return await self.reader.get_chess_info(state, location, check_equipment)

    # 便捷代理: 游戏操作
    async def buy_chess(self, index):
        await self.control.buy_chess(index)

    async def buy_xp(self):
        await self.control.buy_xp()

    async def roll(self):
        await self.control.roll()
