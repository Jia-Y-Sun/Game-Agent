import statistics
import time

import assist_dataset
from argparses import args
from globalInfo import GlobalInfo
from game_profile import GameProfilePrior, load_game_profile
from lane_macro import LaneMacroController
from live_human_assist import LiveHumanAssist
from minimap_vision import MinimapVision
from phase_controller import GamePhaseController
from target_tracker import TargetTracker

ZERO_ACTION = [0, 0, 0, 0, 0, 0, 0, 0]


class Environment:
    def __init__(self, android_controller, rewordUtil, traffic_probe=None):
        self.android_controller = android_controller
        self.rewordUtil = rewordUtil
        self.traffic_probe = traffic_probe
        self.globalInfo = GlobalInfo()
        self.target_tracker = TargetTracker()
        self.minimap_vision = MinimapVision()

        # 整场人为辅助层：整局都在监听人手有没有接管
        self.human_assist = LiveHumanAssist(tool=android_controller)
        self.game_profile = load_game_profile()
        self.game_profile_prior = GameProfilePrior(self.game_profile, seed=getattr(args, 'seed', None))

        self.lane_macro = LaneMacroController()
        self.phase_controller = GamePhaseController()
        if self.game_profile.available:
            self.phase_controller.attach_profile_prior(self.game_profile_prior)
        self.was_dead_dark = False

        self.step_index = 0
        self.last_info_step = -args.info_action_cooldown_steps
        self.last_lane_info = self.lane_macro.default_info()
        self.last_phase_info = self.phase_controller.default_info()

        self.assist_dataset = None
        self.episode_human_steps = 0
        self.episode_override_steps = 0

        # 【P1】对局结束健康判据用的滑动窗口
        self._end_traffic = []
        self._end_frame_change = []
        self._end_self_visible = []

        action_lengths = [2, 360, 9, 11, 3, 360, 100, 5]
        self.action_space_n = sum(action_lengths)
        print(self.action_space_n)

    def reset_episode(self):
        self.step_index = 0
        self.last_info_step = -args.info_action_cooldown_steps
        self.rewordUtil.reset_episode()
        self.target_tracker.reset_episode()
        self.minimap_vision.reset_episode()
        self.lane_macro.reset_episode()
        self.phase_controller.reset_episode()
        self.human_assist.reset_episode()
        self.game_profile_prior.reset_episode()
        self.was_dead_dark = False
        # 用控制器的默认视图打底：人为接管可能发生在第 0 步，
        # 此时 refine_action 还没跑过，日志里不能出现缺字段的 info。
        self.last_lane_info = self.lane_macro.default_info()
        self.last_phase_info = self.phase_controller.default_info()
        self.episode_human_steps = 0
        self.episode_override_steps = 0
        self._end_traffic = []
        self._end_frame_change = []
        self._end_self_visible = []
        if self.traffic_probe is not None:
            self.traffic_probe.reset_episode()
        self._start_assist_episode()

    # ------------------------------------------------------------------ #
    # 人为辅助数据集
    # ------------------------------------------------------------------ #
    def _start_assist_episode(self):
        if self.assist_dataset is not None:
            try:
                self.assist_dataset.close()
            except Exception:
                pass
            self.assist_dataset = None

        if not args.assist_record_enabled or args.assist_mode == 'off':
            return
        try:
            writer = assist_dataset.create_episode_writer(
                root_dir=args.assist_dataset_dir,
                episode_id=args.assist_episode_id or None,
                frame_policy=args.assist_frame_policy,
                frame_max_width=args.assist_frame_max_width,
                jpeg_quality=args.assist_jpeg_quality,
                preroll_steps=args.assist_preroll_steps,
                pointer_dir=args.human_data_dir,
                metadata={
                    "assist_mode": args.assist_mode,
                    "assist_partial": bool(args.assist_partial),
                    "device_serial": getattr(self.android_controller, 'device_serial', ''),
                    "screen_width": getattr(self.android_controller, 'actual_width', 0),
                    "screen_height": getattr(self.android_controller, 'actual_height', 0),
                    "move_joystick_center": {
                        "x": float(args.move_joystick_center_x),
                        "y": float(args.move_joystick_center_y),
                    },
                    "touch_transform": {
                        "raw_width": args.touch_raw_width,
                        "raw_height": args.touch_raw_height,
                        "swap_xy": bool(args.touch_swap_xy),
                        "invert_x": bool(args.touch_invert_x),
                        "invert_y": bool(args.touch_invert_y),
                    },
                    "obs_size": int(args.obs_size),
                },
            )
            self.human_assist.attach_dataset(writer)
            self.assist_dataset = writer
            print(f"[assist] 本局示范数据写入: {writer.samples_path}")
        except OSError as exc:
            print(f"[assist] 示范数据目录创建失败: {exc}")

    def close(self):
        if self.assist_dataset is not None:
            self.assist_dataset.close()
            stats = self.assist_dataset.stats()
            self.assist_dataset = None
            print(f"[assist] 本局示范数据统计: {stats}")
        self.human_assist.detach_dataset(close=False)
        self.human_assist.stop()

    def _prepare_action(self, action, suppress_attack=False, suppress_info=False):
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = [int(v) for v in action]

        if move_action == 0:
            angle = 0

        if suppress_info:
            info_action = 0

        if suppress_attack:
            attack_action = 0
            action_type = 0
            arg1 = 0
            arg2 = 0
            arg3 = 0

        if attack_action == 0 or attack_action < 7:
            action_type = 0
            arg1 = 0
            arg2 = 0
            arg3 = 0

        can_use_info = self.step_index - self.last_info_step >= args.info_action_cooldown_steps
        if info_action != 0 and not can_use_info:
            info_action = 0

        if args.force_attack_while_moving and not suppress_attack and move_action != 0 and attack_action == 0:
            attack_action = 1

        # Keep movement high-frequency. By default, allow buy/upgrade/info taps
        # to be interleaved with combat, which is closer to real play.
        if not args.allow_info_attack_combo and info_action != 0 and attack_action != 0:
            if can_use_info:
                attack_action = 0
                action_type = 0
                arg1 = 0
                arg2 = 0
                arg3 = 0
            else:
                info_action = 0

        return move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3

    def step(self, action, current_state):
        env_started_at = time.perf_counter()
        plan_started_at = env_started_at
        minimap_state = self.minimap_vision.detect(current_state)
        if self.was_dead_dark and not minimap_state.get("dead_screen_dark", False):
            self.target_tracker.reset_episode()
            self.lane_macro.reset_episode()
            self.phase_controller.reset_episode()
            minimap_state["respawn_route_reset"] = True
            self.was_dead_dark = False
        elif minimap_state.get("dead_screen_dark", False):
            self.was_dead_dark = True
            minimap_state["respawn_route_reset"] = False
        else:
            minimap_state["respawn_route_reset"] = False

        target_info = self.target_tracker.detect(current_state)
        target_info = self._merge_minimap_target(target_info, minimap_state)

        # 先看人有没有伸手接管；接管期间冻结所有宏/阶段控制器，
        # 避免它们的步数与内部状态在人工操作期间继续推进。
        human_observation = self.human_assist.poll()
        human_predicted = self.human_assist.will_take_over(human_observation)

        if human_predicted:
            lane_action = [int(v) for v in action]
            lane_info = dict(self.last_lane_info)
            lane_info.update({
                "lane_macro_override": False,
                "lane_macro_suppress_attack": False,
                "lane_macro_suppress_info": False,
                "lane_macro_name": "human_assist_hold",
                "lane_macro_phase": lane_info.get("lane_macro_phase", "human"),
                "human_assist_hold": True,
                "lane_macro_lock_target_tracking": True,
            })
            target_refined_action = lane_action
            phase_action = lane_action
            phase_info = dict(self.last_phase_info)
            phase_info.update({
                "phase_override": False,
                "phase": phase_info.get("phase", self.phase_controller.phase),
                "phase_action_name": "human_assist_hold",
                "phase_reason": "human_takeover",
                "human_assist_hold": True,
            })
        else:
            lane_action, lane_info = self.lane_macro.refine_action(action, target_info, current_state, minimap_state)
            if lane_info.get("lane_macro_lock_target_tracking"):
                target_refined_action = lane_action
            else:
                target_refined_action = self.target_tracker.refine_action(lane_action, target_info)
            phase_action, phase_info = self.phase_controller.refine_action(
                target_refined_action,
                current_state,
                target_info,
                lane_info,
                minimap_state,
            )
            self.last_lane_info = lane_info
            self.last_phase_info = phase_info

        suppress_attack = bool(lane_info.get("lane_macro_suppress_attack"))
        suppress_info = bool(lane_info.get("lane_macro_suppress_info"))
        ai_action = self._prepare_action(phase_action, suppress_attack=suppress_attack, suppress_info=suppress_info)

        reward_action, inject_action, human_control, assist_info = self.human_assist.arbitrate(
            ai_action, human_observation
        )
        if human_control:
            suppress_attack = False
            suppress_info = False
        action_plan_ms = (time.perf_counter() - plan_started_at) * 1000
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = inject_action
        traffic_start = self.traffic_probe.begin_step() if self.traffic_probe is not None else None
        execute_started_at = time.perf_counter()

        if args.action_burst_enabled:
            executed = self.android_controller.execute_action_burst(
                inject_action,
                suppress_attack=suppress_attack,
                suppress_info=suppress_info,
                burst_duration_ms=self.human_assist.suggest_burst_ms(args.action_burst_duration_ms),
                human_control=human_control,
            )
        elif human_control:
            # 人在操作时绝不注入任何东西：先松开程序自己的摇杆，然后什么都不做。
            self.android_controller.stop_persistent_move()
            executed = (False, False, False)
        else:
            futures = [
                self.android_controller.action_move({"action": move_action, "angle": angle}),
                self.android_controller.action_info({"action": info_action}),
                self.android_controller.action_attack(
                    {"action": attack_action, "action_type": action_type, "arg1": arg1, "arg2": arg2, "arg3": arg3}
                ),
            ]
            executed = [future.result() for future in futures]
        execute_ms = (time.perf_counter() - execute_started_at) * 1000

        if info_action != 0 and executed[1]:
            self.last_info_step = self.step_index

        if args.observation_delay_sec > 0:
            time.sleep(args.observation_delay_sec)

        observe_started_at = time.perf_counter()
        next_state = self.android_controller.screenshot_window()
        while next_state is None or next_state.size == 0:
            time.sleep(0.01)
            next_state = self.android_controller.screenshot_window()
        observe_ms = (time.perf_counter() - observe_started_at) * 1000

        step_latency_ms = (time.perf_counter() - execute_started_at) * 1000
        traffic_started_at = time.perf_counter()
        traffic_metrics = self.traffic_probe.end_step(traffic_start) if self.traffic_probe is not None else None
        traffic_ms = (time.perf_counter() - traffic_started_at) * 1000
        reward_started_at = time.perf_counter()
        reward, done, info = self.rewordUtil.get_reword(
            next_state,
            True,
            reward_action,
            previous_image=current_state,
            traffic_metrics=traffic_metrics,
            step_latency_ms=step_latency_ms,
            target_info=target_info,
            phase_info=phase_info,
        )
        if human_control and args.assist_human_reward_bonus:
            reward += float(args.assist_human_reward_bonus)
            info["assist_reward_bonus"] = float(args.assist_human_reward_bonus)
        reward_ms = (time.perf_counter() - reward_started_at) * 1000
        self.phase_controller.update_event(info.get("event"))
        if info.get("event") == "death":
            self.was_dead_dark = True

        info["raw_action"] = tuple(int(v) for v in action)
        info["lane_macro_action"] = tuple(int(v) for v in lane_action)
        info["lane_macro"] = lane_info
        info["target_refined_action"] = tuple(int(v) for v in target_refined_action)
        info["phase_action"] = tuple(int(v) for v in phase_action)
        info["phase"] = phase_info
        info["minimap"] = minimap_state
        info["effective_action"] = tuple(int(v) for v in reward_action)
        info["injected_action"] = tuple(int(v) for v in inject_action)
        info["ai_action"] = tuple(int(v) for v in ai_action)
        info["human_assist"] = assist_info
        info["human_control"] = bool(human_control)
        info["executed"] = tuple(bool(v) for v in executed)
        info["step_latency_ms"] = round(step_latency_ms, 2)
        info["timing"] = {
            "action_plan_ms": round(action_plan_ms, 2),
            "execute_ms": round(execute_ms, 2),
            "observe_ms": round(observe_ms, 2),
            "traffic_ms": round(traffic_ms, 2),
            "reward_ms": round(reward_ms, 2),
            "env_step_total_ms": round((time.perf_counter() - env_started_at) * 1000, 2),
        }
        info["traffic"] = traffic_metrics
        info["target"] = target_info

        # 【P1】除了 OCR 胜负，再用「对局内健康度」判断这一局是否已经结束：
        # 对局结束后画面会回到选将/大厅，网络流量会从 30~40 万字节/步掉到 5~8 万，
        # 而程序原来会继续在这个界面上按「对局中」发操作（实测多跑了 306 步，占 22%）。
        end_reason = self._check_episode_end(traffic_metrics, minimap_state, info)
        if end_reason:
            done = 1
            info["episode_end_reason"] = end_reason

        if human_control:
            self.episode_override_steps += 1
        if human_observation.present:
            self.episode_human_steps += 1

        # 写入整场示范数据集（帧就是产生这个动作之前的那一帧）
        self.human_assist.record_step(
            step=self.step_index,
            frame=current_state,
            learned_action=reward_action,
            ai_action=ai_action,
            human_action=assist_info.get("assist_human_act"),
            human_present=human_observation.present,
            human_control=human_control,
            phase_info=phase_info,
            lane_info=lane_info,
            minimap_state=minimap_state,
            info=info,
            reward=reward,
            done=done,
        )

        self.step_index += 1
        return next_state, reward, done, info

    def _check_episode_end(self, traffic_metrics, minimap_state, info):
        """返回结束原因字符串，或 None。

        判据（按优先级）：

        1. `--max_episode_steps` 硬上限；
        2. 最近 `--episode_end_confirm_steps` 步的网络流量中位数低于
           `--episode_end_traffic_bytes`，且同一窗口里「小地图自身可见」占比很低
           或「画面变化」中位数很低 —— 说明画面已经不在对局内。
        """
        if not getattr(args, "episode_end_enabled", True):
            return None

        max_steps = int(getattr(args, "max_episode_steps", 0) or 0)
        if max_steps > 0 and self.step_index >= max_steps:
            return "max_steps"

        if self.step_index < max(0, int(getattr(args, "episode_end_min_steps", 120) or 0)):
            return None

        total_bytes = int((traffic_metrics or {}).get("total_bytes") or 0)
        self._end_traffic.append(total_bytes)
        self._end_frame_change.append(float(info.get("frame_change") or 0.0))
        self._end_self_visible.append(bool((minimap_state or {}).get("minimap_self_visible", False)))

        need = max(3, int(getattr(args, "episode_end_confirm_steps", 30) or 30))
        if len(self._end_traffic) < need:
            return None
        while len(self._end_traffic) > need:
            self._end_traffic.pop(0)
            self._end_frame_change.pop(0)
            self._end_self_visible.pop(0)

        traffic_threshold = int(getattr(args, "episode_end_traffic_bytes", 120000) or 0)
        if traffic_threshold <= 0:
            return None

        median_bytes = statistics.median(self._end_traffic)
        self_visible_ratio = sum(1 for flag in self._end_self_visible if flag) / float(need)
        median_frame_change = statistics.median(self._end_frame_change)

        if median_bytes >= traffic_threshold:
            return None
        if self_visible_ratio > 0.2 and median_frame_change >= 0.002:
            return None
        return "traffic_idle"

    @staticmethod
    def _merge_minimap_target(target_info, minimap_state):
        target_info = dict(target_info or {})
        if target_info.get("visible"):
            target_info["attack_avatar_visible"] = bool(minimap_state.get("attack_avatar_visible", False))
            return target_info

        if not minimap_state.get("attack_avatar_visible", False):
            return target_info

        angle = minimap_state.get("minimap_enemy_seek_angle")
        if angle is None:
            angle = 0
        target_info.update({
            "visible": True,
            "center": minimap_state.get("attack_avatar_center"),
            "box": None,
            "area": float(minimap_state.get("attack_avatar_area", 0.0) or 0.0),
            "angle": int(angle) % 360,
            "distance_ratio": 0.18,
            "source": "attack_avatar",
            "attack_avatar_visible": True,
        })
        return target_info
