# 显存与性能

默认结构与 wzry 原项目**同体量**（`legacy`：1.99 亿参数、762MB、640×640 输入），
在 8GB 显卡上单卡可训；需要更省显存时可以切到可选的 `compact` 小结构。
本文给出两者的实测数据、显存从哪来、以及哪些优化在不动模型规模的前提下依然生效。

实测环境：RTX 4060 Laptop 8GB / Windows / CUDA 11.8 / torch 2.7。复现命令见 §6。

---

## 1. 两种结构

| 结构 | 参数量 | `.pt` 体积 | 输入 | 训练峰值显存 | 单步（batch 64） |
| --- | --- | --- | --- | --- | --- |
| **`legacy`（默认）** | 199,788,562 | **762 MB** | 640×640 | 5046 MB（AMP） | 430 ms |
| `compact`（可选） | 1,099,922 | 4.2 MB | 160×160 | 159 MB | 2.4 ms |

`.pt` 里只存 `state_dict`（fp32，每参数 4 字节），所以文件大小 ≈ 参数量 × 4：
legacy 762MB、compact 4.2MB，与原项目一一对应。

切换方式：

```bat
:: 默认就是 legacy，不用写
python train.py --iphone_id 127.0.0.1:5555

:: 想省显存时
python train.py --iphone_id 127.0.0.1:5555 --net_arch compact --obs_size 160
```

> `--net_arch auto` 等同 `compact`（兼容旧配置）。`--obs_size` 默认 640；选 `compact` 时建议显式给 160。

---

## 2. legacy 的 5GB 从哪来

### 2.1 参数量：`fc` 层一家独大

```text
640×640
 → Conv(3→64, k8, s4)     64×159×159
 → Conv(64→128, k4, s2)   128×78×78 = 778,752 维
 → Linear(778752 → 256)   ← 199,360,512 个参数 = 762 MB，占整个模型的 99.8%
```

其余层加起来约 42.6 万参数（conv1 1.2 万 + conv2 13.1 万 + fc1 6.6 万 + 8 个动作头 21.8 万）。

### 2.2 训练时的显存构成（batch 64）

| 项 | 大小 |
| --- | --- |
| policy 权重 | 762 MB |
| target 权重（`requires_grad=False`） | 762 MB |
| Adam 一阶 + 二阶矩 | 1524 MB |
| 梯度 | 762 MB |
| 激活（640 输入、batch 64） | 约 1.5 GB |

合计约 5.3GB fp32 —— 这就是 `5312.65 MB` 的来源。权重/优化器/梯度是**固定成本**，
所以 batch 从 64 降到 16 也只降到 4661.8MB，省不出多少。

### 2.3 AMP：对 legacy 是净收益

实测（batch 64）：

| 配置 | 峰值显存 | 单步 |
| --- | --- | --- |
| legacy + fp32 | 5312.7 MB | 823 ms |
| legacy + AMP | **5046.3 MB** | **430 ms** |

大网络下 fp16 的算力收益盖过了 autocast 的固定开销：**省 266MB，快 1.9 倍**。
因此 `--amp` 默认是自动策略：`legacy` 开、`compact` 关（小网络开 AMP 反而慢 5 倍，见 §5）。

### 2.4 经验池：按内存上限自动限幅

一条经验是 `state + next_state` 两张 uint8 观测：

| 观测边长 | 每条经验 | 10000 条 |
| --- | --- | --- |
| 640（legacy） | 2.34 MB | 22.9 GB |
| 160（compact） | 0.15 MB | 1.4 GB |

640 观测下 10000 条要 22.9GB 内存，所以新增了 `--memory_ram_limit_gb`（默认 8）：

```text
[memory] obs=640 时每条经验约 2.34MB，经验池 10000 条需要约 22.9GB 内存。
[memory] 按 --memory_ram_limit_gb 8 自动下调到 3495 条（约 8.0GB）。想保留更大经验池请调大该上限。
```

内存充足就调大：`--memory_ram_limit_gb 16`（约 7000 条）；`0` 表示不限（自负 OOM 风险）。

---

## 3. 保留 legacy 前提下做的优化

这些改动**不改变模型规模**，因此不影响 `.pt` 体积与精度：

### 3.1 checkpoint 走 CPU 中转

```python
state_dict = torch.load(path, map_location="cpu")   # 关键：不要整块读进显存
...
vram_manager.release_unused()
```

原版 `torch.load(path)` 会把 762MB 权重直接塞进显存，峰值额外多 0.7GB。
实测加载峰值从 **2286 MB → 8.4 MB**。

### 3.2 ONNX / OCR 不再常驻

```python
providers = [("CUDAExecutionProvider", {
    "arena_extend_strategy": "kSameAsRequested",   # 不预扩张 arena
    "gpu_mem_limit": 512 * 1024 * 1024,            # 单会话上限
    "cudnn_conv_algo_search": "HEURISTIC",
}), "CPUExecutionProvider"]
```

`death.onnx` / PPOCR 改懒加载，可整体关掉或赶到 CPU：

```bat
--death_check_enabled false --finish_check_enabled false
--ocr_provider cpu --onnx_provider cpu --onnx_gpu_mem_limit_mb 256
```

对 8GB 卡这能省出 0.3~1GB，正好容纳 legacy 训练。

### 3.3 开回 cuDNN

原版在 `dqnAgent.__init__` 里 `torch.backends.cudnn.enabled = False`，卷积退化成通用实现。
现在 `vram_manager.configure_backends()` 默认 `enabled=True` + `benchmark=True`（输入尺寸固定）。

### 3.4 批量数据一次拷贝

```python
stacked = torch.from_numpy(np.ascontiguousarray(np.stack(observations)))  # uint8 上卡
tensor  = stacked.pin_memory().to(device, non_blocking=True)
tensor  = tensor.permute(0, 3, 1, 2).float().div_(255.0)                  # 在 GPU 上转 float
```

先搬 uint8（数据量 1/4）再转 float，且整个 batch 只拷一次；`batch_next_state` 只为非终止样本构建。

### 3.5 训练预算与稳定性

- `--replays_per_env_step 4`：限制每环境步最多训练几次；
- `--train_time_budget_per_step_sec 0.8`：再限制每环境步最多花多少秒训练
  （legacy 单次迭代 0.43s，靠它保证采集不被训练饿死）；
- `--td_target_clip 50` + `--grad_clip_norm 10`：防 Q 自举发散；
- `--step_metrics_max_mb 32`：指标文件自动轮转。

---

## 4. 可选：compact 小结构

`--net_arch compact --obs_size 160` 时：

```text
160×160
 → Conv(3→64,   k8, s4)
 → Conv(64→128, k4, s2)
 → Conv(128→128,k3, s1, p1)
 → AdaptiveAvgPool2d(4)  → 2048
 → Linear(2048 → 256) → 8 个动作头
```

参数量 1,099,922（4.2MB），训练峰值 159MB，单步 2.4ms。

**代价**（详见 [architecture.md](architecture.md)）：

1. 丢掉了 `fc` 的“位置精确聚合”：`AdaptiveAvgPool2d` 是位置无关的平均，看重整体局势而非细节；
2. `obs_size` 提到 224/640 意义有限——池化恒为 4×4，只是把池化做得更糊；
   真要提升分辨率收益，应调 `--net_pool_size`（如 8）；
3. 旧 legacy checkpoint 只能迁 38.9% 的参数（conv1/conv2/fc1/动作头），顶层要重训。

什么时候值得切：显存 < 6GB，或者只想验证训练链路是否跑通。要“与原项目同精度”就用默认 legacy。

---

## 5. AMP 的两种相反结论

| 网络 | fp32 | AMP | 结论 |
| --- | --- | --- | --- |
| `legacy` + 640 | 5312.7 MB / 823 ms | 5046.3 MB / 430 ms | **开**：省显存且快 1.9× |
| `compact` + 160 | 159.0 MB / 2.4 ms | 110.9 MB / 12.7 ms | **关**：省 30% 显存但慢 5× |

原因：compact 单步计算量太小，autocast 的算子分派与 `GradScaler` 的 unscale/step/update 是固定开销，
摊不平；legacy 的计算量大到足以盖过这些开销。

所以 `--amp` 默认是自动策略（`legacy`→开、`compact`→关）。想强制：

```bat
python train.py --amp false     :: 显式关闭
python train.py --amp true      :: 显式开启
```

---

## 6. 自己复现

```bat
:: 默认结构（legacy）fp32 vs AMP
python bench_vram.py --batch-sizes 64 --obs-sizes 640 --iters 5

:: 只测小结构
python bench_vram.py --skip-legacy --obs-sizes 160,224 --iters 12

:: 结果写 JSON
python bench_vram.py --iters 12 --json bench.json
```

输出示例：

```text
arch      obs  batch  amp      params     w_mb   peak_mb  step_ms
legacy    640     64 False 199,788,562   762.13   5312.65   823.41
legacy    640     64  True 199,788,562   762.13   5046.28   429.55
compact   640     64 False   1,099,922      4.2   1697.41    20.09
compact   640     64  True   1,099,922      4.2   1085.48    71.01
```

训练中持续观察：

```bat
python train.py --vram_log_interval_steps 50
```

```text
[vram] replay_200 alloc=1810MB reserved=5120MB peak_alloc=5046MB used=63.2%
```

`alloc` 稳定、不随时间单调上涨就正常；`reserved` 高于 `alloc` 是分配器缓存。

---

## 7. 默认值一览

| 参数 | 默认 | 理由 |
| --- | --- | --- |
| `--net_arch` | `legacy` | 与原项目同体量（1.99 亿参数 / 762MB / 640 输入） |
| `--obs_size` | 640 | legacy 的 `fc` 维度写死 640，启动时自动对齐 |
| `--amp` | 自动（legacy 开） | 实测省 266MB 且快 1.9× |
| `--batch_size` | 64 | 与原项目一致；再小也省不了多少（权重是固定成本） |
| `--memory_size` | 10000（会被限幅） | 受 `--memory_ram_limit_gb 8` 约束，640 下约 3495 条 |
| `--replays_per_env_step` | 4 | 防止训练线程空转把样本反复采样 |
| `--train_time_budget_per_step_sec` | 0.8 | legacy 单步 0.43s，避免训练饿死采集 |
| `--vram_empty_cache_interval_steps` | 200 | 太频繁反而慢 |

**按显存档位选配置：**

| 显存 | 建议 |
| --- | --- |
| ≥ 8GB | 默认（legacy + AMP，约 5GB）；模拟器约 1.5GB 也能共存 |
| 6GB | `--batch_size 32 --onnx_provider cpu --ocr_provider cpu`（约 4.8GB） |
| 4GB | 切 `--net_arch compact --obs_size 160`（159MB）；或 `--device_id cpu` 用 CPU 训 |
