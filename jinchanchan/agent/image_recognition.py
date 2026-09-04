import os
import cv2
import numpy as np
import mss
import pyautogui
from config import ButtonImages, CONFIDENCE_THRESHOLD, CONFIDENCE_HIGH, CONFIDENCE_LOW, SCREENSHOT_DIR
from utils.logger import logger


# ==========================================================
# 使用 mss 实现高速截图（比 PIL 快 3~5 倍）
# ==========================================================
_sct = mss.mss()


def capture_screen(region=None):
    """截取屏幕区域，返回OpenCV图像(BGR格式)
    使用 mss 库实现高性能截图
    """
    if region:
        left, top, width, height = region
        monitor = {"left": left, "top": top, "width": width, "height": height}
    else:
        monitor = _sct.monitors[1]  # 主显示器

    img = _sct.grab(monitor)
    return cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)


# ==========================================================
# 问号圆圈专用搜索区域（根据游戏UI布局缩小范围）
# 通常出现在屏幕右侧中间区域，或在棋盘中间
# ==========================================================
QUESTION_MARK_REGIONS = [
    # 区域1: 屏幕右侧中部（最常出现）
    (1300, 200, 1000, 800),
    # 区域2: 棋盘中央区域
    (600, 400, 1000, 800),
    # 区域3: 屏幕左上角（选英雄阶段可能出现）
    (100, 100, 700, 600),
    # 区域4: 屏幕底部（商店区）
    (600, 1200, 1200, 300),
]


def _match_single_template(screen_bgr, template_path, region_offset, confidence=0.55):
    """在截图中匹配单个模板，返回坐标或None
    支持多尺度匹配以适应不同分辨率
    """
    if not os.path.exists(template_path):
        return None
    template = cv2.imread(template_path)
    if template is None:
        return None
    t_h, t_w = template.shape[:2]
    try:
        s_h, s_w = screen_bgr.shape[:2]

        # 如果区域很小，直接用固定缩放0.5倍快速匹配（同原有逻辑）
        if s_w < 500 and s_h < 500:
            small_screen = cv2.resize(screen_bgr, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_LINEAR)
            small_template = cv2.resize(template, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_LINEAR)
            gray_screen = cv2.cvtColor(small_screen, cv2.COLOR_BGR2GRAY)
            gray_template = cv2.cvtColor(small_template, cv2.COLOR_BGR2GRAY)
            result = cv2.matchTemplate(gray_screen, gray_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val >= confidence:
                cx = region_offset[0] + (max_loc[0] + t_w // 2) * 2
                cy = region_offset[1] + (max_loc[1] + t_h // 2) * 2
                return (cx, cy, max_val, os.path.basename(template_path))
            return None

        # 大面积区域：用多尺度匹配
        # 计算屏幕相对于基准1474x824的缩放
        base_w, base_h = 2560, 1600
        scale = min(s_w / base_w, s_h / base_h)
        if abs(scale - 1.0) > 0.15:
            new_tw = int(t_w * scale)
            new_th = int(t_h * scale)
            if new_tw > 0 and new_th > 0:
                scaled_template = cv2.resize(template, (new_tw, new_th), interpolation=cv2.INTER_LINEAR)
                gray_screen = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
                gray_template = cv2.cvtColor(scaled_template, cv2.COLOR_BGR2GRAY)
                result = cv2.matchTemplate(gray_screen, gray_template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val >= confidence:
                    cx = region_offset[0] + max_loc[0] + new_tw // 2
                    cy = region_offset[1] + max_loc[1] + new_th // 2
                    return (cx, cy, max_val, os.path.basename(template_path))

        # 退回到原始大小匹配
        gray_screen = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        gray_template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_screen, gray_template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= confidence:
            cx = region_offset[0] + max_loc[0] + t_w // 2
            cy = region_offset[1] + max_loc[1] + t_h // 2
            return (cx, cy, max_val, os.path.basename(template_path))
    except:
        pass
    return None


def _scan_question_marks(regions=None, confidence=0.70, log_find=True):
    """通用问号扫描：在指定区域中同时匹配两种?模板"
    返回 (x, y) 或 None
    注意：两种问号用不同置信度，第一种用高阈值避免误报
    """
    if regions is None:
        regions = QUESTION_MARK_REGIONS
    # 定义两种问号及其各自的置信度
    templates = [
        (ButtonImages.QUESTION_MARK, "question_mark_1", 0.65),
        (ButtonImages.QUESTION_MARK_2, "question_mark_2", 0.55),
    ]
    for region in regions:
        try:
            screen = capture_screen(region)
            if screen is None or screen.size == 0:
                continue
            for tmpl_path, tmpl_name, tmpl_conf in templates:
                result = _match_single_template(screen, tmpl_path, region, tmpl_conf)
                if result:
                    cx, cy, conf, name = result
                    if log_find:
                        logger.info(f"[问号检测] 找到{name} 置信度={conf:.2f} 坐标=({cx},{cy})")
                    return (cx, cy)
        except:
            continue
    return None


def find_question_mark_fast():
    """高速检测?圆圈（双区域 + 双模板）"""
    return _scan_question_marks()


# ==========================================================
# 图片查找（通用版）
# ==========================================================
def find_image(template_path, region=None, confidence=CONFIDENCE_THRESHOLD):
    """在屏幕中查找模板图片，返回中心坐标 (x, y) 或 None
    真正的多尺度匹配：尝试将模板缩放到多种比例在屏幕上匹配
    """
    if not os.path.exists(template_path):
        return None

    try:
        screen = capture_screen(region)
        template = cv2.imread(template_path)
        if template is None:
            return None

        t_h, t_w = template.shape[:2]
        s_h, s_w = screen.shape[:2]
        region_offset_x = region[0] if region else 0
        region_offset_y = region[1] if region else 0

        # 当前屏幕2560x1600，模板在小窗口截图，最佳缩放为0.4倍
        scales = [0.4, 0.6, 0.8, 1.0]

        for sc in scales:
            new_tw = max(int(t_w * sc), 10)
            new_th = max(int(t_h * sc), 10)
            if new_tw > s_w or new_th > s_h:
                continue
            try:
                scaled_template = cv2.resize(template, (new_tw, new_th), interpolation=cv2.INTER_LINEAR)
                result = cv2.matchTemplate(screen, scaled_template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > confidence:
                    cx = region_offset_x + max_loc[0] + new_tw // 2
                    cy = region_offset_y + max_loc[1] + new_th // 2
                    logger.info(f"找到图片 [{os.path.basename(template_path)}] 置信度={max_val:.2f} 坐标=({cx},{cy}) [缩放%.1f倍]" % sc)
                    return (cx, cy)
            except:
                continue

        return None

    except Exception as e:
        logger.error(f"图片识别异常 [{os.path.basename(template_path)}]: {e}")
        return None


def find_all_images(template_path, region=None, confidence=CONFIDENCE_THRESHOLD):
    """查找所有匹配位置，返回坐标列表"""
    if not os.path.exists(template_path):
        return []

    try:
        screen = capture_screen(region)
        template = cv2.imread(template_path)
        if template is None:
            return []

        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        h, w = template.shape[:2]
        locations = np.where(result >= confidence)
        points = []
        base_x, base_y = (region[0], region[1]) if region else (0, 0)

        for pt in zip(*locations[::-1]):
            cx = base_x + pt[0] + w // 2
            cy = base_y + pt[1] + h // 2
            points.append((cx, cy))

        # 去重
        unique = []
        for p in points:
            if not any(abs(p[0] - u[0]) < 20 and abs(p[1] - u[1]) < 20 for u in unique):
                unique.append(p)
        return unique

    except Exception as e:
        logger.error(f"批量识别异常: {e}")
        return []


def wait_for_image(template_path, region=None, confidence=CONFIDENCE_LOW,
                   timeout=30, interval=0.5):
    """等待图片出现，超时返回 None"""
    import time
    start = time.time()
    while time.time() - start < timeout:
        pos = find_image(template_path, region, confidence)
        if pos:
            return pos
        time.sleep(interval)
    logger.warning(f"等待超时: {os.path.basename(template_path)} ({timeout}s)")
    return None


def click_image(template_path, region=None, confidence=CONFIDENCE_LOW, timeout=10):
    try:
        pos = wait_for_image(template_path, region, confidence, timeout)
        if pos:
            pyautogui.click(pos[0], pos[1])
            return True
    except:
        pass
    return False

def find_question_mark_superfast():
    """超快速问号检测 — 只搜区域1 + 双模板（供守护线程使用）"""
    return _scan_question_marks(regions=QUESTION_MARK_REGIONS, log_find=False)


def find_question_mark():
    """对外接口：检测?圆圈（双区域 + 双模板）"""
    return find_question_mark_fast()
