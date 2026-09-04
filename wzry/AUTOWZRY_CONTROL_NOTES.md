## autowzry 对局控制参考说明

这次参考的是 `E:\autowzry_repro` 中 autowzry 的对局内控制方式。它不是智能策略，进入对局后主要做三件事：

1. 用图片模板识别并缓存摇杆、普攻等按钮坐标。
2. 循环滑动摇杆、点击普攻，防止挂机。
3. 低频随机点击技能、恢复、买装等按钮，增加交互密度。

本项目没有照搬它的随机策略，只借用了工程层思路：

- 修正对局内按键坐标，尤其是左摇杆中心。
- 在 `android_tool.py` 增加 `combat_assist` 兜底交互层。
- 开局纯移动阶段仍尊重 `lane_macro_suppress_attack` 和 `lane_macro_suppress_info`，不会乱点普攻/技能/买装。
- 正常对局阶段，如果策略输出太弱，会补基础普攻、间隔技能、恢复和买装。

### 关键参数

```powershell
--combat_assist_enabled true
--combat_assist_attack_interval_ms 260
--combat_assist_skill_interval_ms 2400
--combat_assist_recover_interval_ms 16000
--combat_assist_buy_interval_ms 9000
--combat_assist_idle_move_enabled false
```

`combat_assist_idle_move_enabled` 默认关闭，原因是之前已经出现过持续走到塔下的问题。需要测试防发呆时可以临时打开：

```powershell
--combat_assist_idle_move_enabled true --combat_assist_idle_move_angle 315
```

### 推荐运行命令

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 220 --action_burst_duration_ms 700 --lane_arrival_debug true --lane_laning_debug true
```

如果摇杆仍然偏，可以只调半径：

```powershell
--move_joystick_radius 180
```

或者：

```powershell
--move_joystick_radius 260
```
