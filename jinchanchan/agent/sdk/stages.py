# -*- coding: utf-8 -*-
"""对局环节(Stage)识别器 — 全自动循环的"眼睛"

把一局游戏从主菜单到结算拆成细粒度环节, 每个环节用一组"启动标志词"
(OCR 证据)识别。标志词库经过 2026-09-17 实机测试校准:

  - [已确认] 词: 实机测试中确实命中过的词
  - [待实测] 词: 沿袭旧赛季/推测, 尚未在实机中确认

识别逻辑(优先级从高到低):
  1. 标志词表匹配(EVIDENCE 顺序即优先级)
  2. 回合号推导: 识别到"X-4"且无备战标志 -> 选秀轮; 有回合号但无其他标志 -> 战斗环节
     (战斗横幅"战斗开始"只闪 1~2 秒, 采样经常错过, 回合号兜底最可靠)

用法:
    sdk = Sdk()
    await sdk.load(5555)
    det = StageDetector(sdk)
    stage, matched = await det.get_stage()
"""

import re
from enum import auto, StrEnum

from agent.sdk.utils import SdkUtils
from utils.logger import logger


class Stage(StrEnum):
    UNKNOWN = auto()        # 无法识别(过渡/加载画面居多)
    MAIN_MENU = auto()      # 主菜单
    MODE_SELECT = auto()    # 模式选择界面
    QUEUE = auto()          # 匹配房间/队列中
    ACCEPT = auto()         # 弹出"接受对局"
    LOADING = auto()        # 载入对局(玩家列表+百分比)
    AUGMENT = auto()        # 强化符文选择("点击卡片!选择强化效果!")
    CAROUSEL = auto()       # 选秀轮(X-4)
    PREPARE = auto()        # 备战环节
    BATTLE = auto()         # 战斗环节
    RESULT = auto()         # 结算界面
    POPUP = auto()          # 弹窗(公告/奖励/新英雄, 需点关闭)


# 各环节的标志词(OCR 全屏文本去空白后做集合匹配)
# 顺序即优先级: 弹窗/结算/对局内子阶段在最前
EVIDENCE = {
    Stage.POPUP: {
        "每日签到", "今日奖励", "领取奖励", "七日签到", "新手奖励",
        "恭喜获得", "获得奖励", "更新公告", "版本更新",
        "全新英雄", "迎接自然之力的召唤",   # [已确认] 新英雄公告弹窗
    },

    Stage.RESULT: {
        "您获得了", "第一名", "第二名", "第三名", "第四名",   # [已确认] 您获得了/第八名
        "第五名", "第六名", "第七名", "第八名",
        "再来一局", "排名", "段位提升",
        "战绩回顾", "数据详情", "本场段位结算", "胜点", "现在退出", "下一步",  # [已确认]
    },

    Stage.AUGMENT: {
        "点击卡片!选择强化效果!", "选择强化效果", "强化符文选择",   # [已确认] 前两个
    },

    Stage.CAROUSEL: {
        "选秀", "传送门", "选秀环节",   # [待实测] 实机选秀轮无文字横幅, 靠回合号 X-4 兜底
    },

    Stage.PREPARE: {
        "准备阶段", "备战环节", "备战阶段",   # [已确认] 准备阶段(仅开始时闪现)
        "购买经验", "刷新商店",   # [已确认] 购买经验按钮文字, 备战全程可见, 最可靠
    },

    Stage.BATTLE: {
        "战斗开始", "战斗环节",   # [已确认] 战斗开始(仅开始时闪现, 常错过)
    },

    Stage.ACCEPT: {
        # [已确认] 匹配到对手后弹出"对局确定"确认框, 按钮为"接受"
        "对局确定", "接受", "接受对局", "确认进入",
        # 旧赛季词(兜底)
        "对局已找到",
    },

    Stage.QUEUE: {
        "正在匹配", "寻找对局", "匹配中", "预计等待", "取消匹配",
        "房间语音", "战备", "招募",   # [已确认] 战备/房间语音/招募
        "铲友观战", "魔典任务", "本局自动接受匹配", "此处开始聊天", "房间麦克风",   # [已确认]
    },

    Stage.MODE_SELECT: {
        # [已确认] 当前赛季实测: 顶部标签 排位/赛事/匹配, 右侧卡片 标准排位/巅峰赛/
        #          狂暴模式/标准匹配/双人排位, 左侧 海克斯典籍/时空裂痕, 底部 开始游戏
        "赛季玩法", "排位", "赛事", "匹配", "胖胖龙游乐场",
        "标准排位", "巅峰赛", "狂暴模式", "标准匹配", "双人排位",
        "海克斯典籍", "时空裂痕",
        # 注意: "英雄联盟传奇"是主菜单上的模式卡片, 不能作为模式选择界面证据(会误判)
        # 旧赛季词(兜底)
        "排位赛", "匹配对战", "限时模式", "娱乐模式",
    },

    Stage.LOADING: {
        "载入中", "加载中", "游戏即将开始", "正在进入",   # [待实测] 实机载入画面无文字, 只有玩家名+%
    },

    Stage.MAIN_MENU: {
        "商城", "事件", "魔典", "注销",   # [已确认]
        "萌爪补给站", "召唤", "宝库", "活动",   # [已确认]
        # 注意: 不用"开始游戏"——它在匹配房间界面也出现, 会造成误判
    },
}

ROUND_PATTERN = re.compile(r"(\d+)-(\d+)")


def classify(text_set):
    """先按标志词表匹配(模糊匹配, 容错"下一步"被OCR成"下-步"),
    再用回合号推导选秀/战斗, 返回 (Stage, 命中标志列表)"""
    texts = list(text_set)

    for stage, words in EVIDENCE.items():
        matched = []
        for w in words:
            if any(SdkUtils.text_match(t, w) for t in texts):
                matched.append(w)
        if matched:
            return stage, sorted(matched)

    # 回合号推导(注意: 强化选择界面回合号处是"2"这类单数字, 不会误判)
    round_num = None
    for t in texts:
        m = ROUND_PATTERN.search(t)
        if m:
            round_num = m.group(0)
            break

    if round_num:
        sub = int(round_num.split("-")[1])
        if sub == 4:
            return Stage.CAROUSEL, [round_num]
        # 局内(有回合号)但无备战/强化等标志 -> 战斗环节
        return Stage.BATTLE, [round_num]

    return Stage.UNKNOWN, []


class StageDetector:
    def __init__(self, sdk):
        self.sdk = sdk
        self.last = None

    async def get_stage(self):
        """截屏 + OCR + 分类, 返回 (Stage, matched_words, ocr_texts)

        用低置信度 0.35: 界面按钮文字常被识别为低置信度(如"下一步"->"下-步"仅0.45),
        靠模糊匹配+标志词门控防误判。
        """
        results = await self.sdk.get_screen_text(confidence=0.35)
        if not results:
            return Stage.UNKNOWN, [], []
        texts = [r.text for r in results]
        text_set = set(texts)
        stage, matched = classify(text_set)
        return stage, matched, texts

    async def wait_for_stage(self, target, timeout=60, interval=3):
        """轮询等待进入指定环节, 返回 (bool, matched)"""
        import time
        start = time.time()
        while time.time() - start < timeout:
            stage, matched, _ = await self.get_stage()
            if stage == target:
                logger.info(f"[STAGE] 进入 {target.value} (标志: {matched})")
                return True, matched
            await self._sleep(interval)
        logger.warning(f"[STAGE] 等待 {target.value} 超时({timeout}s)")
        return False, []

    @staticmethod
    async def _sleep(seconds):
        import asyncio
        await asyncio.sleep(seconds)
