#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Android QoE (Quality of Experience) 自动标注脚本
每秒自动采集屏幕截图，分析卡顿状态和分辨率

输出格式与样本样例保持一致:
    file_name,time,rtt,trust_resolution,trust_stall

依赖安装:
    pip install opencv-python numpy

使用方法:
    python qoe_monitor.py [--device DEVICE_ID] [--interval 1.0] [--output ./qoe_data] [--pcap-name xxx.pcap]
"""

import os
import sys
import csv
import time
import signal
import argparse
import subprocess
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from collections import Counter

import cv2
import numpy as np


def _adb_binary() -> str:
    """Return the configured adb executable for subprocess calls."""
    return (
        os.getenv('MINI_PILOT_ADB_BIN')
        or os.getenv('ADB_BIN')
        or 'adb'
    )


def _read_image(path) -> Optional[np.ndarray]:
    """Read an image through bytes so Windows non-ASCII paths work."""
    try:
        raw = np.fromfile(str(path), dtype=np.uint8)
        if raw.size == 0:
            return None
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None


# ==================== 可选 OCR 依赖 ====================
try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

try:
    from rapidocr_onnxruntime import RapidOCR
    RAPIDOCR_AVAILABLE = True
except ImportError:
    RAPIDOCR_AVAILABLE = False

LATENCY_MS_RE = re.compile(r'(?<!\d)(\d{1,4})\s*m\s*s\b', re.IGNORECASE)
MAX_PLAUSIBLE_LATENCY_MS = 2000

# ==================== APP 检测配置 ====================
APP_DETECTION_PROFILES: Dict[str, Dict] = {
    'kuaishou': {
        'resolution_regions': [
            {'name': '右下画质', 'bbox': (0.72, 0.86, 0.96, 0.97)},
            {'name': '右上设置', 'bbox': (0.82, 0.02, 0.98, 0.12)},
        ],
        'resolution_keywords': {
            '流畅': 360, '高清': 720, '超清': 1080, '蓝光': 1080, '4K': 2160,
            '360': 360, '480': 480, '720': 720, '1080': 1080, '1440': 1440, '2160': 2160,
        },
        'loading_cfg': {
            # 模仿抖音：极小中央 ROI，正常视频页跳过圆形检测（头像/点赞按钮易误判）
            'center_roi': (0.38, 0.35, 0.62, 0.60),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.12,
            'min_concentric_circles': 1,
            'skip_circle_on_video_feed': True,
        }
    },
    'douyin': {
        'resolution_regions': [
            {'name': '右下画质', 'bbox': (0.75, 0.85, 0.95, 0.97)},
            {'name': '右下清晰度', 'bbox': (0.70, 0.88, 0.92, 0.98)},
        ],
        'resolution_keywords': {
            '流畅': 360, '高清': 720, '超清': 1080, '蓝光': 1080,
            '360': 360, '480': 480, '720': 720, '1080': 1080,
        },
        'loading_cfg': {
            # 极度缩小ROI：只检测正中央极小区域，抖音字幕/歌词通常不会出现在这里
            'center_roi': (0.38, 0.35, 0.62, 0.60),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.12,
            'min_concentric_circles': 1,
            # 抖音正常视频页有大量圆形UI（头像、音乐唱片、点赞按钮），直接跳过圆形检测
            'skip_circle_on_video_feed': True,
        }
    },
    'mango': {
        'resolution_regions': [
            {'name': '右下清晰度', 'bbox': (0.70, 0.88, 0.90, 0.98)},
            {'name': '右上设置', 'bbox': (0.80, 0.02, 0.98, 0.15)},
        ],
        'resolution_keywords': {
            '流畅': 360, '清晰': 480, '高清': 720, '超清': 1080, '蓝光': 1080,
            '360': 360, '480': 480, '720': 720, '1080': 1080,
        },
        'loading_cfg': {
            # 模仿抖音：极小中央 ROI，正常视频页跳过圆形检测
            'center_roi': (0.38, 0.35, 0.62, 0.60),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.12,
            'min_concentric_circles': 1,
            'skip_circle_on_video_feed': True,
        }
    },
    'bilibili': {
        'resolution_regions': [
            {'name': '右下画质', 'bbox': (0.72, 0.86, 0.92, 0.97)},
            {'name': '右上设置', 'bbox': (0.78, 0.02, 0.96, 0.14)},
        ],
        'resolution_keywords': {
            '流畅': 360, '清晰': 480, '高清': 720, '超清': 1080, '1080P': 1080,
            '360': 360, '480': 480, '720': 720, '1080': 1080,
        },
        'loading_cfg': {
            # 模仿抖音：极小中央 ROI，正常视频页跳过圆形检测
            'center_roi': (0.38, 0.35, 0.62, 0.60),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.12,
            'min_concentric_circles': 1,
            'skip_circle_on_video_feed': True,
        }
    },
    'tencent_video': {
        'resolution_regions': [
            {'name': '右下清晰度', 'bbox': (0.70, 0.87, 0.92, 0.98)},
            {'name': '右上设置', 'bbox': (0.80, 0.02, 0.98, 0.14)},
        ],
        'resolution_keywords': {
            '流畅': 360, '标清': 480, '高清': 720, '超清': 1080, '蓝光': 1080,
            '360': 360, '480': 480, '720': 720, '1080': 1080,
        },
        'loading_cfg': {
            # 模仿抖音：极小中央 ROI，正常视频页跳过圆形检测
            'center_roi': (0.38, 0.35, 0.62, 0.60),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.12,
            'min_concentric_circles': 1,
            'skip_circle_on_video_feed': True,
        }
    },
    'xiaohongshu': {
        'resolution_regions': [
            {'name': '右下画质', 'bbox': (0.75, 0.86, 0.95, 0.97)},
        ],
        'resolution_keywords': {
            '流畅': 360, '高清': 720, '超清': 1080,
            '360': 360, '480': 480, '720': 720, '1080': 1080,
        },
        'loading_cfg': {
            # 模仿抖音：极小中央 ROI，正常视频页跳过圆形检测
            'center_roi': (0.38, 0.35, 0.62, 0.60),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.12,
            'min_concentric_circles': 1,
            'skip_circle_on_video_feed': True,
        }
    },
    'honorofkings': {
        'resolution_regions': [
            {'name': '右下', 'bbox': (0.72, 0.85, 0.96, 0.98)},
            {'name': '右上', 'bbox': (0.78, 0.02, 0.98, 0.15)},
        ],
        'resolution_keywords': {
            '流畅': 360, '高清': 720, '超清': 1080, '蓝光': 1080, '4K': 2160,
        },
        'loading_cfg': {
            # MOBA HUD contains many circles; only the strongest spinner is useful.
            'center_roi': (0.25, 0.25, 0.75, 0.75),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.15,
            'min_concentric_circles': 2,
            'skip_circle_on_video_feed': True,
            'skip_loading_detection': True,
        }
    },
    'default': {
        'resolution_regions': [
            {'name': '右下', 'bbox': (0.72, 0.85, 0.96, 0.98)},
            {'name': '右上', 'bbox': (0.78, 0.02, 0.98, 0.15)},
            {'name': '下中右', 'bbox': (0.60, 0.88, 0.82, 0.98)},
        ],
        'resolution_keywords': {
            '流畅': 360, '清晰': 480, '高清': 720, '超清': 1080, '蓝光': 1080, '4K': 2160,
            '360': 360, '480': 480, '720': 720, '1080': 1080, '1440': 1440, '2160': 2160,
        },
        'loading_cfg': {
            'center_roi': (0.25, 0.25, 0.75, 0.75),
            'circle_min_radius_ratio': 0.03,
            'circle_max_radius_ratio': 0.15,
            'min_concentric_circles': 1,
        }
    }
}


def get_app_profile(app_name: Optional[str]) -> Dict:
    """获取APP检测配置"""
    if app_name and app_name in APP_DETECTION_PROFILES:
        return APP_DETECTION_PROFILES[app_name]
    return APP_DETECTION_PROFILES['default']


# ==================== 页面分类器 ====================
class PageClassifier:
    """抖音页面分类器 - 区分正常视频页、广告页、团购页、直播入口页等"""

    # 关键词映射：page_type -> 关联关键词列表
    KEYWORD_MAP = {
        'ad_page': ['广告', '查看详情', '立即下载', ' Sponsored'],
        'group_buy_page': ['团购', '购买', '¥', '元', '优惠券', '立即抢购', '下单'],
        'live_entry_page': ['直播中', 'Live', '进入直播间', '红包', '抢', '热门直播'],
        'search_page': ['搜索', '综合', '视频', '用户', '商品', '筛选'],
        'shop_page': ['商城', '购物车', '店铺', '销量', '好评', '商品', '进店'],
    }

    def __init__(self, app_name: Optional[str] = None):
        self.app_name = app_name
        self.page_type_history: List[str] = []
        self.history_size = 3

    def classify(self, image_path: str) -> Tuple[str, Dict]:
        """对截图进行页面分类，返回 (page_type, details)"""
        if self.app_name != 'douyin':
            return 'unknown', {}

        img = _read_image(image_path)
        if img is None:
            return 'unknown', {}

        h, w = img.shape[:2]

        # 1. 结构特征
        has_right_buttons = self._detect_right_buttons(img, h, w)
        has_bottom_tab = self._detect_bottom_tab(img, h, w)
        center_is_video_like = self._detect_center_video_like(img, h, w)

        # 2. OCR 关键词
        keywords_found = self._detect_keywords(img, h, w)

        # 3. 综合判断
        page_type = self._judge_page_type(
            has_right_buttons, has_bottom_tab, center_is_video_like, keywords_found
        )

        # 5. 时间上下文修正：若当前帧丢失 UI 但近期是视频 feed，可能是全屏沉浸模式
        #    常见于右侧按钮自动隐藏或暗场画面导致 center_is_video_like 为 False
        if page_type in {'other_page', 'unknown'} and h > w and not has_bottom_tab:
            if self.page_type_history:
                recent_types = set(self.page_type_history[-2:])
                if recent_types & {'normal_video_feed', 'fullscreen_video_feed'}:
                    # 无强业务关键词时，判定为全屏视频
                    strong_keywords = set()
                    for kws in self.KEYWORD_MAP.values():
                        strong_keywords.update(kws)
                    if not any(kw in keywords_found for kw in strong_keywords):
                        page_type = 'fullscreen_video_feed'

        # 4. 滑动窗口稳定
        self.page_type_history.append(page_type)
        if len(self.page_type_history) > self.history_size:
            self.page_type_history.pop(0)

        if len(self.page_type_history) >= 2:
            type_counts = Counter(self.page_type_history)
            most_common = type_counts.most_common(1)[0]
            if most_common[1] >= 2:
                page_type = most_common[0]

        return page_type, {
            'has_right_buttons': has_right_buttons,
            'has_bottom_tab': has_bottom_tab,
            'center_is_video_like': center_is_video_like,
            'keywords_found': keywords_found,
        }

    def _detect_right_buttons(self, img: np.ndarray, h: int, w: int) -> bool:
        """检测右侧是否有垂直排列的按钮列（点赞/评论/分享/头像）"""
        roi = img[int(h*0.35):int(h*0.85), int(w*0.78):int(w*0.98)]
        if roi.size == 0:
            return False

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # 方法1：霍夫圆检测
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=25,
            param1=80, param2=25, minRadius=10, maxRadius=45
        )
        if circles is not None and len(circles[0]) >= 3:
            return True

        # 方法2：边缘密度垂直分布
        edges = cv2.Canny(gray, 50, 150)
        h_proj = np.sum(edges > 0, axis=1)
        if len(h_proj) == 0:
            return False

        mean_val = float(np.mean(h_proj))
        if mean_val == 0:
            return False

        # 找局部峰值
        peaks = 0
        for i in range(5, len(h_proj)-5):
            local_window = h_proj[max(0, i-5):min(len(h_proj), i+6)]
            local_max = int(np.max(local_window))
            if int(h_proj[i]) == local_max and h_proj[i] > mean_val * 1.5:
                peaks += 1

        return peaks >= 3

    def _detect_bottom_tab(self, img: np.ndarray, h: int, w: int) -> bool:
        """检测底部是否有固定的导航 Tab 栏"""
        # 排除最底部 5% 的系统手势/导航条区域，避免全屏视频被误判为有 Tab
        bottom_roi = img[int(h*0.88):int(h*0.95), 0:w]
        above_roi = img[int(h*0.82):int(h*0.88), 0:w]

        if bottom_roi.size == 0 or above_roi.size == 0:
            return False

        def edge_density(roi: np.ndarray) -> float:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            if edges.size == 0:
                return 0.0
            return float(np.sum(edges > 0) / edges.size)

        bottom_density = edge_density(bottom_roi)
        above_density = edge_density(above_roi)

        # Tab 栏区域边缘密度明显高于上方，且自身有一定密度
        return bottom_density > above_density * 1.3 and bottom_density > 0.015

    def _detect_center_video_like(self, img: np.ndarray, h: int, w: int) -> bool:
        """检测中心区域是否像视频（有纹理、非纯文字/卡片）"""
        roi = img[int(h*0.15):int(h*0.82), int(w*0.1):int(w*0.75)]
        if roi.size == 0:
            return False

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        variance = float(np.var(gray))

        # 视频区域通常方差较大
        if variance < 400:
            return False

        edges = cv2.Canny(gray, 50, 150)
        edge_ratio = float(np.sum(edges > 0) / edges.size)

        # 视频页边缘密度适中；纯文字页偏低，UI密集页偏高
        return 0.015 < edge_ratio < 0.22

    def _detect_keywords(self, img: np.ndarray, h: int, w: int) -> List[str]:
        """通过 OCR 检测页面关键词"""
        if not PYTESSERACT_AVAILABLE:
            return []

        # 检测几个关键区域
        regions = [
            (0.0, 0.0, 1.0, 0.15),      # 顶部
            (0.0, 0.85, 1.0, 1.0),      # 底部
            (0.0, 0.2, 0.6, 0.8),       # 左侧中部
            (0.6, 0.1, 1.0, 0.5),       # 右上（红包/广告标签常见区域）
        ]

        all_text = ""
        for roi_norm in regions:
            x1, y1, x2, y2 = int(w*roi_norm[0]), int(h*roi_norm[1]), int(w*roi_norm[2]), int(h*roi_norm[3])
            roi = img[y1:y2, x1:x2]
            if roi.size == 0:
                continue

            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            try:
                _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                text = pytesseract.image_to_string(
                    binary,
                    config='--psm 6 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789直播红包搜索商城广告团购购买查看详情立即下载进入直播间优惠券立即抢购¥元销量好评店铺商品用户综合视频筛选下单进店热门直播'
                ).strip()
                all_text += text + " "
            except Exception:
                continue

        found = []
        for page_type, kws in self.KEYWORD_MAP.items():
            for kw in kws:
                if kw in all_text:
                    found.append(kw)

        return list(set(found))

    def _judge_page_type(self, has_right_buttons: bool, has_bottom_tab: bool,
                         center_is_video_like: bool, keywords_found: List[str]) -> str:
        """综合判断页面类型"""
        # 关键词强信号
        keyword_signals = {
            'ad_page': ['广告', '查看详情', '立即下载'],
            'group_buy_page': ['团购', '优惠券', '立即抢购', '下单'],
            'live_entry_page': ['直播中', '进入直播间', '红包', '抢', '热门直播'],
            'search_page': ['搜索', '综合', '用户', '商品', '筛选'],
            'shop_page': ['商城', '购物车', '店铺', '销量', '好评', '商品', '进店'],
        }

        for page_type, kws in keyword_signals.items():
            if any(kw in keywords_found for kw in kws):
                return page_type

        # 结构信号
        if has_right_buttons and center_is_video_like and not has_bottom_tab:
            return 'normal_video_feed'

        # 全屏沉浸式竖屏视频：右侧按钮列自动隐藏、无底部 Tab、中心区域仍为视频
        if not has_right_buttons and center_is_video_like and not has_bottom_tab:
            return 'fullscreen_video_feed'

        if has_bottom_tab and not has_right_buttons:
            # 有底部Tab但没有右侧按钮列，可能是搜索/商城/团购等
            if any(kw in keywords_found for kw in keyword_signals['search_page']):
                return 'search_page'
            if any(kw in keywords_found for kw in keyword_signals['shop_page']):
                return 'shop_page'
            if any(kw in keywords_found for kw in keyword_signals['group_buy_page']):
                return 'group_buy_page'
            return 'other_page'

        if not center_is_video_like and not has_right_buttons:
            return 'other_page'

        # 有右侧按钮时，若中心区域也像视频，则优先判定为正常视频 feed
        # （底部 Tab 检测易受系统导航条/播放器控件干扰，不能单独否决视频页）
        if has_right_buttons:
            if has_bottom_tab and not center_is_video_like:
                return 'other_page'
            return 'normal_video_feed'

        return 'unknown'


# ==================== 数字识别器 (Hu矩+KNN) ====================
class DigitRecognizer:
    """基于Hu矩特征的数字/字母识别器"""
    
    CHARS = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'P']
    
    def __init__(self):
        self.templates: Dict[str, np.ndarray] = {}
        self._build_templates()
    
    def _draw_char(self, char: str, size: Tuple[int, int] = (40, 28)) -> np.ndarray:
        """生成标准字符模板"""
        img = np.zeros(size, dtype=np.uint8)
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 1.0
        thickness = 2
        # 获取文字大小以居中
        (text_w, text_h), _ = cv2.getTextSize(char, font, scale, thickness)
        x = (size[1] - text_w) // 2
        y = (size[0] + text_h) // 2
        cv2.putText(img, char, (x, y), font, scale, 255, thickness, cv2.LINE_AA)
        return img
    
    def _extract_features(self, img: np.ndarray) -> np.ndarray:
        """提取Hu矩+形状特征"""
        # 统一尺寸
        img = cv2.resize(img, (28, 40))
        # 二值化
        _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        # Hu矩
        moments = cv2.moments(binary)
        hu = cv2.HuMoments(moments).flatten()
        # 防止log0
        hu = np.sign(hu) * np.log10(np.abs(hu) + 1e-10)
        # 轮廓辅助特征
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cnt = max(contours, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(cnt)
            aspect = w / h if h > 0 else 0
            area_ratio = cv2.contourArea(cnt) / (w * h) if w * h > 0 else 0
            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull) if len(hull) >= 3 else 1e-6
            solidity = cv2.contourArea(cnt) / hull_area
        else:
            aspect = area_ratio = solidity = 0.0
        return np.concatenate([hu, [aspect, area_ratio, solidity]])
    
    def _build_templates(self):
        for char in self.CHARS:
            tpl = self._draw_char(char)
            feat = self._extract_features(tpl)
            self.templates[char] = feat
    
    def recognize(self, roi_img: np.ndarray) -> Tuple[Optional[str], float]:
        """识别单个字符区域"""
        if roi_img.size == 0:
            return None, float('inf')
        feat = self._extract_features(roi_img)
        best_char = None
        best_dist = float('inf')
        for char, tpl_feat in self.templates.items():
            dist = np.linalg.norm(feat - tpl_feat)
            if dist < best_dist:
                best_dist = dist
                best_char = char
        return best_char, best_dist
    
    def recognize_sequence(self, roi_img: np.ndarray) -> Tuple[str, List[float]]:
        """识别ROI中的字符序列（数字+P）"""
        gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY) if len(roi_img.shape) == 3 else roi_img
        
        # 使用MSER提取文字候选区
        mser = cv2.MSER_create()
        mser.setMinArea(50)
        mser.setMaxArea(int(gray.size * 0.3))
        regions, _ = mser.detectRegions(gray)
        
        boxes = []
        for region in regions:
            x, y, w, h = cv2.boundingRect(region)
            aspect = w / h if h > 0 else 0
            if h < 10 or w < 6 or aspect > 1.3 or aspect < 0.15:
                continue
            # 填充率过滤
            area = cv2.contourArea(region)
            if w * h == 0 or area / (w * h) < 0.15:
                continue
            boxes.append((x, y, w, h))
        
        if not boxes:
            # MSER失败，回退到轮廓分割
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                aspect = w / h if h > 0 else 0
                if h < 10 or w < 5 or aspect > 1.3 or aspect < 0.15:
                    continue
                boxes.append((x, y, w, h))
        
        if not boxes:
            return "", []
        
        # 非极大值抑制合并重叠框
        boxes = self._nms_boxes(boxes, threshold=0.3)
        # 按x坐标排序
        boxes.sort(key=lambda b: b[0])
        
        result = []
        confidences = []
        for x, y, w, h in boxes:
            pad = 2
            x1, y1 = max(0, x-pad), max(0, y-pad)
            x2, y2 = min(gray.shape[1], x+w+pad), min(gray.shape[0], y+h+pad)
            char_img = gray[y1:y2, x1:x2]
            char, dist = self.recognize(char_img)
            if char and dist < 3.5:  # 距离阈值
                result.append(char)
                confidences.append(dist)
        
        return "".join(result), confidences
    
    @staticmethod
    def _nms_boxes(boxes: List[Tuple], threshold: float = 0.3) -> List[Tuple]:
        """简单的NMS"""
        if not boxes:
            return []
        boxes = sorted(boxes, key=lambda b: b[2]*b[3], reverse=True)
        keep = []
        while boxes:
            current = boxes[0]
            keep.append(current)
            cx1, cy1, cw, ch = current
            ca = cw * ch
            boxes = boxes[1:]
            filtered = []
            for b in boxes:
                bx1, by1, bw, bh = b
                inter_x1 = max(cx1, bx1)
                inter_y1 = max(cy1, by1)
                inter_x2 = min(cx1+cw, bx1+bw)
                inter_y2 = min(cy1+ch, by1+bh)
                inter_area = max(0, inter_x2-inter_x1) * max(0, inter_y2-inter_y1)
                union_area = ca + bw*bh - inter_area
                iou = inter_area / union_area if union_area > 0 else 0
                if iou < threshold:
                    filtered.append(b)
            boxes = filtered
        return keep


# ==================== 加载检测器 ====================
class LoadingDetector:
    """检测屏幕中央的加载圈和加载文字"""
    
    def __init__(self, profile: Dict):
        self.cfg = profile.get('loading_cfg', APP_DETECTION_PROFILES['default']['loading_cfg'])
    
    def detect(self, image_path: str, page_type: str = 'unknown') -> Tuple[bool, str]:
        """
        检测是否正在加载
        Args:
            image_path: 截图路径
            page_type: 页面类型（用于跳过非视频页的 loading_circle 误判）
        Returns: (is_loading, reason)
        """
        try:
            img = _read_image(image_path)
            if img is None:
                return False, ""

            if self.cfg.get('skip_loading_detection', False):
                return False, ""
            
            # 非视频页面（团购/直播入口/搜索/商城等）跳过 loading_circle 检测，避免圆形UI误判
            non_video_pages = {'group_buy_page', 'live_entry_page', 'search_page',
                              'shop_page', 'other_page'}
            skip_circle = page_type in non_video_pages
            
            # 对于配置了 skip_circle_on_video_feed 的APP，在正常视频页也跳过圆形检测
            # （抖音视频中大量圆形UI元素：头像、音乐唱片、点赞按钮等极易误判）
            if self.cfg.get('skip_circle_on_video_feed', False):
                if page_type in {'normal_video_feed', 'unknown'}:
                    skip_circle = True
            
            h, w = img.shape[:2]
            roi = self.cfg['center_roi']
            x1, y1, x2, y2 = int(w*roi[0]), int(h*roi[1]), int(w*roi[2]), int(h*roi[3])
            if x2 <= x1 or y2 <= y1:
                return False, ""
            
            center_img = img[y1:y2, x1:x2]
            gray = cv2.cvtColor(center_img, cv2.COLOR_BGR2GRAY)
            
            # 1. 转圈检测（同心圆弧检测）- 非视频页/配置了skip_circle的APP跳过
            if not skip_circle:
                has_circle = self._detect_spinning_circle(gray)
                if has_circle:
                    return True, "loading_circle"
            
            # 2. 加载文字检测（传入彩色图用于颜色过滤，排除白色/黄色字幕）
            has_text = self._detect_loading_text(gray, center_img)
            if has_text:
                return True, "loading_text"
            
            # 3. 纯色背景+中央图形兜底（用于检测如"加载失败"等极简页面）
            variance = np.var(gray)
            # 再次提高方差阈值：1200仍然可能命中暗色视频，提高到1800
            if variance < 1800:
                edges = cv2.Canny(gray, 50, 150)
                edge_ratio = np.sum(edges > 0) / edges.size
                # 提高边缘比例下限，减少低纹理视频的误判
                if 0.008 < edge_ratio < 0.12:
                    return True, "loading_screen"
            
            return False, ""
        except Exception as e:
            return False, ""
    
    def _detect_spinning_circle(self, gray: np.ndarray) -> bool:
        """检测同心圆环/弧形（加载圈特征）"""
        h, w = gray.shape
        min_r = int(min(h, w) * self.cfg.get('circle_min_radius_ratio', 0.03))
        max_r = int(min(h, w) * self.cfg.get('circle_max_radius_ratio', 0.15))
        
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        
        # 方法A：霍夫圆检测完整圆（提高param2阈值，减少误判）
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=min_r*2,
            param1=80, param2=28, minRadius=min_r, maxRadius=max_r
        )
        if circles is not None and len(circles[0]) >= 1:
            return True
        
        # 方法B：轮廓拟合圆检测（对弧形/圆环更有效）
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidate_circles = []
        for cnt in contours:
            if len(cnt) < 5:
                continue
            (cx, cy), radius = cv2.minEnclosingCircle(cnt)
            if not (min_r <= radius <= max_r):
                continue
            area = cv2.contourArea(cnt)
            circle_area = np.pi * radius * radius
            if circle_area == 0:
                continue
            fill_ratio = area / circle_area
            # 收紧填充率范围：圆环的填充率介于 0.12 ~ 0.75 之间（排除实心图标和噪声）
            if 0.12 < fill_ratio < 0.75:
                candidate_circles.append((int(cx), int(cy), int(radius)))
        
        if len(candidate_circles) >= self.cfg.get('min_concentric_circles', 1):
            return True
        
        # 方法C：径向边缘密度（检测放射状边缘聚集）
        center = (w // 2, h // 2)
        radii_edges = {}
        for y in range(h):
            for x in range(w):
                if edges[y, x] > 0:
                    r = int(np.hypot(x - center[0], y - center[1]))
                    radii_edges[r] = radii_edges.get(r, 0) + 1
        
        if radii_edges:
            sorted_counts = sorted(radii_edges.values(), reverse=True)
            # 提高阈值：需要更明显的半径聚集才判定为加载圈
            if sorted_counts[0] > min(h, w) * 0.22:
                return True
        
        return False
    
    def _detect_loading_text(self, gray: np.ndarray, color_img: Optional[np.ndarray] = None) -> bool:
        """检测加载提示文字（极度收紧，排除抖音字幕/歌词/标签）"""
        h, w = gray.shape
        
        # 使用MSER检测文字块
        mser = cv2.MSER_create()
        mser.setMinArea(120)
        mser.setMaxArea(int(h * w * 0.18))
        regions, _ = mser.detectRegions(gray)
        
        valid_blocks = []
        for region in regions:
            x, y, bw, bh = cv2.boundingRect(region)
            aspect = bw / bh if bh > 0 else 0
            # 提高尺寸下限，过滤小噪声
            if bh < 14 or bw < 24:
                continue
            if aspect > 10 or aspect < 0.25:
                continue
            area = cv2.contourArea(region)
            bbox_area = bw * bh
            if bbox_area == 0 or area / bbox_area < 0.18:
                continue
            
            cx, cy = x + bw/2, y + bh/2
            
            # 极度收紧位置：加载文字严格居中，字幕/歌词通常在下方或偏侧
            # 排除偏下的文字块（y中心在ROI下半区）
            if cy > h * 0.52:
                continue
            # 排除过于偏侧的文字块
            if abs(cx - w/2) > w*0.25:
                continue
            
            if abs(cx - w/2) < w*0.25 and abs(cy - h/2) < h*0.30:
                # 亮度过滤：排除高亮度文字块（白色/黄色字幕）
                if color_img is not None and color_img.size > 0:
                    block_color = color_img[y:y+bh, x:x+bw]
                    if block_color.size > 0:
                        block_gray = cv2.cvtColor(block_color, cv2.COLOR_BGR2GRAY)
                        mean_brightness = np.mean(block_gray)
                        # 再次提高阈值：白色字幕通常>220，加载文字通常偏暗
                        if mean_brightness > 230:
                            continue
                valid_blocks.append((x, y, bw, bh, area/bbox_area))
        
        if not valid_blocks:
            return False
        
        # 合并相近块（NMS）
        valid_blocks = self._merge_text_blocks(valid_blocks)
        
        # 计算文字块总面积
        total_text_area = sum(b[2]*b[3] for b in valid_blocks)
        roi_area = h * w
        text_ratio = total_text_area / roi_area
        
        # 极度收紧：加载提示通常只有1个文字块（如"加载中"），面积占比1%~8%
        if 0.005 <= text_ratio <= 0.08 and len(valid_blocks) == 1:
            return True
        
        return False
    
    @staticmethod
    def _merge_text_blocks(blocks: List[Tuple]) -> List[Tuple]:
        """合并相近的文字块"""
        if not blocks:
            return []
        blocks = sorted(blocks, key=lambda b: b[2]*b[3], reverse=True)
        merged = []
        while blocks:
            curr = list(blocks[0])
            blocks = blocks[1:]
            to_remove = []
            for i, b in enumerate(blocks):
                cx1, cy1 = curr[0] + curr[2]/2, curr[1] + curr[3]/2
                cx2, cy2 = b[0] + b[2]/2, b[1] + b[3]/2
                if np.hypot(cx1-cx2, cy1-cy2) < max(curr[2], curr[3], b[2], b[3]) * 0.8:
                    # 合并
                    x1 = min(curr[0], b[0])
                    y1 = min(curr[1], b[1])
                    x2 = max(curr[0]+curr[2], b[0]+b[2])
                    y2 = max(curr[1]+curr[3], b[1]+b[3])
                    curr = [x1, y1, x2-x1, y2-y1, max(curr[4], b[4])]
                    to_remove.append(i)
            for i in reversed(to_remove):
                blocks.pop(i)
            merged.append(tuple(curr))
        return merged


class ADBHelper:
    """ADB工具类"""
    
    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id
        self.base_cmd = self._build_base_cmd(device_id)
        self.connection_error = ''

    @staticmethod
    def _build_base_cmd(device_id: Optional[str]) -> list:
        binary = _adb_binary()
        return [binary, '-s', device_id] if device_id else [binary]

    def _select_device(self, device_id: str) -> None:
        self.device_id = device_id
        self.base_cmd = self._build_base_cmd(device_id)
    
    def _run(self, cmd: list, capture_output: bool = True, timeout: int = 30) -> Tuple[bool, str]:
        """运行ADB命令"""
        full_cmd = self.base_cmd + cmd
        try:
            result = subprocess.run(
                full_cmd, 
                capture_output=capture_output, 
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout
            )
            if result.returncode == 0:
                return True, result.stdout
            return False, result.stderr
        except subprocess.TimeoutExpired:
            return False, "Command timeout"
        except Exception as e:
            return False, str(e)
    
    def check_connection(self) -> bool:
        """Resolve one ready device and pin later commands to its serial."""
        self.connection_error = ''
        try:
            result = subprocess.run(
                [_adb_binary(), 'devices'],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=10,
            )
        except Exception as exc:
            self.connection_error = f"无法执行 adb devices: {exc}"
            return False
        if result.returncode != 0:
            self.connection_error = (
                result.stderr.strip() or result.stdout.strip() or 'adb devices 执行失败'
            )
            return False

        ready_devices = [
            parts[0]
            for line in result.stdout.splitlines()[1:]
            if len(parts := line.split()) >= 2 and parts[1] == 'device'
        ]
        if self.device_id:
            if self.device_id not in ready_devices:
                self.connection_error = f"设备未连接或未就绪: {self.device_id}"
                return False
            self._select_device(self.device_id)
            return True
        if len(ready_devices) == 1:
            self._select_device(ready_devices[0])
            print(f"[设备] 自动选择唯一设备: {self.device_id}")
            return True
        if not ready_devices:
            self.connection_error = '未检测到已就绪的 Android 设备'
        else:
            self.connection_error = (
                '检测到多个 Android 设备，请使用 --device 指定序列号: '
                + ', '.join(ready_devices)
            )
        return False
    
    def get_screen_resolution(self) -> Optional[Tuple[int, int]]:
        """获取屏幕分辨率"""
        success, output = self._run(['shell', 'wm', 'size'])
        if success:
            for line in output.split('\n'):
                if 'size' in line.lower():
                    try:
                        size_str = line.split(':')[-1].strip()
                        width, height = map(int, size_str.split('x'))
                        return (width, height)
                    except:
                        pass
        return None
    
    def capture_screenshot(self, save_path: str) -> bool:
        """截取屏幕并保存"""
        remote_path = '/sdcard/Download/qoe_screenshot.png'
        
        # 截图到设备
        success, _ = self._run(['shell', 'screencap', '-p', remote_path])
        if not success:
            return False
        
        # 拉取到本地
        success, _ = self._run(['pull', remote_path, save_path])
        if not success:
            return False
        
        # 删除设备上的临时文件
        self._run(['shell', 'rm', remote_path], capture_output=False)
        
        return True


class ImageAnalyzer:
    """图像分析工具类 - 用于识别分辨率和卡顿（APP分支优化版）"""
    
    # 分辨率数字模板（用于匹配）
    RESOLUTION_TEMPLATES = {
        360: ['360', '360P', '流畅'],
        480: ['480', '480P', '清晰', '标清'],
        720: ['720', '720P', '高清', 'HD'],
        1080: ['1080', '1080P', '超清', 'FHD', '蓝光'],
        1440: ['1440', '2K'],
        2160: ['2160', '4K'],
    }
    
    def __init__(self, app_name: Optional[str] = None):
        self.app_name = app_name
        self.profile = get_app_profile(app_name)
        self.digit_recognizer = DigitRecognizer()
    
    def detect_resolution_from_screen(self, image_path: str, screen_resolution: Tuple[int, int] = None) -> int:
        """
        从屏幕截图识别视频分辨率（APP分支优化版）
        """
        try:
            img = _read_image(image_path)
            if img is None:
                return -128
            
            height, width = img.shape[:2]
            
            # 检测是否是视频播放界面（中心区域不能是黑屏）
            center_region = img[int(height*0.3):int(height*0.7), int(width*0.2):int(width*0.8)]
            center_gray = cv2.cvtColor(center_region, cv2.COLOR_BGR2GRAY)
            center_variance = np.var(center_gray)
            if center_variance < 80:
                return -128  # 未开始或黑屏
            
            detected_values = []
            keywords = self.profile.get('resolution_keywords', {})
            
            # 策略1: 优先尝试 OCR (如果已安装 tesseract)
            if PYTESSERACT_AVAILABLE:
                for roi_cfg in self.profile.get('resolution_regions', APP_DETECTION_PROFILES['default']['resolution_regions']):
                    roi = roi_cfg['bbox']
                    x1, y1, x2, y2 = int(roi[0]*width), int(roi[1]*height), int(roi[2]*width), int(roi[3]*height)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    roi_img = img[y1:y2, x1:x2]
                    resolution = self._detect_by_ocr(roi_img, keywords)
                    if resolution > 0:
                        detected_values.append(resolution)
            
            # 策略2: 使用 MSER + DigitRecognizer (纯CV，不依赖OCR)
            if not detected_values:
                for roi_cfg in self.profile.get('resolution_regions', APP_DETECTION_PROFILES['default']['resolution_regions']):
                    roi = roi_cfg['bbox']
                    x1, y1, x2, y2 = int(roi[0]*width), int(roi[1]*height), int(roi[2]*width), int(roi[3]*height)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    roi_img = img[y1:y2, x1:x2]
                    resolution = self._detect_by_cv(roi_img, keywords)
                    if resolution > 0:
                        detected_values.append(resolution)
            
            # 策略3: 兜底，使用全局默认ROI
            if not detected_values:
                for roi_cfg in APP_DETECTION_PROFILES['default']['resolution_regions']:
                    roi = roi_cfg['bbox']
                    x1, y1, x2, y2 = int(roi[0]*width), int(roi[1]*height), int(roi[2]*width), int(roi[3]*height)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    roi_img = img[y1:y2, x1:x2]
                    resolution = self._detect_by_cv(roi_img, keywords)
                    if resolution > 0:
                        detected_values.append(resolution)
            
            if detected_values:
                most_common = Counter(detected_values).most_common(1)[0]
                return most_common[0]
            
            return -128
        except Exception as e:
            return -128
    
    def _detect_by_ocr(self, roi_img: np.ndarray, keywords: Dict[str, int]) -> int:
        """使用 pytesseract OCR 识别分辨率"""
        try:
            gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
            # 放大ROI以提高OCR准确率
            scale = 2
            gray = cv2.resize(gray, (gray.shape[1]*scale, gray.shape[0]*scale), interpolation=cv2.INTER_CUBIC)
            # 二值化
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            # OCR，只识别白名单字符
            whitelist = '0123456789P高清超清流畅标清清晰蓝光4K'
            text = pytesseract.image_to_string(
                binary,
                config=f'--psm 7 -c tessedit_char_whitelist={whitelist}'
            ).strip().replace(' ', '').replace('\n', '')
            
            if not text:
                return -1
            
            # 优先精确匹配关键词
            for kw, val in sorted(keywords.items(), key=lambda x: -len(x[0])):
                if kw in text:
                    return val
            
            # 提取数字
            nums = re.findall(r'\d+', text)
            for n in nums:
                if n in keywords:
                    return keywords[n]
            
            # 模糊匹配常见分辨率
            if '1080' in text or '蓝光' in text:
                return 1080
            if '720' in text or '高清' in text or 'HD' in text:
                return 720
            if '480' in text or '清晰' in text or '标清' in text:
                return 480
            if '360' in text or '流畅' in text:
                return 360
            if '2160' in text or '4K' in text:
                return 2160
            
            return -1
        except Exception:
            return -1
    
    def _detect_by_cv(self, roi_img: np.ndarray, keywords: Dict[str, int]) -> int:
        """使用纯CV方法识别分辨率"""
        gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY) if len(roi_img.shape) == 3 else roi_img
        
        # 步骤1: 尝试识别数字序列（如 720P, 1080P）
        seq, confs = self.digit_recognizer.recognize_sequence(roi_img)
        if seq:
            # 从序列中提取纯数字
            nums = re.findall(r'\d+', seq)
            for n in nums:
                if n in keywords:
                    return keywords[n]
            if '720' in seq:
                return 720
            if '1080' in seq:
                return 1080
            if '360' in seq:
                return 360
            if '480' in seq:
                return 480
        
        # 步骤2: 检测中文关键词特征（通过笔画密度和块数判断）
        resolution = self._detect_chinese_quality(gray, keywords)
        if resolution > 0:
            return resolution
        
        # 步骤3: 多尺度模板匹配兜底
        resolution = self._match_resolution_templates(roi_img)
        if resolution > 0:
            return resolution
        
        return -1
    
    def _detect_chinese_quality(self, gray: np.ndarray, keywords: Dict[str, int]) -> int:
        """检测中文清晰度标识（高清/超清/流畅等）"""
        # 使用MSER检测文字块
        mser = cv2.MSER_create()
        mser.setMinArea(80)
        mser.setMaxArea(int(gray.size * 0.4))
        regions, _ = mser.detectRegions(gray)
        
        blocks = []
        for region in regions:
            x, y, w, h = cv2.boundingRect(region)
            aspect = w / h if h > 0 else 0
            # 中文双字通常宽高比 1.0~2.5，高度适中
            if 12 < h < 60 and 15 < w < 120 and 0.8 < aspect < 3.5:
                area = cv2.contourArea(region)
                bbox_area = w * h
                if bbox_area > 0 and 0.2 < area / bbox_area < 0.8:
                    blocks.append((x, y, w, h))
        
        if not blocks:
            return -1
        
        # 合并并取最大的几个块
        blocks = DigitRecognizer._nms_boxes(blocks, threshold=0.4)
        blocks.sort(key=lambda b: b[2]*b[3], reverse=True)
        
        # 分析每个候选块的笔画密度特征
        for bx, by, bw, bh in blocks[:2]:
            roi_char = gray[by:by+bh, bx:bx+bw]
            if roi_char.size == 0:
                continue
            _, binary = cv2.threshold(roi_char, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            stroke_ratio = np.sum(binary > 0) / binary.size
            
            # "高清" 通常笔画密度 0.15~0.35，且块内有2个主要连通区（两个字）
            if 0.10 < stroke_ratio < 0.40:
                # 计算连通区数量
                num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
                # 过滤太小的噪声
                main_components = [i for i in range(1, num_labels) if stats[i, cv2.CC_STAT_AREA] > binary.size * 0.02]
                comp_count = len(main_components)
                
                # 双字: 2-3个主要连通区（含偏旁）
                # "超清"、"高清"、"流畅" 都是双字
                if 2 <= comp_count <= 4:
                    # 根据宽高比和密度做简单分类
                    aspect = bw / bh
                    if 1.8 <= aspect <= 2.5:
                        # 可能是双字中文
                        # 结合APP关键词做概率推断（无法精确识别具体字，但知道大概率是其中之一）
                        # 如果该APP常用720P(高清)，则返回720作为保守估计
                        if '高清' in keywords and '超清' in keywords:
                            # 如果该APP在720和1080之间切换，保守返回720（最常见）
                            return keywords.get('高清', 720)
                        elif '流畅' in keywords:
                            return keywords.get('流畅', 360)
        
        return -1
    
    def _match_resolution_templates(self, roi_img: np.ndarray) -> int:
        """多尺度模板匹配兜底"""
        gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY) if len(roi_img.shape) == 3 else roi_img
        
        # 生成一组合成模板：720P, 1080P 等
        templates = {}
        for text in ['720P', '1080P', '360P', '480P']:
            tpl = np.zeros((30, 80), dtype=np.uint8)
            cv2.putText(tpl, text, (2, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 255, 2, cv2.LINE_AA)
            templates[text] = tpl
        
        best_val = -1
        best_res = -1
        for text, tpl in templates.items():
            # 多尺度匹配
            for scale in [0.8, 1.0, 1.2, 1.5]:
                resized_tpl = cv2.resize(tpl, (int(tpl.shape[1]*scale), int(tpl.shape[0]*scale)))
                if resized_tpl.shape[0] > gray.shape[0] or resized_tpl.shape[1] > gray.shape[1]:
                    continue
                result = cv2.matchTemplate(gray, resized_tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if max_val > best_val:
                    best_val = max_val
                    best_res = int(re.search(r'\d+', text).group())
        
        if best_val > 0.55:
            return best_res
        return -1
    
    @staticmethod
    def calculate_similarity(img1_path: str, img2_path: str) -> Tuple[float, float]:
        """
        计算两张图片的相似度
        Returns:
            (combined_sim, pixel_sim)
            - combined_sim: 综合相似度 (0-100%)，加权融合像素差异和直方图
            - pixel_sim: 像素差异比例 (0-100%)，可直接用于卡顿判定
        """
        try:
            img1 = _read_image(img1_path)
            img2 = _read_image(img2_path)
            if img1 is None or img2 is None:
                return 0.0, 0.0
            if img1.shape != img2.shape:
                img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
            
            # 方法1：像素差异比例（最可靠的帧间差异指标）
            diff = cv2.absdiff(img1, img2)
            gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            similar_pixels = np.sum(gray_diff < 15)
            pixel_sim = (similar_pixels / gray_diff.size) * 100
            
            # 方法2：改进的HSV直方图相似度（H+S双通道，替代原来仅H通道）
            hist_sim = ImageAnalyzer._histogram_similarity(img1, img2)
            
            # 综合：以像素差异为主（0.6），直方图为辅（0.4）
            # 像素差异能直接反映内容变化；直方图补充色调/整体结构信息
            combined = 0.6 * pixel_sim + 0.4 * hist_sim
            return combined, pixel_sim
        except Exception:
            return 0.0, 0.0
    
    @staticmethod
    def _histogram_similarity(img1: np.ndarray, img2: np.ndarray) -> float:
        """使用HSV H+S双通道直方图计算相似度（比单H通道对内容变化更敏感）"""
        try:
            hsv1 = cv2.cvtColor(img1, cv2.COLOR_BGR2HSV)
            hsv2 = cv2.cvtColor(img2, cv2.COLOR_BGR2HSV)
            # 使用H(0-180)和S(0-256)两个维度构建2D直方图，比单H通道更能区分内容变化
            hist1 = cv2.calcHist([hsv1], [0, 1], None, [90, 64], [0, 180, 0, 256])
            hist2 = cv2.calcHist([hsv2], [0, 1], None, [90, 64], [0, 180, 0, 256])
            cv2.normalize(hist1, hist1, 0, 1, cv2.NORM_MINMAX)
            cv2.normalize(hist2, hist2, 0, 1, cv2.NORM_MINMAX)
            similarity = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
            return max(0, similarity * 100)
        except:
            return 0.0
    
    @staticmethod
    def pixel_diff_ratio(img1_path: str, img2_path: str, threshold: int = 10) -> float:
        """计算像素差异比例"""
        try:
            img1 = _read_image(img1_path)
            img2 = _read_image(img2_path)
            if img1 is None or img2 is None:
                return 0.0
            if img1.shape != img2.shape:
                img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
            diff = cv2.absdiff(img1, img2)
            gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            similar_pixels = np.sum(gray_diff < threshold)
            total_pixels = gray_diff.size
            return (similar_pixels / total_pixels) * 100
        except Exception:
            return 0.0


class QoEMonitor:
    """QoE监控主类 - 输出格式与样本保持一致"""
    
    def __init__(
        self, 
        device_id: Optional[str] = None,
        interval: float = 1.0,
        output_dir: str = './qoe_data',
        frozen_threshold: float = 95.0,
        pcap_name: Optional[str] = None,
        app_type: str = 'video',
        app_name: Optional[str] = None,
        scene: Optional[str] = None,
        warmup_seconds: int = 5,
        stall_window: int = 3,
        resolution: Optional[int] = None,
        register_signal: bool = True,
        csv_path: Optional[str] = None
    ):
        self.device_id = device_id
        self.interval = interval
        self.frozen_threshold = frozen_threshold
        self.app_type = app_type
        self.app_name = app_name
        self.scene = scene
        self.warmup_seconds = warmup_seconds
        self.stall_window = stall_window
        self.manual_resolution = resolution
        self.register_signal = register_signal
        self.running = False
        self._latency_ocr = None
        self._latency_roi = (0.775, 0.005, 0.855, 0.045)
        if app_name == 'honorofkings' and RAPIDOCR_AVAILABLE:
            self._latency_ocr = RapidOCR()
        
        # pcap文件名
        if pcap_name:
            self.pcap_name = pcap_name if pcap_name.endswith('.pcap') else f"{pcap_name}.pcap"
        else:
            timestamp = datetime.now().strftime('%Y%m%dT%H%M%S')
            self.pcap_name = f"capture_{timestamp}.pcap"
        
        # 初始化ADB
        self.adb = ADBHelper(device_id)
        
        # CSV文件路径（支持外部指定）
        if csv_path:
            self.csv_path = Path(csv_path)
            self.output_dir = self.csv_path.parent
            self.screenshot_dir = self.output_dir / 'screenshots'
        else:
            # 设置输出目录
            self.output_dir = Path(output_dir)
            self.screenshot_dir = self.output_dir / 'screenshots'
            # CSV文件
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            self.csv_path = self.output_dir / f'qoe_{timestamp}.csv'
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        
        # 图像分析器（APP分支）
        self.image_analyzer = ImageAnalyzer(app_name=app_name)
        self.loading_detector = LoadingDetector(get_app_profile(app_name))
        self.page_classifier = PageClassifier(app_name=app_name)
        
        # 截图历史（用于滑动窗口检测）
        self.screenshot_history: List[str] = []
        self.similarity_history: List[float] = []
        self.pixel_diff_history: List[float] = []  # 像素差异比例历史
        
        # 统计
        self.total_samples = 0
        self.frozen_count = 0
        self.loading_stall_count = 0
        self.start_time: Optional[datetime] = None
        self.business_started = False
        self.business_start_time: Optional[datetime] = None
        
        # 分辨率稳定检测
        self.resolution_history: List[int] = []
        self.resolution_stable_count: int = 3
    
    def init_csv(self):
        """初始化CSV文件 - 与最终七列样本格式一致"""
        with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'file_name',
                'time',
                'rtt',
                'trust_resolution',
                'trust_stall',
                'loading_reason'
            ])
    
    def log_to_csv(self, data: dict):
        """记录数据到CSV - 与样本格式一致"""
        with open(self.csv_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                data['file_name'],
                data['time'],
                data['rtt'],
                data['trust_resolution'],
                data['trust_stall'],
                data.get('loading_reason', '')
            ])
    
    def print_header(self):
        """打印表头"""
        print("\n" + "="*115)
        print(f"{'时间':^20} | {'文件名':^26} | {'rtt':^5} | {'分辨率':^8} | {'卡顿':^6} | {'相似度':^10} | {'像素差':^8} | {'原因':^8}")
        print("-"*115)
    
    def print_status(self, data: dict):
        """打印实时状态"""
        stall_str = {
            -4: "未开始",
            0: "流畅",
            1: "卡顿!"
        }.get(data['trust_stall'], str(data['trust_stall']))
        
        resolution_str = str(data['trust_resolution'])
        if data['trust_resolution'] == -128:
            resolution_str = "未开始"
        elif data['trust_resolution'] == -4:
            resolution_str = "不适用"
        
        # 显示当前帧即时相似度，而非仅历史平均
        sim_str = f"{data.get('similarity', 0):.1f}%"
        pixel_str = f"{data.get('pixel_diff', 0):.1f}%"
        
        loading_info = data.get('loading_reason', '')
        page_info = data.get('page_type', 'unknown')
        if page_info != 'unknown' and page_info != 'normal_video_feed':
            page_label = f"[{page_info}]"
        else:
            page_label = ""
        
        # 页面分类器调试信息（仅控制台显示，帮助调阈值）
        details = data.get('page_details', {})
        if details:
            rb = int(details.get('has_right_buttons', False))
            bt = int(details.get('has_bottom_tab', False))
            cv = int(details.get('center_is_video_like', False))
            page_label += f"(rb={rb},bt={bt},cv={cv})"
        
        print(
            f"{data['time']:^20} | "
            f"{data['file_name'][:26]:^26} | "
            f"{data['rtt']:^5} | "
            f"{resolution_str:^8} | "
            f"{stall_str:^6} | "
            f"{sim_str:^10} | "
            f"{pixel_str:^8} | "
            f"{loading_info:^8} {page_label}"
        )
    
    def detect_stall(self, current_similarity: float, pixel_diff: float, screenshot_path: str, page_type: str = 'unknown') -> Tuple[int, str]:
        """
        检测是否卡顿 - 综合相似度 + 像素差异 + CV加载检测
        Args:
            current_similarity: 当前帧与上一帧的综合相似度
            pixel_diff: 当前帧与上一帧的像素差异比例
            screenshot_path: 截图路径
            page_type: 页面类型
        Returns: (stall_code, reason)
        """
        if not self.business_started:
            return -4, ""
        
        # 非视频页面（团购/直播入口/搜索/商城等）不判定卡顿，避免静态页面被误判
        non_video_pages = {'group_buy_page', 'live_entry_page', 'search_page',
                          'shop_page', 'other_page'}
        if page_type in non_video_pages:
            return 0, "not_video_page"
        
        # 第一步：CV检测加载圈/加载文字（广告页也保留，因为广告也是视频）
        is_loading, loading_reason = self.loading_detector.detect(screenshot_path, page_type=page_type)
        if is_loading:
            # 智能过滤：真正的加载画面应该是静止的
            # 如果检测到"加载"，但当前帧与上一帧差异很大（画面在变化），则认为是误判
            # 例外：加载圈的第一帧可能与上一帧差异大，所以只过滤差异极大的情况
            if current_similarity < 65.0 and pixel_diff < 60.0:
                # 画面在明显变化，不可能是真正的加载/冻结
                return 0, ""
            self.loading_stall_count += 1
            return 1, loading_reason
        
        # 第二步：滑动窗口联合检测（相似度 + 像素差异）
        self.similarity_history.append(current_similarity)
        self.pixel_diff_history.append(pixel_diff)
        if len(self.similarity_history) > self.stall_window:
            self.similarity_history.pop(0)
            self.pixel_diff_history.pop(0)
        
        if len(self.similarity_history) < self.stall_window:
            # 预热期内：需要同时满足极高相似度和极高像素相似度才判定卡顿
            if current_similarity > 99.0 and pixel_diff > 98.0:
                return 1, "frozen_frame"
            return 0, ""
        
        # 联合判定：需要同时满足高直方图相似度 和 高像素相似度
        # 这样可避免单一直方图方法在色调稳定时的误判
        high_similarity_count = sum(1 for s in self.similarity_history if s >= self.frozen_threshold)
        high_pixel_sim_count = sum(1 for p in self.pixel_diff_history if p >= self.frozen_threshold * 0.96)
        if high_similarity_count >= self.stall_window and high_pixel_sim_count >= self.stall_window:
            return 1, "frozen_frame"
        
        return 0, ""
    
    def detect_resolution(self, screenshot_path: str, screen_resolution: Tuple[int, int] = None, page_type: str = 'unknown') -> int:
        """检测视频分辨率"""
        if not self.business_started:
            return -128
        
        if self.app_type != 'video':
            return -4
        
        # 非标准视频页（广告/团购/直播入口/搜索/商城等）不检测分辨率
        if page_type not in {'normal_video_feed', 'fullscreen_video_feed', 'unknown'}:
            return -4
        
        if self.manual_resolution is not None:
            return self.manual_resolution
        
        detected_resolution = self.image_analyzer.detect_resolution_from_screen(screenshot_path, screen_resolution)
        
        self.resolution_history.append(detected_resolution)
        if len(self.resolution_history) > self.resolution_stable_count:
            self.resolution_history.pop(0)
        
        if len(self.resolution_history) < self.resolution_stable_count:
            return detected_resolution if detected_resolution > 0 else -128
        
        valid_resolutions = [r for r in self.resolution_history if r > 0]
        if not valid_resolutions:
            return -128
        
        resolution_counts = Counter(valid_resolutions)
        most_common = resolution_counts.most_common(1)[0]
        if most_common[1] >= (self.resolution_stable_count + 1) // 2:
            return most_common[0]
        
        return valid_resolutions[-1] if valid_resolutions else -128
    
    def _detect_latency_ms(self, screenshot_path: str) -> int:
        if self._latency_ocr is None:
            return -4
        image = _read_image(screenshot_path)
        if image is None:
            return -4
        height, width = image.shape[:2]
        x1, y1, x2, y2 = self._latency_roi
        crop = image[int(y1 * height):int(y2 * height), int(x1 * width):int(x2 * width)]
        if crop.size == 0:
            return -4
        try:
            results, _ = self._latency_ocr(crop, use_det=False, use_cls=False)
        except Exception:
            return -4
        for item in results or []:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            if isinstance(item[0], str):
                text = str(item[0]).strip()
            elif len(item) >= 2:
                text = str(item[1]).strip()
            else:
                continue
            match = LATENCY_MS_RE.search(text.replace('毫秒', 'ms'))
            if not match:
                continue
            value = int(match.group(1))
            if 1 <= value <= MAX_PLAUSIBLE_LATENCY_MS:
                return value
        return -4

    def sample_once(self) -> dict:
        """执行一次采样"""
        timestamp = datetime.now()
        time_str = timestamp.strftime('%Y-%m-%d %H:%M:%S+0800')
        
        screenshot_filename = f"screen_{timestamp.strftime('%Y%m%d_%H%M%S')}.png"
        screenshot_path = self.screenshot_dir / screenshot_filename
        screenshot_path_str = str(screenshot_path)
        
        screenshot_success = self.adb.capture_screenshot(screenshot_path_str)
        
        current_similarity = 0.0
        pixel_diff = 0.0
        if screenshot_success and self.screenshot_history:
            last_screenshot = self.screenshot_history[-1]
            if os.path.exists(last_screenshot):
                current_similarity, pixel_diff = ImageAnalyzer.calculate_similarity(
                    last_screenshot, 
                    screenshot_path_str
                )
        
        if screenshot_success:
            self.screenshot_history.append(screenshot_path_str)
            while len(self.screenshot_history) > self.stall_window + 1:
                self.screenshot_history.pop(0)
        
        if not self.business_started and self.start_time:
            elapsed = (timestamp - self.start_time).total_seconds()
            if elapsed >= self.warmup_seconds:
                self.business_started = True
                self.business_start_time = timestamp
                self.similarity_history.clear()
                self.pixel_diff_history.clear()
                self.resolution_history.clear()
                print(f"\n[信息] 业务开始时间: {time_str}")
                print(f"[信息] 卡顿检测参数: 阈值={self.frozen_threshold}%, 窗口={self.stall_window}帧")
                print(f"[信息] APP检测配置: {self.app_name or 'default'}")
                if PYTESSERACT_AVAILABLE:
                    print(f"[信息] OCR引擎: pytesseract 已启用")
                else:
                    print(f"[信息] OCR引擎: 未安装，使用纯CV检测")
                self.print_header()
        
        # 页面分类（仅针对抖音）
        page_type = 'unknown'
        page_details = {}
        if screenshot_success and self.app_name == 'douyin':
            try:
                page_type, page_details = self.page_classifier.classify(screenshot_path_str)
            except Exception:
                page_type = 'unknown'
                page_details = {}
        
        screen_res = self.adb.get_screen_resolution()
        trust_resolution = self.detect_resolution(screenshot_path_str, screen_res, page_type=page_type) if screenshot_success else -128
        
        trust_stall, stall_reason = self.detect_stall(current_similarity, pixel_diff, screenshot_path_str, page_type=page_type)
        
        rtt = self._detect_latency_ms(screenshot_path_str) if screenshot_success else -4
        
        data = {
            'file_name': self.pcap_name,
            'time': time_str,
            'rtt': rtt,
            'trust_resolution': trust_resolution,
            'trust_stall': trust_stall,
            'similarity': current_similarity,
            'pixel_diff': pixel_diff,
            'screenshot_path': screenshot_path_str if screenshot_success else 'N/A',
            'loading_reason': stall_reason if trust_stall == 1 else '',
            'page_type': page_type,
            'page_details': page_details,
        }
        
        self.total_samples += 1
        if trust_stall == 1:
            self.frozen_count += 1
        
        return data
    
    def _cleanup_old_screenshots(self, keep_count: int = 30):
        """清理旧截图"""
        try:
            screenshots = sorted(
                self.screenshot_dir.glob('screen_*.png'),
                key=lambda p: p.stat().st_mtime
            )
            for old_screenshot in screenshots[:-keep_count]:
                old_screenshot.unlink()
        except Exception:
            pass
    
    def print_summary(self):
        """打印统计摘要"""
        if self.start_time is None:
            return
        
        duration = datetime.now() - self.start_time
        print("\n" + "="*115)
        print("QoE监控统计摘要")
        print("-"*115)
        print(f"运行时长: {duration}")
        print(f"总采样次数: {self.total_samples}")
        business_samples = max(1, self.total_samples - int(self.warmup_seconds / self.interval))
        print(f"业务开始后采样: {business_samples}")
        print(f"卡顿次数: {self.frozen_count} (CV加载检测: {self.loading_stall_count})")
        print(f"卡顿率: {(self.frozen_count/business_samples*100):.2f}%")
        print(f"检测参数: 阈值={self.frozen_threshold}%, 窗口={self.stall_window}帧")
        print(f"APP配置: {self.app_name or 'default'}")
        
        # 页面分类统计（从CSV读取，仅抖音）
        if self.app_name == 'douyin' and self.csv_path.exists():
            try:
                page_type_counts = Counter()
                with open(self.csv_path, 'r', encoding='utf-8', newline='') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        pt = row.get('page_type', 'unknown')
                        if pt:
                            page_type_counts[pt] += 1
                if page_type_counts:
                    print("页面分类统计:")
                    for pt, cnt in page_type_counts.most_common():
                        pct = cnt / sum(page_type_counts.values()) * 100
                        print(f"  - {pt}: {cnt}次 ({pct:.1f}%)")
            except Exception:
                pass
        
        print(f"数据保存路径: {self.csv_path}")
        print(f"截图保存路径: {self.screenshot_dir}")
        print("="*115)
    
    def signal_handler(self, signum, frame):
        """信号处理"""
        print("\n\n[信息] 收到终止信号，正在停止监控...")
        self.running = False
    
    def run(self):
        """运行监控"""
        print("[信息] 检查ADB设备连接...")
        if not self.adb.check_connection():
            print(f"[错误] {self.adb.connection_error or '未检测到Android设备'}")
            print("  1. USB调试是否已开启")
            print("  2. 设备是否已连接")
            print("  3. 是否已授权调试")
            print("\n已连接设备列表:")
            subprocess.run([_adb_binary(), 'devices'])
            return 1

        self.device_id = self.adb.device_id
        
        print(f"[信息] 设备已连接: {self.device_id}")
        
        resolution = self.adb.get_screen_resolution()
        if resolution:
            print(f"[信息] 屏幕分辨率: {resolution[0]}x{resolution[1]}")
        
        self.init_csv()
        
        if self.register_signal:
            signal.signal(signal.SIGINT, self.signal_handler)
            signal.signal(signal.SIGTERM, self.signal_handler)
        
        self.start_time = datetime.now()
        self.running = True
        
        print(f"[信息] 开始QoE监控，采样间隔: {self.interval}秒")
        print(f"[信息] pcap文件名: {self.pcap_name}")
        print(f"[信息] 卡顿判定: 相似度>{self.frozen_threshold}% 且连续{self.stall_window}帧，或CV检测到加载")
        if self.manual_resolution:
            print(f"[信息] 分辨率: 手动指定为 {self.manual_resolution}P")
        else:
            print(f"[信息] 分辨率: 自动检测（APP配置: {self.app_name or 'default'}）")
        print(f"[信息] 预热时间: {self.warmup_seconds}秒（此期间标记为业务未开始）")
        print(f"[信息] 按 Ctrl+C 停止监控")
        print(f"\n[信息] 预热中... ({self.warmup_seconds}秒后业务开始，期间不判定卡顿)\n")
        
        next_sample_time = time.time()
        
        while self.running:
            try:
                current_time = time.time()
                if current_time < next_sample_time:
                    sleep_time = next_sample_time - current_time
                    time.sleep(sleep_time)
                
                sample_start = time.time()
                data = self.sample_once()
                
                if self.business_started:
                    self.print_status(data)
                    self.log_to_csv(data)
                
                self._cleanup_old_screenshots()
                next_sample_time = sample_start + self.interval
                    
            except Exception as e:
                print(f"\n[错误] 采样失败: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(self.interval)
                next_sample_time = time.time() + self.interval
        
        self.print_summary()
        return 0


def main():
    parser = argparse.ArgumentParser(
        description='Android QoE 自动标注脚本 - 输出格式与样本保持一致',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python qoe_monitor.py                              # 基本用法
  python qoe_monitor.py --device abc123              # 指定设备
  python qoe_monitor.py --interval 1.0               # 每1秒采样一次
  python qoe_monitor.py --pcap-name mango_xxx.pcap   # 指定pcap文件名
  python qoe_monitor.py --app-type video             # 视频类APP（检测分辨率）
  python qoe_monitor.py --app-name kuaishou          # 指定APP以使用专属检测配置
  python qoe_monitor.py --app-type game              # 游戏类APP
  python qoe_monitor.py --warmup 10                  # 10秒预热时间
  python qoe_monitor.py --stall-window 2             # 更敏感的卡顿检测（直播场景）
  python qoe_monitor.py --threshold 90 --stall-window 4  # 宽松阈值+大窗口（电影场景）
  python qoe_monitor.py --resolution 720                  # 指定720P分辨率
  python qoe_monitor.py --resolution 1080 --app-type video  # 指定1080P分辨率
        """
    )
    
    parser.add_argument('--device', '-d', help='指定设备ID (adb devices查看)')
    parser.add_argument('--interval', '-i', type=float, default=1.0, help='采样间隔秒数 (默认: 1.0)')
    parser.add_argument('--output', '-o', default='./qoe_data', help='输出目录路径 (默认: ./qoe_data)')
    parser.add_argument('--threshold', '-t', type=float, default=99.5, help='卡顿判定阈值 (默认: 99.5%%)')
    parser.add_argument('--pcap-name', '-p', help='pcap文件名（用于QoE CSV中的file_name列）')
    parser.add_argument('--app-type', default='video', choices=['video', 'game', 'social', 'shopping', 'cloudgame'], help='APP类型 (默认: video)')
    parser.add_argument('--app-name', '-a', default=None, help='指定APP名称以使用专属检测配置（如 kuaishou, douyin, mango）')
    parser.add_argument('--scene', '-s', default=None, help='指定场景（用于日志区分）')
    parser.add_argument('--warmup', type=int, default=5, help='预热时间（秒）(默认: 5)')
    parser.add_argument('--stall-window', type=int, default=3, help='卡顿检测窗口大小 (默认: 3)')
    parser.add_argument('--resolution', '-r', type=int, choices=[360, 480, 720, 1080, 1440, 2160], help='手动指定视频分辨率')
    
    args = parser.parse_args()
    
    try:
        subprocess.run([_adb_binary(), 'version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[错误] 未找到adb命令，请确保Android SDK已安装并添加到PATH")
        return 1
    
    threshold = args.threshold
    stall_window = args.stall_window
    
    if threshold == 99.5:
        if args.app_type == 'video':
            print(f"[信息] 视频类APP自动调整: 阈值=92%, 窗口={stall_window}帧")
            threshold = 92.0
        elif args.app_type == 'game':
            print(f"[信息] 游戏类APP自动调整: 阈值=95%, 窗口={stall_window}帧")
            threshold = 95.0
    
    monitor = QoEMonitor(
        device_id=args.device,
        interval=args.interval,
        output_dir=args.output,
        frozen_threshold=threshold,
        pcap_name=args.pcap_name,
        app_type=args.app_type,
        app_name=args.app_name,
        scene=args.scene,
        warmup_seconds=args.warmup,
        stall_window=stall_window,
        resolution=args.resolution
    )
    
    return monitor.run()


if __name__ == '__main__':
    sys.exit(main())
