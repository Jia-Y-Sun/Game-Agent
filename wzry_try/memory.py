from collections import namedtuple
import random

# Transition - 一个命名元组(named tuple)用于表示环境中的单次状态迁移(single transition)。
#
# 相比原版新增两个字段：
#   human_action - 这一步如果是人操作的，记录人给出的动作标签；否则为 None。
#                  它是“整场人为辅助训练”的核心：既是模仿学习的标签，
#                  也用来把这条经验标记成优先样本。
#   priority     - 采样权重提示（人为示范 / 高奖励经验会更高），供混合采样使用。
#
# 后两个字段有默认值，因此旧的 `Transition(state, action, reward, next_state, done)`
# 调用方式仍然可用。
Transition = namedtuple(
    'Transition',
    ('state', 'action', 'reward', 'next_state', 'done', 'human_action', 'priority'),
    defaults=(None, 0.0),
)


class ReplayMemory(object):
    """定长循环缓冲。

    与原始实现相比有三处针对性改动，都是为了“更少内存 + 更好地用人示范”：

    1. `state/next_state` 存放的是**降分辨率后的 uint8 观测**（由 `dqnAgent` 负责缩放），
       而不是整屏 BGR 截图。原版 `memory_size=10000` 存 1260x2800x3 的截图，
       理论常驻内存超过 100GB；缩到 160x160 后同样容量只要约 1.5GB。
    2. 支持“人为示范样本”和“自主探索样本”按比例混合采样，
       让人工接管带来的少量高质量经验不会被海量随机探索淹没。
    3. 记录字节估算，便于在日志里直接看到经验池的内存压力。
    """

    def __init__(self, capacity, seed=None):
        self.capacity = max(1, int(capacity))
        self.memory = []
        self.position = 0
        self._rng = random.Random(seed)

        self._version = 0
        self._cache_version = -1
        self._human_slots = []
        self._ai_slots = []

    # -- 写入 ---------------------------------------------------------------
    def push(self, *args):
        """保存一条迁移。

        支持两种调用方式：`push(state, action, reward, next_state, done)`
        或带标签的 `push(state, action, reward, next_state, done, human_action, priority)`。
        """
        if len(args) == 1 and isinstance(args[0], tuple):
            transition = args[0] if isinstance(args[0], Transition) else Transition(*args[0])
        else:
            transition = Transition(*args)

        if len(self.memory) < self.capacity:
            self.memory.append(transition)
        else:
            self.memory[self.position] = transition
        self.position = (self.position + 1) % self.capacity
        self._version += 1

    def extend(self, transitions):
        for transition in transitions:
            self.push(transition)

    # -- 读取 ---------------------------------------------------------------
    def __len__(self):
        return len(self.memory)

    def is_ready(self, batch_size=1):
        return len(self.memory) >= max(1, int(batch_size))

    def sample(self, batch_size):
        batch_size = max(1, int(batch_size))
        size = len(self.memory)
        if size == 0:
            return []
        if batch_size >= size:
            return list(self.memory)
        return self._rng.sample(self.memory, batch_size)

    def split_indices(self):
        """返回 (人为样本下标列表, 自主样本下标列表)，带版本缓存。

        采集线程每步只 push 一次，而训练线程可能高频采样，
        因此缓存只在“经验池内容变化后第一次采样”时重建。
        """
        if self._cache_version == self._version:
            return self._human_slots, self._ai_slots

        human = []
        auto = []
        for index, transition in enumerate(self.memory):
            if getattr(transition, "human_action", None) is not None:
                human.append(index)
            else:
                auto.append(index)
        self._human_slots = human
        self._ai_slots = auto
        self._cache_version = self._version
        return human, auto

    def human_count(self):
        return len(self.split_indices()[0])

    def sample_mixed(self, batch_size, human_fraction=0.0, human_boost=1.0):
        """按比例混合“人为示范”和“自主探索”样本。

        `human_boost > 1` 时，人为样本占用的配额会被放大，
        等价于“同样条数的人为示范被反复利用更多次”。
        """
        batch_size = max(1, int(batch_size))
        human_slots, ai_slots = self.split_indices()
        total = len(self.memory)
        if total == 0:
            return []
        if batch_size >= total:
            return list(self.memory)

        wanted_human = 0
        if human_slots and human_fraction > 0:
            wanted_human = int(round(batch_size * float(human_fraction) * max(1.0, float(human_boost))))
            wanted_human = min(wanted_human, batch_size, len(human_slots))

        picked = []
        if wanted_human:
            picked.extend(self._rng.sample(human_slots, wanted_human))

        remaining = batch_size - len(picked)
        if remaining > 0:
            if len(ai_slots) >= remaining:
                picked.extend(self._rng.sample(ai_slots, remaining))
            else:
                picked.extend(ai_slots)
                remaining = batch_size - len(picked)
                if remaining > 0:
                    picked_set = set(picked)
                    pool = [index for index in range(total) if index not in picked_set]
                    if not pool:
                        pool = list(range(total))
                    if remaining >= len(pool):
                        picked.extend(pool[:remaining])
                    else:
                        picked.extend(self._rng.sample(pool, remaining))

        # 下标抽样后可能重复（human 与 ai 互斥，不会重复；这里兜底）
        seen = set()
        transitions = []
        for index in picked:
            if index in seen:
                continue
            seen.add(index)
            transitions.append(self.memory[index])
        while len(transitions) < batch_size:
            transitions.append(self.memory[self._rng.randrange(total)])
        return transitions

    # -- 观测 ---------------------------------------------------------------
    def state_bytes(self):
        total = 0
        for transition in self.memory:
            for value in (transition.state, transition.next_state):
                nbytes = getattr(value, "nbytes", None)
                if nbytes is not None:
                    total += int(nbytes)
                elif value is not None:
                    try:
                        total += int(value.numel() * value.element_size())
                    except AttributeError:
                        pass
        return total

    def stats(self):
        human_slots, ai_slots = self.split_indices()
        from vram_manager import human_bytes

        return {
            "size": len(self.memory),
            "capacity": self.capacity,
            "human_samples": len(human_slots),
            "auto_samples": len(ai_slots),
            "human_ratio": round(len(human_slots) / max(1, len(self.memory)), 4),
            "state_bytes": self.state_bytes(),
            "state_human": human_bytes(self.state_bytes()),
        }
