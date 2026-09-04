import time
import threading
import pyautogui
from config import ButtonImages, PAGE_LOAD_DELAY, MATCH_FOUND_DELAY, ScreenCoords
from agent.image_recognition import find_image, click_image as img_click, find_question_mark_superfast
from agent.mouse_controller import click
from agent.phase_detector import (
    detect_round, is_monster_round, is_pvp_round, is_carousel_round,
    detect_countdown, countdown_seconds,
    detect_prepare_phase, detect_battle_phase,
    wait_for_prepare_phase, wait_for_battle_phase,
    detect_question_mark
)
from utils.logger import logger


_question_mark_watcher_running = [False]
_question_mark_watcher_thread = None


def _start_q_watcher():
    if _question_mark_watcher_running[0]:
        return
    _question_mark_watcher_running[0] = True
    def _watch():
        while _question_mark_watcher_running[0]:
            try:
                pos = find_question_mark_superfast()
                if pos:
                    print("  [Q-WATCH] Found ? at %s, clicking" % str(pos))
                    click(pos[0], pos[1], delay=0.2)
                    time.sleep(1.0)
                else:
                    time.sleep(0.5)
            except:
                time.sleep(0.5)
    global _question_mark_watcher_thread
    _question_mark_watcher_thread = threading.Thread(target=_watch, daemon=True)
    _question_mark_watcher_thread.start()
    print("[Q-WATCH] ? detection daemon started")


def _stop_q_watcher():
    _question_mark_watcher_running[0] = False
    print("[Q-WATCH] ? detection daemon stopped")


def _scale_coords(x, y):
    sw, sh = pyautogui.size()
    return (int(x * sw / 1920), int(y * sh / 1080))


class GameFlow:
    def __init__(self):
        self._shop_slots = None
        self._buy_exp_btn = None
        self._bench_slots = None
        self._battle_slots = None
        self._current_round = None

    def _init_coords(self):
        if self._shop_slots is None:
            self._shop_slots = [_scale_coords(x, y) for x, y in ScreenCoords.SHOP_SLOTS]
            self._buy_exp_btn = _scale_coords(*ScreenCoords.BUY_EXP_BTN)
            self._bench_slots = [_scale_coords(x, y) for x, y in ScreenCoords.BENCH_SLOTS]
            self._battle_slots = [_scale_coords(x, y) for x, y in ScreenCoords.BATTLE_SLOTS]
            # 最右侧商店位置
            self._rightmost_shop = _scale_coords(*ScreenCoords.SHOP_SLOTS[-1])
            # 拖动起点（靠右5cm approx 190px）
            self._drag_start = (self._bench_slots[0][0] + 190, self._bench_slots[0][1] - 90)
            # 丢弃终点（从起点向下4cm approx 150px）
            self._discard_end = (self._drag_start[0], self._drag_start[1] + 150)
            # 界面中部
            sw, sh = pyautogui.size()
            self._screen_center = (sw // 2, sh // 2)

    def enter_game(self):
        print("")
        print("=" * 50)
        print("   Entering Game")
        print("=" * 50)

        print("[Step1] Click START_GAME_1...")
        if img_click(ButtonImages.START_GAME_1, timeout=10, confidence=0.6):
            print("  -> Clicked START_GAME_1")
            time.sleep(PAGE_LOAD_DELAY)
        else:
            print("  -> START_GAME_1 not found")

        print("[Step2] Select ranked mode...")
        if img_click(ButtonImages.RANKED_TAB, timeout=5):
            print("  -> Clicked RANKED_TAB")
            time.sleep(PAGE_LOAD_DELAY)
        else:
            print("  -> RANKED_TAB not found")

        if img_click(ButtonImages.START_GAME_2, timeout=8):
            print("  -> Clicked START_GAME_2")
            time.sleep(PAGE_LOAD_DELAY)
        else:
            print("  -> START_GAME_2 not found")

        print("[Step3] Enter match queue...")
        if img_click(ButtonImages.START_GAME_3, timeout=10):
            print("  -> In match queue")
        else:
            print("  -> START_GAME_3 not found")

        print("[Step4] Wait for accept...")
        if img_click(ButtonImages.ACCEPT_MATCH, timeout=15):
            print("  -> Match accepted!")
            time.sleep(MATCH_FOUND_DELAY)
        else:
            print("  -> ACCEPT_MATCH not found")

        print("")
        print("=" * 50)
        print("   In Game")
        print("=" * 50)

    def _buy_heroes_rightmost_only(self, time_limit):
        end_t = time.time() + time_limit
        count = 0
        pos = self._rightmost_shop
        print("  Buy heroes (rightmost only): click 1 per sec")
        while time.time() < end_t:
            click(pos[0], pos[1], delay=0.2)
            count += 1
            time.sleep(0.8)
        print("  Buy heroes done, clicked %d times" % count)

    def _buy_exp_five_times(self, time_limit):
        end_t = time.time() + time_limit
        count = 0
        print("  Buy exp: try both images, click 5 times")
        for attempt in range(5):
            if time.time() >= end_t:
                print("  Buy exp timeout at attempt %d" % attempt)
                break
            pos = find_image(ButtonImages.BUY_EXPERIENCE, confidence=0.45)
            if not pos:
                pos = find_image(ButtonImages.BUY_EXPERIENCE_2, confidence=0.45)
            if pos:
                click(pos[0], pos[1], delay=0.3)
                count += 1
                time.sleep(0.5)
            else:
                time.sleep(0.3)
        print("  Buy exp done, clicked %d times" % count)

    def _deploy_heroes(self, time_limit):
        end_t = time.time() + time_limit
        print("  Deploy heroes: drag from %s to battle" % str(self._drag_start))
        if self._battle_slots:
            battle_pos = self._battle_slots[0]
            pyautogui.moveTo(self._drag_start[0], self._drag_start[1])
            pyautogui.dragTo(battle_pos[0], battle_pos[1] - 90, duration=0.5)
            print("  Deploy done")
        else:
            print("  No battle slots, skip deploy")

    def _discard_hero(self, time_limit):
        end_t = time.time() + time_limit
        print("  Discard hero: drag from %s down to %s, hold 2s" % (
            str(self._drag_start), str(self._discard_end)))
        pyautogui.moveTo(self._drag_start[0], self._drag_start[1])
        pyautogui.dragTo(self._discard_end[0], self._discard_end[1], duration=0.5, button="left")
        print("  Holding mouse for 2s...")
        time.sleep(min(2.0, end_t - time.time()))
        pyautogui.mouseUp()
        print("  Discard done")

    def _carousel_phase(self, time_limit):
        end_t = time.time() + time_limit
        count = 0
        sw, sh = pyautogui.size()
        cx, cy = sw // 2, sh // 2
        print("  Carousel phase: click center every 1s for %ds" % time_limit)
        while time.time() < end_t:
            click(cx, cy, delay=0.2)
            count += 1
            time.sleep(0.8)
        print("  Carousel phase done, clicked %d times" % count)

    def _prepare_phase_flow(self):
        self._init_coords()
        print("")
        print("--- Prepare Phase Flow ---")

        # 尝试OCR识别准备阶段（5s超时）
        print("1) Try OCR: wait for prepare phase sign...")
        ocr_ok = wait_for_prepare_phase(timeout=5)

        if ocr_ok:
            # OCR成功：读倒计时
            print("2) OCR success, read countdown...")
            cd_str = detect_countdown()
            if not cd_str:
                print("   No countdown, using default 30s")
                total_seconds = 30
            else:
                total_seconds = countdown_seconds(cd_str)
                if total_seconds is None:
                    total_seconds = 30
            print("   Countdown: %s (%d seconds)" % (cd_str, total_seconds))
        else:
            # OCR失败，降级到固定30s周期
            print("2) OCR failed, fallback to fixed 30s cycle")
            total_seconds = 30

        phase_end = time.time() + total_seconds

        # 2a) Buy heroes: rightmost only, hard limit 5s
        hero_time = min(5.0, max(0, phase_end - time.time()))
        if hero_time > 0:
            self._buy_heroes_rightmost_only(hero_time)

        # 2b) Buy exp: click 5 times, hard limit 10s
        exp_deadline = min(time.time() + 10.0, phase_end)
        exp_time = min(10.0, max(0, exp_deadline - time.time()))
        if exp_time > 0:
            self._buy_exp_five_times(exp_time)

        # 2c) Deploy heroes: hard limit 5s
        deploy_deadline = min(time.time() + 5.0, phase_end)
        deploy_time = min(5.0, max(0, deploy_deadline - time.time()))
        if deploy_time > 0:
            self._deploy_heroes(deploy_time)

        # 2d) Discard hero: hard limit 7s
        discard_deadline = min(time.time() + 7.0, phase_end)
        discard_time = min(7.0, max(0, discard_deadline - time.time()))
        if discard_time > 0:
            self._discard_hero(discard_time)

        # Wait for remaining time (if any)
        remaining = phase_end - time.time()
        if remaining > 0:
            print("  Prepare phase remaining: %.1fs, waiting" % remaining)
            time.sleep(remaining)

        print("--- Prepare Phase Complete ---")

    def run(self):
        print("")
        print("=" * 50)
        print("   JCC Auto Script v2")
        print("")
        print("   Features:")
        print("   - ? detection daemon (always on)")
        print("   - OCR round/countdown/phase detection")
        print("   - Prepare phase: buy > exp > deploy > discard")
        print("   - Carousel round (x-4): click center 20s")
        print("=" * 50)
        print("")

        try:
            self.enter_game()

            print("")
            print("Waiting for game_start.png to confirm in-game...")
            found = img_click(ButtonImages.GAME_START, timeout=60, confidence=0.7)
            if not found:
                print("  game_start not found, proceeding anyway")
            print("  In-game confirmed")

            # Start ? detection daemon
            _start_q_watcher()

            round_count = 0
            while True:
                round_count += 1

                print("")
                print("=" * 50)
                print("   Round %d" % round_count)
                print("=" * 50)

                # Detect current round number
                current_round = detect_round()
                print("  Detected round: %s" % current_round)

                if is_carousel_round(current_round):
                    print("  -> Carousel round! Clicking center for 20s")
                    self._carousel_phase(20)
                    print("  Carousel round done, wait for prepare phase")
                    time.sleep(3.0)
                else:
                    print("  -> Normal round, waiting for prepare phase")
                    time.sleep(2.0)

                # Prepare phase flow
                self._prepare_phase_flow()

                # Wait for battle phase
                print("  Waiting for battle phase...")
                if wait_for_battle_phase(timeout=35):
                    print("  Battle phase detected")
                else:
                    print("  Battle phase not detected, continuing")

                # Wait a bit before next cycle
                time.sleep(1.0)

        except (KeyboardInterrupt, SystemExit):
            print("")
            print("User interrupted")
        except Exception as e:
            print("")
            print("Runtime error: %s" % e)
            import traceback
            traceback.print_exc()
        finally:
            _stop_q_watcher()
            print("Script terminated")