# 小地图视觉模块说明

这次加入了 `minimap_vision.py`，目的是替代单纯“固定走线 + 中心红蓝颜色压力”的粗判断，让脚本能参考左上角小地图和技能区上方的敌方头像提示。

## 新增识别内容

每一帧会输出一个 `minimap` 状态，主要字段如下：

- `minimap_self_visible`：是否识别到小地图上的己方绿色边缘圆圈。
- `minimap_self`：己方在小地图 ROI 内的位置比例。
- `minimap_lane_tower`：右路一塔目标位置，优先自动吸附到右侧蓝色塔候选。
- `minimap_lane_distance`：自己到一塔目标点的距离。
- `minimap_lane_arrived`：是否已经接近一塔。
- `minimap_lane_move_angle`：从当前自己位置朝一塔移动的角度。
- `minimap_enemy_visible`：小地图是否看到红色边缘敌方圆圈。
- `minimap_enemy_seek_angle`：没有可攻击目标时，短距离找敌人的移动角度。
- `attack_avatar_visible`：技能上方红色敌方头像是否出现。出现时表示普攻大概率能打到人。
- `dead_screen_dark`：屏幕是否进入死亡暗屏状态。
- `respawn_route_reset`：死亡暗屏结束后是否触发重新上线重置。

这些字段会写入 `src/step_metrics.jsonl`。

## 行为逻辑变化

### 1. 开局走线

开局不再只靠固定角度或固定步数。如果小地图能看到自己和一塔目标，会优先使用：

```text
minimap_lane_move_angle
```

作为持续长按摇杆方向。

当连续确认：

```text
minimap_lane_arrived = true
```

就认为走到一塔附近，释放摇杆并进入对线。

### 2. 对线平 A

如果技能上方出现红色敌方头像：

```text
attack_avatar_visible = true
```

阶段控制会优先进入 `trade`，通常会站住平 A 或穿插技能。

这比只看屏幕中心血条更贴近真实逻辑：有攻击头像说明普攻已经能锁到对方。

### 3. 没人可打时找人

如果没有兵线压力，也没有攻击头像，但小地图能看到敌方红圈，会每隔几步做一次短移动：

```text
phase = map_seek
phase_action_name = map_seek_enemy
```

移动方式仍然是摇杆中心向目标方向短暂长按，不是点一下地面。

### 4. 死亡和复活

如果画面明显变暗：

```text
dead_screen_dark = true
```

阶段控制进入恢复/等待。等屏幕恢复正常后，会重置开局走线状态：

```text
respawn_route_reset = true
```

之后重新按小地图一塔方向上线。

## 推荐运行命令

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --iphone_id 10AD880X7Q001RX --move_control_mode persistent_motionevent --move_joystick_radius 260 --action_burst_duration_ms 900 --lane_arrival_debug true --lane_laning_debug true --phase_debug true --minimap_debug true
```

如果日志刷太多，可以关掉调试：

```powershell
--minimap_debug false --phase_debug false
```

## 关键调参

如果小地图框偏了：

```powershell
--minimap_roi_left 0.045 --minimap_roi_top 0.0 --minimap_roi_right 0.205 --minimap_roi_bottom 0.31
```

如果一塔目标点偏了，可以手动指定：

```powershell
--minimap_lane_tower_auto_enabled false --minimap_lane_tower_x 0.96 --minimap_lane_tower_y 0.86
```

如果到线停得太远：

```powershell
--minimap_lane_arrival_distance 0.08
```

如果到线不停：

```powershell
--minimap_lane_arrival_distance 0.16
```

如果小地图找人太激进：

```powershell
--minimap_enemy_seek_enabled false
```

如果攻击头像误检：

```powershell
--minimap_attack_avatar_min_area 1200
```

## 当前限制

这是轻量 OpenCV 启发式模块，还不是训练好的检测模型。它依赖当前分辨率、UI 布局和颜色阈值。后续更稳的做法是：

1. 保存小地图截图。
2. 标注己方、敌方、防御塔和兵线。
3. 训练一个小目标检测或分割模型。
4. 用模型输出替代当前颜色/形状规则。
