# 高交互应用流量拨测改造说明

## 1. 目标

这个项目的目标不是把某一款游戏自动打得更强，而是把高交互、低时延应用当成被测对象，自动产生多样化操作，并观察这些操作是否成功激发了对应的界面变化与网络流量。

当前阶段的目标可以概括为四点：

1. 让动作更连续、更高频，减少“站桩”和无效乱点。
2. 让奖励尽量在当前 step 内返回，缩短动作与反馈之间的时间差。
3. 把训练目标从“赢得对局”转向“覆盖更多有效交互并激发更多可观测流量”。
4. 保留可扩展接口，后续能把更专业的抓包、RTT、抖动、丢包指标接进来。

## 2. 当前架构

一次 step 的执行链路如下：

```text
策略网络输出动作
        |
        v
动作合法化与节流
        |
        v
动作前流量快照
        |
        v
并发执行移动 / 信息 / 攻击动作
        |
        v
短暂观测等待
        |
        v
截图 + 动作后流量快照
        |
        v
奖励计算
        |
        v
写入经验池 + 异步记录 step_metrics.jsonl
```

关键文件：

| 文件 | 作用 |
| --- | --- |
| `wzry_env.py` | 负责 step 时序、动作整理、流量窗口采样 |
| `getReword.py` | 负责奖励计算 |
| `traffic_probe.py` | 负责流量探针抽象与 ADB 采样实现 |
| `metrics_logger.py` | 负责异步记录每一步结构化日志 |
| `train.py` | 负责串联环境、探针、日志与经验池 |

## 3. 这次改造解决了什么问题

### 3.1 原有问题

原始版本存在几个会明显影响强化学习效果的问题：

1. 动作发出后立刻截图，奖励经常不是当前动作造成的。
2. 每一步同时随机发送多类动作，动作之间互相干扰，训练样本噪声很大。
3. 奖励主要依赖攻击、死亡、胜负等稀疏事件，缺少 step 级即时反馈。
4. 没有把“交互是否真的激发了流量”纳入目标函数。
5. 经验池里存的是网络原始输出动作，而不是实际执行动作，容易把奖励学到错误动作上。

### 3.2 已完成改造

当前版本已经完成以下改造：

1. **动作与反馈重新对齐**
   - 动作执行完成后，再等待一个短窗口截取下一帧。
   - 每步记录 `step_latency_ms`。

2. **动作更接近真实交互**
   - 移动保持高频。
   - 信息类动作设置冷却步数。
   - 同一步只允许一个辅助动作，减少冲突。
   - 对无效参数进行清洗。

3. **即时奖励塑形**
   - 引入画面变化奖励、交互奖励、动作新颖度奖励、连续移动奖励。
   - 降低胜负、死亡等终局事件对训练方向的支配程度。

4. **流量探针接入**
   - 当前默认使用 `ADB + /proc/net/dev` 读取设备网卡字节数和包数。
   - 每个 step 都采集动作前后差分。
   - 流量指标被并入 reward。

5. **结构化日志**
   - 每一步输出到 `src/step_metrics.jsonl`。
   - 日志异步写入，尽量减少对在线交互时序的影响。

6. **带先验的探索动作**
   - 不再在探索期对所有动作做完全均匀采样。
   - 普攻被设为高频动作，技能保持中频，升级动作保留但降为低频。
   - 这样更接近真实玩家操作，也能减少早期经验池被无意义随机动作占满。

## 4. 奖励函数设计

总奖励由三部分组成：

```text
total_reward = probe_reward + traffic_reward + event_reward
```

### 4.1 `probe_reward`

这部分面向“是否产生了有效交互”：

- `frame_change_score`
  - 动作前后画面差异越明显，奖励越高。
- `interaction_bonus`
  - 只要本步确实执行了动作，就给少量正奖励。
- `novelty_bonus`
  - 新动作组合第一次出现时给奖励，鼓励覆盖更多操作模式。
- `move_bonus`
  - 鼓励移动。
- `smooth_move_bonus`
  - 连续方向相近时奖励，帮助改善走位。
- `reverse_turn_penalty`
  - 大角度来回折返时惩罚，减少抖动。
- `ineffective_action_penalty`
  - 有动作但几乎没有任何画面变化时惩罚。

### 4.2 `traffic_reward`

这部分面向“是否真正激发了网络交互”：

```text
bytes_score   = min(total_bytes   / traffic_bytes_target,   1)
packets_score = min(total_packets / traffic_packets_target, 1)
activity      = 0.65 * bytes_score + 0.35 * packets_score
```

如果本步有动作并且流量可用：

- `traffic_reward_weight * activity`
- 若动作后没有观察到任何流量，则施加 `traffic_inactive_penalty`

此外，当前版本还把 `step_latency_ms` 作为一个轻量的响应质量代理信号：

```text
latency_score = max(0, 1 - step_latency_ms / latency_target_ms)
```

这不是严格意义上的网络 RTT，只是当前阶段对“交互是否及时返回”的近似刻画。后续接入真实 RTT、抖动、丢包后，应逐步降低这部分代理指标的权重。

### 4.3 `event_reward`

这部分保留少量游戏内事件信号：

- `attack`
- `death`
- `successes`
- `failed`

这些信号仍然有用，但不再作为训练主目标。这样能避免模型为了赢一局而牺牲交互覆盖度。

## 5. 流量探针设计

### 5.1 当前实现

当前实现位于 `traffic_probe.py`：

- `NullTrafficProbe`
  - 关闭流量拨测时使用。
- `AdbProcNetDevTrafficProbe`
  - 通过 ADB 读取设备 `/proc/net/dev`。
  - 统计接口的：
    - `rx_bytes`
    - `tx_bytes`
    - `rx_packets`
    - `tx_packets`

### 5.2 为什么先用 `/proc/net/dev`

优点：

- 接入快。
- 不需要修改游戏或系统网络路径。
- 能先把“动作 -> 流量 -> 奖励”这条链路打通。

限制：

- 这是网卡级别统计，不是应用级别统计。
- 不能直接得到 RTT、抖动、丢包。
- 如果设备后台流量很多，会引入一定噪声。

### 5.3 后续推荐升级方向

优先级从高到低建议如下：

1. **VPN / TUN 代理**
   - 可做到按应用或五元组聚合。
   - 能拿到更接近真实业务流的统计。

2. **tcpdump / pcap**
   - 适合研究和离线分析。
   - 可以进一步提取：
     - 包间隔
     - 突发段
     - 重传
     - RTT 估计

3. **旁路镜像或交换机采集**
   - 适合规模化部署。
   - 与终端侧自动化解耦。

当前代码只要求探针最终返回统一的 `traffic_metrics` 字典，因此替换 backend 时，不需要改 `Environment` 和奖励主逻辑。

## 6. 新增参数

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `--observation_delay_sec` | `0.08` | 动作完成后等待多久再截图 |
| `--info_action_cooldown_steps` | `8` | 信息类动作的冷却步数 |
| `--move_swipe_duration_ms` | `120` | 移动滑动时长 |
| `--skill_swipe_duration_ms` | `120` | 技能滑动时长 |
| `--long_press_unit_ms` | `150` | 长按动作基础时长 |
| `--finish_check_interval_steps` | `5` | OCR 终局检测频率 |
| `--traffic_probe_mode` | `adb_proc_net_dev` | 流量探针类型 |
| `--traffic_probe_interfaces` | 空 | 需要统计的网卡，逗号分隔 |
| `--traffic_probe_timeout_sec` | `1.0` | 流量探针超时 |
| `--traffic_bytes_target` | `12000` | 字节奖励饱和阈值 |
| `--traffic_packets_target` | `24` | 包数奖励饱和阈值 |
| `--traffic_reward_weight` | `0.8` | 流量奖励权重 |
| `--traffic_inactive_penalty` | `0.1` | 有动作但无流量时的惩罚 |
| `--latency_target_ms` | `350.0` | 响应延迟代理目标 |
| `--latency_reward_weight` | `0.2` | 响应延迟奖励权重 |
| `--step_metrics_path` | `src/step_metrics.jsonl` | 逐步日志输出文件 |

## 7. 日志字段

每一步都会记录一行 JSON，常用字段如下：

| 字段 | 含义 |
| --- | --- |
| `reward` | 总奖励 |
| `probe_reward` | 画面与交互奖励 |
| `traffic_reward` | 流量奖励 |
| `event_reward` | 游戏事件奖励 |
| `step_latency_ms` | 当前 step 总耗时 |
| `frame_change` | 动作前后画面变化 |
| `novel_action` | 是否属于近期未出现的新动作 |
| `smooth_move` | 本步移动是否连续平滑 |
| `traffic_available` | 本步是否成功读取到流量 |
| `traffic_activity_score` | 流量激活得分 |
| `traffic` | 原始流量差分数据 |
| `raw_action` | 网络原始动作 |
| `effective_action` | 实际执行动作 |

这些日志可直接用于后续分析：

- 哪类动作最容易激发上行/下行流量
- 哪类动作画面变化大但流量弱
- 哪类动作有流量但交互成功率低
- 流量奖励是否被后台噪声污染
- 调参后走位、覆盖度、即时性是否改善

## 8. 使用建议

### 8.1 第一阶段建议

先固定以下条件：

1. 设备后台尽量干净。
2. 只保留游戏相关网络。
3. 先用默认参数跑一段。
4. 查看 `step_metrics.jsonl` 里：
   - `traffic_available`
   - `traffic_activity_score`
   - `frame_change_score`
   - `step_latency_ms`

### 8.2 调参建议

如果出现以下现象：

- **动作很多，但流量奖励偏低**
  - 降低 `traffic_bytes_target`
  - 降低 `traffic_packets_target`

- **后台流量导致奖励虚高**
  - 指定 `--traffic_probe_interfaces`
  - 或尽快换成更精细的流量后端

- **角色走位仍然抖**
  - 提高 `smooth_move_bonus`
  - 提高 `reverse_turn_penalty`

- **交互次数仍然偏少**
  - 提高 `interaction_bonus`
  - 提高 `novelty_bonus`
  - 检查 `info_action_cooldown_steps` 是否过大

- **早期探索阶段太不像真人**
  - 调整 `dqnAgent.py` 里的 `_sample_exploration_action()`
  - 默认版本中，普攻已被设为最高频探索动作

- **每步太慢**
  - 先观察 `step_latency_ms`
  - 适当降低 `observation_delay_sec`
  - 关闭或放宽部分高代价检测

## 9. 适配其他高交互应用

要迁移到其他游戏或高交互应用，建议分三层改：

1. **动作映射层**
   - 改按钮坐标、滑动区域、可执行动作集合。

2. **状态识别层**
   - 改终局识别、死亡识别、关键 UI 识别。

3. **探针层**
   - 默认仍可先沿用通用流量接口。
   - 如果目标不是游戏，而是直播、云游戏、短视频、协同编辑等应用，可以直接替换动作模板和界面反馈规则。

强化学习框架、流量探针接口、日志格式都可以继续复用。

## 10. 后续工作建议

建议下一步按这个顺序推进：

1. 增加一个基于真实抓包的 backend。
2. 加入按动作类型统计的离线分析脚本。
3. 把流量特征扩展到：
   - 首包响应时间
   - 突发持续时长
   - 包间隔方差
   - 重传率
   - RTT / jitter / loss
4. 将当前 DQN 逐步升级为更适合复杂动作空间的算法，例如：
   - Branching DQN
   - PPO
   - 带参数化动作空间的 actor-critic

这样项目会从“自动玩某个游戏”真正演进成“面向高交互应用的自动化流量拨测框架”。
