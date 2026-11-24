import numpy as np

from argparses import args


class GameplayBehaviorPrior:
    """Stateful macro-action sampler for human-like early exploration."""

    def __init__(self):
        self.reset_episode()

    def reset_episode(self):
        self.step_index = 0
        self.current_angle = int(np.random.choice([330, 345, 0, 15, 30]))
        self.direction_steps_left = args.macro_direction_hold_steps
        self.next_buy_step = 0
        self.next_upgrade_step = max(1, args.info_action_cooldown_steps)
        self.upgrade_cycle = [6, 7, 8, 6, 7, 8]
        self.upgrade_index = 0
        self.last_macro = "start"

    def sample(self):
        macro = self._choose_macro()
        action = self._macro_to_action(macro)
        self.last_macro = macro
        self.step_index += 1
        return action, macro

    def _choose_macro(self):
        if self.step_index >= self.next_buy_step:
            self.next_buy_step = self.step_index + max(10, args.macro_buy_interval_steps)
            return "buy_equipment"

        if self.step_index >= self.next_upgrade_step:
            self.next_upgrade_step = self.step_index + max(6, args.macro_upgrade_interval_steps)
            return "upgrade_skill"

        if self.step_index < 30:
            macros = [
                "move_attack",
                "move_attack",
                "last_hit",
                "skill_1",
                "skill_2",
                "move_only",
                "push_tower",
            ]
            probs = [0.34, 0.22, 0.13, 0.12, 0.08, 0.07, 0.04]
        else:
            macros = [
                "move_attack",
                "move_attack",
                "last_hit",
                "skill_1",
                "skill_2",
                "skill_3",
                "push_tower",
                "move_only",
                "retreat",
                "recover",
            ]
            probs = [0.25, 0.20, 0.12, 0.12, 0.10, 0.08, 0.05, 0.04, 0.03, 0.01]

        return str(np.random.choice(macros, p=probs))

    def _lane_angle(self):
        self.direction_steps_left -= 1
        if self.direction_steps_left <= 0:
            self.direction_steps_left = int(np.random.randint(
                max(4, args.macro_direction_hold_steps),
                max(6, args.macro_direction_hold_steps * 2 + 1),
            ))
            target_angle = int(np.random.choice([300, 315, 330, 345, 0, 15, 30, 45, 60]))
            self.current_angle = self._turn_towards(self.current_angle, target_angle, max_delta=35)
        else:
            self.current_angle = int((self.current_angle + np.random.randint(-8, 9)) % 360)
        return self.current_angle

    def _retreat_angle(self):
        return int((self._lane_angle() + 180 + np.random.randint(-12, 13)) % 360)

    def _turn_towards(self, current_angle, target_angle, max_delta=35):
        delta = (target_angle - current_angle + 180) % 360 - 180
        delta = int(np.clip(delta, -max_delta, max_delta))
        return int((current_angle + delta) % 360)

    def _skill_args(self, angle, directional_prob=0.30):
        if np.random.rand() < directional_prob:
            return 1, int((angle + np.random.randint(-15, 16)) % 360), int(np.random.randint(35, 76)), 0
        return 0, 0, 0, 0

    def _macro_to_action(self, macro):
        move_action = 1
        angle = self._lane_angle()
        info_action = 0
        attack_action = 1
        action_type = 0
        arg1 = 0
        arg2 = 0
        arg3 = 0

        if macro == "buy_equipment":
            move_action = 0
            angle = 0
            info_action = int(np.random.choice([1, 2], p=[0.65, 0.35]))
            attack_action = 0
        elif macro == "upgrade_skill":
            move_action = 0
            angle = 0
            info_action = self.upgrade_cycle[self.upgrade_index % len(self.upgrade_cycle)]
            self.upgrade_index += 1
            attack_action = 0
        elif macro == "move_only":
            attack_action = 0
        elif macro == "move_attack":
            attack_action = 1
        elif macro == "last_hit":
            attack_action = 2
        elif macro == "push_tower":
            attack_action = 3
        elif macro == "skill_1":
            attack_action = 8
            action_type, arg1, arg2, arg3 = self._skill_args(angle, directional_prob=0.25)
        elif macro == "skill_2":
            attack_action = 9
            action_type, arg1, arg2, arg3 = self._skill_args(angle, directional_prob=0.35)
        elif macro == "skill_3":
            attack_action = 10
            action_type, arg1, arg2, arg3 = self._skill_args(angle, directional_prob=0.20)
        elif macro == "retreat":
            angle = self._retreat_angle()
            attack_action = int(np.random.choice([0, 1, 5], p=[0.55, 0.25, 0.20]))
        elif macro == "recover":
            move_action = 0
            angle = 0
            attack_action = 5

        return [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]
