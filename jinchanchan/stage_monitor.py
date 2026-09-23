# -*- coding: utf-8 -*-
"""
环节监视器 — 测试时运行, 自动采集各环节的"启动标志"
====================================================
每 N 秒截屏 + OCR, 识别当前环节(Stage), 并把:
  - 环节切换记录 + 命中的标志词          -> 终端打印
  - 每次采样的完整 OCR 文本(带坐标)      -> logs/stage_monitor_*.log
  - 环节切换瞬间的截图                  -> images/screenshots/stages/

测试结束后, 根据日志/截图校准 agent/sdk/stages.py 里的标志词库和
agent/sdk/regions.py 里的区域坐标。

用法:
  python stage_monitor.py                # 默认连 5555, 每 6 秒采样
  python stage_monitor.py --interval 4   # 4 秒采样
  python stage_monitor.py --no-shot      # 不存截图
  停止: Ctrl+C (结束时会打印本次会话的环节切换摘要)
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2

from agent.sdk import Sdk
from agent.sdk.stages import StageDetector, Stage
from utils.logger import logger

STAGE_SHOT_DIR = os.path.join("images", "screenshots", "stages")


def _log_file(args):
    os.makedirs(args.logdir, exist_ok=True)
    name = "stage_monitor_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S")
    return open(os.path.join(args.logdir, name), "a", encoding="utf-8")


async def main():
    parser = argparse.ArgumentParser(description="金铲铲环节监视器(测试采集工具)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--interval", type=float, default=6.0, help="采样间隔(秒)")
    parser.add_argument("--logdir", default="logs")
    parser.add_argument("--no-shot", action="store_true", help="不保存环节切换截图")
    args = parser.parse_args()

    sdk = Sdk()
    print("[MONITOR] 连接雷电模拟器 %s:%d ..." % (args.host, args.port))
    await sdk.load(port=args.port, host=args.host, scan_if_fail=True)
    print("[MONITOR] 已连接, 截图分辨率 %dx%d, 每 %.0f 秒采样一次" % (
        sdk.scaler.device_size[0], sdk.scaler.device_size[1], args.interval))
    print("[MONITOR] 开始监视 (Ctrl+C 停止)")

    det = StageDetector(sdk)
    f = _log_file(args)
    f.write("# 环节监视日志 开始于 %s\n" % datetime.now())
    f.write("# 格式: 时间 | 环节 | 命中标志词 | 全部OCR文本(坐标)\n")
    f.flush()

    os.makedirs(STAGE_SHOT_DIR, exist_ok=True)
    transitions = []
    last_stage = None

    try:
        while True:
            ts = datetime.now().strftime("%H:%M:%S")
            stage, matched, texts = await det.get_stage()

            # 写日志: 完整 OCR 文本供事后挖掘标志词
            line = "%s | %s | %s" % (ts, stage.value, ",".join(matched))
            for t in texts:
                line += " | " + t
            f.write(line + "\n")
            f.flush()

            if stage != last_stage:
                transition = (last_stage, stage, matched, ts)
                transitions.append(transition)
                if last_stage is not None:
                    print("[%s] 环节切换: %s -> %s (标志: %s)" % (
                        ts, last_stage.value, stage.value, ",".join(matched)))
                else:
                    print("[%s] 初始环节: %s (标志: %s)" % (ts, stage.value, ",".join(matched)))

                if not args.no_shot:
                    screen = await sdk.get_screen()
                    if screen is not None:
                        shot = os.path.join(
                            STAGE_SHOT_DIR, "%s_%s.png" % (datetime.now().strftime("%H%M%S"), stage.value))
                        cv2.imwrite(shot, screen)
                        print("       截图已存: %s" % shot)
                last_stage = stage

            await asyncio.sleep(args.interval)

    except KeyboardInterrupt:
        pass
    finally:
        f.write("# 会话结束于 %s\n" % datetime.now())
        f.close()
        print("")
        print("=" * 50)
        print("本次会话环节切换摘要:")
        for prev, cur, matched, ts in transitions:
            prev_name = prev.value if prev else "(开始)"
            print("  [%s] %s -> %s  标志: %s" % (ts, prev_name, cur.value, ",".join(matched)))
        print("=" * 50)
        print("日志与截图已保存, 可用于校准 stages.py 标志词库")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print("监视器运行失败: %s" % e)
        traceback.print_exc()
