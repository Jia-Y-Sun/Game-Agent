import pyautogui
import time
import random
from config import CLICK_DELAY
from utils.logger import logger


pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


def click(x, y, delay=CLICK_DELAY):
    """点击指定坐标，带微随机偏移和延迟"""
    offset_x = random.randint(-3, 3)
    offset_y = random.randint(-3, 3)
    pyautogui.click(x + offset_x, y + offset_y)
    time.sleep(delay)


def double_click(x, y, delay=CLICK_DELAY):
    """双击"""
    click(x, y, 0.1)
    click(x, y, delay)


def drag(start_x, start_y, end_x, end_y, duration=0.3):
    """拖拽操作"""
    pyautogui.moveTo(start_x, start_y)
    pyautogui.drag(end_x - start_x, end_y - start_y, duration=duration)
    time.sleep(CLICK_DELAY)


def move_to(x, y):
    """移动鼠标"""
    pyautogui.moveTo(x, y)


def scroll(clicks=1):
    """滚动滚轮"""
    pyautogui.scroll(clicks)
    time.sleep(CLICK_DELAY)


def click_center():
    """点击屏幕中心（选英雄时使用）"""
    from config import ScreenCoords
    click(ScreenCoords.SCREEN_CENTER[0], ScreenCoords.SCREEN_CENTER[1], delay=0.1)
