# -*- coding: utf-8 -*-
"""SdkBridge: 同步桥接层

game_flow 等同步代码通过它调用异步 SDK:
后台线程跑独立 asyncio 事件循环, 主线程用 run_coroutine_threadsafe 提交协程并等结果。
"""

import asyncio
import threading

from agent.sdk.sdk import Sdk
from utils.logger import logger


class SdkBridge:
    def __init__(self, host="localhost", port=5555, scan_if_fail=True,
                 timeout=180, load=True):
        self._timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._sdk = Sdk()
        if load:
            self.call(self._sdk.load(port, host=host, scan_if_fail=scan_if_fail),
                      timeout=timeout)
            logger.info("[SDK] 桥接层就绪(连接成功)")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro, timeout=None):
        """提交协程并同步等待结果"""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout or self._timeout)

    def stop(self):
        try:
            self.call(self._sdk.close(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def sdk(self):
        return self._sdk

    # ---------- 同步便捷方法(供 game_flow 使用) ----------

    def get_game_state(self):
        return self.call(self._sdk.get_game_state())

    def get_basic_game_info(self):
        return self.call(self._sdk.get_basic_game_info())

    def get_store(self):
        return self.call(self._sdk.get_store())

    def buy_chess(self, index):
        return self.call(self._sdk.buy_chess(index))

    def buy_xp(self):
        return self.call(self._sdk.buy_xp())
