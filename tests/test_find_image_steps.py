"""找图步骤（find_image）的测试。

覆盖：config 默认参数与摘要；tasks.run_find_image_step 的分支
（全屏/区域/未找到/模板失败/无变量/停止/抓屏异常）；步骤编辑对话框表单
（回填/手动输入坐标解析/模板与变量必填校验）。
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app import finder
from app.config import FlowStep, default_step_params
from app.tasks import run_find_image_step

PARAMS = {"image": "tpl.png", "image_path": "", "confidence": 0.85,
          "region": "", "variable": "pos"}


def _img(w=8, h=6):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ---------- config ----------

class TestFindImageConfig(unittest.TestCase):
    def test_registered(self):
        from app.config import FLOW_STEP_TYPES
        self.assertEqual(FLOW_STEP_TYPES.get("find_image"), "找图")

    def test_default_params(self):
        p = default_step_params("find_image")
        self.assertEqual(set(p), {"image", "image_path", "confidence", "region",
                                  "variable", "preview", "preview_duration"})
        self.assertEqual(p["image"], "")
        self.assertEqual(p["image_path"], "")
        self.assertEqual(p["confidence"], 0.85)
        self.assertEqual(p["region"], "")
        self.assertEqual(p["variable"], "")
        self.assertFalse(p["preview"])
        self.assertEqual(p["preview_duration"], 1.0)

    def test_summary(self):
        s = FlowStep(type="find_image", params={"image": "tpl.png", "variable": "pos"})
        self.assertIn("找图", s.summary())
        self.assertIn("tpl.png", s.summary())
        self.assertIn("pos", s.summary())
        s2 = FlowStep(type="find_image")
        self.assertIn("未选模板", s2.summary())
        self.assertIn("未指定变量", s2.summary())


# ---------- tasks.run_find_image_step ----------

class TestRunFindImageStep(unittest.TestCase):
    def test_found_fullscreen(self):
        """全屏找图：locate 命中，把矩形区域坐标写入结果变量。"""
        template, screen = _img(4, 3), _img(20, 20)
        variables = {}
        with mock.patch.object(finder, "load_template", return_value=template), \
             mock.patch.object(finder, "grab_full_screen", return_value=screen), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)) as loc:
            ok, why = run_find_image_step(dict(PARAMS, region=""), variables)
        self.assertTrue(ok)
        loc.assert_called_once_with(template, screen, 0.85)
        # 模板 4x3，中心 (11,9) -> 左上 (9,8)、右下 (13,11)
        self.assertEqual(variables["pos"], "9,8,13,11")
        self.assertIn("9,8,13,11", why)

    def test_found_region(self):
        """指定区域：调用 locate_in_region，命中同样写矩形区域坐标。"""
        template, screen = _img(4, 3), _img(20, 20)
        variables = {}
        with mock.patch.object(finder, "load_template", return_value=template), \
             mock.patch.object(finder, "grab_full_screen", return_value=screen), \
             mock.patch.object(finder, "locate_in_region", return_value=(5, 5, 0.9)) as loc:
            ok, _ = run_find_image_step(dict(PARAMS, region="10,20,100,50"), variables)
        self.assertTrue(ok)
        loc.assert_called_once_with(template, screen, 0.85, (10, 20, 100, 50))
        # 模板 4x3，中心 (5,5) -> 左上 (3,4)、右下 (7,7)
        self.assertEqual(variables["pos"], "3,4,7,7")

    def test_not_found(self):
        """未找到：结果变量写 false，步骤不失败。"""
        with mock.patch.object(finder, "load_template", return_value=_img(2, 2)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=None):
            variables = {}
            ok, why = run_find_image_step(PARAMS, variables)
        self.assertTrue(ok)
        self.assertIs(variables["pos"], False)
        self.assertIn("未找到", why)

    def test_requires_variable(self):
        """未指定结果变量：失败，且不加载模板。"""
        with mock.patch.object(finder, "load_template") as lt:
            ok, why = run_find_image_step(dict(PARAMS, variable=""), {})
        self.assertFalse(ok)
        self.assertIn("结果变量", why)
        lt.assert_not_called()

    def test_template_load_failure(self):
        with mock.patch.object(finder, "load_template", return_value=None):
            ok, why = run_find_image_step(PARAMS, {})
        self.assertFalse(ok)
        self.assertIn("模板图加载失败", why)

    def test_stopped(self):
        stop = threading.Event()
        stop.set()
        ok, why = run_find_image_step(PARAMS, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")

    def test_grab_failure(self):
        with mock.patch.object(finder, "load_template", return_value=_img(2, 2)), \
             mock.patch.object(finder, "grab_full_screen", side_effect=OSError("no screen")):
            ok, why = run_find_image_step(PARAMS, {})
        self.assertFalse(ok)
        self.assertIn("找图失败", why)

    def test_preview_on_found_highlights(self):
        """勾选效果预览且命中：在目标区域画红框（默认 1 秒）。"""
        template, screen = _img(4, 3), _img(20, 20)
        with mock.patch.object(finder, "load_template", return_value=template), \
             mock.patch.object(finder, "grab_full_screen", return_value=screen), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)), \
             mock.patch("app.find_preview.show_find_highlight") as highlight:
            ok, _ = run_find_image_step(
                dict(PARAMS, preview=True, preview_duration=1.0), {})
        self.assertTrue(ok)
        highlight.assert_called_once_with((9, 8, 13, 11), 1.0)

    def test_preview_custom_duration(self):
        """自定义持续时间透传给红框。"""
        template, screen = _img(4, 3), _img(20, 20)
        with mock.patch.object(finder, "load_template", return_value=template), \
             mock.patch.object(finder, "grab_full_screen", return_value=screen), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)), \
             mock.patch("app.find_preview.show_find_highlight") as highlight:
            ok, _ = run_find_image_step(
                dict(PARAMS, preview=True, preview_duration=2.5), {})
        self.assertTrue(ok)
        highlight.assert_called_once_with((9, 8, 13, 11), 2.5)

    def test_preview_off_no_highlight(self):
        """未勾选效果预览：不画红框。"""
        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)), \
             mock.patch("app.find_preview.show_find_highlight") as highlight:
            ok, _ = run_find_image_step(PARAMS, {})
        self.assertTrue(ok)
        highlight.assert_not_called()

    def test_preview_not_found_no_highlight(self):
        """未找到目标：不画红框，结果写 false。"""
        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=None), \
             mock.patch("app.find_preview.show_find_highlight") as highlight:
            variables = {}
            ok, _ = run_find_image_step(dict(PARAMS, preview=True), variables)
        self.assertTrue(ok)
        self.assertIs(variables["pos"], False)
        highlight.assert_not_called()

    def test_preview_failure_does_not_fail_step(self):
        """红框预览抛异常：不影响找图本身。"""
        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)), \
             mock.patch("app.find_preview.show_find_highlight",
                        side_effect=RuntimeError("no qt")):
            ok, why = run_find_image_step(dict(PARAMS, preview=True), {})
        self.assertTrue(ok)
        self.assertIn("找到目标", why)


# ---------- 对话框 ----------

class TestFindImageDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="find_image", params=params))

    def test_form_roundtrip(self):
        dlg = self._open({"image": "tpl.png", "image_path": "", "confidence": 0.9,
                          "region": "10,20,100,50", "variable": "",
                          "preview": True, "preview_duration": 2.5})
        self.assertEqual(dlg.confidence.value(), 0.9)
        self.assertEqual(dlg.region_edit.text(), "10, 20, 100 x 50")
        self.assertTrue(dlg.preview_check.isChecked())
        self.assertEqual(dlg.preview_spin.value(), 2.5)
        step = FlowStep(type="find_image")
        dlg._set_combo_value(dlg.find_var, "shot")
        dlg.apply_to(step)
        self.assertEqual(step.params["image"], "tpl.png")
        self.assertEqual(step.params["confidence"], 0.9)
        self.assertEqual(step.params["region"], "10,20,100,50")
        self.assertEqual(step.params["variable"], "shot")
        self.assertTrue(step.params["preview"])
        self.assertEqual(step.params["preview_duration"], 2.5)

    def test_form_preview_off_by_default(self):
        """默认不勾选预览，时长 1 秒，spinbox 禁用。"""
        dlg = self._open(PARAMS)
        self.assertFalse(dlg.preview_check.isChecked())
        self.assertEqual(dlg.preview_spin.value(), 1.0)
        self.assertFalse(dlg.preview_spin.isEnabled())
        step = FlowStep(type="find_image")
        dlg.apply_to(step)
        self.assertFalse(step.params["preview"])
        self.assertEqual(step.params["preview_duration"], 1.0)

    def test_apply_manual_region(self):
        """手动输入「左上x,左上y,右下x,右下y」转成 x,y,w,h。"""
        dlg = self._open(PARAMS)
        dlg.manual_edit.setText("100,200,400,500")
        dlg._apply_manual_region()
        self.assertEqual(dlg._region, "100,200,300,300")
        self.assertEqual(dlg.region_edit.text(), "100, 200, 300 x 300")
        self.assertEqual(dlg.manual_edit.text(), "")   # 应用后清空

    def test_apply_manual_region_bad_format(self):
        dlg = self._open(PARAMS)
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.manual_edit.setText("abc")
            dlg._apply_manual_region()
        warn.assert_called_once()

    def test_apply_manual_region_invalid_coords(self):
        """右下角不大于左上角：拦截。"""
        dlg = self._open(PARAMS)
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.manual_edit.setText("400,500,100,200")
            dlg._apply_manual_region()
        warn.assert_called_once()

    def test_accept_requires_template(self):
        dlg = self._open({"image": "", "variable": "shot"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()

    def test_accept_requires_variable(self):
        dlg = self._open({"image": "tpl.png", "variable": ""})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
