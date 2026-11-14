import time
from config import ScreenCoords
from agent.image_recognition import find_image
from utils.logger import logger


class GameState:
    """游戏状态管理"""

    def __init__(self):
        self.in_game = False              # 是否在对局中
        self.gold = 0                     # 当前金币数（估算）
        self.level = 1                    # 当前等级
        self.battle_heroes = []           # 战斗区英雄列表
        self.bench_heroes = []            # 备战区英雄列表
        self.weapons = []                 # 持有的武器列表
        self.prepare_phase = False        # 是否在备战阶段
        self.phase_start_time = 0         # 备战阶段开始时间

    def reset(self):
        """重置状态（新对局）"""
        self.__init__()
        logger.info("游戏状态已重置")

    def is_bench_full(self):
        """备战区是否满"""
        return len(self.bench_heroes) >= ScreenCoords.MAX_BENCH_SLOTS

    def is_battle_full(self):
        """战斗区是否满"""
        return len(self.battle_heroes) >= ScreenCoords.MAX_BATTLE_HEROES

    def get_empty_bench_slot(self):
        """获取第一个空备战位索引"""
        return len(self.bench_heroes)

    def get_next_battle_slot(self):
        """获取下一个战斗区空位索引（从上到下，从左到右）"""
        return len(self.battle_heroes)

    def add_battle_hero(self, hero_name=""):
        """添加战斗区英雄"""
        if not self.is_battle_full():
            self.battle_heroes.append(hero_name)
            return True
        return False

    def add_bench_hero(self, hero_name=""):
        """添加备战区英雄"""
        if not self.is_bench_full():
            self.bench_heroes.append(hero_name)
            return True
        return False

    def update_gold(self, gold):
        """更新金币数"""
        self.gold = gold

    def get_prepare_remaining(self, total_phase_time=30):
        """获取备战阶段剩余时间"""
        if not self.prepare_phase:
            return 0
        elapsed = time.time() - self.phase_start_time
        return max(0, total_phase_time - elapsed)

    def can_buy_hero(self):
        """是否可以购买英雄"""
        return self.gold >= 2 and not self.is_bench_full()

    def can_buy_exp(self):
        """是否可以购买经验"""
        return self.gold >= 4
