"""外部资源路径解析。

项目里有些资源体积很大、且不适合提交到 Git 仓库（`models/*.onnx`、`scrcpy-win64-v2.0/`、
训练产出的 `*.pt`）。本模块统一处理它们的定位：

1. 相对路径优先按「当前工作目录 → 项目根目录」解析；
2. 缺失时再尝试命令行 `--asset_root` 指定的额外只读目录（可以是绝对路径，也可以是相对项目根的路径）；
3. 输出文件一律落在项目目录内，不会写到项目外。

用法：

```bat
# 把权重放在项目外的某个目录，再让程序去那里找
python train.py --asset_root D:\\models --model_path wzry_ai.pt
```
"""

import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PARENT_ROOT = os.path.dirname(PROJECT_ROOT)

#: 只读回退根目录，按优先级排列（由 `--asset_root` 填充）
_FALLBACK_ROOTS = []


def set_asset_roots(extra_roots=None):
    """设置只读回退根目录（`--asset_root` 里传的逗号分隔路径）。"""
    roots = []
    for root in list(extra_roots or []):
        if not root:
            continue
        root = os.path.abspath(os.path.expanduser(str(root)))
        if root not in roots:
            roots.append(root)

    global _FALLBACK_ROOTS
    _FALLBACK_ROOTS = roots
    return list(_FALLBACK_ROOTS)


def fallback_roots():
    if not _FALLBACK_ROOTS:
        set_asset_roots()
    return list(_FALLBACK_ROOTS)


def _is_abs(path):
    return os.path.isabs(path) or (len(path) > 1 and path[1] == ":")


def _exists_file(path):
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _exists_dir(path):
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def resolve_asset_path(path, must_exist=False, description=""):
    """解析一个只读资源路径。

    - 绝对路径原样返回（存在性由调用方判断）。
    - 相对路径依次尝试：当前工作目录 -> 项目根目录 -> 回退根目录。
    - 全部不存在时返回项目根目录下的路径，便于报错信息定位。
    """
    if not path:
        return path

    path = str(path)
    if _is_abs(path):
        return path

    candidates = [
        os.path.abspath(path),
        os.path.join(PROJECT_ROOT, path),
    ]
    for root in fallback_roots():
        candidates.append(os.path.join(root, path))

    for candidate in candidates:
        if _exists_file(candidate) or _exists_dir(candidate):
            return candidate

    resolved = os.path.join(PROJECT_ROOT, path)
    if must_exist:
        label = f"{description} " if description else ""
        raise FileNotFoundError(
            f"{label}未找到: {path}\n已尝试: " + " | ".join(candidates)
        )
    return resolved


def resolve_dir(path, must_exist=True, description=""):
    resolved = resolve_asset_path(path, must_exist=must_exist, description=description)
    return resolved


def local_path(path):
    """把路径固定到项目目录内，用于输出文件，避免写到项目外。"""
    if not path:
        return path
    if _is_abs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return parent


def describe_assets():
    """返回用于启动日志的资源解析快照。"""
    return {
        "project_root": PROJECT_ROOT,
        "fallback_roots": fallback_roots(),
    }
