# -*- coding: utf-8 -*-
"""Sunflower SDK 基础类型(移植自 github.com/Carey8175/Sunflower)

Sunflower 的所有 UI 坐标都以 1024x720 设备分辨率为基准,
实际设备分辨率不同时通过 BoxScaler 按比例缩放, 因此兼容雷电模拟器的任意分辨率。
"""

from dataclasses import dataclass

# Sunflower 基准分辨率(MuMu 模拟器, 作者校准时的设备分辨率)
BASE_RESOLUTION = (1024, 720)


@dataclass
class BoundingBox:
    x: int
    y: int
    width: int
    height: int

    def get_middle_coordinate(self):
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass
class OcrResult(BoundingBox):
    text: str


class BoxScaler:
    """把 1024x720 基准坐标缩放到实际设备分辨率"""

    def __init__(self, device_size):
        if not device_size or device_size[0] <= 0 or device_size[1] <= 0:
            raise ValueError(f"非法的设备分辨率: {device_size}")
        self.device_size = device_size
        self.sx = device_size[0] / BASE_RESOLUTION[0]
        self.sy = device_size[1] / BASE_RESOLUTION[1]

    def scale(self, box):
        if box is None:
            return None
        return BoundingBox(
            x=round(box.x * self.sx),
            y=round(box.y * self.sy),
            width=round(box.width * self.sx),
            height=round(box.height * self.sy),
        )
