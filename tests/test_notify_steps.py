"""消息通知（notify）步骤的测试。

覆盖：类型注册、默认参数、摘要、序列化往返、通知浮窗展示（offscreen）、
run_notify_step 的 $变量名 解析与空内容报错，以及参数对话框构建/回填/提交。
通知浮窗依赖主线程 UI，用 QT_QPA_PLATFORM=offscreen 冒烟。
"""
from __future__ import annotations

import os
import unittest

from app.config import FLOW_STEP_TYPES, Flow, FlowStep, default_step_params, \
    flow_from_dict, flow_to_dict


class TestStepMetadata(unittest.TestCase):
    def test_registered_as_step_type(self):
        self.assertEqual(FLOW_STEP_TYPES.get("notify"), "消息通知")

    def test_default_params(self):
        p = default_step_params("notify")
        self.assertEqual(p["msg_type"], "info")
        self.assertEqual(p["position"], "bottom")
        self.assertEqual(p["content"], "")
        self.assertEqual(p["duration"], 2.0)
        self.assertEqual(p["width"], 320)


class TestSummary(unittest.TestCase):
    def _summary(self, **params):
        s = FlowStep(type="notify")
        s.params.update(params)
        return s.summary()

    def test_info_type(self):
        self.assertIn("信息通知", self._summary(msg_type="info", content="你好"))

    def test_success_type(self):
        self.assertIn("成功通知", self._summary(msg_type="success", content="完成"))

    def test_empty_content(self):
        self.assertIn("空内容", self._summary(content="   "))

    def test_long_content_truncated(self):
        s = self._summary(content="这是一段非常非常非常非常非常非常非常长的消息内容")
        self.assertLessEqual(len(s), 30)


class TestSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="通知流程", steps=[FlowStep(type="notify", name="弹通知", params={
            "msg_type": "error", "position": "top_right", "content": "出错了 $name",
            "duration": 5.0, "width": 400,
        })])
        back = flow_from_dict(flow_to_dict(f))
        p = back.steps[0].params
        self.assertEqual(p["msg_type"], "error")
        self.assertEqual(p["position"], "top_right")
        self.assertEqual(p["duration"], 5.0)
        self.assertEqual(p["width"], 400)


class TestRunNotifyStep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_resolves_variable_references(self):
        from app.tasks import run_notify_step
        ok, why = run_notify_step(
            {"content": "你好 $name", "duration": 0}, {"name": "张三"})
        self.assertTrue(ok, why)
        self.assertIn("通知", why)

    def test_empty_content_fails(self):
        from app.tasks import run_notify_step
        ok, why = run_notify_step({"content": "   "}, {})
        self.assertFalse(ok)
        self.assertIn("为空", why)

    def test_unresolved_reference_preserved(self):
        # 未知变量 $missing 原样保留（与 log/script/http 等步骤一致），内容非空即成功
        from app.tasks import run_notify_step
        ok, why = run_notify_step({"content": "$missing", "duration": 0}, {})
        self.assertTrue(ok, why)

    def tearDown(self):
        from app.notify_actor import close_all
        close_all()


class TestNotifyActor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_show_and_close(self):
        from app.notify_actor import active_count, show_notification
        before = active_count()
        n = show_notification("测试消息", msg_type="success", duration=0)
        self.assertIsNotNone(n)
        self.assertEqual(active_count(), before + 1)
        n.close()
        self.assertEqual(active_count(), before)

    def test_empty_content_returns_none(self):
        from app.notify_actor import show_notification
        self.assertIsNone(show_notification("   "))

    def test_invalid_type_falls_back_to_info(self):
        from app.notify_actor import _theme
        self.assertEqual(_theme("bogus")["label"], "信息")

    def test_width_clamped(self):
        from app.notify_actor import show_notification
        n = show_notification("宽度钳制", width=99999, duration=0)
        self.assertIsNotNone(n)
        self.assertLessEqual(n.width(), 1200)
        n.close()

    def tearDown(self):
        from app.notify_actor import close_all
        close_all()


class TestFormSmoke(unittest.TestCase):
    """消息通知参数对话框的构建 / 回填 / 提交冒烟测试（offscreen）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dlg(self, **params):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="notify", params=params))

    def test_build_and_fill(self):
        dlg = self._dlg(msg_type="error", position="top_right",
                        content="出错了", duration=5.0, width=400)
        self.assertEqual(dlg.nt_type.currentData(), "error")
        self.assertEqual(dlg.nt_position.currentData(), "top_right")
        self.assertIn("出错了", dlg.nt_content.toPlainText())
        self.assertEqual(dlg.nt_duration.value(), 5.0)
        self.assertEqual(dlg.nt_width.value(), 400)

    def test_apply_roundtrip(self):
        dlg = self._dlg()
        dlg.nt_type.setCurrentIndex(dlg.nt_type.findData("warning"))
        dlg.nt_position.setCurrentIndex(dlg.nt_position.findData("bottom_left"))
        dlg.nt_content.setPlainText("请注意")
        dlg.nt_duration.setValue(3.0)
        dlg.nt_width.setValue(260)
        step = FlowStep(type="notify")
        dlg.apply_to(step)
        self.assertEqual(step.params["msg_type"], "warning")
        self.assertEqual(step.params["position"], "bottom_left")
        self.assertEqual(step.params["content"], "请注意")
        self.assertEqual(step.params["duration"], 3.0)
        self.assertEqual(step.params["width"], 260)

    def test_insert_var_disabled_when_no_flow_vars(self):
        # 无流程上下文（无「变量」步骤）时，插入变量下拉禁用并给出引导占位
        dlg = self._dlg()
        self.assertFalse(dlg.nt_var.isEnabled())
        self.assertEqual(dlg._combo_value(dlg.nt_var), "")
        self.assertIn("暂无变量", dlg.nt_var.currentText())

    def test_insert_var_appends_token_and_resets(self):
        from PySide6.QtGui import QTextCursor
        dlg = self._dlg(content="")
        dlg.nt_var.addItem("user_name", "user_name")   # 模拟流程里已声明的变量
        dlg.nt_var.setCurrentIndex(dlg.nt_var.count() - 1)
        self.assertEqual(dlg.nt_content.toPlainText(), "$user_name")
        # 下拉已复位回「插入变量…」占位项
        self.assertEqual(dlg.nt_var.currentIndex(), 0)
        self.assertEqual(dlg._combo_value(dlg.nt_var), "")
        # 光标在插入文本之后，继续输入不打断
        dlg.nt_content.insertPlainText("，你好")
        self.assertEqual(dlg.nt_content.toPlainText(), "$user_name，你好")


if __name__ == "__main__":
    unittest.main()
