"""整场对局行为 profile 的读取与回放。

这是 `wzry` 里“开局走线 profile”思路的整场推广：

* 原版：`build_lane_profile.py` -> `src/lane_profile.json` -> `lane_macro.py`
  只回放开局前几十步的移动路线；
* 现在：`build_game_profile.py` -> `src/game_profile.json` -> 本模块，
  把一整场人为示范按“对局阶段”切开，统计每个阶段的走位角度分布、
  普攻/技能/补刀/推塔节奏、买装升级节奏，
  再以“整场先验”的形式喂回 `phase_controller`，
  让 AI 在所有阶段都能模仿人的节奏，而不是只在开局。

同时也输出与 `lane_profile.json` 兼容的字段，
可以直接 `--lane_profile_path src/game_profile.json` 让 `lane_macro.py` 继续用。
"""

import json
import math
import os
import random

from asset_paths import resolve_asset_path

ZERO_ACTION = [0, 0, 0, 0, 0, 0, 0, 0]

#: 把细分阶段归并成几组，样本不足时用组级统计兜底
PHASE_GROUPS = {
    "opening": ("opening",),
    "laning": ("laning", "clear_wave", "hold_lane", "poke", "push_tower"),
    "combat": ("trade", "chase"),
    "retreat": ("retreat",),
    "recover": ("recover",),
    "seek": ("map_seek",),
}

PHASE_TO_GROUP = {}
for _group, _phases in PHASE_GROUPS.items():
    for _phase in _phases:
        PHASE_TO_GROUP.setdefault(_phase, _group)

GROUP_TO_ATTACKS = {
    "opening": (1, 2),
    "laning": (1, 2, 3, 8, 9),
    "combat": (1, 8, 9, 10),
    "retreat": (1, 5),
    "seek": (1,),
    "recover": (5,),
}


def phase_group(phase_name):
    return PHASE_TO_GROUP.get(str(phase_name or ""), "laning")


def _weighted_choice(pairs, rng):
    total = sum(max(0.0, float(weight)) for _, weight in pairs)
    if total <= 0:
        return None
    target = rng.random() * total
    cursor = 0.0
    for value, weight in pairs:
        cursor += max(0.0, float(weight))
        if cursor >= target:
            return value
    return pairs[-1][0]


def _int_list(values, default=0):
    try:
        return int(values)
    except (TypeError, ValueError):
        return default


class GameProfile:
    """`src/game_profile.json` 的只读封装，带组级/全局兜底。"""

    def __init__(self, data=None, path=""):
        self.data = data or {}
        self.path = path
        self.phases = self.data.get("phases") or {}
        self.groups = self.data.get("groups") or {}
        self.global_stats = self.data.get("global") or {}

    @property
    def available(self):
        return bool(self.phases or self.global_stats)

    def has_phase(self, phase_name):
        return str(phase_name or "") in self.phases

    def stats_for(self, phase_name, min_steps=None):
        """按 精确阶段 -> 阶段组 -> 全局 的顺序取统计量。"""
        if min_steps is None:
            try:
                from argparses import args
                min_steps = int(getattr(args, "game_profile_min_steps", 6) or 6)
            except Exception:
                min_steps = 6
        phase_name = str(phase_name or "")
        stats = self.phases.get(phase_name)
        if stats and _int_list(stats.get("step_count", 0)) >= min_steps:
            return stats, phase_name

        group = phase_group(phase_name)
        group_stats = self.groups.get(group)
        if group_stats and _int_list(group_stats.get("step_count", 0)) > 0:
            return group_stats, group

        if self.global_stats:
            return self.global_stats, "global"
        return {}, ""

    def top_angles(self, phase_name, limit=3):
        stats, _ = self.stats_for(phase_name)
        return [item["angle"] for item in (stats.get("top_angles") or [])[:limit]]

    def describe(self):
        return {
            "path": self.path,
            "available": self.available,
            "phases": sorted(self.phases.keys()),
            "groups": sorted(self.groups.keys()),
            "source_records": _int_list(self.data.get("source_record_count", 0)),
        }

    @staticmethod
    def load(path):
        if not path:
            return GameProfile()
        resolved = resolve_asset_path(path)
        if not os.path.exists(resolved):
            return GameProfile(path=path)
        try:
            with open(resolved, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return GameProfile(path=path)
        if not isinstance(data, dict):
            return GameProfile(path=path)
        return GameProfile(data=data, path=resolved)


def load_game_profile(path=None):
    from argparses import args  # 延迟导入，方便单独做单元测试

    profile_path = path if path is not None else getattr(args, "game_profile_path", "src/game_profile.json")
    if not getattr(args, "game_profile_enabled", True):
        return GameProfile(path=profile_path)
    return GameProfile.load(profile_path)


class GameProfilePrior:
    """以人为示范统计出的“整场先验”生成动作。

    用法与 `gameplay_policy.GameplayBehaviorPrior` 类似，但每一个数值都来自
    `--game_profile_path` 指向的统计文件，因此对所有阶段都有效，而不只是开局。
    """

    def __init__(self, profile=None, seed=None):
        self.profile = profile if profile is not None else GameProfile()
        self.rng = random.Random(seed)
        self.reset_episode()

    def reset_episode(self):
        self.step_index = 0
        self.current_angle = None
        self.direction_steps_left = 0
        self.last_attack_step = -999
        self.last_skill_step = -999
        self.last_info_step = -999
        self.next_attack_interval = 3
        self.last_phase = None
        self.last_source = ""

    @property
    def available(self):
        return self.profile.available

    def _angle(self, phase, stats):
        top = stats.get("top_angles") or []
        pairs = [(int(item["angle"]), float(item.get("ratio", 0.0) or 0.0)) for item in top]
        pairs = [item for item in pairs if item[1] > 0]
        if not pairs:
            return None
        if self.current_angle is not None and self.direction_steps_left > 0:
            self.direction_steps_left -= 1
            jitter = int(getattr(self, "_jitter", 0) or 0)
            if jitter > 0:
                self.current_angle = int((self.current_angle + self.rng.randint(-jitter, jitter)) % 360)
            return self.current_angle

        chosen = _weighted_choice(pairs, self.rng)
        if chosen is None:
            return None
        self.current_angle = int(chosen) % 360
        self.direction_steps_left = self.rng.randint(3, 8)
        return self.current_angle

    def _attack_for(self, phase, stats, step):
        group = phase_group(phase)
        cadence = stats.get("attack_cadence") or {}
        move_ratio = float(stats.get("move_ratio", 0.0) or 0.0)
        priority = GROUP_TO_ATTACKS.get(group, (1,))
        base = max(2, _int_list(cadence.get("all", 3), 3))

        if step - self.last_attack_step < base:
            return 0
        if self.rng.random() > min(1.0, max(0.15, move_ratio + 0.15)):
            return 0

        attack_hist = stats.get("attack_hist") or {}
        pairs = []
        for key, value in attack_hist.items():
            try:
                action_id = int(key)
            except (TypeError, ValueError):
                continue
            if action_id <= 0 or action_id not in priority:
                continue
            pairs.append((action_id, float(value)))
        if not pairs:
            return int(self.rng.choice(priority))

        chosen = int(_weighted_choice(pairs, self.rng) or priority[0])
        self.last_attack_step = step
        if chosen >= 8:
            self.last_skill_step = step
        return chosen

    def _info_for(self, phase, stats, step):
        info_cadence = stats.get("info_cadence") or {}
        buy_interval = max(4, _int_list(info_cadence.get("buy", 40), 40))
        upgrade_interval = max(3, _int_list(info_cadence.get("upgrade", 14), 14))

        info_hist = stats.get("info_hist") or {}
        buys = []
        upgrades = []
        for key, value in info_hist.items():
            try:
                action_id = int(key)
            except (TypeError, ValueError):
                continue
            if action_id in (1, 2):
                buys.append((action_id, float(value)))
            elif action_id in (6, 7, 8):
                upgrades.append((action_id, float(value)))

        if buys and step - self.last_info_step >= buy_interval:
            self.last_info_step = step
            return int(_weighted_choice(buys, self.rng) or 1)
        if upgrades and step - self.last_info_step >= upgrade_interval:
            self.last_info_step = step
            return int(_weighted_choice(upgrades, self.rng) or 6)
        return 0

    def sample(self, phase, step=None, jitter=0, only_fill=None, base_action=None):
        """按阶段统计采样一个动作。

        `only_fill`：为 True 时只补空分支（`--game_profile_prior_mode fill`）。
        """
        if not self.available:
            return None

        phase = str(phase or "laning")
        step = self.step_index if step is None else int(step)
        stats, source = self.profile.stats_for(phase)
        if not stats:
            return None
        self.last_source = source
        self._jitter = int(jitter or 0)

        base = [int(v) for v in (base_action or ZERO_ACTION)]
        angle = self._angle(phase, stats)
        move_ratio = float(stats.get("move_ratio", 0.0) or 0.0)
        want_move = self.rng.random() <= max(0.0, min(1.0, move_ratio))
        attack = self._attack_for(phase, stats, step)
        info = self._info_for(phase, stats, step)

        sampled = [0] * 8
        if angle is not None and want_move:
            sampled[0] = 1
            sampled[1] = int(angle) % 360
        if attack:
            sampled[3] = int(attack)
            if attack >= 8:
                directional_ratio = float(stats.get("directional_skill_ratio", 0.0) or 0.0)
                if self.rng.random() <= directional_ratio and angle is not None:
                    sampled[4] = 1
                    sampled[5] = int(angle) % 360
                    sampled[6] = int(max(26, min(95, self.rng.uniform(0.3, 0.55) * 150)))
        if info:
            sampled[2] = int(info)

        if only_fill:
            # 只补 base 里空着的分支：base 已经有动作的分支一律保留
            action = list(base)
            if action[0] == 0 and sampled[0] != 0:
                action[0] = sampled[0]
                action[1] = sampled[1]
            if action[2] == 0 and sampled[2] != 0:
                action[2] = sampled[2]
            if action[3] == 0 and sampled[3] != 0:
                action[3:8] = sampled[3:8]
        else:
            # force：完全按 profile 生成，base 只用来兜底方向
            action = list(sampled)
            if action[0] != 0 and action[1] == 0 and base[1]:
                action[1] = base[1]

        self.step_index += 1
        self.last_phase = phase
        return [int(v) for v in action]

    def describe(self):
        return {
            "available": self.available,
            "phase": self.last_phase,
            "source": self.last_source,
            **self.profile.describe(),
        }


def angular_distance(first, second):
    return abs((first - second + 180) % 360 - 180)


def smooth_angle(angle, previous, max_delta=35):
    if previous is None:
        return int(angle) % 360
    delta = int(angle) - int(previous)
    delta = (delta + 180) % 360 - 180
    delta = max(-max_delta, min(max_delta, delta))
    return int((int(previous) + delta) % 360)


def angle_histogram(angles, bins=36):
    """把角度列表压成 10 度一格的直方图。"""
    histogram = {}
    for angle in angles:
        try:
            bucket = int(int(angle) % 360 // (360 // bins))
        except (TypeError, ValueError):
            continue
        histogram[bucket] = histogram.get(bucket, 0) + 1
    return {int(key): int(value) for key, value in sorted(histogram.items())}


def circular_mean(angles):
    if not angles:
        return 0
    sin_sum = sum(math.sin(math.radians(angle)) for angle in angles)
    cos_sum = sum(math.cos(math.radians(angle)) for angle in angles)
    return int(round(math.degrees(math.atan2(sin_sum, cos_sum)))) % 360
