"""显存与内存调节工具。

这个模块把“怎么少占显存、怎么观测显存”从模型/训练代码里抽出来，
集中解决 `wzry` 原版在 8GB 显卡上最容易踩的几个坑：

1. 混合精度（AMP）：卷积网络前向/反向默认走 fp16，激活值直接减半；
2. cuDNN 后端配置：原版在 `dqnAgent` 里 `cudnn.enabled = False`，Conv 会退化成慢速实现；
3. 显存快照与周期性缓存回收，方便定位到底是哪一步把显存吃满；
4. 经验池内存估算，避免 `memory_size=10000` 存整屏截图直接吃掉上百 GB 内存；
5. 统一的“把 uint8 观测一次性搬到 GPU”路径，减少碎片化的小拷贝。
"""

import gc
import os

import torch

from asset_paths import PROJECT_ROOT  # noqa: F401  (便于外部统一 import)


# --------------------------------------------------------------------------- #
# 设备与后端
# --------------------------------------------------------------------------- #
def cuda_available():
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_device(device_id="cuda:0"):
    """按配置解析设备；CUDA 不可用时安全回退到 CPU。"""
    if cuda_available():
        if device_id is None or str(device_id).strip() == "":
            device_id = "cuda:0"
        try:
            return torch.device(device_id)
        except (RuntimeError, ValueError):
            return torch.device("cuda:0")
    return torch.device("cpu")


def describe_device(device=None):
    info = {
        "device": str(device) if device is not None else "unknown",
        "device_type": str(getattr(device, "type", device if device is not None else "unknown")),
        "cuda": cuda_available(),
    }
    if not info["cuda"]:
        return info
    try:
        index = 0
        if device is not None and getattr(device, "index", None) is not None:
            index = int(device.index)
        props = torch.cuda.get_device_properties(index)
        info.update({
            "name": props.name,
            "total_gb": round(props.total_memory / 1024 ** 3, 2),
            "capability": f"{props.major}.{props.minor}",
            "multi_processor_count": props.multi_processor_count,
        })
    except Exception as exc:  # pragma: no cover - 只在异常驱动上触发
        info["error"] = str(exc)
    return info


def configure_backends(benchmark=None, deterministic=False):
    """配置卷积后端。

    `benchmark=True` 会在首轮做一次算法搜索，之后固定用最快的卷积实现；
    输入尺寸固定（`obs_size` 固定）时收益明显。
    """
    try:
        torch.backends.cudnn.enabled = True
        if benchmark is None:
            benchmark = cuda_available()
        torch.backends.cudnn.benchmark = bool(benchmark)
        torch.backends.cudnn.deterministic = bool(deterministic)
    except Exception:
        pass

    try:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
    except Exception:
        pass

    if hasattr(torch, "set_float32_matmul_precision"):
        try:
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 显存观测
# --------------------------------------------------------------------------- #
def _mb(value):
    return round(float(value) / 1024.0 / 1024.0, 2)


def vram_stats():
    """返回当前显存占用快照（单位 MB）；CPU 环境返回 available=False。"""
    if not cuda_available():
        return {"available": False}
    try:
        stats = {
            "available": True,
            "allocated_mb": _mb(torch.cuda.memory_allocated()),
            "reserved_mb": _mb(torch.cuda.memory_reserved()),
            "max_allocated_mb": _mb(torch.cuda.max_memory_allocated()),
            "max_reserved_mb": _mb(torch.cuda.max_memory_reserved()),
        }
        try:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            stats["free_mb"] = _mb(free_bytes)
            stats["total_mb"] = _mb(total_bytes)
            stats["used_ratio"] = round(1.0 - float(free_bytes) / max(1.0, float(total_bytes)), 4)
        except Exception:
            pass
        return stats
    except Exception as exc:  # pragma: no cover
        return {"available": False, "error": str(exc)}


def reset_peak_vram():
    if not cuda_available():
        return
    try:
        torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def format_vram(stats=None):
    stats = vram_stats() if stats is None else stats
    if not stats.get("available"):
        return "vram=unavailable"
    parts = [
        f"alloc={stats.get('allocated_mb')}MB",
        f"reserved={stats.get('reserved_mb')}MB",
        f"peak_alloc={stats.get('max_allocated_mb')}MB",
    ]
    if "used_ratio" in stats:
        parts.append(f"used={stats['used_ratio']:.1%}")
    return " ".join(parts)


def log_vram(tag, stats=None, printer=print):
    printer(f"[vram] {tag} {format_vram(stats)}")
    return stats


def maybe_empty_cache(step, interval=0, tag="", printer=print, log=True):
    """按步数周期回收显存缓存。

    `torch.cuda.empty_cache()` 会打断分配器的复用，频繁调用反而更慢，
    因此只在显存占用出现明显增长或间隔足够大时才做一次。
    """
    if not cuda_available() or not interval or interval <= 0:
        return False
    if step <= 0 or step % int(interval) != 0:
        return False
    before = vram_stats()
    _empty_cache()
    if log:
        after = vram_stats()
        printer(
            f"[vram] empty_cache {tag} step={step} "
            f"reserved {before.get('reserved_mb')}MB -> {after.get('reserved_mb')}MB"
        )
    return True


def _empty_cache():
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def release_unused():
    _empty_cache()


# --------------------------------------------------------------------------- #
# 混合精度
# --------------------------------------------------------------------------- #
def amp_supported(device):
    try:
        return bool(getattr(device, "type", str(device)) == "cuda" and cuda_available())
    except Exception:
        return False


def autocast_context(enabled, dtype=None, device_type="cuda"):
    """返回可直接用于 `with` 的 autocast 上下文。

    兼容 torch 1.10 ~ 2.x：优先用新的 `torch.amp.autocast`，
    旧版本回退到 `torch.cuda.amp.autocast`。
    """
    if not enabled:
        return _null_context()

    dtype = dtype or torch.float16
    try:
        return torch.amp.autocast(device_type=device_type, dtype=dtype)
    except (AttributeError, TypeError):
        pass
    try:
        return torch.cuda.amp.autocast(dtype=dtype)
    except (AttributeError, TypeError):
        return _null_context()


def build_grad_scaler(enabled):
    """构建 GradScaler；不可用时返回一个惰性空实现。"""
    if not enabled:
        return _NullScaler()
    try:
        return torch.amp.GradScaler("cuda")
    except (AttributeError, TypeError):
        pass
    try:
        return torch.cuda.amp.GradScaler()
    except (AttributeError, TypeError):
        return _NullScaler()


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class _NullScaler:
    """`GradScaler` 的空实现，保证同一份训练代码既能跑 AMP 也能跑纯 fp32。"""

    def __init__(self):
        self._scale_value = 1.0

    def scale(self, loss):
        return loss

    def unscale_(self, optimizer):
        return None

    def step(self, optimizer, *args, **kwargs):
        optimizer.step(*args, **kwargs)

    def update(self, *args, **kwargs):
        return None

    def state_dict(self):
        return {}

    def load_state_dict(self, state):
        return None

    def get_scale(self):
        return 1.0


# --------------------------------------------------------------------------- #
# 容量估算
# --------------------------------------------------------------------------- #
def human_bytes(value):
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}PB"


def obs_nbytes(obs_size, channels=3, dtype_bytes=1):
    return int(obs_size) * int(obs_size) * int(channels) * int(dtype_bytes)


def replay_memory_estimate(capacity, obs_size, channels=3, dtype_bytes=1):
    """估算经验池常驻内存。

    原版把整屏截图（例如 1260x2800x3 的 uint8，约 10.6MB）直接塞进经验池，
    `memory_size=10000` 时理论占用超过 100GB。改成“入库前先缩放到 obs_size”
    之后，占用量下降 2~3 个数量级。
    """
    per_state = obs_nbytes(obs_size, channels, dtype_bytes)
    # state + next_state 各一份
    total = per_state * 2 * int(capacity)
    return {
        "per_obs_bytes": per_state,
        "per_obs_human": human_bytes(per_state),
        "total_bytes": total,
        "total_human": human_bytes(total),
    }


def report_startup(device, printer=print):
    info = describe_device(device)
    printer(f"[vram] device={info}")
    return info
