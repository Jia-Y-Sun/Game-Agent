# 智能游戏行为策略改造说明

## 1. 改造目标

本轮改造把默认目标从“尽可能触发流量”调整为“尽量生成更接近正常对局的游戏行为”。流量、延迟、卡顿相关指标仍然保留在日志里，后续可以继续用于用户体验分析，但默认不再参与奖励函数，避免模型为了流量而学习到不自然的乱点行为。

当前默认行为更偏向：

1. 持续走位，而不是频繁站桩。
2. 高频普通攻击，穿插补兵、推塔和技能。
3. 定期购买装备和升级技能。
4. 方向变化更平滑，减少来回抖动。
5. 对局早期先形成可用的正常行为数据，再让 DQN 逐步学习。

请只在自定义房间、训练环境、测试账号或授权受控环境中运行，避免用于真实排位、匹配等影响他人的场景。

## 2. 核心变化

### 2.1 新增智能行为先验

新增文件：

```text
gameplay_policy.py
```

它实现了 `GameplayBehaviorPrior`，在探索阶段不再完全均匀随机采样动作，而是按“宏动作”生成更像玩家的动作序列：

| 宏动作 | 实际动作倾向 |
| --- | --- |
| `buy_equipment` | 点击购买装备 |
| `upgrade_skill` | 按 1、2、3 技能顺序升级 |
| `move_attack` | 边移动边普通攻击 |
| `last_hit` | 边移动边攻击小兵 |
| `push_tower` | 边移动边点塔 |
| `skill_1` / `skill_2` / `skill_3` | 边移动边释放技能，少量概率带方向参数 |
| `move_only` | 单纯移动 |
| `retreat` | 反向撤退，少量概率普攻或恢复 |
| `recover` | 原地恢复 |

这样做的作用是：即使 `epsilon=1.0`，早期采样也不是乱点，而是从一组相对合理的游戏行为模板里探索。

### 2.2 默认不再用流量奖励驱动训练

参数默认值已调整：

```text
--traffic_reward_weight 0.0
--traffic_inactive_penalty 0.0
--latency_reward_weight 0.0
```

也就是说，流量和 step 延迟仍然会记录到 `src/step_metrics.jsonl`，但默认不会影响训练奖励。

如果后续需要恢复“流量拨测”实验，可以手动打开：

```powershell
python train.py --traffic_reward_weight 0.8 --latency_reward_weight 0.2 --traffic_inactive_penalty 0.1
```

### 2.3 新增游戏行为奖励

`getReword.py` 中新增了 `gameplay_reward`，用于鼓励正常对局中更常见的动作组合：

| 行为 | 奖励方向 |
| --- | --- |
| 移动 + 普攻/补兵/推塔/技能 | 正向 |
| 普通攻击 | 正向 |
| 补兵、推塔 | 正向 |
| 使用 1/2/3 技能 | 正向 |
| 购买装备 | 小幅正向 |
| 升级技能 | 小幅正向 |
| 长按、频繁回城、无动作 | 负向或弱负向 |

总奖励现在是：

```text
total_reward = probe_reward + gameplay_reward + traffic_reward + event_reward
```

在默认参数下，`traffic_reward` 通常为 0，但日志仍然保留。

### 2.4 日志新增策略来源字段

`train.py` 写入 `src/step_metrics.jsonl` 时新增：

| 字段 | 含义 |
| --- | --- |
| `policy_source` | 当前动作来源，常见值为 `smart_prior`、`weighted`、`uniform`、`model` |
| `macro_action` | 智能先验产生的宏动作，例如 `move_attack`、`upgrade_skill` |
| `gameplay_reward` | 游戏行为奖励 |
| `gameplay_action_score` | 行为奖励的诊断分数 |

这些字段可以用来判断当前是否真的在执行“普攻为主、技能穿插、定期升级”的策略。

## 3. 如何运行实验

默认直接运行即可：

```powershell
python train.py
```

此时等价于：

```powershell
python train.py --behavior_prior_mode smart
```

如果要做对照实验，可以分别运行：

```powershell
python train.py --behavior_prior_mode uniform
python train.py --behavior_prior_mode weighted
python train.py --behavior_prior_mode smart
```

三种模式含义：

| 模式 | 说明 |
| --- | --- |
| `uniform` | 原始均匀随机探索，最容易乱点 |
| `weighted` | 带权重的动作探索，普攻和移动更高频 |
| `smart` | 当前默认，使用宏动作和状态化方向控制 |

建议先用 `smart` 跑一局，然后分析 `src/step_metrics.jsonl`。

## 4. 重点观察日志

实验后重点看这些字段：

| 字段 | 正常期望 |
| --- | --- |
| `policy_source` | 早期大多是 `smart_prior`，后期随着 epsilon 下降会逐渐出现 `model` |
| `macro_action` | 应该大量出现 `move_attack`，并穿插 `last_hit`、`skill_1`、`skill_2`、`skill_3` |
| `effective_action` | 普攻键 `attack_action=1` 应该明显变多 |
| `effective_action` | 升级技能应出现 `info_action=6/7/8` |
| `gameplay_reward` | 多数有效操作应为正 |
| `frame_change_score` | 动作后画面变化越明显越好 |
| `smooth_move` | 多数移动步应为 `true` 或方向变化不剧烈 |
| `traffic_activity_score` | 只作为观测数据，不再代表训练目标 |

如果你看到 `raw_action` 里有升级技能，但 `effective_action` 里没有，通常说明被环境的冷却或动作冲突规则过滤了。当前智能先验会把购买、升级做成独立动作，并把 `info_action_cooldown_steps` 默认降到 6，已经尽量减少这个问题。

## 5. 常用分析命令

统计宏动作分布：

```powershell
python -c "import json,collections; p='src/step_metrics.jsonl'; c=collections.Counter(); [c.update([json.loads(x).get('macro_action')]) for x in open(p,encoding='utf-8') if x.strip()]; print(c)"
```

统计实际执行的攻击动作：

```powershell
python -c "import json,collections; p='src/step_metrics.jsonl'; c=collections.Counter(); [c.update([json.loads(x).get('effective_action',[0,0,0,0])[3]]) for x in open(p,encoding='utf-8') if x.strip()]; print(c)"
```

统计实际执行的信息动作：

```powershell
python -c "import json,collections; p='src/step_metrics.jsonl'; c=collections.Counter(); [c.update([json.loads(x).get('effective_action',[0,0,0,0])[2]]) for x in open(p,encoding='utf-8') if x.strip()]; print(c)"
```

其中：

```text
attack_action=1  普通攻击
attack_action=2  攻击小兵
attack_action=3  攻击塔
attack_action=8  1技能
attack_action=9  2技能
attack_action=10 3技能

info_action=1/2  购买装备
info_action=6    升级1技能
info_action=7    升级2技能
info_action=8    升级3技能
```

## 6. 调参建议

如果普通攻击仍然太少：

```powershell
python train.py --behavior_prior_mode smart --epsilon_decay 0.998
```

或者在 `gameplay_policy.py` 中提高 `move_attack` 的概率。

如果升级技能仍然太少：

```powershell
python train.py --macro_upgrade_interval_steps 12 --info_action_cooldown_steps 5
```

如果走位还是太抖：

```powershell
python train.py --macro_direction_hold_steps 20
```

如果想更快让模型接管，而不是长期依赖智能先验：

```powershell
python train.py --epsilon_decay 0.99
```

如果想更长时间收集“像人”的先验探索数据：

```powershell
python train.py --epsilon_decay 0.999
```

## 7. 后续更智能的方向

当前改造属于“行为先验 + DQN 微调”的轻量版本，并已加入轻量目标跟踪攻击能力：

```text
截图 -> 红色/敌方血条候选检测 -> 估计目标方向 -> 修正移动/技能方向 -> 普攻连点
```

常用开关：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --target_tracking_enabled true --target_tracking_debug true
```

日志中可以查看：

```text
target_visible
target_angle
target_distance_ratio
target_refined_action
target_reward
```

这只是启发式检测，适合先验证“看到敌方后主动靠近并攻击”。如果要明显提升智能程度，建议按这个路线继续：

同时已新增“开局走线 + 前期对线”宏策略。默认在前期强制执行：

```text
开局买装/升级
按预设路线持续走到线上
到线后清兵、补刀、普攻、推塔、穿插技能
目标可见时朝目标移动并释放技能
```

常用参数：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --lane_macro_enabled true --lane_opening_route_angles 195,205,190,175 --lane_opening_route_steps 8,14,12,8
```

如果已经采集过真人开局数据，建议先生成走线 profile：

```powershell
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 160 --opening_steps 80 --out src/lane_profile.json
```

`train.py` 默认会读取 `src/lane_profile.json`，并优先复用其中的路线、升级、普攻、技能、补刀和推塔节奏。这样前期行为不再只是手写宏，而是从真实玩家样本里抽取出的可复用开局模板。

如果走错方向，优先调整：

```text
lane_opening_route_angles
lane_opening_route_steps
lane_laning_patrol_angles
```

日志中可以查看：

```text
lane_profile_loaded
lane_macro_phase
lane_macro_name
lane_macro_step
lane_macro_action
lane_opening_skill_start_step
lane_laning_skill_interval_steps
lane_laning_last_hit_interval_steps
lane_laning_push_tower_interval_steps
```

1. 录制正常对局数据，至少记录截图、动作、时间戳、胜负、经济、死亡、击杀等标签。
2. 先做行为克隆，让模型模仿正常玩家动作。
3. 再用强化学习微调，用生存、经济、推线、参团、胜负等指标设计奖励。
4. 将动作空间升级为 PPO 或 actor-critic，更适合多分支动作和连续方向参数。
5. 将日志中的流量、延迟、卡顿指标作为旁路观测，用于 QoE 分析，而不是直接驱动游戏行为。

现在这版的目标是先让采集行为从“能点”变成“看起来像在正常玩”，为后续真实对局数据训练和体验指标分析打底。

真实玩家数据采集和行为克隆的具体脚本、命令和排错方法见：

```text
doc/human_data_collection_cn.md
```

## 8. 开局之后的阶段控制器

现在新增 `phase_controller.py`，用于解决“走到线上之后还怎么继续打”的问题。动作链路变为：

```text
模型动作
  -> 开局/对线 lane macro
  -> 敌方目标跟踪 target tracker
  -> 对局阶段控制 phase controller
  -> 执行层 burst 控制
```

阶段控制器会根据当前画面和目标跟踪结果，在下面阶段之间切换：

```text
opening      开局走线，基本透传 lane macro
laning       对线清兵，补刀、普攻、穿插技能
chase        发现敌方但距离较远，朝目标移动并普攻
trade        目标较近，普攻和方向技能穿插
push_tower   周期性点推塔键，形成推塔压力
retreat      低血或危险时后撤，穿插恢复
recover      死亡/恢复后的保守移动或恢复
```

默认开启：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --phase_controller_enabled true
```

日志里重点看：

```text
phase
phase_action_name
phase_reason
phase_action
phase_reward
phase_own_hp_ratio
phase_low_hp
phase_center_pressure_score
phase_target_visible
phase_target_close
```

如果发现过早后撤，可以降低低血敏感度：

```powershell
python train.py --phase_low_hp_threshold 0.22
```

如果发现追人/打架太激进，可以拉远 trade 判定：

```powershell
python train.py --phase_trade_distance 0.24 --phase_trade_skill_interval_steps 7
```

如果发现一直不推塔，可以提前或加快推塔阶段：

```powershell
python train.py --phase_push_start_step 80 --phase_push_tower_interval_steps 8
```

这个阶段控制器不是完整游戏理解模型，只是把网上常见“状态 -> 合理动作 -> 反馈”的结构先落成一个可跑版本。后续真正要提升智能程度，应继续训练视觉状态模型，用标注数据识别小兵、塔、死亡、击杀、血量、兵线位置等事件。

## 9. 持续移动控制

王者荣耀的移动不是点击摇杆，而是按住摇杆并持续拖动。当前执行层已把移动默认改成：

```text
后台启动一次长按摇杆端点
同方向时跨多个决策步复用这次长按
方向变化时切换新的长按方向
不需要移动时释放
```

开局出泉水阶段现在默认是纯移动：

```text
lane_opening_move_only_steps = 28
这段时间禁止普攻、技能、升级、买装和自动补普攻
```

更推荐的方式是你亲自示范一次“只长按摇杆走到线上”，然后生成纯移动开局 profile：

```powershell
python human_data_collector.py --human_action_only true --human_collect_fps 10 --human_collect_seconds 45 --human_collect_traffic false --touch_label_window_ms 80 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 120 --opening_steps 70 --opening_move_only_profile --opening_route_max_segments 1 --out src/lane_profile.json
```

这个 profile 会把开局压成 1 个稳定主方向，例如：

```text
route_angles=[197]
route_steps=[61]
info_schedule=[]
attack_schedule=[]
```

运行时会把整条 opening 路线都当作 move-only，不会掺普攻和技能。

如果还没走出泉水或没到线上，把纯移动步数加大：

```powershell
python train.py --lane_opening_move_only_steps 45 --persistent_move_hold_ms 12000
```

可以先单独测试移动，不用跑完整训练：

```powershell
python test_move_control.py --angle 208 --seconds 5 --burst_ms 900 --move_control_mode persistent_swipe
```

常见方向可以试：

```text
180  向左
200  左下
220  更偏左下
0    向右
90   向下
270  向上
```

如果端点长按太远或太近，可以调摇杆半径：

```powershell
python test_move_control.py --angle 208 --seconds 5 --move_control_mode persistent_swipe --move_joystick_radius 320
```

如果后台长按和普攻/技能冲突，可以退回普通长滑动模式：

```powershell
python train.py --move_control_mode swipe --action_burst_duration_ms 800 --move_joystick_radius 300
```

如果长滑动仍不稳定，再退回重复短滑：

```powershell
python train.py --move_control_mode repeat_swipe --move_repeat_interval_ms 120
```

真正最接近真人多指操作的方案仍然是 `minitouch` 或 scrcpy control API；当前 ADB 方案是兼容性优先的近似实现。
