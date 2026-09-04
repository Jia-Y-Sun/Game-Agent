import datetime
import json
import os
import subprocess
import threading
import time
from collections import deque

import cv2

from android_tool import AndroidTool
from argparses import args
from touch_event_parser import MultiTouchState, TouchActionMapper
from traffic_probe import build_traffic_probe

_WINDOW_FALLBACK_WARNED = False


def _run_adb_text(adb_path, command, timeout=8):
    result = subprocess.run(
        [adb_path, *command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def _list_adb_devices(adb_path):
    returncode, stdout, stderr = _run_adb_text(adb_path, ["devices", "-l"])
    devices = {}
    if returncode != 0:
        return devices, stdout, stderr

    for line in stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2:
            devices[fields[0]] = {
                "state": fields[1],
                "line": line,
            }
    return devices, stdout, stderr


def _ensure_device_ready(adb_path, device_serial):
    devices, stdout, stderr = _list_adb_devices(adb_path)
    device = devices.get(device_serial)
    if device and device["state"] == "device":
        return

    print("")
    print("ADB 设备预检查失败：目标设备当前不可用。")
    print(f"目标设备: {device_serial}")
    print("当前 adb devices -l 输出:")
    print((stdout or stderr or "").strip() or "(empty)")
    if device is None:
        print("原因：设备列表里没有目标设备。请检查 USB、授权弹窗、设备 ID，或重启 adb server。")
    else:
        print(f"原因：目标设备状态是 {device['state']}，不是 device。")
    raise RuntimeError("ADB target device is not ready")


class TouchEventRecorder:
    def __init__(self, adb_path, device_serial, touch_device, raw_log_path, sample_log_path):
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.touch_device = touch_device
        self.raw_log_path = raw_log_path
        self.sample_log_path = sample_log_path

        self.parser = MultiTouchState()
        self.samples = deque(maxlen=50000)
        self.active_samples = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.process = None
        self.thread = None
        self.raw_file = None
        self.sample_file = None
        self.raw_line_count = 0
        self.parsed_sample_count = 0

    def start(self):
        os.makedirs(os.path.dirname(self.raw_log_path), exist_ok=True)
        self.raw_file = open(self.raw_log_path, "a", encoding="utf-8")
        self.sample_file = open(self.sample_log_path, "a", encoding="utf-8")

        command = [self.adb_path, "-s", self.device_serial, "shell", "getevent", "-lt"]
        if self.touch_device:
            command.append(self.touch_device)

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        time.sleep(0.25)
        if self.process.poll() is not None:
            raise RuntimeError(
                f"getevent exited immediately with returncode={self.process.poll()}. "
                "Run touch_event_diagnose.py after confirming adb devices shows the target as device."
            )

    def _read_loop(self):
        while not self.stop_event.is_set():
            line = self.process.stdout.readline()
            if not line:
                if self.process.poll() is not None:
                    break
                time.sleep(0.01)
                continue

            host_ts = time.time()
            line = line.rstrip("\r\n")
            self.raw_file.write(json.dumps({"host_ts": host_ts, "line": line}, ensure_ascii=False) + "\n")
            self.raw_line_count += 1

            parsed_samples = self.parser.process_line(line, host_ts)
            if parsed_samples:
                with self.lock:
                    for sample in parsed_samples:
                        self.parsed_sample_count += 1
                        self.samples.append(sample)
                        sample_key = (sample.get("slot", 0), sample.get("tracking_id") or sample.get("slot", 0))
                        if sample.get("active"):
                            self.active_samples[sample_key] = sample
                        else:
                            self.active_samples.pop(sample_key, None)
                        self.sample_file.write(json.dumps(sample, ensure_ascii=False) + "\n")

    def get_samples(self, start_ts, end_ts):
        with self.lock:
            window_samples = [
                dict(sample)
                for sample in self.samples
                if start_ts <= sample["host_ts"] <= end_ts
            ]
            existing_keys = {
                (sample.get("slot", 0), sample.get("tracking_id") or sample.get("slot", 0))
                for sample in window_samples
            }
            for sample_key, sample in self.active_samples.items():
                if sample_key in existing_keys or sample["host_ts"] > end_ts:
                    continue
                held_sample = dict(sample)
                held_sample["host_ts"] = start_ts
                held_sample["phase"] = "hold"
                window_samples.append(held_sample)
            window_samples.sort(key=lambda item: item["host_ts"])
            return window_samples

    def stop(self):
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.raw_file is not None:
            self.raw_file.flush()
            self.raw_file.close()
        if self.sample_file is not None:
            self.sample_file.flush()
            self.sample_file.close()

    def stats(self):
        return {
            "raw_line_count": self.raw_line_count,
            "parsed_sample_count": self.parsed_sample_count,
            "getevent_running": self.process is not None and self.process.poll() is None,
            "getevent_returncode": None if self.process is None else self.process.poll(),
        }


def _episode_id():
    if args.human_episode_id:
        return args.human_episode_id
    return "human_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _capture_frame(tool):
    global _WINDOW_FALLBACK_WARNED
    if args.human_capture_source == "adb_screencap":
        return tool.take_screenshot()

    if tool.is_window_available():
        frame = tool.screenshot_window()
        if frame is not None and frame.size > 0:
            return frame

    if not _WINDOW_FALLBACK_WARNED:
        print("未找到 scrcpy 窗口，自动改用 adb screencap 截图；采集会稍慢，但不影响触摸标签采集。")
        _WINDOW_FALLBACK_WARNED = True
    return tool.take_screenshot()


def _prepare_frame_for_save(frame):
    max_width = int(args.human_frame_max_width)
    if max_width > 0 and frame.shape[1] > max_width:
        scale = max_width / float(frame.shape[1])
        new_size = (max_width, max(1, int(round(frame.shape[0] * scale))))
        frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
    return frame


def _write_metadata(path, episode_id, tool):
    metadata = {
        "episode_id": episode_id,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "device_serial": tool.device_serial,
        "screen_width": tool.actual_width,
        "screen_height": tool.actual_height,
        "capture_source": args.human_capture_source,
        "fps": args.human_collect_fps,
        "action_only": bool(args.human_action_only),
        "save_frames": bool(args.human_save_frames and not args.human_action_only),
        "frame_max_width": args.human_frame_max_width,
        "jpeg_quality": args.human_jpeg_quality,
        "collect_traffic": bool(args.human_collect_traffic),
        "touch_device": args.touch_device,
        "touch_label_window_ms": args.touch_label_window_ms,
        "touch_transform": {
            "raw_width": args.touch_raw_width,
            "raw_height": args.touch_raw_height,
            "swap_xy": args.touch_swap_xy,
            "invert_x": args.touch_invert_x,
            "invert_y": args.touch_invert_y,
        },
        "move_joystick_center": {
            "enabled": bool(args.move_joystick_center_enabled),
            "x": float(args.move_joystick_center_x),
            "y": float(args.move_joystick_center_y),
        },
        "action_format": [
            "move_action",
            "angle",
            "info_action",
            "attack_action",
            "action_type",
            "arg1",
            "arg2",
            "arg3",
        ],
    }
    with open(path, "w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)


def _write_latest_pointers(human_data_dir, episode_dir, save_frames):
    human_data_dir = os.path.abspath(human_data_dir)
    os.makedirs(human_data_dir, exist_ok=True)
    latest_path = os.path.join(human_data_dir, "latest_episode.txt")
    latest_action_path = os.path.join(human_data_dir, "latest_action_episode.txt")

    with open(latest_path, "w", encoding="utf-8") as file:
        file.write(episode_dir)
    with open(latest_action_path, "w", encoding="utf-8") as file:
        file.write(episode_dir)

    if save_frames:
        latest_frame_path = os.path.join(human_data_dir, "latest_frame_episode.txt")
        with open(latest_frame_path, "w", encoding="utf-8") as file:
            file.write(episode_dir)


def collect_human_data():
    adb_path = os.path.join("scrcpy-win64-v2.0", "adb")
    _ensure_device_ready(adb_path, args.iphone_id)
    tool = AndroidTool()
    adb_path = f"{tool.scrcpy_dir}/adb"
    if not args.human_action_only and args.human_capture_source == "scrcpy_window":
        tool.show_scrcpy()
        time.sleep(1.0)
        if not tool.is_window_available():
            print(f"提示：未检测到标题为 '{args.window_title}' 的 scrcpy 窗口，将在采集时自动使用 adb screencap。")

    episode_id = _episode_id()
    episode_dir = os.path.abspath(os.path.join(args.human_data_dir, episode_id))
    frames_dir = os.path.join(episode_dir, "frames")
    os.makedirs(episode_dir, exist_ok=True)
    save_frames = bool(args.human_save_frames and not args.human_action_only)
    if save_frames:
        os.makedirs(frames_dir, exist_ok=True)

    samples_path = os.path.join(episode_dir, "samples.jsonl")
    raw_touch_path = os.path.join(episode_dir, "raw_getevent.jsonl")
    touch_samples_path = os.path.join(episode_dir, "touch_samples.jsonl")
    metadata_path = os.path.join(episode_dir, "metadata.json")
    _write_metadata(metadata_path, episode_id, tool)

    recorder = TouchEventRecorder(
        adb_path=adb_path,
        device_serial=tool.device_serial,
        touch_device=args.touch_device,
        raw_log_path=raw_touch_path,
        sample_log_path=touch_samples_path,
    )
    recorder.start()
    print(f"触摸采集命令: adb -s {tool.device_serial} shell getevent -lt {args.touch_device}".rstrip())

    mapper = TouchActionMapper(screen_width=tool.actual_width, screen_height=tool.actual_height)
    traffic_probe = build_traffic_probe(tool) if args.human_collect_traffic else None

    fps = max(0.1, float(args.human_collect_fps))
    interval_sec = 1.0 / fps
    label_window_sec = max(0.0, args.touch_label_window_ms / 1000.0)
    started_at = time.time()
    next_capture_at = started_at
    frame_id = 0

    print(f"采集目录: {episode_dir}")
    if args.human_action_only:
        print("轻量动作采集模式：不截图，只采触摸动作；适合生成开局走线 profile。")
    elif not save_frames:
        print("采集模式：会截图用于对齐时间，但不保存图片；适合生成动作 profile。")
    else:
        print("采集模式：保存截图和触摸动作；适合行为克隆训练。")
    print("开始采集：请正常操作手机或 scrcpy 窗口；按 Ctrl+C 结束。")

    try:
        with open(samples_path, "a", encoding="utf-8") as samples_file:
            while True:
                now = time.time()
                if args.human_collect_seconds > 0 and now - started_at >= args.human_collect_seconds:
                    break

                if now < next_capture_at:
                    time.sleep(next_capture_at - now)

                traffic_start = traffic_probe.begin_step() if traffic_probe is not None else None
                capture_started_at = time.time()
                frame = None
                frame_ts = capture_started_at

                if not args.human_action_only:
                    frame = _capture_frame(tool)
                    frame_ts = time.time()
                    if frame is None or frame.size == 0:
                        print("截图失败，跳过当前帧")
                        next_capture_at += interval_sec
                        continue

                if label_window_sec > 0:
                    time.sleep(label_window_sec)
                label_end_ts = time.time()

                frame_path = ""
                if save_frames and frame is not None:
                    frame_name = f"{frame_id:08d}.jpg"
                    frame_path = os.path.abspath(os.path.join(frames_dir, frame_name))
                    frame_to_save = _prepare_frame_for_save(frame)
                    jpeg_quality = int(max(30, min(100, args.human_jpeg_quality)))
                    cv2.imwrite(frame_path, frame_to_save, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])

                touch_samples = recorder.get_samples(capture_started_at, label_end_ts)
                parsed_action, action_diagnostics = mapper.samples_to_action(touch_samples)
                traffic_metrics = traffic_probe.end_step(traffic_start) if traffic_probe is not None else None

                record = {
                    "episode_id": episode_id,
                    "frame_id": frame_id,
                    "timestamp": frame_ts,
                    "frame_path": frame_path,
                    "frame_saved": bool(frame_path),
                    "parsed_action": parsed_action,
                    "action_diagnostics": action_diagnostics,
                    "touch_samples": touch_samples,
                    "traffic": traffic_metrics,
                    "capture_source": "touch_only" if args.human_action_only else args.human_capture_source,
                }
                samples_file.write(json.dumps(record, ensure_ascii=False) + "\n")

                frame_id += 1
                if frame_id % 20 == 0:
                    samples_file.flush()
                    print(f"已采集 {frame_id} 帧，最近动作: {parsed_action}")

                next_capture_at = max(next_capture_at + interval_sec, time.time())
    except KeyboardInterrupt:
        print("收到 Ctrl+C，正在结束采集。")
    finally:
        recorder.stop()
        tool.stop()
        recorder_stats = recorder.stats()
        print(
            "触摸采集统计: "
            f"raw_lines={recorder_stats['raw_line_count']}, "
            f"parsed_samples={recorder_stats['parsed_sample_count']}, "
            f"getevent_returncode={recorder_stats['getevent_returncode']}"
        )
        if recorder_stats["raw_line_count"] == 0:
            print("警告：没有采到任何 getevent 原始触摸行。请先运行 touch_event_diagnose.py 确认触摸设备。")
        if recorder_stats["parsed_sample_count"] > 0 and frame_id > 0:
            _write_latest_pointers(args.human_data_dir, episode_dir, save_frames)
            print("已更新 latest episode 指针。")
        else:
            print("本次未采到有效触摸样本，未更新 latest episode 指针。")
        print(f"采集完成: {samples_path}")
        print(f"触摸原始日志: {raw_touch_path}")


if __name__ == "__main__":
    collect_human_data()
