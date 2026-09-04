import time

from argparses import args
from globalInfo import GlobalInfo
from lane_macro import LaneMacroController
from minimap_vision import MinimapVision
from phase_controller import GamePhaseController
from target_tracker import TargetTracker


class Environment:
    def __init__(self, android_controller, rewordUtil, traffic_probe=None):
        self.android_controller = android_controller
        self.rewordUtil = rewordUtil
        self.traffic_probe = traffic_probe
        self.globalInfo = GlobalInfo()
        self.step_index = 0
        self.last_info_step = -args.info_action_cooldown_steps
        self.target_tracker = TargetTracker()
        self.minimap_vision = MinimapVision()
        self.lane_macro = LaneMacroController()
        self.phase_controller = GamePhaseController()
        self.was_dead_dark = False

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
        self.was_dead_dark = False
        if self.traffic_probe is not None:
            self.traffic_probe.reset_episode()

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
        suppress_attack = bool(lane_info.get("lane_macro_suppress_attack"))
        suppress_info = bool(lane_info.get("lane_macro_suppress_info"))
        effective_action = self._prepare_action(phase_action, suppress_attack=suppress_attack, suppress_info=suppress_info)
        action_plan_ms = (time.perf_counter() - plan_started_at) * 1000
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = effective_action
        traffic_start = self.traffic_probe.begin_step() if self.traffic_probe is not None else None
        execute_started_at = time.perf_counter()

        if args.action_burst_enabled:
            executed = self.android_controller.execute_action_burst(
                effective_action,
                suppress_attack=suppress_attack,
                suppress_info=suppress_info,
            )
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
            effective_action,
            previous_image=current_state,
            traffic_metrics=traffic_metrics,
            step_latency_ms=step_latency_ms,
            target_info=target_info,
            phase_info=phase_info,
        )
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
        info["effective_action"] = effective_action
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

        self.step_index += 1
        return next_state, reward, done, info

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
