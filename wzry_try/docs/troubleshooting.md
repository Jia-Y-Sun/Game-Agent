# 排错手册

按现象查。每条都给了判断依据和具体参数。

---

## 1. 连不上设备

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `adb devices` 空 | 模拟器没开 ADB / 端口不对 | 打开模拟器 ADB 调试；`adb connect 127.0.0.1:5555`（多开 5557/5559…） |
| `adb server version doesn't match` | 用了不同版本的 adb | 全程只用 `.\scrcpy-win64-v2.0\adb.exe`；先 `kill-server` |
| 状态 `offline` | 连接半死 | `adb disconnect` → `adb connect` → 仍不行重启模拟器 |
| 读取设备分辨率失败 | 设备 `offline` 或没授权 | 先修好 `adb devices`；真机确认 USB 调试授权弹窗已点允许 |

---

## 2. 截图 / 窗口

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `窗口 'wzry_ai' 未找到` | scrcpy 没起来 / 标题不一致 / 被最小化 | 结束残留 `scrcpy.exe` 再启动；`--window_title` 要与 scrcpy 一致；不要最小化 |
| 截图全黑或尺寸不对 | scrcpy 窗口被缩放 / 被遮挡 | 不要用 scrcpy 的缩放；重启 scrcpy；保持窗口可见 |
| 启动时一行红字 `scrcpy-server: 1 file pushed` | adb 的 stderr | 忽略，不影响使用 |

---

## 3. 动作不对（摇杆 / 按钮）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 人物完全不动 | `input motionevent` 不被接受 | 看日志有没有 `fallback to persistent_center_swipe`；或 `--move_control_mode center_swipe` |
| 走的方向偏 90° | 摇杆中心量错（量到按钮区） | 用 `showposition.py` 重量 `--move_joystick_center_x/y` |
| 方向乱 / 原地转圈 | 半径太大，推过死区 | 按 `radius ≈ 0.17 × 屏幕短边` 调小（1080→184，900→155） |
| 技能点空 / 买错装备 | 按钮比例与当前分辨率不符 | `showposition.py` 重新对照 `argparses.py` 的 `attack_actions_detail` / `info_actions_detail` |
| 一直低血量乱撤 | 血量 ROI 比例不匹配 | 调 `--phase_self_hp_expected_width_ratio`（0.07~0.10）、`--phase_low_hp_confirm_steps 5` |
| 从不识别敌人 | 红血条面积阈值不匹配 | `--target_tracking_min_area 16 --target_tracking_max_area 2500`（按截图像素缩放） |
| 走路走过头 / 撞塔 | 开局纯移动步数太多 | `--lane_opening_min_move_steps 65`；或开 `--lane_opening_stop_on_contact true` |

---

## 4. 人为辅助采不到

| 现象 | 判断 | 处理 |
| --- | --- | --- |
| `assist` 一直 `idle`、`human=n` | 触摸没进 `getevent` | 加 `--assist_debug_touch true` 看有没有 `[touch] raw#...` 行 |
| 有 `[touch]` 行但没有动作 | 坐标变换没配对 | 调 `--touch_swap_xy / --touch_invert_x / --touch_invert_y`；或用 `relabel_human_actions.py` 重标注 |
| `touch_event_diagnose.py` 探测 0 行 | 该模拟器的鼠标注入不走 evdev | 见 [human-assist.md](human-assist.md#7-采不到触摸时怎么办) 的替代方案（真机采示范） |
| 启动日志有 `未能启动 adb getevent 触摸监听` | 触摸设备路径错或设备没就绪 | 用诊断脚本确认 `/dev/input/eventN`，再填 `--touch_device` |
| 采集到但 Y 一直是旧值/解析不出 | 模拟器对重复坐标做了去重 | 真实手指不会出现；用注入方式测试时要让坐标有微小变化 |
| 接管有半秒延迟 | 当前 burst 还没结束 | `--assist_preempt_burst_duration_ms 220`，必要时同时降 `--action_burst_duration_ms` |

---

## 5. 训练看起来不对

| 现象 | 判断依据 | 处理 |
| --- | --- | --- |
| `loss` 变成 1e7 级 | Q 自举发散 | 确认 `--td_target_clip 50`（不要设 0）；`--grad_clip_norm 10` |
| `q` 与 `target` 都卡在同一个整数 | 撞上 TD 裁剪上限 | 检查 `--reward_scale`（默认 0.1）与 checkpoint 的尺度是否一致；不一致会有启动警告，按提示换新模型或沿用旧尺度 |
| ε 几十步就掉到 0.01 | 用了 `--epsilon_decay_mode replay` | 改回默认 `step` |
| `training` 每秒几十条 | `--replays_per_env_step` 设成 0 或很大 | 保持默认 4 |
| 长期 `src=smart_prior` | ε 还很高（冷启动正常）或模型没学到 | 看 `eps=` 是否在下降；必要时先跑行为克隆给初值 |
| 动作熵很低、只会一个方向 | 规则层一直覆盖模型 | 看 `macro=` 是否长期是 `opening_*`；可降 `--lane_opening_min_move_steps` 或 `--lane_macro_control_steps` |
| 对局结束后还在动 | 没触发结束判据 | 确认 `--episode_end_enabled true`；看日志有没有 `对局结束(traffic_idle)` |

---

## 6. 显存 / 性能

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 显存不够（默认 legacy 约 5GB） | 8GB 卡还要同时跑模拟器 | `--batch_size 32 --onnx_provider cpu --ocr_provider cpu`；或切 `--net_arch compact --obs_size 160`（159MB） |
| 显存不够（4GB 卡） | legacy 固定成本就 4.6GB | 必须切 `--net_arch compact --obs_size 160`，或 `--device_id cpu` |
| `reserved` 一直涨 | 分配器缓存 | `--vram_empty_cache_interval_steps 50`；看 `alloc` 是否稳定 |
| 单步很慢 | AMP 策略不匹配 / 模拟器掉帧 | legacy 保持 AMP 自动（开）；compact 用 `--amp false`；给模拟器 4 核 4GB |
| 训练把 CPU 吃满 | ONNX 线程多 | `--onnx_intra_op_threads 2` |
| 内存被经验池吃光 | 640 观测每条 2.34MB | 调低 `--memory_ram_limit_gb`（默认 8，约 3495 条） |

---

## 7. 其它

| 现象 | 处理 |
| --- | --- |
| 终端刷屏太快 | `--step_info_log false --step_timing_debug false --action_preview_enabled false` |
| 指标文件越来越大 | `--step_metrics_max_mb 32`（超了自动轮转）或 `--step_metrics_enabled false` |
| 一直在打印 `对局未开始` | 还没进对局，属正常；进对局后自动开始 |
| 想看单局统计 | `python analyze_episode.py --range 0:N`（N 用结束判据命中的步数） |
| 想确认代码没坏 | `python test_assist_pipeline.py --device cpu`（应 100 项全过） |
