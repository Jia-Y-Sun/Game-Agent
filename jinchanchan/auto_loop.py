# -*- coding: utf-8 -*-
"""
全自动对局循环 — v1 (可执行验证版)
==================================
设计原则(按用户要求):
  - 进入游戏 / 打完退出(结算): 基于识别标志 —— 全屏 OCR 定位按钮文字后点击
  - 对局过程中的所有功能: 全部调用 Sunflower SDK(agent/sdk, ADB 直连)

流程: 识别当前环节 -> 执行该环节动作 -> 重新识别(自愈式循环, 不依赖固定时序)

用法:
  python auto_loop.py                  # 启动全自动循环(会真的进对局操作)
  python auto_loop.py --dry-run        # 只识别不操作(观察模式)
  python auto_loop.py --max-rounds 10  # 打到第 10 回合后停止
  停止: Ctrl+C
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.sdk import Sdk
from agent.sdk.stages import StageDetector, Stage
from agent.sdk.qmark import QuestionMarkDetector
from agent.sdk.banner import (BannerDetector, KIND_PREPARE, KIND_BATTLE,
                              KIND_AUGMENT, KIND_CAROUSEL)
from agent.sdk.utils import SdkUtils
from utils.logger import logger


class AutoLoop:
    def __init__(self, sdk, dry_run=False, max_rounds=99):
        self.sdk = sdk
        self.det = StageDetector(sdk)
        self.dry_run = dry_run
        self.max_rounds = max_rounds
        self.unknown_streak = 0        # 连续 UNKNOWN 计数(自愈用)
        self.stage_actions = 0         # 已执行的环节动作数
        self.last_period = None
        self._last_acted_period = None  # 已执行过购买动作的回合号(避免同一回合重复花钱)
        self._last_queue_click = 0     # 匹配房间上次点"开始游戏"的时间(避免重复点)
        self._current_stage = None     # 当前环节(供问号监视任务使用)
        self._last_known_stage = None  # 上一个已知环节(结算过渡期间禁用返回键自救)
        self.qm = QuestionMarkDetector()
        self.banner = BannerDetector(sdk)   # 窄区域识别(顶部横幅 + 左下HUD)

    # ---------- 基础工具 ----------

    def log(self, msg):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))

    async def click_text(self, keyword, timeout_retries=1):
        """全屏 OCR 定位文字(全等匹配)并点击其中心, 返回 True/False

        注意: 必须全等匹配, 子串匹配会把"本局自动接受匹配"当成"接受"按钮误点。
        """
        results = await self.sdk.get_screen_text(confidence=0.35)
        if not results:
            return False
        for r in results:
            if SdkUtils.text_match(r.text, keyword):
                x, y = r.x + r.width // 2, r.y + r.height // 2
                self.log("点击文字[%s] @(%d,%d)" % (r.text, x, y))
                if not self.dry_run:
                    await self.sdk.click(x, y)
                return True
        return False

    # ---------- 各环节动作 ----------

    async def handle_main_menu(self):
        self.log("主菜单: 尝试点击[开始游戏]")
        if await self.click_text("开始游戏"):
            await asyncio.sleep(8)
            return True
        return False

    async def handle_mode_select(self):
        self.log("模式选择: 点击[开始游戏]进入匹配(沿用上次选择的模式)")
        if await self.click_text("开始游戏"):
            await asyncio.sleep(8)
            return True
        return False

    async def handle_popup(self):
        self.log("弹窗: 按返回键关闭")
        if not self.dry_run:
            await self.sdk.go_back()
        await asyncio.sleep(2)

    async def handle_queue(self):
        """匹配房间(用户确认的流程):
        1. 匹配到对手时出现[接受]按钮 -> 点击接受
        2. 否则点[开始游戏]才开始匹配(每 30s 最多点一次, 避免连点)
        """
        if await self.click_text("接受"):
            self.log("匹配房间: 已点击[接受]")
            await asyncio.sleep(6)
            return
        now = time.time()
        if now - self._last_queue_click > 30:
            self.log("匹配房间: 点击[开始游戏]开始匹配")
            if await self.click_text("开始游戏"):
                self._last_queue_click = now
                await asyncio.sleep(5)
                return
            self._last_queue_click = now
        self.log("匹配房间: 等待匹配...")
        await asyncio.sleep(6)

    async def handle_accept(self):
        self.log("接受对局弹窗: 点击[接受]")
        if await self.click_text("接受"):
            await asyncio.sleep(5)
            return
        # 没找到"接受"文字就点屏幕中间(接受按钮常在中部)
        w, h = self.sdk.scaler.device_size
        if not self.dry_run:
            await self.sdk.click(w // 2, h // 2)
        await asyncio.sleep(5)

    async def handle_loading(self):
        await asyncio.sleep(5)

    async def handle_augment(self):
        self.log("强化选择: 点第 1 张强化卡")
        await self.sdk.control.choose_augment(0)
        await asyncio.sleep(2)

    async def handle_carousel(self):
        self.log("选秀轮: 持续点屏幕中心(每 2s 一次, 直到环节变化)")
        w, h = self.sdk.scaler.device_size
        for _ in range(30):     # 最多点 30 次(约 60s), 期间环节变化则退出
            kind, _ = await self.banner.detect()
            if kind != KIND_CAROUSEL:
                return
            if not self.dry_run:
                await self.sdk.click(w // 2, h // 2)
            await asyncio.sleep(2)

    @staticmethod
    def _parse_round(round_str):
        """'2-3' -> (2, 3), 解析失败返回 None"""
        if not round_str:
            return None
        parts = round_str.split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
        return None

    async def handle_prepare(self, round_str=None):
        """备战环节: 全部走 Sunflower SDK, 同一回合只执行一次购买动作"""
        # 1) 读基础信息(金币/等级/回合/商店) — SDK 细粒度读取
        try:
            info = await self.sdk.get_basic_game_info()
            if info:
                self.last_period = info.period
                self.log("SDK 读取: 金币=%s 等级=%s 回合=%s 商店=%s" % (
                    info.coin, info.level, info.period, info.store))
            else:
                self.log("SDK 基础信息读取失败(None)")
        except Exception as e:  # noqa: BLE001
            self.log("SDK 基础信息读取异常: %s" % e)
            info = None

        # 回合号: 优先 info.period, 读不到就用窄区识别到的回合号兜底
        period = info.period if (info and info.period) else self._parse_round(round_str)

        # 回合号都拿不到(过渡帧/弹窗遮挡), 不盲目执行购买动作
        if period is None:
            self.log("回合号缺失, 本回合不执行动作")
            await asyncio.sleep(3)
            return

        # 同一回合已执行过动作则跳过(每 10s 一轮采样, 一个备战回合会进来 2~3 次)
        if period == self._last_acted_period:
            self.log("回合 %s 已执行过动作, 本回合跳过" % (period,))
            await asyncio.sleep(4)
            return
        self._last_acted_period = period

        # 2) 买最贵的英雄(商店最右格 x3) — SDK 操作
        try:
            for i in range(3):
                await self.sdk.buy_chess(4)
                await asyncio.sleep(0.6)
            self.log("SDK 买英雄完成(最右格 x3)")
        except Exception as e:  # noqa: BLE001
            self.log("SDK 买英雄异常: %s" % e)

        # 3) 买经验 x2 — SDK 操作
        try:
            for i in range(2):
                await self.sdk.buy_xp()
                await asyncio.sleep(0.6)
            self.log("SDK 买经验完成(x2)")
        except Exception as e:  # noqa: BLE001
            self.log("SDK 买经验异常: %s" % e)

        # 4) 上阵 1 个棋子(备战区0 -> 棋盘, 坐标待校准, 单独隔离风险)
        try:
            await self.sdk.control.move_chess((0,), (1, 2))
            self.log("SDK 上阵完成(备战区0 -> 棋盘1行2列)")
        except Exception as e:  # noqa: BLE001
            self.log("SDK 上阵异常: %s" % e)

        await asyncio.sleep(3)

    async def handle_battle(self):
        self.log("战斗环节: 等待...")
        await asyncio.sleep(8)

    async def handle_result(self):
        """结算: 战败首屏是[现在退出], 之后依次[下一步]...[再来一局], 出现哪个点哪个"""
        self.log("结算: 按[现在退出]->[下一步]->[再来一局]顺序点击出现的按钮")
        for keyword in ("现在退出", "下一步", "再来一局"):
            if await self.click_text(keyword):
                await asyncio.sleep(5)
                return
        # 都没找到: 点右下角兜底
        w, h = self.sdk.scaler.device_size
        if not self.dry_run:
            await self.sdk.click(int(w * 0.85), int(h * 0.92))
        await asyncio.sleep(5)

    # ---------- 问号监视(用户要求: 对局内每 5 秒识别一次, 识别到点一次) ----------

    IN_MATCH_STAGES = {Stage.PREPARE, Stage.BATTLE, Stage.CAROUSEL, Stage.AUGMENT}

    async def _question_mark_watcher(self):
        while True:
            try:
                if self._current_stage in self.IN_MATCH_STAGES and self.qm.ready:
                    pos = await self.qm.find(self.sdk)
                    if pos:
                        self.log("问号识别: 点击 (%d,%d)" % (pos[0], pos[1]))
                        if not self.dry_run:
                            await self.sdk.click(pos[0], pos[1])
            except Exception as e:  # noqa: BLE001
                self.log("问号识别异常: %s" % e)
            await asyncio.sleep(5)

    # ---------- 主循环 ----------

    async def run(self):
        self.log("=" * 55)
        self.log("自动循环启动 (dry_run=%s, max_rounds=%s, 问号模板=%d张)" % (
            self.dry_run, self.max_rounds, len(self.qm._templates)))
        self.log("=" * 55)

        asyncio.create_task(self._question_mark_watcher())

        while True:
            try:
                await self._run_cycle()
            except Exception as e:  # noqa: BLE001
                # 任何异常都不能杀死循环(如模拟器重启/游戏退出导致断连)
                self.log("循环异常(已忽略, 5s后继续): %s" % e)
                await asyncio.sleep(5)

    async def _run_cycle(self):
        while True:
            # 先走窄区域识别(对局内阶段, 快): 顶部横幅 + 左下HUD
            kind, round_str = await self.banner.detect()
            if kind is not None:
                if kind == KIND_PREPARE:
                    stage, matched = Stage.PREPARE, ["准备阶段/购买经验"]
                elif kind == KIND_BATTLE:
                    stage, matched = Stage.BATTLE, ["战斗开始/回合号兜底"]
                elif kind == KIND_AUGMENT:
                    stage, matched = Stage.AUGMENT, ["点击卡片!选择强化效果!"]
                else:
                    stage, matched = Stage.CAROUSEL, ["选秀轮 %s" % round_str]
                if round_str:
                    self.log("窄区识别: %s 回合=%s" % (stage.value, round_str))
            else:
                # 不在对局内: 全屏识别菜单/结算等环节
                stage, matched, _ = await self.det.get_stage()

            self._current_stage = stage
            self.log("当前环节: %s (标志: %s)" % (stage.value, ",".join(matched)))

            # 回合数达到上限则停止(观察用)
            if (self.last_period and self.max_rounds
                    and self.last_period[0] * 100 + self.last_period[1]
                    >= self.max_rounds * 100):
                self.log("达到回合上限 %s, 停止(仍可 Ctrl+C 退出)" % self.max_rounds)
                await asyncio.sleep(60)

            if stage == Stage.MAIN_MENU:
                await self.handle_main_menu()
            elif stage == Stage.MODE_SELECT:
                await self.handle_mode_select()
            elif stage == Stage.POPUP:
                await self.handle_popup()
            elif stage == Stage.QUEUE:
                await self.handle_queue()
            elif stage == Stage.ACCEPT:
                await self.handle_accept()
            elif stage == Stage.LOADING:
                await self.handle_loading()
            elif stage == Stage.AUGMENT:
                await self.handle_augment()
            elif stage == Stage.CAROUSEL:
                await self.handle_carousel()
            elif stage == Stage.PREPARE:
                await self.handle_prepare(round_str)
            elif stage == Stage.BATTLE:
                await self.handle_battle()
            elif stage == Stage.RESULT:
                await self.handle_result()
            else:  # UNKNOWN: 过渡画面/加载, 连续多次无识别则按返回键自救
                # 结算流程的过渡画面很多, 按返回键会打断结算 -> 只等待
                if self._last_known_stage != Stage.RESULT:
                    self.unknown_streak += 1
                    if self.unknown_streak >= 5:
                        self.log("连续 %d 次无法识别, 尝试按返回键自救" % self.unknown_streak)
                        if not self.dry_run:
                            await self.sdk.go_back()
                        self.unknown_streak = 0
                await asyncio.sleep(4)
                continue

            self.unknown_streak = 0
            self._last_known_stage = stage
            self.stage_actions += 1
            await asyncio.sleep(2)


async def main():
    parser = argparse.ArgumentParser(description="金铲铲全自动对局循环 v1")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--dry-run", action="store_true", help="只识别不操作")
    parser.add_argument("--max-rounds", type=int, default=99)
    args = parser.parse_args()

    sdk = Sdk()
    print("[AUTO] 连接雷电模拟器 %s:%d (未启动时每10秒自动重试)..." % (args.host, args.port))
    while True:
        try:
            await sdk.connect_only(port=args.port, host=args.host, scan_if_fail=True)
            break
        except Exception as e:  # noqa: BLE001
            print("[AUTO] 连接失败(%s), 10秒后重试..." % e)
            await asyncio.sleep(10)
    await sdk.finish_init()
    print("[AUTO] 已连接, 截图分辨率 %dx%d" % sdk.scaler.device_size)

    loop = AutoLoop(sdk, dry_run=args.dry_run, max_rounds=args.max_rounds)
    try:
        await loop.run()
    except KeyboardInterrupt:
        print("\n[AUTO] 用户中断, 退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print("自动循环失败: %s" % e)
        traceback.print_exc()
