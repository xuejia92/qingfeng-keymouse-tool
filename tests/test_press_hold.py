# -*- coding: utf-8 -*-
"""「键盘连按」的「每次按住」时长（2026-09-22）。

用户原话：「键盘连按模块的持续时长没有生效，这个应该是每次按下按键的持续时长，
修复一下，流程列表里面吧描述加上」。

背景：流程里的「键盘连按」步骤原来有个「持续时长」（`duration_sec`），语义是
**整个连按跑多久**（与「按压次数」同为停止条件）。用户按"每次按住多久"去理解它，
自然"没有效果"：次数默认 1 时第一轮就停了，时长再大也没用。

修法：参数改名改语义 → `hold_ms`（每次按下按住多久再松开，默认 50ms），
`duration_sec` 从流程步骤里移除（主界面连按器的停止条件仍用它，互不影响）；
摘要里补上「按住 Nms」。
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import config, input_actors, tasks
from app.config import (PRESS_HOLD_DEFAULT_MS, FlowStep, default_step_params)
from app.tasks import run_press_step


class _FakeKb:
    """假键盘：记录 press/release 的顺序与时刻。"""

    def __init__(self):
        self.events: list[tuple[str, object, float]] = []

    def press(self, key):
        self.events.append(("press", key, time.monotonic()))

    def release(self, key):
        self.events.append(("release", key, time.monotonic()))


class TestPressHoldParam(unittest.TestCase):
    def test_default_constant(self):
        self.assertEqual(PRESS_HOLD_DEFAULT_MS, 50)

    def test_default_params_use_hold_ms(self):
        p = default_step_params("press")
        self.assertEqual(p["hold_ms"], PRESS_HOLD_DEFAULT_MS)
        self.assertNotIn("duration_sec", p)      # 旧的「持续时长」已从步骤参数里移除

    def test_hold_seconds_parsing(self):
        self.assertEqual(tasks._hold_seconds({}), 0.05)              # 缺省 50ms
        self.assertEqual(tasks._hold_seconds({"hold_ms": 0}), 0.0)   # 0 = 按下即松开
        self.assertEqual(tasks._hold_seconds({"hold_ms": 250}), 0.25)
        for bad in ("abc", None, []):
            self.assertEqual(tasks._hold_seconds({"hold_ms": bad}), 0.05, bad)
        self.assertEqual(tasks._hold_seconds({"hold_ms": -5}), 0.0)  # 负数当 0

    def test_summary_shows_hold(self):
        """流程列表描述要带上按住时长（用户要求）。"""
        s = FlowStep(type="press", params={"keys": "space", "interval_ms": 100,
                                           "count": 3}).summary()
        self.assertIn("按住 50ms", s)
        s2 = FlowStep(type="press", params={"keys": "space", "interval_ms": 100,
                                            "count": 3, "hold_ms": 200}).summary()
        self.assertIn("按住 200ms", s2)
        self.assertIn("间隔 100ms", s2)


class TestPressComboHold(unittest.TestCase):
    """input_actors.press_combo 真的按住再松开。"""

    def test_holds_before_release(self):
        kb = _FakeKb()
        with mock.patch.object(input_actors, "_get_keyboard", return_value=kb):
            t0 = time.monotonic()
            input_actors.press_combo("f24", hold_sec=0.15)
            elapsed = time.monotonic() - t0
        self.assertEqual([e[0] for e in kb.events], ["press", "release"])
        self.assertGreaterEqual(elapsed, 0.14)
        self.assertGreaterEqual(kb.events[1][2] - kb.events[0][2], 0.14)   # 按下到松开确实等了

    def test_zero_hold_releases_immediately(self):
        kb = _FakeKb()
        with mock.patch.object(input_actors, "_get_keyboard", return_value=kb):
            t0 = time.monotonic()
            input_actors.press_combo("f24")
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.05)
        self.assertEqual([e[0] for e in kb.events], ["press", "release"])

    def test_stop_aborts_the_hold_and_releases(self):
        """按住期间点「停止」→ 立刻松开，不赖着不放。"""
        kb = _FakeKb()
        stop = threading.Event()
        stop.set()
        with mock.patch.object(input_actors, "_get_keyboard", return_value=kb):
            t0 = time.monotonic()
            input_actors.press_combo("f24", hold_sec=5.0, stop=stop)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertEqual([e[0] for e in kb.events], ["press", "release"])

    def test_modifiers_released_after_hold(self):
        kb = _FakeKb()
        with mock.patch.object(input_actors, "_get_keyboard", return_value=kb):
            input_actors.press_combo("ctrl+c", hold_sec=0.05)
        kinds = [e[0] for e in kb.events]
        self.assertEqual(kinds, ["press", "press", "release", "release"])


class TestRunPressStepUsesHold(unittest.TestCase):
    def test_hold_passed_to_press_combo(self):
        seen: list[tuple] = []

        def rec(keys, hold_sec=0.0, stop=None):
            seen.append((keys, hold_sec))

        with mock.patch.object(tasks.input_actors, "press_combo", rec):
            reason = run_press_step({"keys": "f24", "interval_ms": 20, "count": 2,
                                     "hold_ms": 120}, threading.Event(),
                                    lambda d, e: None)
        self.assertEqual(reason, "已完成 2 次")
        self.assertEqual(seen, [("f24", 0.12), ("f24", 0.12)])

    def test_missing_hold_uses_default(self):
        seen: list[tuple] = []

        def rec(keys, hold_sec=0.0, stop=None):
            seen.append((keys, hold_sec))

        with mock.patch.object(tasks.input_actors, "press_combo", rec):
            run_press_step({"keys": "f24", "interval_ms": 20, "count": 1},
                           threading.Event(), lambda d, e: None)
        self.assertEqual(seen, [("f24", PRESS_HOLD_DEFAULT_MS / 1000.0)])

    def test_hold_reread_each_round(self):
        """运行期改按住时长（连按器场景）：下一轮就用新值。"""
        live = {"keys": "f24", "interval_ms": 20, "count": 3, "hold_ms": 10}
        seen: list[float] = []

        def rec(keys, hold_sec=0.0, stop=None):
            seen.append(hold_sec)
            if len(seen) == 1:
                live["hold_ms"] = 300          # 第一轮之后改

        with mock.patch.object(tasks.input_actors, "press_combo", rec):
            run_press_step(lambda: dict(live), threading.Event(),
                           lambda d, e: None, zero_count_as_one=False)
        self.assertEqual(seen[0], 0.01)
        self.assertGreaterEqual(seen[-1], 0.3)


class TestPressDialogHold(unittest.TestCase):
    """步骤对话框：控件回填 / 写回（旧的 duration_sec 不再出现在参数里）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_hold_filled_and_applied(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="press", params={"keys": "space", "interval_ms": 100,
                                              "count": 2, "hold_ms": 300})
        dlg = StepParamsDialog(step)
        self.assertEqual(dlg.hold.value(), 300)
        target = FlowStep(type="press")
        dlg.apply_to(target)
        self.assertEqual(target.params["hold_ms"], 300)
        self.assertNotIn("duration_sec", target.params)

    def test_default_hold_from_config(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="press"))
        self.assertEqual(dlg.hold.value(), PRESS_HOLD_DEFAULT_MS)

    def test_legacy_duration_ignored(self):
        """老流程里的 duration_sec（旧语义）不再回填，也不会被写回。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="press", params={"keys": "space", "interval_ms": 100,
                                              "count": 2, "duration_sec": 30.0})
        dlg = StepParamsDialog(step)
        self.assertEqual(dlg.hold.value(), PRESS_HOLD_DEFAULT_MS)
        target = FlowStep(type="press")
        dlg.apply_to(target)
        self.assertEqual(target.params["hold_ms"], PRESS_HOLD_DEFAULT_MS)


if __name__ == "__main__":
    unittest.main()
