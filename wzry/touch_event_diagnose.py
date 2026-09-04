import argparse
import os
import queue
import re
import subprocess
import threading
import time


def parse_args():
    parser = argparse.ArgumentParser(description="Diagnose adb getevent touch input.")
    parser.add_argument("--adb_path", default=os.path.join("scrcpy-win64-v2.0", "adb"))
    parser.add_argument("--device_id", default="")
    parser.add_argument("--touch_device", default="")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--max_lines", type=int, default=30)
    return parser.parse_args()


def run_text(command, timeout=10):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def resolve_device_id(adb_path, requested):
    if requested:
        return requested

    returncode, stdout, stderr = run_text([adb_path, "devices"])
    if returncode != 0:
        raise RuntimeError(stderr.strip() or "adb devices failed")

    devices = []
    for line in stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])

    if len(devices) != 1:
        raise RuntimeError(f"请用 --device_id 指定设备；当前可用设备: {devices}")
    return devices[0]


def parse_input_devices(output):
    devices = []
    current = None

    for line in output.splitlines():
        match = re.match(r"add device \d+:\s+(?P<path>/dev/input/event\d+)", line)
        if match:
            if current is not None:
                devices.append(current)
            current = {
                "path": match.group("path"),
                "name": "",
                "body": [],
            }
            continue

        if current is None:
            continue

        current["body"].append(line)
        name_match = re.search(r'name:\s+"(?P<name>.*)"', line)
        if name_match:
            current["name"] = name_match.group("name")

    if current is not None:
        devices.append(current)
    return devices


def find_range(body, code):
    pattern = re.compile(rf"{re.escape(code)}.*min\s+(-?\d+),\s+max\s+(-?\d+)")
    for line in body:
        match = pattern.search(line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def print_candidates(adb_path, device_id):
    returncode, stdout, stderr = run_text([adb_path, "-s", device_id, "shell", "getevent", "-lp"], timeout=15)
    if returncode != 0:
        print(stderr.strip() or "getevent -lp failed")
        return []

    devices = parse_input_devices(stdout)
    candidates = []
    for device in devices:
        text = "\n".join(device["body"])
        has_x = "ABS_MT_POSITION_X" in text or "0035" in text
        has_y = "ABS_MT_POSITION_Y" in text or "0036" in text
        if has_x and has_y:
            candidates.append(device)

    print("候选触摸设备：")
    if not candidates:
        print("  没有自动识别到 ABS_MT_POSITION_X/Y，下面列出所有输入设备供你手动看：")
        for device in devices:
            print(f"  {device['path']}  {device['name']}")
        return []

    for device in candidates:
        x_range = find_range(device["body"], "ABS_MT_POSITION_X")
        y_range = find_range(device["body"], "ABS_MT_POSITION_Y")
        print(f"  {device['path']}  {device['name']}")
        if x_range:
            print(f"    X range: {x_range[0]}..{x_range[1]}")
        if y_range:
            print(f"    Y range: {y_range[0]}..{y_range[1]}")
    return candidates


def reader_thread(process, output_queue):
    while True:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                break
            time.sleep(0.01)
            continue
        output_queue.put(line.rstrip("\r\n"))


def probe_events(adb_path, device_id, touch_device, seconds, max_lines):
    command = [adb_path, "-s", device_id, "shell", "getevent", "-lt"]
    if touch_device:
        command.append(touch_device)

    print("")
    print("开始实时探测触摸事件。现在请在手机屏幕上滑动摇杆、点普攻/技能。")
    print("命令:", " ".join(command))

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    output_queue = queue.Queue()
    thread = threading.Thread(target=reader_thread, args=(process, output_queue), daemon=True)
    thread.start()

    lines = []
    deadline = time.time() + max(0.5, seconds)
    while time.time() < deadline:
        try:
            line = output_queue.get(timeout=0.1)
            lines.append(line)
        except queue.Empty:
            pass

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    thread.join(timeout=1)

    while not output_queue.empty():
        lines.append(output_queue.get_nowait())

    print("")
    print(f"探测结果: {len(lines)} 行")
    for line in lines[:max_lines]:
        print(line)
    if len(lines) > max_lines:
        print(f"... 还有 {len(lines) - max_lines} 行未显示")
    if not lines:
        print("没有读到任何触摸事件。请换一个 --touch_device，或者确认你是在手机真机屏幕上触摸。")


def main():
    args = parse_args()
    device_id = resolve_device_id(args.adb_path, args.device_id)
    print(f"ADB: {args.adb_path}")
    print(f"设备: {device_id}")
    candidates = print_candidates(args.adb_path, device_id)

    touch_device = args.touch_device
    if not touch_device and len(candidates) == 1:
        touch_device = candidates[0]["path"]
        print(f"自动选择唯一候选触摸设备: {touch_device}")
    elif not touch_device:
        print("未指定 --touch_device，将监听所有输入设备。")

    probe_events(args.adb_path, device_id, touch_device, args.seconds, args.max_lines)


if __name__ == "__main__":
    main()
