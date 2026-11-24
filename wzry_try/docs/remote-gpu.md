# 用远程服务器 GPU 跑本项目

本文说明怎样把本项目的**计算部分**放到一台带显卡的远程服务器上运行，以及每一步的坑。
本机（Windows + 模拟器）的操作链路见 [quickstart.md](quickstart.md)，显存数字见 [vram.md](vram.md)。

> **先看这里**：如果你想要的是「游戏画面和操作留在本机、训练跑在服务器」，
> 项目已经内建了双端模式，直接用 [training-modes.md](training-modes.md) 的第二部分，
> 本文第 3 节的环境搭建同样适用。
> 本文剩下的内容适用于「只想在服务器上跑基准 / 离线行为克隆 / 分析」的场景。

---

## 0. 先明确：远程能跑什么

| 任务 | 远程能跑吗 | 原因 |
| --- | --- | --- |
| `bench_vram.py`（显存/耗时基准） | ✅ | 纯 PyTorch 计算 |
| `test_assist_pipeline.py`（离线自检） | ✅ | 合成数据，不需要设备 |
| `train_behavior_clone.py`（行为克隆训练） | ✅ | 只读 `human_data/` 里的样本和帧 |
| `build_game_profile.py` / `build_lane_profile.py` | ✅ | 纯统计 |
| `analyze_episode.py`（单局指标统计） | ✅（CPU 即可） | 只读 JSONL |
| `train.py --device_source remote`（双端训练） | ✅ | 服务器只跑模型，设备在本机 |
| `train.py`（服务器直连模拟器） | ❌ | 需要图形会话 + 服务器能 adb 到设备，见下 |

服务器**直接**驱动模拟器跑不起来，原因是硬依赖：

1. **截图靠 PyQt5 抓 scrcpy 窗口**（`android_tool.screenshot_window()`），实现基于 Windows 的
   `win32gui`，无图形会话的 Linux 服务器上没有窗口可抓；
2. **必须有 Android 设备/模拟器**（`AndroidTool.__init__` 第一件事就是 `adb shell wm size`，
   读不到就 `RuntimeError`）；
3. 执行动作也走本机 adb。

这三条正好就是双端模式存在的原因：把「碰设备」的那半边留在本机，其余搬到服务器。

> 如果确实要在服务器上跑完整训练，需要同时满足：服务器上有 X11 图形会话（远程桌面/VNC）、
> 装了模拟器或能 adb 连到真机、并且把 `screenshot_window()` 改成截屏方案（如 `adb exec-out screencap`）。
> 这属于改造项目，不在本文范围内。

---

## 1. 前置检查

```bash
ssh sqx@10.201.4.118
```

登进去先确认四件事，任何一件不满足都会白忙：

```bash
# ① 显卡与空闲显存
nvidia-smi                       # 原生 Linux
/usr/lib/wsl/lib/nvidia-smi      # WSL2 里 nvidia-smi 不在 PATH

# ② 驱动对应的 CUDA 版本（决定能装哪个 torch）
nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv

# ③ Python 版本（决定有没有对应的 torch 轮子）
python3 -V

# ④ 网络能不能装包
curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 https://pypi.org/simple/
curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 https://pypi.tuna.tsinghua.edu.cn/simple/
```

实测例子（本文所有数字都来自这台机器，可以拿来自查）：

```text
NVIDIA GeForce RTX 2080 Ti, 11264 MiB, 10618 MiB, 560.94
Python 3.14.4
pypi.org: 000（只解析到 IPv6，走不通）
tuna:     200
```

**踩坑 1：`nvidia-smi: command not found`。** WSL2 里被放在 `/usr/lib/wsl/lib/`，
不在 `PATH`，要用绝对路径，或者 `export PATH=$PATH:/usr/lib/wsl/lib`。

**踩坑 2：pypi 只有 IPv6 记录 → `000`。** 换镜像源即可（见第 3 节）。

---

## 2. 上传代码（只传代码）

不要整目录 `scp -r`：`models/`（225MB）和 `scrcpy-win64-v2.0/`（15MB）在服务器上用不到，
`*.pt` / `*.jsonl` / `human_data/` 还可能是好几个 GB。

在本机（Windows PowerShell）打包：

```powershell
cd D:\SRTP\wzry_try
tar -czf D:\SRTP\.tmp_wzry_code.tar.gz `
  --exclude="__pycache__" --exclude="models" --exclude="scrcpy-win64-v2.0" `
  --exclude="*.pt" --exclude="*.onnx" --exclude="*.jsonl" `
  --exclude="human_data" --exclude="images" --exclude="*.log" --exclude=".git" .

# 实测 188 KB
```

上传并解压：

```powershell
scp D:\SRTP\.tmp_wzry_code.tar.gz sqx@10.201.4.118:~/wzry_code.tar.gz
ssh sqx@10.201.4.118 "mkdir -p ~/wzry_code && tar xzf ~/wzry_code.tar.gz -C ~/wzry_code && ls ~/wzry_code/*.py | wc -l"
```

应该看到 35 个 `.py`。

### 2.1 Windows 上怎么非交互输密码

Windows 自带的 `ssh` 没有 `sshpass`，但支持 `SSH_ASKPASS`。建一个只回显密码的 `.bat`：

```bat
@echo off
echo 你的密码
```

```powershell
$env:SSH_ASKPASS = "D:\SRTP\.askpass.bat"
$env:SSH_ASKPASS_REQUIRE = "force"
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL sqx@10.201.4.118 "hostname"
```

> ⚠️ 这只是为了跑通脚本。正式使用请改成密钥登录：
> `ssh-keygen -t ed25519` → `type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh sqx@主机 "cat >> ~/.ssh/authorized_keys"`，
> 之后既不用密码，也不会把密码留在脚本和命令历史里。

---

## 3. 建环境（版本匹配是最大的坑）

### 3.1 torch 的 CUDA 版本必须 ≤ 驱动支持的版本

| torch 轮子 | 需要的驱动 | 说明 |
| --- | --- | --- |
| `+cu118` | ≥ 450 | 老卡/老驱动最稳 |
| `+cu126` | ≥ 525 | 12.x 驱动通用 |
| `+cu128` | ≥ 525（12.x 次版本兼容） | **实测 560.94 可用** |
| `+cu13x` | ≥ 580 | **实测 560.94 报 driver too old** |

**踩坑 3：** PyPI 上 `torch` 的默认轮子会跟着最新 CUDA 走。本文实测第一次装到
`torch 2.15.0+cu132`，结果：

```text
UserWarning: CUDA initialization: The NVIDIA driver on your system is too old (found version 12060)
torch.cuda.is_available() -> False
```

降到 `torch==2.9.1`（`+cu128`）才正常。**装完一定立刻验证**：

```bash
python -c "import torch;print(torch.__version__, torch.cuda.is_available(), torch.version.cuda)"
```

### 3.2 Python 版本决定有没有轮子

- `torch 2.9+` 才有 `cp314` 轮子，更老的 torch 只到 `cp313`；
- Python 3.14 上可选范围很小，**能用 3.10 / 3.12 就别用 3.14**；
- 服务器上想换版本：`sudo apt install python3.12 python3.12-venv`，再用它建 venv。

### 3.3 装依赖

```bash
# 有些发行版默认没有 venv/pip
echo '你的密码' | sudo -S apt-get install -y python3-venv python3-pip

python3 -m venv ~/vram_env
~/vram_env/bin/python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple

# 关键：锁版本 + 只用二进制轮子（--only-binary 避免它去源码编译几小时）
~/vram_env/bin/python -m pip install --only-binary=:all: -i https://pypi.tuna.tsinghua.edu.cn/simple \
    torch==2.9.1

~/vram_env/bin/python -m pip install --only-binary=:all: -i https://pypi.tuna.tsinghua.edu.cn/simple \
    numpy filelock opencv-python-headless
```

说明：

* `opencv-python-headless` 而不是 `opencv-python`：服务器没有图形界面，headless 少一堆依赖；
* **不需要** `PyQt5` / `pywin32` / `ppocr-onnx`：这些只在 `train.py` 和 `android_tool.py` 里用；
* 体积：`torch` + CUDA 库约 **8.8 GB**（venv），pip 缓存还会再占 6–7 GB，装完记得清缓存。

验证：

```bash
~/vram_env/bin/python -c "import torch,numpy,cv2;print(torch.__version__, torch.cuda.is_available(), numpy.__version__, cv2.__version__)"
```

---

## 4. 跑测试

### 4.1 项目自带基准

```bash
cd ~/wzry_code
~/vram_env/bin/python bench_vram.py --batch-sizes 64 --obs-sizes 640 --iters 8
```

输出（RTX 2080 Ti 实测）：

```text
arch      obs  batch  amp      params     w_mb   peak_mb  step_ms
legacy    640     64 False 199,788,562   762.13   6027.48    55.41
legacy    640     64  True 199,788,562   762.13   5046.28    81.51
compact   640     64 False   1,099,922      4.2   2584.86    16.01
compact   640     64  True   1,099,922      4.2   1088.49    36.65
```

### 4.2 完整 `DQNAgent.replay` 探针（更接近真实训练）

`bench_vram` 不含经验池采样、uint8 上卡、辅助模仿损失、`GradScaler`，真实峰值会高约 2 GB。
把下面内容存成 `~/vram_probe.py`（**不要放进项目目录**）：

```python
import json, os, sys, time
ARCH, BATCH, OBS = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
AMP = bool(int(sys.argv[4]))
HOLD_MB = int(sys.argv[5]) if len(sys.argv) > 5 else 0
STEPS = int(sys.argv[6]) if len(sys.argv) > 6 else 3

PROJECT = os.path.expanduser("~/wzry_code")
os.chdir(PROJECT); sys.path.insert(0, PROJECT)

import numpy as np, torch
sys.argv = ["probe", "--device_id", "cuda:0", "--batch_size", str(BATCH),
            "--memory_size", str(max(64, BATCH * 4)), "--net_arch", ARCH,
            "--obs_size", str(OBS), "--amp", "true" if AMP else "false",
            "--model_path", "src/__none_probe__.pt"]
from dqnAgent import DQNAgent
from globalInfo import GlobalInfo

hold, result = None, {"arch": ARCH, "batch": BATCH, "amp": AMP, "hold_mb": HOLD_MB}
try:
    torch.cuda.empty_cache(); torch.cuda.reset_peak_memory_stats()
    result["gpu_total_mb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**20)
    if HOLD_MB:                      # 模拟显卡已被别的进程占用
        hold = torch.empty(HOLD_MB * 1024 * 1024, dtype=torch.uint8, device="cuda:0")
    started = time.perf_counter()
    agent = DQNAgent()
    obs = np.zeros((agent.obs_size, agent.obs_size, 3), dtype=np.uint8)
    for i in range(max(64, BATCH * 4)):
        GlobalInfo().store_transition_dqn(obs, [1, i % 360, 0, 1, 0, 0, 0, 0], 0.1, obs, 0, None, 0.0)
    info = None
    for _ in range(STEPS):
        info = agent.replay()
    torch.cuda.synchronize()
    result.update(ok=True, amp_enabled=bool(agent.amp_enabled),
                  peak_alloc_mb=round(torch.cuda.max_memory_allocated() / 2**20, 1),
                  peak_reserved_mb=round(torch.cuda.max_memory_reserved() / 2**20, 1),
                  step_ms=round((time.perf_counter() - started) * 1000 / STEPS, 1),
                  loss=round(float(info["loss"]), 5) if info else None)
except RuntimeError as exc:
    result.update(ok=False, oom="out of memory" in str(exc).lower(), err=str(exc)[:200])
finally:
    del hold
    print("PROBE_JSON " + json.dumps(result, ensure_ascii=False), flush=True)
```

跑：

```bash
~/vram_env/bin/python ~/vram_probe.py legacy 64 640 1 0        # 默认配置
~/vram_env/bin/python ~/vram_probe.py legacy 64 640 1 3000     # 模拟已被占用 3GB
~/vram_env/bin/python ~/vram_probe.py compact 64 160 0 0      # 对照小网络
```

### 4.3 自检

```bash
~/vram_env/bin/python test_assist_pipeline.py --device cuda:0
```

100 项应全过（`--device cuda:0` 还会额外验证 AMP 路径）。

---

## 5. 结果判读

RTX 2080 Ti（11264 MiB）实测，`peak_reserved` 才是决定会不会爆的量：

| 结构 | batch | AMP | 人为占用 | peak_reserved | 单步 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| legacy | 64 | ✗ | 0 | 8226 MB | 1382 ms | 不爆 |
| legacy | 64 | ✓ | 0 | **7598 MB** | 1305 ms | 不爆，余量约 3.4 GB |
| legacy | 32 | ✓ | 0 | 6276 MB | 1241 ms | 不爆 |
| compact | 64×160 | ✗ | 0 | 370 MB | 343 ms | 不爆 |
| legacy | 64 | ✓ | 3000 MB | 10598 MB | 1295 ms | 不爆 |
| legacy | 64 | ✓ | 7500 MB | 15098 MB | 3960 ms | 不爆，但慢 3× |

### 5.1 “爆显存”在不同平台上表现不同

| 平台 | 超出物理显存时 | 你会看到 |
| --- | --- | --- |
| Windows / WSL2（本文两台机器都是） | 驱动把分配分页到系统内存（WDDM 超卖） | **不报错**，`step_ms` 突然翻几倍 |
| 原生 Linux + 独占显卡 | 硬失败 | `RuntimeError: CUDA out of memory` |

所以判据有两个，缺一不可：

1. `peak_reserved` 是否超过显卡总量（本文 11264 MB）；
2. `step_ms` 是否明显变大（同样配置正常 1.3 s，一旦分页会到 3–4 s）。

**不要只看有没有抛异常**：在 WSL2/Windows 上它永远不抛。

---

## 6. 注意事项清单

1. **先看显卡是否空闲**：`nvidia-smi` 看 `memory.used`。多人共用的服务器上，别人在跑任务时你抢显存只会两边都变慢。
   需要指定卡时用 `CUDA_VISIBLE_DEVICES=1`（此时程序里的 `cuda:0` 就是物理 1 号卡）。
2. **AMP 的结论因卡而异**：2080 Ti 上 `bench_vram` 显示 AMP 更慢（82 vs 55 ms），但完整 replay 路径里 AMP 更快
   （1305 vs 1382 ms）。换卡先跑 `bench_vram` 再决定，别照抄别人的参数。
3. **服务器内存通常更大**，可以放开经验池：`--memory_ram_limit_gb 32`（640 观测下约 1.4 万条）。
   默认 8 GB 会让 `--memory_size` 被下调到 3495 条。
4. **断线保护**：长任务用 `tmux` 或 `nohup ... &`，并给 ssh 加 `-o ServerAliveInterval=30`；
   直接 `ssh -c "python ..."` 一旦网络抖动，训练就没了。
5. **不要在服务器上放密码**：`~/.bash_history` 会记录。用密钥。
6. **不要上传大文件**：`models/*.onnx`（225MB）和 `*.pt`（762MB）对基准/BC 训练都不需要。
7. **磁盘**：venv 8.8 GB + pip 缓存 6.7 GB。`--no-cache-dir` 可以省掉缓存；
   装完清理一次（见第 7 节）。
8. **别在服务器上跑 `train.py`**（原因见第 0 节）。它的依赖（PyQt5/pywin32/ppocr/scrcpy）也不要装。

---

## 7. 测试完清理

```bash
rm -rf ~/vram_env ~/wzry_code ~/wzry_code.tar.gz ~/vram_probe.py
rm -rf ~/.cache/pip

# 如果你还为它装过系统包
sudo apt-get purge -y python3-pip python3-venv
sudo apt-get clean
```

清理前后用 `du -sh ~` 和 `df -h ~` 对比确认。实测一次完整测试装完占 **8.8 GB（venv）+ 6.7 GB（pip 缓存）**，
删干净后家目录回到原有大小。

> 顺带一提：`sudo apt-get purge` 前先用 `apt-get -s remove --purge ...` 做一次模拟，
> 确认不会连带删掉系统 Python 本身。
