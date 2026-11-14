## 低水平对线玩家规则

这版不是完整智能体，而是一个保守的对线意图层。目标是先让行为像一个会基本对线的低水平玩家，而不是随机点按。

### 意图顺序

1. 开局走线阶段仍由 `lane_macro` 控制，不普攻、不技能、不买装。
2. 低血量连续确认后进入 `retreat`，后撤并尝试恢复。
3. 敌方很近时进入 `trade`，站位换血，不盲目追。
4. 看到兵线压力时进入 `clear_wave`，优先清线和补刀。
5. 有兵线且压力足够时，低频 `push_tower`，但不主动向塔下走。
6. 敌方在中距离时进入 `poke`，用普攻/技能消耗，不追太深。
7. 没有兵线也没有目标时进入 `hold_lane`，停住守线。

### 关键参数

```powershell
--rookie_laner_enabled true
--rookie_laner_wave_pressure 0.14
--rookie_laner_push_pressure 0.34
--rookie_laner_trade_distance 0.30
--rookie_laner_poke_distance 0.46
--rookie_laner_chase_enabled false
--rookie_laner_poke_move_enabled false
--rookie_laner_no_wave_patrol_enabled false
```

默认不追远处目标，也不在没兵线时巡逻，原因是当前视觉状态不够可靠，过度移动容易走到塔下。

### 日志字段

看 `src/step_metrics.jsonl` 里的：

- `phase`: 当前阶段，比如 `clear_wave`、`poke`、`trade`、`hold_lane`、`retreat`。
- `phase_action_name`: 具体动作名。
- `phase_reason`: 为什么进入这个阶段。
- `phase_center_pressure_score`: 兵线/战斗视觉压力。
- `phase_target_visible`: 是否看到目标。
- `phase_target_distance_ratio`: 目标距离。
- `phase_no_wave_steps`: 连续没有兵线/目标的步数。

### 推荐命令

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 220 --action_burst_duration_ms 700 --lane_arrival_debug true --lane_laning_debug true --phase_debug true
```

如果它太保守，可以打开轻微移动：

```powershell
--rookie_laner_poke_move_enabled true
```

如果它还是站得太久，可以打开无兵线巡逻：

```powershell
--rookie_laner_no_wave_patrol_enabled true
```

如果它又开始冲塔，先关掉这两个开关。
