import datetime
import math
import os
import random
import subprocess
import sys
import threading
import time
import tempfile
from queue import Queue, Empty
import concurrent.futures

import cv2
import numpy as np
import win32gui
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

from argparses import move_actions_detail, info_actions_detail, attack_actions_detail, args


class AndroidTool:
    def __init__(self, scrcpy_dir="scrcpy-win64-v2.0"):
        self.scrcpy_dir = scrcpy_dir
        self.device_serial = args.iphone_id
        self.actual_height, self.actual_width = self.get_device_resolution()
        if args.move_debug:
            print("device_control_resolution", {"width": self.actual_width, "height": self.actual_height})
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self._show_action_log = False
        self._move_lock = threading.Lock()
        self._move_process = None
        self._move_angle = None
        self._move_until = 0.0
        self._move_started_at = 0.0
        self._motion_contact_active = False
        self._motion_contact_point = None
        self._motionevent_failed = False
        self._minitouch_device = None
        self._minitouch_contact_active = False
        self._minitouch_failed = False
        self._qt_app = None
        self._assist_last_basic_attack_at = 0.0
        self._assist_last_skill_at = 0.0
        self._assist_last_recover_at = 0.0
        self._assist_last_buy_at = 0.0
        self._assist_skill_index = 0
        self._assist_buy_index = 0

    def show_action_log(self):
        self._show_action_log = True

    def hidden_action_log(self):
        self._show_action_log = False

    def get_device_resolution(self):
        adb_path = f"{self.scrcpy_dir}/adb"
        command = [adb_path, "-s", self.device_serial, "shell", "wm", "size"]
        last_error = None

        for attempt in range(3):
            try:
                output = subprocess.check_output(command, stderr=subprocess.STDOUT, timeout=8).decode('utf-8')
                raw_width, raw_height = [int(value) for value in output.split()[-1].split('x')]
                # All control coordinates in this project are landscape ratios.
                # Some devices report wm size in portrait order, others in the
                # current landscape order, so normalize before applying ratios.
                width = max(raw_width, raw_height)
                height = min(raw_width, raw_height)
                return height, width
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                last_error = exc
                time.sleep(0.5 + attempt * 0.5)

        devices_output = ""
        try:
            devices_output = subprocess.check_output(
                [adb_path, "devices", "-l"],
                stderr=subprocess.STDOUT,
                timeout=8,
            ).decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            devices_output = f"adb devices failed: {exc}"

        print("读取设备分辨率失败。")
        print(f"目标设备: {self.device_serial}")
        print("当前 adb devices -l:")
        print(devices_output.strip())
        print("请确认目标设备状态是 device，不是 offline/unauthorized，并重试。")
        raise RuntimeError(f"Failed to read device resolution via adb wm size: {last_error}")

    def execute_move(self, task_params):
        # 移动逻辑
        action_index = task_params['action']
        if action_index == 1:
            actions_detail = move_actions_detail[action_index]
            if self._show_action_log:
                print(actions_detail['action_name'])
            start_x, start_y = self.calculate_startpoint(self._move_center_position(actions_detail))

            radius = int(task_params.get("radius", args.move_joystick_radius or actions_detail["radius"]))
            end_x, end_y = self.calculate_endpoint((start_x, start_y),
                                                   radius,
                                                   task_params['angle'])
            duration_ms = int(task_params.get("duration_ms", args.move_swipe_duration_ms))

            subprocess.run([f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell",
                            "input", "swipe", str(start_x), str(start_y), str(end_x), str(end_y),
                            str(max(1, duration_ms))])
            return True
        return False

    def _angular_distance(self, first, second):
        return abs((int(first) - int(second) + 180) % 360 - 180)

    def _cleanup_finished_move_locked(self):
        if self._move_process is not None and self._move_process.poll() is not None:
            self._move_process = None
            self._move_angle = None
            self._move_until = 0.0
            self._move_started_at = 0.0

    def stop_persistent_move(self):
        with self._move_lock:
            if self._move_process is not None and self._move_process.poll() is None:
                try:
                    self._move_process.terminate()
                    self._move_process.wait(timeout=0.3)
                except (OSError, subprocess.SubprocessError):
                    try:
                        self._move_process.kill()
                    except OSError:
                        pass
            self._move_process = None
            if self._motion_contact_active:
                x, y = self._motion_contact_point or (0, 0)
                self._send_motionevent("UP", x, y)
            if self._minitouch_contact_active:
                self._send_minitouch("u 1\nc\n")
            self._motion_contact_active = False
            self._motion_contact_point = None
            self._minitouch_contact_active = False
            self._move_angle = None
            self._move_until = 0.0
            self._move_started_at = 0.0

    def _send_motionevent(self, event_name, x, y):
        try:
            result = subprocess.run(
                [
                    f"{self.scrcpy_dir}/adb",
                    "-s",
                    self.device_serial,
                    "shell",
                    "input",
                    "motionevent",
                    event_name,
                    str(int(x)),
                    str(int(y)),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _get_minitouch_device(self):
        if self._minitouch_device is not None:
            return self._minitouch_device
        try:
            from pyminitouch import MNTDevice

            self._minitouch_device = MNTDevice(self.device_serial)
            return self._minitouch_device
        except Exception as exc:
            if not self._minitouch_failed:
                print(f"pyminitouch init failed: {exc}")
            self._minitouch_failed = True
            return None

    def _send_minitouch(self, command):
        device = self._get_minitouch_device()
        if device is None:
            return False
        try:
            device.connection.send(command)
            return True
        except Exception as exc:
            if not self._minitouch_failed:
                print(f"pyminitouch send failed: {exc}")
            self._minitouch_failed = True
            return False

    def _map_display_to_minitouch(self, x, y):
        device = self._get_minitouch_device()
        connection = getattr(device, "connection", None) if device is not None else None
        max_x = int(getattr(connection, "max_x", self.actual_width) or self.actual_width)
        max_y = int(getattr(connection, "max_y", self.actual_height) or self.actual_height)
        mapped_x = int(max(0, min(max_x, int(x) * max_x / max(1, self.actual_width))))
        mapped_y = int(max(0, min(max_y, int(y) * max_y / max(1, self.actual_height))))
        return mapped_x, mapped_y

    def _stop_adb_swipe_process_locked(self):
        if self._move_process is not None and self._move_process.poll() is None:
            try:
                self._move_process.terminate()
                self._move_process.wait(timeout=0.15)
            except (OSError, subprocess.SubprocessError):
                try:
                    self._move_process.kill()
                except OSError:
                    pass
        self._move_process = None

    def _move_points(self, action_index, angle, radius=None):
        actions_detail = move_actions_detail[action_index]
        if self._show_action_log:
            print(actions_detail["action_name"])
        start_x, start_y = self.calculate_startpoint(self._move_center_position(actions_detail))
        move_radius = int(radius if radius is not None else (args.move_joystick_radius or actions_detail["radius"]))
        end_x, end_y = self.calculate_endpoint((start_x, start_y), move_radius, int(angle) % 360)
        return start_x, start_y, end_x, end_y, move_radius

    def execute_move_center_swipe(self, task_params, deadline=None):
        action_index = task_params["action"]
        if action_index != 1:
            self.stop_persistent_move()
            return False

        requested_angle = int(task_params["angle"]) % 360
        radius = int(task_params.get("radius", args.move_joystick_radius))
        start_x, start_y, end_x, end_y, radius = self._move_points(action_index, requested_angle, radius)
        if deadline is None:
            duration_ms = int(task_params.get("duration_ms", max(args.move_swipe_duration_ms, args.action_burst_duration_ms)))
        else:
            remaining_ms = int(max(1, (deadline - time.perf_counter()) * 1000))
            duration_ms = int(max(args.move_swipe_duration_ms, remaining_ms))

        self.stop_persistent_move()
        if args.move_debug:
            print(
                "move_center_swipe",
                {
                    "angle": requested_angle,
                    "radius": radius,
                    "start": (start_x, start_y),
                    "end": (end_x, end_y),
                    "duration_ms": duration_ms,
                },
            )

        try:
            result = subprocess.run(
                [
                    f"{self.scrcpy_dir}/adb",
                    "-s",
                    self.device_serial,
                    "shell",
                    "input",
                    "touchscreen",
                    "swipe",
                    str(start_x),
                    str(start_y),
                    str(end_x),
                    str(end_y),
                    str(max(1, duration_ms)),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(2.0, duration_ms / 1000.0 + 1.0),
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            if args.move_debug:
                print(f"move_center_swipe failed: {exc}")
            return False

    def execute_move_persistent_center_swipe(self, task_params):
        action_index = task_params["action"]
        if action_index != 1:
            self.stop_persistent_move()
            return False

        now = time.perf_counter()
        requested_angle = int(task_params["angle"]) % 360
        radius = int(task_params.get("radius", args.move_joystick_radius))
        with self._move_lock:
            self._cleanup_finished_move_locked()
            reusable = (
                self._move_process is not None
                and self._move_process.poll() is None
                and self._move_angle is not None
                and self._angular_distance(self._move_angle, requested_angle) <= args.persistent_move_angle_tolerance
            )
            if reusable:
                return True

            self._stop_adb_swipe_process_locked()

            start_x, start_y, end_x, end_y, radius = self._move_points(action_index, requested_angle, radius)
            hold_ms = int(max(args.move_swipe_duration_ms, args.persistent_center_swipe_hold_ms))
            if args.move_debug:
                print(
                    "move_persistent_center_swipe",
                    {
                        "angle": requested_angle,
                        "radius": radius,
                        "start": (start_x, start_y),
                        "end": (end_x, end_y),
                        "hold_ms": hold_ms,
                    },
                )
            try:
                self._move_process = subprocess.Popen(
                    [
                        f"{self.scrcpy_dir}/adb",
                        "-s",
                        self.device_serial,
                        "shell",
                        "input",
                        "touchscreen",
                        "swipe",
                        str(start_x),
                        str(start_y),
                        str(end_x),
                        str(end_y),
                        str(hold_ms),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self._move_process = None
                if args.move_debug:
                    print(f"move_persistent_center_swipe failed: {exc}")
                return False

            self._move_angle = requested_angle
            self._move_started_at = now
            self._move_until = now + hold_ms / 1000.0
            return True

    def execute_move_persistent_swipe(self, task_params):
        action_index = task_params["action"]
        if action_index != 1:
            self.stop_persistent_move()
            return False

        now = time.perf_counter()
        requested_angle = int(task_params["angle"]) % 360
        with self._move_lock:
            self._cleanup_finished_move_locked()
            reusable = (
                self._move_process is not None
                and self._move_process.poll() is None
                and self._move_angle is not None
                and self._angular_distance(self._move_angle, requested_angle) <= args.persistent_move_angle_tolerance
                and (now - self._move_started_at) * 1000.0 < args.persistent_move_refresh_ms
            )
            if reusable:
                return True

            self._stop_adb_swipe_process_locked()

            actions_detail = move_actions_detail[action_index]
            if self._show_action_log:
                print(actions_detail["action_name"])
            start_x, start_y = self.calculate_startpoint(self._move_center_position(actions_detail))
            radius = int(task_params.get("radius", args.move_joystick_radius or actions_detail["radius"]))
            end_x, end_y = self.calculate_endpoint((start_x, start_y), radius, requested_angle)
            press_x, press_y = (end_x, end_y) if args.persistent_move_press_endpoint else (start_x, start_y)
            hold_ms = int(max(args.move_swipe_duration_ms, args.persistent_move_hold_ms))
            command = [
                f"{self.scrcpy_dir}/adb",
                "-s",
                self.device_serial,
                "shell",
                "input",
                "swipe",
                str(press_x),
                str(press_y),
                str(end_x),
                str(end_y),
                str(hold_ms),
            ]
            self._move_process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._move_angle = requested_angle
            self._move_started_at = now
            self._move_until = now + hold_ms / 1000.0
            return True

    def execute_move_persistent_motionevent(self, task_params):
        action_index = task_params["action"]
        if action_index != 1:
            self.stop_persistent_move()
            return False

        if args.persistent_motionevent_use_center_swipe:
            return self.execute_move_persistent_center_swipe(task_params)

        if self._motionevent_failed:
            return self.execute_move_persistent_center_swipe(task_params)

        now = time.perf_counter()
        requested_angle = int(task_params["angle"]) % 360
        actions_detail = move_actions_detail[action_index]
        if self._show_action_log:
            print(actions_detail["action_name"])
        start_x, start_y = self.calculate_startpoint(self._move_center_position(actions_detail))
        radius = int(task_params.get("radius", args.move_joystick_radius or actions_detail["radius"]))
        end_x, end_y = self.calculate_endpoint((start_x, start_y), radius, requested_angle)
        fallback_to_swipe = False
        if args.move_debug:
            print(
                "move_persistent_motionevent",
                {
                    "angle": requested_angle,
                    "radius": radius,
                    "start": (start_x, start_y),
                    "end": (end_x, end_y),
                    "active": bool(self._motion_contact_active),
                },
            )

        with self._move_lock:
            self._cleanup_finished_move_locked()
            self._stop_adb_swipe_process_locked()
            reusable = (
                self._motion_contact_active
                and self._move_angle is not None
                and self._angular_distance(self._move_angle, requested_angle) <= args.persistent_move_angle_tolerance
            )
            if reusable:
                self._move_started_at = now
                self._move_until = 0.0
                return True

            if self._motion_contact_active:
                ok = self._send_motionevent("MOVE", end_x, end_y)
                if not ok:
                    self._send_motionevent("UP", *(self._motion_contact_point or (end_x, end_y)))
                    self._motion_contact_active = False
            else:
                ok = self._send_motionevent("DOWN", start_x, start_y)
                if ok:
                    time.sleep(max(0.005, args.move_motionevent_settle_ms / 1000.0))
                    ok = self._send_motionevent("MOVE", end_x, end_y)

            if not ok:
                self._motionevent_failed = True
                self._motion_contact_active = False
                self._motion_contact_point = None
                print("input motionevent failed; fallback to persistent_center_swipe.")
                fallback_to_swipe = True
            else:
                self._motion_contact_active = True
                self._motion_contact_point = (end_x, end_y)
                self._move_angle = requested_angle
                self._move_started_at = now
                self._move_until = 0.0

        if fallback_to_swipe:
            return self.execute_move_persistent_center_swipe(task_params)
        return True

    def execute_move_persistent_minitouch(self, task_params):
        action_index = task_params["action"]
        if action_index != 1:
            self.stop_persistent_move()
            return False

        if self._minitouch_failed:
            return self.execute_move_persistent_motionevent(task_params)

        now = time.perf_counter()
        requested_angle = int(task_params["angle"]) % 360
        actions_detail = move_actions_detail[action_index]
        if self._show_action_log:
            print(actions_detail["action_name"])
        start_x, start_y = self.calculate_startpoint(self._move_center_position(actions_detail))
        radius = int(task_params.get("radius", args.move_joystick_radius or actions_detail["radius"]))
        end_x, end_y = self.calculate_endpoint((start_x, start_y), radius, requested_angle)
        start_x, start_y = self._map_display_to_minitouch(start_x, start_y)
        end_x, end_y = self._map_display_to_minitouch(end_x, end_y)
        fallback_to_motionevent = False

        with self._move_lock:
            self._cleanup_finished_move_locked()
            self._stop_adb_swipe_process_locked()
            reusable = (
                self._minitouch_contact_active
                and self._move_angle is not None
                and self._angular_distance(self._move_angle, requested_angle) <= args.persistent_move_angle_tolerance
            )
            if reusable:
                self._move_started_at = now
                return True

            if self._minitouch_contact_active:
                ok = self._send_minitouch(f"m 1 {end_x} {end_y} 100\nc\n")
            else:
                ok = self._send_minitouch(
                    f"d 1 {start_x} {start_y} 300\nc\n"
                    f"m 1 {end_x} {end_y} 100\nc\n"
                )

            if not ok:
                self._minitouch_failed = True
                self._minitouch_contact_active = False
                fallback_to_motionevent = True
            else:
                self._minitouch_contact_active = True
                self._move_angle = requested_angle
                self._move_started_at = now
                self._move_until = 0.0

        if fallback_to_motionevent:
            return self.execute_move_persistent_motionevent(task_params)
        return True

    def execute_move_motionevent(self, task_params, deadline):
        action_index = task_params["action"]
        if action_index != 1:
            return False

        actions_detail = move_actions_detail[action_index]
        if self._show_action_log:
            print(actions_detail["action_name"])

        start_x, start_y = self.calculate_startpoint(self._move_center_position(actions_detail))
        radius = int(task_params.get("radius", args.move_joystick_radius or actions_detail["radius"]))
        end_x, end_y = self.calculate_endpoint((start_x, start_y), radius, task_params["angle"])
        adb_prefix = [f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell", "input", "motionevent"]

        subprocess.run([*adb_prefix, "DOWN", str(start_x), str(start_y)])
        time.sleep(max(0.01, args.move_motionevent_settle_ms / 1000.0))
        subprocess.run([*adb_prefix, "MOVE", str(end_x), str(end_y)])

        remaining_sec = deadline - time.perf_counter()
        if remaining_sec > 0:
            time.sleep(remaining_sec)

        subprocess.run([*adb_prefix, "UP", str(end_x), str(end_y)])
        return True

    def execute_info(self, task_params):
        # 信息操作逻辑
        action_index = task_params['action']
        if not action_index == 0:
            actions_detail = info_actions_detail[action_index]
            if self._show_action_log:
                print(actions_detail['action_name'])
            start_x, start_y = self.calculate_startpoint(actions_detail['position'])
            subprocess.run([f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell",
                            "input", "tap", str(start_x), str(start_y)])
            return True
        return False

    def execute_attack(self, task_params):
        # 攻击操作逻辑
        action_index = task_params['action']
        action_type = task_params['action_type']
        arg1 = task_params['arg1']
        arg2 = task_params['arg2'] + 1
        arg3 = task_params['arg3'] + 1

        if action_index != 0:
            actions_detail = attack_actions_detail[action_index]
            if self._show_action_log:
                print(actions_detail['action_name'])
            start_x, start_y = self.calculate_startpoint(actions_detail['position'])
            if action_index < 7:
                subprocess.run([f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell",
                                "input", "tap", str(start_x), str(start_y)])
            else:
                if action_type == 0:
                    subprocess.run([f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell",
                                    "input", "tap", str(start_x), str(start_y)])
                elif action_type == 1:
                    end_x, end_y = self.calculate_endpoint((start_x, start_y),
                                                           arg2,
                                                           arg1)
                    subprocess.run([f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell",
                                    "input", "swipe", str(start_x), str(start_y), str(end_x), str(end_y),
                                    str(args.skill_swipe_duration_ms)])
                else:
                    subprocess.run([f"{self.scrcpy_dir}/adb", "-s", self.device_serial, "shell",
                                    "input", "swipe", str(start_x), str(start_y), str(start_x), str(start_y),
                                    str(arg3 * args.long_press_unit_ms)])
            return True
        return False

    def execute_move_burst(self, task_params, deadline):
        if args.continuous_move_enabled and args.move_control_mode == "persistent_minitouch":
            params = dict(task_params)
            params["radius"] = args.move_joystick_radius
            return self.execute_move_persistent_minitouch(params)

        if args.continuous_move_enabled and args.move_control_mode == "persistent_motionevent":
            params = dict(task_params)
            params["radius"] = args.move_joystick_radius
            if args.persistent_motionevent_use_center_swipe:
                return self.execute_move_persistent_center_swipe(params)
            return self.execute_move_persistent_motionevent(params)

        if args.continuous_move_enabled and args.move_control_mode == "persistent_swipe":
            params = dict(task_params)
            params["radius"] = args.move_joystick_radius
            return self.execute_move_persistent_swipe(params)

        if args.continuous_move_enabled and args.move_control_mode == "center_swipe":
            params = dict(task_params)
            params["radius"] = args.move_joystick_radius
            return self.execute_move_center_swipe(params, deadline)

        if args.continuous_move_enabled and args.move_control_mode == "motionevent":
            params = dict(task_params)
            params["radius"] = args.move_joystick_radius
            return self.execute_move_motionevent(params, deadline)

        if args.continuous_move_enabled and args.move_control_mode == "swipe":
            duration_ms = int(max(
                args.move_swipe_duration_ms,
                (deadline - time.perf_counter()) * 1000 + args.continuous_move_extra_ms,
            ))
            params = dict(task_params)
            params["duration_ms"] = duration_ms
            params["radius"] = args.move_joystick_radius
            return self.execute_move(params)

        executed = False
        interval_sec = max(0.03, args.move_repeat_interval_ms / 1000.0)
        while time.perf_counter() < deadline:
            executed = self.execute_move(task_params) or executed
            remaining_sec = deadline - time.perf_counter()
            if remaining_sec <= 0:
                break
            time.sleep(min(interval_sec, remaining_sec))
        return executed

    def execute_attack_burst(self, task_params, deadline):
        action_index = task_params["action"]
        if action_index == 0:
            return False

        if action_index in [1, 2, 3]:
            interval_ms = (
                args.basic_attack_repeat_interval_ms
                if action_index == 1
                else args.attack_repeat_interval_ms
            )
            return self._execute_repeated_attack(task_params, deadline, interval_ms)

        return self.execute_attack(task_params)

    def _execute_repeated_attack(self, task_params, deadline, interval_ms):
        executed = False
        interval_sec = max(0.03, int(interval_ms) / 1000.0)
        while time.perf_counter() < deadline:
            tap_started_at = time.perf_counter()
            executed = self.execute_attack(task_params) or executed
            next_tap_at = tap_started_at + interval_sec
            remaining_sec = deadline - time.perf_counter()
            if remaining_sec <= 0:
                break
            sleep_sec = min(max(0.0, next_tap_at - time.perf_counter()), remaining_sec)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
        return executed

    def execute_basic_attack_burst(self, deadline, initial_delay_ms=0):
        if initial_delay_ms > 0:
            time.sleep(initial_delay_ms / 1000.0)

        params = {"action": 1, "action_type": 0, "arg1": 0, "arg2": 0, "arg3": 0}
        return self._execute_repeated_attack(
            params,
            deadline,
            args.basic_attack_repeat_interval_ms,
        )

    def _parse_combat_assist_skill_actions(self):
        values = []
        for raw_item in str(args.combat_assist_skill_actions).split(","):
            raw_item = raw_item.strip()
            if not raw_item:
                continue
            try:
                value = int(raw_item)
            except ValueError:
                continue
            if value in attack_actions_detail and value >= 8:
                values.append(value)
        return values or [8, 9, 10]

    @staticmethod
    def _interval_due(last_time, interval_ms):
        return (time.perf_counter() - float(last_time)) * 1000.0 >= max(0, int(interval_ms))

    def _combat_assist_skill_params(self, current_angle):
        skill_actions = self._parse_combat_assist_skill_actions()
        skill_action = skill_actions[self._assist_skill_index % len(skill_actions)]
        self._assist_skill_index += 1

        aim_angle = int(current_angle) % 360 if int(current_angle) != 0 else int(args.combat_assist_default_skill_angle) % 360
        if args.combat_assist_directional_skills:
            return {
                "action": skill_action,
                "action_type": 1,
                "arg1": aim_angle,
                "arg2": int(max(1, args.combat_assist_skill_distance)),
                "arg3": 0,
            }

        return {"action": skill_action, "action_type": 0, "arg1": 0, "arg2": 0, "arg3": 0}

    def _submit_combat_assist_actions(
        self,
        futures,
        deadline,
        move_action,
        angle,
        info_action,
        attack_action,
        suppress_attack,
        suppress_info,
    ):
        if not args.combat_assist_enabled:
            return

        if attack_action != 0:
            self._assist_last_basic_attack_at = time.perf_counter()
            if attack_action in [8, 9, 10]:
                self._assist_last_skill_at = time.perf_counter()

        if info_action != 0:
            self._assist_last_buy_at = time.perf_counter()

        if args.combat_assist_idle_move_enabled and move_action == 0:
            futures.append((
                "move",
                self.executor.submit(
                    self.execute_move_burst,
                    {"action": 1, "angle": int(args.combat_assist_idle_move_angle) % 360},
                    deadline,
                ),
            ))

        if not suppress_info and args.combat_assist_buy_enabled and info_action == 0:
            if self._interval_due(self._assist_last_buy_at, args.combat_assist_buy_interval_ms):
                buy_action = 1 if self._assist_buy_index % 2 == 0 else 2
                self._assist_buy_index += 1
                self._assist_last_buy_at = time.perf_counter()
                futures.append((
                    "info",
                    self.executor.submit(self.execute_info, {"action": buy_action}),
                ))

        if suppress_attack:
            return

        if args.combat_assist_basic_attack_enabled and attack_action == 0 and move_action == 0 and info_action == 0:
            if self._interval_due(self._assist_last_basic_attack_at, args.combat_assist_attack_interval_ms):
                self._assist_last_basic_attack_at = time.perf_counter()
                futures.append((
                    "attack",
                    self.executor.submit(self.execute_basic_attack_burst, deadline, 0),
                ))

        if args.combat_assist_skill_enabled and attack_action not in [8, 9, 10]:
            if self._interval_due(self._assist_last_skill_at, args.combat_assist_skill_interval_ms):
                self._assist_last_skill_at = time.perf_counter()
                futures.append((
                    "attack",
                    self.executor.submit(self.execute_attack, self._combat_assist_skill_params(angle)),
                ))

        if args.combat_assist_recover_enabled and attack_action != 5:
            if self._interval_due(self._assist_last_recover_at, args.combat_assist_recover_interval_ms):
                self._assist_last_recover_at = time.perf_counter()
                futures.append((
                    "attack",
                    self.executor.submit(
                        self.execute_attack,
                        {"action": 5, "action_type": 0, "arg1": 0, "arg2": 0, "arg3": 0},
                    ),
                ))

    def execute_action_burst(self, action, suppress_attack=False, suppress_info=False):
        move_action, angle, info_action, attack_action, action_type, arg1, arg2, arg3 = [int(v) for v in action]
        if move_action == 0 and info_action == 0 and attack_action == 0:
            self.stop_persistent_move()
            if (
                not args.combat_assist_enabled
                or (suppress_attack and suppress_info)
            ):
                return False, False, False

        if move_action == 0 and args.move_control_mode in ["persistent_minitouch", "persistent_motionevent", "persistent_swipe"]:
            self.stop_persistent_move()

        burst_sec = max(0.05, args.action_burst_duration_ms / 1000.0)
        deadline = time.perf_counter() + burst_sec
        futures = []

        if move_action != 0:
            futures.append((
                "move",
                self.executor.submit(self.execute_move_burst, {"action": move_action, "angle": angle}, deadline),
            ))

        if info_action != 0:
            futures.append((
                "info",
                self.executor.submit(self.execute_info, {"action": info_action}),
            ))

        if attack_action != 0:
            futures.append((
                "attack",
                self.executor.submit(
                    self.execute_attack_burst,
                    {"action": attack_action, "action_type": action_type, "arg1": arg1, "arg2": arg2, "arg3": arg3},
                    deadline,
                ),
            ))

        self._submit_combat_assist_actions(
            futures,
            deadline,
            move_action,
            angle,
            info_action,
            attack_action,
            suppress_attack,
            suppress_info,
        )

        should_autofire = (
            args.autofire_basic_attack
            and not suppress_attack
            and attack_action in [0, 8, 9, 10]
            and (move_action != 0 or attack_action in [8, 9, 10] or info_action in [1, 2, 6, 7, 8])
        )
        if should_autofire:
            delay_ms = args.skill_followup_attack_delay_ms if attack_action in [8, 9, 10] else 0
            futures.append((
                "attack",
                self.executor.submit(self.execute_basic_attack_burst, deadline, delay_ms),
            ))

        executed_move = False
        executed_info = False
        executed_attack = False
        for name, future in futures:
            result = bool(future.result())
            if name == "move":
                executed_move = executed_move or result
            elif name == "info":
                executed_info = executed_info or result
            else:
                executed_attack = executed_attack or result

        return executed_move, executed_info, executed_attack

    def calculate_startpoint(self, center):
        p_x, p_y = center
        start_x = int(self.actual_width * p_x)
        start_y = int(self.actual_height * p_y)
        return start_x, start_y

    @staticmethod
    def _move_center_position(actions_detail):
        if args.move_joystick_center_enabled:
            return (
                max(0.0, min(1.0, float(args.move_joystick_center_x))),
                max(0.0, min(1.0, float(args.move_joystick_center_y))),
            )
        return actions_detail["position"]

    def calculate_endpoint(self, center, radius, angle):
        angle_rad = math.radians(angle)
        x = int(center[0] + radius * math.cos(angle_rad))
        y = int(center[1] + radius * math.sin(angle_rad))
        return x, y

    def find_window_handle(self):
        return win32gui.FindWindow(None, args.window_title)

    def is_window_available(self):
        return self.find_window_handle() != 0

    def show_scrcpy(self):
        try:
            return subprocess.Popen(
                [f"{self.scrcpy_dir}/scrcpy.exe", "-s", self.device_serial, "-m", "1080", "--window-title",
                 args.window_title])
        except OSError as exc:
            print(f"scrcpy 启动失败: {exc}")
            return None

    def action_move(self, params):
        return self.executor.submit(self.execute_move, params)

    def action_attack(self, params):
        return self.executor.submit(self.execute_attack, params)

    def action_info(self, params):
        return self.executor.submit(self.execute_info, params)

    def stop(self):
        self.stop_persistent_move()
        self.executor.shutdown(wait=True)

    def _adb(self, extra_args, timeout=None):
        return subprocess.run(
            [f'{self.scrcpy_dir}/adb', '-s', self.device_serial, *extra_args],
            capture_output=True,
            text=False,
            timeout=timeout,
        )

    def _decode_screenshot_bytes(self, screenshot_data, source_name):
        if not screenshot_data:
            print(f"Failed to decode the screenshot: empty output from {source_name}.")
            return None

        candidates = []

        def add_candidate(data, label):
            if data and all(existing_label != label for existing_label, _ in candidates):
                candidates.append((label, data))

        add_candidate(screenshot_data, source_name)

        png_index = screenshot_data.find(b"\x89PNG")
        if png_index > 0:
            add_candidate(screenshot_data[png_index:], f"{source_name}, clipped PNG prefix")

        # Some adb shell paths insert CR bytes into PNG streams on Windows.
        add_candidate(screenshot_data.replace(b"\r\r\n", b"\r\n"), f"{source_name}, fixed CRCRLF")
        add_candidate(screenshot_data.replace(b"\r\n", b"\n"), f"{source_name}, fixed CRLF")

        for label, data in candidates:
            screenshot_array = np.frombuffer(data, np.uint8)
            screenshot_image = cv2.imdecode(screenshot_array, cv2.IMREAD_COLOR)
            if screenshot_image is not None:
                return screenshot_image

        head = screenshot_data[:32].hex()
        print(f"Failed to decode the screenshot from {source_name}. bytes={len(screenshot_data)}, head={head}")
        return None

    def _capture_screenshot_via_stdout(self, source_name, command):
        try:
            result = self._adb(command, timeout=max(1.0, args.adb_screenshot_timeout_sec))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Screenshot command failed ({source_name}): {exc}")
            return None

        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            print(f"Screenshot command failed ({source_name}): {error}")
            return None

        return self._decode_screenshot_bytes(result.stdout, source_name)

    def _capture_screenshot_via_remote_file(self):
        remote_path = "/sdcard/wzry_ai_screen.png"
        local_path = None
        try:
            timeout = max(1.0, args.adb_screenshot_timeout_sec)
            save_result = self._adb(["shell", "screencap", "-p", remote_path], timeout=timeout)
            if save_result.returncode != 0:
                error = save_result.stderr.decode("utf-8", errors="replace").strip()
                print(f"Screenshot command failed (remote file): {error}")
                return None

            temp_file = tempfile.NamedTemporaryFile(prefix="wzry_ai_screen_", suffix=".png", delete=False)
            local_path = temp_file.name
            temp_file.close()

            pull_result = self._adb(["pull", remote_path, local_path], timeout=timeout)
            if pull_result.returncode != 0:
                error = pull_result.stderr.decode("utf-8", errors="replace").strip()
                print(f"Screenshot command failed (remote pull): {error}")
                return None

            image = cv2.imread(local_path, cv2.IMREAD_COLOR)
            if image is None:
                with open(local_path, "rb") as file:
                    return self._decode_screenshot_bytes(file.read(), "remote screencap file")
            return image
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Screenshot command failed (remote fallback): {exc}")
            return None
        finally:
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass

    def _capture_screenshot_image(self):
        method = args.adb_screenshot_method

        if method == "remote_file":
            return self._capture_screenshot_via_remote_file()
        if method == "exec_out":
            return self._capture_screenshot_via_stdout("exec-out screencap -p", ["exec-out", "screencap", "-p"])
        if method == "shell":
            return self._capture_screenshot_via_stdout("shell screencap -p", ["shell", "screencap", "-p"])

        methods = [
            self._capture_screenshot_via_remote_file,
            lambda: self._capture_screenshot_via_stdout("exec-out screencap -p", ["exec-out", "screencap", "-p"]),
            lambda: self._capture_screenshot_via_stdout("shell screencap -p", ["shell", "screencap", "-p"]),
        ]
        for capture in methods:
            image = capture()
            if image is not None:
                return image
        return None

    def take_screenshot_save(self):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        screenshot_filename = f"screenshot_{timestamp}.png"

        try:
            screenshot_image = self.take_screenshot()
            if screenshot_image is not None:
                cv2.imwrite(screenshot_filename, screenshot_image)
                print(f"Screenshot saved to {screenshot_filename}")
            else:
                print("Failed to take screenshot.")
        except FileNotFoundError:
            print("adb is not installed or not found in your PATH.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")

    def take_screenshot(self):
        try:
            return self._capture_screenshot_image()
        except FileNotFoundError:
            print("adb is not installed or not found in your PATH.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")

        return None

    def screenshot_window(self):
        """
        截取指定窗口的内容并返回图像数据。

        参数:
        window_name (str): 窗口标题的部分或全部字符串。

        返回:
        np.ndarray: 截图的图像数据，如果窗口未找到则返回 None。
        """
        try:
            # 获取窗口句柄
            handle = self.find_window_handle()
            if handle == 0:
                raise Exception(f"窗口 '{args.window_title}' 未找到。")

            if self._qt_app is None:
                self._qt_app = QApplication.instance() or QApplication(sys.argv)
            screen = self._qt_app.primaryScreen()

            # 截取指定窗口的内容
            img = screen.grabWindow(handle).toImage()

            # 将 QImage 转换为 numpy 数组
            img = img.convertToFormat(QImage.Format.Format_RGB32)
            width = img.width()
            height = img.height()
            ptr = img.bits()
            ptr.setsize(height * width * 4)
            arr = np.array(ptr).reshape(height, width, 4)
            arr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)

            return arr
        except Exception as e:
            print(e)
            return None


def generate_random_number(n):
    return random.randint(0, n)
