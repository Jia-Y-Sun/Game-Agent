# 架构与实现说明

本文说明 `wzry_try` 的整体结构、每个模块的职责、动作空间、决策链、奖励构成和已知限制。
操作步骤见 [quickstart.md](quickstart.md)，训练方法见 [human-assist.md](human-assist.md)。

---

## 1. 五层结构

```text
            ┌─────────────────────────────────────────────┐
   L5 入口  │ train.py / bench_vram.py / analyze_episode.py│
            └─────────────────────────────────────────────┘
            ┌─────────────────────────────────────────────┐
   L4 决策  │ DQNAgent（DQN）· GameplayBehaviorPrior（先验）│
            │ GameProfilePrior（人为示范蒸馏的整场先验）    │
            └─────────────────────────────────────────────┘
            ┌─────────────────────────────────────────────┐
   L3 修正  │ LaneMacro（开局/对线）· GamePhaseController  │
            │ TargetTracker（目标）· LiveHumanAssist（接管）│
            └─────────────────────────────────────────────┘
            ┌─────────────────────────────────────────────┐
   L2 感知  │ 截图 · MinimapVision · TargetTracker · 流量  │
            │ GetRewordUtil（奖励 + 事件检测）              │
            └─────────────────────────────────────────────┘
            ┌─────────────────────────────────────────────┐
   L1 执行  │ AndroidTool：adb tap / swipe / motionevent   │
            └─────────────────────────────────────────────┘
```

一句话概括：**模型产出原始动作 → 规则层修正 → 人可能中途接管 → 落到 adb 执行 → 截图算奖励 → 回灌经验池**。

---

## 2. 动作空间

内部统一用 8 维动作：

```text
[move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]
```

| 维度 | 取值 | 含义 |
| --- | --- | --- |
| `move_action` | 0/1 | 是否推摇杆 |
| `angle` | 0–359 | 屏幕坐标系：0 向右、90 向下、180 向左、270 向上 |
| `info_action` | 0–8 | 买装 1/2、信号 3/4/5、升级技能 6/7/8 |
| `attack_action` | 0–10 | 普攻 1、补刀 2、推塔 3、回城 4、恢复 5、装备技能 6、召唤师 7、1/2/3 技能 8/9/10 |
| `action_type` | 0/1/2 | 点击 / 方向性滑动 / 长按 |
| `arg1` | 0–359 | 技能方向角 |
| `arg2` | 0–99 | 技能滑动距离 |
| `arg3` | 0–4 | 长按单位数 |

策略网络是 **8 个独立分类头**，总 Q 值取 8 个头之和（`dqnAgent._gather_action_q`）。
按钮坐标在 `argparses.py` 的 `move_actions_detail / info_actions_detail / attack_actions_detail` 里，全部是屏幕百分比。

---

## 3. 模块表

### 3.1 入口与工具

| 文件 | 职责 |
| --- | --- |
| `train.py` | 主循环：截图 → 判定开局 → 选动作 → `env.step` → 存经验；后台线程做 replay 训练；写指标 |
| `wzry_env.py` | 环境层：把模型动作依次过感知/规则/接管仲裁，再执行、观测、算奖励 |
| `argparses.py` | 全部命令行参数 + 按钮坐标表 + 设备/日志路径解析（无 torch 时也可导入） |
| `asset_paths.py` | 只读资源定位（`--asset_root`），输出路径固定在本项目内 |
| `device_agent.py` | 双端模式的**设备端**：把 `AndroidTool` 的能力（截屏/注入/触摸/流量）通过 TCP 暴露出去 |
| `remote_device.py` | 双端模式的**程序端替身**：`RemoteAndroidTool`、`RemoteTouchStream`、`RemoteProcNetDevTrafficProbe` |
| `remote_protocol.py` | 双端通信协议：长度前缀帧 + JSON 头 + 可选二进制负载（仅标准库） |
| `vram_manager.py` | 显存快照、AMP、缓存回收、经验池内存估算 |
| `analyze_episode.py` | 单局统计（奖励构成、探索度、动作分布、事件、时延） |
| `bench_vram.py` | 网络结构/分辨率/精度组合的显存与耗时基准 |
| `test_assist_pipeline.py` | 离线自检（数据、接管、profile、网络、训练一步、结束判定） |

### 3.2 感知与奖励

| 文件 | 职责 |
| --- | --- |
| `android_tool.py` | adb/scrcpy 执行层：截图、点击、摇杆（多种后端）、combat_assist 兜底 |
| `minimap_vision.py` | 小地图/攻击头像/暗屏检测，输出 `minimap_*` 状态 |
| `target_tracker.py` | 红色血条启发式找敌方目标，输出 `angle / distance_ratio` |
| `getReword.py` | 奖励计算 + 事件检测（攻击/死亡/胜负）+ 事件 ROI 落盘 |
| `traffic_probe.py` | 通过 adb 读设备网卡计数，得到每步收发字节/包数 |

### 3.3 决策与规则

| 文件 | 职责 |
| --- | --- |
| `dqnAgent.py` | DQN：选动作、replay、AMP、梯度裁剪、TD 目标裁剪、人为样本混合采样、辅助模仿损失 |
| `net_actor.py` | 策略网络（`compact` / `legacy` 两种结构，自适应池化） |
| `memory.py` | 经验池：uint8 小观测、人为/自主样本分池、混合采样 |
| `globalInfo.py` | 单例状态：对局状态、经验池读写、内存统计 |
| `gameplay_policy.py` | 冷启动用的宏观先验（走位+普攻+补刀+买装的概率组合） |
| `lane_macro.py` | 开局走线 + 早期对线宏（路线来源、到线判定、接触停线） |
| `phase_controller.py` | 阶段控制：清线/换血/消耗/守线/撤退/恢复/推塔/找团 |
| `game_profile.py` | 整场行为先验：按阶段回放人为示范的节奏 |
| `live_human_assist.py` | 人为接管：getevent 触摸流、动作还原、状态机、数据落盘 |
| `assist_dataset.py` | 整场示范数据集（JSONL schema v2、预卷帧、旧格式兼容） |

### 3.4 离线工具

| 文件 | 职责 |
| --- | --- |
| `human_data_collector.py` | 离线采集真人截图+触摸（老流程，仍可用） |
| `touch_event_parser.py` | getevent 解析 + 触摸→8 维动作映射 |
| `touch_event_diagnose.py` | 触摸设备诊断（列设备、看原始量程、实时探测） |
| `relabel_human_actions.py` | 用新的坐标变换重标注已采数据，不用重采 |
| `build_lane_profile.py` | 从离线示范生成开局走线 profile |
| `build_game_profile.py` | 从整场示范生成整场行为 profile |
| `train_behavior_clone.py` | 行为克隆训练（读整场示范数据集） |
| `showposition.py` | 截图点选，读出屏幕百分比坐标 |
| `test_move_control.py` | 单独测摇杆：验证中心/半径/方向 |

---

## 4. 一次 `env.step` 的完整决策链

```text
current_state（整屏截图）
   │
   ├─ MinimapVision.detect          小地图自身/敌方/一塔/攻击头像/暗屏
   ├─ TargetTracker.detect          红色血条 → 目标角度与距离
   │
   ├─ LiveHumanAssist.poll          人现在有没有按屏幕
   │
   ├─ 若人在操作：冻结规则层，learned_action = 人的动作，注入全 0
   │
   └─ 否则：
        LaneMacro.refine_action           开局走线 / 早期对线
          └ TargetTracker.refine_action    朝目标修正
             └ GamePhaseController        阶段动作
                 └ GameProfilePrior       按人为示范的阶段节奏补/改
                    └ _prepare_action     冷却、互斥、force_attack
                       └ LiveHumanAssist.arbitrate  最终仲裁
                          │
                          ├─ AndroidTool.execute_action_burst   真正注入 adb
                          ├─ 截图观测 → GetRewordUtil.get_reword  奖励 + 事件
                          ├─ 更新阶段/复活状态
                          └─ 写入示范数据集（asist_dataset）
```

关键设计：

* **规则层可以整体冻结**：人为接管时 `lane_macro` / `phase_controller` 的步数不推进，避免“人打了一波团，回来宏还以为在开局”。
* **宏只做兜底**：`phase_controller` 在 `model_action` 阶段会原样放过模型动作（日志里 `macro=model_action`）。
* **奖励与动作解耦**：奖励只看画面/目标/阶段/事件，不依赖动作来源，所以规则动作和模型动作可以用同一套经验。
* **设备层可替换**：`Environment` 只通过注入的 `android_controller` 碰设备，所以在它前面换一个网络替身，
  上面整条链路一行都不用改——这就是双端模式的实现方式。

### 4.1 双端模式：切在哪一刀

```text
设备端 device_agent.py                   程序端 train.py
  AndroidTool（真 adb/scrcpy）             RemoteAndroidTool（网络替身）
       ▲                                          │
       │  ┌───────────────────────────────────────┘
       │  │  hello / shot / act / stop / exec / procnet / touch_sub
       └──┴─  touch（异步推送）
```

* 切分点选在**设备 ↔ 其它**：`Environment` / `DQNAgent` / `GetRewordUtil` 全部留在程序端，
  因此视觉、奖励、经验池、训练都不受影响；
* **训练数据不过网**：程序端收到帧后自己算 `obs`、自己算奖励、自己存经验池，
  网上只有「一帧 jpeg」和「一个 8 维动作」；
* 触摸也走同一条连接（设备端把 `getevent` 原始行成批推过来，程序端用原有解析器处理），
  所以人为辅助在双端模式下同样可用；
* 设备端带看门狗：程序端断开或长时间无请求会自动 `stop_persistent_move()`。

操作步骤见 [training-modes.md](training-modes.md)。

---

## 5. 奖励构成

`getReword.get_reword()` 把六个分量相加：

| 分量 | 依据 | 默认作用 |
| --- | --- | --- |
| `probe_reward` | 画面变化量、是否交互、动作新颖度、走位平滑度 | 乘以 `--probe_reward_scale`（默认 0.4），避免“动一动就有分” |
| `gameplay_reward` | 动作本身是否合理（移动+攻击、普攻/技能/买装） | 权重 1 |
| `target_reward` | 移动/技能是否朝目标、距离是否够近 | 权重 1 |
| `phase_reward` | 当前阶段是否做了该做的事 | 权重 1 |
| `event_reward` | 攻击命中、回城、死亡、胜负 | 死亡 −2、胜负 ±5 |
| `traffic_reward` | 每步网络流量/时延 | 默认权重 0（需显式开启） |

写入经验池前还会再乘 `--reward_scale`（默认 0.1），把回报量级压到 Q 值稳定区间。

---

## 6. 网络结构与训练调度

### 6.1 两种结构

| | `legacy`（默认） | `compact`（可选） |
| --- | --- | --- |
| 参数 / `.pt` | 199,788,562 / 762MB | 1,099,922 / 4.2MB |
| 输入 | 640×640（`fc` 维度写死） | 160×160（自适应池化，尺寸自由） |
| 结构 | conv1 + conv2 + `Linear(778752→256)` + fc1 + 8 头 | conv1 + conv2 + conv3 + `AdaptiveAvgPool2d` + `Linear(2048→256)` + fc1 + 8 头 |
| 峰值显存 / 单步 | 5046MB（AMP）/ 430ms | 159MB / 2.4ms |
| 启用 | 默认 | `--net_arch compact --obs_size 160` |

`--amp` 不指定时自动：legacy 开、compact 关（两种结构的最优解相反，见 [vram.md](vram.md) §5）。

### 6.2 调度

| 机制 | 参数 | 说明 |
| --- | --- | --- |
| ε-贪婪 | `--epsilon / --epsilon_decay / --epsilon_min` | 默认每个**环境步**衰减一次（`--epsilon_decay_mode step`） |
| 训练节流 | `--replays_per_env_step`（默认 4） | 每个环境步最多训练 4 次，避免 20~30 次/步把经验反复采爆 |
| 训练时间预算 | `--train_time_budget_per_step_sec`（默认 0.8） | 每个环境步最多花 0.8s 训练，防 legacy 大网络饿死采集 |
| 目标网络 | `--target_update_mode step`、`--target_update 10` | 每 10 个环境步同步一次 |
| TD 目标裁剪 | `--td_target_clip`（默认 50） | 防 8 头相加后的 Q 自举发散 |
| 人为经验优先 | `--assist_human_sample_fraction / boost` | 每个批次至少 N% 来自人为示范 |
| 辅助模仿 | `--human_aux_bc_weight`（默认 0.5） | 在人为样本上加交叉熵，向人的动作靠拢 |

冷启动时 ε≈1，动作主要来自 `--behavior_prior_mode smart` 的宏观先验；
随 ε 衰减逐步交给模型。日志里 `src=smart_prior|model` 就是动作来源。

---

## 7. 规则层行为一览

### 7.1 开局与到线（`lane_macro.py`）

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--lane_opening_route_source` | `auto` | `auto` 优先可信 profile，否则用 autowzry 风格固定路线 |
| `--lane_opening_route_angles / steps` | `195,205,190,175` / `8,14,12,8` | 手动路线 |
| `--lane_opening_min_move_steps` | 150 | 至少纯移动多少步 |
| `--lane_opening_force_right_until_tower` | true | 到塔前一律向右推 |
| `--lane_opening_stop_on_contact` | false | 看到兵线/敌人是否提前松手 |
| `--lane_opening_contact_pressure_threshold` | 0.16 | 接触判定阈值 |
| `--lane_profile_replay_steps` | 60 | profile 里的信息/攻击表回放多少步 |

判定到线后会先空一步松摇杆（`lane_arrival_stop_steps`），再进入对线。

### 7.2 阶段控制（`phase_controller.py`）

| 阶段 | 触发 | 行为 |
| --- | --- | --- |
| `opening` | 宏仍在开局 | 原样放过 |
| `retreat` | 血量估计低于 `--phase_low_hp_threshold`，连续 `--phase_low_hp_confirm_steps` 帧 | 朝 `--phase_retreat_angle` 后撤，禁技能 |
| `recover` | 死亡/回城事件后 | 空放几步等复活 |
| `trade` | 目标距离 < `--phase_trade_distance` | 站住平 A + 周期技能 |
| `chase` | 目标可见但稍远 | 朝目标移动 + 技能 |
| `clear_wave` | `center_pressure_score` 高 | 补刀/技能清线 |
| `poke` | 中距离目标 | 消耗不追 |
| `hold_lane` | 无兵线无目标 | 停住守线 |
| `push_tower` | 步数够且有兵线压力 | 周期推塔键 |

`--rookie_laner_enabled true`（默认）时用更保守的意图层：不追远处目标、没兵线不巡逻，避免走到塔下。

### 7.3 小地图线索（`minimap_vision.py`）

| 字段 | 用途 |
| --- | --- |
| `minimap_self_visible / minimap_self` | 己方位置；用于开局转向和到线判定 |
| `minimap_lane_tower / lane_distance / lane_arrived / lane_move_angle` | 一塔方向与到线 |
| `minimap_enemy_visible / enemy_seek_angle` | 没目标时短距离找人 |
| `attack_avatar_visible` | 技能上方出现敌方头像 → 普攻大概率能打到 |
| `dead_screen_dark` | 死亡暗屏，进入恢复并重置走线 |

ROI 默认 `left 0.045 / top 0.0 / right 0.205 / bottom 0.31`；一塔默认 `(0.71, 0.93)`（`--minimap_lane_tower_auto_enabled` 可自动吸附）。

### 7.4 执行层兜底（`android_tool.py`）

* 摇杆后端：`persistent_motionevent`（默认）、`persistent_minitouch`、`persistent_swipe`、`center_swipe`、`swipe`；
  `input motionevent` 不被接受时会自动回退 `persistent_center_swipe`。
* `combat_assist`：策略输出太弱时周期性补普攻/技能/恢复/买装，避免长时间零交互。
* 到线/接管/结束等场景都会 `stop_persistent_move()`，确保不会“松手了摇杆还按着”。

---

## 8. 产出与日志

| 路径 | 内容 |
| --- | --- |
| `src/step_metrics.jsonl` | 每步一行：动作、阶段、奖励构成、流量、时延、视觉状态；超过 `--step_metrics_max_mb` 自动轮转 |
| `src/wzry_ai.pt` + `.meta.json` | 训练产出的模型（含 arch/obs/奖励尺度等元信息）；默认 legacy 约 762MB |
| `src/lane_profile.json` | 开局走线 profile（仓库自带一份默认值） |
| `src/game_profile.json` | 整场行为 profile（由 `build_game_profile.py` 生成） |
| `human_data/live/<时间戳>_assist/` | 人为辅助示范数据集（`assist_samples.jsonl` + `frames/`） |
| `human_data/latest_*.txt` | 最新一局的指针 |

---

## 9. 已知限制

1. **视觉是启发式**：颜色阈值 + 区域比例，不能真正分割兵线/塔/特效；换分辨率、换 UI 布局都要重标定。
2. **一局结束靠多重判据**：胜负 OCR + 网络流量健康度 + 步数上限；离线训练模式没有结算界面，主要靠流量判据。
3. **不解析网络包**：流量只作为“有没有在交互”的粗指标，不读动作语义。
4. **行为克隆依赖采集质量**：截图+触摸不同步、坐标变换配错都会学进偏置动作。
5. **对线偏保守**：会补刀/换血/撤退，但还不是稳定的人类水平。
6. **模拟器差异**：不同模拟器/手机的触摸注入路径不同，人为辅助能否采集取决于 `getevent` 是否可见。

后续方向：用小目标检测模型替掉颜色阈值；把规则宏的输出显式作为先验特征喂给网络；多分辨率自动标定。
