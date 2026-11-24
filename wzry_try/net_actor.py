"""策略网络。

原版 `NetDQN` 的结构是：3x640x640 -> Conv(3->64,k8,s4) -> Conv(64->128,k4,s2)
-> Flatten(778752) -> Linear(778752->256) -> 8 个动作头。

仅 `fc` 一层就有 **199,360,512 个参数（762MB, fp32）**，再算上 Adam 的两份一阶/二阶矩
和 target_net，8GB 显卡上几乎立刻见底，而且 640x640 的卷积激活值也很大。

这里提供两种结构：

* `legacy`：与旧 checkpoint 完全一致，保证历史权重严格可读；
* `compact`（默认）：Conv 主干保留与原版一致的 `conv1/conv2` 形状（因此旧权重可以直接迁移），
  追加一层 3x3 卷积后用 `AdaptiveAvgPool2d` 把任意输入尺寸压成固定特征，
  再进 `Linear(2048 -> 256)`。参数量从 1.99 亿降到约 110 万（约 4.4MB），
  而且输入分辨率可以从 640 降到 160，激活值再降一个数量级。

`AdaptiveAvgPool2d` 让网络对输入尺寸不敏感，这带来两个好处：

1. 同一份权重可以在 `--obs_size 160 / 224 / 640` 之间切换；
2. 旧 `conv1/conv2` 权重可以无缝迁移到新结构。
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

ARCH_LEGACY = "legacy"
ARCH_COMPACT = "compact"
ARCH_CHOICES = (ARCH_COMPACT, ARCH_LEGACY)

#: 旧结构 `fc` 的输入维度（640x640 输入时 Conv2 的输出展平长度）
LEGACY_FC_FEATURES = 778752
LEGACY_INPUT_SIZE = 640

#: 两种结构的已知参数量与 fp32 权重体积（用于启动日志里预估显存/文件大小，避免真建一遍模型）
KNOWN_ARCH_PARAMS = {ARCH_LEGACY: 199788562, ARCH_COMPACT: 1099922}
KNOWN_ARCH_WEIGHT_MB = {ARCH_LEGACY: 762.1, ARCH_COMPACT: 4.2}


def expected_params(arch=None):
    arch = str(arch or default_arch()).lower()
    return KNOWN_ARCH_PARAMS.get(arch, 0)


def expected_weight_mb(arch=None):
    arch = str(arch or default_arch()).lower()
    return KNOWN_ARCH_WEIGHT_MB.get(arch, 0.0)


def _conv_out(size, kernel, stride, padding=0):
    return (int(size) - int(kernel) + 2 * int(padding)) // int(stride) + 1


def default_arch():
    """默认结构。

    默认 `legacy`（与原项目同体量：1.99 亿参数 / 762MB / 640 输入）。
    `compact` 仍然保留，需要显式 `--net_arch compact --obs_size 160` 才会启用；
    `auto` 等同 `compact`（兼容旧配置）。
    """
    try:
        from argparses import args
    except Exception:
        return ARCH_LEGACY
    arch = str(getattr(args, "net_arch", ARCH_LEGACY) or ARCH_LEGACY).lower()
    return arch if arch in ARCH_CHOICES else ARCH_COMPACT


def default_obs_size():
    try:
        from argparses import args
    except Exception:
        return LEGACY_INPUT_SIZE
    try:
        return max(64, int(getattr(args, "obs_size", LEGACY_INPUT_SIZE)))
    except (TypeError, ValueError):
        return LEGACY_INPUT_SIZE


class NetDQN(nn.Module):
    def __init__(self, arch=None, input_size=None, pool_size=None):
        super(NetDQN, self).__init__()
        self.arch = str(arch or default_arch()).lower()
        if self.arch not in ARCH_CHOICES:
            self.arch = ARCH_COMPACT
        if self.arch == ARCH_LEGACY:
            # legacy 结构把 Conv2 的输出直接展平进 fc，输入尺寸必须固定 640
            self.input_size = LEGACY_INPUT_SIZE
        else:
            self.input_size = int(input_size or default_obs_size())
        try:
            from argparses import args
            self.pool_size = int(pool_size or getattr(args, "net_pool_size", 4))
        except Exception:
            self.pool_size = int(pool_size or 4)
        self.pool_size = max(2, min(8, self.pool_size))

        self.conv1 = nn.Conv2d(3, 64, kernel_size=8, stride=4)
        self.conv2 = nn.Conv2d(64, 128, kernel_size=4, stride=2)

        if self.arch == ARCH_LEGACY:
            self.conv3 = None
            self.pool = None
            self.feature_size = self._legacy_feature_size(LEGACY_INPUT_SIZE)
        else:
            self.conv3 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)
            self.pool = nn.AdaptiveAvgPool2d(self.pool_size)
            self.feature_size = 128 * self.pool_size * self.pool_size

        self.fc = nn.Linear(self.feature_size, 256)
        self.fc1 = nn.Linear(256, 256)

        self.fc_move = nn.Linear(256, 2)
        self.fc_angle = nn.Linear(256, 360)
        self.fc_info = nn.Linear(256, 9)
        self.fc_attack = nn.Linear(256, 11)
        self.fc_action_type = nn.Linear(256, 3)
        self.fc_arg1 = nn.Linear(256, 360)
        self.fc_arg2 = nn.Linear(256, 100)
        self.fc_arg3 = nn.Linear(256, 5)
        self._initialize_weights()

    # -- 结构工具 -----------------------------------------------------------
    @staticmethod
    def _legacy_feature_size(size):
        width = _conv_out(_conv_out(size, 8, 4), 4, 2)
        return 128 * width * width

    def conv_output_size(self, size=None):
        """给定输入边长时的展平特征长度（仅用于日志/自检）。"""
        size = int(size or self.input_size)
        width = _conv_out(_conv_out(size, 8, 4), 4, 2)
        if self.arch == ARCH_LEGACY:
            return 128 * width * width
        return 128 * self.pool_size * self.pool_size

    def _initialize_weights(self):
        for module in self.modules():
            if isinstance(module, nn.Conv2d):
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.constant_(module.bias, 0)

    # -- 前向 ---------------------------------------------------------------
    def forward(self, x):
        weight_device = self.conv1.weight.device
        if x.device != weight_device:
            x = x.to(weight_device)

        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        if self.conv3 is not None:
            x = F.relu(self.conv3(x))
        if self.pool is not None:
            x = self.pool(x)
        x = x.flatten(1)
        x = F.relu(self.fc(x))
        x = F.relu(self.fc1(x))

        return (
            self.fc_move(x),
            self.fc_angle(x),
            self.fc_info(x),
            self.fc_attack(x),
            self.fc_action_type(x),
            self.fc_arg1(x),
            self.fc_arg2(x),
            self.fc_arg3(x),
        )

    # -- 自检与迁移 ---------------------------------------------------------
    def describe(self):
        total = sum(parameter.numel() for parameter in self.parameters())
        trainable = sum(parameter.numel() for parameter in self.parameters() if parameter.requires_grad)
        return {
            "arch": self.arch,
            "input_size": self.input_size,
            "pool_size": self.pool_size,
            "feature_size": self.feature_size,
            "params": int(total),
            "trainable_params": int(trainable),
            "fp32_mb": round(total * 4 / 1024 ** 2, 2),
            "conv_output_at_input": self.conv_output_size(self.input_size),
        }


def detect_arch_from_state_dict(state_dict):
    """根据 checkpoint 里 `fc.weight` 的形状判断它是哪种结构。"""
    if not isinstance(state_dict, dict):
        return ARCH_COMPACT
    weight = state_dict.get("fc.weight")
    if weight is None:
        return ARCH_COMPACT
    try:
        in_features = int(weight.shape[1])
    except (AttributeError, IndexError, TypeError):
        return ARCH_COMPACT
    return ARCH_LEGACY if in_features == LEGACY_FC_FEATURES else ARCH_COMPACT


def load_state_dict_flexible(model, state_dict, verbose=True):
    """尽量把旧权重复用起来，并返回一份可打印的迁移报告。

    规则：

    * 形状完全一致的张量直接加载（`conv1/conv2/fc1/动作头` 都属此类）；
    * `compact` 结构缺 `conv3` 时随机初始化，不影响其它层；
    * `legacy` 的 `fc.weight` 无法直接搬进 `compact` 的池化特征，跳过并提示。
    """
    if isinstance(state_dict, dict) and "state_dict" in state_dict and isinstance(state_dict["state_dict"], dict):
        state_dict = state_dict["state_dict"]

    model_state = model.state_dict()
    loadable = {}
    skipped = []
    for name, tensor in state_dict.items():
        target = model_state.get(name)
        if target is None:
            skipped.append((name, "no_such_layer"))
            continue
        if tuple(target.shape) != tuple(tensor.shape):
            skipped.append((name, f"shape {tuple(tensor.shape)} != {tuple(target.shape)}"))
            continue
        loadable[name] = tensor

    model.load_state_dict(loadable, strict=False)
    missing = [name for name in model_state if name not in loadable]

    report = {
        "source_arch": detect_arch_from_state_dict(state_dict),
        "target_arch": model.arch,
        "loaded": len(loadable),
        "skipped": skipped,
        "missing": missing,
        "transferred_ratio": round(
            sum(tensor.numel() for tensor in loadable.values())
            / max(1, sum(tensor.numel() for tensor in model_state.values())),
            4,
        ),
    }
    if verbose:
        print(
            f"[net] 权重迁移 {report['source_arch']} -> {report['target_arch']}: "
            f"loaded={report['loaded']} skipped={len(skipped)} missing={len(missing)} "
            f"transferred={report['transferred_ratio']:.1%}"
        )
        for name, reason in skipped[:6]:
            print(f"[net]   skip {name} ({reason})")
        if len(skipped) > 6:
            print(f"[net]   ... 还有 {len(skipped) - 6} 层被跳过")
    return report


def build_model(arch=None, input_size=None, device=None):
    model = NetDQN(arch=arch, input_size=input_size)
    info = model.describe()
    print(
        f"[net] arch={info['arch']} obs={info['input_size']} "
        f"params={info['params']:,} ({info['fp32_mb']}MB fp32)"
    )
    if device is not None:
        model = model.to(device)
    return model


def param_report(model):
    rows = []
    for name, module in model.named_children():
        count = sum(parameter.numel() for parameter in module.parameters())
        rows.append((name, count))
    return rows
