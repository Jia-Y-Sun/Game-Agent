# 人为辅助与模仿训练

`wzry_try` 的核心能力之一：**人可以在整局任意时刻接管，接管产生的数据会变成训练信号**。
本文说明三种模式、数据格式、以及从“人示范”到“模型学会”的完整链路。
操作步骤（含触摸设备标定）见 [quickstart.md](quickstart.md)。

---

## 1. 三种模式

| 模式 | 参数 | 谁在操作 | 用途 |
| --- | --- | --- | --- |
| `off` | `--assist_mode off` | 只有 AI | 全自动自学 |
| `assist` | `--assist_mode assist` | AI 为主，人可随时接管 | 冷启动、纠正坏习惯 |
| `manual` | `--assist_mode manual` | 只有人，AI 只观测不注入 | 录一整场高质量示范 |

### 1.1 `assist` 的接管状态机

```text
            手指按下
   IDLE ────────────────▶ HUMAN
     ▲                      │ 松手
     │  resume_grace_steps  ▼
     └────────────────── RELEASING ──(release_grace_steps 步)──┘
```

* **HUMAN**：`stop_persistent_move()` 松开程序自己的摇杆，注入动作全 0，
  连 `combat_assist` 的兜底普攻也停掉——避免和人抢操作。
* **RELEASING**：松手后仍保持 `--assist_release_grace_steps`（默认 2）步不注入。
* 回到 **IDLE** 前再空 `--assist_resume_grace_steps`（默认 1）步。
* 接管期间 `lane_macro` / `phase_controller` 的步数与内部状态**全部冻结**。

接管生效时日志长这样：

```text
step=93  phase=retreat  macro=human_assist_hold  action=(1, 1, 0, 0, 0, 0, 0, 0)  reward=+0.445  eps=0.412  src=model  assist=human  human=Y
```

`assist=human` 表示这一步归人，`human=Y` 表示这一步有人的动作标签。

### 1.2 只接管一部分：`--assist_partial true`

只接管人真正碰到的分支：只推摇杆就只接管移动，普攻/技能仍由 AI 补。
注意它需要**多指同时按下**，用鼠标在模拟器上通常做不到，主要适合真机/触屏设备。

### 1.3 接管后不想还给 AI

```bat
--assist_auto_resume false
```

第一次接管之后本局不再交还（等价于“我先打，后面你别插手”）。

---

## 2. 先决条件：`getevent` 能读到触摸

人为动作来自 `adb shell getevent`。请先按 [quickstart.md](quickstart.md) 第 4.4 节确认：

* 能读到 `EV_ABS ABS_MT_POSITION_*` → 可以直接用；
* 读不到（探测 0 行）→ 走本文第 7 节的替代方案。

> 程序注入（`adb shell input`、scrcpy 鼠标）走 InputManager，**不会**回灌 `getevent`，
> 所以 AI 自己的操作不会被误判成人为操作。只有 `persistent_minitouch` 这类 uinput 方案才需要
> `--assist_echo_guard 120`。

---

## 3. 数据落盘

### 3.1 目录

每局一个目录，程序启动对局时自动创建：

```text
human_data/
├── latest_episode.txt            # 最新一局指针（build_game_profile / BC 默认读它）
├── latest_action_episode.txt
├── latest_frame_episode.txt
└── live/
    └── 20261109_183254_assist/
        ├── metadata.json         # 设备、坐标变换、帧策略、obs 尺寸
        ├── assist_samples.jsonl  # 一步一行
        └── frames/00000042.jpg   # 关键帧（策略允许时才有）
```

### 3.2 一行记录

```json
{
  "schema": 2, "episode_id": "20261109_183254_assist", "step": 42, "ts": 1699999999.1,
  "act":  [1, 175, 0, 8, 1, 175, 40, 0],
  "ai_act": [1, 200, 0, 1, 0, 0, 0, 0],
  "human_act": [1, 175, 0, 8, 1, 175, 40, 0],
  "human_present": true, "human_override": true, "assist_mode": "assist",
  "phase": "combat", "lane_phase": "laning",
  "macro": "laning_trade_attack", "phase_action_name": "trade_target",
  "reward": 1.34, "done": 0, "event": null,
  "obs": {"hp": 0.62, "pressure": 0.41, "target_dist": 0.22,
          "target_visible": true, "mm_lane_arrived": true, "dead": false},
  "frame_path": ".../frames/00000042.jpg", "frame_saved": true
}
```

* `act`：游戏里真实发生的动作；`ai_act`：AI 当时想做的；`human_act`：人做的（没接管时为 `null`）。
* **`human_act` 为 `null` 就表示“这一步是 AI 打的”**，所以一个数据集里天然混着示范和探索，不用删。
* `obs` 只留几个关键标量（血量/压力/目标距离/小地图状态），足够统计，不撑大文件。

### 3.3 帧策略与预卷

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--assist_frame_policy` | `human` | `human` 只存接管相关帧；`all` 每步都存（行为克隆要用）；`none` 不存图（只做 profile） |
| `--assist_frame_max_width` | 640 | 帧的最大宽度 |
| `--assist_jpeg_quality` | 78 | JPEG 质量 |
| `--assist_preroll_steps` | 4 | 接管前额外保留几帧的上下文 |

实现方式是“延迟落盘”：某一步之后（含自身）出现过人为操作才写帧，所以既有接管前的上下文，
又不会全程存图。

### 3.4 触摸→动作的映射

| 玩家触摸 | 解析出的 8 维动作 |
| --- | --- |
| 摇杆滑动 | `move_action=1, angle=方向角` |
| 普攻 / 补刀 / 推塔 | `attack_action=1 / 2 / 3` |
| 1/2/3 技能 | `attack_action=8 / 9 / 10`（带方向滑动时 `action_type=1`） |
| 回城 / 恢复 | `attack_action=4 / 5` |
| 购买装备 / 升级技能 | `info_action=1,2 / 6,7,8` |

摇杆中心取自 `--move_joystick_center_x/y`（与执行层共用同一个标定值）；
按钮命中半径是 `--touch_hit_radius_ratio`，死区是 `--touch_move_deadzone_ratio`。

---

## 4. 用示范训练

### 4.1 行为克隆（监督学习）

```bat
python train_behavior_clone.py --bc_epochs 5 --bc_batch_size 32
```

默认读 `human_data/latest_frame_episode.txt` 指向的那一局；也可以显式指定、并合并多局：

```bat
python train_behavior_clone.py --bc_samples_path "human_data/live/局1,human_data/live/局2" --bc_epochs 8
```

* 默认**只学人操作的步**；`--bc_include_ai_steps true` 会把 AI 的步也加进来（样本更多但质量参差）。
* 输出 `src/wzry_ai_bc.pt`，之后用 `--model_path src/wzry_ai_bc.pt` 继续强化学习。
* 打印 `move_acc / angle_acc / info_acc / attack_acc / coarse_acc`，先看 `coarse_acc` 是否上升。

### 4.2 强化学习里直接用人为经验

不用额外开关，`train.py` 已经做了两件事：

1. **优先采样**：`--assist_human_sample_fraction`（默认 0.35）保证每批至少 35% 来自人为操作过的经验；
2. **辅助模仿损失**：`--human_aux_bc_weight`（默认 0.5）在被纠正过的状态上再算一个交叉熵，
   和 TD loss 一起优化。

日志里能看到：

```text
training replay=840 loss=4.0824 aux=0.6451 q=... human_in_batch=22/64 eps=0.12 arch=legacy memory=120/3495 human=27 ram=282.0MB
```

* `human_in_batch`：这批里有多少条来自人为示范；
* `aux`：辅助模仿损失；
* `human` / `ram`：经验池里人为样本数与常驻内存。

想加重人为影响：

```bat
--assist_human_sample_fraction 0.5 --assist_human_sample_boost 2.0 --human_aux_bc_weight 1.0
```

想让模型自己多探索（人为经验只做冷启动）：

```bat
--assist_human_sample_fraction 0.15 --human_aux_bc_weight 0.2
```

### 4.3 整场行为 profile（把人的节奏蒸馏成先验）

```bat
python build_game_profile.py --assist_source human_data --out src/game_profile.json
```

它会按阶段（opening / laning / combat / retreat / recover / seek）统计走位角度分布、
普攻/技能/补刀/推塔节奏、买装升级节奏，输出 `src/game_profile.json`。
这个文件同时兼容 `lane_profile.json` 的字段，所以也能直接给 `lane_macro` 用。

启用方式：

```bat
python train.py --game_profile_prior_mode fill
```

| 值 | 行为 |
| --- | --- |
| `off` | 不启用（默认） |
| `fill` | 只补空分支：AI 这一步什么都不做时，按人的阶段节奏补走位/普攻/升级 |
| `force` | 按 profile 生成完整动作，规则层只做安全兜底 |

样本不足的阶段会自动回退到“阶段组 → 全局”统计（阈值 `--game_profile_min_steps`）。

---

## 5. 只做开局走线（旧流程，仍然可用）

如果只想固定“从泉水走到线上”这一段，不必用整场模式：

```bat
:: ① 采集（只记触摸，不存图，体积小）
python human_data_collector.py --iphone_id 127.0.0.1:5555 --human_action_only true ^
  --human_collect_fps 10 --human_collect_seconds 60 --human_collect_traffic false ^
  --touch_label_window_ms 80 --touch_device /dev/input/event4 ^
  --touch_raw_width 2400 --touch_raw_height 1080 --human_episode_id lane_hold_demo

:: ② 生成 profile
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 140 --opening_steps 120 ^
  --opening_move_only_profile --opening_touch_route --opening_route_max_segments 1 ^
  --opening_min_move_ratio 0.65 --out src/lane_profile.json
```

采集时只做一件事：从泉水开始长按摇杆同一方向，看到兵线或敌人就松手，然后 `Ctrl+C`。

生成后看输出里的 `opening_move_ratio`：低于 `--lane_opening_profile_min_move_ratio`（默认 0.65）
说明这次示范里有效移动太少，运行时默认不会信任它。运行日志里对应：

```text
lane_opening_route_source=profile             # 用了你的 profile
lane_opening_route_source=autowzry_mid        # 没有可信 profile，回退固定路线
```

---

## 6. 参数速查

### 接管行为

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--assist_mode` | `off` | `off` / `assist` / `manual` |
| `--assist_partial` | false | 只接管人碰到的分支 |
| `--assist_auto_resume` | true | 松手后是否还给 AI |
| `--assist_touch_window_ms` | 400 | 多长触摸窗口折算成一步 |
| `--assist_release_grace_steps` | 2 | 松手后保持不注入的步数 |
| `--assist_resume_grace_steps` | 1 | 交还 AI 前的空步数 |
| `--assist_preempt_burst_duration_ms` | 0 | 压缩单步 burst 以降低接管延迟（0=不改） |
| `--assist_echo_guard` | 0 | 回声保护窗口（ms），minitouch 方案才需要 |

### 数据与训练

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--assist_record_enabled` | true | 是否写数据集 |
| `--assist_dataset_dir` | `human_data/live` | 数据集根目录 |
| `--assist_episode_id` | 空 | 本局 id，空=时间戳；重名会自动加 `_1`、`_2` |
| `--assist_human_sample_fraction` | 0.35 | 每批人为经验最低占比 |
| `--human_aux_bc_weight` | 0.5 | 辅助模仿损失权重 |
| `--assist_human_reward_bonus` | 0.0 | 接管这一步的额外奖励（0=不改奖励） |
| `--game_profile_prior_mode` | off | `off` / `fill` / `force` |
| `--bc_include_ai_steps` | false | 行为克隆是否也学 AI 的步 |

---

## 7. 采不到触摸时怎么办

如果按 quickstart 4.4 探测是 0 行，说明这台模拟器不把鼠标注入写进 `getevent`。三个替代：

1. **真机采示范 + 模拟器跑训练**（推荐）
   真机上走本节流程，把 `human_data/live/<某局>/` 整个拷到模拟器这边，训练侧完全一致：

   ```bat
   python build_game_profile.py --assist_source human_data/live --out src/game_profile.json
   python train_behavior_clone.py --bc_epochs 5
   python train.py --model_path src/wzry_ai_bc.pt --game_profile_prior_mode fill
   ```

   动作角度和屏幕比例与分辨率无关，真机示范可以直接用；只有摇杆半径等执行层参数按模拟器改。

2. **退化成离线模仿**：录屏 + 人工标注，工作量大、精度低，不推荐。

3. **换一个模拟器**：触摸注入是否走 evdev 取决于模拟器实现，换之前先跑一遍
   `touch_event_diagnose.py`，能读到 `EV_ABS` 才算可用。

另外两个常见问题的解法：

* **`parsed_action` 几乎全是 0**：坐标变换没配对。先确认 `raw_getevent.jsonl` 非空，
  再调 `--touch_swap_xy / --touch_invert_x / --touch_invert_y`。
* **能采到但方向不对**：不用重采，用新参数重标注即可：

  ```bat
  python relabel_human_actions.py --relabel_episode_dir human_data/live/某一局 ^
    --touch_raw_width 2400 --touch_raw_height 1080 --touch_hit_radius_ratio 0.06 --relabel_in_place
  ```

---

## 8. 推荐工作流

```text
第 1 轮｜冷启动
  train.py --assist_mode assist（人只纠正明显错误，20~30 分钟）
    ↓ build_game_profile.py → src/game_profile.json
    ↓ train_behavior_clone.py → src/wzry_ai_bc.pt
第 2 轮｜带先验训练
  train.py --model_path src/wzry_ai_bc.pt --assist_mode assist --game_profile_prior_mode fill
    ↓ 观察 step_metrics.jsonl 里 assist_human_steps 是否下降
第 3 轮｜只做兜底
  train.py --assist_mode assist --assist_partial true（或直接 off 全自动）
```

判断有没有进步，盯一个数就够：**同样时长里人接管的步数是否下降**。
也可以用 `python analyze_episode.py` 看动作熵、阶段覆盖和奖励构成的变化。
