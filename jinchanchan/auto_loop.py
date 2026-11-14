# -*- coding: utf-8 -*-
"""
全自动对局循环 — standalone / external
=====================================
设计原则(按用户要求):
  - 进入游戏 / 打完退出(结算): 基于识别标志 —— 全屏 OCR 定位按钮文字后点击
  - 对局过程中的所有功能: 全部调用 Sunflower SDK(agent/sdk, ADB 直连)

流程: 识别当前环节 -> 执行该环节动作 -> 重新识别(自愈式循环, 不依赖固定时序)

用法:
  python auto_loop.py                              # standalone 全自动循环
  python auto_loop.py --mode external ...          # 外部总控对局执行器
  python auto_loop.py --dry-run                    # 只识别不操作(观察模式)
  python auto_loop.py --max-rounds 10              # 打到第 10 回合后停止
  python auto_loop.py --mode external `
    --device-id 127.0.0.1:5555 `
    --event-file runs/<run_id>/events.jsonl `
    --stop-file runs/<run_id>/stop.game `
    --timeout 900
  停止: Ctrl+C
"""

import argparse
import asyncio
import os
import sys
import time
from contextlib import suppress
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.sdk import Sdk
from agent.sdk.adb_bin import AdbBinaryTransport
from agent.sdk.stages import StageDetector, Stage
from agent.sdk.qmark import QuestionMarkDetector
from agent.sdk.banner import (BannerDetector, KIND_PREPARE, KIND_BATTLE,
                              KIND_AUGMENT, KIND_CAROUSEL)
from agent.sdk.utils import SdkUtils
from agent.lifecycle import (
    EXIT_BUSINESS_ERROR,
    EXIT_ENV_ERROR,
    EXIT_OK,
    EXIT_STOPPED,
    EXIT_TIMEOUT,
    EventWriter,
    LifecycleGuard,
    MATCH_READY_TIMEOUT,
    RunAbort,
    StableMatchReadyGate,
    parse_match_result,
    resolve_device_endpoint,
)
from utils.logger import logger


class AutoLoop:
    def __init__(self, sdk, dry_run=False, max_rounds=99,
                 mode="standalone", events=None, match_ready_timeout=None):
        self.sdk = sdk
        self.det = StageDetector(sdk)
        self.dry_run = dry_run
        self.max_rounds = max_rounds
        self.mode = mode
        self.events = events or EventWriter()
        self.match_ready_timeout = (
            MATCH_READY_TIMEOUT
            if match_ready_timeout is None
            else match_ready_timeout
        )
        self.unknown_streak = 0        # 连续 UNKNOWN 计数(自愈用)
        self.stage_actions = 0         # 已执行的环节动作数
        self.last_period = None
        self._last_acted_period = None  # 已执行过购买动作的回合号(避免同一回合重复花钱)
        self._last_queue_click = 0     # 匹配房间上次点"开始游戏"的时间(避免重复点)
        self._current_stage = None     # 当前环节(供问号监视任务使用)
        self._last_known_stage = None  # 上一个已知环节(结算过渡期间禁用返回键自救)
        self._last_phase_event = None  # 上次输出的 ROUND_PHASE
        self._qm_task = None
        self.qm = QuestionMarkDetector()
        self.banner = BannerDetector(sdk)   # 窄区域识别(顶部横幅 + 左下HUD)

    # ---------- 基础工具 ----------

    def log(self, msg):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))

    def _emit_phase(self, stage, round_str=None):
        phase_map = {
            Stage.PREPARE: "buy",
            Stage.BATTLE: "battle",
            Stage.AUGMENT: "augment",
            Stage.CAROUSEL: "carousel",
        }
        phase = phase_map.get(stage)
        if phase is None:
            return
        key = (phase, round_str)
        if key == self._last_phase_event:
            return
        self._last_phase_event = key
        self.events.emit(
            "ROUND_PHASE",
            phase=phase,
            round=round_str,
            stage=stage.value,
        )

    def _emit_match_ready(self, kind, round_str):
        emitted = self.events.emit(
            "MATCH_READY",
            once=True,
            kind=kind,
            round=round_str,
        )
        if emitted:
            self.log("MATCH_READY: kind=%s round=%s" % (kind, round_str))

    def _emit_match_finished(self, result, placement=None):
        fields = {"result": result}
        if placement is not None:
            fields["placement"] = placement
        emitted = self.events.emit("MATCH_FINISHED", once=True, **fields)
        if emitted:
            self.log("MATCH_FINISHED: result=%s placement=%s" % (result, placement))

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
        self.log("接受对局弹窗: 快速点击固定[接受]按钮")
        if self.dry_run:
            self.log("[DRY-RUN] 跳过接受对局点击")
            await asyncio.sleep(1)
            return
        # 接受窗口时间很短, 不能再做一次全屏 OCR; 直接点击已校准按钮.
        await self.sdk.control.accept_combat()
        self.log("已点击[接受对局]")
        await asyncio.sleep(1)

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

    def _start_question_watcher(self):
        if self._qm_task is None or self._qm_task.done():
            self._qm_task = asyncio.create_task(self._question_mark_watcher())

    async def _stop_question_watcher(self):
        if self._qm_task is None:
            return
        self._qm_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._qm_task
        self._qm_task = None

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
        if self.mode == "external":
            return await self._run_external()
        return await self._run_standalone()

    async def _run_standalone(self):
        self.log("=" * 55)
        self.log("standalone 自动循环启动 (dry_run=%s, max_rounds=%s, 问号模板=%d张)" % (
            self.dry_run, self.max_rounds, len(self.qm._templates)))
        self.log("=" * 55)

        self._start_question_watcher()

        try:
            while True:
                try:
                    await self._run_cycle()
                except Exception as e:  # noqa: BLE001
                    # standalone 保持自愈; external 异常会直接中止.
                    self.log("循环异常(已忽略, 5s后继续): %s" % e)
                    await asyncio.sleep(5)
        finally:
            await self._stop_question_watcher()

    async def _run_external(self):
        """只处理对局内动作, 不控制大厅/匹配/结算/下一局。"""
        self.log("external 对局执行器启动, 等待 MATCH_READY")
        self.events.emit("WAITING_MATCH_READY")
        gate = StableMatchReadyGate(
            self.banner,
            timeout=self.match_ready_timeout,
        )
        kind, round_str = await gate.wait()
        self._emit_match_ready(kind, round_str)
        self._start_question_watcher()

        try:
            await self._run_external_match(kind, round_str)
        finally:
            await self._stop_question_watcher()

    async def _run_external_match(self, kind, round_str):
        unknown_streak = 0
        while True:
            stage = None
            matched = []
            texts = []

            if kind is not None:
                stage, matched = self._stage_from_banner(kind, round_str)
            else:
                stage, matched, texts = await self.det.get_stage()

            self._current_stage = stage

            if stage == Stage.RESULT:
                result, placement = parse_match_result(texts or matched)
                self._emit_match_finished(result, placement)
                return

            if stage in self.IN_MATCH_STAGES:
                existing_round = round_str
                self._emit_phase(stage, existing_round)
                if stage == Stage.PREPARE:
                    await self.handle_prepare(existing_round)
                elif stage == Stage.AUGMENT:
                    await self.handle_augment()
                elif stage == Stage.CAROUSEL:
                    await self.handle_carousel()
                else:
                    # external 的 battle 不长时间阻塞, 提高结算检测频率.
                    await asyncio.sleep(2)
                    probe_stage, probe_matched, probe_texts = await self.det.get_stage()
                    if probe_stage == Stage.RESULT:
                        result, placement = parse_match_result(
                            probe_texts or probe_matched
                        )
                        self._emit_match_finished(result, placement)
                        return
                self._last_known_stage = stage
                unknown_streak = 0
            else:
                unknown_streak += 1
                if stage in {
                    Stage.MAIN_MENU,
                    Stage.MODE_SELECT,
                    Stage.QUEUE,
                    Stage.ACCEPT,
                }:
                    raise RunAbort(
                        EXIT_BUSINESS_ERROR,
                        "left_match_without_result",
                        stage="match_finished",
                        message=f"unexpected stage after MATCH_READY: {stage.value}",
                    )
                if unknown_streak >= 30:
                    raise RunAbort(
                        EXIT_BUSINESS_ERROR,
                        "match_finished_timeout",
                        stage="match_finished",
                        message="unable to identify match phase or result",
                    )
                await asyncio.sleep(2)
                kind, round_str = await self.banner.detect()
                continue

            kind, round_str = await self.banner.detect()

    @staticmethod
    def _stage_from_banner(kind, round_str):
        if kind == KIND_PREPARE:
            return Stage.PREPARE, ["准备阶段/购买经验"]
        if kind == KIND_BATTLE:
            return Stage.BATTLE, ["战斗开始/回合号兜底"]
        if kind == KIND_AUGMENT:
            return Stage.AUGMENT, ["点击卡片!选择强化效果!"]
        return Stage.CAROUSEL, ["选秀轮 %s" % round_str]

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
            if stage in self.IN_MATCH_STAGES:
                self._emit_match_ready(kind or stage.value, round_str)
                self._emit_phase(stage, round_str)

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
                result, placement = parse_match_result(matched)
                self._emit_match_finished(result, placement)
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


def _build_parser():
    parser = argparse.ArgumentParser(description="金铲铲对局执行器")
    parser.add_argument("--mode", choices=("standalone", "external"),
                        default="standalone")
    parser.add_argument("--device-id", help="ADB TCP 设备地址, 格式 host:port")
    parser.add_argument("--host", help="ADB host; 不能与 --device-id 同时使用")
    parser.add_argument("--port", type=int, help="ADB port; 不能与 --device-id 同时使用")
    parser.add_argument("--event-file", help="JSONL 事件文件")
    parser.add_argument("--stop-file", help="外部停止标记文件")
    parser.add_argument("--timeout", type=float, help="全局运行超时(秒)")
    parser.add_argument(
        "--match-ready-timeout",
        type=float,
        default=MATCH_READY_TIMEOUT,
        help="进入对局的 MATCH_READY 等待超时(秒)",
    )
    parser.add_argument("--run-id", help="外部总控生成的运行标识")
    parser.add_argument("--dry-run", action="store_true", help="只识别不操作")
    parser.add_argument("--max-rounds", type=int, default=99)
    parser.add_argument("--adb-bin", action="store_true",
                        help="通过 adb 二进制和设备 serial 连接, 支持 USB 真机/模拟器")
    parser.add_argument("--serial", default=None,
                        help="ADB 设备序列号; 与 --adb-bin 配合使用, 缺省自动选择")
    parser.add_argument("--adb-path", default="adb",
                        help="adb 可执行文件路径, 如 D:/leidian/LDPlayer14/adb.exe")
    return parser


async def _connect_sdk(sdk, endpoint, external_mode):
    transport = getattr(sdk, "_transport", None)
    if endpoint is None:
        host, port = "localhost", 5555
        target = "serial=%s" % (transport.serial or "自动发现")
        scan_if_fail = False
    else:
        host, port = endpoint.host, endpoint.port
        target = endpoint.device_id
        scan_if_fail = endpoint.host in ("localhost", "127.0.0.1", "::1")
    print("[AUTO] 连接设备 %s ..." % target)
    while True:
        try:
            await sdk.connect_only(
                port=port,
                host=host,
                scan_if_fail=scan_if_fail,
            )
            return
        except Exception as exc:  # noqa: BLE001
            if external_mode:
                raise ConnectionError(
                    f"无法连接设备 {target}: {exc}"
                ) from exc
            print("[AUTO] 连接失败(%s), 10秒后重试..." % exc)
            await asyncio.sleep(10)


async def _wait_driver_or_abort(driver, guard):
    driver_task = asyncio.create_task(driver.run())
    abort_task = asyncio.create_task(guard.wait_for_abort())
    try:
        done, _ = await asyncio.wait(
            {driver_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if driver_task in done:
            abort_task.cancel()
            with suppress(asyncio.CancelledError):
                await abort_task
            exception = driver_task.exception()
            if exception is not None:
                raise exception
            return None

        signal = abort_task.result()
        driver_task.cancel()
        with suppress(asyncio.CancelledError):
            await driver_task
        return signal
    finally:
        for task in (driver_task, abort_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(driver_task, abort_task, return_exceptions=True)


async def _run_application(args, endpoint, events):
    transport = (
        AdbBinaryTransport(adb_path=args.adb_path, serial=args.serial)
        if args.adb_bin else None
    )
    sdk = Sdk(transport=transport)
    driver = None
    try:
        try:
            await _connect_sdk(sdk, endpoint, args.mode == "external")
            await sdk.finish_init()
        except Exception as exc:  # noqa: BLE001
            events.emit("ERROR", stage="environment", message=str(exc))
            events.emit("SCRIPT_STOPPED", once=True, reason="environment_error")
            return EXIT_ENV_ERROR

        size = sdk.scaler.device_size
        density = await sdk.get_screen_density()
        binding_mode = "adb_serial" if args.adb_bin else "adb_tcp"
        device_id = sdk._transport.connected_serial if args.adb_bin else endpoint.device_id
        events.emit(
            "DEVICE_READY",
            device_id=device_id,
            binding_mode=binding_mode,
            resolution=f"{size[0]}x{size[1]}",
            dpi=density,
        )
        print("[AUTO] 已连接, 截图分辨率 %dx%d, DPI %s" % (size[0], size[1], density))

        driver = AutoLoop(
            sdk,
            dry_run=args.dry_run,
            max_rounds=args.max_rounds,
            mode=args.mode,
            events=events,
            match_ready_timeout=args.match_ready_timeout,
        )
        guard = LifecycleGuard(args.stop_file, args.timeout)
        signal = await _wait_driver_or_abort(driver, guard)
        if signal is not None:
            if signal.code == EXIT_TIMEOUT:
                current_stage = getattr(driver, "_current_stage", None)
                stage = signal.stage or (
                    current_stage.value if current_stage else "unknown"
                )
                events.emit("ERROR", stage=stage, message=signal.message)
            events.emit("SCRIPT_STOPPED", once=True, reason=signal.reason)
            return signal.code

        reason = "match_finished" if args.mode == "external" else "completed"
        events.emit("SCRIPT_STOPPED", once=True, reason=reason)
        return EXIT_OK
    except RunAbort as exc:
        if exc.code in (EXIT_BUSINESS_ERROR, EXIT_TIMEOUT):
            events.emit("ERROR", stage=exc.stage or "unknown", message=exc.message)
        events.emit("SCRIPT_STOPPED", once=True, reason=exc.reason)
        return exc.code
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        events.emit("ERROR", stage="internal", message=str(exc))
        events.emit("SCRIPT_STOPPED", once=True, reason="internal_error")
        return EXIT_BUSINESS_ERROR
    finally:
        if driver is not None:
            await driver._stop_question_watcher()
        await sdk.close()


def main():
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.adb_bin:
            if args.device_id is not None or args.host is not None or args.port is not None:
                parser.error("--adb-bin 不能与 --device-id/--host/--port 同时使用")
            endpoint = None
        else:
            if args.serial is not None or args.adb_path != "adb":
                parser.error("--serial 和 --adb-path 需要配合 --adb-bin 使用")
            endpoint = resolve_device_endpoint(args.device_id, args.host, args.port)
    except ValueError as exc:
        parser.error(str(exc))

    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.mode == "external":
        if not args.adb_bin and args.device_id is None and args.host is None and args.port is None:
            parser.error("external 模式必须指定 --device-id 或 --host/--port")
        missing = [
            name for name, value in (
                ("--event-file", args.event_file),
                ("--stop-file", args.stop_file),
                ("--timeout", args.timeout),
            ) if value is None
        ]
        if missing:
            parser.error("external 模式缺少参数: %s" % ", ".join(missing))

    requested_device_id = (
        "serial:%s" % (args.serial or "auto")
        if args.adb_bin else endpoint.device_id
    )
    binding_mode = "adb_serial" if args.adb_bin else "adb_tcp"
    events = EventWriter(args.event_file, run_id=args.run_id)
    events.emit(
        "SCRIPT_STARTED",
        mode=args.mode,
        device_id=requested_device_id,
        binding_mode=binding_mode,
        dry_run=args.dry_run,
    )
    try:
        return asyncio.run(_run_application(args, endpoint, events))
    except KeyboardInterrupt:
        events.emit("SCRIPT_STOPPED", once=True, reason="keyboard_interrupt")
        print("\n[AUTO] 用户中断, 退出")
        return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main())
