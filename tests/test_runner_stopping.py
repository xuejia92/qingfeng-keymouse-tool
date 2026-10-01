# -*- coding: utf-8 -*-
"""FlowRunner.stopping：区分「还在跑」与「已请求停止、线程在收尾」。

频繁启停场景靠它：用户按下停止后，线程要几十毫秒才真正结束（is_running 仍为 True），
这期间再按「运行」若被当成「又停一次」就会被吞掉。见 FlowTab.toggle_flow。
"""
from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import Flow, FlowStep
from app.flows import FlowRunner


def _flow(seconds=0.05) -> Flow:
    return Flow(name="流程", steps=[FlowStep(type="wait", params={"seconds": seconds})])


class TestStoppingFlag(unittest.TestCase):
    def test_false_before_start(self):
        self.assertFalse(FlowRunner(_flow()).stopping)

    def test_false_while_running_normally(self):
        r = FlowRunner(_flow())
        r.is_running = True                      # 模拟线程正在跑、没人按停止
        self.assertFalse(r.stopping)

    def test_true_after_stop_request(self):
        r = FlowRunner(_flow())
        r.is_running = True
        r.stop()                                 # 置停止位（线程还在收尾）
        self.assertTrue(r.is_running)            # 线程确实还没结束
        self.assertTrue(r.stopping)

    def test_false_after_thread_finished(self):
        r = FlowRunner(_flow())
        r.is_running = False                     # 线程收尾完成
        r._stop.set()
        self.assertFalse(r.stopping)


class TestStoppingWithRealThread(unittest.TestCase):
    """真线程冒烟：起→停→收尾后 stopping 归 False。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_thread_lifecycle(self):
        r = FlowRunner(_flow(0.3))
        self.assertTrue(r.start())
        self.assertTrue(r.is_running)
        self.assertFalse(r.stopping)
        r.stop()
        self._wait_stopped(r)
        self.assertFalse(r.is_running)
        self.assertFalse(r.stopping)

    def test_restart_same_runner_clears_stop(self):
        """同一个 runner 二次 start 会清掉停止位（stopping 随之复位）。"""
        r = FlowRunner(_flow(0.05))
        r.start()
        r.stop()
        self._wait_stopped(r)
        r.start()                                # 再跑一次
        self.assertTrue(r.is_running)
        self.assertFalse(r.stopping)
        r.stop()
        self._wait_stopped(r)

    @staticmethod
    def _wait_stopped(runner, timeout: float = 5.0) -> None:
        """轮询 is_running（不依赖 Qt 事件循环：stateChanged 是跨线程 emit）。"""
        deadline = time.monotonic() + timeout
        while runner.is_running and time.monotonic() < deadline:
            time.sleep(0.02)
        if runner.is_running:
            raise AssertionError("停止后线程应在几秒内收尾")


if __name__ == "__main__":
    unittest.main()
