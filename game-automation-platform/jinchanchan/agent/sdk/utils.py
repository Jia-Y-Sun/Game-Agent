# -*- coding: utf-8 -*-
"""名字匹配工具(移植自 Sunflower utils.py, 修复了其缺失 os import 的问题)"""

import os
import json

import cv2
import numpy

from agent.sdk.base import BASE_RESOLUTION

STATIC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statics")
EQUIPMENTS_PATH = os.path.join(STATIC_PATH, "equipments.json")
HEROES_PATH = os.path.join(STATIC_PATH, "heroes.json")
AUGMENTS_PATH = os.path.join(STATIC_PATH, "hex.json")
EVOLUTIONS_PATH = os.path.join(STATIC_PATH, "goop.json")
IMAGE_PATH = STATIC_PATH


class SdkUtils:
    @staticmethod
    def text_match(text, keyword):
        """文字匹配: 先全等; 长度>=3 的关键词允许模糊匹配(容错 OCR 误识,
        如"下一步"被识别成"下-步")"""
        if text == keyword:
            return True
        if len(keyword) >= 3:
            from difflib import SequenceMatcher
            return SequenceMatcher(None, text, keyword).ratio() >= 0.6
        return False

    @staticmethod
    def get_equipments(file_path=EQUIPMENTS_PATH):
        data = json.load(open(file_path, encoding="utf-8"))["data"]
        return {data[k]["name"] for k in data.keys()}

    @staticmethod
    def is_equipment(ocr_text_set):
        """OCR 文本集合中是否含有装备名"""
        shared = SdkUtils.get_equipments() & ocr_text_set
        return None if not shared else list(shared)[0]

    @staticmethod
    def text_similarity(text1, text2):
        t1 = set(text1)
        t2 = set(text2)
        return round(len(t1 & t2) / len(t1 | t2), 2)

    @staticmethod
    def get_heroes(file_path=HEROES_PATH):
        data = json.load(open(file_path, encoding="utf-8"))["data"]
        return {data[k]["name"] for k in data.keys()}

    @staticmethod
    def is_hero(ocr_results):
        """OCR 结果 -> 最匹配的英雄名(精确匹配优先, 否则取最相似)"""
        if ocr_results is None:
            return None

        ocr_name = "".join([r.text for r in ocr_results])
        if ocr_name in SdkUtils.get_heroes():
            return ocr_name

        heroes = SdkUtils.get_heroes()
        return max([(SdkUtils.text_similarity(ocr_name, hero), hero) for hero in heroes])[1]

    @staticmethod
    def get_augments(file_path=AUGMENTS_PATH):
        data = json.load(open(file_path, encoding="utf-8"))["data"]
        return {data[k]["name"] for k in data.keys()}

    @staticmethod
    def is_augments(ocr_results):
        if not ocr_results:
            return None

        augment_name = "".join([r.text for r in ocr_results])
        if augment_name in SdkUtils.get_augments():
            return augment_name

        augments = SdkUtils.get_augments()
        return max([(SdkUtils.text_similarity(augment_name, a), a) for a in augments])[1]

    @staticmethod
    def get_evolutions(file_path=EVOLUTIONS_PATH):
        data = json.load(open(file_path, encoding="utf-8"))["data"]
        return {data[k]["title"] for k in data.keys()}

    @staticmethod
    def is_evolution(ocr_results):
        if not ocr_results:
            return None

        evolution_name = "".join([r.text for r in ocr_results])
        if evolution_name in SdkUtils.get_evolutions():
            return evolution_name

        evolutions = SdkUtils.get_evolutions()
        return max([(SdkUtils.text_similarity(evolution_name, e), e) for e in evolutions])[1]

    @staticmethod
    def count_template_matches(template, image, threshold=0.8):
        """统计模板在图中的匹配数(用于数棋子星级)"""
        template_height, template_width = template.shape[:2]
        result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        locations = numpy.where(result >= threshold)
        locations = list(zip(*locations[::-1]))

        matches = []
        for loc in locations:
            match = True
            for match_loc in matches:
                if abs(loc[0] - match_loc[0]) < template_width and \
                        abs(loc[1] - match_loc[1]) < template_height:
                    match = False
                    break
            if match:
                matches.append(loc)
        return len(matches)

    @staticmethod
    def get_chess_star(image, threshold=0.8):
        """数棋子星级: 用 chess_star.jpg 模板匹配 CHESS_STAR 区域"""
        # cv2.imread 不支持中文路径, 用 np.fromfile + imdecode 读取
        path = os.path.join(IMAGE_PATH, "chess_star.jpg")
        template = None
        if os.path.exists(path):
            data = numpy.fromfile(path, dtype=numpy.uint8)
            template = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if template is None:
            return 0
        if image is None or image.size == 0:
            return 0
        # 模板按设备分辨率缩放(基准 1024x720)
        if image.shape[0] != template.shape[0] or image.shape[1] != template.shape[1]:
            fx = image.shape[1] / BASE_RESOLUTION[0]
            fy = image.shape[0] / BASE_RESOLUTION[1]
            scaled = cv2.resize(template, None, fx=fx, fy=fy, interpolation=cv2.INTER_LINEAR)
            template = scaled
        if template.shape[0] > image.shape[0] or template.shape[1] > image.shape[1]:
            return 0
        return SdkUtils.count_template_matches(template, image, threshold)
