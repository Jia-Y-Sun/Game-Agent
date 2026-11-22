# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [2.2.1] — 2026-11-20

### 修复

- **AMP 下「推理线程污染训练线程」**：`select_action` 原本用 `torch.inference_mode()`，
  AMP 推理时 autocast 会把 fp16 权重缓存成 inference tensor；训练线程（另一条线程）的
  `policy_net(...)` 复用该缓存后直接抛
  `RuntimeError: Inference tensors cannot be saved for backward`，
  导致「第 64 条经验一到就崩、训练线程静默退出、主循环还在继续空转」。
  改为 `torch.no_grad()`（同样不建图，但张量是普通张量）。
  触发条件：`--amp true`（legacy 默认）+ 训练线程独立 —— 完全本地与双端都会中。
- 自检新增第 13 项「推理与训练并发」回归（CUDA 下执行），总项数 98 → 100。

## [2.2.0] — 2026-11-18

### 新增

- **双端模式**：游戏/模拟器/adb/截图留在本机，视觉/策略/奖励/训练放到另一台机器。
  - `device_agent.py`（设备端）：把 `AndroidTool` 的能力（截屏、注入动作、松摇杆、触摸、`/proc/net/dev`）
    通过 TCP 暴露；带 token 校验、单客户端限制、断线与空闲看门狗（自动松摇杆）。
  - `remote_device.py`（程序端替身）：`RemoteAndroidTool` / `RemoteTouchStream` /
    `RemoteProcNetDevTrafficProbe`，接口与本地版一致，`wzry_env`、`dqnAgent` 一行不用改。
  - `remote_protocol.py`：长度前缀帧 + JSON 头 + 可选二进制负载，仅用标准库。
  - 新参数：`--device_source {local,remote}`、`--device_endpoint`、`--device_token`、
    `--device_frame_format {jpeg,png,raw}`、`--device_frame_quality`、`--device_rpc_timeout_ms`、
    `--device_reconnect_attempts`、`--device_listen_host/port`、`--device_watchdog_sec`、
    `--device_getevent_enabled`、`--device_show_scrcpy`。
  - **训练数据不过网**：程序端收到帧后自己算观测/奖励/存经验池，网上只有一帧和一个动作。
- `docs/training-modes.md`：完全本地 / 双端两套完整操作流程、两端参数对照、分步验证、故障排查。
- 自检新增 16 项：协议编解码与 socket 往返、双端链路环回（握手/截图/动作/流量/触摸/断开松摇杆）。

### 变更

- `argparses.py` 改为 **torch 可选**：设备端不需要装 torch 也能导入参数与坐标表。
- `traffic_probe.py` 抽出 `parse_proc_net_dev()`，本地与远程共用同一套解析。
- `train.py` 按 `--device_source` 决定用 `AndroidTool` 还是 `RemoteAndroidTool`，
  `os.add_dll_directory` 加了平台判断（非 Windows 不再报错）。
- `live_human_assist.py` 的触摸流来源可替换：工具对象提供 `create_touch_stream()` 时走网络流，
  否则仍是 `adb shell getevent`。
- 自检项数 82 → 98，相关文档同步更新。

## [2.1.0] — 2026-11-18

### 变更

- **默认模型规模恢复为与原项目同体量**：`--net_arch legacy`（199,788,562 参数 / 640 输入 /
  762MB `.pt`）、`--obs_size 640`。`compact`（1,099,922 参数 / 4.2MB）改为显式可选：
  `--net_arch compact --obs_size 160`；`auto` 仍等同 `compact`。
- **AMP 改为按结构自动**：`--amp` 默认 `None`，`legacy` 开启（实测 5313MB/823ms → 5046MB/430ms）、
  `compact` 关闭（小网络开 AMP 反而慢 5 倍）；`--amp_inference` 跟随 `--amp`。
- **新增 `--memory_ram_limit_gb`（默认 8）**：按观测尺寸预估经验池内存并自动下调 `--memory_size`；
  640 观测下每条 2.34MB，默认从 10000 条降到约 3495 条（8GB）。
- **新增 `--train_time_budget_per_step_sec`（默认 0.8）**：在次数限速之外再加时间预算，
  避免 legacy 单次迭代 0.43s 时训练饿死采集。
- `bench_vram.py` 增加 legacy + AMP 用例；启动日志新增 `params / weights / memory / batch` 汇总行。
- 跨结构加载 checkpoint 时新增明确提示（不是无缝续训，建议 `--model_path src/none.pt` 从头训）。
- 文档按新默认改写：`README.md`、`docs/vram.md`、`docs/quickstart.md`、`docs/architecture.md`、
  `docs/troubleshooting.md`；新增 `docs/remote-gpu.md`（远程服务器 GPU 跑基准/离线训练的完整步骤与坑）。

### 说明

- 之前的 compact 权重已重命名为 `src/wzry_ai_compact.pt`，与默认 `src/wzry_ai.pt`（legacy 762MB）区分；
  两者不通用，跨结构只能迁移共享的卷积层。

## [2.0.0] — 2026-11-14

### 新增

- **整场人为辅助训练**：`--assist_mode assist|manual|off` + `--assist_partial`，接管状态机
  （`IDLE → HUMAN → RELEASING`），接管期间冻结规则层。
- **整场示范数据集** `assist_dataset.py`：JSONL schema v2（`act / ai_act / human_act / phase / obs`），
  延迟落盘的预卷帧，兼容旧的 `samples.jsonl`。
- **整场行为 profile**：`build_game_profile.py` + `game_profile.py`，按阶段蒸馏人的节奏，
  `--game_profile_prior_mode off|fill|force`。
- **单局统计** `analyze_episode.py`：奖励构成、探索度（ε 占比 / 动作熵 / 阶段覆盖）、动作分布、事件、时延。
- **离线自检** `test_assist_pipeline.py`（79 项）与**显存基准** `bench_vram.py`。
- 触摸流自愈：`getevent` 意外退出会自动重启；`--assist_debug_touch` 打印原始触摸行。
- 文档检查工具 `tools/check_docs.py` + GitHub Actions（语法 + 文档链接）。

### 变更

- **网络结构**：新增 `compact`（`AdaptiveAvgPool2d` + 小 `fc`），参数量 1.99 亿 → 110 万；
  旧 checkpoint 自动识别为 `legacy` 并做部分权重迁移。
- **观测**：默认 `--obs_size 160`，经验池只存 uint8 小图；批量数据一次拷到 GPU。
- **训练调度**：ε 与目标网络默认按**环境步**衰减/同步；新增 `--replays_per_env_step 4` 限速。
- **奖励**：新增 `--reward_scale 0.1`（写入经验前缩放）与 `--probe_reward_scale 0.4`（削弱“动一动就有分”）。
- **对局结束**：除胜负 OCR 外，新增网络流量健康度判据 + `--max_episode_steps` 兜底，
  并丢弃结束前最后 `--episode_end_drop_steps` 步经验。
- **ONNX / OCR**：懒加载、`arena_extend_strategy=kSameAsRequested`、`gpu_mem_limit`、
  可整体切 CPU（`--onnx_provider / --ocr_provider`）。
- **执行层**：`execute_action_burst(human_control=...)`；开回被上游关闭的 cuDNN benchmark。
- **日志**：`--step_info_log false` 输出紧凑单行（step/phase/macro/action/reward/eps/src/assist），
  `--step_metrics_max_mb` 支持指标文件轮转。
- **资源路径**：移除“回退到兄弟目录”的隐式逻辑，改为 `--asset_root` 显式指定；
  仓库不再依赖任何项目外目录（大文件用说明 + `.gitignore` 处理）。
- **文档**：整合为 `README.md` + `docs/` 五篇，删除重复内容；上游历史文档移入 `docs/legacy/`，
  并新增 LICENSE / NOTICE / CONTRIBUTING / CHANGELOG / CI。
- **依赖**：移除未使用的 `keyboard` / `matplotlib`，补齐 `torch` / `onnxruntime` / `numpy`。

### 修复

- 触摸还原沿用硬编码摇杆中心，未跟随 `--move_joystick_center_*`，标定后会导致方向误判。
- ε 按训练迭代衰减，训练线程满速时约 30 个环境步就把探索率打到下限。
- TD 目标无上限导致 Q 值自举发散（实测 loss 涨到 2e7）；新增裁剪后稳定。
- checkpoint 奖励尺度与当前 `--reward_scale` 不一致时 Q 会卡在裁剪上限；加载时给出警告。
- `test_move_control.py` 的 `-- --iphone_id ...` 透传写法会报错。

## [1.0.0] — 2026-06-02

上游 `wzry_ai` 基线：开局走线、阶段控制、小地图/目标启发式、DQN、行为克隆、人为数据采集。
