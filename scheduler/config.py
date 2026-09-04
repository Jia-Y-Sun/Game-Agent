# -*- coding: utf-8 -*-
# 统一调度器配置
#
# 设计前提(按已敲定需求):
#  - decision 1(B): 两套程序都假定跑在雷电模拟器、同屏可见。
#    调度器截"同一片屏幕"判断当前画面属于哪款, 再把控制权交给对应主程序。
#  - decision 2   : 串行执行, 任一时刻至多跑一个游戏子进程。
#  - decision 3   : 调度只承担"调用/启停", 不 import、不修改两套现有代码。
#
# 注意: 本文件禁止用文件内 docstring 标量语法书写, 统一用注释说明(便于被外层原样落盘)。
import os

# ---- 目录 ----
# scheduler/ 的上一级 = game agent 根目录
AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WZRY_DIR = os.path.join(AGENT_ROOT, "wzry")          # 王者荣耀程序本体
JCC_DIR  = os.path.join(AGENT_ROOT, "jinchanchan")   # 金铲铲程序本体
SCH_DIR  = os.path.dirname(os.path.abspath(__file__))

# ---- 主程序启动命令(纯 subprocess, 串行) ----
# 每款封装为可调用函数, 返回 argv 列表; run 时 cwd 会切到对应程序目录。
def _wzry_cmd():
    # 王者主程序入口为 train.py(官方命令行较长), 此处给一份示例 argv。
    # 说明(架构阶段): 真机版需 --iphone_id 设备串号; 待迁到雷电模拟器后,
    #   把入口/参数换成在该模拟器上的跑法即可(详见 README / 结束讲解)。
    return [
        "python", "train.py",
        "--model_path", "src/wzry_ai_bc.pt",
        "--behavior_prior_mode", "smart",
        # "--iphone_id", "10AD880X7Q001RX",   # 真机/模拟器设备号, 迁雷电后按需填
        "--move_control_mode", "persistent_motionevent",
        "--move_joystick_radius", "260",
    ]

def _jcc_cmd():
    # 金铲铲程序入口 main.py(无额外参数)
    return ["python", "main.py"]

GAMES = {
    "wzry":        {"dir": WZRY_DIR, "cmd": _wzry_cmd},
    "jinchanchan": {"dir": JCC_DIR,  "cmd": _jcc_cmd},
}

# ---- 画面特征资源根 ----
# 金铲铲: 复用其本体 images/buttons 的按钮模板图(只读引用, 不改)
JCC_TEMPLATES = os.path.join(JCC_DIR, "images", "buttons")
# 王者: 迁到雷电模拟器后在此补充专属特征模板; 目录/文件缺失时识别器提示而不崩溃
WZRY_TEMPLATES = os.path.join(WZRY_DIR, "templates")

# ---- 识别与轮询 ----
CONFIDENCE   = 0.70     # 特征(模板)匹配置信度
POLL_SECONDS = 1.0      # 主循环识别间隔(秒)
DRY_RUN      = True     # True = 只打印决策不做真启动, 便于架构演示/防误操作
