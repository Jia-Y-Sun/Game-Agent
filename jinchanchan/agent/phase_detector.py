import time
import pyautogui
import numpy as np
import cv2
from agent.image_recognition import capture_screen, find_image
from agent.ocr_helper import read_round_number, read_countdown, ocr_text
from config import ButtonImages


# 顶部区域坐标（2560x1600基准）
ROUND_REGION = (0, 0, 800, 150)       # 轮数区域 1-1, 1-2等
COUNTDOWN_REGION = (1000, 0, 1560, 150)   # 倒计时区域
PHASE_REGION = (0, 0, 2560, 150)        # 阶段文字区域（准备阶段/战斗开始）扩大范围

# 野怪轮次
MONSTER_ROUNDS = {"1-1", "1-2", "1-3", "2-1", "2-7", "3-7", "4-7", "5-7", "6-7", "7-7"}


def _scale_region(reg):
    sw, sh = pyautogui.size()
    x = int(reg[0] * sw / 2560)
    y = int(reg[1] * sh / 1600)
    w = int(reg[2] * sw / 2560)
    h = int(reg[3] * sh / 1600)
    return (x, y, w, h)


def detect_round():
    region = _scale_region(ROUND_REGION)
    screen = capture_screen(region)
    if screen is None or screen.size == 0:
        return None
    round_str = read_round_number(screen)
    return round_str


def is_monster_round(round_str):
    if not round_str:
        return False
    return round_str in MONSTER_ROUNDS


def is_pvp_round(round_str):
    if not round_str:
        return False
    return round_str not in MONSTER_ROUNDS


def detect_countdown():
    region = _scale_region(COUNTDOWN_REGION)
    screen = capture_screen(region)
    if screen is None or screen.size == 0:
        return None
    cd = read_countdown(screen)
    return cd


def countdown_seconds(cd_str):
    if not cd_str:
        return None
    try:
        parts = cd_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1:
            return int(parts[0])
    except:
        pass
    return None


def _check_phase_text(image, target):
    if image is None or image.size == 0:
        return False
    text = ocr_text(image)
    return target in text


def detect_prepare_phase():
    # 方法1: 图片识别
    pos = find_image(ButtonImages.PREPARE_PHASE_SIGN)
    if pos:
        return True
    # 方法2: OCR精确识别"准备阶段"
    region = _scale_region(PHASE_REGION)
    screen = capture_screen(region)
    return _check_phase_text(screen, "准备阶段")


def detect_battle_phase():
    # 方法1: 图片识别
    pos = find_image(ButtonImages.BATTLE_PHASE_SIGN)
    if pos:
        return True
    # 方法2: OCR精确识别"战斗开始"
    region = _scale_region(PHASE_REGION)
    screen = capture_screen(region)
    return _check_phase_text(screen, "战斗开始")


def wait_for_prepare_phase(timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        if detect_prepare_phase():
            print("  [PHASE] Prepare phase detected")
            return True
        time.sleep(0.5)
    return False


def wait_for_battle_phase(timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        if detect_battle_phase():
            print("  [PHASE] Battle phase detected")
            return True
        time.sleep(0.5)
    return False


def is_carousel_round(round_str):
    if not round_str:
        return False
    parts = round_str.split("-")
    if len(parts) == 2:
        sub = int(parts[1])
        return sub == 4 and int(parts[0]) >= 2
    return False


def detect_question_mark():
    from agent.image_recognition import find_question_mark_superfast
    return find_question_mark_superfast()