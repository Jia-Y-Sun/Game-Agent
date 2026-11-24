import atexit
import threading
import time
from collections import deque

from argparses import args
from dqnAgent import DQNAgent
from getReword import GetRewordUtil
from globalInfo import GlobalInfo
from metrics_logger import StepMetricsLogger

import vram_manager
from wzry_env import Environment
from net_actor import expected_params, expected_weight_mb
from onnxRunner import OnnxRunner
from traffic_probe import build_traffic_probe
import os
import torch

# 【关键修复】把 PyTorch 自带的 CUDA 库路径暴露给 ONNX Runtime（只有 Windows 有这个 API）
torch_lib_path = os.path.join(os.path.dirname(torch.__file__), 'lib')
if hasattr(os, 'add_dll_directory') and os.path.exists(torch_lib_path):
    os.add_dll_directory(torch_lib_path)
    print(f"已将 PyTorch CUDA 库路径加入搜索: {torch_lib_path}")

# 卷积后端：原版在 dqnAgent 里关掉了 cuDNN，这里改为开启 benchmark
vram_manager.configure_backends(benchmark=args.cudnn_benchmark)

# 全局状态
globalInfo = GlobalInfo()

class_names = ['started']
start_check = OnnxRunner('models/start.onnx', classes=class_names)

rewordUtil = GetRewordUtil()
if args.device_source == 'remote':
    # 双端模式：设备（模拟器/adb/scrcpy）在另一台机器上，本机只做视觉+策略+训练
    from remote_device import RemoteAndroidTool, RemoteDeviceError
    try:
        tool = RemoteAndroidTool(
            args.device_endpoint,
            token=args.device_token,
            timeout_ms=args.device_rpc_timeout_ms,
            frame_format=args.device_frame_format,
            frame_quality=args.device_frame_quality,
            reconnect_attempts=args.device_reconnect_attempts,
        )
    except RemoteDeviceError as exc:
        print("")
        print(f"[remote] 连接设备端失败: {exc}")
        print("[remote] 请检查：")
        print(f"         1) 设备端那台机器是否已运行: python device_agent.py --device_listen_port {args.device_listen_port}")
        print(f"         2) 地址是否正确: --device_endpoint {args.device_endpoint}")
        print("         3) 两端 --device_token 是否一致")
        print("         4) 防火墙是否放行了该端口")
        raise SystemExit(1)
    print(f"[startup] device_source=remote endpoint={tool.endpoint} "
          f"frame={args.device_frame_format}@{args.device_frame_quality}")
else:
    from android_tool import AndroidTool
    tool = AndroidTool()
    print(f"[startup] device_source=local device={args.iphone_id}")

tool.show_scrcpy()
# tool.show_action_log()
traffic_probe = build_traffic_probe(tool)
env = Environment(tool, rewordUtil, traffic_probe=traffic_probe)
metrics_logger = (
    StepMetricsLogger(args.step_metrics_path, max_mb=args.step_metrics_max_mb)
    if args.step_metrics_enabled else None
)
atexit.register(metrics_logger.close if metrics_logger is not None else lambda: None)
atexit.register(env.close)
atexit.register(getattr(tool, 'close', None) or getattr(tool, 'stop', None) or (lambda: None))

agent = DQNAgent()

print(f"[startup] net={agent.arch} obs={agent.obs_size} amp={agent.amp_enabled}")
print(
    f"[startup] params={expected_params(agent.arch):,} "
    f"weights~{expected_weight_mb(agent.arch):.1f}MB(.pt) "
    f"memory={args.memory_size} 条 ≈ {getattr(args, 'memory_estimate_gb', 0)}GB RAM "
    f"batch={args.batch_size}"
)
print(f"[startup] assist_mode={args.assist_mode} record={args.assist_record_enabled} dir={args.assist_dataset_dir}")
print(f"[startup] game_profile={args.game_profile_path} prior={args.game_profile_prior_mode}")
print(f"[startup] model_in={args.model_path} model_out={args.model_out_path}")
print(f"[startup] replay_memory={globalInfo.dqn_memory_stats()}")
vram_manager.log_vram("startup")


def _human_label(info):
    assist_info = info.get("human_assist") or {}
    return assist_info.get("assist_human_act")


def data_collector():
    while True:
        # 获取当前的图像
        state = tool.screenshot_window()
        # 保证图像能正常获取
        if state is None:
            time.sleep(0.01)
            continue
        # 初始化对局状态 对局未开始
        globalInfo.set_game_end()
        # 判断对局是否开始
        checkGameStart = start_check.get_max_label(state)

        if checkGameStart == 'started':
            print("-------------------------------对局开始-----------------------------------")
            globalInfo.set_game_start()
            env.reset_episode()
            agent.reset_episode()
            episode_step = 0
            state_obs = agent.to_observation(state)
            drop_steps = max(0, int(args.episode_end_drop_steps))
            pending_transitions = deque()
            print(f"[assist] {env.human_assist.describe()}")
            print(
                f"[train] epsilon_decay_mode={args.epsilon_decay_mode} "
                f"target_update_mode={args.target_update_mode} "
                f"replays_per_env_step={args.replays_per_env_step} "
                f"train_time_budget={args.train_time_budget_per_step_sec}s "
                f"reward_scale={args.reward_scale} td_target_clip={args.td_target_clip} "
                f"probe_reward_scale={args.probe_reward_scale} "
                f"episode_end={'on' if args.episode_end_enabled else 'off'} "
                f"max_episode_steps={args.max_episode_steps}"
            )

            # 对局开始了，进行训练
            while globalInfo.is_start_game():
                # 获取预测动作
                select_started_at = time.perf_counter()
                action = agent.select_action(state, obs=state_obs)
                select_action_ms = (time.perf_counter() - select_started_at) * 1000
                policy_info = dict(agent.last_policy_info)
                if args.action_preview_enabled and episode_step < args.action_preview_steps:
                    print(
                        "即将执行动作 "
                        f"step={episode_step} "
                        f"source={policy_info.get('source')} "
                        f"macro={policy_info.get('macro')} "
                        f"action={action} "
                        f"select_ms={select_action_ms:.2f}"
                    )

                next_state, reward, done, info = env.step(action, state)
                info["policy_source"] = policy_info.get("source")
                info["macro_action"] = policy_info.get("macro")
                if args.step_timing_debug:
                    print(
                        "step_timing",
                        {
                            "step": episode_step,
                            "select_action_ms": round(select_action_ms, 2),
                            **info["timing"],
                        },
                    )
                assist_info = info.get("human_assist") or {}
                human_action = _human_label(info)
                if args.step_info_log:
                    print(info, reward)
                else:
                    phase_info = info.get("phase") or {}
                    lane_info = info.get("lane_macro") or {}
                    print(
                        f"step={episode_step:<4} "
                        f"phase={str(phase_info.get('phase')):<10} "
                        f"macro={str(lane_info.get('lane_macro_name')):<28} "
                        f"action={info['effective_action']} "
                        f"reward={float(reward):+.3f} "
                        f"eps={agent.epsilon:.3f} "
                        f"src={str(policy_info.get('source')):<11} "
                        f"assist={assist_info.get('assist_state', '-'):<9} "
                        f"human={'Y' if human_action is not None else 'n'}"
                    )

                if human_action is not None:
                    # 人为示范经验标记一个优先级，供混合采样时优先复用
                    priority = float(args.assist_human_priority)
                else:
                    priority = 0.0

                next_state_obs = agent.to_observation(next_state)
                # 【P4】奖励整体缩放：单步奖励 ~1、gamma 0.99 时真实回报约 100，
                # 会正好撞上 TD 裁剪上限，把 Q 顶成常数（实测 q≈100、loss 0.007）。
                scaled_reward = float(reward) * float(args.reward_scale)
                # 【P1】最后 N 步先压着不写：如果这局是因「已离开对局」被判定结束，
                # 这些交界处的错帧就不要污染经验池。
                pending_transitions.append((
                    state_obs,
                    info["effective_action"],
                    scaled_reward,
                    next_state_obs,
                    done,
                    human_action,
                    priority,
                ))
                while len(pending_transitions) > drop_steps:
                    globalInfo.store_transition_dqn(*pending_transitions.popleft())

                # 【P2】epsilon 衰减/目标网络同步按环境步推进
                agent.on_env_step()

                if metrics_logger is not None:
                    metrics_logger.log({
                        "reward": round(float(reward), 4),
                        "reward_scaled": round(scaled_reward, 4),
                        "done": int(done),
                        "episode_end_reason": info.get("episode_end_reason"),
                        "event": info["event"],
                        "attack_checks": (info.get("event_stats") or {}).get("attack_checks", 0),
                        "attack_hits": (info.get("event_stats") or {}).get("attack_hits", 0),
                        "probe_reward": info["probe_reward"],
                        "gameplay_reward": info["gameplay_reward"],
                        "gameplay_action_score": info["gameplay_action_score"],
                        "traffic_reward": info["traffic_reward"],
                        "target_reward": info["target_reward"],
                        "phase_reward": info["phase_reward"],
                        "phase_reward_score": info["phase_reward_score"],
                        "event_reward": info["event_reward"],
                        "step_latency_ms": info["step_latency_ms"],
                        "select_action_ms": round(select_action_ms, 2),
                        "action_plan_ms": info["timing"]["action_plan_ms"],
                        "execute_ms": info["timing"]["execute_ms"],
                        "observe_ms": info["timing"]["observe_ms"],
                        "traffic_ms": info["timing"]["traffic_ms"],
                        "reward_ms": info["timing"]["reward_ms"],
                        "env_step_total_ms": info["timing"]["env_step_total_ms"],
                        "frame_change": info["frame_change"],
                        "frame_change_score": info["frame_change_score"],
                        "active_action_count": info["active_action_count"],
                        "novel_action": info["novel_action"],
                        "smooth_move": info["smooth_move"],
                        "ineffective_action": info["ineffective_action"],
                        "traffic_available": info["traffic_available"],
                        "traffic_bytes_score": info["traffic_bytes_score"],
                        "traffic_packets_score": info["traffic_packets_score"],
                        "traffic_activity_score": info["traffic_activity_score"],
                        "latency_score": info["latency_score"],
                        "target_visible": info["target_visible"],
                        "target_angle": info["target_angle"],
                        "target_distance_ratio": info["target_distance_ratio"],
                        "target_reward_score": info["target_reward_score"],
                        "raw_action": info["raw_action"],
                        "lane_macro_action": info["lane_macro_action"],
                        "lane_macro_enabled": info["lane_macro"]["lane_macro_enabled"],
                        "lane_profile_loaded": info["lane_macro"]["lane_profile_loaded"],
                        "lane_macro_override": info["lane_macro"]["lane_macro_override"],
                        "lane_macro_phase": info["lane_macro"]["lane_macro_phase"],
                        "lane_macro_name": info["lane_macro"]["lane_macro_name"],
                        "lane_macro_step": info["lane_macro"]["lane_macro_step"],
                        "lane_macro_suppress_attack": info["lane_macro"]["lane_macro_suppress_attack"],
                        "lane_macro_suppress_info": info["lane_macro"]["lane_macro_suppress_info"],
                        "lane_opening_profile_move_only": info["lane_macro"]["lane_opening_profile_move_only"],
                        "lane_opening_total_steps": info["lane_macro"]["lane_opening_total_steps"],
                        "lane_opening_base_steps": info["lane_macro"]["lane_opening_base_steps"],
                        "lane_opening_max_steps": info["lane_macro"]["lane_opening_max_steps"],
                        "lane_opening_move_only_steps": info["lane_macro"]["lane_opening_move_only_steps"],
                        "lane_opening_attack_start_step": info["lane_macro"]["lane_opening_attack_start_step"],
                        "lane_opening_skill_start_step": info["lane_macro"]["lane_opening_skill_start_step"],
                        "lane_arrived": info["lane_macro"]["lane_arrived"],
                        "lane_arrival_reason": info["lane_macro"]["lane_arrival_reason"],
                        "lane_arrival_pressure_score": info["lane_macro"]["lane_arrival_pressure_score"],
                        "lane_arrival_target_visible": info["lane_macro"]["lane_arrival_target_visible"],
                        "lane_arrival_target_distance_ratio": info["lane_macro"]["lane_arrival_target_distance_ratio"],
                        "lane_minimap_arrival_candidate_steps": info["lane_macro"].get("lane_minimap_arrival_candidate_steps", 0),
                        "lane_minimap_lane_arrived": info["lane_macro"].get("lane_minimap_lane_arrived", False),
                        "lane_minimap_lane_distance": info["lane_macro"].get("lane_minimap_lane_distance"),
                        "lane_minimap_lane_move_angle": info["lane_macro"].get("lane_minimap_lane_move_angle"),
                        "lane_minimap_self_visible": info["lane_macro"].get("lane_minimap_self_visible", False),
                        "lane_arrival_stop_steps_left": info["lane_macro"]["lane_arrival_stop_steps_left"],
                        "lane_laning_step": info["lane_macro"]["lane_laning_step"],
                        "lane_laning_pressure_score": info["lane_macro"]["lane_laning_pressure_score"],
                        "lane_laning_own_hp_ratio": info["lane_macro"]["lane_laning_own_hp_ratio"],
                        "lane_laning_low_hp": info["lane_macro"]["lane_laning_low_hp"],
                        "lane_laning_target_visible": info["lane_macro"]["lane_laning_target_visible"],
                        "lane_laning_target_distance_ratio": info["lane_macro"]["lane_laning_target_distance_ratio"],
                        "lane_laning_skill_interval_steps": info["lane_macro"]["lane_laning_skill_interval_steps"],
                        "lane_laning_last_hit_interval_steps": info["lane_macro"]["lane_laning_last_hit_interval_steps"],
                        "lane_laning_push_tower_interval_steps": info["lane_macro"]["lane_laning_push_tower_interval_steps"],
                        "target_refined_action": info["target_refined_action"],
                        "phase_action": info["phase_action"],
                        "phase_controller_enabled": info["phase"]["phase_controller_enabled"],
                        "phase_override": info["phase"]["phase_override"],
                        "phase": info["phase"]["phase"],
                        "phase_action_name": info["phase"]["phase_action_name"],
                        "phase_reason": info["phase"]["phase_reason"],
                        "phase_step": info["phase"]["phase_step"],
                        "phase_own_hp_ratio": info["phase"]["phase_own_hp_ratio"],
                        "phase_low_hp": info["phase"]["phase_low_hp"],
                        "phase_center_pressure_score": info["phase"]["phase_center_pressure_score"],
                        "phase_target_visible": info["phase"]["phase_target_visible"],
                        "phase_target_close": info["phase"]["phase_target_close"],
                        "phase_target_distance_ratio": info["phase"]["phase_target_distance_ratio"],
                        "phase_attack_avatar_visible": info["phase"].get("phase_attack_avatar_visible", False),
                        "phase_minimap_enemy_visible": info["phase"].get("phase_minimap_enemy_visible", False),
                        "phase_minimap_enemy_seek_angle": info["phase"].get("phase_minimap_enemy_seek_angle"),
                        "phase_minimap_enemy_distance": info["phase"].get("phase_minimap_enemy_distance"),
                        "phase_minimap_lane_arrived": info["phase"].get("phase_minimap_lane_arrived", False),
                        "phase_minimap_lane_distance": info["phase"].get("phase_minimap_lane_distance"),
                        "phase_minimap_lane_move_angle": info["phase"].get("phase_minimap_lane_move_angle"),
                        "phase_minimap_self_visible": info["phase"].get("phase_minimap_self_visible", False),
                        "phase_dead_screen_dark": info["phase"].get("phase_dead_screen_dark", False),
                        "phase_dark_mean_v": info["phase"].get("phase_dark_mean_v"),
                        "phase_retreat_steps_left": info["phase"]["phase_retreat_steps_left"],
                        "phase_recover_steps_left": info["phase"]["phase_recover_steps_left"],
                        "effective_action": info["effective_action"],
                        "injected_action": info["injected_action"],
                        "executed": info["executed"],
                        "minimap": info["minimap"],
                        "policy_source": policy_info.get("source"),
                        "macro_action": policy_info.get("macro"),
                        "traffic": info["traffic"],
                        # --- 人为辅助 ---
                        "assist_mode": assist_info.get("assist_mode"),
                        "assist_state": assist_info.get("assist_state"),
                        "assist_human_control": info.get("human_control"),
                        "assist_human_present": assist_info.get("assist_human_present"),
                        "assist_human_hold_ms": assist_info.get("assist_human_hold_ms"),
                        "assist_human_act": assist_info.get("assist_human_act"),
                        "assist_human_steps": assist_info.get("assist_episode_human_steps"),
                        "assist_override_steps": assist_info.get("assist_episode_override_steps"),
                        "assist_takeovers": assist_info.get("assist_episode_takeovers"),
                        "obs_size": agent.obs_size,
                    })

                # 对局结束
                if done == 1:
                    end_reason = info.get("episode_end_reason") or "ocr_finish"
                    if end_reason == "traffic_idle":
                        # 已判定离开对局：最后这几步是交界处的错帧，丢弃
                        dropped = len(pending_transitions)
                        pending_transitions.clear()
                        print(f"[train] 判定对局已结束(traffic_idle)，丢弃交界处 {dropped} 步经验")
                    else:
                        while pending_transitions:
                            globalInfo.store_transition_dqn(*pending_transitions.popleft())
                    print(f"-------------------------------对局结束({end_reason})-----------------------------------")
                    globalInfo.set_game_end()
                    break

                # 追加经验
                state = next_state
                state_obs = next_state_obs
                episode_step += 1

        else:
            print("对局未开始")
            time.sleep(0.1)


def train_agent():
    count = 1
    log_every = max(1, int(args.train_log_interval))
    replay_limit_ratio = float(args.replays_per_env_step or 0.0)
    time_budget = float(getattr(args, "train_time_budget_per_step_sec", 0.0) or 0.0)
    train_seconds_spent = 0.0
    while True:
        if not globalInfo.is_memory_bigger_batch_size_dqn():
            time.sleep(1)
            continue

        # 【P3】把训练次数与环境步绑定：原来训练线程满速跑，实测 27 次迭代/步，
        # 64 条样本被反复采样几十遍，既过拟合又让 epsilon 秒掉到下限。
        if replay_limit_ratio > 0:
            allowed = replay_limit_ratio * max(1, agent.env_steps) + 2.0
            if agent.replay_count >= allowed:
                time.sleep(0.01)
                continue

        # legacy 大网络单次迭代约 0.9 秒，只按次数限速会让训练吃掉整个采集节奏，
        # 所以再叠一层「每个环境步最多花多少秒训练」的时间预算。
        if time_budget > 0:
            allowed_seconds = time_budget * max(1, agent.env_steps) + 2.0
            if train_seconds_spent >= allowed_seconds:
                time.sleep(0.01)
                continue

        replay_started_at = time.perf_counter()
        info = agent.replay()
        train_seconds_spent += time.perf_counter() - replay_started_at
        if info is None:
            time.sleep(0.05)
            continue

        if count % log_every == 0:
            memory_stats = globalInfo.dqn_memory_stats()
            print(
                "training "
                f"replay={count} loss={info['loss']:.4f} aux={info['aux_loss']:.4f} "
                f"q={info.get('q_absmax', 0):.2f} target={info.get('target_absmax', 0):.2f} "
                f"human_in_batch={info['human_in_batch']}/{info['batch_size']} "
                f"epsilon={info['epsilon']} arch={info['arch']} "
                f"memory={memory_stats['size']}/{memory_stats['capacity']} "
                f"human={memory_stats['human_samples']} ram={memory_stats['state_human']}"
            )
        if args.vram_log_interval_steps and count % max(1, int(args.vram_log_interval_steps)) == 0:
            vram_manager.log_vram(f"replay_{count}")

        if count % max(1, int(args.save_model_every_replays)) == 0:
            agent.save_model(args.model_out_path)

        count = count + 1
        if count >= 1000000:
            count = 1


if __name__ == '__main__':
    training_thread = threading.Thread(target=train_agent, daemon=True)
    training_thread.start()
    try:
        data_collector()
    except KeyboardInterrupt:
        print("收到 Ctrl+C，正在退出。")
    finally:
        env.close()
        vram_manager.log_vram("shutdown")
