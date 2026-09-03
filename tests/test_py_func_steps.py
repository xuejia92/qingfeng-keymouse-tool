"""python函数步骤（py_func）的测试。

覆盖：config 层默认参数与摘要、tasks 层 run_py_func_step 执行语义
（同名变量自动传参 / 环境注入 / 各类错误）、FlowStep 序列化往返、
步骤编辑对话框表单。

执行语义约定：
- params["func_name"] 必填：先执行代码，再调用该函数取返回值；
- params["variables"] 勾选的流程变量：
  · 与函数形参**同名**的 → 调用时自动作为关键字实参传入（fn(**kwargs)），
    形参即取到流程变量值；
  · 与形参不同名的 → 注入代码全局环境，代码与函数体可直接读取；
- 无默认值的必填形参没有同名变量可传 → 明确报错；
- 返回值写入 params["result_var"]。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import FLOW_STEP_TYPES, Flow, FlowStep, default_step_params
from app.tasks import run_py_func_step


def _params(**over):
    p = default_step_params("py_func")
    p.update(over)
    return p


class TestPyFuncConfig(unittest.TestCase):
    def test_type_registered(self):
        self.assertEqual(FLOW_STEP_TYPES.get("py_func"), "python函数")

    def test_default_params(self):
        p = default_step_params("py_func")
        self.assertEqual(p["code"], "")
        self.assertEqual(p["func_name"], "")
        self.assertEqual(p["variables"], [])
        self.assertEqual(p["result_var"], "")

    def test_summary_call_mode(self):
        s = FlowStep(type="py_func", params={"func_name": "print_current_time",
                                             "result_var": "t"})
        self.assertIn("print_current_time", s.summary())
        self.assertIn("t", s.summary())

    def test_summary_missing_func_name(self):
        s = FlowStep(type="py_func", params={"func_name": "", "result_var": "out"})
        self.assertEqual(s.summary(), "python函数（未填函数名）→ out")

    def test_summary_no_result_var(self):
        s = FlowStep(type="py_func", params={"func_name": "f"})
        self.assertIn("未指定变量", s.summary())


class TestPyFuncExecution(unittest.TestCase):
    """执行逻辑：函数名必填 + 同名变量自动传参 + 非同名注入环境。"""

    def test_missing_func_name_fails(self):
        ok, why = run_py_func_step(_params(code="def f():\n    return 1",
                                           func_name="", result_var="out"), {})
        self.assertFalse(ok)
        self.assertIn("调用函数名", why)

    def test_empty_code_fails(self):
        ok, why = run_py_func_step(_params(func_name="f", result_var="out"), {"x": 1})
        self.assertFalse(ok)
        self.assertIn("代码为空", why)

    def test_missing_result_var_fails(self):
        ok, why = run_py_func_step(_params(code="def f():\n    return 1",
                                           func_name="f"), {})
        self.assertFalse(ok)
        self.assertIn("结果变量", why)

    def test_undefined_injected_var_fails(self):
        ok, why = run_py_func_step(_params(code="def f():\n    return missing",
                                           func_name="f", variables=["missing"],
                                           result_var="out"), {})
        self.assertFalse(ok)
        self.assertIn("missing", why)

    def test_non_param_var_visible_in_body(self):
        """非形参名的勾选变量注入全局环境，函数体内可直接读取。"""
        store = {"x": 5}
        ok, why = run_py_func_step(_params(code="def f():\n    return x + 1",
                                           func_name="f", variables=["x"],
                                           result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], 6)

    def test_param_auto_bound_by_same_name(self):
        """同名自动传参：变量 date_format/time_format 勾选后成为函数实参。"""
        code = ("def stamp(date_format, time_format):\n"
                "    return date_format + ' ' + time_format")
        store = {"date_format": "%Y/%m/%d", "time_format": "%H:%M"}
        ok, why = run_py_func_step(_params(code=code, func_name="stamp",
                                           variables=["date_format", "time_format"],
                                           result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], "%Y/%m/%d %H:%M")

    def test_param_bound_and_global_injected_mixed(self):
        """同名形参自动传入 + 非同名变量仍从注入环境读取，两者共存。"""
        code = ("def fmt(text, date_format):\n"
                "    return text + '|' + date_format + '|' + extra")
        store = {"text": "T", "date_format": "%Y", "extra": "E"}
        ok, why = run_py_func_step(_params(code=code, func_name="fmt",
                                           variables=["text", "date_format", "extra"],
                                           result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], "T|%Y|E")

    def test_default_param_used_when_no_same_name_var(self):
        store = {"name": "hi"}
        ok, why = run_py_func_step(
            _params(code="def greet(name, suffix='!'):\n    return name + suffix",
                    func_name="greet", variables=["name"], result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], "hi!")

    def test_missing_required_param_reports(self):
        """必填形参 b 无同名变量且无默认值 → 明确报缺失。"""
        store = {"a": 1}
        ok, why = run_py_func_step(
            _params(code="def add(a, b):\n    return a + b",
                    func_name="add", variables=["a"], result_var="out"), store)
        self.assertFalse(ok)
        self.assertIn("缺少必填参数", why)
        self.assertIn("b", why)

    def test_reference_code_sample(self):
        """用户提供的 print_current_time 示例应能原样运行（形参用默认值）。"""
        code = (
            "import datetime\n"
            "def print_current_time(date_format=\"%Y-%m-%d\", time_format=\"%H:%M:%S\"):\n"
            "    now = datetime.datetime.now()\n"
            "    date_part = now.strftime(date_format)\n"
            "    time_part = now.strftime(time_format)\n"
            "    return f\"{date_part} {time_part}\""
        )
        store = {}
        ok, why = run_py_func_step(_params(code=code, func_name="print_current_time",
                                           variables=[], result_var="now_text"), store)
        self.assertTrue(ok, why)
        import re
        self.assertRegex(store["now_text"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_import_available_inside_function(self):
        store = {}
        ok, why = run_py_func_step(
            _params(code="import datetime\n"
                         "def year_now():\n"
                         "    return datetime.datetime.now().year",
                    func_name="year_now", variables=[], result_var="year"), store)
        self.assertTrue(ok, why)
        self.assertIsInstance(store["year"], int)

    def test_unknown_func_fails(self):
        ok, why = run_py_func_step(_params(code="def f():\n    return 1",
                                           func_name="no_such", result_var="out"), {})
        self.assertFalse(ok)
        self.assertIn("未找到函数", why)

    def test_syntax_error_fails(self):
        ok, why = run_py_func_step(_params(code="def f(:\n", func_name="f",
                                           result_var="out"), {})
        self.assertFalse(ok)
        self.assertIn("语法错误", why)

    def test_runtime_error_in_module_code_fails(self):
        ok, why = run_py_func_step(
            _params(code="x = 1 / 0\ndef f():\n    return 1",
                    func_name="f", result_var="out"), {})
        self.assertFalse(ok)
        self.assertIn("代码执行出错", why)

    def test_error_in_call_fails(self):
        ok, why = run_py_func_step(_params(code="def f():\n    raise ValueError('boom')",
                                           func_name="f", result_var="out"), {})
        self.assertFalse(ok)
        self.assertIn("函数 f 执行出错", why)
        self.assertIn("boom", why)

    def test_non_injected_var_not_visible(self):
        """没勾选注入的变量在代码里不可见（NameError 报错，不静默）。"""
        store = {"x": 5}
        ok, why = run_py_func_step(_params(code="def f():\n    return x + 1",
                                           func_name="f", variables=[], result_var="out"),
                                   store)
        self.assertFalse(ok)
        self.assertIn("执行出错", why)

    def test_list_result_saved_as_is(self):
        store = {}
        ok, why = run_py_func_step(
            _params(code="def f():\n    return [1, 2, 3]",
                    func_name="f", result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], [1, 2, 3])

    def test_overwrites_existing_variable(self):
        store = {"out": "old"}
        ok, why = run_py_func_step(_params(code="def f():\n    return 'new'",
                                           func_name="f", result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], "new")

    def test_duplicate_injected_names_deduped(self):
        store = {"x": 3}
        ok, why = run_py_func_step(_params(code="def f():\n    return x * x",
                                           func_name="f", variables=["x", "x"],
                                           result_var="out"), store)
        self.assertTrue(ok, why)
        self.assertEqual(store["out"], 9)

    def test_stopped_returns_manually(self):
        import threading
        stop = threading.Event()
        stop.set()
        ok, why = run_py_func_step(_params(code="def f():\n    return 1",
                                           func_name="f", result_var="out"),
                                   {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")


class TestPyFuncFlowStep(unittest.TestCase):
    def test_flowstep_defaults_and_roundtrip(self):
        flow = Flow(name="py", steps=[FlowStep(type="py_func")])
        s = flow.steps[0]
        self.assertEqual(s.name, "python函数")
        self.assertEqual(s.params["variables"], [])
        from app.config import flow_from_dict, flow_to_dict
        back = flow_from_dict(flow_to_dict(flow))
        self.assertIsNotNone(back)
        self.assertEqual(back.steps[0].params["result_var"], "")

    def test_flowstep_keeps_params(self):
        s = FlowStep(type="py_func", params={"code": "result = 1", "func_name": "f",
                                             "variables": ["a"], "result_var": "out"})
        self.assertEqual(s.params["variables"], ["a"])
        self.assertEqual(s.params["result_var"], "out")


class TestPyFuncDialogs(unittest.TestCase):
    """步骤编辑对话框能按 py_func 构建表单并回填/收集参数。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="py_func", params=params))

    def test_form_builds(self):
        dlg = self._open(_params(code="def f():\n    return 1",
                                 func_name="f", result_var="r"))
        self.assertIn("return 1", dlg.code_edit.toPlainText())
        self.assertEqual(dlg.func_edit.text(), "f")
        self.assertEqual(dlg._combo_value(dlg.py_result_var), "r")

    def test_form_fill_and_apply(self):
        dlg = self._open(_params(code="def f():\n    return x",
                                 func_name="f", variables=["x", "y"],
                                 result_var="r"))
        self.assertEqual(len(dlg._py_var_combos), 2)
        step = FlowStep(type="py_func")
        dlg.apply_to(step)
        self.assertEqual(step.params["func_name"], "f")
        self.assertEqual(step.params["variables"], ["x", "y"])
        self.assertEqual(step.params["result_var"], "r")
        self.assertIn("return x", step.params["code"])

    def test_var_rows_dynamic_add_remove(self):
        dlg = self._open(_params())
        dlg._add_py_var_row("legacy")
        self.assertEqual(len(dlg._py_var_combos), 1)
        self.assertEqual(dlg._combo_value(dlg._py_var_combos[0]), "legacy")
        dlg._add_py_var_row("newone")
        self.assertEqual(len(dlg._py_var_combos), 2)
        combo0 = dlg._py_var_combos[0]
        dlg._remove_py_var_row(combo0)
        self.assertEqual(len(dlg._py_var_combos), 1)
        # 移除后重新收集：只剩后来添加的行
        step = FlowStep(type="py_func")
        dlg.apply_to(step)
        self.assertEqual(step.params["variables"], ["newone"])

    def test_form_rejects_missing_func_name(self):
        dlg = self._open(_params(code="def f():\n    return 1", result_var="r"))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请填写调用函数名")

    def test_form_rejects_missing_result_var(self):
        dlg = self._open(_params(code="def f():\n    return 1", func_name="f"))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请设置结果变量")


if __name__ == "__main__":
    unittest.main()
