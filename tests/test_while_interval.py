# -*- coding: utf-8 -*-
"""while 循环「每轮迭代之间的等待」测试（2026-09-22 新增）。

需求原话（第一轮）：「while循环模块，添加内部运行默认延时等待一秒」
需求原话（第二轮）：「while循环迭代间隔默认0.1秒，流程列表描述里要显示迭代时间」

约定：
- 每轮循环体执行完、endWhile 重新求值条件仍成立时，先等「迭代间隔」再开始下一轮；
- 间隔默认 0.1 秒（`config.WHILE_ITER_INTERVAL_DEFAULT_SEC`），步骤参数 interval_sec 可改，
  0 = 不等待；非法值回退默认、负数按 0、上限 3600 秒；
- 等待必须能被「停止」立刻打断（用 `FlowRunner._stop.wait`，不是 time.sleep）；
- 第一轮进入前不等待；条件一开始就不成立时整块跳过、一次都不等；
- 摘要**恒定显示**间隔（流程列表里要能直接看到迭代节奏）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import config
from app.config import (Flow, FlowStep, FlowVariable, default_step_params,
                        while_iter_interval)
from app.flows import FlowRunner, FlowVariableStore

DEFAULT = 0.1      # 与 config.WHILE_ITER_INTERVAL_DEFAULT_SEC 对应（另有测试钉住这个数）


def _bump():
    """循环体内把计数器 n 加一。"""
    return FlowStep(type="py_func", params={
        "code": "def bump(n):\n    return n + 1",
        "func_name": "bump", "variables": ["n"], "result_var": "n"})


def _while(condition="n < 3", interval=None):
    params = {"condition": condition}
    if interval is not None:
        params["interval_sec"] = interval
    return FlowStep(type="while", params=params)


class _FakeStop:
    """_stop 替身：记录每次等待时长，可模拟「等待期间被停止」。"""

    def __init__(self, stop_after: int | None = None):
        self.waits: list[float] = []
        self._set = False
        self._stop_after = stop_after   # 第 N 次 wait 之后置位（None=永不）

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def clear(self) -> None:
        self._set = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._stop_after is not None and len(self.waits) >= self._stop_after:
            self._set = True
        return self._set


class TestWhileIntervalParam(unittest.TestCase):
    """参数默认值与解析规则。"""

    def test_default_is_one_tenth_second(self):
        """用户明确要求默认 0.1 秒（改过 1 秒 → 0.1 秒），钉住这个数。"""
        self.assertEqual(config.WHILE_ITER_INTERVAL_DEFAULT_SEC, DEFAULT)

    def test_default_step_params_carries_default_interval(self):
        p = default_step_params("while")
        self.assertEqual(p["interval_sec"], DEFAULT)
        self.assertEqual(p["condition"], "")

    def test_if_elseif_have_no_interval(self):
        for t in ("if", "elseif"):
            self.assertNotIn("interval_sec", default_step_params(t))

    def test_step_params_get_default_merged_in(self):
        """FlowStep 会把默认参数并进 params，所以老流程的 while 也自动带默认间隔。"""
        step = FlowStep(type="while", params={"condition": "i<3"})
        self.assertEqual(step.params["interval_sec"], DEFAULT)

    def test_missing_key_falls_back_to_default(self):
        for raw in ({}, None, {"condition": "x"}):
            self.assertEqual(while_iter_interval(raw), DEFAULT)

    def test_explicit_values_pass_through(self):
        self.assertEqual(while_iter_interval({"interval_sec": 0}), 0.0)
        self.assertEqual(while_iter_interval({"interval_sec": "0.5"}), 0.5)
        self.assertEqual(while_iter_interval({"interval_sec": 2}), 2.0)

    def test_bad_values_fall_back_or_clamp(self):
        for bad in ("abc", "", None, [], {}, float("nan")):
            self.assertEqual(while_iter_interval({"interval_sec": bad}), DEFAULT,
                             f"{bad!r} 应回退默认间隔")
        self.assertEqual(while_iter_interval({"interval_sec": -5}), 0.0)
        self.assertEqual(while_iter_interval({"interval_sec": 99999}),
                         config.WHILE_ITER_INTERVAL_MAX_SEC)

    def test_default_constant_is_tunable(self):
        """测试要保持轻快，靠的就是这个常量可被置 0。"""
        with mock.patch.object(config, "WHILE_ITER_INTERVAL_DEFAULT_SEC", 0.0):
            self.assertEqual(while_iter_interval({}), 0.0)


class TestWhileSummary(unittest.TestCase):
    """摘要：迭代间隔恒定显示（用户要在流程列表里看到迭代时间）。"""

    def test_default_interval_shown(self):
        for params in ({"condition": "i<3"}, {"condition": "i<3", "interval_sec": DEFAULT}):
            self.assertEqual(FlowStep(type="while", params=params).summary(),
                             f"while 循环 i<3 · 间隔 {DEFAULT:g} 秒")

    def test_custom_interval_shown(self):
        self.assertEqual(
            FlowStep(type="while", params={"condition": "i<3", "interval_sec": 2.5}).summary(),
            "while 循环 i<3 · 间隔 2.5 秒")
        self.assertEqual(
            FlowStep(type="while", params={"condition": "i<3", "interval_sec": 0}).summary(),
            "while 循环 i<3 · 间隔 0 秒")

    def test_dirty_interval_falls_back_in_summary(self):
        self.assertEqual(
            FlowStep(type="while", params={"condition": "i<3", "interval_sec": "abc"}).summary(),
            f"while 循环 i<3 · 间隔 {DEFAULT:g} 秒")

    def test_condition_still_truncated(self):
        long_cond = "a" * 40
        s = FlowStep(type="while", params={"condition": long_cond}).summary()
        self.assertTrue(s.startswith("while 循环 " + "a" * 27 + "…"), s)
        self.assertTrue(s.endswith(" 秒"), s)


class TestWhileIntervalAtRuntime(unittest.TestCase):
    """真跑 FlowRunner._run_once，检查等待发生在「轮与轮之间」。"""

    def _run(self, condition="n < 3", interval=None, stop_after=None):
        flow = Flow(name="f",
                    variables=[FlowVariable(name="n", type="integer", default_value="0")],
                    steps=[_while(condition, interval), _bump(),
                           FlowStep(type="endWhile")])
        runner = FlowRunner(flow)
        runner.vars = FlowVariableStore(flow)
        fake = _FakeStop(stop_after=stop_after)
        runner._stop = fake
        reason = runner._run_once()
        return runner, reason, fake

    def test_waits_default_interval_between_iterations(self):
        runner, reason, fake = self._run()
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)
        self.assertEqual(fake.waits, [DEFAULT, DEFAULT])   # 3 轮 = 2 次轮间等待

    def test_custom_interval_used(self):
        _, reason, fake = self._run(interval=2.5)
        self.assertIsNone(reason)
        self.assertEqual(fake.waits, [2.5, 2.5])

    def test_zero_interval_skips_waiting(self):
        _, reason, fake = self._run(interval=0)
        self.assertIsNone(reason)
        self.assertEqual(fake.waits, [])

    def test_no_wait_when_condition_false_at_entry(self):
        _, reason, fake = self._run(condition="1 == 2")
        self.assertIsNone(reason)
        self.assertEqual(fake.waits, [])

    def test_single_iteration_waits_nothing(self):
        """只跑一轮（下一轮条件不成立）时不该有轮间等待。"""
        _, reason, fake = self._run(condition="n < 1")
        self.assertIsNone(reason)
        self.assertEqual(fake.waits, [])

    def test_wait_interrupted_by_stop(self):
        _, reason, fake = self._run(interval=30, stop_after=1)
        self.assertEqual(reason, "已手动停止")
        self.assertEqual(fake.waits, [30])            # 只等了一次，没进下一轮

    def test_dead_loop_still_protected(self):
        """等待不改变死循环保护：超上限照样终止。"""
        flow = Flow(name="f", variables=[],
                    steps=[_while("1 == 1", 0),
                           FlowStep(type="log", params={"text": "x"}),
                           FlowStep(type="endWhile")])
        runner = FlowRunner(flow)
        runner.vars = FlowVariableStore(flow)
        runner._stop = _FakeStop()
        with mock.patch("app.flows.MAX_WHILE_ITERATIONS", 5):
            reason = runner._run_once()
        self.assertIn("死循环", reason)


class TestWhileDialogForm(unittest.TestCase):
    """参数对话框里的「迭代间隔」：回填与写回。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dlg(self, **params):
        from app.ui.flow_dialog import StepParamsDialog
        p = {"condition": "i<3"}
        p.update(params)
        return StepParamsDialog(FlowStep(type="while", params=p))

    def test_default_shows_config_default(self):
        dlg = self._dlg()          # 必须留住引用：临时对象被 GC 后 C++ 控件会失效
        self.assertAlmostEqual(dlg.while_interval.value(), DEFAULT)

    def test_fill_from_params(self):
        dlg0 = self._dlg(interval_sec=0.0)
        self.assertAlmostEqual(dlg0.while_interval.value(), 0.0)
        dlg35 = self._dlg(interval_sec=3.5)
        self.assertAlmostEqual(dlg35.while_interval.value(), 3.5)

    def test_fill_tolerates_dirty_value(self):
        dlg = self._dlg()
        dlg._fill(FlowStep(type="while", params={"condition": "x", "interval_sec": "abc"}))
        self.assertAlmostEqual(dlg.while_interval.value(), DEFAULT)

    def test_apply_writes_interval(self):
        dlg = self._dlg()
        dlg.while_interval.setValue(0.5)
        step = FlowStep(type="while", params={"condition": "i<3"})
        dlg.apply_to(step)
        self.assertEqual(step.params["condition"], "i<3")
        self.assertEqual(step.params["interval_sec"], 0.5)

    def test_if_dialog_unaffected(self):
        """if/elseif 不该出现迭代间隔行。"""
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="if", params={"condition": "x"}))
        self.assertFalse(hasattr(dlg, "while_interval"))


if __name__ == "__main__":
    unittest.main()
