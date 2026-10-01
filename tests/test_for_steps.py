# -*- coding: utf-8 -*-
"""for（计数循环）步骤的端到端执行测试（复用 FlowRunner._run_once）。

语义（2026-09-27 用户定的）：**含结束值**的计数循环——从起始值数到结束值、两头都算，
「从 1 数到 10」就是 10 个数（1..10）。起始值等于结束值时跑 1 轮；步长可为负（倒数）。
默认参数：起始 1、结束 10、步长 1 → 变量依次 1..10。

覆盖：循环次数、循环变量取值、含结束值、负步长倒数、步长 >1、反向序列跳过、
变量表达式（$变量 / len()）作为边界、结束值未填 / 步长 0 / 次数超上限的报错、
break/continue、嵌套 for、循环变量循环结束后保留最后值，以及结构校验与注册表（config/conditions）。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.conditions import block_indent_levels, validate_block_structure
from app.config import (AUTO_STEP_TYPES, FLOW_STEP_TYPES, STEP_OUTPUT_FIELDS, Flow,
                        FlowStep, FlowVariable, default_step_params)
from app.flows import FlowRunner, FlowVariableStore


def _run(flow):
    """在独立 runner 上跑一轮 _run_once，返回 (runner, 结束原因)。"""
    runner = FlowRunner(flow)
    runner.vars = FlowVariableStore(flow)
    reason = runner._run_once()
    return runner, reason


def _for(start="1", stop="10", step="1", var="i"):
    return FlowStep(type="for", params={"var": var, "start": start,
                                        "stop": stop, "step": step})


def _end():
    return FlowStep(type="endFor")


def _bump():
    """循环体：n 加一（记执行轮数），形参 i 同步接收当前循环变量值。"""
    return FlowStep(type="py_func", params={
        "code": "def bump(n, i):\n    return n + 1",
        "func_name": "bump", "variables": ["n", "i"], "result_var": "n"})


def _base(start="1", stop="10", step="1", extra=(), variables_extra=()):
    variables = [FlowVariable(name="n", type="integer", default_value="0")]
    variables.extend(variables_extra)
    body = list(extra) + [_bump()]
    return Flow(name="f", variables=variables,
                steps=[_for(start, stop, step), *body, _end()])


class TestForExecution(unittest.TestCase):
    def test_default_counts_one_to_ten(self):
        """默认参数（1..10）正好 10 轮，变量依次 1..10（用 sum 验证）。"""
        flow = Flow(name="f", variables=[FlowVariable(name="acc", type="integer",
                                                      default_value="0")],
                    steps=[_for(),
                           FlowStep(type="py_func", params={
                               "code": "def add(acc, i):\n    return acc + i",
                               "func_name": "add", "variables": ["acc", "i"],
                               "result_var": "acc"}),
                           _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["acc"], sum(range(1, 11)))   # 55
        self.assertEqual(runner.vars.values["i"], 10)                    # 最后一个值

    def test_runs_expected_times(self):
        flow = _base("1", "3", "1")
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)     # 1、2、3 三轮
        self.assertEqual(runner.vars.values["i"], 3)     # 结束值含 → 最后是 3

    def test_stop_is_inclusive(self):
        """结束值包含在内：起始 5、结束 5 → 1 轮（变量就是 5）。"""
        runner, reason = _run(_base("5", "5", "1"))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 1)
        self.assertEqual(runner.vars.values["i"], 5)

    def test_zero_start_counts_from_zero(self):
        """起始 0、结束 2 → 0、1、2 三轮。"""
        runner, reason = _run(_base("0", "2", "1"))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)
        self.assertEqual(runner.vars.values["i"], 2)

    def test_negative_step_counts_down(self):
        runner, reason = _run(_base("3", "1", "-1"))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)     # 3、2、1
        self.assertEqual(runner.vars.values["i"], 1)     # 含结束值 → 最后是 1

    def test_step_greater_than_one(self):
        runner, reason = _run(_base("1", "7", "2"))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 4)     # 1、3、5、7
        self.assertEqual(runner.vars.values["i"], 7)     # 结束值恰好被踩到

    def test_step_overruns_stop(self):
        """步长越过结束值时以最后一个落在范围内的数收尾：1..6 步长 2 → 1,3,5。"""
        runner, reason = _run(_base("1", "6", "2"))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)
        self.assertEqual(runner.vars.values["i"], 5)

    def test_variable_expression_bound(self):
        flow = _base("1", "$n_stop", "1",
                     variables_extra=[FlowVariable(name="n_stop", type="integer",
                                                   default_value="4")])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 4)     # 1..4 四轮

    def test_stop_from_len_expression(self):
        flow = Flow(name="f",
                    variables=[FlowVariable(name="arr", type="list",
                                            default_value="[1, 2, 3]"),
                               FlowVariable(name="n", type="integer", default_value="0")],
                    steps=[_for("1", "len($arr)", "1"), _bump(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)     # 1..3 三轮

    def test_missing_stop_fails(self):
        runner, reason = _run(_base("1", "", "1"))
        self.assertIsNotNone(reason)
        self.assertIn("未填写结束值", reason)

    def test_step_zero_fails(self):
        runner, reason = _run(_base("1", "3", "0"))
        self.assertIsNotNone(reason)
        self.assertIn("步长不能为 0", reason)

    def test_too_many_iterations_fails(self):
        runner, reason = _run(_base("1", "100000000", "1"))
        self.assertIsNotNone(reason)
        self.assertIn("超过上限", reason)

    def test_non_integer_bound_fails(self):
        runner, reason = _run(_base("1", "'abc'", "1"))
        self.assertIsNotNone(reason)
        self.assertIn("不是整数", reason)

    def test_reversed_direction_skips_body(self):
        """start < stop 但步长为负 → 数不到头，整块跳过。"""
        runner, reason = _run(_base("1", "5", "-1"))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 0)

    def test_break_exits_loop(self):
        steps = [_for("1", "5", "1"),
                 FlowStep(type="if", params={"condition": "i == 2"}),
                 FlowStep(type="break"),
                 FlowStep(type="endif"),
                 _bump(),
                 _end()]
        flow = Flow(name="f", variables=[FlowVariable(name="n", type="integer",
                                                      default_value="0")],
                    steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 1)     # 第 1 轮跑完，i==2 时 break

    def test_continue_skips_rest_of_body(self):
        steps = [_for("1", "3", "1"),
                 FlowStep(type="if", params={"condition": "i == 1"}),
                 FlowStep(type="continue"),
                 FlowStep(type="endif"),
                 _bump(),
                 _end()]
        flow = Flow(name="f", variables=[FlowVariable(name="n", type="integer",
                                                      default_value="0")],
                    steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 2)     # i=1 跳过，2、3 两轮执行

    def test_nested_for(self):
        inner = [_for("1", "3", "1", var="j"), _bump(), _end()]
        steps = [_for("1", "2", "1", var="i")] + inner + [_end()]
        flow = Flow(name="f", variables=[FlowVariable(name="n", type="integer",
                                                      default_value="0")],
                    steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 6)     # 外 2 × 内 3


class TestForStructure(unittest.TestCase):
    def test_valid_pair(self):
        self.assertEqual(validate_block_structure([_for(), _end()]), [])

    def test_break_inside_for_allowed(self):
        self.assertEqual(
            validate_block_structure([_for(), FlowStep(type="break"), _end()]), [])

    def test_missing_end_reported(self):
        errors = validate_block_structure([_for()])
        self.assertTrue(any("endFor" in e or "for 循环" in e for e in errors), errors)

    def test_indent_levels(self):
        steps = [_for(), FlowStep(type="wait"), _end(), FlowStep(type="log")]
        self.assertEqual(block_indent_levels(steps), [0, 1, 0, 0])


class TestForRegistries(unittest.TestCase):
    def test_step_types_registered(self):
        self.assertIn("for", FLOW_STEP_TYPES)
        self.assertIn("endFor", FLOW_STEP_TYPES)
        self.assertIn("endFor", AUTO_STEP_TYPES)          # 结束标记不在面板展示

    def test_output_field_registered(self):
        """循环变量要能被其它步骤的变量下拉收集到。"""
        self.assertEqual(STEP_OUTPUT_FIELDS["for"], ("var",))

    def test_default_params(self):
        """默认 1..10 步长 1（2026-09-27 用户定的默认值）。"""
        p = default_step_params("for")
        self.assertEqual(p["var"], "i")
        self.assertEqual(p["start"], "1")
        self.assertEqual(p["stop"], "10")
        self.assertEqual(p["step"], "1")

    def test_summary(self):
        step = FlowStep(type="for", params={"var": "k", "start": "1",
                                            "stop": "10", "step": "2"})
        self.assertIn("for k", step.summary())
        self.assertIn("步长 2", step.summary())


if __name__ == "__main__":
    unittest.main()
