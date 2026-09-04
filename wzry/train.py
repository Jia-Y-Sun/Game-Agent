import atexit
import threading
import time

import cv2
import numpy as np
from android_tool import AndroidTool
from argparses import args
from dqnAgent import DQNAgent
from getReword import GetRewordUtil
from globalInfo import GlobalInfo
from metrics_logger import StepMetricsLogger

from wzry_env import Environment
from onnxRunner import OnnxRunner
from traffic_probe import build_traffic_probe

# 全局状态
globalInfo = GlobalInfo()

class_names = ['started']
start_check = OnnxRunner('models/start.onnx', classes=class_names)

rewordUtil = GetRewordUtil()
tool = AndroidTool()
tool.show_scrcpy()
# tool.show_action_log()
traffic_probe = build_traffic_probe(tool)
env = Environment(tool, rewordUtil, traffic_probe=traffic_probe)
metrics_logger = StepMetricsLogger(args.step_metrics_path)
atexit.register(metrics_logger.close)

agent = DQNAgent()

def data_collector():
    while True:
        # 获取当前的图像
        state = tool.screenshot_window()
        # 保证图像能正常获取
        if state is None:
            time.sleep(0.01)
            continue
        # cv2.imwrite('output_image.jpg', state)
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

            # 对局开始了，进行训练
            while globalInfo.is_start_game():
                # 获取预测动作
                select_started_at = time.perf_counter()
                action = agent.select_action(state)
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
                print(info, reward)
                globalInfo.store_transition_dqn(state, info["effective_action"], reward, next_state, done)
                metrics_logger.log({
                    "reward": round(float(reward), 4),
                    "done": int(done),
                    "event": info["event"],
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
                    "executed": info["executed"],
                    "minimap": info["minimap"],
                    "policy_source": policy_info.get("source"),
                    "macro_action": policy_info.get("macro"),
                    "traffic": info["traffic"],
                })

                # 对局结束
                if done == 1:
                    print("-------------------------------对局结束-----------------------------------")
                    globalInfo.set_game_end()
                    break

                # 追加经验
                state = next_state
                episode_step += 1

        else:
            print("对局未开始")
            time.sleep(0.1)


def train_agent():
    count = 1
    while True:
        if not globalInfo.is_memory_bigger_batch_size_dqn():
            time.sleep(1)
            continue
        print("training")
        agent.replay()
        if count % args.num_episodes == 0:
            agent.save_model('src/wzry_ai.pt')
        count = count + 1
        if count >= 100000:
            count = 1


if __name__ == '__main__':
    training_thread = threading.Thread(target=train_agent)
    training_thread.start()
    data_collector()
