# 真实玩家数据采集与模仿学习说明

## 1. 现在新增了什么

本次新增了一条“真实玩家操作 -> 行为克隆 -> 强化学习微调”的链路：

```text
scrcpy/adb 截图
      +
adb getevent 触摸事件
      |
      v
human_data_collector.py
      |
      v
samples.jsonl
      |
      v
train_behavior_clone.py
      |
      v
src/wzry_ai_bc.pt
      |
      v
train.py 强化学习微调
```

对应新增文件：

| 文件 | 作用 |
| --- | --- |
| `touch_event_parser.py` | 解析 `adb shell getevent -lt`，把触摸点映射成当前项目的 8 维动作 |
| `human_data_collector.py` | 同步采集截图、触摸事件、流量指标，并生成训练样本 |
| `train_behavior_clone.py` | 用真实玩家样本做行为克隆预训练 |

## 2. 采集前准备

建议第一版固定实验条件：

1. 固定手机/模拟器分辨率。
2. 固定英雄和按键布局。
3. 优先用训练营、自定义房间或人机测试。
4. 玩家最好直接触摸手机操作；如果用 scrcpy 鼠标操作，也可以先试，但多指操作可能不如真机自然。

先确认设备：

```powershell
.\scrcpy-win64-v2.0\adb devices
```

再找触摸输入设备：

```powershell
.\scrcpy-win64-v2.0\adb -s 你的设备ID shell getevent -lp
```

在输出里找包含下面字段的 `/dev/input/eventX`：

```text
ABS_MT_POSITION_X
ABS_MT_POSITION_Y
ABS_MT_TRACKING_ID
```

也可以直接运行诊断脚本，它会列出候选触摸设备，并实时检测触摸事件：

```powershell
python touch_event_diagnose.py --device_id 你的设备ID --seconds 8
```

运行后请在手机屏幕上滑动摇杆、点击普攻和技能。如果输出 `探测结果: 0 行`，说明当前没有读到触摸事件，需要更换 `/dev/input/eventX`，或者确认你是在真机屏幕上触摸，而不是只在电脑投屏窗口里点鼠标。

## 3. 开始采集

现在采集分成两种目标：

```text
开局走线 profile：只需要触摸动作序列，不需要截图
行为克隆训练：需要截图 + 动作标签
```

如果只是为了学习“开局到线上”的路线和前期操作节奏，推荐用轻量动作采集模式：

```powershell
python human_data_collector.py --human_action_only true --human_collect_fps 10 --human_collect_seconds 90 --human_collect_traffic false --touch_label_window_ms 80 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12
```

这个模式不会保存截图，体积小很多，频率也可以更高。采完后直接生成走线 profile：

```powershell
python build_lane_profile.py --profile_step_sec 0.5 --max_steps 160 --opening_steps 80 --out src/lane_profile.json
```

轻量动作采集会更新：

```text
human_data/latest_action_episode.txt
```

`build_lane_profile.py` 默认优先读取这个文件。

这里的 `--profile_step_sec 0.5` 表示把高频触摸样本折算成“每 0.5 秒一个宏动作步”。因此即使采集时用了 `--human_collect_fps 10`，生成的走线 profile 也不会被拉得过长。

注意：`--human_action_only true` 采到的数据只能用于 `build_lane_profile.py` 这类动作节奏分析，不能直接用于 `train_behavior_clone.py`，因为行为克隆还需要图片帧作为输入。

如果要训练行为克隆模型，再使用截图采集。建议降低截图频率并压缩图片：

```powershell
python human_data_collector.py --human_capture_source adb_screencap --adb_screenshot_method remote_file --human_collect_fps 2 --human_collect_seconds 300 --human_frame_max_width 640 --human_jpeg_quality 75 --touch_device /dev/input/event6 --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12
```

带截图采集会更新：

```text
human_data/latest_frame_episode.txt
```

`train_behavior_clone.py` 默认优先读取这个文件。

最常用命令：

```powershell
python human_data_collector.py --human_collect_fps 5 --human_collect_seconds 300 --touch_device /dev/input/eventX
```

如果暂时不知道 `eventX`，也可以先不指定：

```powershell
python human_data_collector.py --human_collect_fps 5 --human_collect_seconds 300
```

但不指定时会监听所有输入事件，日志会更杂，动作解析也更容易受干扰。

采集结束时会打印触摸统计：

```text
触摸采集统计: raw_lines=..., parsed_samples=...
```

如果 `raw_lines=0`，说明不是截图问题，而是没有从 `getevent` 读到触摸事件；这时应先回到 `touch_event_diagnose.py` 排查触摸设备。

如果 `raw_lines>0`、`parsed_samples>0`，但 `parsed_action` 仍然几乎全是 0，说明触摸事件已经采到，但 raw 坐标没有正确映射到横屏游戏坐标。此时不用重新采截图，可以直接重标注：

```powershell
python relabel_human_actions.py --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12
```

如果输出里 `nonzero_actions` 明显大于 0，再覆盖原样本：

```powershell
python relabel_human_actions.py --touch_raw_width 12600 --touch_raw_height 28000 --touch_swap_xy --touch_invert_y --touch_hit_radius_ratio 0.12 --relabel_in_place
```

采集输出目录类似：

```text
human_data/
  latest_episode.txt
  human_20260531_153000/
    metadata.json
    samples.jsonl
    raw_getevent.jsonl
    touch_samples.jsonl
    frames/
      00000000.jpg
      00000001.jpg
```

其中最重要的是 `samples.jsonl`，每一行是一条训练样本：

```json
{
  "frame_path": ".../frames/00000012.jpg",
  "parsed_action": [1, 345, 0, 1, 0, 0, 0, 0],
  "touch_samples": [],
  "traffic": {}
}
```

## 4. 动作格式

采集器会把真实触摸映射成当前项目已经使用的动作格式：

```text
[move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]
```

常见映射：

| 玩家触摸 | parsed_action |
| --- | --- |
| 摇杆滑动 | `move_action=1, angle=方向角` |
| 普攻 | `attack_action=1` |
| 补兵 | `attack_action=2` |
| 推塔 | `attack_action=3` |
| 1技能 | `attack_action=8` |
| 2技能 | `attack_action=9` |
| 3技能 | `attack_action=10` |
| 购买装备 | `info_action=1/2` |
| 升级技能 | `info_action=6/7/8` |

## 5. 如果坐标方向不对

不同手机的 `getevent` 坐标可能和横屏游戏坐标不一致。如果发现 `parsed_action` 基本都是 0，或者明明点普攻却解析到别的位置，优先检查坐标变换参数。

常用参数：

```powershell
--touch_swap_xy
--touch_invert_x
--touch_invert_y
--touch_raw_width 1080
--touch_raw_height 2400
```

例如：

```powershell
python human_data_collector.py --touch_device /dev/input/eventX --touch_swap_xy --touch_invert_x
```

判断是否正确的最简单方法：采一小段 20-30 秒，然后看 `samples.jsonl` 里的 `parsed_action` 是否能出现普攻、技能、移动。

## 6. 行为克隆训练

采集完成后直接运行：

```powershell
python train_behavior_clone.py --bc_epochs 5 --bc_batch_size 16
```

默认会读取：

```text
human_data/latest_episode.txt 指向的 samples.jsonl
```

默认输出：

```text
src/wzry_ai_bc.pt
```

也可以手动指定样本：

```powershell
python train_behavior_clone.py --bc_samples_path human_data/human_某次采集/samples.jsonl --bc_model_out src/wzry_ai_bc.pt
```

训练时会打印：

```text
move_acc
info_acc
attack_acc
coarse_acc
```

其中 `coarse_acc` 只看移动、信息动作、攻击动作三类粗粒度分支，前期不用追求很高，先确认模型能学到普攻、技能和走位分布。

## 7. 接强化学习微调

行为克隆完成后，用这个模型作为初始策略继续跑：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart
```

当前 `train.py` 默认会把一次模型决策展开成一小段连续控制：

```text
移动动作 -> 在一个 burst 内重复摇杆滑动
普攻动作 -> 在一个 burst 内重复点普攻
技能动作 -> 先释放技能，再自动补普攻
购买/升级 -> 和普攻/移动穿插执行
```

如果实际动作仍然偏慢，可以适当缩短 burst 或提高重复频率：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --action_burst_duration_ms 420 --attack_repeat_interval_ms 130 --move_repeat_interval_ms 130
```

如果想回到旧的“一次决策只发一次动作”模式：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --behavior_prior_mode smart --action_burst_enabled false
```

也可以让模型更快接管，减少智能先验探索时间：

```powershell
python train.py --model_path src/wzry_ai_bc.pt --epsilon_decay 0.99
```

推荐流程是：

```text
先采集 5-10 分钟真实玩家数据
      |
行为克隆训练 src/wzry_ai_bc.pt
      |
自定义房间里强化学习微调
      |
看 step_metrics.jsonl 里普攻、技能、升级、走位是否更自然
```

## 8. 常见问题

### 8.1 `parsed_action` 几乎全是 0

优先检查：

1. 先看 `raw_getevent.jsonl` 是否为空。如果为空，说明没有采到触摸事件，不是截图时机问题。
2. 运行 `python touch_event_diagnose.py --device_id 你的设备ID --seconds 8`，确认实际哪个 `/dev/input/eventX` 有输出。
3. `--touch_device` 是否选错。
4. 坐标是否需要 `--touch_swap_xy`。
5. 横屏方向是否需要 `--touch_invert_x` 或 `--touch_invert_y`。
6. 玩家是否实际触摸手机，而不是只点了电脑窗口但没有进入设备侧 `getevent`。

### 8.2 动作能解析，但移动很少

可能是摇杆坐标或方向变换不对。先短采一段只滑摇杆的数据，看 `parsed_action[0]` 是否为 1，`parsed_action[1]` 是否随方向变化。

### 8.3 模型学出来总是不动

说明样本里无动作帧太多，或者触摸解析失败。先统计 `samples.jsonl` 里的 `parsed_action` 分布，确认真实动作标签足够多，再开始训练。

### 8.4 截图很慢

优先用 `--human_collect_fps 5`。如果仍然慢，可以改用：

```powershell
python human_data_collector.py --human_capture_source adb_screencap
```

不过当前项目训练时主要还是使用 scrcpy 窗口截图，所以默认保持 `scrcpy_window`。
