# -*- coding: utf-8 -*-
"""对局信息数据结构(移植自 Sunflower datatype.py)"""

from dataclasses import dataclass


@dataclass
class BasicGameInfo:
    coin: int
    level: int
    period: tuple
    store: list


@dataclass
class Chess:
    name: str
    star: int
    location: tuple          # (行, 列) 棋盘 或 (索引,) 备战区/商店
    is_candidate: bool
    equipments: list
