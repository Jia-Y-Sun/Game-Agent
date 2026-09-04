import math

import cv2
import numpy as np

from argparses import args


def _clamp_ratio(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    return max(0.0, min(1.0, value))


def _roi_from_ratios(image, left, top, right, bottom):
    height, width = image.shape[:2]
    left = int(width * _clamp_ratio(left, 0.0))
    right = int(width * _clamp_ratio(right, 1.0))
    top = int(height * _clamp_ratio(top, 0.0))
    bottom = int(height * _clamp_ratio(bottom, 1.0))
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width, right))
    top = max(0, min(height - 1, top))
    bottom = max(top + 1, min(height, bottom))
    return image[top:bottom, left:right], left, top, right, bottom


class MinimapVision:
    """Small-map cues used to stop route walking and guide conservative laning."""

    def __init__(self):
        self.last_state = self._empty_state()
        self.frame_index = 0
        self.predicted_self = None
        self.self_missing_steps = 0

    def reset_episode(self):
        self.last_state = self._empty_state()
        self.frame_index = 0
        self.predicted_self = None
        self.self_missing_steps = 0

    def detect(self, image):
        if not args.minimap_vision_enabled or image is None or getattr(image, "size", 0) == 0:
            self.last_state = self._empty_state()
            return self.last_state

        state = self._empty_state()
        state["enabled"] = True
        state.update(self._detect_dark_screen(image))
        state.update(self._detect_minimap(image))
        state.update(self._detect_attack_avatar(image))
        self.last_state = state

        if args.minimap_debug:
            print("minimap", state)
        return state

    def _detect_minimap(self, image):
        roi, left, top, right, bottom = _roi_from_ratios(
            image,
            args.minimap_roi_left,
            args.minimap_roi_top,
            args.minimap_roi_right,
            args.minimap_roi_bottom,
        )
        roi_height, roi_width = roi.shape[:2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        tower_candidate = self._choose_tower_candidate(hsv, roi_width, roi_height)
        target_ratio = (
            _clamp_ratio(args.minimap_lane_tower_x, 0.96),
            _clamp_ratio(args.minimap_lane_tower_y, 0.86),
        )
        if args.minimap_lane_tower_auto_enabled and tower_candidate is not None:
            target_ratio = tower_candidate["ratio"]
        detected_self = self._choose_self_candidate(hsv, roi_width, roi_height, target_ratio)
        self_candidate, self_source = self._update_self_tracker(detected_self, target_ratio)
        enemy_candidates = self._choose_enemy_candidates(hsv, roi_width, roi_height)

        result = {
            "minimap_available": True,
            "minimap_roi": (left, top, right, bottom),
            "minimap_self_visible": self_candidate is not None,
            "minimap_self_predicted": self_source == "predicted",
            "minimap_self_source": self_source,
            "minimap_self": None,
            "minimap_lane_tower_visible": tower_candidate is not None,
            "minimap_lane_tower": (
                round(float(target_ratio[0]), 4),
                round(float(target_ratio[1]), 4),
            ),
            "minimap_lane_distance": None,
            "minimap_lane_arrived": False,
            "minimap_lane_move_angle": None,
            "minimap_enemy_visible": bool(enemy_candidates),
            "minimap_enemy_count": int(len(enemy_candidates)),
            "minimap_enemy": None,
            "minimap_enemy_distance": None,
            "minimap_enemy_seek_angle": None,
            "minimap_self_lane_min_y": round(float(args.minimap_self_lane_min_y), 4),
            "minimap_self_max_x": round(float(args.minimap_self_max_x), 4),
            "minimap_lane_arrival_distance_threshold": round(float(args.minimap_lane_arrival_distance), 4),
        }

        if self_candidate is not None:
            self_ratio = self_candidate["ratio"]
            result["minimap_self"] = (
                round(float(self_ratio[0]), 4),
                round(float(self_ratio[1]), 4),
            )
            dx = target_ratio[0] - self_ratio[0]
            dy = target_ratio[1] - self_ratio[1]
            distance = math.hypot(dx, dy)
            result["minimap_lane_distance"] = round(float(distance), 4)
            result["minimap_lane_move_angle"] = self._lane_joystick_angle(dx, dy)
            result["minimap_lane_arrived"] = bool(
                args.minimap_lane_arrival_enabled
                and distance <= float(args.minimap_lane_arrival_distance)
            )

            nearest_enemy = self._nearest_enemy(self_ratio, enemy_candidates)
            if nearest_enemy is not None:
                enemy_ratio = nearest_enemy["ratio"]
                edx = enemy_ratio[0] - self_ratio[0]
                edy = enemy_ratio[1] - self_ratio[1]
                enemy_distance = math.hypot(edx, edy)
                result["minimap_enemy"] = (
                    round(float(enemy_ratio[0]), 4),
                    round(float(enemy_ratio[1]), 4),
                )
                result["minimap_enemy_distance"] = round(float(enemy_distance), 4)
                if (
                    args.minimap_enemy_seek_enabled
                    and float(args.minimap_enemy_seek_min_distance) <= enemy_distance <= float(args.minimap_enemy_seek_max_distance)
                ):
                    result["minimap_enemy_seek_angle"] = int(round(math.degrees(math.atan2(edy, edx)))) % 360

        return result

    @staticmethod
    def _lane_joystick_angle(dx, dy):
        weighted_dy = float(dy) * float(args.minimap_lane_move_y_weight)
        if abs(float(dx)) < 1e-6 and abs(weighted_dy) < 1e-6:
            return 0
        return int(round(math.degrees(math.atan2(weighted_dy, float(dx))))) % 360

    def _detect_attack_avatar(self, image):
        if not args.minimap_attack_avatar_enabled:
            return {
                "attack_avatar_visible": False,
                "attack_avatar_center": None,
                "attack_avatar_area": 0.0,
            }

        roi, left, top, _, _ = _roi_from_ratios(
            image,
            args.minimap_attack_roi_left,
            args.minimap_attack_roi_top,
            args.minimap_attack_roi_right,
            args.minimap_attack_roi_bottom,
        )
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        red_mask = self._red_mask(hsv, saturation=70, value=60)
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best = None
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < int(args.minimap_attack_avatar_min_area):
                continue
            x, y, width, height = cv2.boundingRect(contour)
            if width <= 0 or height <= 0:
                continue
            aspect = width / float(height)
            if aspect < 0.72 or aspect > 1.38:
                continue
            score = area - abs(width - height) * 8.0
            if best is None or score > best[0]:
                best = (score, area, x, y, width, height)

        if best is None:
            return {
                "attack_avatar_visible": False,
                "attack_avatar_center": None,
                "attack_avatar_area": 0.0,
            }

        _, area, x, y, width, height = best
        center = (left + x + width / 2.0, top + y + height / 2.0)
        return {
            "attack_avatar_visible": True,
            "attack_avatar_center": (round(center[0], 1), round(center[1], 1)),
            "attack_avatar_area": round(float(area), 1),
        }

    def _detect_dark_screen(self, image):
        if not args.minimap_dark_death_enabled:
            return {
                "dead_screen_dark": False,
                "dark_mean_v": 0.0,
                "dark_fraction": 0.0,
            }

        roi, _, _, _, _ = _roi_from_ratios(image, 0.12, 0.12, 0.88, 0.82)
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        value = hsv[:, :, 2]
        mean_v = float(np.mean(value))
        dark_fraction = float(np.mean(value < 42))
        dead = bool(
            mean_v <= float(args.minimap_dark_death_mean_v_threshold)
            and dark_fraction >= float(args.minimap_dark_death_fraction_threshold)
        )
        return {
            "dead_screen_dark": dead,
            "dark_mean_v": round(mean_v, 2),
            "dark_fraction": round(dark_fraction, 4),
        }

    def _choose_self_candidate(self, hsv, roi_width, roi_height, target_ratio):
        green_mask = cv2.inRange(hsv, np.array([36, 45, 50]), np.array([100, 255, 255]))
        green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates = []
        previous_self = (self.last_state or {}).get("minimap_self")
        predicted_self = self.predicted_self
        for contour in contours:
            candidate = self._circular_candidate(
                contour,
                roi_width,
                roi_height,
                min_area=float(args.minimap_self_min_area),
                max_area=8000,
            )
            if candidate is None:
                continue
            x_ratio, y_ratio = candidate["ratio"]
            if not self._is_lane_self_candidate(candidate):
                continue
            if previous_self:
                try:
                    previous_x = float(previous_self[0])
                    previous_y = float(previous_self[1])
                    previous_distance = math.hypot(x_ratio - previous_x, y_ratio - previous_y)
                    escaping_base_icon = (
                        previous_x < float(args.minimap_self_prefer_min_x)
                        and x_ratio >= float(args.minimap_self_prefer_min_x)
                        and y_ratio >= float(args.minimap_self_prefer_min_y)
                    )
                    if not escaping_base_icon and previous_distance > float(args.minimap_self_max_step_delta):
                        continue
                except (TypeError, ValueError):
                    previous_distance = None
            else:
                previous_distance = None
            aspect = float(candidate.get("aspect", 1.0))
            fill_ratio = float(candidate.get("fill_ratio", 0.5))
            edge_margin = min(x_ratio, 1.0 - x_ratio, y_ratio, 1.0 - y_ratio)

            # The self marker is a circular hero rim. Large solid tower/base
            # icons and clipped edge marks are deliberately down-weighted.
            score = 0.0
            score += 420.0 * max(0.0, 1.0 - abs(aspect - 1.0))
            score += 260.0 * max(0.0, 1.0 - abs(fill_ratio - 0.48) * 2.4)
            score += min(float(candidate["area"]), 650.0) * 0.16
            score += min(edge_margin, 0.24) * 500.0
            if y_ratio >= float(args.minimap_self_prefer_min_y):
                score += x_ratio * 260.0
            if x_ratio < float(args.minimap_self_prefer_min_x) and y_ratio >= float(args.minimap_self_prefer_min_y):
                score -= 260.0
            if float(candidate["area"]) > 950.0:
                score -= (float(candidate["area"]) - 950.0) * 0.35
            if x_ratio < 0.04 or x_ratio > 0.98 or y_ratio < 0.04 or y_ratio > 0.98:
                score -= 380.0

            if previous_distance is not None:
                try:
                    score += max(0.0, 1.0 - previous_distance / 0.20) * 420.0
                except (TypeError, ValueError):
                    pass
            if predicted_self is not None:
                predicted_distance = math.hypot(x_ratio - predicted_self[0], y_ratio - predicted_self[1])
                score += max(0.0, 1.0 - predicted_distance / 0.22) * 520.0
            tower_distance = math.hypot(x_ratio - target_ratio[0], y_ratio - target_ratio[1])
            score += max(0.0, 1.0 - tower_distance / 0.75) * 120.0
            candidates.append((score, candidate))

        if not candidates:
            return None
        preferred = [
            item
            for item in candidates
            if item[1]["ratio"][0] >= float(args.minimap_self_prefer_min_x)
            and item[1]["ratio"][1] >= float(args.minimap_self_prefer_min_y)
        ]
        if preferred:
            candidates = preferred
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    @staticmethod
    def _is_lane_self_candidate(candidate):
        if candidate is None:
            return False
        x_ratio, y_ratio = candidate["ratio"]
        return bool(
            y_ratio >= float(args.minimap_self_lane_min_y)
            and x_ratio <= float(args.minimap_self_max_x)
        )

    def _is_base_icon_candidate(self, candidate):
        if candidate is None:
            return False
        x_ratio, y_ratio = candidate["ratio"]
        return bool(
            x_ratio <= float(args.minimap_self_base_icon_max_x)
            and y_ratio >= float(args.minimap_self_base_icon_min_y)
        )

    def _update_self_tracker(self, detected_candidate, target_ratio):
        if detected_candidate is not None and not self._is_lane_self_candidate(detected_candidate):
            detected_candidate = None

        if detected_candidate is not None:
            if (
                self.frame_index >= max(0, int(args.minimap_self_base_ignore_after_steps))
                and self._is_base_icon_candidate(detected_candidate)
                and self.predicted_self is not None
            ):
                self.frame_index += 1
                return self._predict_self(target_ratio)

            self.predicted_self = tuple(float(value) for value in detected_candidate["ratio"])
            self.self_missing_steps = 0
            self.frame_index += 1
            return detected_candidate, "detected"

        self.frame_index += 1
        return self._predict_self(target_ratio)

    def _predict_self(self, target_ratio):
        if not bool(args.minimap_self_prediction_enabled):
            self.self_missing_steps = 0
            return None, "none"
        if self.predicted_self is None:
            return None, "none"
        self.self_missing_steps += 1
        if self.self_missing_steps > max(1, int(args.minimap_self_predict_max_missing_steps)):
            self.predicted_self = None
            return None, "none"

        current_x, current_y = self.predicted_self
        if current_y < float(args.minimap_self_lane_min_y) or current_x > float(args.minimap_self_max_x):
            self.predicted_self = None
            return None, "none"
        dx = float(target_ratio[0]) - current_x
        dy = float(target_ratio[1]) - current_y
        distance = math.hypot(dx, dy)
        step = max(0.0, float(args.minimap_self_predict_step_distance))
        if distance > 1e-6 and step > 0.0:
            advance = min(step, distance)
            current_x += dx / distance * advance
            current_y += dy / distance * advance
        self.predicted_self = (
            max(0.0, min(1.0, current_x)),
            max(0.0, min(1.0, current_y)),
        )
        return {
            "ratio": self.predicted_self,
            "area": 0.0,
            "box": None,
            "aspect": 1.0,
            "fill_ratio": 0.0,
            "predicted": True,
        }, "predicted"

    def _choose_enemy_candidates(self, hsv, roi_width, roi_height):
        red_mask = self._red_mask(hsv, saturation=70, value=60)
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates = []
        for contour in contours:
            candidate = self._circular_candidate(contour, roi_width, roi_height, min_area=70, max_area=9000)
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(key=lambda item: item["area"], reverse=True)
        return candidates[:6]

    def _choose_tower_candidate(self, hsv, roi_width, roi_height):
        blue_mask = cv2.inRange(hsv, np.array([82, 50, 55]), np.array([135, 255, 255]))
        blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(blue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        target_ratio = (
            _clamp_ratio(args.minimap_lane_tower_x, 0.96),
            _clamp_ratio(args.minimap_lane_tower_y, 0.86),
        )
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 25 or area > 2500:
                continue
            x, y, width, height = cv2.boundingRect(contour)
            if width < 4 or height < 8 or width > 50 or height > 85:
                continue
            ratio = ((x + width / 2.0) / max(1.0, roi_width), (y + height / 2.0) / max(1.0, roi_height))
            if ratio[0] < float(args.minimap_lane_tower_min_x) or ratio[1] < float(args.minimap_lane_tower_min_y):
                continue
            distance = math.hypot(ratio[0] - target_ratio[0], ratio[1] - target_ratio[1])
            candidates.append((distance, area, {"ratio": ratio, "area": float(area), "box": (x, y, width, height)}))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], -item[1]))
        return candidates[0][2]

    @staticmethod
    def _nearest_enemy(self_ratio, enemy_candidates):
        if not enemy_candidates:
            return None
        scored = []
        for candidate in enemy_candidates:
            ratio = candidate["ratio"]
            distance = math.hypot(ratio[0] - self_ratio[0], ratio[1] - self_ratio[1])
            scored.append((distance, candidate))
        scored.sort(key=lambda item: item[0])
        return scored[0][1]

    @staticmethod
    def _red_mask(hsv, saturation=70, value=60):
        red_1 = cv2.inRange(hsv, np.array([0, saturation, value]), np.array([12, 255, 255]))
        red_2 = cv2.inRange(hsv, np.array([168, saturation, value]), np.array([179, 255, 255]))
        return cv2.bitwise_or(red_1, red_2)

    @staticmethod
    def _circular_candidate(contour, roi_width, roi_height, min_area, max_area):
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            return None
        x, y, width, height = cv2.boundingRect(contour)
        if width <= 0 or height <= 0:
            return None
        if width < 10 or height < 10 or width > 95 or height > 95:
            return None
        aspect = width / float(height)
        if aspect < 0.55 or aspect > 1.80:
            return None
        fill_ratio = area / float(max(1, width * height))
        if fill_ratio < 0.04 or fill_ratio > 0.88:
            return None
        cx = x + width / 2.0
        cy = y + height / 2.0
        return {
            "ratio": (cx / max(1.0, roi_width), cy / max(1.0, roi_height)),
            "area": float(area),
            "box": (int(x), int(y), int(width), int(height)),
            "aspect": float(aspect),
            "fill_ratio": float(fill_ratio),
        }

    @staticmethod
    def _empty_state():
        return {
            "enabled": False,
            "minimap_available": False,
            "minimap_roi": None,
            "minimap_self_visible": False,
            "minimap_self_predicted": False,
            "minimap_self_source": "none",
            "minimap_self": None,
            "minimap_lane_tower_visible": False,
            "minimap_lane_tower": None,
            "minimap_lane_distance": None,
            "minimap_lane_arrived": False,
            "minimap_lane_move_angle": None,
            "minimap_enemy_visible": False,
            "minimap_enemy_count": 0,
            "minimap_enemy": None,
            "minimap_enemy_distance": None,
            "minimap_enemy_seek_angle": None,
            "attack_avatar_visible": False,
            "attack_avatar_center": None,
            "attack_avatar_area": 0.0,
            "dead_screen_dark": False,
            "dark_mean_v": 0.0,
            "dark_fraction": 0.0,
        }
