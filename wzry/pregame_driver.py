import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from ppocronnx import TextSystem

from onnxRunner import OnnxRunner
from runtime_paths import resolve_adb
from navigation_rules import takeover_reason, logged_in_start_button, activity_close_button


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ADB = SCRIPT_DIR / "scrcpy-win64-v2.0" / "adb"
DEFAULT_START_MODEL = SCRIPT_DIR / "models" / "start.onnx"
PACKAGE = "com.tencent.tmgp.sgame"
ACTIVITY = ".SGameActivity"


class PregameDriver:
    def __init__(self, args):
        self.args = args
        self.adb = Path(resolve_adb(args.adb))
        self.device = args.device_id
        self.ocr = TextSystem()
        self.start_check = OnnxRunner(str(args.start_model), classes=["started"])
        self.stop_file = Path(args.stop_file) if args.stop_file else None
        self.live_command_file = (
            Path(args.live_command_file) if args.live_command_file else None
        )
        self.live_command_offset = 0
        self.started_at = time.monotonic()
        self.last_action_at = 0.0
        self.last_action = ""
        self.no_action_count = 0

    def _run(self, argv, timeout=8.0):
        return subprocess.run(
            [str(self.adb), "-s", self.device, *argv],
            capture_output=True,
            timeout=timeout,
        )

    def screenshot(self):
        result = self._run(["exec-out", "screencap", "-p"], timeout=10.0)
        if result.returncode != 0 or not result.stdout:
            return None
        return cv2.imdecode(
            np.frombuffer(result.stdout, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )

    def tap(self, x, y, reason, settle_seconds=0.8):
        now = time.monotonic()
        if self.last_action == reason and now - self.last_action_at < 2.0:
            return
        self._run(
            ["shell", "input", "tap", str(int(x)), str(int(y))],
            timeout=5.0,
        )
        self.last_action = reason
        self.last_action_at = now
        print("[pregame] tap %s @(%d,%d)" % (reason, int(x), int(y)), flush=True)
        time.sleep(settle_seconds)

    def back(self, reason):
        now = time.monotonic()
        if self.last_action == reason and now - self.last_action_at < 2.0:
            return
        self._run(["shell", "input", "keyevent", "4"], timeout=5.0)
        self.last_action = reason
        self.last_action_at = now
        print("[pregame] back %s" % reason, flush=True)
        time.sleep(0.8)

    def launch_game(self):
        component = "{}/{}".format(PACKAGE, ACTIVITY)
        self._run(
            ["shell", "am", "start", "-n", component],
            timeout=10.0,
        )

    def stop_requested(self):
        if self.stop_file is not None and self.stop_file.exists():
            return "stop_file"
        if self.live_command_file is None:
            return None
        try:
            text = self.live_command_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        if len(text) < self.live_command_offset:
            self.live_command_offset = 0
        chunk = text[self.live_command_offset :]
        self.live_command_offset = len(text)
        for line in chunk.splitlines():
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            command = str(payload.get("text") or "")
            if command.strip().lower().startswith("/stop"):
                return command.strip()
        return None

    @staticmethod
    def _box_center(box):
        points = np.array(box, dtype=float).reshape(-1, 2)
        return points[:, 0].mean(), points[:, 1].mean()

    @staticmethod
    def _find(results, *keywords):
        for item in results or []:
            text = str(getattr(item, "ocr_text", "")).strip()
            if all(keyword in text for keyword in keywords):
                return item
        return None

    @staticmethod
    def _find_any(results, keywords):
        for item in results or []:
            text = str(getattr(item, "ocr_text", "")).strip()
            for keyword in keywords:
                if keyword in text:
                    return item
        return None

    def _ocr_roi(self, image, x0, y0, x1, y1):
        """OCR a small screen region and translate boxes back to screen coordinates."""
        height, width = image.shape[:2]
        left = max(0, min(width, int(x0)))
        top = max(0, min(height, int(y0)))
        right = max(left, min(width, int(x1)))
        bottom = max(top, min(height, int(y1)))
        if right <= left or bottom <= top:
            return []
        try:
            results = self.ocr.detect_and_ocr(image[top:bottom, left:right])
        except Exception:
            return []
        normalized = []
        for item in results or []:
            box = getattr(item, "box", None)
            if box is None:
                continue
            shifted = np.array(box, dtype=float).reshape(-1, 2)
            shifted[:, 0] += left
            shifted[:, 1] += top
            normalized.append(
                SimpleNamespace(
                    ocr_text=str(getattr(item, "ocr_text", "")),
                    box=shifted.tolist(),
                )
            )
        return normalized

    def dismiss_fast_activity_page(self, image, results=None):
        """Reuse checked OCR results to close a recognized activity corner button."""
        height, width = image.shape[:2]
        if results is None:
            results = self.ocr.detect_and_ocr(image)
        if takeover_reason(results, height):
            return False
        close_node = activity_close_button(results, width, height)
        if close_node is not None:
            x, y = self._box_center(close_node.box)
            self.tap(x, y, "fast_close_popup", settle_seconds=0.35)
            return True

        return False

    def navigate(self, image, results, mode):
        start_button = logged_in_start_button(results)
        if start_button is not None:
            x, y = self._box_center(start_button.box)
            self.tap(x, y, "start_logged_in_game")
            return True
        if self._find(results, "VS"):
            return True

        close_node = self._find_any(results, ("关闭", "跳过", "取消", "稍后"))
        if close_node is not None and self._find_any(
            results,
            ("公告", "活动", "更新", "下载", "提示", "权限"),
        ):
            x, y = self._box_center(close_node.box)
            self.tap(x, y, "close_popup", settle_seconds=0.35)
            return True

        click_result = (
            self._find(results, "点击此处继续")
            or self._find(results, "点击屏幕继续")
            or self._find(results, "点击继续")
        )
        if click_result is not None:
            x, y = self._box_center(click_result.box)
            self.tap(x, y, "settlement_continue")
            return True

        lobby_back = self._find(results, "返回大厅")
        if lobby_back is not None:
            x, y = self._box_center(lobby_back.box)
            self.tap(x, y, "return_lobby")
            return True

        play_again = self._find(results, "再来一局")
        if play_again is not None:
            x, y = self._box_center(play_again.box)
            self.tap(x, y, "play_again")
            return True

        if self._find(results, "分路表现") or self._find(results, "团队贡献"):
            self.tap(image.shape[1] // 2, int(image.shape[0] * 0.92), "settlement")
            return True

        if self._find(results, "取消") and self._find(results, "确定"):
            cancel = self._find(results, "取消")
            x, y = self._box_center(cancel.box)
            self.tap(x, y, "cancel_dialog")
            return True

        if self._find(results, "单人练习场"):
            self.tap(
                image.shape[1] * 0.753,
                image.shape[0] * 0.50,
                "solo_practice",
            )
            return True

        if self._find(results, "单人模式"):
            self.tap(
                image.shape[1] * 0.503,
                image.shape[0] * 0.50,
                "solo_mode",
            )
            return True

        if self._find(results, "英雄练习场"):
            self.tap(
                image.shape[1] * 0.753,
                image.shape[0] * 0.50,
                "hero_practice",
            )
            return True

        if self._find(results, "下一步"):
            item = self._find(results, "下一步")
            x, y = self._box_center(item.box)
            self.tap(x, y, "next")
            return True

        if self._find(results, "进入游戏"):
            item = self._find(results, "进入游戏")
            x, y = self._box_center(item.box)
            self.tap(x, y, "enter_match")
            return True

        lobby = self._find(results, "对战")
        if lobby is not None and (
            self._find(results, "排位")
            or self._find(results, "玩法大厅")
            or self._find(results, "英雄")
        ):
            x, y = self._box_center(lobby.box)
            self.tap(x, y, "battle_tab")
            return True

        if any(
            str(getattr(item, "ocr_text", "")).strip().endswith("%")
            for item in (results or [])
        ):
            return True

        if mode in {"postgame", "recovery"}:
            self.back("postgame_recovery")
            return True
        return False

    def run(self):
        if not self.args.skip_launch_game:
            self.launch_game()
        time.sleep(3)
        mode = self.args.mode
        deadline = time.monotonic() + max(30.0, float(self.args.timeout))
        while time.monotonic() < deadline:
            stop_reason = self.stop_requested()
            if stop_reason:
                print("[pregame] stopped: %s" % stop_reason, flush=True)
                return 3

            image = self.screenshot()
            if image is None:
                time.sleep(1.0)
                continue

            try:
                label = self.start_check.get_max_label(image)
            except Exception:
                label = None
            if label == "started":
                print("[pregame] match started", flush=True)
                return 0

            try:
                results = self.ocr.detect_and_ocr(image)
            except Exception:
                results = []

            takeover = takeover_reason(results, image.shape[0])
            if takeover:
                print(
                    "[pregame] manual_takeover_required: %s"
                    % takeover,
                    flush=True,
                )
                return 2

            if self.dismiss_fast_activity_page(image, results):
                self.no_action_count = 0
                continue

            acted = self.navigate(image, results, mode)
            if acted:
                self.no_action_count = 0
            else:
                self.no_action_count += 1
                if self.no_action_count >= 5:
                    self.back("unrecognized")
                    self.no_action_count = 0
            time.sleep(0.35 if acted else 1.2)

        print("[pregame] timeout", flush=True)
        return 4


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="King of Glory pre-game navigator")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--adb", default=None)
    parser.add_argument("--skip-launch-game", action="store_true")
    parser.add_argument("--start-model", default=str(DEFAULT_START_MODEL))
    parser.add_argument("--live-command-file", default=None)
    parser.add_argument("--stop-file", default=None)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--mode", choices=("first", "postgame", "recovery"), default="first")
    parser.add_argument("--goal", default="")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.goal:
        return PregameDriver(args).run()
    goal = args.goal
    if "结算" in goal or "局后" in goal:
        args.mode = "postgame"
    elif "未知" in goal or "恢复" in goal:
        args.mode = "recovery"
    return PregameDriver(args).run()


if __name__ == "__main__":
    sys.exit(main())
