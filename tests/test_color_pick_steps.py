"""屏幕取色步骤（color_pick）的测试。

覆盖：config 注册/默认参数/摘要；tasks.run_color_pick_step 把配置阶段取到的
颜色写入结果变量；步骤编辑对话框（色块 + HEX/RGB 格式切换 + 变量必选、
取色回写 set_color、颜色文本解析与回填、确定拦截）。
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import FLOW_STEP_TYPES, FlowStep, default_step_params
from app.tasks import run_color_pick_step


# ---------- config ----------

class TestColorPickConfig(unittest.TestCase):
    def test_registered(self):
        self.assertEqual(FLOW_STEP_TYPES.get("color_pick"), "屏幕取色")

    def test_default_params(self):
        p = default_step_params("color_pick")
        self.assertEqual(set(p), {"color", "format", "variable"})
        self.assertEqual(p["color"], "")
        self.assertEqual(p["format"], "hex")
        self.assertEqual(p["variable"], "")

    def test_summary(self):
        s = FlowStep(type="color_pick")
        self.assertIn("取色", s.summary())
        self.assertIn("未取色", s.summary())
        s2 = FlowStep(type="color_pick",
                      params={"color": "#FF0000", "format": "hex", "variable": "c"})
        self.assertIn("#FF0000", s2.summary())
        self.assertIn("c", s2.summary())


# ---------- tasks.run_color_pick_step ----------

class TestRunColorPickStep(unittest.TestCase):
    def test_hex_written_to_variable(self):
        variables = {}
        ok, why = run_color_pick_step(
            {"color": "#FF0000", "format": "hex", "variable": "col"}, variables)
        self.assertTrue(ok)
        self.assertEqual(variables["col"], "#FF0000")
        self.assertIn("col", why)

    def test_rgb_written_to_variable(self):
        variables = {}
        ok, _ = run_color_pick_step(
            {"color": "255,0,128", "format": "rgb", "variable": "col"}, variables)
        self.assertTrue(ok)
        self.assertEqual(variables["col"], "255,0,128")

    def test_missing_variable_fails(self):
        ok, why = run_color_pick_step({"color": "#FF0000", "format": "hex",
                                       "variable": ""}, {})
        self.assertFalse(ok)
        self.assertIn("结果变量", why)

    def test_missing_color_fails(self):
        ok, why = run_color_pick_step({"color": "", "format": "hex",
                                       "variable": "col"}, {})
        self.assertFalse(ok)
        self.assertIn("取色", why)

    def test_stopped(self):
        stop = threading.Event()
        stop.set()
        ok, why = run_color_pick_step(
            {"color": "#FF0000", "format": "hex", "variable": "col"}, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")


# ---------- 对话框 ----------

class TestColorPickDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="color_pick", params=params))

    def test_default_hex_no_color(self):
        dlg = self._open({"color": "", "format": "hex", "variable": ""})
        self.assertTrue(dlg.cp_fmt_hex.isChecked())
        self.assertEqual(dlg.cp_value.text(), "")
        self.assertIsNone(dlg._pick_rgb)

    def test_hex_roundtrip(self):
        dlg = self._open({"color": "#00FF7F", "format": "hex", "variable": "col"})
        self.assertTrue(dlg.cp_fmt_hex.isChecked())
        self.assertEqual(dlg.cp_value.text(), "#00FF7F")
        step = FlowStep(type="color_pick")
        dlg.apply_to(step)
        self.assertEqual(step.params["color"], "#00FF7F")
        self.assertEqual(step.params["format"], "hex")
        self.assertEqual(step.params["variable"], "col")

    def test_rgb_roundtrip(self):
        dlg = self._open({"color": "12,34,56", "format": "rgb", "variable": "col"})
        self.assertTrue(dlg.cp_fmt_rgb.isChecked())
        self.assertEqual(dlg.cp_value.text(), "12,34,56")
        step = FlowStep(type="color_pick")
        dlg.apply_to(step)
        self.assertEqual(step.params["color"], "12,34,56")
        self.assertEqual(step.params["format"], "rgb")

    def test_set_color_hex_then_switch_format(self):
        """set_color 回调后按当前格式显示；切换格式即时重排文本。"""
        dlg = self._open({"color": "", "format": "hex", "variable": "col"})
        dlg.set_color(255, 0, 0)
        self.assertEqual(dlg.cp_value.text(), "#FF0000")
        dlg.cp_fmt_rgb.setChecked(True)
        self.assertEqual(dlg.cp_value.text(), "255,0,0")
        dlg.cp_fmt_hex.setChecked(True)
        self.assertEqual(dlg.cp_value.text(), "#FF0000")

    def test_set_color_lowercase_hex_normalized(self):
        dlg = self._open({"color": "#00ff7f", "format": "hex", "variable": "col"})
        self.assertEqual(dlg.cp_value.text(), "#00FF7F")

    def test_invalid_color_means_not_picked(self):
        dlg = self._open({"color": "not-a-color", "format": "hex", "variable": "col"})
        self.assertIsNone(dlg._pick_rgb)
        self.assertEqual(dlg.cp_value.text(), "")

    def test_accept_requires_color_and_variable(self):
        """未取色 或 未选变量：确定被拦截并提示。"""
        dlg = self._open({"color": "", "format": "hex", "variable": ""})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertIn("取色", warn.call_args.args[1])

        dlg2 = self._open({"color": "#FF0000", "format": "hex", "variable": ""})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn2:
            dlg2.accept()
        warn2.assert_called_once()
        self.assertIn("结果变量", warn2.call_args.args[1])

    def test_accept_passes_when_ready(self):
        dlg = self._open({"color": "#FF0000", "format": "hex", "variable": "col"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_not_called()

    def test_pick_button_hides_and_emits(self):
        """点「屏幕取色…」：对话框隐藏并发 colorPickRequested。"""
        from unittest import mock
        from PySide6.QtCore import QTimer
        dlg = self._open({"color": "", "format": "hex", "variable": "col"})
        fired = []
        dlg.colorPickRequested.connect(lambda: fired.append(True))
        dlg._request_color_pick()
        self.assertFalse(dlg.isVisible())
        self.assertTrue(fired)
        dlg.finish_color_pick()          # 遮罩结束恢复显示
        QTimer.singleShot(0, dlg.close)

    def test_pick_request_button_wiring(self):
        """表单上的「屏幕取色…」按钮确实绑到 _request_color_pick。"""
        dlg = self._open({"color": "", "format": "hex", "variable": ""})
        from PySide6.QtWidgets import QPushButton
        fired = []
        dlg.colorPickRequested.connect(lambda: fired.append(True))
        btn = next(b for b in dlg.findChildren(QPushButton)
                   if "屏幕取色" in b.text())
        btn.click()
        self.assertTrue(fired)


if __name__ == "__main__":
    unittest.main()
