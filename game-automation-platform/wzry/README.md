# wzry_try — 王者荣耀自动化训练框架

![python](https://img.shields.io/badge/python-3.10-blue)
![platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![device](https://img.shields.io/badge/device-Android%20%7C%20LDPlayer-green)
![purpose](https://img.shields.io/badge/purpose-learning%20%26%20research-orange)

一个可以**自己打、也可以被人教**的游戏自动化训练框架：截图 → 决策 → adb 执行 → 算奖励 → 学策略。
支持「全自动自学」和「整场人为辅助」两条路线。

网络默认采用与原项目**同体量**的 `legacy` 结构（1.99 亿参数 / 762MB / 640 输入）；
需要省显存时可切到 `compact` 小结构（110 万参数 / 4.2MB，配 `--obs_size 160`）。

> ⚠️ **声明**：本项目仅用于人工智能学习与研究，**严禁用于外挂、代练或任何影响他人游戏体验的行为**。
> 请只在训练营 / 人机 / 离线模式中使用，自负使用后果。

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [文档](#文档)
- [项目结构](#项目结构)
- [实测数据](#实测数据)
- [与上游项目的关系](#与上游项目的关系)
- [许可](#许可)

---

## 特性

**1. 整场人为辅助训练（本项目的主要改造）**

| 模式 | 行为 |
| --- | --- |
| `assist` | AI 一直在打；人一碰屏幕就立刻停止注入、把控制权让出去；松手约 2~3 步后自动还给 AI |
| `manual` | 整局由人打，AI 只观测不注入，用来一次录完整场示范 |
| `partial` | 只接管人碰到的分支（例如只推摇杆，普攻/技能仍由 AI 补） |

接管期间规则层的步数与内部状态会冻结；每一步都记录「AI 想做什么 / 人做了什么 / 实际执行了什么」，
可以直接用于行为克隆、DQN 的优先采样和辅助模仿损失。

**2. 示范 → 先验 → 训练 的完整链路**

```text
人示范（assist/manual）
   ├─→ assist_samples.jsonl ─→ train_behavior_clone.py ─→ 初始策略
   └─→ build_game_profile.py ─→ src/game_profile.json ─→ 按阶段回放人的节奏
```

**3. 两种跑法：完全本地 / 双端**

| | 完全本地（默认） | 双端 |
| --- | --- | --- |
| 游戏、adb、截图、注入 | 本机 | 本机（`device_agent.py`） |
| 视觉、策略、奖励、训练 | 本机 | 另一台机器（`train.py --device_source remote`） |
| 本机显卡 | 需要（≥6GB） | **不需要** |
| 网络 | 不需要 | 局域网，每步 ~0.6MB |

**训练用的张量不过网**：程序端拿到帧自己算观测/奖励/存经验池，网上只有一个帧和一个动作。
详见 [docs/training-modes.md](docs/training-modes.md)。

**4. 模型规模与显存**

| 结构 | 参数量 | `.pt` 体积 | 输入 | 训练峰值显存 | 单步 |
| --- | --- | --- | --- | --- | --- |
| `legacy`（**默认**，与原项目一致） | 199,788,562 | **762 MB** | 640 | 5046 MB（AMP） | 430 ms |
| `compact`（可选，省显存） | 1,099,922 | 4.2 MB | 160 | 159 MB | 2.4 ms |

- 默认 `legacy` + AMP 自动开启（实测比 fp32 省 266MB 且快 1.9 倍）；
- 经验池按 `--memory_ram_limit_gb`（默认 8GB）自动限幅，640 观测下约 3495 条；
- ONNX/OCR 懒加载 + 会话显存上限，可整体赶到 CPU；
- ε 按环境步衰减、训练次数与环境步绑定（含时间预算）、TD 目标裁剪，避免「探索几十步就没了」和「Q 值撞上限」。

**5. 工程化**

- 每一步的指标写 JSONL（可轮转），`analyze_episode.py` 一键出单局报告；
- `bench_vram.py` 量化显存/耗时；`test_assist_pipeline.py` 100 项离线自检（不需要手机，含双端链路环回测试）；
- 规则层（开局走线 / 阶段控制 / 小地图 / 目标跟踪）与学习层解耦，可以单独关掉。

---

## 快速开始

```bat
:: 1) 环境
conda create -n wzry_ai python=3.10 -y && conda activate wzry_ai
pip install -r requirements.txt

:: 2) 放入 scrcpy 与检测模型（见 docs/quickstart.md 第 2.3 节）
::    scrcpy-win64-v2.0\   models\start.onnx   models\death.onnx

:: 3) 连模拟器
.\scrcpy-win64-v2.0\adb.exe connect 127.0.0.1:5555
.\scrcpy-win64-v2.0\adb.exe devices

:: 4) 进游戏对局后启动（参数需按你的分辨率标定，见 quickstart 第 4 节）
python train.py --iphone_id 127.0.0.1:5555 --assist_mode off --move_joystick_center_x 0.226 --move_joystick_center_y 0.800 --move_joystick_radius 184 --step_info_log false --step_timing_debug false --action_preview_enabled false
```

看到 `step=...` 持续滚动 + 偶尔的 `training ...` 行就是跑起来了。
默认会建 1.99 亿参数的 `legacy` 网络（约 5GB 显存、模型 762MB），首次启动慢几秒属正常。
想省显存就加 `--net_arch compact --obs_size 160`。

上面是**完全本地**的跑法。要用另一台机器的显卡，改两步：

```bat
:: 本机（有模拟器那台，不需要 torch）
python device_agent.py --iphone_id 127.0.0.1:5555 --device_listen_port 5599 --device_token mysecret

:: 另一台（有显卡那台）
python train.py --device_source remote --device_endpoint <本机IP>:5599 --device_token mysecret --assist_mode off
```

完整步骤（坐标标定、人为辅助、整场示范）见 **[docs/quickstart.md](docs/quickstart.md)**，
两种模式的详细对照见 **[docs/training-modes.md](docs/training-modes.md)**。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/quickstart.md](docs/quickstart.md) | 从零到跑起来：环境、连模拟器、坐标标定、启动方式、成功判据 |
| [docs/training-modes.md](docs/training-modes.md) | **两种跑法**：完全本地 / 双端（设备本地 + 训练远端）的分步操作与注意事项 |
| [docs/human-assist.md](docs/human-assist.md) | 人为辅助三种模式、数据格式、行为克隆、整场 profile、开局走线旧流程 |
| [docs/architecture.md](docs/architecture.md) | 五层结构、动作空间、模块表、决策链、奖励构成、规则层行为、限制 |
| [docs/vram.md](docs/vram.md) | 显存优化逐条讲解 + 实测数据 + 复现命令 |
| [docs/remote-gpu.md](docs/remote-gpu.md) | 用远程服务器 GPU 跑基准/离线训练：版本匹配、上传、探针、清理 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 按现象排查：连接/截图/动作/采集/训练/显存 |
| [docs/legacy/](docs/legacy/) | 上游历史设计文档（可能过时，仅作背景资料） |

---

## 项目结构

```text
wzry_try/
├── train.py                  # 主循环：截图 → 决策 → 执行 → 经验 → 训练
├── wzry_env.py               # 环境层：感知 + 规则 + 接管仲裁 + 奖励
├── dqnAgent.py / net_actor.py / memory.py / globalInfo.py
├── lane_macro.py             # 开局走线 / 早期对线宏
├── phase_controller.py       # 阶段控制（清线/换血/撤退/推塔…）
├── minimap_vision.py         # 小地图 / 攻击头像 / 暗屏
├── target_tracker.py         # 目标跟踪
├── live_human_assist.py      # 人为接管（getevent + 状态机）
├── assist_dataset.py         # 整场示范数据集
├── game_profile.py / build_game_profile.py / build_lane_profile.py
├── android_tool.py           # adb / scrcpy 执行层（完全本地模式）
├── device_agent.py           # 设备端服务（双端模式：截屏/注入/触摸/流量转发）
├── remote_device.py          # 程序端替身（双端模式：RemoteAndroidTool / 远程流量探针 / 远程触摸流）
├── remote_protocol.py        # 双端通信协议（长度前缀帧，仅标准库）
├── getReword.py              # 奖励与事件检测
├── human_data_collector.py / touch_event_parser.py / touch_event_diagnose.py
├── train_behavior_clone.py / relabel_human_actions.py
├── analyze_episode.py / bench_vram.py / test_assist_pipeline.py
├── tools/check_docs.py       # 文档链接/格式检查（CI 也跑这个）
├── models/                   # start.onnx / death.onnx（需自行放入，不入库）
├── scrcpy-win64-v2.0/        # adb + scrcpy（需自行放入，不入库）
├── src/lane_profile.json     # 开局走线 profile（自带一份默认值）
├── docs/                     # 文档
└── requirements.txt
```

---

## 实测数据

同一台机器（RTX 4060 Laptop 8GB）、同样 `batch=64`、`forward+backward+Adam`：

| 配置 | 参数量 | `.pt` | 峰值显存 | 单步耗时 |
| --- | --- | --- | --- | --- |
| `legacy` + 640 + fp32 | 199,788,562 | 762 MB | 5312.6 MB | 823 ms |
| `legacy` + 640 + **AMP（默认）** | 199,788,562 | 762 MB | **5046.3 MB** | **430 ms** |
| `compact` + 160 + fp32（可选） | 1,099,922 | 4.2 MB | 159.0 MB | 2.4 ms |

同一份代码在 `--net_arch compact --obs_size 160` 下训练一局（1065 步 / 8 分钟）：
总奖励 1030.9、平均 0.968/步、显存峰值约 300MB、单步时延 455ms（其中决策仅 4ms）。
细节与复现命令见 [docs/vram.md](docs/vram.md)。

---

## 与上游项目的关系

本项目的雏形来自开源项目 [myBoris/wzry_ai](https://github.com/myBoris/wzry_ai)（开局走线、DQN、行为克隆、小地图启发式等）。
在此基础上重写了：显存相关代码路径、整场人为辅助、示范数据集与蒸馏、训练调度与稳定性。
上游的历史设计文档保留在 [docs/legacy/](docs/legacy/)，仅作背景参考。

---

## 许可

- 代码仅供**学习与研究**，禁止商业使用，禁止用于外挂 / 代练 / 影响他人游戏体验。详见 [LICENSE](LICENSE)。
- 本仓库的改动基于上游 `wzry_ai`，其原始权利归原作者所有，见 [NOTICE.md](NOTICE.md)。
- 依赖中包含 **PyQt5（GPLv3）**；如果你要分发本项目（尤其是二进制），需要同时遵守 GPLv3 或取得 Riverbank 的商业授权。
