"""整场人为辅助训练链路的离线自检。

不需要手机、不需要 adb、不需要显卡，验证的是“数据 -> profile -> 训练”这条新链路：

1. `assist_dataset` 的写入/读取/预卷帧保留/旧格式兼容；
2. `memory` 的人为样本混合采样；
3. `live_human_assist` 的触摸解析与接管状态机（用假的 getevent 数据驱动）；
4. `build_game_profile` 的整场 profile 生成 + `GameProfilePrior` 回放；
5. `net_actor` 的结构识别与旧权重迁移；
6. `dqnAgent` 的观测缩放与一次真实 training step（CPU）。

默认跑在 CPU 上，避免占用正在训练用的显存：

```powershell
python test_assist_pipeline.py
python test_assist_pipeline.py --device cuda:0
```

任何一项失败都会返回非 0，可以直接放进 CI 或提交前自检。
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time

import numpy as np

# 必须在 import 项目模块之前固定命令行，否则 argparses 会读走本脚本的参数
_REAL_ARGV = list(sys.argv[1:])
_PRE_ARGS = []
for _index, _value in enumerate(sys.argv):
    if _value == "--device" and _index + 1 < len(sys.argv):
        _PRE_ARGS = ["--device_id", sys.argv[_index + 1]]

# 默认结构是 legacy(1.99 亿参数 / 640 输入)，自检跑在 CPU 上会非常慢；
# 这里固定用 compact 小网络，保证几十秒内跑完。
_PRE_ARGS += [
    "--model_path", "no_such_warm_start_model.pt",
    "--net_arch", "compact",
    "--obs_size", "160",
]
sys.argv = ["test_assist_pipeline"] + _PRE_ARGS

import assist_dataset as ads  # noqa: E402
import build_game_profile  # noqa: E402
import game_profile as gp  # noqa: E402
import live_human_assist as lha  # noqa: E402
import memory  # noqa: E402
from argparses import args, globalInfo  # noqa: E402
from dqnAgent import DQNAgent  # noqa: E402
from net_actor import NetDQN, detect_arch_from_state_dict, load_state_dict_flexible  # noqa: E402

RESULTS = []

#: 自检临时目录固定在工作区内，避免被沙箱/杀软拦住平台临时目录
_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".selftest_tmp")


def _mkdtemp(prefix="run_"):
    """在工作区内建一个唯一临时目录。

    注意不要用 `tempfile.mkdtemp()`：它在 Windows 上按 0o700 建目录，
    在带沙箱/受限权限的环境里，之后再往这个目录下面建子目录会被拒绝。
    """
    os.makedirs(_TMP_ROOT, exist_ok=True)
    base = os.path.join(_TMP_ROOT, f"{prefix}{int(time.time() * 1000)}_{os.getpid()}")
    candidate = base
    suffix = 0
    while os.path.exists(candidate):
        suffix += 1
        candidate = f"{base}_{suffix}"
    os.makedirs(candidate, exist_ok=True)
    return candidate


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(condition)


# --------------------------------------------------------------------------- #
# 1. 数据集
# --------------------------------------------------------------------------- #
def test_dataset(tmpdir):
    print("\n== 1. assist_dataset ==")
    writer = ads.create_episode_writer(
        root_dir=os.path.join(tmpdir, "data"),
        episode_id="ep1",
        frame_policy="human",
        frame_max_width=160,
        preroll_steps=2,
        metadata={"note": "selftest"},
    )
    frame = np.zeros((90, 160, 3), dtype=np.uint8)
    for step in range(6):
        frame[:] = step * 20
        record = ads.make_record(
            step=step,
            ts=1000.0 + step * 0.5,
            act=[1, 300 + step, 0, 1, 0, 0, 0, 0],
            ai_act=[1, 300, 0, 1, 0, 0, 0, 0],
            human_act=[1, 300 + step, 0, 1, 0, 0, 0, 0] if step == 2 else None,
            human_present=(step == 2),
            human_override=(step == 2),
            phase="laning" if step >= 3 else "opening",
            reward=0.1 * step,
            obs={"hp": 0.8, "pressure": 0.2, "target_dist": 0.3},
            episode_id="ep1",
        )
        writer.write(record, frame=frame)
    writer.close()
    stats = writer.stats()

    check("writer 写满 6 步", stats["written_steps"] == 6, str(stats))
    samples_path = writer.samples_path
    records = ads.load_records(samples_path)
    check("reader 读回 6 条", len(records) == 6, f"{len(records)} 条")

    human_steps = [item for item in records if item["human_present"]]
    check("人为步被标记", len(human_steps) == 1, f"{len(human_steps)} 步")

    framed = [item for item in records if item["frame_saved"]]
    check("预卷帧被保留（人为步 + 前 2 步）", len(framed) == 3, f"{len(framed)} 帧")
    check("帧文件真实存在", all(os.path.exists(item["frame_path"]) for item in framed))

    summary = ads.summarize_records(records)
    check("统计里 human_ratio 正确", abs(summary["human_ratio"] - 1 / 6) < 2e-4, str(summary["human_ratio"]))

    # 旧格式兼容
    legacy_path = os.path.join(tmpdir, "legacy", "samples.jsonl")
    os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
    with open(legacy_path, "w", encoding="utf-8") as file:
        for step in range(3):
            file.write(json.dumps({
                "frame_path": "",
                "parsed_action": [1, 200, 0, 9, 1, 200, 40, 0],
                "timestamp": 1.0 + step,
                "touch_samples": [{"raw_x": 1, "raw_y": 2}],
            }) + "\n")
    legacy_records = ads.load_records(legacy_path)
    check("旧 samples.jsonl 兼容", len(legacy_records) == 3 and legacy_records[0]["human_act"] is not None,
          f"{len(legacy_records)} 条, pattern={legacy_records[0]['pattern'] if legacy_records else '-'}")
    return samples_path


# --------------------------------------------------------------------------- #
# 2. 经验池
# --------------------------------------------------------------------------- #
def test_memory():
    print("\n== 2. ReplayMemory / 混合采样 ==")
    replay = memory.ReplayMemory(capacity=200, seed=7)
    obs = np.zeros((16, 16, 3), dtype=np.uint8)
    for index in range(100):
        human = [1, 90, 0, 8, 1, 90, 40, 0] if index < 12 else None
        replay.push(obs, [1, index % 360, 0, 1, 0, 0, 0, 0], 0.1, obs, 0, human, 1.0 if human else 0.0)

    check("容量与长度一致", len(replay) == 100 and replay.capacity == 200, f"{len(replay)}/{replay.capacity}")
    check("人为样本计数", replay.human_count() == 12, str(replay.human_count()))

    batch = replay.sample_mixed(20, human_fraction=0.5)
    human_in_batch = sum(1 for item in batch if item.human_action is not None)
    check("混合采样命中人为配额", human_in_batch == 10, f"{human_in_batch}/20")
    check("混合采样批次长度正确", len(batch) == 20, str(len(batch)))
    stats = replay.stats()
    check("内存估算可用", stats["state_bytes"] > 0, stats["state_human"])


# --------------------------------------------------------------------------- #
# 3. 实时接管
# --------------------------------------------------------------------------- #
def _touch_lines(raw_x, raw_y, tracking_id=1):
    return [
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_SLOT              00000000",
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_TRACKING_ID       0000000{tracking_id}",
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_POSITION_X        {raw_x:08x}",
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_POSITION_Y        {raw_y:08x}",
        f"[   100.000000] /dev/input/event6: EV_SYN       SYN_REPORT               00000000",
    ]


def _release_lines(raw_x, raw_y):
    return [
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_SLOT              00000000",
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_TRACKING_ID       ffffffff",
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_POSITION_X        {raw_x:08x}",
        f"[   100.000000] /dev/input/event6: EV_ABS       ABS_MT_POSITION_Y        {raw_y:08x}",
        f"[   100.000000] /dev/input/event6: EV_SYN       SYN_REPORT               00000000",
    ]


def test_live_assist(tmpdir):
    print("\n== 3. LiveHumanAssist 接管状态机 ==")
    assist = lha.LiveHumanAssist(tool=None, screen_size=(2800, 1260), mode="assist", start_stream=False)
    stream = lha.HumanTouchStream(adb_path="adb", device_serial="fake", auto_start=False)
    assist.stream = stream

    # 摇杆中心 (0.173*2800, 0.784*1260) ≈ (484, 988)，往右拖 200px -> angle≈0
    now = time.time()
    for line in _touch_lines(684, 988):
        stream.feed_line(line, now)

    observation = assist.poll()
    check("按下摇杆被识别为人为操作", observation.present, repr(observation))
    check("触摸被解析成移动动作", observation.action[0] == 1, f"action={observation.action}")
    check("移动角度接近 0 度", min(abs(observation.action[1]), 360 - abs(observation.action[1])) <= 8,
          f"angle={observation.action[1]}")

    # 触摸还原必须跟随 --move_joystick_center_*（否则标定过的摇杆会被误判方向）
    saved_center = (args.move_joystick_center_enabled, args.move_joystick_center_x, args.move_joystick_center_y)
    try:
        from touch_event_parser import TouchActionMapper
        args.move_joystick_center_enabled = True
        args.move_joystick_center_x, args.move_joystick_center_y = 0.30, 0.784
        mapper = TouchActionMapper(screen_width=2800, screen_height=1260)
        centered = [{"host_ts": 1.0, "raw_x": int(2800 * 0.30), "raw_y": 988, "active": True,
                     "tracking_id": 1, "slot": 0, "phase": "move"}]
        action_at_center, _ = mapper.samples_to_action(centered)
        pushed = [{"host_ts": 1.0, "raw_x": int(2800 * 0.30) + 200, "raw_y": 988, "active": True,
                   "tracking_id": 1, "slot": 0, "phase": "move"}]
        action_pushed, _ = mapper.samples_to_action(pushed)
        check("摇杆中心处不产生移动", action_at_center[0] == 0, str(action_at_center))
        check("新摇杆中心下向右推 = 0 度",
              action_pushed[0] == 1 and min(action_pushed[1], 360 - action_pushed[1]) <= 8, str(action_pushed))
    finally:
        (args.move_joystick_center_enabled, args.move_joystick_center_x, args.move_joystick_center_y) = saved_center

    ai_action = [1, 180, 0, 1, 0, 0, 0, 0]
    learned, inject, human_control, info = assist.arbitrate(ai_action, observation)
    check("接管时 learned = 人的动作", learned[1] == observation.action[1], f"learned={learned}")
    check("接管时不注入任何操作", inject == [0] * 8, f"inject={inject}")
    check("接管时 human_control=True", human_control, str(info.get("assist_state")))
    check("接管计数 +1", info["assist_episode_takeovers"] == 1, str(info["assist_episode_takeovers"]))

    # 松手
    for line in _release_lines(684, 988):
        stream.feed_line(line, time.time())
    observation = assist.poll()
    check("松手后 present=False", not observation.present, repr(observation))

    learned, inject, human_control, info = assist.arbitrate(ai_action, observation)
    check("释放窗口内仍不让 AI 注入", human_control and inject == [0] * 8, info.get("assist_state"))

    for _ in range(int(args.assist_release_grace_steps) + 2):
        learned, inject, human_control, info = assist.arbitrate(ai_action, assist.poll())
    check("超过释放等待后交还 AI", (not human_control) and inject == ai_action, info.get("assist_state"))

    # manual 模式：整场都不注入
    manual = lha.LiveHumanAssist(tool=None, screen_size=(2800, 1260), mode="manual", start_stream=False)
    learned, inject, human_control, info = manual.arbitrate(ai_action, lha.HumanObservation())
    check("manual 模式始终接管", human_control and inject == [0] * 8, info.get("assist_state"))

    # 数据集挂载
    writer = ads.create_episode_writer(
        root_dir=os.path.join(tmpdir, "live_data"), episode_id="live", frame_policy="none"
    )
    assist.attach_dataset(writer)
    record = assist.record_step(
        step=3,
        frame=np.zeros((120, 200, 3), dtype=np.uint8),
        learned_action=[1, 42, 0, 8, 0, 0, 0, 0],
        ai_action=[1, 200, 0, 1, 0, 0, 0, 0],
        human_action=[1, 42, 0, 8, 0, 0, 0, 0],
        human_present=True,
        human_control=True,
        phase_info={"phase": "combat", "phase_action_name": "trade_target"},
        lane_info={"lane_macro_phase": "laning", "lane_macro_name": "laning_trade_attack"},
        minimap_state={"minimap_lane_arrived": True},
        info={"event": None},
        reward=0.5,
        done=0,
    )
    check("record_step 返回记录", record is not None and record["phase"] == "combat", str(record and record["phase"]))
    writer.close()


# --------------------------------------------------------------------------- #
# 4. 整场 profile
# --------------------------------------------------------------------------- #
def _synthetic_episode(writer, steps=180):
    phase_angles = {
        "opening": 0,
        "laning": 175,
        "combat": 30,
        "retreat": 210,
        "recover": 90,
    }
    for step in range(steps):
        if step < 40:
            phase = "opening"
        elif step < 60:
            phase = "retreat"
        elif step < 70:
            phase = "recover"
        elif step % 25 < 8:
            phase = "combat"
        else:
            phase = "laning"
        angle = phase_angles[phase]
        attack = 0
        if phase == "combat":
            attack = 8 if step % 4 == 0 else 1
        elif phase == "laning":
            attack = 2 if step % 5 == 0 else 1
        info = 6 if step % 17 == 0 else 0
        action = [1, angle, info, attack, 1 if attack >= 8 else 0, angle if attack >= 8 else 0,
                  40 if attack >= 8 else 0, 0]
        writer.write(ads.make_record(
            step=step,
            ts=2000.0 + step * 0.9,
            act=action,
            ai_act=[1, 0, 0, 1, 0, 0, 0, 0],
            human_act=action,
            human_present=True,
            human_override=True,
            phase=phase,
            lane_phase="opening" if phase == "opening" else "laning",
            reward=0.05,
            obs={"hp": 0.9 if phase != "retreat" else 0.2, "pressure": 0.3, "target_dist": 0.25},
            episode_id="synthetic",
        ))


def test_game_profile(tmpdir):
    print("\n== 4. build_game_profile / GameProfilePrior ==")
    source_dir = os.path.join(tmpdir, "profile_data")
    writer = ads.create_episode_writer(root_dir=source_dir, episode_id="synthetic", frame_policy="none")
    _synthetic_episode(writer)
    writer.close()

    out_path = os.path.join(tmpdir, "game_profile.json")
    code = build_game_profile.main([
        "--assist_source", source_dir,
        "--out", out_path,
        "--opening_steps", "40",
        "--profile_step_sec", "0",
    ])
    check("build_game_profile 正常返回", code == 0, f"exit={code}")
    check("profile 文件已生成", os.path.exists(out_path))

    with open(out_path, "r", encoding="utf-8") as file:
        profile_json = json.load(file)

    check("profil 含 phases", bool(profile_json.get("phases")), str(sorted(profile_json.get("phases", {}))))
    check("profil 含 groups", bool(profile_json.get("groups")), str(sorted(profile_json.get("groups", {}))))
    check("profil 兼容 lane_profile 字段",
          bool(profile_json.get("route_angles")) and bool(profile_json.get("route_steps")),
          f"{profile_json.get('route_angles')} / {profile_json.get('route_steps')}")
    check("profil 记录覆盖时长", profile_json.get("minutes_covered", 0) > 2,
          str(profile_json.get("minutes_covered")))

    loaded = gp.GameProfile.load(out_path)
    check("GameProfile 可加载", loaded.available, str(loaded.describe()))

    prior = gp.GameProfilePrior(loaded, seed=3)
    sampled = [prior.sample("combat") for _ in range(20)]
    check("先验在每个阶段都能出动作", all(item is not None for item in sampled),
          f"样例={sampled[0]}")
    check("先验动作合法", all(0 <= item[1] < 360 and item[0] in (0, 1) for item in sampled if item))

    full_base = [1, 90, 1, 1, 0, 0, 0, 0]
    filled = prior.sample("laning", only_fill=True, base_action=full_base)
    check("fill 模式完全不覆盖 base 已有分支", filled == full_base, f"{filled} vs {full_base}")
    partial_base = [1, 123, 0, 0, 0, 0, 0, 0]
    filled_partial = prior.sample("laning", only_fill=True, base_action=partial_base)
    check("fill 模式保留 base 的走位角度", filled_partial[0] == 1 and filled_partial[1] == 123,
          str(filled_partial))
    forced = prior.sample("laning", only_fill=False, base_action=partial_base)
    check("force 模式允许改写走位", forced[0] in (0, 1), str(forced))

    stats, source = loaded.stats_for("unknown_phase")
    check("未知阶段回退到组/全局统计", bool(stats), f"source={source}")

    # 未加载 profile 时必须安全降级
    empty_prior = gp.GameProfilePrior(gp.GameProfile())
    check("无 profile 时先验返回 None", empty_prior.sample("laning") is None)


# --------------------------------------------------------------------------- #
# 5. 网络结构与迁移
# --------------------------------------------------------------------------- #
def test_network():
    print("\n== 5. net_actor 结构识别与迁移 ==")
    legacy = NetDQN(arch="legacy", input_size=640)
    legacy_info = legacy.describe()
    check("legacy 结构参数量与原版一致", legacy_info["params"] == 199788562, f"{legacy_info['params']:,}")
    check("legacy fc 特征长度正确", legacy_info["feature_size"] == 778752, str(legacy_info["feature_size"]))

    compact = NetDQN(arch="compact", input_size=160)
    compact_info = compact.describe()
    check("compact 参数量 < 2M", compact_info["params"] < 2_000_000, f"{compact_info['params']:,}")
    check("compact 权重体积 < 8MB", compact_info["fp32_mb"] < 8, str(compact_info["fp32_mb"]))

    state_dict = legacy.state_dict()
    check("能从 checkpoint 识别 legacy", detect_arch_from_state_dict(state_dict) == "legacy")

    report = load_state_dict_flexible(compact, state_dict, verbose=False)
    check("旧 conv 权重可迁移", report["loaded"] >= 5, f"loaded={report['loaded']}, ratio={report['transferred_ratio']}")
    check("迁移时跳过 fc 但不报错", any(name == "fc.weight" for name, _ in report["skipped"]), "")

    import torch

    for size in (128, 160, 640):
        model = NetDQN(arch="compact", input_size=size).eval()
        with torch.no_grad():
            outputs = model(torch.zeros(2, 3, size, size))
        shapes = [tuple(output.shape) for output in outputs]
        check(
            f"compact 前向支持 {size}x{size}",
            outputs[0].shape == (2, 2) and outputs[1].shape == (2, 360) and outputs[7].shape == (2, 5),
            str(shapes),
        )


# --------------------------------------------------------------------------- #
# 6. 训练一步
# --------------------------------------------------------------------------- #
def test_training_step():
    print("\n== 6. dqnAgent 观测与一次训练 ==")
    agent = DQNAgent()
    frame = np.zeros((1260, 2800, 3), dtype=np.uint8)
    obs = agent.to_observation(frame)
    check("观测被压到 obs_size", obs.shape == (args.obs_size, args.obs_size, 3), str(obs.shape))
    check("观测是 uint8", obs.dtype == np.uint8, str(obs.dtype))

    batch_tensor = agent._obs_batch_tensor(np.stack([obs] * 4))
    check("批量观测张量形状正确", tuple(batch_tensor.shape) == (4, 3, args.obs_size, args.obs_size),
          str(tuple(batch_tensor.shape)))
    check("批量观测归一化到 0~1", float(batch_tensor.max()) <= 1.0 and float(batch_tensor.min()) >= 0.0, "")

    small = np.zeros((args.obs_size, args.obs_size, 3), dtype=np.uint8)
    for index in range(int(args.batch_size) + 8):
        human = [1, 90, 0, 8, 1, 90, 40, 0] if index % 5 == 0 else None
        globalInfo.store_transition_dqn(
            small,
            [1, index % 360, 0, 1, 0, 0, 0, 0],
            0.1,
            small,
            0,
            human,
            1.0 if human else 0.0,
        )

    info = agent.replay()
    check("replay 返回训练指标", isinstance(info, dict) and "loss" in info, str(info))
    check("loss 是有限值", info and np.isfinite(info["loss"]), str(info and info["loss"]))
    check("批次里包含人为样本", info and info["human_in_batch"] > 0,
          f"{info and info['human_in_batch']}/{info and info['batch_size']}")
    check("辅助模仿损失被计算", info and info["aux_loss"] > 0, str(info and info["aux_loss"]))

    memory_stats = globalInfo.dqn_memory_stats()
    check("经验池内存可估算", memory_stats["state_bytes"] > 0, memory_stats["state_human"])


# --------------------------------------------------------------------------- #
# 7. env.step 冒烟测试（假设备，不需要手机）
# --------------------------------------------------------------------------- #
class _FakeTool:
    """只实现 `Environment.step` 会用到的那一小部分接口。"""

    def __init__(self, width=426, height=240):
        self.actual_width = width
        self.actual_height = height
        self.device_serial = "fake-device"
        self.scrcpy_dir = "scrcpy-win64-v2.0"
        self.last_input_ts = 0.0
        self.calls = []

    def screenshot_window(self):
        frame = np.zeros((self.actual_height, self.actual_width, 3), dtype=np.uint8)
        frame[:, :, 1] = 40
        return frame

    def stop_persistent_move(self):
        self.calls.append("stop")

    def execute_action_burst(self, action, suppress_attack=False, suppress_info=False,
                             burst_duration_ms=None, human_control=False):
        self.calls.append(("burst", tuple(int(v) for v in action), bool(human_control)))
        if human_control:
            # 与真实实现一致：接管时一个操作都不注入
            return False, False, False
        return True, False, True

    def action_move(self, params):
        return _FakeFuture()

    def action_info(self, params):
        return _FakeFuture()

    def action_attack(self, params):
        return _FakeFuture()


class _FakeFuture:
    def result(self):
        return True


class _FakeReward:
    def __init__(self):
        self.step = 0

    def reset_episode(self):
        self.step = 0

    def get_reword(self, image, is_frame, action, previous_image=None, traffic_metrics=None,
                   step_latency_ms=None, target_info=None, phase_info=None):
        self.step += 1
        return 0.05, 0, {
            "event": None,
            "probe_reward": 0.0,
            "gameplay_reward": 0.0,
            "gameplay_action_score": 0.0,
            "traffic_reward": 0.0,
            "target_reward": 0.0,
            "phase_reward": 0.0,
            "phase_reward_score": 0.0,
            "event_reward": 0.0,
            "frame_change": 0.0,
            "frame_change_score": 0.0,
            "active_action_count": 1,
            "novel_action": False,
            "smooth_move": None,
            "ineffective_action": False,
            "traffic_available": False,
            "traffic_bytes_score": 0.0,
            "traffic_packets_score": 0.0,
            "traffic_activity_score": 0.0,
            "latency_score": 0.0,
            "target_visible": False,
            "target_angle": None,
            "target_distance_ratio": 0.0,
            "target_reward_score": 0.0,
        }


def test_env_step(tmpdir):
    print("\n== 7. Environment.step 冒烟测试 ==")
    import wzry_env

    original = (args.assist_mode, args.assist_record_enabled, args.assist_dataset_dir,
                args.human_data_dir, args.observation_delay_sec, args.game_profile_prior_mode)
    args.assist_mode = "manual"
    args.assist_record_enabled = True
    args.assist_dataset_dir = os.path.join(tmpdir, "env_data")
    # 指针文件也写到临时目录，别污染项目里的 human_data/
    args.human_data_dir = os.path.join(tmpdir, "human_data")
    args.observation_delay_sec = 0.0
    args.game_profile_prior_mode = "off"
    try:
        tool = _FakeTool()
        env = wzry_env.Environment(tool, _FakeReward())
        env.reset_episode()
        check("manual 模式创建了本局数据集", env.assist_dataset is not None,
              getattr(env.assist_dataset, "samples_path", ""))

        action = [1, 175, 0, 1, 0, 0, 0, 0]
        state = tool.screenshot_window()
        last_info = None
        for _ in range(6):
            state, reward, done, info = env.step(action, state)
            last_info = info
        env.close()

        required = [
            "effective_action", "injected_action", "ai_action", "human_assist", "human_control",
            "lane_macro", "phase", "minimap", "timing", "reward",
        ]
        # reward 键是 get_reword 提供的，这里只检查 env 自己补的字段
        required.remove("reward")
        missing = [name for name in required if name not in (last_info or {})]
        check("env.step 补全了所有关键 info 字段", not missing, f"missing={missing}")

        assist_info = (last_info or {}).get("human_assist") or {}
        check("manual 模式下 human_control=True", bool((last_info or {}).get("human_control")))
        check("接管期间不注入任何操作", (last_info or {}).get("injected_action") == (0,) * 8,
              str((last_info or {}).get("injected_action")))
        check("human_assist info 结构完整", "assist_mode" in assist_info and "assist_state" in assist_info,
              str(sorted(assist_info.keys())[:6]))

        # 接管发生在第 0 步时，宏/阶段控制器还没跑过；日志字段不能缺
        lane_info = (last_info or {}).get("lane_macro") or {}
        phase_info = (last_info or {}).get("phase") or {}
        check("接管时 lane_macro info 仍有完整字段",
              "lane_macro_enabled" in lane_info and "lane_opening_total_steps" in lane_info,
              f"keys={len(lane_info)}")
        check("接管时 phase info 仍有完整字段",
              "phase_controller_enabled" in phase_info and "phase_own_hp_ratio" in phase_info,
              f"keys={len(phase_info)}")

        human_flags = [call[2] for call in tool.calls if isinstance(call, tuple) and call[0] == "burst"]
        check("human_control 已传到执行层", bool(human_flags) and all(human_flags),
              str(human_flags))

        samples_path = os.path.join(args.assist_dataset_dir, os.listdir(args.assist_dataset_dir)[0],
                                    "assist_samples.jsonl")
        check("本局示范数据已落盘", os.path.exists(samples_path), samples_path)
        with open(samples_path, "r", encoding="utf-8") as file:
            lines = [line for line in file if line.strip()]
        check("落盘步数与 step 数一致", len(lines) == 6, f"{len(lines)} 行")
        first = json.loads(lines[0])
        check("记录里带 phase / obs 字段", "phase" in first and "obs" in first, str(sorted(first.keys())[:8]))
    finally:
        (args.assist_mode, args.assist_record_enabled, args.assist_dataset_dir,
         args.human_data_dir, args.observation_delay_sec, args.game_profile_prior_mode) = original


# --------------------------------------------------------------------------- #
# 8. 对局结束判定（P1）
# --------------------------------------------------------------------------- #
def test_episode_end(tmpdir):
    print("\n== 8. 对局结束判定（P1） ==")
    import wzry_env

    saved = (args.assist_mode, args.assist_record_enabled, args.assist_dataset_dir,
             args.human_data_dir, args.observation_delay_sec,
             args.episode_end_enabled, args.episode_end_min_steps, args.episode_end_confirm_steps,
             args.episode_end_traffic_bytes, args.max_episode_steps)
    args.assist_mode = "off"
    args.assist_record_enabled = False
    args.human_data_dir = os.path.join(tmpdir, "human_data")
    args.observation_delay_sec = 0.0
    args.episode_end_enabled = True
    args.episode_end_min_steps = 0
    args.episode_end_confirm_steps = 10
    args.episode_end_traffic_bytes = 120000
    args.max_episode_steps = 0
    try:
        tool = _FakeTool()
        env = wzry_env.Environment(tool, _FakeReward())
        env.reset_episode()

        alive_info = {"frame_change": 0.01}
        alive_mm = {"minimap_self_visible": True}
        result = None
        for step in range(12):
            env.step_index = step
            result = env._check_episode_end({"total_bytes": 400000}, alive_mm, alive_info)
        check("对局内高流量时不误判结束", result is None, str(result))

        dead_info = {"frame_change": 0.0005}
        dead_mm = {"minimap_self_visible": False}
        result = None
        for step in range(10):
            env.step_index = 100 + step
            result = env._check_episode_end({"total_bytes": 50000}, dead_mm, dead_info)
        check("低流量+静止画面判为已离开对局", result == "traffic_idle", str(result))

        # 低流量但画面还在剧烈变化（例如加载新界面）不应结束
        env2 = wzry_env.Environment(_FakeTool(), _FakeReward())
        env2.reset_episode()
        result = None
        for step in range(10):
            env2.step_index = 200 + step
            result = env2._check_episode_end(
                {"total_bytes": 50000}, {"minimap_self_visible": True}, {"frame_change": 0.05}
            )
        check("低流量但画面活跃时不结束", result is None, str(result))

        args.max_episode_steps = 5
        env3 = wzry_env.Environment(_FakeTool(), _FakeReward())
        env3.reset_episode()
        env3.step_index = 5
        result = env3._check_episode_end({"total_bytes": 400000}, {"minimap_self_visible": True},
                                        {"frame_change": 0.01})
        check("max_episode_steps 硬上限生效", result == "max_steps", str(result))
    finally:
        (args.assist_mode, args.assist_record_enabled, args.assist_dataset_dir,
         args.human_data_dir, args.observation_delay_sec,
         args.episode_end_enabled, args.episode_end_min_steps, args.episode_end_confirm_steps,
         args.episode_end_traffic_bytes, args.max_episode_steps) = saved


def test_reward_scale_convergence(tmpdir):
    """P4：奖励缩放到 0.1 后，Q 不应该再顶在 TD 裁剪上限。"""
    print("\n== 9. 奖励缩放后的 Q 收敛（P4） ==")
    saved = (args.reward_scale, args.td_target_clip, args.epsilon_decay_mode, args.target_update_mode)
    args.reward_scale = 0.1
    args.td_target_clip = 50.0
    args.epsilon_decay_mode = "step"
    args.target_update_mode = "step"
    original_memory = globalInfo.dqn_memory
    try:
        globalInfo.dqn_memory = memory.ReplayMemory(capacity=600, seed=11)
        agent = DQNAgent()
        obs = np.zeros((args.obs_size, args.obs_size, 3), dtype=np.uint8)
        step_reward = 1.2 * args.reward_scale      # 实测对局平均 1.2/步，缩放后 0.12
        for index in range(300):
            globalInfo.store_transition_dqn(obs, [1, index % 360, 0, 1, 0, 0, 0, 0],
                                            step_reward, obs, 0, None, 0.0)
        for _ in range(700):
            agent.replay()
        info = agent.last_replay_info
        q = info.get("q_absmax", 0.0)
        target = info.get("target_absmax", 0.0)
        check("Q 没有顶到裁剪上限", q < args.td_target_clip * 0.5, f"q={q:.2f} clip={args.td_target_clip}")
        check("TD 目标没有顶到裁剪上限", target < args.td_target_clip * 0.5,
              f"target={target:.2f}")
        theoretical = step_reward / max(1e-6, 1.0 - args.gamma)
        check("Q 落在理论回报量级附近", q < theoretical * 6 + 5,
              f"q={q:.2f} 理论回报≈{theoretical:.1f}")

        # epsilon 必须按环境步衰减，而不是按 replay 次数
        epsilon_before = agent.epsilon
        for _ in range(200):
            agent.replay()
        check("replay 不再消耗 epsilon", abs(agent.epsilon - epsilon_before) < 1e-9,
              f"{epsilon_before:.4f} -> {agent.epsilon:.4f}")
        for _ in range(5):
            agent.on_env_step()
        check("on_env_step 会衰减 epsilon", agent.epsilon < epsilon_before,
              f"{epsilon_before:.4f} -> {agent.epsilon:.4f}")
    finally:
        globalInfo.dqn_memory = original_memory
        (args.reward_scale, args.td_target_clip, args.epsilon_decay_mode, args.target_update_mode) = saved


def test_amp_replay_dtype(tmpdir):
    """回归：AMP 下 target_net 输出 fp16，写回 fp32 缓冲会 dtype 报错。"""
    print("\n== 10. AMP 下的 replay dtype（需要 CUDA） ==")
    import torch
    if not torch.cuda.is_available():
        print("  skip: 没有 CUDA，AMP 路径无法验证")
        return

    import dqnAgent as dqn_module
    saved = (args.amp, args.amp_inference, args.batch_size, args.memory_size)
    original_device = dqn_module.device
    original_memory = globalInfo.dqn_memory
    args.amp = True
    args.amp_inference = True
    args.batch_size = 4
    args.memory_size = 32
    try:
        dqn_module.device = torch.device("cuda:0")
        globalInfo.dqn_memory = memory.ReplayMemory(capacity=32, seed=5)
        agent = DQNAgent()
        check("AMP 已启用", bool(agent.amp_enabled), str(agent.amp_enabled))
        obs = np.zeros((agent.obs_size, agent.obs_size, 3), dtype=np.uint8)
        for index in range(8):
            globalInfo.store_transition_dqn(obs, [1, index, 0, 1, 0, 0, 0, 0],
                                            0.1, obs, 0, None, 0.0)
        info, error = None, ""
        try:
            info = agent.replay()
        except RuntimeError as exc:
            error = str(exc)
        loss = float(info["loss"]) if info else float("nan")
        check("AMP 下 replay 不报 dtype 错误", not error, error[:80] or "ok")
        check("AMP 下 loss 是有限值", loss == loss and abs(loss) < 1e6, f"loss={loss:.4f}")
    finally:
        dqn_module.device = original_device
        globalInfo.dqn_memory = original_memory
        (args.amp, args.amp_inference, args.batch_size, args.memory_size) = saved


def test_remote_protocol():
    """双端通信协议：帧编解码、endpoint 解析、socket 往返。"""
    print("\n== 11. 双端协议 ==")
    import remote_protocol as proto

    frame = proto.encode_frame({"kind": "x", "id": 7, "ok": True}, b"payload-123")
    message = proto.decode_frame(frame)
    check("帧编解码往返",
          message.get("kind") == "x" and message.get("id") == 7 and message.blob == b"payload-123",
          str(message))

    empty = proto.decode_frame(proto.encode_frame({"kind": "y"}))
    check("无负载帧往返", empty.blob == b"" and empty.get("kind") == "y", str(empty))

    check("endpoint 解析 host:port", proto.parse_endpoint("192.168.1.9:6000") == ("192.168.1.9", 6000))
    check("endpoint 缺省端口", proto.parse_endpoint("127.0.0.1") == ("127.0.0.1", 5599))

    import socket as _socket
    left, right = _socket.socketpair()
    try:
        proto.send_message(left, {"kind": "q", "id": 1}, b"\x01\x02")
        received = proto.recv_message(right)
        check("socket 收发一致",
              received is not None and received.blob == b"\x01\x02" and received.get("id") == 1,
              str(received))
    finally:
        left.close()
        right.close()


class _FakeDeviceTool:
    """假的 AndroidTool，只实现 DeviceServer 会用到的方法。"""

    def __init__(self):
        self.actual_width = 60
        self.actual_height = 40
        self.scrcpy_dir = "scrcpy-win64-v2.0"
        self.device_serial = "fake-serial"
        self.stopped = 0
        self.actions = []

    def screenshot_window(self):
        return np.zeros((self.actual_height, self.actual_width, 3), dtype=np.uint8)

    def execute_action_burst(self, action, suppress_attack=False, suppress_info=False,
                             burst_duration_ms=None, human_control=False):
        self.actions.append([int(v) for v in action])
        return bool(action[3]), bool(action[2]), bool(action[0])

    def stop_persistent_move(self):
        self.stopped += 1

    def action_move(self, params):
        return _resolved_future(True)

    def action_attack(self, params):
        return _resolved_future(True)

    def action_info(self, params):
        return _resolved_future(False)


def _resolved_future(value):
    import concurrent.futures
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


PROC_NET_TEXT = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:    1000       10    0    0    0     0          0         0     1000       10    0    0    0     0       0          0
  eth0:  500000      400    0    0    0     0          0         0   300000      300    0    0    0     0       0          0
"""


def test_remote_split():
    """双端链路：本机起设备端服务，用 RemoteAndroidTool 走一遍完整调用面。"""
    print("\n== 12. 双端链路（设备端 + 程序端） ==")
    import device_agent
    import remote_device

    fake = _FakeDeviceTool()
    server = device_agent.DeviceServer(
        fake,
        host="127.0.0.1",
        port=0,
        watchdog_sec=0,
        frame_format="raw",
        procnet_reader=lambda: PROC_NET_TEXT,
    )
    port = server.start_background()
    check("设备端已监听", port > 0, f"port={port}")

    tool = None
    try:
        tool = remote_device.RemoteAndroidTool(
            f"127.0.0.1:{port}", timeout_ms=3000, frame_format="raw", verbose=False
        )
        check("握手得到分辨率", (tool.actual_width, tool.actual_height) == (60, 40),
              f"{tool.actual_width}x{tool.actual_height}")
        check("握手得到设备序列号", tool.device_serial == "fake-serial", tool.device_serial)

        frame = tool.screenshot_window()
        check("远程截图尺寸正确",
              frame is not None and frame.shape == (40, 60, 3), "None" if frame is None else str(frame.shape))

        result = tool.execute_action_burst([1, 90, 0, 1, 0, 0, 0, 0], burst_duration_ms=100)
        check("远程执行动作返回三元组", result == (True, False, True), str(result))
        check("设备端收到动作", bool(fake.actions) and fake.actions[-1][1] == 90, str(fake.actions[-1:]))

        text = tool.read_proc_net_dev()
        check("远程读 /proc/net/dev", "eth0" in text, text.splitlines()[-1][:40] if text else "空")

        stream = tool.create_touch_stream()
        for line in _touch_lines(15000, 20000):
            server.push_touch_lines([[time.time(), line]])
        for _ in range(40):
            if stream.parsed_sample_count > 0:
                break
            time.sleep(0.02)
        check("触摸行已转发并解析", stream.parsed_sample_count > 0, str(stream.stats()))
        window = stream.snapshot_window(0.0, time.time() + 1.0)
        check("触摸流可按窗口取样", len(window) > 0, f"{len(window)} 条")

        tool.stop_persistent_move()
        check("远程松摇杆", fake.stopped >= 1, str(fake.stopped))
    finally:
        if tool is not None:
            tool.close()
        time.sleep(0.3)
        server.shutdown()
        time.sleep(0.6)

    check("断开后设备端自动松摇杆", fake.stopped >= 2, str(fake.stopped))


def test_amp_thread_isolation():
    """回归：主线程 AMP 推理不能污染训练线程的 autograd（inference tensor）。"""
    print("\n== 13. AMP 推理与训练线程隔离（需要 CUDA） ==")
    import torch
    if not torch.cuda.is_available():
        print("  skip: 没有 CUDA")
        return

    import dqnAgent as dqn_module
    saved = (args.amp, args.amp_inference, args.batch_size, args.memory_size)
    original_device = dqn_module.device
    original_memory = globalInfo.dqn_memory
    args.amp = True
    args.amp_inference = True
    args.batch_size = 2
    args.memory_size = 16
    try:
        dqn_module.device = torch.device("cuda:0")
        globalInfo.dqn_memory = memory.ReplayMemory(capacity=16, seed=9)
        agent = DQNAgent()
        agent.epsilon = 0.0
        obs = np.zeros((agent.obs_size, agent.obs_size, 3), dtype=np.uint8)
        for index in range(6):
            globalInfo.store_transition_dqn(obs, [1, index, 0, 1, 0, 0, 0, 0],
                                            0.1, obs, 0, None, 0.0)

        outcome = {}

        def trainer():
            try:
                info = agent.replay()
                outcome["loss"] = float(info["loss"])
            except RuntimeError as exc:
                outcome["err"] = str(exc)[:100]

        thread = threading.Thread(target=trainer)
        thread.start()
        for _ in range(10):
            agent.select_action(np.zeros((1080, 2400, 3), dtype=np.uint8), obs=obs)
        thread.join()

        check("推理与训练并发不冲突", "err" not in outcome, outcome.get("err", "ok"))
        loss = outcome.get("loss", float("nan"))
        check("并发训练 loss 有限", loss == loss and abs(loss) < 1e6, f"loss={loss:.4f}")
    finally:
        dqn_module.device = original_device
        globalInfo.dqn_memory = original_memory
        (args.amp, args.amp_inference, args.batch_size, args.memory_size) = saved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    parsed = parser.parse_args(_REAL_ARGV)

    print(f"device={args.device_id} obs_size={args.obs_size} batch={args.batch_size} assist_mode={args.assist_mode}")

    tmpdir = _mkdtemp(prefix="selftest_")
    try:
        test_dataset(tmpdir)
        test_memory()
        test_live_assist(tmpdir)
        test_game_profile(tmpdir)
        test_network()
        test_training_step()
        test_env_step(tmpdir)
        test_episode_end(tmpdir)
        test_reward_scale_convergence(tmpdir)
        test_amp_replay_dtype(tmpdir)
        test_remote_protocol()
        test_remote_split()
        test_amp_thread_isolation()
    finally:
        if parsed.keep:
            print(f"\n临时目录保留: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            # 顺手把空的临时根目录也收掉，避免留下空文件夹
            try:
                os.rmdir(_TMP_ROOT)
            except OSError:
                pass

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 60)
    print(f"共 {len(RESULTS)} 项检查, 通过 {len(RESULTS) - len(failed)}, 失败 {len(failed)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL: {name} {detail}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
