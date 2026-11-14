# -*- coding: utf-8 -*-
"""
退出游戏流程验证脚本(ADB + OCR 版, 对应旧 test_settlement.py)
==============================================================
流程: 点右上角齿轮 -> 结束游戏 -> 确认 -> 立即结束 -> 下一步 -> 再来一局
每一步都靠全屏 OCR 定位按钮文字点击, 找不到会打印当前屏幕文字供诊断。

用法: python test_exit.py [--port 5555]
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.sdk import Sdk
from agent.sdk.regions import SpecificButton
from agent.sdk.utils import SdkUtils
from utils.logger import logger

# 退出流程按钮序列(用户逐步确认: 齿轮 -> "退出游戏" -> "确定" -> "现在退出")
TARGETS = ["退出游戏", "确定", "现在退出", "下一步", "再来一局"]


async def click_text_exact(sdk, keyword):
    """全等匹配点击文字"""
    results = await sdk.get_screen_text(confidence=0.35)
    if not results:
        return False
    for r in results:
        if SdkUtils.text_match(r.text, keyword):
            x, y = r.x + r.width // 2, r.y + r.height // 2
            print("  点击[%s] @(%d,%d)" % (r.text, x, y))
            await sdk.click(x, y)
            return True
    return False


async def dump_texts(sdk, title):
    results = await sdk.get_screen_text(confidence=0.35)
    texts = [r.text for r in results] if results else []
    print("  [%s] 屏幕文字: %s" % (title, texts[:15]))


async def main():
    parser = argparse.ArgumentParser(description="退出游戏流程验证")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5555)
    args = parser.parse_args()

    sdk = Sdk()
    print("[1] 连接雷电模拟器 %s:%d ..." % (args.host, args.port))
    await sdk.load(port=args.port, host=args.host, scan_if_fail=True)

    # 点右上角齿轮(设置)
    print("[2] 点击右上角齿轮")
    box = sdk.scaler.scale(SpecificButton.SETTING_IN_GAME)
    x, y = box.get_middle_coordinate()
    print("  齿轮位置: (%d,%d)" % (x, y))
    await sdk.click(x, y)
    await asyncio.sleep(2)

    # 依次点击退出流程按钮: 每次 OCR 从"当前目标到结尾"扫一遍,
    # 点第一个出现的按钮, 这样中途重启/跳过也能自愈
    print("[3] 依次执行退出流程")
    results_cache = None
    for i, target in enumerate(TARGETS):
        clicked = False
        for _ in range(20):     # 每个阶段最多等 30 秒
            results = await sdk.get_screen_text(confidence=0.35)
            if not results:
                await asyncio.sleep(1.5)
                continue
            # 从当前目标开始找, 点第一个出现的
            for j in range(i, len(TARGETS)):
                for r in results:
                    if SdkUtils.text_match(r.text, TARGETS[j]):
                        x, y = r.x + r.width // 2, r.y + r.height // 2
                        print("  点击[%s] @(%d,%d)" % (r.text, x, y))
                        await sdk.click(x, y)
                        clicked = True
                        break
                if clicked:
                    break
            if clicked:
                break
            await asyncio.sleep(1.5)
        if not clicked:
            texts = [r.text for r in results] if results else []
            print("  [x] 未找到[%s], 屏幕文字: %s" % (target, texts[:15]))
        await asyncio.sleep(2)

    print("[4] 退出流程验证完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        import traceback
        print("运行失败: %s" % e)
        traceback.print_exc()
