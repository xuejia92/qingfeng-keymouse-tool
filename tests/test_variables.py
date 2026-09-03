# -*- coding: utf-8 -*-
"""新模块测试：变量解析、流程变量序列化、变量/日志步骤执行。

只覆盖不依赖 PySide6 的纯逻辑部分（values、config 序列化、tasks 中
不涉及 Qt 的 run_var_step / run_log_step），可在无 GUI 环境运行。
"""
from __future__ import annotations

import unittest

from app.values import (format_value, parse_value, resolve_references,
                        resolve_variable)
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

    def test_list_single_quotes(self):
        """列表默认值兼容单引号写法（原仅支持双引号 JSON）。"""
        self.assertEqual(parse_value("list", "['a','b']"), ["a", "b"])
        self.assertEqual(parse_value("list", "[1, 2, 3]"), [1, 2, 3])

    def test_dict(self):
        self.assertEqual(parse_value("dict", '{"a":1}'), {"a": 1})
        self.assertEqual(parse_value("dict", ""), {})
        with self.assertRaises(ValueError):
            parse_value("dict", "[1,2]")

    def test_dict_single_quotes(self):
        """字典默认值兼容单引号写法（原仅支持双引号 JSON）。"""
        self.assertEqual(parse_value("dict", "{'a':1}"), {"a": 1})
        self.assertEqual(parse_value("dict", "{'k': 'v'}"), {"k": "v"})


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


class TestResolveVariable(unittest.TestCase):
    """变量引用表达式的 Python 语法解析（下标/属性/嵌套）。"""

    def test_plain_name(self):
        ok, value, why = resolve_variable("aaa", {"aaa": 42})
        self.assertTrue(ok, why)
        self.assertEqual(value, 42)

    def test_dict_subscript(self):
        store = {"aaa": {"a": "苹果", "b": "橘子"}}
        ok, value, why = resolve_variable("aaa['a']", store)
        self.assertTrue(ok, why)
        self.assertEqual(value, "苹果")

    def test_dict_subscript_double_quotes(self):
        store = {"aaa": {"a": "苹果"}}
        ok, value, _ = resolve_variable('aaa["a"]', store)
        self.assertTrue(ok)
        self.assertEqual(value, "苹果")

    def test_list_index(self):
        store = {"arr": [10, 20, 30]}
        ok, value, _ = resolve_variable("arr[0]", store)
        self.assertTrue(ok)
        self.assertEqual(value, 10)
        ok, value, _ = resolve_variable("arr[-1]", store)
        self.assertTrue(ok)
        self.assertEqual(value, 30)

    def test_nested_subscript(self):
        store = {"aaa": {"a": {"b": "深"}}}
        ok, value, _ = resolve_variable("aaa['a']['b']", store)
        self.assertTrue(ok)
        self.assertEqual(value, "深")

    def test_undefined_variable(self):
        ok, value, why = resolve_variable("missing", {})
        self.assertFalse(ok)
        self.assertIn("未定义", why)

    def test_missing_key(self):
        ok, value, why = resolve_variable("aaa['x']", {"aaa": {}})
        self.assertFalse(ok)
        self.assertIn("键不存在", why)

    def test_index_out_of_range(self):
        ok, value, why = resolve_variable("arr[9]", {"arr": [1]})
        self.assertFalse(ok)
        self.assertIn("越界", why)

    def test_syntax_error(self):
        ok, value, why = resolve_variable("aaa[", {"aaa": {}})
        self.assertFalse(ok)
        self.assertIn("语法错误", why)

    def test_rejects_function_call(self):
        ok, value, why = resolve_variable("len(aaa)", {"aaa": [1, 2]})
        self.assertFalse(ok)
        self.assertIn("函数调用", why)

    def test_empty_expression(self):
        ok, value, why = resolve_variable("  ", {})
        self.assertFalse(ok)
        self.assertIn("为空", why)


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

    def test_var_summary_shows_default_value(self):
        """步骤列表里变量模块要能看到默认值：名字 [类型] = 默认值。"""
        s = FlowStep(type="var", params={"name": "count", "type": "integer",
                                        "default_value": "5"})
        self.assertEqual(s.summary(), "count  [整数] = 5")

    def test_var_summary_empty_default_shows_kong(self):
        """未填默认值时显示「空」，与「没渲染出来」区分开。"""
        s = FlowStep(type="var", params={"name": "s", "type": "string"})
        self.assertEqual(s.summary(), "s  [字符串] = 空")
        s2 = FlowStep(type="var", params={"name": "s", "type": "string",
                                          "default_value": "   "})
        self.assertEqual(s2.summary(), "s  [字符串] = 空")

    def test_var_summary_long_default_truncated(self):
        """过长的默认值截断，避免撑乱单行列表。"""
        s = FlowStep(type="var", params={"name": "d", "type": "string",
                                         "default_value": "x" * 40})
        text = s.summary()
        self.assertTrue(text.startswith("d  [字符串] = "), text)
        self.assertTrue(text.endswith("…"), text)
        self.assertLessEqual(len(text), len("d  [字符串] = ") + 24)

    def test_var_summary_multiline_default_collapsed(self):
        """多行默认值折叠成字面 \\n，列表里始终是一行。"""
        s = FlowStep(type="var", params={"name": "t", "type": "string",
                                         "default_value": "a\nb"})
        self.assertEqual(s.summary(), "t  [字符串] = a\\nb")

    def test_log_summary(self):
        s = FlowStep(type="log", params={"text": "hi"})
        self.assertEqual(s.summary(), "打印 hi")


class TestRunVarStep(unittest.TestCase):
    """变量步骤默认值：$引用 / 数学运算 / 字符串拼接 / 白名单函数表达式。"""

    def _run(self, p, store, types=None):
        from app.tasks import run_var_step
        return run_var_step(p, store, types)

    def test_string_ref(self):
        store = {"a": "世界"}
        ok, _ = self._run({"name": "b", "type": "string", "default_value": "$a"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], "世界")

    def test_integer_ref(self):
        store = {"a": 5}
        ok, _ = self._run({"name": "b", "type": "integer", "default_value": "$a"}, store)
        self.assertTrue(ok)
        self.assertIsInstance(store["b"], int)
        self.assertEqual(store["b"], 5)

    def test_bool_ref(self):
        store = {"a": True}
        ok, _ = self._run({"name": "b", "type": "bool", "default_value": "$a"}, store)
        self.assertTrue(ok)
        self.assertIs(store["b"], True)

    def test_list_ref(self):
        store = {"a": [1, 2, 3]}
        ok, _ = self._run({"name": "b", "type": "list", "default_value": "$a"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], [1, 2, 3])

    def test_arithmetic(self):
        """数学运算：$a + 1。"""
        store = {"a": 5}
        ok, _ = self._run({"name": "b", "type": "integer",
                           "default_value": "$a + 1"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], 6)

    def test_string_concat(self):
        """字符串拼接：$a + 常量。"""
        store = {"a": "张"}
        ok, _ = self._run({"name": "b", "type": "string",
                           "default_value": '$a + "三"'}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], "张三")

    def test_function_call(self):
        """白名单函数：len($s)。"""
        store = {"s": "abcd"}
        ok, _ = self._run({"name": "n", "type": "integer",
                           "default_value": "len($s)"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["n"], 4)

    def test_compare_to_bool(self):
        """比较表达式投影到 bool 类型：$n > 2。"""
        store = {"n": 3}
        ok, _ = self._run({"name": "big", "type": "bool",
                           "default_value": "$n > 2"}, store)
        self.assertTrue(ok)
        self.assertIs(store["big"], True)

    def test_subscript_expression_without_dollar(self):
        """无 $ 的下标表达式：arr[0] + 1。"""
        store = {"arr": [10, 20]}
        ok, _ = self._run({"name": "b", "type": "integer",
                           "default_value": "arr[0] + 1"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], 11)

    def test_math_expr_on_string_type(self):
        """表达式结果按声明类型投影：string 类型默认 1+1 得字符串 "2"。"""
        store = {}
        ok, _ = self._run({"name": "b", "type": "string",
                           "default_value": "1+1"}, store)
        self.assertTrue(ok)
        self.assertIsInstance(store["b"], str)
        self.assertEqual(store["b"], "2")

    def test_inline_ref_with_plain_text(self):
        """$ 写在普通句子里（拼不成表达式）时按简单占位替换。"""
        store = {"a": "世界"}
        ok, _ = self._run({"name": "b", "type": "string",
                           "default_value": "你好，$a！"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], "你好，世界！")

    def test_plain_text_kept(self):
        """普通文本（含空格/裸词）仍是字面量，不参与表达式。"""
        store = {}
        ok, _ = self._run({"name": "b", "type": "string",
                           "default_value": "hello world"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], "hello world")
        ok2, _ = self._run({"name": "c", "type": "string", "default_value": "hello"}, store)
        self.assertTrue(ok2)
        self.assertEqual(store["c"], "hello")

    def test_dollar_literal_inside_sentence_kept(self):
        """$ 写在普通句子里且变量未定义时，字面 $name 原样保留。"""
        store = {}
        ok, _ = self._run({"name": "b", "type": "string",
                           "default_value": "见 $zz 价"}, store)
        self.assertTrue(ok)
        self.assertEqual(store["b"], "见 $zz 价")

    def test_unknown_ref_fails(self):
        """裸 $引用 变量未定义时表达式求值失败，步骤报错（不再静默保留）。"""
        store = {}
        ok, why = self._run({"name": "b", "type": "string",
                             "default_value": "$zz"}, store)
        self.assertFalse(ok)
        self.assertIn("未定义", why)

    def test_ref_snapshots_value_at_execution_time(self):
        """引用取的是本步骤执行那一刻的值；之后源变量再变不影响已赋值。"""
        store = {"a": 1}
        ok, _ = self._run({"name": "b", "type": "integer", "default_value": "$a"}, store)
        self.assertTrue(ok)
        store["a"] = 99
        self.assertEqual(store["b"], 1)

    def test_types_recorded(self):
        store = {"a": "x"}
        types = {}
        ok, _ = self._run({"name": "b", "type": "string", "default_value": "$a"},
                          store, types)
        self.assertTrue(ok)
        self.assertEqual(types["b"], "string")


class TestRunLogStep(unittest.TestCase):
    """打印输出步骤支持变量名 + Python 下标表达式。"""

    def test_logs_dict_subscript(self):
        from unittest import mock
        from app.tasks import run_log_step
        store = {"aaa": {"a": "苹果"}}
        with mock.patch("app.tasks.log_print") as lg:
            ok, why = run_log_step({"variables": "aaa['a']", "text": ""}, store)
        self.assertTrue(ok)
        self.assertEqual(lg.call_args[0][0], "aaa['a'] = 苹果")

    def test_logs_list_index(self):
        from unittest import mock
        from app.tasks import run_log_step
        store = {"arr": [1, 2, 3]}
        with mock.patch("app.tasks.log_print") as lg:
            run_log_step({"variables": "arr[0]", "text": ""}, store)
        self.assertEqual(lg.call_args[0][0], "arr[0] = 1")

    def test_logs_undefined_with_reason(self):
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg:
            ok, _ = run_log_step({"variables": "nope", "text": ""}, {})
        self.assertTrue(ok)   # 未定义变量不视为失败，仅标注原因
        self.assertIn("未定义", lg.call_args[0][0])

    def test_raw_output_uses_log_print_raw(self):
        """勾选「原始输出」时走原始打印通道（不加时间戳、不自动换行）。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print_raw") as lg:
            ok, _ = run_log_step({"text": "hello", "raw": True}, {})
        self.assertTrue(ok)
        lg.assert_called_once_with("hello")

    def test_raw_false_uses_log_print(self):
        """未勾选「原始输出」时仍走普通打印通道。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg, \
             mock.patch("app.tasks.log_print_raw") as lr:
            run_log_step({"text": "hello", "raw": False}, {})
        lg.assert_called_once_with("hello")
        lr.assert_not_called()

    def test_raw_variable_prints_value_only(self):
        """「原始输出」打印变量时只输出值，不带「变量名 =」前缀。"""
        from unittest import mock
        from app.tasks import run_log_step
        store = {"aaa": {"a": "苹果"}}
        with mock.patch("app.tasks.log_print_raw") as lg:
            ok, _ = run_log_step({"variables": "aaa['a']", "raw": True}, store)
        self.assertTrue(ok)
        self.assertEqual(lg.call_args[0][0], "苹果")

    def test_raw_variables_concat_without_newline(self):
        """「原始输出」多个变量默认直接拼接，不换行。"""
        from unittest import mock
        from app.tasks import run_log_step
        store = {"a": "甲", "b": "乙"}
        with mock.patch("app.tasks.log_print_raw") as lg:
            run_log_step({"variables": "a, b", "raw": True}, store)
        self.assertEqual(lg.call_args[0][0], "甲乙")

    def test_raw_variable_newline_suffix(self):
        """「原始输出」变量后加 \\n 换行。"""
        from unittest import mock
        from app.tasks import run_log_step
        store = {"a": "甲", "b": "乙"}
        with mock.patch("app.tasks.log_print_raw") as lg:
            run_log_step({"variables": "a\\n, b", "raw": True}, store)
        self.assertEqual(lg.call_args[0][0], "甲\n乙")

    def test_raw_variable_backspace_suffix_adds_space(self):
        """「原始输出」变量后加 \\b 加空格。"""
        from unittest import mock
        from app.tasks import run_log_step
        store = {"a": "甲", "b": "乙"}
        with mock.patch("app.tasks.log_print_raw") as lg:
            run_log_step({"variables": "a\\b, b", "raw": True}, store)
        self.assertEqual(lg.call_args[0][0], "甲 乙")

    def test_text_newline_escape(self):
        """「附加文本」字面量 \\n 转成换行。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg:
            ok, _ = run_log_step({"text": "第一行\\n第二行", "raw": False}, {})
        self.assertTrue(ok)
        self.assertEqual(lg.call_args[0][0], "第一行\n第二行")

    def test_text_backspace_escape(self):
        """「附加文本」字面量 \\b 转成空格。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg:
            ok, _ = run_log_step({"text": "甲\\b乙", "raw": False}, {})
        self.assertTrue(ok)
        self.assertEqual(lg.call_args[0][0], "甲 乙")

    def test_text_escape_not_applied_to_variable_value(self):
        """字面量转义只作用于用户手打文本，不误转变量值里的反斜杠序列。"""
        from unittest import mock
        from app.tasks import run_log_step
        store = {"a": "x\\n"}   # 变量值里字面量 backslash+n 应原样保留
        with mock.patch("app.tasks.log_print") as lg:
            run_log_step({"variables": "a", "text": "前\\n后", "raw": False}, store)
        # text 里的 \\n 转成换行；变量 a 的值 x\\n 保持字面量
        self.assertEqual(lg.call_args[0][0], "前\n后\na = x\\n")

    def test_no_variable_outputs_nothing(self):
        """选「无」（variables 空）且无附加文本时，什么都不输出、也不报失败。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg, \
             mock.patch("app.tasks.log_print_raw") as lr:
            ok, why = run_log_step({"variables": "", "text": "", "raw": False}, {"a": "甲"})
        self.assertTrue(ok)
        lg.assert_not_called()
        lr.assert_not_called()

    def test_no_variable_with_text_still_prints_text(self):
        """选「无」（variables 空）但填了附加文本时，附加文本仍要生效。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg:
            ok, _ = run_log_step({"variables": "", "text": "只有文本", "raw": False}, {"a": "甲"})
        self.assertTrue(ok)
        lg.assert_called_once_with("只有文本")

    def test_no_variable_text_only_newline_emitted(self):
        """选「无」且附加文本只填 \\n（转义成换行）时仍要输出——不能被 strip 吞掉。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg:
            ok, _ = run_log_step({"variables": "", "text": "\\n", "raw": False}, {})
        self.assertTrue(ok)
        lg.assert_called_once_with("\n")

    def test_no_variable_text_only_backspace_emitted(self):
        """选「无」且附加文本只填 \\b（转义成空格）时仍要输出。"""
        from unittest import mock
        from app.tasks import run_log_step
        with mock.patch("app.tasks.log_print") as lg:
            ok, _ = run_log_step({"variables": "", "text": "\\b", "raw": False}, {})
        self.assertTrue(ok)
        lg.assert_called_once_with(" ")


if __name__ == "__main__":
    unittest.main()
