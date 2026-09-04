import time
import pyautogui
from config import ButtonImages, ScreenCoords
from agent.image_recognition import find_image, click_image as img_click
from agent.mouse_controller import click


def _scale_coords(x, y):
    sw, sh = pyautogui.size()
    return (int(x * sw / 1920), int(y * sh / 1080))


def test_settlement():
    """测试10轮之后的结算流程：点击齿轮 -> 依次识别图片并点击"""
    print("")
    print("=" * 50)
    print("   Settlement Test")
    print("=" * 50)
    print("")

    # Step 1: 点击右上角齿轮（固定坐标）
    print("[Step 1] Click gear at top-right corner...")
    sw, sh = pyautogui.size()
    gear_x = int(2440 * sw / 2560)
    gear_y = int(114 * sh / 1600)
    print("  Screen: %dx%d, gear click at (%d, %d)" % (sw, sh, gear_x, gear_y))
    click(gear_x, gear_y, delay=0.5)
    time.sleep(1.0)

    # Step 2: stop_game.png
    print("[Step 2] Click stop_game...")
    if img_click(ButtonImages.STOP_GAME, timeout=15):
        print("  -> stop_game clicked")
        time.sleep(1.0)
    else:
        print("  -> stop_game not found, continuing")

    # Step 3: confirm.png
    print("[Step 3] Click confirm...")
    if img_click(ButtonImages.CONFIRM, timeout=15):
        print("  -> confirm clicked")
        time.sleep(1.0)
    else:
        print("  -> confirm not found, continuing")

    # Step 4: stop_now.png
    print("[Step 4] Click stop_now...")
    if img_click(ButtonImages.STOP_NOW, timeout=15):
        print("  -> stop_now clicked")
        time.sleep(2.0)
    else:
        print("  -> stop_now not found, continuing")

    # Step 5: next_period_1.png
    print("[Step 5] Click next_period_1...")
    if img_click(ButtonImages.NEXT_PERIOD_1, timeout=15):
        print("  -> next_period_1 clicked")
        time.sleep(1.0)
    else:
        print("  -> next_period_1 not found, continuing")

    # Step 6: next_period_2.png
    print("[Step 6] Click next_period_2...")
    if img_click(ButtonImages.NEXT_PERIOD_2, timeout=15):
        print("  -> next_period_2 clicked")
        time.sleep(1.0)
    else:
        print("  -> next_period_2 not found, continuing")

    # Step 7: game_again.png
    print("[Step 7] Click game_again...")
    if img_click(ButtonImages.GAME_AGAIN, timeout=15):
        print("  -> game_again clicked")
        time.sleep(3.0)
    else:
        print("  -> game_again not found, continuing")

    # Wait for game_start
    print("")
    print("Waiting for game_start.png...")
    found = img_click(ButtonImages.GAME_START, timeout=60, confidence=0.7)
    if found:
        print("  -> game_start detected")
    else:
        print("  -> game_start not found")

    print("")
    print("=" * 50)
    print("   Settlement test complete")
    print("=" * 50)


if __name__ == "__main__":
    try:
        test_settlement()
    except KeyboardInterrupt:
        print("")
        print("User interrupted")
    except Exception as e:
        print("")
        print("Error: %s" % e)
        import traceback
        traceback.print_exc()
