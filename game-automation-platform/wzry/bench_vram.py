"""显存 / 参数量基准对比。

不需要游戏、不需要 adb、不需要截图，直接量化“改了什么、省了多少”：

```powershell
python bench_vram.py                    # 测全部组合
python bench_vram.py --skip-legacy      # 跳过 1.99 亿参数的旧结构（8GB 卡可能 OOM）
python bench_vram.py --batch-sizes 16,64 --obs-sizes 160,224
```

每一项都会真实跑一轮 `forward + backward + Adam.step`，记录

* 参数量与 fp32 权重体积；
* 峰值显存分配（`torch.cuda.max_memory_allocated`）；
* 单步耗时（毫秒）。

对比对象是 `wzry` 原版配置：`legacy` 结构 + 640 输入 + 64 批次 + fp32。
"""

import argparse
import gc
import time
import sys

import numpy as np
import torch
from torch import optim

# `net_actor` 在实例化时会读 `argparses.args`，而 argparses 会解析 sys.argv。
# 先把本脚本自己的参数藏起来，避免被 argparse 当成未知参数报错。
_REAL_ARGV = list(sys.argv[1:])
sys.argv = ["bench_vram"]

import vram_manager  # noqa: E402
from net_actor import NetDQN  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark VRAM usage of wzry_try network/training configs.")
    parser.add_argument("--batch-sizes", type=str, default="64,16")
    parser.add_argument("--obs-sizes", type=str, default="160,224,640")
    parser.add_argument("--iters", type=int, default=3)
    parser.add_argument("--skip-legacy", action="store_true")
    parser.add_argument("--device", type=str, default="cuda:0")
    parser.add_argument("--with-target-net", action="store_true",
                        help="同时统计 target_net 的常驻显存(默认开启, 用 --no-target-net 关闭)")
    parser.add_argument("--no-target-net", dest="with_target_net", action="store_false")
    parser.add_argument("--json", type=str, default="", help="把结果写到 json 文件")
    parser.set_defaults(with_target_net=True)
    return parser.parse_args(_REAL_ARGV)


def _int_list(raw):
    values = []
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values or [160]


def _null_ctx():
    class _Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    return _Ctx()


def run_case(arch, obs_size, batch_size, iters, amp, device, with_target_net=True):
    torch.cuda.empty_cache()
    gc.collect()
    vram_manager.reset_peak_vram()

    model = NetDQN(arch=arch, input_size=obs_size).to(device)
    target = None
    if with_target_net:
        target = NetDQN(arch=arch, input_size=obs_size).to(device)
        target.eval()
        for parameter in target.parameters():
            parameter.requires_grad_(False)

    optimizer = optim.Adam(model.parameters(), lr=1e-4)
    criterion = torch.nn.SmoothL1Loss()
    scaler = vram_manager.build_grad_scaler(amp)
    autocast = vram_manager.autocast_context(amp) if amp else _null_ctx()

    params = sum(p.numel() for p in model.parameters())
    weights_mb = params * 4 / 1024 ** 2
    total_resident_mb = weights_mb * (2 if with_target_net else 1)

    sample = np.random.randint(0, 255, (batch_size, obs_size, obs_size, 3), dtype=np.uint8)
    batch = torch.from_numpy(sample).to(device).permute(0, 3, 1, 2).float().div_(255.0)
    labels = torch.randint(0, 2, (batch_size,), device=device)
    rewards = torch.rand(batch_size, device=device)

    timings = []
    try:
        for _ in range(max(1, iters)):
            start = time.perf_counter()
            model.train()
            with autocast:
                outputs = model(batch)
                q = outputs[0].gather(1, labels.unsqueeze(1))
                loss = criterion(q.squeeze(1), rewards)
            optimizer.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            timings.append((time.perf_counter() - start) * 1000.0)
    except (RuntimeError, MemoryError) as exc:
        message = str(exc).splitlines()[0][:160] if str(exc) else exc.__class__.__name__
        return {
            "arch": arch,
            "obs": obs_size,
            "batch": batch_size,
            "amp": amp,
            "params": params,
            "weights_mb": round(weights_mb, 2),
            "peak_mb": None,
            "step_ms": None,
            "error": message,
        }

    peak_mb = vram_manager.vram_stats().get("max_allocated_mb")
    result = {
        "arch": arch,
        "obs": obs_size,
        "batch": batch_size,
        "amp": amp,
        "params": params,
        "weights_mb": round(weights_mb, 2),
        "resident_weights_mb": round(total_resident_mb, 2),
        "peak_mb": peak_mb,
        "step_ms": round(sum(timings) / len(timings), 2),
        "error": "",
    }

    del model, target, optimizer, batch, labels, rewards
    gc.collect()
    torch.cuda.empty_cache()
    return result


def main():
    args = parse_args()
    device = vram_manager.resolve_device(args.device)
    info = vram_manager.describe_device(device)
    print(f"device={info}")
    if not info.get("cuda"):
        print("当前环境没有可用 CUDA，后续数字会退化为 CPU 计时，显存恒为 0。")

    batch_sizes = _int_list(args.batch_sizes)
    obs_sizes = _int_list(args.obs_sizes)

    cases = []
    # 默认结构（与 wzry 原项目同体量）：fp32 与 AMP 各测一次
    if not args.skip_legacy:
        cases.append(("legacy", 640, max(batch_sizes), False))
        if info.get("cuda"):
            cases.append(("legacy", 640, max(batch_sizes), True))
    # 可选的小结构 × 分辨率 × 精度
    for obs in obs_sizes:
        for amp in ((False, True) if info.get("cuda") else (False,)):
            cases.append(("compact", obs, max(batch_sizes), amp))
    # 小批次
    for obs in obs_sizes:
        for batch in batch_sizes:
            if batch == max(batch_sizes):
                continue
            cases.append(("compact", obs, batch, bool(info.get("cuda"))))

    print("")
    print(f"{'arch':<8}{'obs':>5}{'batch':>7}{'amp':>5}{'params':>12}{'w_mb':>9}{'peak_mb':>10}{'step_ms':>9}  note")
    print("-" * 96)

    results = []
    seen = set()
    for arch, obs, batch, amp in cases:
        key = (arch, obs, batch, amp)
        if key in seen:
            continue
        seen.add(key)
        result = run_case(
            arch=arch,
            obs_size=obs,
            batch_size=batch,
            iters=args.iters,
            amp=amp,
            device=device,
            with_target_net=args.with_target_net,
        )
        results.append(result)
        print(
            f"{result['arch']:<8}{result['obs']:>5}{result['batch']:>7}{str(result['amp']):>5}"
            f"{result['params']:>12,}{result['weights_mb']:>9}"
            f"{str(result['peak_mb']):>10}{str(result['step_ms']):>9}  {result['error']}"
        )

    if results:
        baseline = next((item for item in results if item["arch"] == "legacy" and not item["error"]), None)
        best = next(
            (item for item in results
             if item["arch"] == "compact" and item["amp"] and item["obs"] == min(obs_sizes) and not item["error"]),
            None,
        )
        if baseline and best and baseline.get("peak_mb") and best.get("peak_mb"):
            print("")
            print(
                f"legacy(640,fp32,batch={baseline['batch']}) peak={baseline['peak_mb']}MB "
                f"-> compact({best['obs']},amp,batch={best['batch']}) peak={best['peak_mb']}MB "
                f"= {baseline['peak_mb'] / max(1.0, best['peak_mb']):.1f}x 降幅"
            )
            print(
                f"参数量 {baseline['params']:,} -> {best['params']:,} "
                f"= {baseline['params'] / max(1, best['params']):.0f}x 减少"
            )

    if args.json:
        import json

        with open(args.json, "w", encoding="utf-8") as file:
            json.dump({"device": info, "results": results}, file, ensure_ascii=False, indent=2)
        print(f"json={args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
