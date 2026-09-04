# 开局长按走线采集与停线实验说明

目标：你手动示范一次“只长按摇杆走到线上”的过程，程序把这段路线压缩成开局 profile。之后运行时先复现这条长按路线，看到敌方英雄或小兵后立刻松开摇杆，进入对线逻辑。

## 这次代码改了什么

1. `human_data_collector.py` 会把当前摇杆中心写进 `metadata.json`。
2. `build_lane_profile.py` 不再使用旧摇杆中心 `(0.305, 0.796)`，默认改为执行层一致的 `(0.173, 0.784)`，并优先读取采集 metadata。
3. `lane_macro.py` 新增 `--lane_opening_route_source auto`，默认会优先使用可信的 `src/lane_profile.json`，否则回退到 `autowzry_mid`。
4. 对新采的纯移动 profile，不再用固定最小步数强行拉长，而是尊重示范里的 route step。
5. 开局移动期间新增接触停线：`--lane_opening_stop_on_contact true`，连续看到兵线压力或敌方目标后释放摇杆。

## 1. 重新采集长按走线

先进入对局开局泉水附近，准备好后运行：

```powershell
python human_data_collector.py --iphone_id 10AD880X7Q001RX --human_action_only true --human_collect_fps 10 --human_collect_seconds 60 --human_collect_traffic false --touch_label_window_ms 80 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12 --human_episode_id lane_hold_demo
```

采集时只做一件事：

1. 从泉水开始，长按摇杆的同一个目标位置。
2. 不普攻、不点技能、不买装备。
3. 看到第一波小兵或敌方英雄后松开。
4. 再按 `Ctrl+C` 结束采集。

采集结束后确认日志里不是全 0，并且有：

```text
raw_lines > 0
parsed_samples > 0
已更新 latest episode 指针
```

## 2. 生成走线 profile

```powershell
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 140 --opening_steps 120 --opening_move_only_profile --opening_touch_route --opening_route_max_segments 1 --opening_min_move_ratio 0.65 --out src/lane_profile.json
```

重点看输出：

```text
route_angles=[...]
route_steps=[...]
move_joystick_center={'x': 0.173, 'y': 0.784}
opening_move_ratio=...
```

如果 `opening_move_ratio < 0.65`，说明这次示范里有效移动比例太低，运行时默认不会信任这个 profile。通常是采集前等太久、走到线后停太久，或者触摸坐标变换参数不对。

## 3. 运行自动对局

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 260 --action_burst_duration_ms 900 --lane_arrival_debug true --lane_laning_debug true
```

正常情况下，早期日志里应该看到：

```text
lane_opening_route_source_requested=auto
lane_opening_route_source=profile
lane_opening_profile_route_trusted=True
lane_macro_name=opening_move_only
```

到线上或遇到兵线/敌方后，应该出现下面之一：

```text
lane_arrival_reason=opening_contact_wave
lane_arrival_reason=opening_contact_target
lane_arrival_reason=base_route_complete
```

其中 `opening_contact_wave` 或 `opening_contact_target` 是最理想的，说明它是在看到兵线或敌人后停下。`base_route_complete` 表示没有视觉接触，但示范路线走完后安全停下。

## 4. 常见问题

如果日志显示：

```text
lane_opening_route_source=autowzry_mid
```

说明 `src/lane_profile.json` 没被信任。重新看 `opening_move_ratio`，必要时重新采集，采集时尽量少等待，手指一按下就开始走线。

如果走到一半停太早：

```powershell
--lane_opening_contact_pressure_threshold 0.24 --lane_opening_contact_confirm_steps 3
```

如果看到兵线还不停：

```powershell
--lane_opening_contact_pressure_threshold 0.10 --lane_opening_contact_confirm_steps 1
```

如果摇杆方向仍然不对，优先检查 build 输出里的 `move_joystick_center` 是否是 `{'x': 0.173, 'y': 0.784}`，以及采集命令里的 `--touch_swap_xy --touch_invert_y` 是否还适合当前手机。
