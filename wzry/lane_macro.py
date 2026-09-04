import json
import os

import numpy as np

from argparses import args
from phase_controller import VisualStateObserver


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


class LaneMacroController:
    """Scripted opening and early-laning macro inspired by human demos."""

    def __init__(self):
        self.arrival_observer = VisualStateObserver()
        self.reset_episode()

    def reset_episode(self):
        self.step_index = 0
        self.profile = self._load_profile()
        self.route_angles = _parse_int_list(args.lane_opening_route_angles, [195, 205, 190, 175])
        self.route_steps = _parse_int_list(args.lane_opening_route_steps, [8, 14, 12, 8])
        self.route_source_requested = str(args.lane_opening_route_source)
        self.profile_route_trusted = self._profile_route_is_trusted()
        self.route_source = self.route_source_requested
        if self.route_source == "auto":
            self.route_source = "profile" if self.profile_route_trusted else "autowzry_mid"
        if self.route_source == "autowzry_mid":
            self.route_angles = _parse_int_list(args.lane_opening_autowzry_angles, [300, 310, 320])
            self.route_steps = _parse_int_list(args.lane_opening_autowzry_steps, [34, 28, 18])
        elif self.route_source == "profile" and self.profile:
            self.route_angles = [int(value) for value in self.profile.get("route_angles", self.route_angles)]
            self.route_steps = [int(value) for value in self.profile.get("route_steps", self.route_steps)]
        elif self.route_source == "profile":
            self.route_source = "manual"
        if len(self.route_steps) < len(self.route_angles):
            self.route_steps.extend([self.route_steps[-1]] * (len(self.route_angles) - len(self.route_steps)))
        self.opening_total_steps = sum(self.route_steps[:len(self.route_angles)])
        self.opening_profile_move_only = bool(
            self.route_source == "profile" and (self.profile or {}).get("opening_move_only_profile")
        )
        self.opening_base_steps = max(1, self.opening_total_steps, max(0, args.lane_opening_min_move_steps))
        if self.opening_profile_move_only:
            self.opening_move_only_steps = self.opening_base_steps
        else:
            self.opening_move_only_steps = max(0, args.lane_opening_move_only_steps)
        self.opening_max_steps = self.opening_base_steps + max(0, args.lane_arrival_extend_steps)
        self.lane_arrived = False
        self.lane_arrival_reason = "not_checked"
        self.lane_arrival_stop_steps_left = 0
        self.arrival_candidate_steps = 0
        self.minimap_arrival_candidate_steps = 0
        self.minimap_reopen_candidate_steps = 0
        self.opening_contact_candidate_steps = 0
        self.opening_danger_candidate_steps = 0
        self.last_arrival_info = self._arrival_info(False, "not_checked")
        self.laning_started_at = None
        self.last_laning_visual = self._empty_laning_visual()
        self.patrol_angles = _parse_int_list(args.lane_laning_patrol_angles, [175, 190, 205, 190])
        if self.profile:
            self.patrol_angles = [int(value) for value in self.profile.get("patrol_angles", self.patrol_angles)]
        self.opening_attack_start_step = self._profile_int(
            "opening_attack_start_step",
            args.lane_opening_attack_start_step,
            minimum=0,
        )
        self.opening_skill_start_step = self._profile_int(
            "opening_skill_start_step",
            args.lane_opening_skill_start_step,
            minimum=0,
        )
        self.laning_skill_interval_steps = self._profile_int(
            "skill_interval_steps",
            args.lane_laning_skill_interval_steps,
            minimum=3,
        )
        self.laning_last_hit_interval_steps = self._profile_int(
            "last_hit_interval_steps",
            args.lane_laning_last_hit_interval_steps,
            minimum=2,
        )
        self.laning_push_tower_interval_steps = self._profile_int(
            "push_tower_interval_steps",
            args.lane_laning_push_tower_interval_steps,
            minimum=3,
        )
        self.upgrade_cycle = [6, 7, 8, 6, 7, 8]
        self.upgrade_index = 0
        self.next_upgrade_step = 2
        self.next_buy_step = 0
        self.info_schedule = {
            int(item.get("step", -1)): int(item.get("action", 0))
            for item in (self.profile or {}).get("info_schedule", [])
            if int(item.get("action", 0)) != 0
        }
        self.attack_schedule = {
            int(item.get("step", -1)): [
                0,
                0,
                0,
                int(item.get("action", 0)),
                int(item.get("action_type", 0)),
                int(item.get("arg1", 0)),
                int(item.get("arg2", 0)),
                int(item.get("arg3", 0)),
            ]
            for item in (self.profile or {}).get("attack_schedule", [])
            if int(item.get("action", 0)) != 0
        }
        self.last_macro = "lane_start"

    def refine_action(self, action, target_info, current_state=None, minimap_state=None):
        if not args.lane_macro_enabled:
            return action, self._info(False, "disabled", "off")

        minimap_state = minimap_state or {}
        self._reopen_false_minimap_arrival(minimap_state)
        arrival_info = self._detect_lane_arrival(current_state, target_info, minimap_state)
        if not self.lane_arrived and arrival_info["arrived"]:
            self.lane_arrived = True
            self.lane_arrival_reason = arrival_info["reason"]
            self.lane_arrival_stop_steps_left = max(0, args.lane_arrival_stop_steps)
        self.last_arrival_info = arrival_info

        if self.lane_arrival_stop_steps_left > 0:
            self.lane_arrival_stop_steps_left -= 1
            self.step_index += 1
            self.last_macro = "opening_arrived_stop"
            return [0, 0, 0, 0, 0, 0, 0, 0], self._info(
                True,
                "opening",
                "opening_arrived_stop",
                lock_target_tracking=True,
                suppress_attack=True,
                suppress_info=True,
                arrival_info=arrival_info,
            )

        if self._should_continue_opening():
            force_pure_move = self.opening_profile_move_only or self.step_index < self.opening_move_only_steps
            macro_action, macro_name = self._opening_action(
                target_info,
                force_pure_move=force_pure_move,
                minimap_state=minimap_state,
            )
            suppress_attack = macro_name in {"opening_move_only", "opening_minimap_move", "opening_right_until_tower"}
            self.step_index += 1
            self.last_macro = macro_name
            return macro_action, self._info(
                True,
                "opening",
                macro_name,
                lock_target_tracking=True,
                suppress_attack=suppress_attack,
                suppress_info=suppress_attack,
                arrival_info=arrival_info,
            )

        visual_state = self._detect_laning_visual(current_state, target_info, minimap_state)
        if self.laning_started_at is None:
            self.laning_started_at = self.step_index
        laning_step = self._laning_step()

        if laning_step < args.lane_macro_control_steps:
            macro_action, macro_name = self._laning_action(target_info, visual_state, laning_step)
            suppress_move_to_tower = macro_name == "laning_minimap_reposition_to_tower"
            self.step_index += 1
            self.last_macro = macro_name
            return macro_action, self._info(
                True,
                "laning",
                macro_name,
                lock_target_tracking=suppress_move_to_tower,
                suppress_attack=suppress_move_to_tower,
                suppress_info=suppress_move_to_tower,
                visual_state=visual_state,
            )

        self.step_index += 1
        if self._is_idle(action):
            macro_action, macro_name = self._laning_action(target_info, visual_state, laning_step)
            suppress_move_to_tower = macro_name == "laning_minimap_reposition_to_tower"
            self.last_macro = macro_name
            return macro_action, self._info(
                True,
                "fill_idle",
                macro_name,
                lock_target_tracking=suppress_move_to_tower,
                suppress_attack=suppress_move_to_tower,
                suppress_info=suppress_move_to_tower,
                visual_state=visual_state,
            )
        return action, self._info(False, "model", "model_action", visual_state=visual_state)

    def _opening_action(self, target_info, force_pure_move=False, minimap_state=None):
        minimap_state = minimap_state or {}
        pure_move = bool(force_pure_move or self.step_index < self.opening_move_only_steps)
        angle = self._opening_angle(move_only=pure_move)
        if args.lane_opening_force_right_until_tower and not self.lane_arrived:
            angle = int(args.lane_opening_right_angle) % 360
        elif (
            args.minimap_opening_steer_enabled
            and self.step_index >= max(0, int(args.minimap_opening_steer_after_steps))
            and minimap_state.get("minimap_lane_move_angle") is not None
            and not minimap_state.get("minimap_lane_arrived", False)
        ):
            angle = int(minimap_state["minimap_lane_move_angle"]) % 360
        if pure_move:
            if args.lane_opening_force_right_until_tower and not self.lane_arrived:
                macro_name = "opening_right_until_tower"
            else:
                macro_name = "opening_minimap_move" if minimap_state.get("minimap_lane_move_angle") is not None else "opening_move_only"
            return [1, angle, 0, 0, 0, 0, 0, 0], macro_name

        info_action = 0 if pure_move and args.lane_opening_disable_info_actions else self._early_info_action()
        attack_action = 0
        action_type = 0
        arg1 = 0
        arg2 = 0
        arg3 = 0
        macro_name = "opening_run"

        scheduled_attack = self._scheduled_attack_action()
        if scheduled_attack is not None and self.step_index < args.lane_profile_replay_steps and not args.lane_opening_disable_profile_attacks:
            attack_action = scheduled_attack[3]
            action_type = scheduled_attack[4]
            arg1 = scheduled_attack[5]
            arg2 = scheduled_attack[6]
            arg3 = scheduled_attack[7]
            macro_name = "opening_profile_attack"
        elif self.step_index >= self.opening_attack_start_step:
            attack_action = 1
            macro_name = "opening_run_attack"

        if self.step_index >= self.opening_skill_start_step and self.step_index % self.laning_skill_interval_steps == 0:
            attack_action = int(np.random.choice([8, 9], p=[0.58, 0.42]))
            action_type, arg1, arg2, arg3 = self._directional_skill(angle, 0.55)
            macro_name = "opening_run_skill"

        if info_action in [1, 2]:
            macro_name = "opening_buy"
        elif info_action in [6, 7, 8]:
            macro_name = "opening_upgrade"

        return [1, angle, info_action, attack_action, action_type, arg1, arg2, arg3], macro_name

    def _laning_action(self, target_info, visual_state=None, laning_step=None):
        visual_state = visual_state or self.last_laning_visual
        laning_step = self._laning_step() if laning_step is None else int(laning_step)
        target_visible = bool(target_info and target_info.get("visible"))
        target_angle = int(target_info.get("angle", 0)) if target_visible else self._patrol_angle()
        distance_ratio = float(target_info.get("distance_ratio", 1.0)) if target_visible else 1.0
        pressure = float(visual_state.get("center_pressure_score", 0.0))
        low_hp = bool(visual_state.get("low_hp", False))
        info_action = self._early_info_action()
        move_action = 1
        angle = target_angle
        action_type = 0
        arg1 = 0
        arg2 = 0
        arg3 = 0

        if self._minimap_needs_tower_reposition(visual_state):
            if args.lane_opening_force_right_until_tower:
                angle = int(args.lane_opening_right_angle) % 360
            else:
                angle = int(visual_state.get("minimap_lane_move_angle")) % 360
            if args.lane_laning_debug:
                print(
                    "lane_laning",
                    {
                        "step": self.step_index,
                        "laning_step": laning_step,
                        "macro": "laning_minimap_reposition_to_tower",
                        "minimap_distance": visual_state.get("minimap_lane_distance"),
                        "angle": angle,
                        "action": [1, angle, 0, 0, 0, 0, 0, 0],
                    },
                )
            return [1, angle, 0, 0, 0, 0, 0, 0], "laning_minimap_reposition_to_tower"

        if target_visible:
            if args.lane_laning_kite_enabled and (low_hp or distance_ratio <= args.lane_laning_kite_distance):
                move_action = 1
                angle = (target_angle + 180) % 360
                if self.step_index % self.laning_skill_interval_steps == 0 and not low_hp:
                    attack_action = int(np.random.choice([8, 9], p=[0.55, 0.45]))
                    action_type, arg1, arg2, arg3 = self._directional_skill(target_angle, 0.85, distance_ratio)
                    macro_name = "laning_kite_skill"
                else:
                    attack_action = 1
                    macro_name = "laning_low_hp_kite" if low_hp else "laning_kite_attack"
            elif distance_ratio <= args.lane_laning_trade_distance:
                move_action = 0 if args.lane_laning_standstill_clear_enabled else 1
                angle = 0 if move_action == 0 else target_angle
                if self.step_index % self.laning_skill_interval_steps == 0:
                    attack_action = int(np.random.choice([8, 9, 10], p=[0.40, 0.40, 0.20]))
                    action_type, arg1, arg2, arg3 = self._directional_skill(target_angle, 0.95, distance_ratio)
                    macro_name = "laning_trade_skill"
                else:
                    attack_action = 1
                    macro_name = "laning_trade_attack"
            elif self.step_index % self.laning_skill_interval_steps == 0:
                attack_action = int(np.random.choice([8, 9, 10], p=[0.42, 0.38, 0.20]))
                action_type, arg1, arg2, arg3 = self._directional_skill(target_angle, 0.90, distance_ratio)
                macro_name = "laning_target_skill"
            else:
                attack_action = 1
                macro_name = "laning_target_attack"
        elif pressure >= args.lane_laning_hold_pressure_threshold:
            move_action = 0 if args.lane_laning_standstill_clear_enabled else 1
            angle = 0 if move_action == 0 else self._patrol_angle()
            if self.step_index % self.laning_skill_interval_steps == 0:
                attack_action = int(np.random.choice([8, 9], p=[0.58, 0.42]))
                action_type, arg1, arg2, arg3 = self._directional_skill(self._patrol_angle(), 0.70, 0.38)
                macro_name = "laning_hold_clear_skill"
            elif self.step_index % self.laning_last_hit_interval_steps == 0:
                attack_action = 2
                macro_name = "laning_hold_last_hit"
            else:
                attack_action = 1
                macro_name = "laning_hold_attack"
        elif pressure >= args.lane_laning_pressure_threshold:
            move_action = 0 if args.lane_laning_standstill_clear_enabled and laning_step % 3 != 0 else 1
            angle = 0 if move_action == 0 else self._patrol_angle()
            if self.step_index % self.laning_last_hit_interval_steps == 0:
                attack_action = 2
                macro_name = "laning_wave_last_hit"
            elif self.step_index % self.laning_skill_interval_steps == 0:
                attack_action = int(np.random.choice([8, 9], p=[0.55, 0.45]))
                action_type, arg1, arg2, arg3 = self._directional_skill(self._patrol_angle(), 0.55, 0.34)
                macro_name = "laning_wave_clear_skill"
            else:
                attack_action = 1
                macro_name = "laning_wave_attack"
        elif self.step_index % self.laning_push_tower_interval_steps == 0:
            attack_action = 3
            macro_name = "laning_push_tower"
        elif pressure <= args.lane_laning_low_pressure_threshold:
            move_action = 1 if args.lane_laning_find_wave_move_enabled else 0
            angle = self._patrol_angle() if move_action != 0 else 0
            attack_action = 0
            macro_name = "laning_find_wave"
        else:
            attack_action = 1
            macro_name = "laning_patrol_attack"

        scheduled_attack = self._scheduled_attack_action()
        if scheduled_attack is not None and self.step_index < args.lane_profile_replay_steps:
            attack_action = scheduled_attack[3]
            action_type = scheduled_attack[4]
            arg1 = scheduled_attack[5] if scheduled_attack[5] else arg1
            arg2 = scheduled_attack[6] if scheduled_attack[6] else arg2
            arg3 = scheduled_attack[7]
            macro_name = "laning_profile_attack"

        if info_action in [1, 2]:
            macro_name = f"{macro_name}_buy"
        elif info_action in [6, 7, 8]:
            macro_name = f"{macro_name}_upgrade"

        if args.lane_laning_debug:
            print(
                "lane_laning",
                {
                    "step": self.step_index,
                    "laning_step": laning_step,
                    "macro": macro_name,
                    "pressure": round(pressure, 4),
                    "target_visible": target_visible,
                    "distance": round(distance_ratio, 4),
                    "low_hp": low_hp,
                    "action": [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3],
                },
            )

        return [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3], macro_name

    def _opening_angle(self, move_only=False):
        remaining = self.step_index
        route_index = max(0, len(self.route_angles) - 1)
        for index, steps in enumerate(self.route_steps[:len(self.route_angles)]):
            if remaining < steps:
                route_index = index
                break
            remaining -= steps
        base_angle = self.route_angles[min(route_index, len(self.route_angles) - 1)]
        jitter = int(args.lane_opening_move_only_jitter if move_only else args.lane_opening_angle_jitter)
        if jitter > 0:
            base_angle += int(np.random.randint(-jitter, jitter + 1))
        return int(base_angle % 360)

    def _patrol_angle(self):
        index = ((self.step_index - self.opening_total_steps) // 4) % len(self.patrol_angles)
        jitter = int(np.random.randint(-5, 6))
        return int((self.patrol_angles[index] + jitter) % 360)

    def _should_continue_opening(self):
        if self.lane_arrived:
            return False
        if args.lane_opening_force_right_until_tower:
            hard_max_steps = max(self.opening_max_steps, int(args.lane_opening_force_right_hard_max_steps))
            return self.step_index < hard_max_steps
        if self.step_index < self.opening_base_steps:
            return True
        if not args.lane_arrival_detection_enabled:
            return self.step_index < self.opening_total_steps
        if args.lane_opening_extend_last_angle:
            return self.step_index < self.opening_max_steps
        return self.step_index < self.opening_total_steps

    @staticmethod
    def _minimap_blocks_visual_arrival(minimap_state):
        minimap_state = minimap_state or {}
        return bool(
            args.minimap_require_lane_arrival_before_laning
            and args.minimap_lane_arrival_enabled
            and minimap_state.get("minimap_self_visible", False)
            and not minimap_state.get("minimap_lane_arrived", False)
        )

    @staticmethod
    def _minimap_needs_tower_reposition(visual_state):
        visual_state = visual_state or {}
        return bool(
            args.minimap_require_lane_arrival_before_laning
            and args.minimap_lane_arrival_enabled
            and visual_state.get("minimap_self_visible", False)
            and not visual_state.get("minimap_lane_arrived", False)
            and visual_state.get("minimap_lane_move_angle") is not None
        )

    def _reopen_false_minimap_arrival(self, minimap_state):
        if not bool(getattr(args, "minimap_reopen_false_visual_arrival", True)):
            self.minimap_reopen_candidate_steps = 0
            return
        if not self.lane_arrived or not self._minimap_blocks_visual_arrival(minimap_state):
            self.minimap_reopen_candidate_steps = 0
            return

        visual_reasons = {
            "opening_contact_wave",
            "opening_contact_target",
            "opening_high_pressure",
            "opening_close_target",
            "opening_low_hp",
            "center_pressure",
            "target_visible",
            "base_route_complete",
            "max_opening_steps",
            "minimap_lane_tower",
        }
        if self.lane_arrival_reason not in visual_reasons:
            self.minimap_reopen_candidate_steps = 0
            return
        if self.lane_arrival_reason == "minimap_lane_tower":
            try:
                distance = float(minimap_state.get("minimap_lane_distance"))
            except (TypeError, ValueError):
                self.minimap_reopen_candidate_steps = 0
                return
            reopen_distance = max(
                float(args.minimap_lane_arrival_distance) * 2.0,
                float(args.minimap_lane_arrival_distance) + 0.08,
            )
            if distance <= reopen_distance:
                self.minimap_reopen_candidate_steps = 0
                return
            self.minimap_reopen_candidate_steps += 1
            if self.minimap_reopen_candidate_steps < max(1, int(args.minimap_reopen_confirm_steps)):
                return
        else:
            self.minimap_reopen_candidate_steps = 0

        self.lane_arrived = False
        self.laning_started_at = None
        self.lane_arrival_reason = "minimap_reopen_to_tower"
        self.lane_arrival_stop_steps_left = 0
        self.arrival_candidate_steps = 0
        self.opening_contact_candidate_steps = 0
        self.opening_danger_candidate_steps = 0
        self.minimap_reopen_candidate_steps = 0
        self.last_arrival_info = self._arrival_info(
            False,
            "minimap_reopen_to_tower",
            minimap_state=minimap_state,
        )

    def _detect_opening_contact(self, visual_state):
        pressure_score = float(visual_state.get("center_pressure_score", 0.0))
        target_visible = bool(visual_state.get("target_visible", False))
        target_distance_ratio = float(visual_state.get("target_distance_ratio", 1.0))
        pressure_contact = pressure_score >= float(args.lane_opening_contact_pressure_threshold)
        contact_candidate = bool(target_visible or pressure_contact)

        if contact_candidate:
            self.opening_contact_candidate_steps += 1
        else:
            self.opening_contact_candidate_steps = 0

        confirm_steps = max(1, int(args.lane_opening_contact_confirm_steps))
        arrived = bool(self.opening_contact_candidate_steps >= confirm_steps)
        if arrived and target_visible:
            reason = "opening_contact_target"
        elif arrived and pressure_contact:
            reason = "opening_contact_wave"
        elif contact_candidate:
            reason = "opening_contact_pending"
        else:
            reason = "not_contact"

        return {
            "arrived": arrived,
            "reason": reason,
            "pressure_score": pressure_score,
            "target_visible": target_visible,
            "target_distance_ratio": target_distance_ratio,
        }

    def _detect_lane_arrival(self, current_state, target_info, minimap_state=None):
        minimap_state = minimap_state or {}
        if self.lane_arrived:
            return self._arrival_info(True, self.lane_arrival_reason, minimap_state=minimap_state)

        if not args.lane_arrival_detection_enabled:
            self.arrival_candidate_steps = 0
            self.minimap_arrival_candidate_steps = 0
            self.opening_contact_candidate_steps = 0
            self.opening_danger_candidate_steps = 0
            return self._arrival_info(False, "disabled")

        minimap_arrival_mature = self.step_index >= max(0, int(args.minimap_lane_arrival_min_opening_steps))
        early_minimap_arrival = bool(
            args.minimap_lane_arrival_enabled
            and minimap_arrival_mature
            and minimap_state.get("minimap_lane_arrived", False)
            and minimap_state.get("minimap_self")
        )

        if (
            args.lane_opening_ignore_visual_before_base
            and self.step_index < self.opening_base_steps
            and not early_minimap_arrival
        ):
            self.arrival_candidate_steps = 0
            self.minimap_arrival_candidate_steps = 0
            self.opening_contact_candidate_steps = 0
            self.opening_danger_candidate_steps = 0
            return self._arrival_info(False, "before_base_route_complete", minimap_state=minimap_state)

        if (
            args.minimap_lane_arrival_enabled
            and minimap_arrival_mature
            and minimap_state.get("minimap_lane_arrived", False)
        ):
            self.minimap_arrival_candidate_steps += 1
        else:
            self.minimap_arrival_candidate_steps = 0

        if self.minimap_arrival_candidate_steps >= max(1, int(args.minimap_lane_arrival_confirm_steps)):
            self.arrival_candidate_steps = 0
            self.opening_contact_candidate_steps = 0
            self.opening_danger_candidate_steps = 0
            return self._arrival_info(
                True,
                "minimap_lane_tower",
                pressure_score=0.0,
                target_visible=bool(target_info and target_info.get("visible")),
                target_distance_ratio=float(target_info.get("distance_ratio", 1.0)) if target_info else 1.0,
                minimap_state=minimap_state,
            )

        visual_state = None
        contact_info = None
        contact_after = max(0, int(args.lane_opening_contact_detect_after_steps))
        if (
            args.lane_opening_stop_on_contact
            and self.step_index >= contact_after
            and current_state is not None
            and getattr(current_state, "size", 0) != 0
        ):
            visual_state = self.arrival_observer.detect(current_state, target_info, minimap_state)
            contact_info = self._detect_opening_contact(visual_state)
            if contact_info["arrived"]:
                if self._minimap_blocks_visual_arrival(minimap_state):
                    return self._arrival_info(
                        False,
                        "minimap_not_arrived_ignore_contact",
                        pressure_score=contact_info["pressure_score"],
                        target_visible=contact_info["target_visible"],
                        target_distance_ratio=contact_info["target_distance_ratio"],
                        minimap_state=minimap_state,
                    )
                self.arrival_candidate_steps = 0
                self.opening_danger_candidate_steps = 0
                return self._arrival_info(
                    True,
                    contact_info["reason"],
                    pressure_score=contact_info["pressure_score"],
                    target_visible=contact_info["target_visible"],
                    target_distance_ratio=contact_info["target_distance_ratio"],
                    minimap_state=minimap_state,
                )
        elif self.step_index < contact_after:
            self.opening_contact_candidate_steps = 0

        if args.lane_opening_ignore_visual_before_base and self.step_index < self.opening_base_steps:
            self.arrival_candidate_steps = 0
            self.opening_danger_candidate_steps = 0
            if contact_info is not None:
                return self._arrival_info(
                    False,
                    contact_info["reason"],
                    pressure_score=contact_info["pressure_score"],
                    target_visible=contact_info["target_visible"],
                    target_distance_ratio=contact_info["target_distance_ratio"],
                    minimap_state=minimap_state,
                )
            return self._arrival_info(False, "before_base_route_complete", minimap_state=minimap_state)

        if args.lane_opening_force_stop_at_base and self.step_index >= self.opening_base_steps:
            if self._minimap_blocks_visual_arrival(minimap_state):
                self.arrival_candidate_steps = 0
                self.opening_contact_candidate_steps = 0
                self.opening_danger_candidate_steps = 0
                return self._arrival_info(False, "minimap_not_arrived_ignore_base_stop", minimap_state=minimap_state)
            self.arrival_candidate_steps = 0
            self.minimap_arrival_candidate_steps = 0
            self.opening_contact_candidate_steps = 0
            self.opening_danger_candidate_steps = 0
            return self._arrival_info(True, "base_route_complete", minimap_state=minimap_state)

        if current_state is None or getattr(current_state, "size", 0) == 0:
            self.arrival_candidate_steps = 0
            self.minimap_arrival_candidate_steps = 0
            self.opening_contact_candidate_steps = 0
            self.opening_danger_candidate_steps = 0
            return self._arrival_info(False, "no_image", minimap_state=minimap_state)

        if visual_state is None:
            visual_state = self.arrival_observer.detect(current_state, target_info, minimap_state)
        pressure_score = float(visual_state.get("center_pressure_score", 0.0))
        target_visible = bool(visual_state.get("target_visible", False))
        target_distance_ratio = float(visual_state.get("target_distance_ratio", 1.0))
        low_hp = bool(visual_state.get("low_hp", False))

        danger_after = max(0, args.lane_opening_danger_after_steps)
        danger_pressure = pressure_score >= args.lane_opening_danger_pressure_threshold
        danger_target = target_visible and target_distance_ratio <= args.lane_opening_danger_target_distance
        danger_low_hp = bool(args.lane_opening_danger_low_hp_enabled and low_hp)
        danger_candidate = bool(
            self.step_index >= danger_after
            and (danger_pressure or danger_target or danger_low_hp)
        )
        if danger_candidate:
            self.opening_danger_candidate_steps += 1
        else:
            self.opening_danger_candidate_steps = 0
        danger_abort = bool(
            args.lane_opening_abort_on_danger
            and self.opening_danger_candidate_steps >= max(1, args.lane_opening_danger_confirm_steps)
        )
        if danger_abort:
            self.arrival_candidate_steps = 0
            if danger_target:
                reason = "opening_close_target"
            elif danger_pressure:
                reason = "opening_high_pressure"
            else:
                reason = "opening_low_hp"
            if self._minimap_blocks_visual_arrival(minimap_state):
                return self._arrival_info(
                    False,
                    "minimap_not_arrived_ignore_danger",
                    pressure_score=pressure_score,
                    target_visible=target_visible,
                    target_distance_ratio=target_distance_ratio,
                    minimap_state=minimap_state,
                )
            return self._arrival_info(
                True,
                reason,
                pressure_score=pressure_score,
                target_visible=target_visible,
                target_distance_ratio=target_distance_ratio,
                minimap_state=minimap_state,
            )

        detect_after = max(0, args.lane_arrival_detect_after_steps)
        if self.step_index < detect_after:
            self.arrival_candidate_steps = 0
            return self._arrival_info(False, "before_min_steps", minimap_state=minimap_state)

        pressure_arrived = bool(
            args.lane_arrival_pressure_enabled
            and pressure_score >= args.lane_arrival_pressure_threshold
        )
        target_arrived = bool(args.lane_arrival_target_enabled and target_visible)

        arrival_candidate = bool(pressure_arrived or target_arrived)
        if arrival_candidate:
            self.arrival_candidate_steps += 1
        else:
            self.arrival_candidate_steps = 0

        arrived = bool(self.arrival_candidate_steps >= max(1, args.lane_arrival_confirm_steps))
        if arrived and pressure_arrived:
            reason = "center_pressure"
        elif arrived and target_arrived:
            reason = "target_visible"
        elif self.step_index >= self.opening_max_steps:
            arrived = True
            reason = "max_opening_steps"
        elif arrival_candidate:
            reason = "arrival_candidate_pending"
        else:
            reason = "not_arrived"

        if arrived and self._minimap_blocks_visual_arrival(minimap_state):
            arrived = False
            reason = "minimap_not_arrived_ignore_visual"
            self.arrival_candidate_steps = 0

        info = self._arrival_info(
            arrived,
            reason,
            pressure_score=pressure_score,
            target_visible=target_visible,
            target_distance_ratio=target_distance_ratio,
            minimap_state=minimap_state,
        )
        if args.lane_arrival_debug:
            print("lane_arrival", info)
        return info

    def _detect_laning_visual(self, current_state, target_info, minimap_state=None):
        if current_state is None or getattr(current_state, "size", 0) == 0:
            visual_state = self._empty_laning_visual(target_info, minimap_state)
        else:
            visual_state = self.arrival_observer.detect(current_state, target_info, minimap_state)
        self.last_laning_visual = visual_state
        return visual_state

    def _laning_step(self):
        if self.laning_started_at is None:
            return 0
        return max(0, int(self.step_index - self.laning_started_at))

    @staticmethod
    def _empty_laning_visual(target_info=None, minimap_state=None):
        minimap_state = minimap_state or {}
        attack_avatar_visible = bool(minimap_state.get("attack_avatar_visible", False))
        target_visible = bool(target_info and target_info.get("visible")) or attack_avatar_visible
        target_distance_ratio = float(target_info.get("distance_ratio", 1.0)) if target_visible and target_info else 1.0
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

    def _arrival_info(
        self,
        arrived,
        reason,
        pressure_score=0.0,
        target_visible=False,
        target_distance_ratio=1.0,
        minimap_state=None,
    ):
        minimap_state = minimap_state or {}
        return {
            "arrived": bool(arrived),
            "reason": reason,
            "pressure_score": round(float(pressure_score), 4),
            "target_visible": bool(target_visible),
            "target_distance_ratio": round(float(target_distance_ratio), 4),
            "arrival_candidate_steps": int(getattr(self, "arrival_candidate_steps", 0)),
            "arrival_confirm_steps": int(max(1, args.lane_arrival_confirm_steps)),
            "minimap_arrival_candidate_steps": int(getattr(self, "minimap_arrival_candidate_steps", 0)),
            "minimap_arrival_confirm_steps": int(max(1, args.minimap_lane_arrival_confirm_steps)),
            "minimap_lane_arrived": bool(minimap_state.get("minimap_lane_arrived", False)),
            "minimap_lane_distance": minimap_state.get("minimap_lane_distance"),
            "minimap_lane_move_angle": minimap_state.get("minimap_lane_move_angle"),
            "minimap_self_visible": bool(minimap_state.get("minimap_self_visible", False)),
            "contact_candidate_steps": int(getattr(self, "opening_contact_candidate_steps", 0)),
            "contact_confirm_steps": int(max(1, args.lane_opening_contact_confirm_steps)),
            "danger_candidate_steps": int(getattr(self, "opening_danger_candidate_steps", 0)),
            "danger_confirm_steps": int(max(1, args.lane_opening_danger_confirm_steps)),
        }

    def _early_info_action(self):
        if self.step_index in self.info_schedule and self.step_index < args.lane_profile_replay_steps:
            return int(self.info_schedule[self.step_index])

        if self.step_index == 0 or self.step_index == args.lane_opening_second_buy_step:
            return int(np.random.choice([1, 2], p=[0.70, 0.30]))

        if self.step_index >= self.next_upgrade_step:
            info_action = self.upgrade_cycle[self.upgrade_index % len(self.upgrade_cycle)]
            self.upgrade_index += 1
            self.next_upgrade_step = self.step_index + max(7, args.macro_upgrade_interval_steps)
            return int(info_action)

        if self.step_index >= self.next_buy_step and self.step_index > self.opening_total_steps:
            self.next_buy_step = self.step_index + max(18, args.macro_buy_interval_steps)
            return int(np.random.choice([1, 2], p=[0.65, 0.35]))

        return 0

    def _scheduled_attack_action(self):
        return self.attack_schedule.get(self.step_index)

    def _directional_skill(self, angle, directional_prob=0.6, distance_ratio=0.38):
        if np.random.rand() <= directional_prob:
            distance = int(max(28, min(85, distance_ratio * 160)))
            return 1, int(angle % 360), distance, 0
        return 0, 0, 0, 0

    @staticmethod
    def _is_idle(action):
        move_action, _, info_action, attack_action, _, _, _, _ = [int(v) for v in action]
        return move_action == 0 and info_action == 0 and attack_action == 0

    def _info(
        self,
        override,
        phase,
        macro_name,
        lock_target_tracking=False,
        suppress_attack=False,
        suppress_info=False,
        arrival_info=None,
        visual_state=None,
    ):
        arrival_info = arrival_info or self.last_arrival_info
        visual_state = visual_state or self.last_laning_visual
        own_hp_ratio = visual_state.get("own_hp_ratio")
        return {
            "lane_macro_enabled": bool(args.lane_macro_enabled),
            "lane_profile_loaded": bool(self.profile),
            "lane_macro_override": bool(override),
            "lane_macro_phase": phase,
            "lane_macro_name": macro_name,
            "lane_macro_step": self.step_index,
            "lane_macro_lock_target_tracking": bool(lock_target_tracking),
            "lane_macro_suppress_attack": bool(suppress_attack),
            "lane_macro_suppress_info": bool(suppress_info),
            "lane_opening_profile_move_only": bool(self.opening_profile_move_only),
            "lane_opening_route_source_requested": str(getattr(self, "route_source_requested", "")),
            "lane_opening_route_source": str(getattr(self, "route_source", "")),
            "lane_opening_profile_route_trusted": bool(getattr(self, "profile_route_trusted", False)),
            "lane_opening_route_angles": [int(value) for value in self.route_angles],
            "lane_opening_route_steps": [int(value) for value in self.route_steps[:len(self.route_angles)]],
            "lane_opening_total_steps": int(self.opening_total_steps),
            "lane_opening_base_steps": int(self.opening_base_steps),
            "lane_opening_max_steps": int(self.opening_max_steps),
            "lane_opening_move_only_steps": int(self.opening_move_only_steps),
            "lane_opening_attack_start_step": int(self.opening_attack_start_step),
            "lane_opening_skill_start_step": int(self.opening_skill_start_step),
            "lane_arrived": bool(self.lane_arrived),
            "lane_arrival_reason": arrival_info["reason"],
            "lane_arrival_pressure_score": float(arrival_info["pressure_score"]),
            "lane_arrival_target_visible": bool(arrival_info["target_visible"]),
            "lane_arrival_target_distance_ratio": float(arrival_info["target_distance_ratio"]),
            "lane_arrival_candidate_steps": int(arrival_info.get("arrival_candidate_steps", 0)),
            "lane_arrival_confirm_steps": int(arrival_info.get("arrival_confirm_steps", 1)),
            "lane_minimap_arrival_candidate_steps": int(arrival_info.get("minimap_arrival_candidate_steps", 0)),
            "lane_minimap_arrival_confirm_steps": int(arrival_info.get("minimap_arrival_confirm_steps", 1)),
            "lane_minimap_lane_arrived": bool(arrival_info.get("minimap_lane_arrived", False)),
            "lane_minimap_lane_distance": arrival_info.get("minimap_lane_distance"),
            "lane_minimap_lane_move_angle": arrival_info.get("minimap_lane_move_angle"),
            "lane_minimap_self_visible": bool(arrival_info.get("minimap_self_visible", False)),
            "lane_opening_contact_candidate_steps": int(arrival_info.get("contact_candidate_steps", 0)),
            "lane_opening_contact_confirm_steps": int(arrival_info.get("contact_confirm_steps", 1)),
            "lane_opening_danger_candidate_steps": int(arrival_info.get("danger_candidate_steps", 0)),
            "lane_opening_danger_confirm_steps": int(arrival_info.get("danger_confirm_steps", 1)),
            "lane_arrival_stop_steps_left": int(self.lane_arrival_stop_steps_left),
            "lane_laning_step": int(self._laning_step()),
            "lane_laning_pressure_score": round(float(visual_state.get("center_pressure_score", 0.0)), 4),
            "lane_laning_own_hp_ratio": None if own_hp_ratio is None else round(float(own_hp_ratio), 4),
            "lane_laning_low_hp": bool(visual_state.get("low_hp", False)),
            "lane_laning_target_visible": bool(visual_state.get("target_visible", False)),
            "lane_laning_target_distance_ratio": round(float(visual_state.get("target_distance_ratio", 1.0)), 4),
            "lane_laning_skill_interval_steps": int(self.laning_skill_interval_steps),
            "lane_laning_last_hit_interval_steps": int(self.laning_last_hit_interval_steps),
            "lane_laning_push_tower_interval_steps": int(self.laning_push_tower_interval_steps),
        }

    def _profile_int(self, key, default_value, minimum=0):
        raw_value = (self.profile or {}).get(key, default_value)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = int(default_value)
        return max(int(minimum), value)

    def _profile_route_is_trusted(self):
        if not self.profile:
            return False
        if not self.profile.get("route_angles") or not self.profile.get("route_steps"):
            return False
        try:
            move_ratio = float(self.profile.get("opening_move_ratio", 0.0))
        except (TypeError, ValueError):
            move_ratio = 0.0
        return bool(
            self.profile.get("opening_move_only_profile")
            and self.profile.get("opening_touch_route")
            and move_ratio >= float(args.lane_opening_profile_min_move_ratio)
        )

    @staticmethod
    def _load_profile():
        if not args.lane_profile_enabled or not args.lane_profile_path:
            return None
        if not os.path.exists(args.lane_profile_path):
            return None
        try:
            with open(args.lane_profile_path, "r", encoding="utf-8") as file:
                profile = json.load(file)
            if profile.get("route_angles") and profile.get("route_steps"):
                return profile
        except (OSError, ValueError, TypeError):
            return None
        return None
