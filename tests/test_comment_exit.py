# -*- coding: utf-8 -*-
"""注释/取消注释 与「退出流程」步骤的端到端测试（复用 FlowRunner._run_once）。

覆盖：注释标记序列化往返、被注释步骤运行期跳过、退出流程立即终止、退出前可选
打印变量、退出流程正常结束（不判失败）、exit 摘要与默认参数。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import (Flow, FlowStep, FlowVariable, default_step_params,
                        flow_from_dict, flow_to_dict)
from app.flows import FlowRunner, FlowVariableStore


def _run(flow):
    runner = FlowRunner(flow)
    runner.vars = FlowVariableStore(flow)
    reason = runner._run_once()
    return runner, reason


def _var(name, default="0", vtype="integer"):
    return FlowVariable(name=name, type=vtype, default_value=default)


class TestCommentSerialization(unittest.TestCase):
    def test_roundtrip_preserves_commented(self):
        flow = Flow(name="f", steps=[
            FlowStep(type="log", params={"text": "a"}),
            FlowStep(type="log", params={"text": "b"}, commented=True),
        ])
        back = flow_from_dict(flow_to_dict(flow))
        self.assertEqual([s.commented for s in back.steps], [False, True])

    def test_from_dict_defaults_false(self):
        flow = flow_from_dict({
            "name": "x",
            "steps": [{"type": "wait", "params": {"seconds": 1}}],
        })
        self.assertFalse(flow.steps[0].commented)

    def test_default_commented_false(self):
        self.assertFalse(FlowStep(type="log").commented)


class TestCommentExecution(unittest.TestCase):
    def test_commented_step_skipped(self):
        """被注释的变量步骤不执行：变量不应被写入。"""
        variables = [_var("n", "5")]
        steps = [FlowStep(type="var", params={"name": "n", "type": "integer",
                                              "default_value": "99"}, commented=True),
                 FlowStep(type="log", params={"text": "hi"})]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 5)   # 声明值未被注释步骤覆盖

    def test_comment_whole_block_skipped(self):
        """注释整个 if 块（if + 体 + endif）时，体内步骤都不执行。"""
        variables = [_var("n", "0")]
        steps = [
            FlowStep(type="if", params={"condition": "1 == 1"}, commented=True),
            FlowStep(type="var", params={"name": "n", "type": "integer",
                                         "default_value": "1"}, commented=True),
            FlowStep(type="endif", commented=True),
        ]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 0)

    def test_mixed_comment_only_skips_marked(self):
        """只注释 if 头、保留体：体仍执行（头被跳过，不再条件分支）。"""
        variables = [_var("n", "0")]
        steps = [
            FlowStep(type="if", params={"condition": "1 == 1"}, commented=True),
            FlowStep(type="var", params={"name": "n", "type": "integer",
                                         "default_value": "7"}),
            FlowStep(type="endif"),
        ]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 7)


class TestExitExecution(unittest.TestCase):
    def test_exit_terminates_immediately(self):
        """exit 之后的步骤不再执行。"""
        variables = [_var("n", "0")]
        steps = [
            FlowStep(type="exit"),
            FlowStep(type="var", params={"name": "n", "type": "integer",
                                         "default_value": "1"}),
        ]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertEqual(reason, "已退出流程")
        self.assertEqual(runner.vars.values["n"], 0)   # 后续步骤未执行

    def test_exit_prints_variable(self):
        """退出前打印指定变量的值（走打印通道，蓝色显示）。"""
        variables = [_var("n", "42")]
        steps = [FlowStep(type="exit", params={"variable": "n"})]
        with mock.patch("app.flows.log_print") as fake_log:
            runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertEqual(reason, "已退出流程")
        printed = [c[0][0] for c in fake_log.call_args_list if c[0]]
        self.assertTrue(any("n = 42" in m for m in printed), printed)

    def test_exit_undefined_variable_does_not_crash(self):
        """打印未定义变量：不崩溃，正常退出。"""
        variables = [_var("n", "1")]
        steps = [FlowStep(type="exit", params={"variable": "missing"})]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertEqual(reason, "已退出流程")

    def test_exit_no_variable(self):
        """不打印变量也能正常退出。"""
        runner, reason = _run(Flow(name="f", steps=[FlowStep(type="exit")]))
        self.assertEqual(reason, "已退出流程")

    def test_exit_inside_loop_terminates_whole_flow(self):
        """exit 在循环体内也应立即终止整个流程，而非只跳出循环。"""
        variables = [_var("items", "[1, 2, 3]", "list"),
                     _var("n", "0")]
        steps = [
            FlowStep(type="foreach", params={"items": "items",
                                             "item_var": "x", "index_var": "i"}),
            FlowStep(type="exit"),
            FlowStep(type="endForeach"),
        ]
        runner, reason = _run(Flow(name="f", variables=variables, steps=steps))
        self.assertEqual(reason, "已退出流程")

    def test_run_marks_exit_as_ok(self):
        """退出流程应视为正常结束：_run 发出的 stateChanged 的 ok=True。"""
        runner = FlowRunner(Flow(name="f", steps=[FlowStep(type="exit")]))
        captured = {}
        runner.stateChanged.connect(
            lambda state, reason, ok: captured.update(
                state=state, reason=reason, ok=ok))
        runner._run()   # 直接调用（不起线程），验证结束原因与 ok 判定
        self.assertEqual(captured["state"], "stopped")
        self.assertEqual(captured["reason"], "已退出流程")
        self.assertTrue(captured["ok"])


class TestExitConfig(unittest.TestCase):
    def test_default_params(self):
        self.assertEqual(default_step_params("exit"), {"variable": ""})

    def test_summary_no_variable(self):
        self.assertEqual(FlowStep(type="exit").summary(), "退出流程")

    def test_summary_with_variable(self):
        s = FlowStep(type="exit", params={"variable": "n"})
        self.assertEqual(s.summary(), "退出流程（打印 n）")

    def test_name(self):
        self.assertEqual(FlowStep(type="exit").name, "退出流程")


if __name__ == "__main__":
    unittest.main()
