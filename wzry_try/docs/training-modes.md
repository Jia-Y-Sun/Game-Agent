# 两种运行模式：完全本地 / 双端

本项目支持两种跑法，**同一份代码，只差启动参数**：

| | 完全本地（默认） | 双端（设备本地 + 训练远端） |
| --- | --- | --- |
| 游戏、模拟器、adb | 本机 | 本机 |
| 截图 + 注入动作 | 本机 | 本机（`device_agent.py`） |
| 视觉 / 策略 / 奖励 / 训练 | 本机 | 另一台机器（`train.py`） |
| 本机需要显卡 | 是（≥6GB 显存） | **不需要**（能跑模拟器即可） |
| 本机显存占用 | 6.5–7.9 GB | 1–2.5 GB（只有模拟器） |
| 网络 | 不需要 | 需要 ≥2MB/s，RTT < 30ms |
| 适合 | 单机、想省事 | 本机显卡弱 / 想用服务器的大卡 |

两种模式的**数据格式、奖励函数、训练逻辑完全一致**，唯一的区别是「`AndroidTool` 是本地的对象，还是网络的代理」。

---

# 一、完全本地模式

所有东西都在一台机器上跑：截图、视觉、策略、注入、训练。

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 |
| 显卡 | NVIDIA，显存 ≥ 6GB（legacy@batch64 实测 5046–7596MB） |
| Python | 3.10（conda 环境，见 [quickstart.md](quickstart.md) 第 2 节） |
| 设备 | 雷电模拟器或真机，`adb devices` 能看到 |
| 资源 | `models/start.onnx`、`models/death.onnx`、`scrcpy-win64-v2.0/` |

环境安装、模拟器设置、坐标标定、触摸设备探测都在 [quickstart.md](quickstart.md) 的前 4 节，这里不重复。

## 2. 启动

先进入对局（训练营 / 人机 / 离线训练都行），然后：

```bat
python train.py --iphone_id 127.0.0.1:5555 --assist_mode off ^
  --move_joystick_center_x 0.226 --move_joystick_center_y 0.800 --move_joystick_radius 184 ^
  --step_info_log false --step_timing_debug false --action_preview_enabled false
```

`--device_source` 默认就是 `local`，不用写。

## 3. 启动成功的标志

```text
[startup] device_source=local device=127.0.0.1:5555
[net] arch=legacy obs=640 params=199,788,562 (762.13MB fp32)
[startup] net=legacy obs=640 amp=True
[startup] params=199,788,562 weights~762.1MB(.pt) memory=3495 条 ≈ 8.0GB RAM batch=64
-------------------------------对局开始-----------------------------------
step=0    phase=opening  macro=opening_right_until_tower  action=(1, 7, 0, 0, 0, 0, 0, 0) reward=+0.361 eps=1.000 src=smart_prior assist=idle  human=n
training replay=20 loss=0.8142 ... arch=legacy memory=20/3495
```

* `step=` 在滚 + 偶尔 `training` 行 = 正常；
* `[startup] device_source=local` 说明走的是本机 adb。

## 4. 参数建议

| 场景 | 参数 |
| --- | --- |
| 默认（8GB 卡） | 不用加，legacy + AMP 自动 |
| 显存紧（6GB） | `--batch_size 32 --onnx_provider cpu --ocr_provider cpu` |
| 显存很紧（4GB） | `--net_arch compact --obs_size 160` |
| 内存大、想多存经验 | `--memory_ram_limit_gb 16` |
| 训练想更快 | `--replays_per_env_step 6 --train_time_budget_per_step_sec 1.2`（先看显存/温度） |

## 5. 停止与产物

`Ctrl+C` 停止。模型每 200 次训练迭代自动存 `src/wzry_ai.pt`（legacy 约 762MB），
指标写 `src/step_metrics.jsonl`。

---

# 二、双端模式

把「手」留在本地，「脑子」搬到另一台机器。

```text
┌──────────────── 本机（Windows + 模拟器） ────────────────┐
│  device_agent.py                                        │
│    scrcpy 窗口 → 截帧 → JPEG(≈300KB) ──┐                 │
│    adb ← 动作(8 个整数) ◄──────────────┼──┐              │
│    adb shell getevent → 触摸行 ────────┼──┼─►            │
│    adb shell cat /proc/net/dev → 文本 ─┼──┼─►            │
└────────────────────────────────────────┼──┼──────────────┘
                       TCP :5599          │  │
┌──────────────── 另一台机器 ─────────────┼──┼──────────────┐
│  train.py                              ▼  │              │
│    RemoteAndroidTool ◄────────────────────┘              │
│    MinimapVision / TargetTracker / PhaseController        │
│    DQNAgent（2080 Ti 满载）+ 经验池 + 训练                 │
└───────────────────────────────────────────────────────────┘
```

**关键点：训练数据不过网。** 程序端拿到帧后自己算观测、自己算奖励、自己存经验池，
所以网上只有一个「帧」和一个「动作」，而不是几十 MB 的 batch。

## 1. 本机（设备端）

### 1.1 依赖

只需要：`numpy`、`opencv-python`、`PyQt5`、`pywin32`、`filelock`。
**不需要 torch**——`argparses` 已做成 torch 可选，`device_agent.py` 也不会导入 `dqnAgent`。

```bat
pip install numpy opencv-contrib-python PyQt5 pywin32 filelock
```

### 1.2 启动

```bat
:: 先确认设备在线
.\scrcpy-win64-v2.0\adb.exe devices

python device_agent.py --iphone_id 127.0.0.1:5555 ^
  --device_listen_port 5599 --device_token mysecret ^
  --touch_device /dev/input/event4 ^
  --move_joystick_center_x 0.226 --move_joystick_center_y 0.800 --move_joystick_radius 184
```

> **摇杆/按钮的标定参数必须给设备端**，因为注入动作的是它。程序端传 `--move_joystick_*` 没有作用。

启动后会打印：

```text
[device] 启动 scrcpy 投屏窗口（截图源）…
====================================================================
[device] 监听 0.0.0.0:5599
[device] 分辨率 2400x1080，帧 jpeg@82，getevent=on
[device] 程序端启动参数：--device_source remote --device_endpoint 192.168.1.23:5599
[device] 提示：没有设置 --device_token，同网段任何人都能连；只在可信网络里这样用
[device] Ctrl+C 停止
====================================================================
[device] 程序端已连接 ('192.168.1.50', 51234)
```

把最后那行 `--device_endpoint` 的地址抄给另一台机器。

### 1.3 它做了什么

| 请求 | 动作 |
| --- | --- |
| `hello` | 回报分辨率、scrcpy 目录、设备序列号、触摸设备（token 不对就拒绝） |
| `shot` | 抓 scrcpy 窗口 → 编码（jpeg/png/raw）→ 回传 |
| `act` | `execute_action_burst(...)`：注入移动/普攻/技能/买装 |
| `stop` | `stop_persistent_move()`：松开摇杆 |
| `exec` | `action_move/attack/info`（`--action_burst_enabled false` 时才会用到） |
| `procnet` | `adb shell cat /proc/net/dev`，把文本回给程序端解析 |
| `touch_sub` | 起 `adb shell getevent -lt`，把触摸行成批（~30ms / 8 行）推给程序端 |

**看门狗**：程序端断开、或 `--device_watchdog_sec`（默认 10s）内没有任何请求，设备端会自动松摇杆——
避免程序端崩了以后角色一直朝一个方向跑。

## 2. 另一台机器（程序端）

### 2.1 依赖

需要完整的训练环境（`torch` + `numpy` + `opencv` + `onnxruntime` + `filelock`），
以及 `models/start.onnx`、`models/death.onnx`（程序端做开局检测和死亡检测）。
**不需要** `PyQt5` / `pywin32` / `scrcpy`（这些只在设备端用）。

```bash
pip install -r requirements.txt      # 或按 docs/remote-gpu.md 的镜像方式装 CUDA 版 torch
```

### 2.2 上传代码

只传代码，别传 `models/`、`scrcpy-win64-v2.0/`、`*.pt`、`human_data/`：

```bash
tar -czf code.tar.gz --exclude=__pycache__ --exclude=models \
  --exclude=scrcpy-win64-v2.0 --exclude='*.pt' --exclude='*.onnx' \
  --exclude=human_data --exclude=images .
scp code.tar.gz user@server:~/wzry_code.tar.gz
ssh user@server "mkdir -p ~/wzry_code && tar xzf ~/wzry_code.tar.gz -C ~/wzry_code"
# 再把两个 onnx 传过去（程序端要用）
scp models/*.onnx user@server:~/wzry_code/models/
```

### 2.3 启动

```bat
python train.py --device_source remote --device_endpoint 192.168.1.23:5599 ^
  --device_token mysecret ^
  --device_frame_format jpeg --device_frame_quality 82 ^
  --assist_mode assist --touch_device /dev/input/event4 ^
  --step_info_log false --step_timing_debug false --action_preview_enabled false
```

（`--iphone_id` 在双端模式下不生效，设备地址由 `--device_endpoint` 决定。）

启动后应看到：

```text
[remote] 已连接设备端 192.168.1.23:5599 (分辨率 2400x1080, 帧格式 jpeg@82)
[startup] device_source=remote endpoint=192.168.1.23:5599 frame=jpeg@82
[remote] scrcpy 由设备端负责，本机不启动投屏窗口
[net] arch=legacy obs=640 params=199,788,562
-------------------------------对局开始-----------------------------------
step=0 ... reward=+0.361 eps=1.000 src=smart_prior assist=idle human=n
```

## 3. 两端必须一致的东西

| 参数 | 谁生效 | 说明 |
| --- | --- | --- |
| `--device_token` | 两端 | 不一致 → 握手被拒 |
| `--device_frame_format / --device_frame_quality` | 记录在设备端，程序端按请求传 | 两端用同一组；jpeg 是有损的，质量变了「画面差奖励」会漂移 |
| `--move_joystick_*`、`--move_control_mode`、`--action_burst_duration_ms` | **设备端** | 注入动作在设备端执行 |
| `--touch_device` | 两端 | 设备端用它决定 `getevent` 读哪个设备；程序端用它过滤触摸行 |
| `--net_arch`、`--obs_size`、`--batch_size`、奖励相关 | **程序端** | 视觉/策略/奖励/训练都在程序端 |
| `--assist_mode` | 程序端 | 接管判定的状态机在程序端，触摸数据由设备端喂 |

## 4. 分步验证链路

**第 1 步：只验证连通与截图**（不训练、不进对局也能跑）

```bash
python -c "from remote_device import RemoteAndroidTool; t=RemoteAndroidTool('192.168.1.23:5599', token='mysecret'); print('分辨率', t.actual_width, t.actual_height); f=t.screenshot_window(); print('帧', None if f is None else f.shape); t.close()"
```

期望输出 `分辨率 2400 1080` + `帧 (1080, 2400, 3)`。

> 没开游戏也能截到画面（桌面/登录界面），所以这一步可以在进对局前先做通。

**第 2 步：验证动作能注入**

```bash
python -c "from remote_device import RemoteAndroidTool; t=RemoteAndroidTool('192.168.1.23:5599', token='mysecret'); print(t.execute_action_burst([1,180,0,0,0,0,0,0], burst_duration_ms=600)); import time; time.sleep(1); print(t.stop_persistent_move()); t.close()"
```

设备端人物应向左走一下再停。

**第 3 步：验证触摸转发**（只有人为辅助需要）

```bash
python -c "from remote_device import RemoteAndroidTool; import time; t=RemoteAndroidTool('192.168.1.23:5599', token='mysecret'); s=t.create_touch_stream(); s.start(); time.sleep(0.5); [print(len(s.snapshot_window(0, time.time()+1)))]; t.close()"
```

一边用鼠标按住模拟器摇杆拖动，`snapshot_window` 应返回 >0。

**第 4 步：跑自检**（不需要设备）

```bash
python test_assist_pipeline.py --device cpu     # 100 项，含双端链路环回测试
```

**第 5 步：正式跑对局**

## 5. 实测数据（本机 4060 + 服务器 2080 Ti）

| 指标 | 数值 |
| --- | --- |
| 每帧大小（jpeg q82，2400×1080） | 250–400 KB |
| 上行带宽（局域网） | 14.7 MB/s 实测；每步只占 ~0.6 MB/s |
| RTT | 2–7 ms |
| 每步新增延迟 | +40–80 ms（编码 + 传输 + 解码） |
| 步时延 | 455 ms（本地）→ 约 550 ms（双端） |
| 程序端显存 | 5046–7596 MB（legacy@64 AMP） |
| 设备端显存 | 1–2.5 GB（模拟器 + scrcpy） |

## 6. 故障处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `连不上设备端` | 端口被防火墙拦 / 端口写错 | Windows 防火墙放行 5599；确认设备端打印的端口 |
| `设备端拒绝: token 不匹配` | 两端 token 不一致 | 统一 `--device_token` |
| `shot 超时` | scrcpy 窗口没起来 / 被最小化 | 设备端别最小化；`--device_show_scrcpy true` |
| 触摸一直没数据 | 设备端 getevent 读不到 | 先在设备端跑 `touch_event_diagnose.py` |
| 人物一直朝一个方向跑 | 程序端死了但摇杆还按着 | 看门狗应在 10s 内松手；可调小 `--device_watchdog_sec` |
| 画面差奖励和本地不一致 | jpeg 质量不同 | 两端统一 `--device_frame_quality` |
| 程序端 `ImportError: win32gui` | 在 Linux 上误装了本地分支 | 用 `--device_source remote`，不要 `import android_tool` |

## 7. 安全

1. **一定设 `--device_token`**：不设的话同网段任何人都能控制你的游戏；
2. 不要把 5599 暴露到公网；跨网段优先用 SSH 隧道：

```bash
# 在程序端机器上执行，把远端 5599 映射到本地 5599
ssh -N -L 5599:127.0.0.1:5599 user@设备所在主机
# 然后程序端用 --device_endpoint 127.0.0.1:5599
```

3. 设备端跑在真实桌面上（需要 scrcpy 窗口），不要在无人值守的机器上长期开着。

---

# 三、两种模式互换

同一份代码、同一份数据，随时可以换：

```bat
:: 本地 → 双端：把 train.py 换成设备端 + 程序端两条命令
python device_agent.py --iphone_id 127.0.0.1:5555 --device_listen_port 5599
python train.py --device_source remote --device_endpoint 192.168.1.23:5599

:: 双端 → 本地：去掉 --device_source remote 即可
python train.py --iphone_id 127.0.0.1:5555
```

* 模型文件、`human_data/`、`step_metrics.jsonl` 都是**程序端**产物，换模式不影响；
* `device_agent.py` 不写任何训练产物，换机器时它那边是干净的。
