import os

# ============================================================
# 金铲铲之战 自动化脚本 — 配置文件
# ============================================================

# ------ 项目路径 ------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "images", "buttons")
SCREENSHOT_DIR = os.path.join(BASE_DIR, "images", "screenshots")

# ------ 游戏窗口设置 ------
# 雷电模拟器标题关键字（用于自动定位窗口）
WINDOW_TITLE_KEYWORDS = ["雷电模拟器", "LDPlayer", "金铲铲"]

# 若无法自动定位，可手动指定游戏区域 (left, top, width, height)
GAME_REGION = None  # 例如: (0, 0, 1920, 1080)

# ------ 置信度阈值 ------
CONFIDENCE_THRESHOLD = 0.45
CONFIDENCE_HIGH = 0.9
CONFIDENCE_LOW = 0.45

# ------ 点击延迟（秒）------
CLICK_DELAY = 0.3
PAGE_LOAD_DELAY = 1.5
MATCH_FOUND_DELAY = 0.5
PREPARE_PHASE_DELAY = 1.0

# ------ 图片资源路径 ------
class ButtonImages:
    BASE = IMAGE_DIR

    # 一、主界面开始游戏
    START_GAME_1 = os.path.join(BASE, "start_game_1.png")

    # 二、排位模式
    RANKED_TAB = os.path.join(BASE, "ranked_tab.png")
    STANDARD_RANKED = os.path.join(BASE, "standard_ranked.png")
    START_GAME_2 = os.path.join(BASE, "start_game_2.png")

    # 三、匹配开始
    START_GAME_3 = os.path.join(BASE, "start_game_3.png")

    # 四、接受对局
    ACCEPT_MATCH = os.path.join(BASE, "accept_match.png")

    # 五、问号圆圈（两种?标志）
    QUESTION_MARK = os.path.join(BASE, "question_mark.png")
    QUESTION_MARK_2 = os.path.join(BASE, "question_mark_2.png")
    QUESTION_MARK_3 = os.path.join(BASE, "question_mark_3.png")

    # 六、金币拾取
    GOLD_COIN = os.path.join(BASE, "gold_coin.png")

    # 七、商店区域（英雄位由动态坐标计算，无需截图）

    # 八、购买经验
    BUY_EXPERIENCE = os.path.join(BASE, "buy_experience.png")
    BUY_EXPERIENCE_2 = os.path.join(BASE, "buy_experience_2.png")

    # 九、特技选择
    TALENT_1 = os.path.join(BASE, "talent_1.png")
    TALENT_2 = os.path.join(BASE, "talent_2.png")
    TALENT_3 = os.path.join(BASE, "talent_3.png")

    # 十、阶段标识（用于判断当前是准备阶段还是战斗阶段）
    PREPARE_PHASE_SIGN = os.path.join(BASE, "prepare_phase.png")
    BATTLE_PHASE_SIGN = os.path.join(BASE, "battle_phase.png")

    # 11. game start
    GAME_START = os.path.join(BASE, "game_start.png")

    # 12. plus
    PLUS = os.path.join(BASE, "plus.png")

    # 13. settlement
    MANAGE = os.path.join(BASE, "manage.png")
    STOP_GAME = os.path.join(BASE, "stop_game.png")
    CONFIRM = os.path.join(BASE, "confirm.png")
    STOP_NOW = os.path.join(BASE, "stop_now.png")
    NEXT_PERIOD_1 = os.path.join(BASE, "next_period_1.png")
    NEXT_PERIOD_2 = os.path.join(BASE, "next_period_2.png")
    GAME_AGAIN = os.path.join(BASE, "game_again.png")

# ------ 坐标区域（1920x1080参考）------
class ScreenCoords:
    # 商店英雄位 (横向5个)
    SHOP_SLOTS = [
        (710, 985),
        (820, 985),
        (930, 985),
        (1040, 985),
        (1150, 985),
        (1260, 985),
        (1315, 985),
        (1370, 985),
        (1480, 985),
    ]

    # 备战区（下方格子，最多9个）
    BENCH_SLOTS = [
        (520, 890), (620, 890), (720, 890), (820, 890), (920, 890),
        (1020, 890), (1120, 890), (1220, 890), (1320, 890),
    ]

    # 战斗区英雄位（棋盘格，从上到下、从左到右）
    BATTLE_SLOTS = [
        # 第一行（己方前排）
        (580, 660), (730, 660), (880, 660), (1030, 660), (1180, 660),
        # 第二行（己方后排）
        (580, 770), (730, 770), (880, 770), (1030, 770), (1180, 770),
    ]

    # 武器/装备栏
    WEAPON_SLOTS = [
        (1400, 400),
        (1400, 480),
        (1400, 560),
        (1400, 640),
        (1400, 720),
    ]

    # 屏幕中心
    SCREEN_CENTER = (960, 540)

    # 购买经验按钮
    BUY_EXP_BTN = (1600, 980)

    # 刷新按钮
    REFRESH_BTN = (1350, 985)

    # 特技选择（最左边）
    TALENT_CHOICE = (430, 600)

    # 金币拾取区域
    GOLD_COLLECT_REGION = (0, 0, 1920, 1080)

    MAX_BATTLE_HEROES = 10
    MAX_BENCH_SLOTS = 9
