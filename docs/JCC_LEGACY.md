# 金铲铲之战 自动化拨测与 QoE 卡顿检测（统一工程）

> 历史文档：保存整合前的根 README。当前部署命令和完成度以 [根说明](../README.md) 及 [状态表](IMPLEMENTATION_STATUS.md) 为准。下面的相对路径按原仓库根目录语境理解。

把「Android 自动化采集与后处理工程」与「金铲铲对局执行器」串成**一条命令即可跑完**的自动拨测流水线：

```text
启动采集 -> 启动游戏 -> 前置 Agent 处理登录/公告/大厅/匹配/接受对局
        -> MATCH_READY -> 对局执行器接管 -> MATCH_FINISHED
        -> 停止采集 -> 自动 QoE 后处理 -> 输出统一 manifest.json
```

核心不是重写任何一侧，而是新增一层**跨进程的协议与编排**：用 JSONL 事件把「不确定的页面导航」「确定性的对局操作」「被动的数据采集」对齐到同一条业务时间轴上。

## 仓库结构

```text
jcc-auto-probe/
├── README.md                       本说明
├── docs/
│   ├── 架构与技术路线.md            分层架构图、流程、各模块技术选型
│   ├── 对接任务清单与实施指南.md      双方接口契约与验收标准
│   └── 组会汇报-项目工作日志.md       实机联调记录与当前进展
├── minipilot-main/                 采集与总控工程（MiniPilot + Unicapture）
│   ├── integration/                ★ 统一总控层（本次新增）
│   ├── mini_pilot/                 前置视觉 Agent、ADB 设备控制、采集会话
│   └── unicapture/                 录屏 / 抓包 / logcat / 实时 QoE / 后处理
└── game-agent/                     对局执行工程
    ├── jinchanchan/                金铲铲对局执行器（auto_loop.py）
    └── scheduler/                  多游戏统一调度器（预留）
```

## 系统架构

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ L1 编排层  minipilot-main/integration/                                     │
│   game_probe_orchestrator.py   单轮总控状态机（主入口）                      │
│   multi_round_orchestrator.py  多轮串行会话编排                             │
│   game_events.py               JSONL 事件尾部读取 + seq 校验                │
│   game_driver.py               对局器进程适配（stop-file / 退出码）          │
│   pregame_agent.py             前置 Agent 进程适配（live-command /stop）     │
└──────┬────────────────┬────────────────────┬────────────────────┬─────────┘
       │ subprocess     │ subprocess         │ subprocess         │ 文件事件
       ▼                ▼                    ▼                    ▼
┌─────────────┐  ┌──────────────┐  ┌─────────────────┐  ┌──────────────────┐
│ L2 前置交互  │  │ L3 对局执行   │  │ L4 采集          │  │ L5 状态通道       │
│ mini_pilot/ │  │ jinchanchan/ │  │ unicapture/     │  │ events/*.jsonl   │
│ Seed 2.0 Pro│  │ auto_loop    │  │ app_collector   │  │ control/*.stop   │
│ ADB 操作     │  │ external     │  │ qoe_monitor     │  │ business_timing  │
└─────────────┘  └──────────────┘  └─────────────────┘  └──────────────────┘
                                          │
                                          ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ L6 后处理层  unicapture/postprocess_runner.py（四阶段隔离执行）             │
│   ① video_timing  ② activate_time  ③ qoe（逐秒）  ④ flow_labeling        │
└───────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ L7 产物层  runs/<run_id>/                                                 │
│   manifest.json  events/  control/  game_driver.log  capture/<sample>/    │
└───────────────────────────────────────────────────────────────────────────┘
```

详细的分层图、控制权边界图与各模块技术选型见 [docs/架构与技术路线.md](docs/架构与技术路线.md)。

## 工作流程

| 步骤 | 动作 | 负责模块 | 产出 / 证据 |
|---|---|---|---|
| 0 | 设备预检：ADB 连接、`wm size`、DPI、横屏 | `mini_pilot/device.py` | `DEVICE_READY` 事件 |
| 1 | **先启动采集**（录屏 + 抓包 + logcat + 实时 QoE） | `mini_pilot/capture.py` → `unicapture/app_collector.py` | `unicapture.ready` |
| 2 | 启动游戏 `ZGamePolicyActivity` | `game_probe_orchestrator._launch_game` | 可 `--skip-launch-game` |
| 3 | 启动对局器（external 等待中）+ 前置 Agent | `game_driver.py` / `pregame_agent.py` | `SCRIPT_STARTED` |
| 4 | 前置 Agent 冷启动 → 大厅 → 匹配 → 接受对局 | `mini_pilot/agent.py` + Seed 2.0 Pro | 前置 run 目录与截图 |
| 5 | 严格 `MATCH_READY`（连续 2 次、间隔 1.5s、合法回合） | `jinchanchan/agent/lifecycle.py` | `MATCH_READY` |
| 6 | 写入业务开始标记（采集早于业务开始） | `game_probe_orchestrator._write_marker` | `capture/business_timing.json` |
| 7 | `/stop` 停止前置 Agent，移交控制权 | `pregame_agent.request_stop` | `live_commands.jsonl` |
| 8 | 对局内操作 + 阶段事件 | `jinchanchan/auto_loop.py` | `ROUND_PHASE`（buy/battle/carousel/augment） |
| 9 | 自然结算，立即停止游戏输入 | `lifecycle.parse_match_result` | `MATCH_FINISHED`（result/placement） |
| 10 | 更新业务结束标记 → 停对局器 → 停采集 | `game_events.py` / `capture.stop()` | `SCRIPT_STOPPED` |
| 11 | 自动后处理四阶段 | `unicapture/postprocess_runner.py` | `postprocess/summary.json` |
| 12 | 写统一清单 | `game_probe_orchestrator._write_manifest` | `manifest.json` |

## 接口契约

**事件协议（权威状态通道，JSONL）**

```json
{"schema_version":1,"run_id":"run-1","seq":1,"ts":"2026-09-29T21:00:00+08:00","event":"SCRIPT_STARTED"}
{"schema_version":1,"run_id":"run-1","seq":4,"ts":"2026-09-29T21:00:03+08:00","event":"MATCH_READY","round":"2-2"}
{"schema_version":1,"run_id":"run-1","seq":5,"ts":"2026-09-29T21:01:03+08:00","event":"MATCH_FINISHED","result":"loss","placement":6}
{"schema_version":1,"run_id":"run-1","seq":6,"ts":"2026-09-29T21:01:04+08:00","event":"SCRIPT_STOPPED","reason":"match_finished"}
```

- `seq` 严格递增；`MATCH_READY` / `MATCH_FINISHED` 每次运行最多各一次；`SCRIPT_STOPPED` 必须是最后一条。
- 事件文件先写临时文件再原子替换，总控只读事件文件，不解析 stdout。

**退出码**

```text
0 正常结束   1 业务失败   2 参数或环境错误   3 外部停止文件触发   4 超时退出
```

**时间语义**

```text
CAPTURE_STARTED  <  MATCH_READY  <=  MATCH_FINISHED  <  CAPTURE_STOPPED  <  POSTPROCESS_FINISHED
```

业务时长只由 `MATCH_READY → MATCH_FINISHED` 计算，登录、公告、匹配时间不计入对局业务时间。

**控制权边界**

```text
外部总控    ：启动游戏、登录/公告/大厅/匹配/接受对局、启动与停止采集
对局执行器  ：MATCH_READY 之后执行对局操作；MATCH_FINISHED 之后立即停止输入
外部总控    ：停止采集、QoE 后处理、输出统一报告
```

## 快速开始

**环境要求**

```text
Windows + PowerShell
Python 3.12
LDPlayer 14（横屏 1920x1080，DPI 280）
ADB：D:\leidian\LDPlayer14\adb.exe
设备：127.0.0.1:5555（游戏包名 com.tencent.jkchess）
```

**1. 采集工程**

```powershell
cd .\minipilot-main
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .\mini_pilot.config.example.json .\mini_pilot.config.json
# 在 mini_pilot.config.json 中填入自己的 API Key（该文件已被 .gitignore 排除）
```

**2. 对局执行工程**

```powershell
cd ..\game-agent\jinchanchan
python -m pip install -r requirements.txt
```

**3. 单条命令跑一轮完整拨测**

```powershell
cd .\minipilot-main
$adb = "D:\leidian\LDPlayer14\adb.exe"

& .\.venv\Scripts\python.exe -m integration.game_probe_orchestrator `
  --config .\mini_pilot.config.json `
  --adb $adb `
  --device-id 127.0.0.1:5555 `
  --game-python "..\game-agent\.venv\Scripts\python.exe" `
  --game-driver "..\game-agent\jinchanchan\auto_loop.py" `
  --game-directory "..\game-agent\jinchanchan" `
  --run-id my-full-test `
  --game-timeout 3600 `
  --capture-ceiling 3900 `
  --match-ready-timeout 900 `
  --pregame-timeout 600 `
  --pregame-max-loops 40 `
  --business-duration 2400 `
  --postprocess-ocr-every 10 `
  --capture-app jcc `
  --capture-scene battle `
  --capture-pcap-mode standard `
  --profile seed2pro
```

**4. 多轮连续拨测**

```powershell
& .\.venv\Scripts\python.exe -m integration.multi_round_orchestrator `
  --rounds 3 --round-delay 10 --session-id jcc-session-01 `
  --config .\mini_pilot.config.json --adb $adb --device-id 127.0.0.1:5555 `
  --game-python "..\game-agent\.venv\Scripts\python.exe" `
  --game-driver "..\game-agent\jinchanchan\auto_loop.py" `
  --game-directory "..\game-agent\jinchanchan"
```

## 产物目录

```text
runs/<run_id>/
├── manifest.json                 本次运行总清单（状态、业务时间、退出码）
├── events/game_driver.jsonl      对局器事件流（权威状态通道）
├── control/stop.game             外部停止标记
├── game_driver.log               对局器完整日志
├── pregame_agent.log             前置 Agent 日志
└── capture/
    ├── business_timing.json      业务开始/结束标记
    └── <sample>/
        ├── *.mp4  *.pcap  *.log  *.yaml
        ├── *_qoe.csv  *_sa.csv  *_flow_labeled.csv
        └── postprocess/summary.json
```

## 当前状态

已实机跑通：

- 外部总控 + Unicapture + 金铲铲 external 的完整链路：`MATCH_READY → 采集 → MATCH_FINISHED → 后处理 → summary.json`。
- 冷启动前置流程：桌面启动 → 前置 Agent 匹配/接受对局 → 对局器接管 → 自然结算。
- 结算结果已可输出 `result` 与 `placement`（实测 `loss` / 第 6 名）。
- 2026-10-27 最新实机记录：自然结算第 6 名，采集与结算后处理完成；结算后未能稳定返回大厅，因此多轮连续流程仍未验证通过。

已知限制：

- 结算后的返回大厅/重新匹配链路尚不稳定；该问题尚未修复或复测，不应视为完整多轮闭环。
- 登录、公告、广告、权限弹窗仅在已登录态下验证，未真正触发过。
- 后处理依赖 `ffprobe`，缺失时保留 MKV，MP4 校验不完整。
- 模型调用依赖网络，离线环境无法完成完整拨测。
- 前置页面采用「UI 控件 → OCR → 模板匹配 → 视觉模型 → 人工接管」的回退顺序，登录/验证码/实名等默认转人工。

## 子工程来源

| 目录 | 来源 | 说明 |
|---|---|---|
| `minipilot-main/` | 本地自动化采集工程 | MiniPilot + Unicapture，含本阶段新增的 `integration/` 总控层 |
| `game-agent/jinchanchan/` | 金铲铲对局执行器 | 原 `Game-Agent` 仓库的 jinchanchan 子工程，已补 external 模式与生命周期接口 |

`Game-Agent` 中的 `wzry/`（王者荣耀，约 5.9 GB）与本项目无关，未纳入本仓库。
