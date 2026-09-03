"""剪贴板步骤（赋值剪贴板 / 获取剪贴板内容）的测试。

覆盖：tasks 层的 run_clip_set_step / run_clip_get_step 执行逻辑（mock pyperclip，
不碰真实系统剪贴板）、config 层默认参数与摘要、步骤编辑对话框表单。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import FlowStep, default_step_params
from app.tasks import run_clip_get_step, run_clip_set_step


class TestClipSetStep(unittest.TestCase):
    """赋值剪贴板：变量或自定义文本 -> 剪贴板。"""

    def test_requires_source(self):
        ok, why = run_clip_set_step({"name": "", "text": ""}, {})
        self.assertFalse(ok)
        self.assertIn("选择变量", why)

    def test_undefined_variable_fails(self):
        ok, why = run_clip_set_step({"name": "missing", "text": "x"}, {})
        self.assertFalse(ok)
        self.assertIn("未定义", why)

    def test_copies_formatted_value(self):
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, why = run_clip_set_step({"name": "n", "text": ""}, {"n": 42})
        self.assertTrue(ok)
        copy.assert_called_once_with("42")

    def test_copies_dict_subscript_value(self):
        """变量名支持 Python 下标语法：aaa['a'] 取字典值写入剪贴板。"""
        store = {"aaa": {"a": "苹果", "b": "橘子"}}
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, why = run_clip_set_step({"name": "aaa['a']", "text": ""}, store)
        self.assertTrue(ok, why)
        copy.assert_called_once_with("苹果")

    def test_copies_list_index_value(self):
        store = {"arr": [10, 20, 30]}
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, _ = run_clip_set_step({"name": "arr[1]", "text": ""}, store)
        self.assertTrue(ok)
        copy.assert_called_once_with("20")

    def test_dict_subscript_missing_key_fails(self):
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, why = run_clip_set_step({"name": "aaa['x']", "text": ""},
                                        {"aaa": {"a": 1}})
        self.assertFalse(ok)
        self.assertIn("键不存在", why)

    def test_copies_list_as_text(self):
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, _ = run_clip_set_step({"name": "lst", "text": ""}, {"lst": ["a", "b"]})
        self.assertTrue(ok)
        copy.assert_called_once()          # format_value 转文本，不抛异常

    def test_copies_custom_text(self):
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, why = run_clip_set_step({"name": "", "text": "  你好  "}, {})
        self.assertTrue(ok)
        copy.assert_called_once_with("你好")

    def test_text_supports_var_reference(self):
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, _ = run_clip_set_step({"name": "", "text": "值=$v"}, {"v": "abc"})
        self.assertTrue(ok)
        copy.assert_called_once_with("值=abc")

    def test_variable_takes_priority_over_text(self):
        with mock.patch("app.tasks.pyperclip.copy") as copy:
            ok, _ = run_clip_set_step({"name": "n", "text": "other"}, {"n": "real"})
        self.assertTrue(ok)
        copy.assert_called_once_with("real")

    def test_copy_exception_reports_failure(self):
        with mock.patch("app.tasks.pyperclip.copy", side_effect=OSError("denied")):
            ok, why = run_clip_set_step({"name": "n", "text": ""}, {"n": "v"})
        self.assertFalse(ok)
        self.assertIn("失败", why)


class TestClipGetStep(unittest.TestCase):
    """获取剪贴板内容：剪贴板 -> 变量。"""

    def test_requires_variable_name(self):
        ok, why = run_clip_get_step({"variable": ""}, {})
        self.assertFalse(ok)
        self.assertIn("未指定", why)

    def test_paste_assigns_to_variable(self):
        with mock.patch("app.tasks.pyperclip.paste", return_value="hello"):
            ok, why = run_clip_get_step({"variable": "v"}, {})
        self.assertTrue(ok)
        self.assertIn("v", why)

    def test_sets_type_string(self):
        types = {}
        with mock.patch("app.tasks.pyperclip.paste", return_value="text"):
            run_clip_get_step({"variable": "v"}, {}, types)
        self.assertEqual(types["v"], "string")

    def test_paste_exception_reports_failure(self):
        with mock.patch("app.tasks.pyperclip.paste", side_effect=OSError("busy")):
            ok, why = run_clip_get_step({"variable": "v"}, {})
        self.assertFalse(ok)
        self.assertIn("失败", why)


class TestClipConfig(unittest.TestCase):
    """config 层的默认参数 / 步骤校验 / 摘要。"""

    def test_default_params(self):
        p = default_step_params("clip_set")
        self.assertEqual(set(p), {"name", "text"})
        p2 = default_step_params("clip_get")
        self.assertEqual(set(p2), {"variable"})

    def test_flowstep_roundtrip(self):
        s = FlowStep(type="clip_set", params={"name": "x"})
        self.assertEqual(s.params["name"], "x")
        self.assertEqual(s.name, "赋值剪贴板")
        self.assertIn("剪贴板", s.summary())
        s2 = FlowStep(type="clip_get", params={"variable": "y"})
        self.assertEqual(s2.params["variable"], "y")
        self.assertIn("剪贴板", s2.summary())


class TestClipDialogs(unittest.TestCase):
    """步骤编辑对话框能按剪贴板类型构建表单并回填/收集参数。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, step_type: str, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type=step_type, params=params))
        return dlg

    def test_clip_set_form(self):
        dlg = self._open("clip_set", {"name": "x", "text": "hi"})
        self.assertEqual(dlg.clip_name.currentText(), "x")
        self.assertEqual(dlg.clip_text.text(), "hi")
        step = FlowStep(type="clip_set")
        dlg.apply_to(step)
        self.assertEqual(step.params["name"], "x")
        self.assertEqual(step.params["text"], "hi")

    def test_clip_set_form_text_only(self):
        dlg = self._open("clip_set", {"name": "", "text": "hello"})
        self.assertEqual(dlg.clip_name.currentText(), "（不使用变量）")
        step = FlowStep(type="clip_set")
        dlg.apply_to(step)
        self.assertEqual(step.params["name"], "")
        self.assertEqual(step.params["text"], "hello")

    def test_clip_get_form(self):
        dlg = self._open("clip_get", {"variable": "y"})
        self.assertEqual(dlg.clip_variable.currentText(), "y")
        step = FlowStep(type="clip_get")
        dlg.apply_to(step)
        self.assertEqual(step.params["variable"], "y")


if __name__ == "__main__":
    unittest.main()
