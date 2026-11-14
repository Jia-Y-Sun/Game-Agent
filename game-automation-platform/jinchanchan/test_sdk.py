# -*- coding: utf-8 -*-
"""
Sunflower SDK 移植版测试脚本
============================
连接雷电模拟器(ADB), 验证细粒度识别能力:
  - 自动识别当前界面(主菜单/模式选择/房间/对局中/结算)
  - 对局中: 金币 / 等级 / 回合 / 商店 5 格英雄名
  - --scan:  扫描备战区棋子(名字/星级)
  - --chess R C: 读取棋盘指定位置棋子
  - --buy N: 购买商店第 N 格(1-5)英雄
  - --xp:   购买一次经验
  - --shot: 保存当前截屏到 images/screenshots/

用法:
  python test_sdk.py                 # 连接默认 5555, 只读基础信息
  python test_sdk.py --port 5557     # 多开时指定端口
  python test_sdk.py --scan --shot   # 扫描备战区 + 保存截图
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2

from agent.sdk import Sdk, GameState
from utils.logger import logger


async def main():
    parser = argparse.ArgumentParser(description="Sunflower SDK 移植版测试")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5555, help="雷电模拟器 ADB 端口(默认 5555)")
    parser.add_argument("--scan", action="store_true", help="扫描备战区 9 个位置棋子")
    parser.add_argument("--chess", type=int, nargs=2, metavar=("ROW", "COL"), help="读取棋盘棋子")
    parser.add_argument("--buy", type=int, metavar="N", help="购买商店第 N 格(1-5)")
    parser.add_argument("--xp", action="store_true", help="购买一次经验")
    parser.add_argument("--shot", action="store_true", help="保存截屏")
    args = parser.parse_args()

    sdk = Sdk()
    print("[1/4] 连接雷电模拟器 %s:%d ..." % (args.host, args.port))
    await sdk.load(port=args.port, host=args.host, scan_if_fail=True)

    size = await sdk.get_screen_size()
    density = await sdk.get_screen_density()
    print("       设备物理分辨率: %dx%d (DPI %s)" % (size[0], size[1], density))
    print("       实际截图分辨率: %dx%d (坐标缩放 x%.3f / y%.3f)" % (
        sdk.scaler.device_size[0], sdk.scaler.device_size[1], sdk.scaler.sx, sdk.scaler.sy))

    print("[2/4] 识别当前界面 ...")
    state = await sdk.get_game_state()
    print("       当前界面: %s" % state)

    if args.shot:
        screen = await sdk.get_screen()
        if screen is not None:
            os.makedirs("images/screenshots", exist_ok=True)
            path = os.path.join("images", "screenshots", "sdk_shot.png")
            cv2.imwrite(path, screen)
            print("       截屏已保存: %s" % path)

    if state != GameState.IN_GAME:
        print("[3/4] 未在对局中, 跳过对局信息读取")
        print("       可尝试: 进入对局后重新运行; 或 --shot 查看当前画面")
        return

    print("[3/4] 读取基础对局信息 ...")
    info = await sdk.get_basic_game_info()
    if info:
        print("       金币: %s" % info.coin)
        print("       等级: %s" % info.level)
        print("       回合: %s" % ("%d-%d" % info.period if info.period else None))
        print("       商店: %s" % info.store)
    else:
        print("       读取失败(可能处于战斗环节或 OCR 未就绪)")

    if args.scan:
        print("[+] 扫描备战区棋子 ...")
        for i in range(9):
            chess = await sdk.get_chess_info((i,))
            if chess:
                print("       备战区[%d]: %s 星级=%d" % (i, chess.name, chess.star))

    if args.chess:
        row, col = args.chess
        print("[+] 读取棋盘 (%d,%d) ..." % (row, col))
        chess = await sdk.get_chess_info((row, col), check_equipment=True)
        if chess:
            print("       %s 星级=%d 装备=%s" % (chess.name, chess.star, chess.equipments))

    if args.buy:
        index = args.buy - 1
        if not 0 <= index <= 4:
            print("[x] --buy 取值 1-5")
            return
        print("[+] 购买商店第 %d 格 ..." % args.buy)
        await sdk.buy_chess(index)

    if args.xp:
        print("[+] 购买一次经验 ...")
        await sdk.buy_xp()

    print("[4/4] 测试完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        import traceback
        print("\n运行失败: %s" % e)
        traceback.print_exc()
