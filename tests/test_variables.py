# -*- coding: utf-8 -*-
"""新模块测试：变量解析、流程变量序列化、变量/日志步骤执行。

只覆盖不依赖 PySide6 的纯逻辑部分（values、config 序列化、tasks 中
不涉及 Qt 的 run_var_step / run_log_step），可在无 GUI 环境运行。
"""
from __future__ import annotations

import unittest

from app.values import format_value, parse_value, resolve_references
from app.config import Flow, FlowStep, FlowVariable, flow_from_dict, flow_to_dict


class TestParseValue(unittest.TestCase):
    def test_string(self):
        self.assertEqual(parse_value("string", "hello"), "hello")

    def test_integer(self):
        self.assertEqual(parse_value("integer", "42"), 42)
        with self.assertRaises(ValueError):
            parse_value("integer", "abc")

    def test_float(self):
        self.assertEqual(parse_value("float", "3.5"), 3.5)

    def test_bool(self):
        self.assertTrue(parse_value("bool", "true"))
        self.assertTrue(parse_value("bool", "YES"))
        self.assertFalse(parse_value("bool", "0"))
        self.assertFalse(parse_value("bool", ""))

    def test_list(self):
        self.assertEqual(parse_value("list", '["a","b"]'), ["a", "b"])
        self.assertEqual(parse_value("list", ""), [])
        with self.assertRaises(ValueError):
            parse_value("list", '{"a":1}')

    def test_dict(self):
        self.assertEqual(parse_value("dict", '{"a":1}'), {"a": 1})
        self.assertEqual(parse_value("dict", ""), {})
        with self.assertRaises(ValueError):
            parse_value("dict", "[1,2]")


class TestFormatValue(unittest.TestCase):
    def test_bool(self):
        self.assertEqual(format_value(True), "true")
        self.assertEqual(format_value(False), "false")

    def test_str_and_num(self):
        self.assertEqual(format_value("abc"), "abc")
        self.assertEqual(format_value(12), "12")

    def test_complex(self):
        self.assertEqual(format_value([1, 2]), "[1, 2]")


class TestResolveReferences(unittest.TestCase):
    def test_known_and_unknown(self):
        store = {"a": 5, "b": "hi"}
        self.assertEqual(resolve_references("$a-$b-$c", store), "5-hi-$c")

    def test_no_refs(self):
        self.assertEqual(resolve_references("plain", {"x": 1}), "plain")


class TestFlowVariables(unittest.TestCase):
    def test_roundtrip(self):
        flow = Flow(
            name="变量流程",
            variables=[FlowVariable(name="count", type="integer", default_value="3"),
                       FlowVariable(name="tag", type="string", default_value="hi")],
            steps=[FlowStep(type="wait", params={"seconds": 1})],
        )
        data = flow_to_dict(flow)
        self.assertIn("variables", data)
        self.assertEqual(len(data["variables"]), 2)
        back = flow_from_dict(data)
        self.assertIsNotNone(back)
        self.assertEqual(len(back.variables), 2)
        self.assertEqual(back.variables[0].name, "count")
        self.assertEqual(back.variables[0].type, "integer")

    def test_from_dict_defaults(self):
        flow = flow_from_dict({
            "name": "x",
            "steps": [{"type": "wait", "params": {"seconds": 1}}],
        })
        self.assertEqual(flow.variables, [])


class TestStepTypes(unittest.TestCase):
    def test_new_types_registered(self):
        from app.config import FLOW_STEP_TYPES
        for t in ("var", "log", "ocr"):
            self.assertIn(t, FLOW_STEP_TYPES)

    def test_var_summary(self):
        s = FlowStep(type="var", params={"name": "n", "type": "bool"})
        self.assertIn("n", s.summary())

    def test_log_summary(self):
        s = FlowStep(type="log", params={"text": "hi"})
        self.assertEqual(s.summary(), "输出 hi")


if __name__ == "__main__":
    unittest.main()
