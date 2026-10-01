# -*- coding: utf-8 -*-
"""「执行次数 = 0」在流程与主界面上的两种语义（2026-09-22）。

现场反馈：「键盘连按的按下间隔好像没有效果」。

实测根因：主界面连按器默认「执行次数 0（界面写的就是 0 = 无限）」，而共用的
`run_press_step` 里有一条**为流程写的**防呆——`count<=0 且 duration<=0` 时按 1 次执行
（避免流程被"无限"步骤卡死）。结果用户在连按器上启动后**只按了一次**，
第二次都没发生，间隔自然"没有效果"。鼠标连点器、找图点击任务同一形态。

修法：给三个共用循环加 `zero_count_as_one` 开关——流程侧保持 True（防卡死），
主界面任务侧传 False（0 = 真正无限，连到用户按停止）。
顺带补上「间隔每轮重读」：连按器/连点器运行期间改间隔也能立刻生效
（原来只在启动那刻读一次，用户改完不重启就一直没变化）。
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import tasks
from app.config import ClickerConfig, PresserConfig
from app.tasks import (ClickTask, PressTask, run_click_step, run_find_step,
                       run_press_step)


def _noop(*_a, **_kw):
    return None


def _run_in_thread(fn, seconds: float, stop: threading.Event):
    """在后台线程跑 fn（会自己循环），seconds 后停止；返回线程对象。"""
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(3)
    return t


class TestFlowSideKeepsGuard(unittest.TestCase):
    """流程步骤：0 次/0 时长仍防呆成 1 次（否则「无限」步骤会把流程永远卡住）。"""

    def test_press_zero_count_runs_once(self):
        stamps = []
        with mock.patch.object(tasks.input_actors, "press_combo",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            reason = run_press_step({"keys": "f24", "interval_ms": 20, "count": 0,
                                     "duration_sec": 0}, threading.Event(), _noop)
        self.assertEqual(reason, "已完成 1 次")
        self.assertEqual(len(stamps), 1)

    def test_click_zero_count_runs_once(self):
        stamps = []
        with mock.patch.object(tasks.input_actors, "click",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            reason = run_click_step({"interval_ms": 20, "count": 0, "duration_sec": 0},
                                    threading.Event(), _noop)
        self.assertEqual(reason, "已完成 1 次")
        self.assertEqual(len(stamps), 1)

    def test_find_zero_count_clicks_once(self):
        stamps = []
        with mock.patch.object(tasks.finder, "load_template", return_value=object()), \
                mock.patch.object(tasks, "_wait_hit", return_value=(10, 20, 0.9)), \
                mock.patch.object(tasks.input_actors, "click",
                                  lambda *a, **kw: stamps.append(time.monotonic())):
            reason = run_find_step({"image": "x.png", "interval_ms": 50, "count": 0,
                                    "duration_sec": 0}, threading.Event(), _noop)
        self.assertEqual(reason, "已完成 1 次")
        self.assertEqual(len(stamps), 1)


class TestMainUiZeroMeansUnlimited(unittest.TestCase):
    """主界面连点/连按/找图：0 = 无限（持续到按停止），不再只跑 1 次。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_presser_runs_continuously(self):
        stamps = []
        task = PressTask()
        task.get_config = lambda: PresserConfig(keys="f24", interval_ms=50,
                                                count=0, duration_sec=0.0)
        with mock.patch.object(tasks.input_actors, "press_combo",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            _run_in_thread(task.work, 0.35, task._stop)
        self.assertGreaterEqual(len(stamps), 2, f"只按了 {len(stamps)} 次：0 次被当成 1 次了")

    def test_clicker_runs_continuously(self):
        stamps = []
        task = ClickTask()
        task.get_config = lambda: ClickerConfig(interval_ms=50, count=0,
                                                duration_sec=0.0)
        with mock.patch.object(tasks.input_actors, "click",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            _run_in_thread(task.work, 0.35, task._stop)
        self.assertGreaterEqual(len(stamps), 2)

    def test_find_task_runs_continuously(self):
        stamps = []
        stop = threading.Event()
        with mock.patch.object(tasks.finder, "load_template", return_value=object()), \
                mock.patch.object(tasks, "_wait_hit", return_value=(10, 20, 0.9)), \
                mock.patch.object(tasks.input_actors, "click",
                                  lambda *a, **kw: stamps.append(time.monotonic())):
            _run_in_thread(
                lambda: run_find_step({"image": "x.png", "interval_ms": 50,
                                       "count": 0, "duration_sec": 0},
                                      stop, _noop, zero_count_as_one=False),
                0.3, stop)
        self.assertGreaterEqual(len(stamps), 2)

    def test_explicit_count_still_stops(self):
        """显式设了次数就按次数停（无限只是 0 的语义）。"""
        stamps = []
        task = PressTask()
        task.get_config = lambda: PresserConfig(keys="f24", interval_ms=20,
                                                count=3, duration_sec=0.0)
        with mock.patch.object(tasks.input_actors, "press_combo",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            task.work()
        self.assertEqual(len(stamps), 3)


class TestIntervalRereadAtRuntime(unittest.TestCase):
    """运行期间改间隔要立刻生效（原来只在启动那刻读一次）。"""

    def test_presser_interval_change_takes_effect_mid_run(self):
        live = {"keys": "f24", "interval_ms": 300, "count": 0, "duration_sec": 0}
        stamps = []
        stop = threading.Event()
        with mock.patch.object(tasks.input_actors, "press_combo",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            t = threading.Thread(
                target=run_press_step,
                args=(lambda: dict(live), stop, _noop),
                kwargs={"zero_count_as_one": False}, daemon=True)
            t.start()
            time.sleep(0.75)
            live["interval_ms"] = 50          # 运行中改小
            time.sleep(0.4)
            stop.set()
            t.join(3)
        gaps = [round(b - a, 3) for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(gaps, f"没有产生连按：{stamps}")
        self.assertGreaterEqual(max(gaps), 0.25, f"改之前应约 300ms：{gaps}")
        self.assertLessEqual(min(gaps), 0.10, f"改之后应约 50ms：{gaps}")

    def test_callable_params_also_works_for_click(self):
        live = {"interval_ms": 20, "count": 0, "duration_sec": 0}
        stamps = []
        stop = threading.Event()
        with mock.patch.object(tasks.input_actors, "click",
                               lambda *a, **kw: stamps.append(time.monotonic())):
            t = threading.Thread(target=run_click_step,
                                 args=(lambda: dict(live), stop, _noop),
                                 kwargs={"zero_count_as_one": False}, daemon=True)
            t.start()
            time.sleep(0.3)
            stop.set()
            t.join(3)
        self.assertGreaterEqual(len(stamps), 2)


if __name__ == "__main__":
    unittest.main()
