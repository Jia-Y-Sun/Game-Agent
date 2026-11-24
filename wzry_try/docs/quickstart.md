# 快速开始（雷电模拟器 / 真机）

从零到“看到日志在滚”的完整步骤。所有命令都在 Windows + 雷电模拟器（2400×1080）上实测通过。
原理见 [architecture.md](architecture.md)，训练方法见 [human-assist.md](human-assist.md)，出问题先看 [troubleshooting.md](troubleshooting.md)。

---

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 |
| Python | 3.10（推荐 conda 环境，名字随意，下文用 `wzry_ai`） |
| 显卡 | 有 CUDA 最好（显存 ≥ 4GB）；CPU 也能跑，只是慢 |
| 模拟器 | 雷电 9 / 14（Android 7+，需要支持 `input motionevent`）；也支持真机 |
| 游戏 | 王者荣耀，**先登录好**，能进训练营 / 人机 / 离线训练 |
| 磁盘 | 至少 2GB 余量（模型 + 截图 + 日志） |

---

## 2. 安装

### 2.1 建环境

```bat
conda create -n wzry_ai python=3.10 -y
conda activate wzry_ai
```

### 2.2 装依赖

```bat
cd /d D:\SRTP\wzry_try
pip install -r requirements.txt
```

GPU 版 PyTorch 与 ONNX Runtime 按自己的 CUDA 版本装（CPU 版也能跑）：

```bat
:: CUDA 11.8
pip install torch --index-url https://download.pytorch.org/whl/cu118
pip install onnxruntime-gpu
```

> 装完验证：`python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`

### 2.3 放两个大资源

仓库里**不包含**体积大或第三方的文件，需要自己放进来（都已写进 `.gitignore`）：

| 路径 | 来源 | 说明 |
| --- | --- | --- |
| `scrcpy-win64-v2.0/` | [scrcpy releases](https://github.com/Genymobile/scrcpy/releases) 解压 | 里面要有 `adb.exe` 与 `scrcpy.exe` |
| `models/start.onnx` | 上游模型发布页 <https://stack-traceable.top/archives/wzry-ai-model> 或社区分享 | 判断“对局开始” |
| `models/death.onnx` | 同上 | 判断“死亡” |

模型也可以放在项目外，用 `--asset_root D:\models` 指过去。

---

## 3. 连上模拟器

### 3.1 模拟器设置

| 项目 | 推荐 |
| --- | --- |
| 分辨率 | 横屏，16:9 或 20:9 都行（本文实测 2400×1080） |
| 自动旋转 | 关闭 |
| ADB 调试 | 打开（设置 → 其他设置） |
| 按键映射 | **关闭**（否则点击会被转成按键，AI 点不到按钮） |
| 帧率 | 60 FPS |

游戏内设置：

| 设置项 | 建议 | 原因 |
| --- | --- | --- |
| 操作设置 → **补刀推塔键** | **开启** | `attack_action=2/3` 点的就是这两个键 |
| 目标锁定 | 优先最近单位 | 视觉目标跟踪假设目标稳定 |
| 画质 / 特效 | 流畅、关特效 | 颜色阈值更稳，帧率更稳 |
| 自定义按键布局 | **不要改** | 所有坐标都是按默认布局量的 |

### 3.2 连接

```bat
.\scrcpy-win64-v2.0\adb.exe kill-server
.\scrcpy-win64-v2.0\adb.exe connect 127.0.0.1:5555
.\scrcpy-win64-v2.0\adb.exe devices
```

期望：

```text
List of devices attached
127.0.0.1:5555  device
```

雷电多开时第一个实例 5555，之后每个 +2（5557、5559…）。真机直接看 `adb devices` 里的序列号。

确认分辨率（后面标定要用）：

```bat
.\scrcpy-win64-v2.0\adb.exe -s 127.0.0.1:5555 shell wm size
```

---

## 4. 坐标标定（做一次，之后复用）

**为什么必须做**：仓库里的默认值是在 **2800×1260（20:9 真机）** 上标的。模拟器常见 16:9，
摇杆/按钮的屏幕比例会变；摇杆半径是**设备像素**，也要按短边换算。

### 4.1 摇杆中心和按钮

```bat
:: 抓一张游戏内截图（随便用 adb 或 scrcpy 都行）
.\scrcpy-win64-v2.0\adb.exe -s 127.0.0.1:5555 shell screencap -p /sdcard/s.png
.\scrcpy-win64-v2.0\adb.exe -s 127.0.0.1:5555 pull /sdcard/s.png images\screen.png

python showposition.py
```

在窗口里打开 `images\screen.png`，依次点击：

1. **左摇杆圆心** → 得到 `--move_joystick_center_x / _y`；
2. 右下角普攻、补刀、推塔、1/2/3 技能、回城、恢复 → 对照 `argparses.py` 的 `attack_actions_detail`；
3. 左侧商店/升级按钮 → 对照 `info_actions_detail`。

偏差超过 ±0.02 就改 `argparses.py`，改完保存，以后不用再传这些参数。

### 4.2 摇杆半径

```text
radius ≈ 0.17 × 屏幕短边

1080 短边 → 184      900 → 155      720 → 122
```

**不要照抄真机的 220~260**：在 900 高的模拟器上那是 0.24 倍短边，会推过死区。

### 4.3 验证方向

```bat
python test_move_control.py --angle 0 --seconds 3 --iphone_id 127.0.0.1:5555 --move_joystick_center_x 0.226 --move_joystick_center_y 0.800 --move_joystick_radius 184
```

人物应持续向右跑。判据（本机实测）：跑 `--angle 180` 后小地图自身坐标 x 变小，跑 `--angle 0` 后变大。

> 冒烟阶段看不到小地图坐标时，直接用肉眼确认方向即可：0=右、90=下、180=左、270=上。

### 4.4 触摸设备（只有“人为辅助”需要）

```bat
python touch_event_diagnose.py --device_id 127.0.0.1:5555 --seconds 10
```

一边用鼠标在模拟器窗口里按住摇杆拖动。输出会先列候选触摸设备，再打印原始事件：

```text
候选触摸设备：
  /dev/input/event4  "input"
    X range: 0..2399
    Y range: 0..1079
```

按量程填参数（**填交换前的 X/Y 范围**）：

```text
--touch_device /dev/input/event4 --touch_raw_width 2400 --touch_raw_height 1080
```

`--touch_swap_xy` 的判据：X 量程 ≈ 屏幕宽 → 不用换；X 量程 ≈ 屏幕高 → 需要换。
方向反了再加 `--touch_invert_x / --touch_invert_y`。

**如果探测结果是 0 行**：说明该模拟器把鼠标注入在 InputManager 层，`getevent` 看不到。
人为辅助在这台模拟器上无法采集，只能按 [human-assist.md](human-assist.md#7-采不到触摸时怎么办) 的替代方案做。

---

## 5. 启动

先进入**对局内**（训练营 / 人机 / 离线训练都行），能看到小地图和摇杆。

本节是**完全本地**模式（一台机器跑完）。想让「游戏留在本机、训练跑在另一台」，
见 [training-modes.md](training-modes.md) 的双端部分——只多两条命令。

### 5.1 全自动自学

```bat
python train.py --iphone_id 127.0.0.1:5555 --assist_mode off --move_joystick_center_x 0.226 --move_joystick_center_y 0.800 --move_joystick_radius 184 --step_info_log false --step_timing_debug false --action_preview_enabled false
```

### 5.2 人为辅助（随时接管）

```bat
python train.py --iphone_id 127.0.0.1:5555 --assist_mode assist --touch_device /dev/input/event4 --touch_raw_width 2400 --touch_raw_height 1080 --move_joystick_center_x 0.226 --move_joystick_center_y 0.800 --move_joystick_radius 184 --assist_preempt_burst_duration_ms 260 --step_info_log false --step_timing_debug false --action_preview_enabled false
```

### 5.3 整场示范采集（AI 不插手）

```bat
python train.py --iphone_id 127.0.0.1:5555 --assist_mode manual --assist_frame_policy all --touch_device /dev/input/event4 --touch_raw_width 2400 --touch_raw_height 1080 --assist_episode_id demo_01 --step_metrics_enabled false
```

> 三条命令都是一整行，别拆。`^` 续行在 cmd 与 PowerShell 里语法不同，单行最省事。

### 5.4 想固定参数就别每次敲

在 `argparses.py` 里把 `move_joystick_center_x/y`、`move_joystick_radius` 的 `default` 改成自己的值，
之后命令可以直接简化为 `python train.py --iphone_id 127.0.0.1:5555`。

---

## 6. 看到这些就算启动成功

```text
[net] 未指定 --amp，按结构自动设为 True
[net] arch=legacy obs=640 params=199,788,562 (762.13MB fp32)
[dqn] 未找到初始模型 ...\src\wzry_ai.pt，从随机初始化开始     # 有模型时是「模型已加载（严格匹配）」
[vram] device={'device': 'cuda:0', 'cuda': True, ...}
[startup] net=legacy obs=640 amp=True
[startup] params=199,788,562 weights~762.1MB(.pt) memory=3495 条 ≈ 8.0GB RAM batch=64
[startup] assist_mode=off record=True dir=...\human_data/live
-------------------------------对局开始-----------------------------------
[train] epsilon_decay_mode=step target_update_mode=step replays_per_env_step=4.0 train_time_budget=0.8s reward_scale=0.1 td_target_clip=50.0 probe_reward_scale=0.4 episode_end=on max_episode_steps=3600
step=0    phase=opening  macro=opening_right_until_tower  action=(1, 7, 0, 0, 0, 0, 0, 0) reward=+0.361 eps=1.000 src=smart_prior assist=idle  human=n
step=1    phase=opening  macro=opening_right_until_tower  action=(1, 7, 0, 0, 0, 0, 0, 0) reward=+0.860 eps=0.995 src=smart_prior assist=idle  human=n
training replay=20 loss=0.8142 aux=0.0000 q=0.62 target=0.62 human_in_batch=0/64 eps=0.96 arch=legacy memory=20/3495 human=0 ram=48.0MB
```

启动时还会有两行提示（`legacy` + 640 观测的必然结果）：

```text
[memory] obs=640 时每条经验约 2.34MB，经验池 10000 条需要约 22.9GB 内存。
[memory] 按 --memory_ram_limit_gb 8 自动下调到 3495 条（约 8.0GB）。想保留更大经验池请调大该上限。
```

> 首次启动会构建 1.99 亿参数的 `legacy` 网络，比小网络慢几秒、显存约 5GB（AMP 已自动开启）。
> 想省显存就加 `--net_arch compact --obs_size 160`；想保留更大的经验池就调大 `--memory_ram_limit_gb`。

字段含义：

| 字段 | 含义 |
| --- | --- |
| `step` | 本局第几步 |
| `phase` | 当前阶段（opening / laning / combat / retreat / clear_wave …） |
| `macro` | 当前宏动作名，`model_action` 表示规则层放手、由模型决定 |
| `action` | 真正执行的动作 `(移动, 角度, 信息, 攻击, 类型, arg1, arg2, arg3)` |
| `reward` | 本步奖励（未缩放的原始值） |
| `eps` | 当前 ε（探索率） |
| `src` | 动作来源：`smart_prior`（先验探索）/ `model`（模型） |
| `assist` / `human` | 人为接管状态 / 这一步有没有人的动作标签 |

**`step=` 在持续滚动 + 偶尔出现 `training` 行 = 启动成功。**

---

## 7. 停止与产物

`Ctrl+C` 停止。模型每 `--save_model_every_replays`（默认 200）次训练迭代自动保存到 `src/wzry_ai.pt`，
`Ctrl+C` 本身不会再额外保存。

| 产物 | 位置 |
| --- | --- |
| 模型 | `src/wzry_ai.pt` + `src/wzry_ai.pt.meta.json` |
| 逐步指标 | `src/step_metrics.jsonl`（超 `--step_metrics_max_mb` 自动轮转） |
| 人为辅助示范 | `human_data/live/<时间戳>_assist/` |
| 单局统计 | `python analyze_episode.py`（终端输出，可 `--json` 存文件） |

---

## 8. 下一步

* 想把 AI 教得更像人：读 [human-assist.md](human-assist.md)（接管、示范采集、蒸馏、行为克隆）。
* 想知道显存怎么省的、参数怎么调：读 [vram.md](vram.md)。
* 跑不起来 / 行为怪异：查 [troubleshooting.md](troubleshooting.md)。
