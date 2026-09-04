# -*- coding: utf-8 -*-
"""
金铲铲之战 自动化操作Agent
===========================
简化版功能：
  1. 进入游戏（依次点击进入游戏按钮）
  2. 识别"?"圆圈并自动点击抓取
  3. 识别"准备阶段"标识，20秒倒计时显示在终端
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.game_flow import GameFlow
from utils.logger import logger


def main():
    from config import IMAGE_DIR
    if not os.path.exists(IMAGE_DIR):
        os.makedirs(IMAGE_DIR)
        logger.info(f"已创建图片目录: {IMAGE_DIR}")
        logger.info("请将按钮截图放入该目录后重新运行")
        return

    images = [f for f in os.listdir(IMAGE_DIR) if f.endswith(('.png', '.jpg', '.jpeg'))]
    if not images:
        logger.warning("图片资源目录为空！请先放入按钮截图")
        return

    logger.info(f"检测到 {len(images)} 个图片资源")
    for img in images:
        logger.info(f"  - {img}")

    flow = GameFlow()
    flow.run()


if __name__ == "__main__":
    main()
