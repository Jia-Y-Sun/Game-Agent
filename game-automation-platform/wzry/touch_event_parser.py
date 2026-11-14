import math
import re
from collections import defaultdict

from argparses import (
    args,
    attack_actions_detail,
    info_actions_detail,
    move_actions_detail,
)


GETEVENT_RE = re.compile(
    r"^\[\s*(?P<event_ts>[0-9]+\.[0-9]+)\]\s+"
    r"(?:(?P<device>/dev/input/event\d+):\s+)?"
    r"(?P<event_type>\S+)\s+(?P<code>\S+)\s+(?P<value>[0-9a-fA-F]+)"
)


def parse_getevent_line(line):
    match = GETEVENT_RE.match(line.strip())
    if not match:
        return None

    raw_value = match.group("value")
    value = int(raw_value, 16)
    if value >= 0x80000000:
        value -= 0x100000000

    return {
        "event_ts": float(match.group("event_ts")),
        "device": match.group("device"),
        "event_type": match.group("event_type"),
        "code": match.group("code"),
        "value": value,
        "raw_value": raw_value,
    }


class MultiTouchState:
    """Incrementally turns adb getevent lines into touch samples."""

    def __init__(self):
        self.current_slot = 0
        self.slots = defaultdict(self._new_slot)
        self.updated_slots = set()

    @staticmethod
    def _new_slot():
        return {
            "tracking_id": None,
            "active": False,
            "x": None,
            "y": None,
            "phase": "idle",
        }

    def process_line(self, line, host_ts):
        event = parse_getevent_line(line)
        if event is None:
            return []

        event_type = event["event_type"]
        code = event["code"]
        value = event["value"]

        if event_type == "EV_ABS":
            if code == "ABS_MT_SLOT":
                self.current_slot = value
                self.slots[self.current_slot]
                return []

            slot = self.slots[self.current_slot]
            if code == "ABS_MT_TRACKING_ID":
                if value < 0:
                    slot["active"] = False
                    slot["phase"] = "up"
                else:
                    slot["tracking_id"] = value
                    slot["active"] = True
                    slot["phase"] = "down"
                self.updated_slots.add(self.current_slot)
            elif code in ("ABS_MT_POSITION_X", "ABS_X"):
                slot["x"] = value
                if slot["phase"] == "idle":
                    slot["phase"] = "move"
                self.updated_slots.add(self.current_slot)
            elif code in ("ABS_MT_POSITION_Y", "ABS_Y"):
                slot["y"] = value
                if slot["phase"] == "idle":
                    slot["phase"] = "move"
                self.updated_slots.add(self.current_slot)
            return []

        if event_type == "EV_SYN" and code == "SYN_REPORT":
            samples = []
            for slot_id in sorted(self.updated_slots):
                slot = self.slots[slot_id]
                if slot["x"] is None or slot["y"] is None:
                    continue

                samples.append({
                    "host_ts": host_ts,
                    "event_ts": event["event_ts"],
                    "slot": int(slot_id),
                    "tracking_id": slot["tracking_id"],
                    "raw_x": int(slot["x"]),
                    "raw_y": int(slot["y"]),
                    "phase": slot["phase"],
                    "active": bool(slot["active"]),
                })

                if slot["phase"] == "up":
                    slot["tracking_id"] = None
                slot["phase"] = "move" if slot["active"] else "idle"

            self.updated_slots.clear()
            return samples

        return []


class TouchActionMapper:
    """Maps touch samples to the project's 8-branch action format."""

    def __init__(self, screen_width, screen_height):
        self.screen_width = int(screen_width)
        self.screen_height = int(screen_height)
        self.default_hit_radius = min(self.screen_width, self.screen_height) * args.touch_hit_radius_ratio
        self.move_deadzone = min(self.screen_width, self.screen_height) * args.touch_move_deadzone_ratio

    def samples_to_action(self, samples):
        transformed = [self._transform_sample(sample) for sample in samples if self._valid_sample(sample)]
        transformed.sort(key=lambda item: item["host_ts"])

        action = [0, 0, 0, 0, 0, 0, 0, 0]
        diagnostics = {
            "touch_sample_count": len(transformed),
            "touch_track_count": 0,
            "matched_move": False,
            "matched_info_action": 0,
            "matched_attack_action": 0,
        }

        if not transformed:
            return action, diagnostics

        tracks = self._group_tracks(transformed)
        diagnostics["touch_track_count"] = len(tracks)

        move_action, angle = self._map_move(transformed)
        action[0] = move_action
        action[1] = angle
        diagnostics["matched_move"] = bool(move_action)

        info_action = self._map_info_action(tracks)
        attack_action, action_type, arg1, arg2, arg3 = self._map_attack_action(tracks)

        action[2] = info_action
        action[3] = attack_action
        action[4] = action_type
        action[5] = arg1
        action[6] = arg2
        action[7] = arg3
        diagnostics["matched_info_action"] = info_action
        diagnostics["matched_attack_action"] = attack_action
        return [int(value) for value in action], diagnostics

    def _valid_sample(self, sample):
        return sample.get("raw_x") is not None and sample.get("raw_y") is not None

    def _transform_sample(self, sample):
        raw_x = float(sample["raw_x"])
        raw_y = float(sample["raw_y"])
        raw_width = float(args.touch_raw_width or self.screen_width)
        raw_height = float(args.touch_raw_height or self.screen_height)

        if args.touch_swap_xy:
            raw_x, raw_y = raw_y, raw_x
            raw_width, raw_height = raw_height, raw_width

        x = raw_x / max(1.0, raw_width) * self.screen_width
        y = raw_y / max(1.0, raw_height) * self.screen_height

        if args.touch_invert_x:
            x = self.screen_width - x
        if args.touch_invert_y:
            y = self.screen_height - y

        item = dict(sample)
        item["x"] = max(0.0, min(float(self.screen_width), x))
        item["y"] = max(0.0, min(float(self.screen_height), y))
        return item

    def _group_tracks(self, samples):
        tracks = defaultdict(list)
        for sample in samples:
            tracking_id = sample.get("tracking_id")
            if tracking_id is None:
                tracking_id = sample.get("slot", 0)
            tracks[(sample.get("slot", 0), tracking_id)].append(sample)
        return tracks

    def _map_move(self, samples):
        center = self._action_center(move_actions_detail[1])
        radius = float(move_actions_detail[1].get("radius", 0))
        move_zone_radius = max(radius * 1.4, self.default_hit_radius * 1.8)
        move_samples = [
            sample for sample in samples
            if self._distance((sample["x"], sample["y"]), center) <= move_zone_radius
        ]
        if not move_samples:
            return 0, 0

        latest = move_samples[-1]
        dx = latest["x"] - center[0]
        dy = latest["y"] - center[1]
        distance = math.hypot(dx, dy)
        if distance < self.move_deadzone:
            return 0, 0
        return 1, self._angle(dx, dy)

    def _map_info_action(self, tracks):
        candidates = []
        for points in tracks.values():
            for action_index, detail in info_actions_detail.items():
                hit_ts = self._latest_hit_ts(points, detail)
                if hit_ts is not None:
                    candidates.append((hit_ts, int(action_index)))

        if not candidates:
            return 0
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    def _map_attack_action(self, tracks):
        candidates = []
        for points in tracks.values():
            for action_index, detail in attack_actions_detail.items():
                hit_index = self._first_hit_index(points, detail)
                if hit_index is None:
                    continue

                action_type, arg1, arg2, arg3 = self._attack_args(int(action_index), points, hit_index)
                candidates.append((points[-1]["host_ts"], int(action_index), action_type, arg1, arg2, arg3))

        if not candidates:
            return 0, 0, 0, 0, 0
        candidates.sort(key=lambda item: item[0])
        _, action_index, action_type, arg1, arg2, arg3 = candidates[-1]
        return action_index, action_type, arg1, arg2, arg3

    def _attack_args(self, action_index, points, hit_index):
        if action_index < 8:
            return 0, 0, 0, 0

        start = points[hit_index]
        end = points[-1]
        dx = end["x"] - start["x"]
        dy = end["y"] - start["y"]
        distance = math.hypot(dx, dy)
        drag_threshold = max(35.0, self.default_hit_radius * 0.7)
        duration_ms = max(0.0, (end["host_ts"] - start["host_ts"]) * 1000.0)

        if distance >= drag_threshold:
            return 1, self._angle(dx, dy), int(max(0, min(99, round(distance)))), 0

        if duration_ms >= args.long_press_unit_ms * 2:
            units = max(1, int(round(duration_ms / max(1, args.long_press_unit_ms))))
            return 2, 0, 0, int(max(0, min(4, units - 1)))

        return 0, 0, 0, 0

    def _latest_hit_ts(self, points, detail):
        hit_times = [point["host_ts"] for point in points if self._hit(point, detail)]
        if not hit_times:
            return None
        return max(hit_times)

    def _first_hit_index(self, points, detail):
        for index, point in enumerate(points):
            if self._hit(point, detail):
                return index
        return None

    def _hit(self, point, detail):
        center = self._action_center(detail)
        radius = max(float(detail.get("radius", 0)), self.default_hit_radius)
        return self._distance((point["x"], point["y"]), center) <= radius

    def _action_center(self, detail):
        x_ratio, y_ratio = detail["position"]
        return self.screen_width * float(x_ratio), self.screen_height * float(y_ratio)

    @staticmethod
    def _distance(first, second):
        return math.hypot(first[0] - second[0], first[1] - second[1])

    @staticmethod
    def _angle(dx, dy):
        return int(round(math.degrees(math.atan2(dy, dx)))) % 360
