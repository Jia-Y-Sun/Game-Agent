from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
import numpy as np
from ppocronnx import TextSystem

from argparses import args, device
from globalInfo import GlobalInfo
from onnxRunner import OnnxRunner


class GetRewordUtil:
    def __init__(self):
        self.device = device
        self.globalInfo = GlobalInfo()
        self.death_check = OnnxRunner('models/death.onnx', classes=['death'])
        self.text_sys = TextSystem()
        self.executor = ThreadPoolExecutor(max_workers=3)

        self.frame_change_target = 0.08
        self.frame_change_weight = 1.5
        self.interaction_bonus = 0.2
        self.idle_penalty = 0.15
        self.novelty_bonus = 0.2
        self.move_bonus = 0.1
        self.smooth_move_bonus = 0.15
        self.reverse_turn_penalty = 0.2
        self.ineffective_action_penalty = 0.15

        self.reset_episode()

    def reset_episode(self):
        self.recent_action_signatures = deque(maxlen=64)
        self.last_move_angle = None
        self.step_index = 0

    def predict_attack_delta(self, previous_image, image):
        if previous_image is None or previous_image.size == 0:
            return False, 0

        previous_attack_visible, previous_reward_count = self.calculate_attack_reword(previous_image)
        current_attack_visible, current_reward_count = self.calculate_attack_reword(image)
        if not previous_attack_visible or not current_attack_visible:
            return False, 0

        reward_delta = max(0, current_reward_count - previous_reward_count)
        return reward_delta > 0, reward_delta

    def calculate_attack_reword(self, img):
        image_height, image_width = img.shape[:2]
        width = image_width * 0.116
        height = image_height * 0.024
        total_area = int(width * height)

        left = int(image_width * 0.57)
        top = int(image_height * 0.019)
        right = int(left + width)
        bottom = int(top + height)
        cropped_img = img[top:bottom, left:right]

        hsv_image = cv2.cvtColor(cropped_img, cv2.COLOR_BGR2HSV)
        bgr_color = np.uint8([[[62, 54, 175]]])
        hsv_color = cv2.cvtColor(bgr_color, cv2.COLOR_BGR2HSV)
        hue = hsv_color[0][0][0]

        tolerance = 10
        lower_bound = np.array([hue - tolerance, 50, 50])
        upper_bound = np.array([hue + tolerance, 255, 255])
        mask = cv2.inRange(hsv_image, lower_bound, upper_bound)
        color_segment = cv2.bitwise_and(cropped_img, cropped_img, mask=mask)
        gray = cv2.cvtColor(color_segment, cv2.COLOR_BGR2GRAY)

        rightmost_position = 0
        for col in range(gray.shape[1]):
            if np.any(gray[:, col] != 0):
                rightmost_position = col

        area = rightmost_position * height
        is_attack = False
        result = 0
        if area > 0:
            is_attack = True
            p = int((area * 10) / total_area)
            if p <= 9:
                result = 11 - int((area * 10) / total_area)

        return is_attack, result

    def _frame_change(self, previous_image, image):
        if previous_image is None or previous_image.size == 0:
            return 0.0

        previous_gray = cv2.cvtColor(cv2.resize(previous_image, (96, 96)), cv2.COLOR_BGR2GRAY)
        current_gray = cv2.cvtColor(cv2.resize(image, (96, 96)), cv2.COLOR_BGR2GRAY)
        delta = cv2.absdiff(previous_gray, current_gray)
        return float(np.mean(delta) / 255.0)

    def _angle_bucket(self, angle):
        return int(angle // 30)

    def _angular_distance(self, first, second):
        return abs((first - second + 180) % 360 - 180)

    def _action_signature(self, action):
        move_action, angle, info_action, attack_action, action_type, _, _, _ = action
        angle_bucket = self._angle_bucket(angle) if move_action != 0 else 0
        return move_action, angle_bucket, info_action, attack_action, action_type

    def calculate_probe_reward(self, previous_image, image, action):
        move_action, _, info_action, attack_action, _, _, _, _ = action
        active_action_count = int(move_action != 0) + int(info_action != 0) + int(attack_action != 0)
        raw_frame_change = self._frame_change(previous_image, image)
        frame_change_score = min(raw_frame_change / self.frame_change_target, 1.0)

        reward = self.frame_change_weight * frame_change_score if active_action_count > 0 else 0.25 * frame_change_score
        if active_action_count > 0:
            reward += self.interaction_bonus
        else:
            reward -= self.idle_penalty

        action_signature = self._action_signature(action)
        is_novel_action = active_action_count > 0 and action_signature not in self.recent_action_signatures
        if is_novel_action:
            reward += self.novelty_bonus
        self.recent_action_signatures.append(action_signature)

        smooth_move = None
        if move_action != 0:
            reward += self.move_bonus
            current_angle = action[1]
            if self.last_move_angle is not None:
                angle_delta = self._angular_distance(current_angle, self.last_move_angle)
                smooth_move = angle_delta <= 45
                if angle_delta <= 45:
                    reward += self.smooth_move_bonus
                elif angle_delta >= 135:
                    reward -= self.reverse_turn_penalty
            self.last_move_angle = current_angle

        ineffective_action = active_action_count > 0 and frame_change_score < 0.05
        if ineffective_action:
            reward -= self.ineffective_action_penalty

        diagnostics = {
            "frame_change": round(raw_frame_change, 4),
            "frame_change_score": round(frame_change_score, 4),
            "active_action_count": active_action_count,
            "novel_action": is_novel_action,
            "smooth_move": smooth_move,
            "ineffective_action": ineffective_action,
        }
        return reward, diagnostics

    def calculate_gameplay_reward(self, action, diagnostics):
        move_action, _, info_action, attack_action, action_type, _, _, _ = action
        reward = 0.0

        if move_action != 0 and attack_action in [1, 2, 3, 8, 9, 10]:
            reward += 0.30
        elif move_action != 0:
            reward += 0.08

        if attack_action == 1:
            reward += 0.18
        elif attack_action == 2:
            reward += 0.12
        elif attack_action == 3:
            reward += 0.08
        elif attack_action in [8, 9, 10]:
            reward += 0.14
        elif attack_action == 4:
            reward -= 0.15
        elif attack_action == 5:
            reward -= 0.05
        elif attack_action in [6, 7]:
            reward -= 0.02

        if info_action in [6, 7, 8]:
            reward += 0.12
        elif info_action in [1, 2]:
            reward += 0.08
        elif info_action in [3, 4, 5]:
            reward -= 0.05

        if action_type == 2:
            reward -= 0.10

        if diagnostics["active_action_count"] == 0:
            reward -= 0.10

        return reward, {"gameplay_action_score": round(reward, 4)}

    def calculate_traffic_reward(self, traffic_metrics, active_action_count, step_latency_ms):
        traffic_metrics = traffic_metrics or {}
        traffic_available = bool(traffic_metrics.get("available"))
        total_bytes = int(traffic_metrics.get("total_bytes", 0))
        total_packets = int(traffic_metrics.get("total_packets", 0))

        bytes_score = min(total_bytes / max(1, args.traffic_bytes_target), 1.0)
        packets_score = min(total_packets / max(1, args.traffic_packets_target), 1.0)
        activity_score = 0.65 * bytes_score + 0.35 * packets_score

        reward = 0.0
        if traffic_available and active_action_count > 0:
            reward += args.traffic_reward_weight * activity_score
            if total_bytes == 0 and total_packets == 0:
                reward -= args.traffic_inactive_penalty

        latency_score = 0.0
        if step_latency_ms is not None:
            latency_score = max(0.0, 1.0 - (step_latency_ms / max(1.0, args.latency_target_ms)))
            reward += args.latency_reward_weight * latency_score

        diagnostics = {
            "traffic_available": traffic_available,
            "traffic_bytes_score": round(bytes_score, 4),
            "traffic_packets_score": round(packets_score, 4),
            "traffic_activity_score": round(activity_score, 4),
            "latency_score": round(latency_score, 4),
        }
        return reward, diagnostics

    def calculate_target_reward(self, action, target_info):
        if not args.target_tracking_enabled or not target_info or not target_info.get("visible"):
            return 0.0, {
                "target_visible": False,
                "target_angle": None,
                "target_distance_ratio": 0.0,
                "target_reward_score": 0.0,
            }

        move_action, angle, _, attack_action, action_type, arg1, _, _ = action
        target_angle = int(target_info.get("angle", 0))
        distance_ratio = float(target_info.get("distance_ratio", 0.0))
        reward = 0.05

        if move_action != 0:
            angle_delta = self._angular_distance(angle, target_angle)
            if angle_delta <= 35:
                reward += 0.18
            elif angle_delta <= 70:
                reward += 0.08
            elif angle_delta >= 130:
                reward -= 0.10

        if attack_action in [1, 2, 3, 8, 9, 10]:
            reward += 0.10

        if attack_action in [8, 9, 10] and action_type == 1:
            skill_delta = self._angular_distance(arg1, target_angle)
            if skill_delta <= 40:
                reward += 0.10

        if distance_ratio <= args.target_tracking_attack_distance:
            reward += 0.06

        diagnostics = {
            "target_visible": True,
            "target_angle": target_angle,
            "target_distance_ratio": round(distance_ratio, 4),
            "target_reward_score": round(reward, 4),
        }
        return reward, diagnostics

    def calculate_phase_reward(self, action, phase_info):
        phase_info = phase_info or {}
        if not phase_info.get("phase_controller_enabled"):
            return 0.0, {"phase_reward_score": 0.0}

        move_action, angle, _, attack_action, action_type, arg1, _, _ = [int(v) for v in action]
        phase = phase_info.get("phase")
        target_angle = phase_info.get("target_angle")
        reward = 0.0

        if phase in ["trade", "chase"]:
            if attack_action in [1, 8, 9, 10]:
                reward += 0.10
            if move_action != 0 and phase_info.get("phase_target_visible"):
                reward += 0.05
            if attack_action in [8, 9, 10] and action_type == 1:
                reward += 0.04

        elif phase == "laning":
            if attack_action in [1, 2, 8, 9]:
                reward += 0.08
            if move_action != 0:
                reward += 0.03

        elif phase == "push_tower":
            if attack_action == 3:
                reward += 0.12
            elif attack_action in [1, 2]:
                reward += 0.04

        elif phase == "retreat":
            if move_action != 0:
                reward += 0.10
            if attack_action in [4, 5]:
                reward += 0.04
            if attack_action in [3, 8, 9, 10]:
                reward -= 0.08

        elif phase == "recover":
            if attack_action == 5 or (move_action != 0 and attack_action == 0):
                reward += 0.06

        return reward, {"phase_reward_score": round(reward, 4)}

    def calculate_event_reward(self, status_name, attack_reward, action):
        if status_name is None:
            return 0.0

        move_action, _, _, attack_action, _, _, _, _ = action
        if status_name == "attack":
            pass_attack_action = [1, 2, 3, 8, 9, 10]
            if move_action != 0 or attack_action in pass_attack_action:
                return min(float(attack_reward), 10.0) * 0.2
            return -0.1
        if status_name == "backHome":
            return 0.2 if move_action == 0 and attack_action == 0 else -0.1
        if status_name == "death":
            return -2.0
        if status_name == "successes":
            return 5.0
        if status_name == "failed":
            return -5.0
        return 0.0

    def check_finish(self, image):
        res = self.text_sys.detect_and_ocr(image)
        done = 0
        class_name = None
        for boxed_result in res:
            if boxed_result.ocr_text == "\u80dc\u5229" or boxed_result.ocr_text == "VICTORY":
                done = 1
                class_name = 'successes'
                break
            if boxed_result.ocr_text == "\u5931\u8d25" or boxed_result.ocr_text == "DEFEAT":
                done = 1
                class_name = 'failed'
                break
        return done, class_name

    def check_death(self, image):
        check_game_death = self.death_check.get_max_label(image)
        if check_game_death == 'death':
            return check_game_death
        return None

    def get_reword(
            self,
            image_path,
            isFrame,
            action,
            previous_image=None,
            traffic_metrics=None,
            step_latency_ms=None,
            target_info=None,
            phase_info=None,
    ):
        image = image_path if isFrame else cv2.imread(image_path)

        done = 0
        class_name = None
        death_class_name = None
        md_class_name = None
        attack_reward_count = 0

        futures = {
            self.executor.submit(self.predict_attack_delta, previous_image, image): "attack",
        }

        death_interval = max(1, args.death_check_interval_steps)
        if self.step_index % death_interval == 0:
            futures[self.executor.submit(self.check_death, image)] = "death"

        finish_interval = max(1, args.finish_check_interval_steps)
        if self.step_index >= args.finish_check_start_step and self.step_index % finish_interval == 0:
            futures[self.executor.submit(self.check_finish, image)] = "finish"

        for future in as_completed(futures):
            future_type = futures[future]
            if future_type == "finish":
                done, class_name = future.result()
            elif future_type == "death":
                death_class_name = future.result()
            else:
                is_attack, attack_reward_count = future.result()
                if is_attack:
                    md_class_name = "attack"

        if done == 0:
            if death_class_name is not None:
                class_name = death_class_name
            elif md_class_name is not None:
                class_name = md_class_name

        probe_reward, diagnostics = self.calculate_probe_reward(previous_image, image, action)
        gameplay_reward, gameplay_diagnostics = self.calculate_gameplay_reward(action, diagnostics)
        traffic_reward, traffic_diagnostics = self.calculate_traffic_reward(
            traffic_metrics,
            diagnostics["active_action_count"],
            step_latency_ms,
        )
        target_reward, target_diagnostics = self.calculate_target_reward(action, target_info)
        phase_reward, phase_diagnostics = self.calculate_phase_reward(action, phase_info)
        event_reward = self.calculate_event_reward(class_name, attack_reward_count, action)
        total_reward = probe_reward + gameplay_reward + traffic_reward + target_reward + phase_reward + event_reward
        info = {
            "event": class_name,
            "probe_reward": round(probe_reward, 4),
            "gameplay_reward": round(gameplay_reward, 4),
            "traffic_reward": round(traffic_reward, 4),
            "target_reward": round(target_reward, 4),
            "phase_reward": round(phase_reward, 4),
            "event_reward": round(event_reward, 4),
            **diagnostics,
            **gameplay_diagnostics,
            **traffic_diagnostics,
            **target_diagnostics,
            **phase_diagnostics,
        }

        self.step_index += 1
        return total_reward, done, info
