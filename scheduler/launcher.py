# -*- coding: utf-8 -*-
# ---- 纯 subprocess 子进程调度层 ----
# 职责: 串行地 启动/停止 某款游戏主程序。
# 不 import 任何游戏本体, 也不修改其代码。
# 子进程 cwd 会切到对应游戏目录; argv 由 config.GAMES[game]["cmd"]() 提供。
#
# NOTE: 本文件内容不含三引号 docstring, 统一用注释, 便于当作纯文本原样落盘。
import os
import subprocess
import sys
import time

from config import GAMES, DRY_RUN


class _FakeProc:
    """DRY_RUN 下的假句柄, 模拟"一直存活", 保持接口一致。"""
    def poll(self):
        return None


class ProcessSlot:
    """串行槽位: 某一时刻只允许一个游戏子进程占据。"""

    def __init__(self):
        self.current = None      # 'wzry' 或 'jinchanchan' 或 None
        self.proc = None         # Popen 句柄(或 _FakeProc)

    @property
    def occupied(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, game):
        if self.occupied:
            raise RuntimeError("串行约束冲突: 已有子进程在跑, 需先 stop()")
        spec = GAMES[game]
        cwd = spec["dir"]
        argv = spec["cmd"]()
        print("[Launcher] start=%s cwd=%s argv=%s" % (game, cwd, argv))
        if DRY_RUN:
            print("[Launcher] DRY_RUN: 仅打印, 不创建真实子进程")
            self.proc = _FakeProc()
        else:
            self.proc = subprocess.Popen([sys.executable] + argv[1:] + [],
                                         cwd=cwd)
        self.current = game
        return self.proc

    def stop(self, wait_seconds=3):
        if self.proc is None:
            return
        print("[Launcher] stop current=%s" % self.current)
        if not DRY_RUN and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=wait_seconds)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.current = None
        self.proc = None

    def switch_to(self, game):
        """停旧启新, 保证串行。返回是否真的发生了切换。"""
        if self.current == game and self.occupied:
            return False
        self.stop()
        if game is not None:
            self.start(game)
        return True


if __name__ == "__main__":
    slot = ProcessSlot()
    slot.start("jinchanchan")
    print("occupied?", slot.occupied, "current=", slot.current)
    slot.switch_to("wzry")
    slot.stop()
    print("selftest done")
