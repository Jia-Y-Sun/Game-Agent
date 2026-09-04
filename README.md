# 王者荣耀 / 金铲铲 统一调度

本目录集中管理: 两套游戏自动化程序本体(wzry / jinchanchan) + 统一调度器(scheduler)。

## 布局

    game agent/
    ├── scheduler/            # 统一调度器(纯调用/串行, 不修改任何下游代码)
    │   ├── main.py           # 入口: 轮询识别当前画面 -> 串行调用对应程序子进程
    │   ├── config.py         # 路径/阈值/启动命令等全部配置
    │   ├── launcher.py       # 纯 subprocess 串行 启动/停止 王者 或 金铲铲
    │   ├── state.py          # 串行运行状态(空闲 / 跑王者 / 跑金铲铲)
    │   └── detectors/        # 画面识别层(判断同一片雷电屏幕此刻属于哪款)
    │       ├── capture.py    #   统一截图抽象
    │       ├── recognizer.py #   特征判定 -> 归到场景(Scene)
    │       └── features/     #   各游戏"专属界面特征"判定
    │           ├── wzry_features.py
    │           └── jinchanchan_features.py
    ├── wzry/                 # 王者荣耀程序本体(分类迁入, 不被本工程改)
    └── jinchanchan/          # 金铲铲程序本体(分类迁入, 不被本工程改)

## 架构要点(已敲定需求)

1. decision-1(B): 两套程序*假设都跑在雷电模拟器、同屏可见*。调度器截"同一片屏幕",
   用各游戏"专属界面特征"判断当前是哪款/哪一屏, 再把控制权交给对应主程序。
2. decision-2( 串行 ): 任一时刻至多一个游戏子进程在跑; 仅在识别结果显示需要
   切换某款时才 "停旧启新"。
3. decision-3( 纯调用 ): 调度器不 import 也不修改两套现有代码, 只通过 subprocess 启停。
4. build: 王者尚未迁到雷电模拟器(现为真机 scrcpy)。迁好后在 wzry/ 补一套界面特征模板图
   (参考金铲铲 images/buttons), 识别器对缺失会给提示而不崩溃, 框架不变。

## 运行说明

- DRY_RUN=True 是默认, 只打印决策, 不真启动, 便于先跑通闭环看画面识别结果。
- 真要接管时把 config.DRY_RUN 改为 False, 并把 GameLauncher 的启动 argv / 设备号填准。
- 依赖(如需真跑识别): pyautogui / mss(截图) + opencv-python / easyocr(模板与文字)。
