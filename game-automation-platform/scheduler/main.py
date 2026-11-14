# -*- coding: utf-8 -*-
# ---- 统一调度器主循环 ----
# 循环流程(架构版):
#   1) detectors.recognizer.detect_screen_scene() 截"同一片雷电屏幕"并归到 Scene
#   2) state: 由 Scene 算出应前台运行的游戏 desired
#   3) launcher: 与 ProcessSlot 比较, 不同则 停旧 -> 启新(串行)
#
# NOTE: 无三引号 docstring, 用注释, 便于原样文本落盘。
import time

import config
from detectors import recognizer
from launcher import ProcessSlot
from state import SchedulerState


def main():
    slot = ProcessSlot()
    st = SchedulerState()
    print("== 统一调度器 started | DRY_RUN=%s | poll=%.1fs ==" %
          (config.DRY_RUN, config.POLL_SECONDS))
    try:
        while True:
            scene = recognizer.detect_screen_scene()
            desired = st.desired_from(scene)
            print("[sched] scene=%-12s desired=%-12s curr=%s" %
                  (scene.name, str(desired), slot.current))

            if desired is not None:
                slot.switch_to(desired)   # 相同则内部跳过
            else:
                # 无任何游戏需要前台: 若当前仍在跑且画面已不属于该游戏, 可停掉。
                if slot.current is not None:
                    slot.switch_to(None)
            time.sleep(config.POLL_SECONDS)
    except KeyboardInterrupt:
        print("\n用户中断, 清理子进程")
    finally:
        try:
            slot.stop()
        except Exception as e:
            print(e)


if __name__ == "__main__":
    main()
