import argparse
import sys
import time


def parse_args():
    parser = argparse.ArgumentParser(description="Quickly test joystick movement execution.")
    parser.add_argument("--angle", type=int, default=180)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--burst_ms", type=int, default=700)
    parser.add_argument("--sleep_ms", type=int, default=80)
    local_args, remaining_args = parser.parse_known_args()
    sys.argv = [sys.argv[0], *remaining_args]
    return local_args


def main():
    local_args = parse_args()
    from android_tool import AndroidTool
    from argparses import args

    tool = AndroidTool()
    original_burst = args.action_burst_duration_ms
    args.action_burst_duration_ms = int(local_args.burst_ms)

    print(
        f"Testing movement: angle={local_args.angle}, seconds={local_args.seconds}, "
        f"burst_ms={args.action_burst_duration_ms}, continuous={args.continuous_move_enabled}, "
        f"mode={args.move_control_mode}, radius={args.move_joystick_radius}"
    )
    deadline = time.time() + max(0.1, local_args.seconds)
    try:
        while time.time() < deadline:
            tool.execute_action_burst([1, local_args.angle % 360, 0, 0, 0, 0, 0, 0])
            time.sleep(max(0.0, local_args.sleep_ms / 1000.0))
    finally:
        args.action_burst_duration_ms = original_burst
        tool.stop()


if __name__ == "__main__":
    main()
