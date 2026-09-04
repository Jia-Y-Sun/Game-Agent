# 王者荣耀自动化操作脚本技术说明

本文档说明当前自动化脚本的整体实现思路、主要方法、模块划分、已具备功能和当前限制，便于阶段汇报和后续交接。

## 1. 项目目标

当前项目的目标不是制作游戏外挂或追求稳定上分，而是构建一个可控的高交互游戏自动化执行框架，用于在真实手机/受控环境中产生接近正常玩家行为的操作序列，为后续游戏体验、时延、卡顿、流量和交互质量分析提供基础数据。

现阶段重点包括：

- 让角色能够完成开局走线、到线停下、对线、普攻、补刀、技能释放等低水平玩家行为。
- 使用真实玩家触摸数据生成开局走线 profile，使开局移动更接近人工操作。
- 在执行过程中记录动作、视觉状态、目标检测、奖励、流量和步骤耗时等指标。
- 保留强化学习、行为克隆、规则宏和视觉启发式控制的混合框架，便于后续迭代。

## 2. 总体架构

系统可以理解为五层：

```text
游戏画面/手机输入
    ↓
截图、触摸、流量采集层
    ↓
策略决策层：DQN / smart prior / 行为克隆模型
    ↓
规则增强层：开局走线、目标跟踪、对线阶段控制
    ↓
Android 执行层：ADB tap / swipe / motionevent / persistent movement
```

主运行入口是 `train.py`。它循环截图，判断对局是否开始，然后调用 agent 选择动作，再通过 `Environment.step()` 完成动作修正、执行、观测、奖励计算和日志记录。

## 3. 核心动作表示

项目内部使用 8 维动作格式：

```text
[move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]
```

含义如下：

- `move_action`：是否移动，`1` 表示移动。
- `angle`：移动方向角度，屏幕坐标系中 `0` 为向右，`90` 为向下，`270/300` 附近为右上方向。
- `info_action`：购买装备、升级技能、信号等低频信息动作。
- `attack_action`：普攻、补刀、推塔、回城、恢复、技能等攻击/交互动作。
- `action_type`：技能释放方式，普通点击、方向性滑动、长按等。
- `arg1/arg2/arg3`：方向、距离、长按单位等技能参数。

该格式的优点是能同时表达移动、普攻、技能、补刀、购买和升级等多分支行为，后续强化学习和行为克隆都围绕这个动作空间展开。

## 4. 主要模块

### 4.1 `train.py`

负责主循环：

- 启动 scrcpy 窗口。
- 截取当前画面。
- 用 `models/start.onnx` 判断对局是否开始。
- 调用 `DQNAgent.select_action()` 选择原始动作。
- 调用 `Environment.step()` 执行动作并计算奖励。
- 将每一步指标写入 `src/step_metrics.jsonl`。
- 后台线程进行 DQN replay 训练。

### 4.2 `wzry_env.py`

负责把模型动作转成最终执行动作：

1. `TargetTracker` 根据画面检测敌方目标并修正移动/技能方向。
2. `LaneMacroController` 处理开局走线、到线停下和早期对线宏。
3. `GamePhaseController` 根据视觉状态进入清线、换血、撤退、补刀等阶段。
4. `AndroidTool` 执行动作。
5. `GetRewordUtil` 计算奖励和诊断信息。
6. `traffic_probe` 记录每步流量指标。

### 4.3 `android_tool.py`

负责底层设备控制：

- 截图：scrcpy 窗口截图或 ADB screencap。
- 点击：装备、技能、普攻、补刀、恢复等按钮。
- 移动：支持 `swipe`、`motionevent`、`persistent_swipe`、`persistent_motionevent`、`persistent_minitouch`。
- 持续移动：通过保持摇杆按压，解决“一秒才点一次、点一下就松开”的问题。
- combat assist：在策略输出较弱时周期性补普攻、技能、恢复、购买，增加正常游戏交互密度。

当前默认摇杆中心为：

```text
x = 0.173
y = 0.784
```

### 4.4 `lane_macro.py`

负责开局和早期对线：

- 支持 `auto/profile/manual/autowzry_mid` 四种开局路线来源。
- `auto` 默认优先使用可信的 `src/lane_profile.json`。
- 对纯移动 profile，按真实玩家示范路线持续长按移动。
- 支持开局到线检测：看到兵线压力或敌方目标后释放摇杆。
- 到线后进入对线逻辑，执行补刀、清线、普攻、技能、保守站位等动作。

当前开局 profile 来源于 `build_lane_profile.py`，可以用真实触摸数据生成。

### 4.5 `phase_controller.py`

负责更高层的对局阶段判断：

- 根据中心区域红/蓝颜色占比估计 `center_pressure_score`，粗略表示兵线/战斗压力。
- 根据血条区域估计自身血量。
- 根据目标检测结果判断是否存在可见目标。
- 在早期对线阶段选择 `clear_wave`、`trade`、`poke`、`hold_lane`、`retreat` 等阶段动作。

注意：当前视觉检测是启发式方法，不是真正的目标分割模型，因此无法严格区分自家兵线、敌方兵线、自家塔、敌方塔和技能特效。

### 4.6 `target_tracker.py`

负责简单敌方目标跟踪：

- 通过红色血条/目标区域检测候选敌人。
- 输出目标是否可见、目标角度、距离比例。
- 在非开局锁定阶段，可将移动和方向性技能朝目标修正。

### 4.7 `human_data_collector.py`

负责采集真实玩家操作：

- 读取 Android `getevent` 触摸事件。
- 可选择只采触摸动作，不保存截图，用于轻量开局走线 profile。
- 也可保存截图和触摸标签，用于行为克隆训练。
- 记录触摸坐标变换、摇杆中心、设备分辨率等 metadata。

### 4.8 `build_lane_profile.py`

负责把真实玩家触摸示范转换为开局走线 profile：

- 从 `human_data/latest_action_episode.txt` 或指定 `samples.jsonl` 读取采集数据。
- 通过触摸点相对摇杆中心的位置反推移动角度。
- 将连续移动压缩为 `route_angles` 和 `route_steps`。
- 支持纯移动 profile，适合“只长按一个方向走到线上”的示范。

### 4.9 `train_behavior_clone.py`

负责行为克隆训练：

- 输入保存了截图和动作标签的 human_data。
- 训练 `NetDQN` 多分支动作输出。
- 可生成初始策略模型，用于替代纯随机/纯规则的初始策略。

### 4.10 `getReword.py`

负责奖励和指标：

- 画面变化奖励：鼓励动作后画面发生变化。
- 交互奖励：鼓励移动、普攻、技能、购买、升级等有效动作。
- 平滑移动奖励：减少方向剧烈抖动。
- 低效动作惩罚：动作执行后画面几乎不变时扣分。
- 目标/阶段奖励：根据目标检测和阶段控制结果给出辅助奖励。
- 流量/时延奖励：可接入每步网络流量和响应耗时，但当前权重默认较低或为 0。

## 5. 当前采用的方法

### 5.1 规则宏 + 强化学习混合

单纯强化学习在真实游戏环境里成本高、反馈稀疏、探索风险大。因此当前采用混合方法：

- 早期开局走线和对线用规则宏保证基础行为可用。
- DQN/智能先验提供原始动作。
- 目标跟踪和阶段控制对动作进行修正。
- 奖励函数持续记录画面变化、交互、流量、时延和动作质量，为后续训练提供信号。

### 5.2 真实玩家示范生成开局 profile

开局走线目前更适合从真实示范中学习：

1. 人工只长按摇杆走到线上。
2. `human_data_collector.py` 采集触摸轨迹。
3. `build_lane_profile.py` 生成 `src/lane_profile.json`。
4. 运行时 `LaneMacroController` 按 profile 复现长按走线。

### 5.3 行为克隆作为下一步策略初始化

行为克隆可行，但需要同时采集截图和触摸动作。它适合作为强化学习前的初始策略，而不是直接完全替代规则宏。推荐路线是：

```text
真实玩家截图+触摸采集
    ↓
行为克隆训练初始模型
    ↓
规则宏兜底运行
    ↓
强化学习/在线微调
```

### 5.4 流量与时延拨测

项目保留了 `traffic_probe.py`，通过 adb 读取 `/proc/net/dev` 类指标，记录每步收发字节、包数和耗时。当前自动化行为越接近正常玩家，后续基于这些日志分析网络体验才越有意义。

## 6. 已实现功能

- 连接 Android 设备并启动 scrcpy。
- 自动检测对局开始。
- 多分支动作空间：移动、普攻、补刀、推塔、技能、购买、升级。
- 持续摇杆长按移动。
- 开局走线 profile 采集、生成和回放。
- 到线/接触兵线或目标后释放摇杆。
- 低水平对线行为：清线、补刀、普攻、技能、保守站位、低血撤退。
- 目标可见时进行追踪和方向性技能修正。
- 行为克隆数据采集与训练脚本。
- DQN replay 训练框架。
- 每步日志记录：动作、阶段、奖励、流量、时延、视觉压力、目标信息等。

## 7. 常用命令

### 7.1 运行自动对局

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 260 --action_burst_duration_ms 900 --lane_arrival_debug true --lane_laning_debug true
```

### 7.2 采集纯开局走线

```powershell
python human_data_collector.py --iphone_id 10AD880X7Q001RX --human_action_only true --human_collect_fps 10 --human_collect_seconds 60 --human_collect_traffic false --touch_label_window_ms 80 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12 --human_episode_id lane_hold_demo
```

### 7.3 生成开局走线 profile

```powershell
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 140 --opening_steps 120 --opening_move_only_profile --opening_touch_route --opening_route_max_segments 1 --opening_min_move_ratio 0.65 --out src/lane_profile.json
```

### 7.4 测试固定开局角度

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 260 --action_burst_duration_ms 900 --lane_opening_route_source manual --lane_opening_route_angles 315 --lane_opening_route_steps 80 --lane_opening_contact_detect_after_steps 60 --lane_arrival_debug true
```

### 7.5 训练行为克隆模型

```powershell
python train_behavior_clone.py --bc_samples_path human_data/xxx/samples.jsonl --bc_model_out src/wzry_ai_bc.pt
```

## 8. 当前限制

- 视觉检测仍然是颜色和区域启发式，无法真正理解游戏物体。
- 开局路线高度依赖触摸坐标标定和采集质量。
- 目前不解析游戏网络包中的动作语义，主要依赖截图、触摸和 ADB 指标。
- 行为克隆需要高质量截图+触摸数据，否则模型会学到大量无效或偏置动作。
- 当前对线逻辑偏保守，能产生基础行为，但还不是稳定的人类水平策略。
- 不同手机分辨率、刘海屏、UI 布局和英雄技能位置变化会影响坐标配置。

## 9. 后续优化方向

- 标注并训练更稳定的兵线/敌方/塔检测模型，替代当前颜色压力检测。
- 增加开局走线的可视化校验工具，直接展示采集点、摇杆中心和反推角度。
- 采集更多真实玩家对线数据，训练行为克隆初始策略。
- 将规则宏输出作为行为先验，与强化学习策略融合。
- 加强日志分析脚本，把卡顿、延迟、流量波动和动作阶段关联起来。
- 对不同分辨率和设备建立坐标校准流程。

## 10. 本次打包说明

代码包建议包含：

- 核心 Python 源码。
- `requirements.txt`。
- `src/lane_profile.json`。
- 当前技术说明文档和已有说明文档。

不建议打包：

- `src/wzry_ai_bc.pt`：模型权重约 799MB。
- `src/step_metrics.jsonl`：运行日志。
- `human_data/`：采集数据量较大。
- `scrcpy-win64-v2.0/`：第三方工具，可单独安装或保留在本机。
- `__pycache__/`、`.idea/` 等缓存和 IDE 文件。
