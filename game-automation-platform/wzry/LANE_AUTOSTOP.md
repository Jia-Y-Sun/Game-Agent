## 开局走线与到线停止说明

这版逻辑的目标是：开局先只做持续移动，尽量模仿真人从泉水走到线上；到线后释放摇杆，进入对线阶段；对线阶段再恢复普攻、补刀、清线、技能和换血。

### 当前流程

1. 默认使用 autowzry 风格的固定右上路线：`--lane_opening_route_source autowzry_mid`。
2. 如果要回放采集到的路线，可手动改为 `--lane_opening_route_source profile`。
3. 开局至少纯移动 `--lane_opening_min_move_steps` 步，默认 80 步。
4. 纯移动期间不普攻、不升级、不买装，避免刚出泉水就乱点。
5. 开局路线没跑完前默认不看视觉到线/危险信号：`--lane_opening_ignore_visual_before_base true`。
6. 跑完最小开局路线后默认直接释放摇杆：`--lane_opening_force_stop_at_base true`。
7. 如果关闭固定停止，才会从 `--lane_arrival_detect_after_steps` 开始允许视觉到线检测。
8. 低血量默认不再作为开局中断信号，因为早期 HP ROI 容易误报；需要时可用 `--lane_opening_danger_low_hp_enabled true` 打开。
9. 到线或固定路线完成后，先执行 `opening_arrived_stop` 空动作释放摇杆。
10. 随后进入 `laning`，恢复普攻、补刀、清线、技能、换血和撤退。
11. 对线阶段低血量撤退也需要连续 `--phase_low_hp_confirm_steps` 帧确认，默认 3 帧。

### 推荐命令

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 260 --action_burst_duration_ms 900 --lane_arrival_debug true --lane_laning_debug true
```

现在更建议先用：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 220 --action_burst_duration_ms 700 --lane_arrival_debug true --lane_laning_debug true --phase_debug true
```

如果终端或 `src/step_metrics.jsonl` 里仍然显示 `lane_opening_base_steps=150`，说明你跑的是旧进程，或者命令行里还传了旧参数。请先停掉旧的 `python train.py`，重新运行上面的命令。

### 关键日志字段

- `lane_macro_phase`: `opening` 表示还在开局走线，`laning` 表示已经进入对线。
- `lane_macro_name`: 当前宏动作名。
- `lane_opening_route_source`: 当前开局路线来源，默认 `autowzry_mid`。
- `lane_opening_route_angles`: 当前路线角度。
- `lane_opening_route_steps`: 当前路线步数。
- `lane_opening_base_steps`: 实际至少纯移动步数，默认 80。
- `lane_opening_max_steps`: 没检测到到线时的最大开局步数，默认 100。
- `lane_arrival_reason`: 到线或停止原因。
- `lane_arrival_candidate_steps`: 当前连续到线候选帧数。
- `lane_arrival_confirm_steps`: 需要多少帧才确认到线。
- `lane_opening_danger_candidate_steps`: 当前连续危险候选帧数。
- `lane_opening_danger_confirm_steps`: 需要多少帧才确认危险中断。
- `lane_laning_step`: 到线后的对线步数。
- `lane_laning_pressure_score`: 对线区域视觉压力。
- `lane_laning_target_visible`: 是否看见目标。
- `phase_low_hp_candidate_steps`: 当前连续低血量候选帧数。
- `phase_low_hp_confirm_steps`: 需要多少帧才触发撤退。

### 对线动作含义

- `laning_find_wave`: 没看到兵线/目标，默认停住等线，避免继续走进塔。
- `laning_wave_last_hit`: 看到兵线，补刀。
- `laning_wave_clear_skill`: 看到兵线，用技能清线。
- `laning_hold_attack`: 压力较高，站位普攻。
- `laning_trade_attack`: 目标较近，换血。
- `laning_kite_attack`: 目标过近或血量低，后拉普攻。
- `laning_push_tower`: 周期性推塔键。

### 调参建议

如果还是冲过头：

```powershell
--lane_opening_min_move_steps 65
```

如果停得太早，还没到线上：

```powershell
--lane_opening_min_move_steps 95
```

如果方向明显不对，优先试：

```powershell
--lane_opening_autowzry_angles 290,305,315
```

或：

```powershell
--lane_opening_autowzry_angles 315,325,335
```

不要再直接把 `lane_opening_min_move_steps` 拉到 150。现在摇杆已经是持续长按，150 步会明显过冲。

如果 `persistent_motionevent` 不生效，可以切到参考 WZCQ 思路的 minitouch 后端：

```powershell
pip install pyminitouch==0.3.3
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_minitouch --move_joystick_radius 260 --action_burst_duration_ms 900 --lane_arrival_debug true --lane_laning_debug true
```

单独测试摇杆：

```powershell
python test_move_control.py --angle 343 --seconds 8 -- --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 260
```
