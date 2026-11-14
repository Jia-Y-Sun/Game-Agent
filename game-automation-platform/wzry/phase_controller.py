import math

import cv2
import numpy as np

from argparses import args


def _angular_distance(first, second):
    return abs((first - second + 180) % 360 - 180)


def _parse_int_list(raw_value, default_values):
    values = []
    for item in str(raw_value).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values or list(default_values)


class VisualStateObserver:
    """Cheap visual cues used by the phase controller.

    The current project does not have game-engine state. These detectors are
    intentionally lightweight and conservative: if a cue is uncertain, they
    return None/False instead of forcing a risky phase.
    """

    def detect(self, image, target_info, minimap_state=None):
        if image is None or image.size == 0:
            return self._empty(target_info, minimap_state)

        height, width = image.shape[:2]
        minimap_state = minimap_state or {}
        own_hp_ratio = self._estimate_own_hp(image)
        pressure_score = self._estimate_center_pressure(image)
        attack_avatar_visible = bool(minimap_state.get("attack_avatar_visible", False))
        target_visible = bool(target_info and target_info.get("visible")) or attack_avatar_visible
        target_distance_ratio = (
            float(target_info.get("distance_ratio", 1.0))
            if target_visible and target_info
            else 1.0
        )
        if attack_avatar_visible and (not target_info or not target_info.get("visible")):
            target_distance_ratio = min(target_distance_ratio, 0.18)

        low_hp = own_hp_ratio is not None and own_hp_ratio <= args.phase_low_hp_threshold
        close_target = target_visible and target_distance_ratio <= args.phase_trade_distance

        return {
            "own_hp_ratio": own_hp_ratio,
            "low_hp": bool(low_hp),
            "center_pressure_score": pressure_score,
            "target_visible": target_visible,
            "target_close": bool(close_target),
            "target_distance_ratio": target_distance_ratio,
            "screen_size": (int(width), int(height)),
            "attack_avatar_visible": attack_avatar_visible,
            "minimap_enemy_visible": bool(minimap_state.get("minimap_enemy_visible", False)),
            "minimap_enemy_seek_angle": minimap_state.get("minimap_enemy_seek_angle"),
            "minimap_enemy_distance": minimap_state.get("minimap_enemy_distance"),
            "minimap_lane_arrived": bool(minimap_state.get("minimap_lane_arrived", False)),
            "minimap_lane_distance": minimap_state.get("minimap_lane_distance"),
            "minimap_lane_move_angle": minimap_state.get("minimap_lane_move_angle"),
            "minimap_self_visible": bool(minimap_state.get("minimap_self_visible", False)),
            "dead_screen_dark": bool(minimap_state.get("dead_screen_dark", False)),
            "dark_mean_v": float(minimap_state.get("dark_mean_v", 0.0) or 0.0),
        }

    def _estimate_own_hp(self, image):
        height, width = image.shape[:2]
        left = int(width * args.phase_self_hp_roi_left)
        right = int(width * args.phase_self_hp_roi_right)
        top = int(height * args.phase_self_hp_roi_top)
        bottom = int(height * args.phase_self_hp_roi_bottom)
        if right <= left or bottom <= top:
            return None

        roi = image[top:bottom, left:right]
        if roi.size == 0:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        green_mask = cv2.inRange(hsv, np.array([35, 70, 70]), np.array([95, 255, 255]))
        kernel = np.ones((2, 8), np.uint8)
        green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_OPEN, kernel)
        green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if w <= 0 or h <= 0 or area < 12:
                continue
            aspect = w / float(h)
            if aspect < 2.0 or aspect > 25.0:
                continue
            cx = left + x + w / 2.0
            cy = top + y + h / 2.0
            center_bias = abs(cx / width - 0.50) + abs(cy / height - 0.52) * 0.6
            candidates.append((center_bias, w, area))

        if not candidates:
            return None

        candidates.sort(key=lambda item: (item[0], -item[2]))
        _, bar_width, _ = candidates[0]
        expected_width = max(24.0, width * args.phase_self_hp_expected_width_ratio)
        return round(float(max(0.0, min(1.0, bar_width / expected_width))), 4)

    def _estimate_center_pressure(self, image):
        height, width = image.shape[:2]
        top = int(height * 0.16)
        bottom = int(height * 0.72)
        left = int(width * 0.18)
        right = int(width * 0.86)
        roi = image[top:bottom, left:right]
        if roi.size == 0:
            return 0.0

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        red_mask_1 = cv2.inRange(hsv, np.array([0, 80, 80]), np.array([12, 255, 255]))
        red_mask_2 = cv2.inRange(hsv, np.array([168, 80, 80]), np.array([179, 255, 255]))
        blue_mask = cv2.inRange(hsv, np.array([95, 70, 70]), np.array([135, 255, 255]))
        mask = cv2.bitwise_or(cv2.bitwise_or(red_mask_1, red_mask_2), blue_mask)
        score = float(np.count_nonzero(mask)) / float(max(1, mask.size))
        return round(min(score * 12.0, 1.0), 4)

    @staticmethod
    def _empty(target_info, minimap_state=None):
        minimap_state = minimap_state or {}
        attack_avatar_visible = bool(minimap_state.get("attack_avatar_visible", False))
        target_visible = bool(target_info and target_info.get("visible")) or attack_avatar_visible
        target_distance_ratio = (
            float(target_info.get("distance_ratio", 1.0))
            if target_visible and target_info
            else 1.0
        )
        if attack_avatar_visible and (not target_info or not target_info.get("visible")):
            target_distance_ratio = min(target_distance_ratio, 0.18)
        return {
            "own_hp_ratio": None,
            "low_hp": False,
            "center_pressure_score": 0.0,
            "target_visible": target_visible,
            "target_close": False,
            "target_distance_ratio": target_distance_ratio,
            "screen_size": (0, 0),
            "attack_avatar_visible": attack_avatar_visible,
            "minimap_enemy_visible": bool(minimap_state.get("minimap_enemy_visible", False)),
            "minimap_enemy_seek_angle": minimap_state.get("minimap_enemy_seek_angle"),
            "minimap_enemy_distance": minimap_state.get("minimap_enemy_distance"),
            "minimap_lane_arrived": bool(minimap_state.get("minimap_lane_arrived", False)),
            "minimap_lane_distance": minimap_state.get("minimap_lane_distance"),
            "minimap_lane_move_angle": minimap_state.get("minimap_lane_move_angle"),
            "minimap_self_visible": bool(minimap_state.get("minimap_self_visible", False)),
            "dead_screen_dark": bool(minimap_state.get("dead_screen_dark", False)),
            "dark_mean_v": float(minimap_state.get("dark_mean_v", 0.0) or 0.0),
        }


class GamePhaseController:
    """Turns early macro play into a longer phase-based gameplay loop."""

    def __init__(self):
        self.observer = VisualStateObserver()
        self.reset_episode()

    def reset_episode(self):
        self.step_index = 0
        self.phase = "opening"
        self.retreat_steps_left = 0
        self.recover_steps_left = 0
        self.low_hp_candidate_steps = 0
        self.no_wave_steps = 0
        self.last_event = None
        self.patrol_angles = _parse_int_list(args.phase_laning_patrol_angles, [175, 190, 205, 190])

    def update_event(self, event_name):
        self.last_event = event_name
        if event_name == "death":
            self.recover_steps_left = max(self.recover_steps_left, args.phase_recover_steps)

    def refine_action(self, action, image, target_info, lane_info, minimap_state=None):
        if not args.phase_controller_enabled:
            self.step_index += 1
            return action, self._info(False, "disabled", "off", {}, reason="disabled")

        visual_state = self.observer.detect(image, target_info, minimap_state)
        phase, reason = self._select_phase(visual_state, lane_info)

        if phase == "opening":
            refined = [int(v) for v in action]
            override = False
            action_name = "opening_passthrough"
        elif phase == "retreat":
            refined = self._retreat_action(action, target_info)
            override = True
            action_name = "retreat_safe"
        elif phase == "recover":
            refined = self._recover_action(action, target_info)
            override = True
            action_name = "recover_space"
        elif phase == "trade":
            refined = self._trade_action(action, target_info)
            override = True
            action_name = "trade_target"
        elif phase == "chase":
            refined = self._chase_action(action, target_info)
            override = True
            action_name = "chase_target"
        elif phase == "clear_wave":
            refined = self._clear_wave_action(action, visual_state)
            override = True
            action_name = "clear_wave"
        elif phase == "poke":
            refined = self._poke_action(action, target_info)
            override = True
            action_name = "poke_target"
        elif phase == "hold_lane":
            refined = self._hold_lane_action(action)
            override = True
            action_name = "hold_lane"
        elif phase == "map_seek":
            refined = self._map_seek_action(action, visual_state)
            override = True
            action_name = "map_seek_enemy"
        elif phase == "push_tower":
            refined = self._push_tower_action(action)
            override = True
            action_name = "push_tower"
        else:
            refined, override, action_name = self._laning_action(action, visual_state, lane_info)

        if args.phase_debug:
            print("phase", phase, action_name, reason, visual_state)

        info = self._info(override, phase, action_name, visual_state, reason=reason)
        self.phase = phase
        self.step_index += 1
        return refined, info

    def _select_phase(self, visual_state, lane_info):
        lane_phase = (lane_info or {}).get("lane_macro_phase")
        lane_macro_name = (lane_info or {}).get("lane_macro_name", "")
        arrival_reason = (lane_info or {}).get("lane_arrival_reason", "")
        opening_danger_reasons = {"opening_low_hp", "opening_close_target", "opening_high_pressure"}
        if lane_macro_name == "laning_minimap_reposition_to_tower":
            self.low_hp_candidate_steps = 0
            return "opening", "lane_macro_tower_reposition"
        if lane_phase == "opening" and arrival_reason not in opening_danger_reasons:
            self.low_hp_candidate_steps = 0
            return "opening", "lane_macro_opening"

        if visual_state.get("dead_screen_dark"):
            self.low_hp_candidate_steps = 0
            return "recover", "dark_death_screen"

        if self.retreat_steps_left > 0:
            self.retreat_steps_left -= 1
            return "retreat", "retreat_hold"

        if self.recover_steps_left > 0:
            self.recover_steps_left -= 1
            return "recover", "death_or_recover_hold"

        if visual_state.get("low_hp"):
            self.low_hp_candidate_steps += 1
        else:
            self.low_hp_candidate_steps = 0

        if self.low_hp_candidate_steps >= max(1, args.phase_low_hp_confirm_steps):
            self.retreat_steps_left = max(0, args.phase_retreat_steps - 1)
            self.low_hp_candidate_steps = 0
            return "retreat", "low_hp_confirmed"

        if args.rookie_laner_enabled:
            return self._select_rookie_phase(visual_state)

        if visual_state.get("target_close"):
            return "trade", "close_target"

        if visual_state.get("target_visible"):
            return "chase", "visible_target"

        if self.step_index >= args.phase_push_start_step:
            interval = max(3, args.phase_push_tower_interval_steps)
            if self.step_index % interval == 0:
                return "push_tower", "push_interval"

        return "laning", "default_laning"

    def _select_rookie_phase(self, visual_state):
        pressure = float(visual_state.get("center_pressure_score", 0.0))
        target_visible = bool(visual_state.get("target_visible", False))
        distance_ratio = float(visual_state.get("target_distance_ratio", 1.0))
        wave_visible = pressure >= args.rookie_laner_wave_pressure

        if wave_visible or target_visible:
            self.no_wave_steps = 0
        else:
            self.no_wave_steps += 1

        if bool(visual_state.get("attack_avatar_visible", False)):
            return "trade", "rookie_attack_avatar_trade"

        if target_visible and distance_ratio <= args.rookie_laner_trade_distance:
            return "trade", "rookie_close_trade"

        if wave_visible:
            if (
                self.step_index >= args.phase_push_start_step
                and pressure >= args.rookie_laner_push_pressure
                and self.step_index % max(3, args.phase_push_tower_interval_steps) == 0
            ):
                return "push_tower", "rookie_safe_push"
            return "clear_wave", "rookie_clear_wave"

        if target_visible and distance_ratio <= args.rookie_laner_poke_distance:
            return "poke", "rookie_poke_without_chase"

        if target_visible and args.rookie_laner_chase_enabled:
            return "chase", "rookie_chase_enabled"

        if (
            args.minimap_enemy_seek_enabled
            and visual_state.get("minimap_enemy_seek_angle") is not None
            and self.no_wave_steps >= max(1, args.minimap_enemy_seek_interval_steps)
        ):
            self.no_wave_steps = 0
            return "map_seek", "rookie_minimap_enemy_seek"

        return "hold_lane", "rookie_hold_lane"

    def _retreat_action(self, action, target_info):
        _, _, _, _, _, _, _, _ = [int(v) for v in action]
        if target_info and target_info.get("visible"):
            angle = (int(target_info.get("angle", args.phase_retreat_angle)) + 180) % 360
        else:
            angle = int(args.phase_retreat_angle) % 360
        attack_action = 5 if self.step_index % 3 == 0 else 1
        return [1, angle, 0, attack_action, 0, 0, 0, 0]

    def _recover_action(self, action, target_info):
        if target_info and target_info.get("visible"):
            return self._retreat_action(action, target_info)
        if self.step_index % 3 == 0:
            return [0, 0, 0, 5, 0, 0, 0, 0]
        return [1, int(args.phase_retreat_angle) % 360, 0, 0, 0, 0, 0, 0]

    def _trade_action(self, action, target_info):
        target_angle = int(target_info.get("angle", 0)) if target_info else int(action[1])
        distance_ratio = float(target_info.get("distance_ratio", 0.35)) if target_info else 0.35
        move_action = 0 if args.rookie_laner_enabled and distance_ratio <= args.rookie_laner_trade_distance else 1
        move_angle = 0 if move_action == 0 else target_angle
        interval = max(3, args.phase_trade_skill_interval_steps)
        if self.step_index % interval == 0:
            skill = int(np.random.choice([8, 9, 10], p=[0.42, 0.38, 0.20]))
            return [move_action, move_angle, int(action[2]), skill, 1, target_angle, self._skill_distance(distance_ratio), 0]
        return [move_action, move_angle, int(action[2]), 1, 0, 0, 0, 0]

    def _chase_action(self, action, target_info):
        target_angle = int(target_info.get("angle", int(action[1]))) if target_info else int(action[1])
        distance_ratio = float(target_info.get("distance_ratio", 0.6)) if target_info else 0.6
        interval = max(4, args.phase_chase_skill_interval_steps)
        if self.step_index % interval == 0:
            skill = int(np.random.choice([8, 9], p=[0.55, 0.45]))
            return [1, target_angle, int(action[2]), skill, 1, target_angle, self._skill_distance(distance_ratio), 0]
        return [1, target_angle, int(action[2]), 1, 0, 0, 0, 0]

    def _push_tower_action(self, action):
        angle = self._patrol_angle()
        if args.rookie_laner_enabled:
            return [0, 0, int(action[2]), 3, 0, 0, 0, 0]
        return [1, angle, int(action[2]), 3, 0, 0, 0, 0]

    def _clear_wave_action(self, action, visual_state):
        angle = self._patrol_angle()
        info_action = int(action[2])
        pressure = float(visual_state.get("center_pressure_score", 0.0))
        high_pressure = pressure >= args.rookie_laner_push_pressure

        if self.step_index % max(3, args.phase_clear_skill_interval_steps) == 0:
            skill = int(np.random.choice([8, 9], p=[0.55, 0.45]))
            return [0, 0, info_action, skill, 1, angle, 45 if high_pressure else 35, 0]

        if self.step_index % max(2, args.phase_last_hit_interval_steps) == 0:
            return [0, 0, info_action, 2, 0, 0, 0, 0]

        return [0, 0, info_action, 1, 0, 0, 0, 0]

    def _poke_action(self, action, target_info):
        target_angle = int(target_info.get("angle", int(action[1]))) if target_info else int(action[1])
        distance_ratio = float(target_info.get("distance_ratio", 0.42)) if target_info else 0.42
        move_action = 1 if args.rookie_laner_poke_move_enabled else 0
        move_angle = target_angle if move_action else 0
        interval = max(4, args.phase_chase_skill_interval_steps)

        if self.step_index % interval == 0:
            skill = int(np.random.choice([8, 9], p=[0.55, 0.45]))
            return [move_action, move_angle, int(action[2]), skill, 1, target_angle, self._skill_distance(distance_ratio), 0]

        return [move_action, move_angle, int(action[2]), 1, 0, 0, 0, 0]

    def _hold_lane_action(self, action):
        info_action = int(action[2])
        if (
            args.rookie_laner_no_wave_patrol_enabled
            and self.no_wave_steps >= max(1, args.rookie_laner_no_wave_patrol_interval_steps)
        ):
            self.no_wave_steps = 0
            return [1, self._patrol_angle(), info_action, 0, 0, 0, 0, 0]

        if self.step_index % max(1, args.rookie_laner_hold_attack_interval_steps) == 0:
            return [0, 0, info_action, 1, 0, 0, 0, 0]

        return [0, 0, info_action, 0, 0, 0, 0, 0]

    def _map_seek_action(self, action, visual_state):
        info_action = int(action[2])
        raw_angle = visual_state.get("minimap_enemy_seek_angle")
        try:
            angle = int(raw_angle) % 360
        except (TypeError, ValueError):
            angle = self._patrol_angle()
        return [1, angle, info_action, 0, 0, 0, 0, 0]

    def _laning_action(self, action, visual_state, lane_info=None):
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = [int(v) for v in action]
        override = False
        action_name = "model_laning"
        lane_macro_name = (lane_info or {}).get("lane_macro_name", "")
        lane_macro_holds_position = (
            bool((lane_info or {}).get("lane_macro_override"))
            and str(lane_macro_name).startswith(("laning_hold", "laning_wave", "laning_trade", "laning_find_wave"))
        )

        if move_action == 0 and not lane_macro_holds_position:
            move_action = 1
            angle = self._patrol_angle()
            override = True
            action_name = "laning_fill_move"
        elif move_action == 0 and lane_macro_holds_position:
            action_name = "laning_hold_position"

        if attack_action == 0:
            override = True
            pressure = float(visual_state.get("center_pressure_score", 0.0))
            if self.step_index % max(2, args.phase_last_hit_interval_steps) == 0:
                attack_action = 2
                action_name = "laning_last_hit"
            elif pressure >= args.phase_clear_wave_pressure and self.step_index % max(3, args.phase_clear_skill_interval_steps) == 0:
                attack_action = int(np.random.choice([8, 9], p=[0.55, 0.45]))
                action_type = 1
                arg1 = angle
                arg2 = 45
                arg3 = 0
                action_name = "laning_clear_skill"
            else:
                attack_action = 1
                action_name = "laning_basic_attack"

        return [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3], override, action_name

    def _patrol_angle(self):
        if not self.patrol_angles:
            return 190
        index = (self.step_index // 4) % len(self.patrol_angles)
        jitter = int(np.random.randint(-6, 7))
        return int((self.patrol_angles[index] + jitter) % 360)

    @staticmethod
    def _skill_distance(distance_ratio):
        return int(max(26, min(95, distance_ratio * 150)))

    def _info(self, override, phase, action_name, visual_state, reason):
        own_hp_ratio = visual_state.get("own_hp_ratio")
        target_distance_ratio = visual_state.get("target_distance_ratio", 0.0)
        return {
            "phase_controller_enabled": bool(args.phase_controller_enabled),
            "phase_override": bool(override),
            "phase": phase,
            "phase_action_name": action_name,
            "phase_reason": reason,
            "phase_step": int(self.step_index),
            "phase_own_hp_ratio": None if own_hp_ratio is None else round(float(own_hp_ratio), 4),
            "phase_low_hp": bool(visual_state.get("low_hp", False)),
            "phase_center_pressure_score": round(float(visual_state.get("center_pressure_score", 0.0)), 4),
            "phase_target_visible": bool(visual_state.get("target_visible", False)),
            "phase_target_close": bool(visual_state.get("target_close", False)),
            "phase_target_distance_ratio": round(float(target_distance_ratio), 4),
            "phase_attack_avatar_visible": bool(visual_state.get("attack_avatar_visible", False)),
            "phase_minimap_enemy_visible": bool(visual_state.get("minimap_enemy_visible", False)),
            "phase_minimap_enemy_seek_angle": visual_state.get("minimap_enemy_seek_angle"),
            "phase_minimap_enemy_distance": visual_state.get("minimap_enemy_distance"),
            "phase_minimap_lane_arrived": bool(visual_state.get("minimap_lane_arrived", False)),
            "phase_minimap_lane_distance": visual_state.get("minimap_lane_distance"),
            "phase_minimap_lane_move_angle": visual_state.get("minimap_lane_move_angle"),
            "phase_minimap_self_visible": bool(visual_state.get("minimap_self_visible", False)),
            "phase_dead_screen_dark": bool(visual_state.get("dead_screen_dark", False)),
            "phase_dark_mean_v": round(float(visual_state.get("dark_mean_v", 0.0)), 2),
            "phase_rookie_laner_enabled": bool(args.rookie_laner_enabled),
            "phase_no_wave_steps": int(getattr(self, "no_wave_steps", 0)),
            "phase_low_hp_candidate_steps": int(getattr(self, "low_hp_candidate_steps", 0)),
            "phase_low_hp_confirm_steps": int(max(1, args.phase_low_hp_confirm_steps)),
            "phase_retreat_steps_left": int(self.retreat_steps_left),
            "phase_recover_steps_left": int(self.recover_steps_left),
        }
