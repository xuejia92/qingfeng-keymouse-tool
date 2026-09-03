# -*- coding: utf-8 -*-
"""break / continue 控制流步骤的端到端执行测试（复用 FlowRunner._run_once）。

覆盖：foreach 内 break 提前跳出、continue 跳过本次剩余步骤；while 内 break /
continue；嵌套循环中 break 只中断最内层；break 后继续执行循环外的步骤；孤儿
break/continue（不在任何循环内）报错。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import Flow, FlowStep, FlowVariable, flow_from_dict
from app.flows import FlowRunner, FlowVariableStore


def _run(flow):
    runner = FlowRunner(flow)
    runner.vars = FlowVariableStore(flow)
    reason = runner._run_once()
    return runner, reason


def _foreach(items="items", item_var="x", index_var="i"):
    return FlowStep(type="foreach", params={"items": items,
                                            "item_var": item_var,
                                            "index_var": index_var})


def _endf():
    return FlowStep(type="endForeach")


def _while(condition):
    return FlowStep(type="while", params={"condition": condition})


def _endw():
    return FlowStep(type="endWhile")


def _if(condition):
    return FlowStep(type="if", params={"condition": condition})


def _endif():
    return FlowStep(type="endif")


def _break():
    return FlowStep(type="break")


def _continue():
    return FlowStep(type="continue")


def _inc(name):
    """把流程变量 name 自增 1（用 python 函数步骤实现）。"""
    return FlowStep(type="py_func", params={
        "code": f"def inc({name}):\n    return {name} + 1",
        "func_name": "inc", "variables": [name], "result_var": name})


class TestBreakContinueExecution(unittest.TestCase):
    def test_foreach_break(self):
        """遍历 [1..5]，x>=3 时 break：只累加前两项。"""
        variables = [FlowVariable(name="items", type="list",
                                  default_value="[1, 2, 3, 4, 5]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [_foreach(), _if("x >= 3"), _break(), _endif(), _inc("n"), _endf()]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 2)

    def test_foreach_continue(self):
        """只累加奇数（偶数项 continue 跳过累加）。"""
        variables = [FlowVariable(name="items", type="list",
                                  default_value="[1, 2, 3, 4]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [_foreach(), _if("x % 2 == 0"), _continue(), _endif(),
                 _inc("n"), _endf()]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 2)   # 奇数 1、3

    def test_while_break(self):
        """n 累加到 3 时 break，跳出 while。"""
        variables = [FlowVariable(name="n", type="integer", default_value="0")]
        steps = [_while("n < 100"), _if("n == 3"), _break(), _endif(),
                 _inc("n"), _endw()]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)

    def test_while_continue(self):
        """i 从 0 到 5，偶数项 continue 跳过 count 累加：count=3（奇数 1/3/5）。"""
        variables = [FlowVariable(name="i", type="integer", default_value="0"),
                     FlowVariable(name="count", type="integer", default_value="0")]
        steps = [_while("i < 5"), _inc("i"), _if("i % 2 == 0"), _continue(),
                 _endif(), _inc("count"), _endw()]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["count"], 3)
        self.assertEqual(runner.vars.values["i"], 5)

    def test_orphan_break_fails(self):
        runner, reason = _run(Flow(name="f", steps=[_break()]))
        self.assertIsNotNone(reason)
        self.assertIn("不在任何循环体内", reason)

    def test_orphan_continue_fails(self):
        runner, reason = _run(Flow(name="f", steps=[_continue()]))
        self.assertIsNotNone(reason)
        self.assertIn("不在任何循环体内", reason)

    def test_nested_break_breaks_inner_only(self):
        """外层遍历行，内层遍历每行元素；内层 v>=2 时 break，只中断内层循环。"""
        variables = [FlowVariable(name="rows", type="list",
                                  default_value="[[1, 2, 3], [4, 5, 6]]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [
            _foreach(items="rows", item_var="row", index_var="ri"),
            _foreach(items="row", item_var="v", index_var="ci"),
            _if("v >= 2"), _break(), _endif(),
            _inc("n"),
            _endf(),          # 内层
            _endf(),          # 外层
        ]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        # 第一行：v=1 累加 n=1，v=2 break；第二行：v=4 第一项即 break → 共累加 1 次
        self.assertEqual(runner.vars.values["n"], 1)

    def test_break_continues_after_loop(self):
        """break 后应跳到循环结束标记之后，继续执行循环外的步骤。"""
        variables = [FlowVariable(name="items", type="list",
                                  default_value="[1, 2, 3, 4]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [
            _foreach(),
            _if("x == 2"), _break(), _endif(),
            _inc("n"),
            _endf(),
            _inc("n"),       # 循环外：break 后应执行
        ]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 2)


class TestStructuralNameRefresh(unittest.TestCase):
    """循环模块重命名后，结构标记步骤的 name 应自动刷新为新显示名。"""

    def test_foreach_old_name_refreshed(self):
        self.assertEqual(FlowStep(type="foreach", name="循环").name, "Foreach 循环")

    def test_while_old_name_refreshed(self):
        self.assertEqual(FlowStep(type="while", name="当循环").name, "while 循环")

    def test_end_markers_old_name_refreshed(self):
        self.assertEqual(FlowStep(type="endForeach", name="循环结束").name,
                         "Foreach 循环结束")
        self.assertEqual(FlowStep(type="endWhile", name="当循环结束").name,
                         "while 循环结束")

    def test_break_continue_names(self):
        self.assertEqual(FlowStep(type="break").name, "break 中断循环")
        self.assertEqual(FlowStep(type="continue").name, "continue 继续循环")

    def test_flow_from_dict_refreshes_old_names(self):
        data = {
            "id": "x", "name": "f", "group": "", "hotkey": "", "loops": 1,
            "variables": [],
            "steps": [
                {"type": "foreach", "name": "循环", "params": {}},
                {"type": "endForeach", "name": "循环结束", "params": {}},
                {"type": "while", "name": "当循环",
                 "params": {"condition": "i<3"}},
                {"type": "endWhile", "name": "当循环结束", "params": {}},
            ],
        }
        flow = flow_from_dict(data)
        self.assertEqual([s.name for s in flow.steps],
                         ["Foreach 循环", "Foreach 循环结束",
                          "while 循环", "while 循环结束"])

    def test_plain_step_keeps_custom_name(self):
        """普通步骤（非结构标记）不参与 name 刷新，保留显式传入的 name。"""
        self.assertEqual(FlowStep(type="log", name="自定义日志").name, "自定义日志")


if __name__ == "__main__":
    unittest.main()
