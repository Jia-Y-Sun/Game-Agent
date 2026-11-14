# 本次更新汇报

## 1. 更新目标

本次更新的核心目标是把原项目从“单纯强化学习自动操作”推进到“真实玩家数据采集 + 模仿学习预训练 + 强化学习微调”的路线。

之前的问题主要有：

1. 模型操作节奏慢，一秒左右才做一次动作。
2. 普攻、走位、技能之间缺少真实玩家那种连续穿插。
3. 纯强化学习奖励函数很难准确约束“正常游戏行为”。
4. 采集目标不应该只是增加点击次数，而是要更接近真实对局行为。

因此这次改造重点不是提高外挂式能力，而是建立一条可控环境下的真实行为数据采集和学习链路。

## 2. 主要更新内容

### 2.1 新增真实玩家数据采集链路

新增 `human_data_collector.py`，用于同步采集：

```text
游戏截图
真实玩家触摸事件
流量/网络指标
动作标签 parsed_action
```

采集后的样本格式为：

```text
当前画面 state -> 玩家真实动作 action
```

其中动作格式沿用当前项目的 8 维动作：

```text
[move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]
```

### 2.2 新增触摸事件解析

新增 `touch_event_parser.py`，解析 Android 的 `adb shell getevent -lt` 输出。

当前已确认真机触摸设备为：

```text
/dev/input/event6
```

并完成了坐标映射参数调试：

```text
--touch_raw_width 12600
--touch_raw_height 28000
--touch_swap_xy
--touch_invert_y
--touch_hit_radius_ratio 0.12
```

### 2.3 新增触摸诊断和重标注工具

新增 `touch_event_diagnose.py`，用于判断哪个 `/dev/input/eventX` 是真实触摸设备。

新增 `relabel_human_actions.py`，用于在不重新截图、不重新采集的情况下，重新用不同坐标参数生成动作标签。

这个工具解决了前期 `parsed_action` 全 0 的问题。

### 2.4 新增行为克隆训练

新增 `train_behavior_clone.py`，用于离线训练模仿模型。

训练时不需要连接手机，只读取已经采集好的：

```text
human_data/.../samples.jsonl
human_data/.../frames/*.jpg
```

输出模型：

```text
src/wzry_ai_bc.pt
```

这个模型后续可以作为强化学习的初始策略。

### 2.5 强化学习执行层优化

原来的 `train.py` 是一次模型决策只发一次动作，导致实际运行像：

```text
点一下普攻
等一秒
再走位
再等一秒
```

这不符合真实玩家操作。

本次新增了 burst 连续执行机制：

```text
移动动作 -> 短时间内重复摇杆滑动
普攻动作 -> 短时间内重复点普攻
技能动作 -> 先放技能，再自动补普攻
购买/升级 -> 可以和移动、普攻穿插执行
```

对应新增参数：

```text
--action_burst_duration_ms
--attack_repeat_interval_ms
--move_repeat_interval_ms
--autofire_basic_attack
--force_attack_while_moving
```

## 3. 当前实验进展

目前已经跑通了真实玩家数据采集链路。

一次测试采集结果：

```text
samples = 42
frames = 42
touch_frames = 42
nonzero_actions = 42
```

动作分布示例：

```text
移动：37 帧
普攻：35 帧
推塔：3 帧
补兵：2 帧
三技能：2 帧
升级1技能：3 帧
升级2技能：2 帧
升级3技能：1 帧
```

说明目前已经能够采集到：

```text
游戏画面 + 玩家真实操作标签
```

并且可以识别移动、普攻、补兵、推塔、技能、升级技能等动作。

## 4. 当前推荐实验流程

### 4.1 采集真实玩家数据

只为了重新学习开局走线时，用轻量动作采集，不保存截图：

```powershell
python human_data_collector.py --human_action_only true --human_collect_fps 10 --human_collect_seconds 90 --human_collect_traffic false --touch_label_window_ms 80 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12
```

这类数据默认写入 `human_data/latest_action_episode.txt`，供 `build_lane_profile.py` 读取。

如果要训练行为克隆模型，再采截图数据：

```powershell
python human_data_collector.py --iphone_id 10AD880X7Q001RX --human_capture_source adb_screencap --adb_screenshot_method remote_file --human_collect_fps 2 --human_collect_seconds 300 --human_frame_max_width 640 --human_jpeg_quality 75 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12
```

这类数据默认写入 `human_data/latest_frame_episode.txt`，供 `train_behavior_clone.py` 读取。

### 4.2 离线行为克隆训练

```powershell
python train_behavior_clone.py --bc_epochs 5 --bc_batch_size 8
```

### 4.3 从真人开局样本生成走线 profile

现在新增了 `build_lane_profile.py`，可以从最近一次真人采集的 `samples.jsonl` 中提取：

```text
开局走线角度
每段路线持续步数
前期升级/购买节奏
普攻、三技能、补刀、推塔节奏
```

生成命令：

```powershell
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 160 --opening_steps 80 --out src/lane_profile.json
```

运行 `train.py` 时默认会读取 `src/lane_profile.json`。如果 profile 加载成功，日志里的 `lane_profile_loaded` 会是 `true`。

### 4.4 实机强化学习微调

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX
```

如果动作仍然偏慢，可使用：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --action_burst_duration_ms 420 --attack_repeat_interval_ms 130 --move_repeat_interval_ms 130
```

## 5. 当前限制

1. 当前截图主要使用 ADB 截图，速度较慢，测试时大约 2 FPS 更稳定。
2. `adb input` 不是真正的多指连续控制，只能模拟短时间高频点击和滑动。
3. 数据量还比较少，42 帧只能证明链路跑通，正式训练至少需要几百到几千帧。
4. 如果要更像真实玩家，后续建议接入 `minitouch` 或 scrcpy control API，实现更稳定的多指并发控制。
5. 当前阶段控制器仍是轻量启发式：能让开局之后进入对线、交战、推塔、后撤等循环，但还不是完整的游戏状态理解模型。

## 6. 下一步计划

1. 正式采集 5-10 分钟真实玩家数据。
2. 用采集数据训练第一版行为克隆模型 `src/wzry_ai_bc.pt`。
3. 从真人开局样本生成 `src/lane_profile.json`，让自动体先按真实路线走到线上。
4. 使用 `phase_controller.py` 管理开局后的对线、交战、推塔、后撤阶段。
5. 将行为克隆模型接入 `train.py` 做强化学习微调。
6. 分析 `step_metrics.jsonl` 中普攻、走位、技能、升级动作、走线阶段、对局阶段的分布。
7. 优化执行层，将 ADB 控制升级为更接近真实多指操作的控制方式。

## 7. 总结

本次更新完成了从“纯强化学习探索”到“真实玩家数据驱动”的关键过渡。

现在项目已经具备：

```text
真实玩家数据采集
触摸动作解析
离线模仿学习
强化学习微调
连续动作执行
真人开局走线 profile
开局后阶段控制器
```

后续只要扩大真实玩家数据规模，就可以训练出比纯随机探索和人工奖励函数更稳定、更接近正常对局行为的初始策略模型。
