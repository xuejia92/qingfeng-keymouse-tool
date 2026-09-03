# -*- coding: utf-8 -*-
"""while 循环步骤的端到端执行测试（复用 FlowRunner._run_once）。

覆盖：条件成立反复执行直到不成立、进入前不成立则跳过整块、死循环保护、
循环体内修改变量、条件引用未定义变量报错、空条件报错。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import Flow, FlowStep, FlowVariable
from app.flows import FlowRunner, FlowVariableStore


def _run(flow):
    runner = FlowRunner(flow)
    runner.vars = FlowVariableStore(flow)
    reason = runner._run_once()
    return runner, reason


def _while(condition):
    return FlowStep(type="while", params={"condition": condition})


def _end():
    return FlowStep(type="endWhile")


def _bump():
    """循环体内把计数器 n 加一。"""
    return FlowStep(type="py_func", params={
        "code": "def bump(n):\n    return n + 1",
        "func_name": "bump", "variables": ["n"], "result_var": "n"})


class TestWhileExecution(unittest.TestCase):
    def _flow(self, condition, body_steps, variables=None):
        variables = variables or [FlowVariable(name="n", type="integer",
                                               default_value="0")]
        return Flow(name="f", variables=variables,
                    steps=[_while(condition)] + body_steps + [_end()])

    def test_loops_until_false(self):
        flow = self._flow("n < 3", [_bump()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)

    def test_false_condition_skips(self):
        flow = self._flow("1 == 2", [_bump()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 0)   # 循环体未执行

    def test_dead_loop_protection(self):
        """条件恒真 + 空转体（log 成功但不修改变量）→ 超限报错。"""
        body = [FlowStep(type="log", params={"text": "x"})]
        flow = self._flow("1 == 1", body)
        with mock.patch("app.flows.MAX_WHILE_ITERATIONS", 5):
            runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("5", reason)
        self.assertIn("死循环", reason)

    def test_condition_undefined_variable_fails(self):
        flow = self._flow("missing > 0", [_bump()])
        runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("未定义", reason)

    def test_empty_condition_fails(self):
        flow = self._flow("", [_bump()])
        runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("为空", reason)

    def test_literal_true_with_break(self):
        """while 直接填 true：恒真循环，靠体内 if+break 在 n=3 时退出。"""
        steps = [
            _while("true"),
            FlowStep(type="if", params={"condition": "n >= 3"}),
            FlowStep(type="break"),
            FlowStep(type="endif"),
            _bump(),
            _end(),
        ]
        flow = Flow(name="f", variables=[FlowVariable(name="n", type="integer",
                                                      default_value="0")],
                    steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)   # 第 4 轮开头 break，bump 未再执行

    def test_literal_true_still_protected_from_dead_loop(self):
        """字面量 true 同样受死循环保护上限约束。"""
        body = [FlowStep(type="log", params={"text": "x"})]
        flow = self._flow("true", body)
        with mock.patch("app.flows.MAX_WHILE_ITERATIONS", 5):
            runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("死循环", reason)


if __name__ == "__main__":
    unittest.main()
