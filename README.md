# 多游戏自动拨测与自动化操作平台：金铲铲之战 + 王者荣耀

本仓库将金铲铲之战与王者荣耀整合为**同一套调度 + 自动化操作平台**下的两个独立执行器：
共用设备绑定、前置导航、采集(录屏/抓包/QoE)、对局生命周期事件(JSONL)、单轮/多轮编排和按游戏的中文报告。

平台由两个来源整合而成:
- **金铲铲自动化(自研)**: ADB 直连的 Sunflower SDK 移植 + 环节识别状态机 + 全自动对局循环(实机验证)
- **jcc-auto-probe 编排框架**: minipilot 前置视觉 Agent + 游戏配置 + 单轮/多轮编排 + 采集后处理(代码接入, 待实机验收)

## 工程结构

```text
minipilot-main/            前置视觉 Agent、设备/采集接口与编排器
  integration/
    game_profiles.py        两款游戏的包名、入口和前置目标
    game_probe_orchestrator.py   单轮编排(采集 + 前置导航 + 对局执行 + 报告)
    multi_round_orchestrator.py  同一游戏多轮编排与重试
    probe_report.py         按游戏生成中文报告
  mini_pilot/               视觉 Agent(VLM 前置导航)与设备接口
  unicapture/               录屏、抓包、逐秒 QoE 与后处理
jinchanchan/               金铲铲执行器
  auto_loop.py              对局执行器(standalone / --mode external 双模式)
  agent/sdk/                Sunflower SDK 移植(ADB 直连, 支持 TCP 模拟器与 adb 二进制+serial 真机)
  agent/lifecycle.py        external 模式的 JSONL 生命周期契约
  run_capture_probe.ps1     单轮/多轮拨测启动脚本
wzry/                      王者执行器
  train.py                  对局执行器(external 模式入口)
  pregame_driver.py         确定性前置导航(OCR 规则)
  lifecycle.py / navigation_rules.py / runtime_paths.py
  run_capture_probe.ps1     单轮/多轮拨测启动脚本
scheduler/                 历史跨游戏调度实验(默认 DRY_RUN)
docs/                      架构、实现与验证状态、来源与历史记录
```

## 共用流程

启动采集 → 启动游戏/前置导航 → `MATCH_READY` → 对局执行 → `MATCH_FINISHED` → 停止采集 → 后处理 → `manifest.json` + `report.md`。

- 金铲铲: 环节识别状态机驱动(主菜单→模式选择→匹配房间→接受→对局: 备战买英雄/经验/上阵、战斗问号识别、选秀点中心 → 结算 → 再来一局), 前置于对局的导航可用 minipilot 视觉 Agent 兜底。
- 王者荣耀: ADB + OCR 规则前置导航进入单人练习场, 局内策略由 DQN/BC 模型执行。
- 生命周期契约: 执行器以 `--mode external` 运行, 向 `--event-file` 写 JSONL 事件 `MATCH_READY` / `MATCH_FINISHED` / `ERROR`, 受 `--stop-file` 和 `--timeout` 约束, 退出码见 `agent/lifecycle.py`。

## 部署

目标环境: Windows / PowerShell / 雷电模拟器(或 USB 真机)。设备序列号以 `adb devices` 为准。

```powershell
# 1) 采集/编排环境
py -3.12 -m venv .\minipilot-main\.venv
& .\minipilot-main\.venv\Scripts\python.exe -m pip install -r .\minipilot-main\requirements.txt
Copy-Item .\minipilot-main\mini_pilot.config.example.json .\minipilot-main\mini_pilot.config.json

# 2) 金铲铲环境(与采集环境一致的 Python 3.12)
py -3.12 -m venv .\.venv   # 或直接用系统 python(已装依赖)
& .\.venv\Scripts\python.exe -m pip install -r .\jinchanchan\requirements.txt

# 3) 王者荣耀: 独立 Python 3.10 环境, 见 wzry/README.md(模型与依赖清单)
```

ADB、scrcpy、FFmpeg、PCAPdroid 等外部工具需自行准备(见 minipilot-main/README.md)。

## 运行入口

先预览命令(-DryRun 不启动游戏/设备/采集):

```powershell
.\jinchanchan\run_capture_probe.ps1 -DryRun -RunId jcc-preview
.\wzry\run_capture_probe.ps1 -DryRun -RunId wzry-preview
```

真实运行(设备号按本机修改):

```powershell
.\jinchanchan\run_capture_probe.ps1 -DeviceId '127.0.0.1:5555' -RunId jcc-round-01
.\wzry\run_capture_probe.ps1 -DeviceId 'emulator-5554' -RunId wzry-round-01
```

多轮(同一游戏连续多轮, 上一轮结算后自动进入下一局):

```powershell
.\jinchanchan\run_capture_probe.ps1 -Rounds 2 -SessionId jcc-two-rounds
```

不走采集编排、直接跑金铲铲全自动循环(实机已验证):

```powershell
python jinchanchan\auto_loop.py                          # adb_shell TCP(雷电 5555)
python jinchanchan\auto_loop.py --adb-bin --serial emulator-5554 --adb-path D:\leidian\LDPlayer9\adb.exe   # adb 二进制+serial(真机/模拟器)
```

## 结果与判断

默认产物在 `minipilot-main/runs/<run_id>-<game_id>-external/`:
- `manifest.json`: 运行/采集/后处理状态与结束来源
- `events/game_driver.jsonl`: 权威生命周期事件
- `capture/`: 视频、抓包、逐秒 QoE、流级结果和后处理摘要
- `report.md`: 按游戏的中文汇总

`status=ok` 表示流程正常返回; 采集文件、后处理状态、胜负识别仍需人工复核。真实登录、验证码、实名和账号安全页面需人工处理。

## 文档

- [实现与验证状态](docs/IMPLEMENTATION_STATUS.md): 验证记录、未验证/未实现项
- [架构与技术路线](docs/架构与技术路线.md)、[对接任务清单](docs/对接任务清单与实施指南.md)
- [金铲铲全自动循环总结](jinchanchan/对局自动化实现总结.md)(实机验证)
- [王者采集接口](wzry/CAPTURE_INTEGRATION.md)

源码和必要的小型识别素材入库; 密钥配置、虚拟环境、模型、录屏、抓包、日志和运行数据不入库。
