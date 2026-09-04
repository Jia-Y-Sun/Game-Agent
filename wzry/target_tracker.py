import math

import cv2
import numpy as np

from argparses import args


class TargetTracker:
    """Lightweight enemy-target tracker based on red health-bar-like regions."""

    def __init__(self):
        self.last_center = None
        self.last_target = None

    def reset_episode(self):
        self.last_center = None
        self.last_target = None

    def detect(self, image):
        if not args.target_tracking_enabled or image is None or image.size == 0:
            self.last_target = self._empty_target()
            return self.last_target

        height, width = image.shape[:2]
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

        red_mask_1 = cv2.inRange(hsv, np.array([0, 90, 90]), np.array([12, 255, 255]))
        red_mask_2 = cv2.inRange(hsv, np.array([168, 90, 90]), np.array([179, 255, 255]))
        mask = cv2.bitwise_or(red_mask_1, red_mask_2)

        # Ignore fixed UI regions where red icons/text can produce false targets.
        mask[:int(height * 0.08), :] = 0
        mask[int(height * 0.76):, :] = 0
        mask[:, :int(width * 0.12)] = 0
        mask[:, int(width * 0.92):] = 0

        kernel = np.ones((3, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        target = self._choose_target(contours, width, height)
        self.last_target = target
        if args.target_tracking_debug and target["visible"]:
            print(
                "target",
                target["center"],
                "angle",
                target["angle"],
                "dist",
                round(target["distance_ratio"], 3),
                "area",
                round(target["area"], 1),
            )
        return target

    def refine_action(self, action, target):
        if not args.target_tracking_enabled or not target.get("visible"):
            return action

        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = [int(v) for v in action]
        target_angle = int(target["angle"])
        distance_ratio = float(target["distance_ratio"])

        if move_action != 0:
            angle = target_angle
        elif distance_ratio > args.target_tracking_attack_distance:
            move_action = 1
            angle = target_angle

        if attack_action == 0 and distance_ratio <= args.target_tracking_attack_distance:
            attack_action = 1

        if attack_action in [8, 9, 10] and distance_ratio <= args.target_tracking_skill_distance:
            action_type = 1
            arg1 = target_angle
            arg2 = int(max(20, min(99, distance_ratio * 140)))
            arg3 = 0

        return [move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3]

    def _choose_target(self, contours, width, height):
        center_point = (
            width * args.target_tracking_center_x,
            height * args.target_tracking_center_y,
        )
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < args.target_tracking_min_area or area > args.target_tracking_max_area:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            if h <= 0 or w <= 0:
                continue

            aspect = w / float(h)
            if aspect < 1.4 or aspect > 18.0:
                continue

            cx = x + w / 2.0
            cy = y + h / 2.0
            distance = math.hypot(cx - center_point[0], cy - center_point[1])
            vertical_bias = abs((cy / height) - 0.42)
            score = distance + vertical_bias * width * 0.25 - min(area, 1200) * 0.02
            candidates.append((score, area, (cx, cy), (x, y, w, h)))

        if not candidates:
            self.last_center = None
            return self._empty_target()

        candidates.sort(key=lambda item: item[0])
        _, area, center, box = candidates[0]
        smoothed_center = self._smooth_center(center)
        dx = smoothed_center[0] - center_point[0]
        dy = smoothed_center[1] - center_point[1]
        distance_px = math.hypot(dx, dy)
        distance_ratio = distance_px / max(1.0, math.hypot(width, height))
        angle = int(round(math.degrees(math.atan2(dy, dx)))) % 360

        return {
            "visible": True,
            "center": (round(smoothed_center[0], 1), round(smoothed_center[1], 1)),
            "box": tuple(int(v) for v in box),
            "area": float(area),
            "angle": angle,
            "distance_ratio": distance_ratio,
        }

    def _smooth_center(self, center):
        if self.last_center is None:
            self.last_center = center
            return center

        factor = max(0.0, min(1.0, args.target_tracking_smooth_factor))
        smoothed = (
            self.last_center[0] * factor + center[0] * (1.0 - factor),
            self.last_center[1] * factor + center[1] * (1.0 - factor),
        )
        self.last_center = smoothed
        return smoothed

    @staticmethod
    def _empty_target():
        return {
            "visible": False,
            "center": None,
            "box": None,
            "area": 0.0,
            "angle": 0,
            "distance_ratio": 0.0,
        }
