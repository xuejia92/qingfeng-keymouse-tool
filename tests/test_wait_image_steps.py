"""「等待图片出现」（wait_image）步骤的测试。

覆盖：类型注册/默认参数/摘要/序列化；tasks.run_wait_image_step 的分支
（全屏/区域/循环检测后命中/超时/停止/模板失败/抓屏异常/无变量不写值）；
参数对话框（默认值/回填/模板必填校验）。
真实抓屏与模板匹配全部 mock，不依赖屏幕与 Qt 事件循环。
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app import finder
from app.config import (FLOW_STEP_TYPES, STEP_OUTPUT_FIELDS, Flow, FlowStep,
                        default_step_params, flow_from_dict, flow_to_dict)
from app.tasks import run_wait_image_step

PARAMS = {"image": "tpl.png", "image_path": "", "confidence": 0.85,
          "region": "", "interval_sec": 1.0, "timeout_sec": 0.0,
          "result_var": "rect", "pos_var": "center"}


def _img(w=8, h=6):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ---------- config ----------

class TestWaitImageConfig(unittest.TestCase):
    def test_registered(self):
        self.assertEqual(FLOW_STEP_TYPES.get("wait_image"), "等待图片出现")

    def test_old_display_name_migrated(self):
        """旧流程文件残留的「等待图片」名称加载时自动刷新为「等待图片出现」。"""
        s = FlowStep(type="wait_image", name="等待图片")
        self.assertEqual(s.name, "等待图片出现")

    def test_custom_name_preserved(self):
        """用户自定义名称不受迁移影响。"""
        s = FlowStep(type="wait_image", name="等登录框")
        self.assertEqual(s.name, "等登录框")

    def test_default_params(self):
        p = default_step_params("wait_image")
        self.assertEqual(set(p), {"image", "image_path", "confidence", "region",
                                  "interval_sec", "timeout_sec",
                                  "result_var", "pos_var"})
        self.assertEqual(p["image"], "")
        self.assertEqual(p["image_path"], "")
        self.assertEqual(p["confidence"], 0.85)
        self.assertEqual(p["region"], "")
        self.assertEqual(p["interval_sec"], 1.0)
        self.assertEqual(p["timeout_sec"], 0.0)
        self.assertEqual(p["result_var"], "")
        self.assertEqual(p["pos_var"], "")

    def test_output_fields_registered(self):
        self.assertEqual(STEP_OUTPUT_FIELDS["wait_image"], ("result_var", "pos_var"))

    def test_summary(self):
        s = FlowStep(type="wait_image", params={"image": "tpl.png"})
        self.assertIn("等待图片出现", s.summary())
        self.assertIn("tpl.png", s.summary())
        s2 = FlowStep(type="wait_image")
        self.assertIn("未选模板", s2.summary())


class TestWaitImageSerialization(unittest.TestCase):
    def test_roundtrip(self):
        f = Flow(name="等图流程", steps=[FlowStep(type="wait_image", name="等图", params={
            "image": "ok.png", "image_path": "", "confidence": 0.9,
            "region": "10,20,100,50", "interval_sec": 0.5, "timeout_sec": 30.0,
            "result_var": "r", "pos_var": "p",
        })])
        back = flow_from_dict(flow_to_dict(f))
        p = back.steps[0].params
        self.assertEqual(p["image"], "ok.png")
        self.assertEqual(p["confidence"], 0.9)
        self.assertEqual(p["region"], "10,20,100,50")
        self.assertEqual(p["interval_sec"], 0.5)
        self.assertEqual(p["timeout_sec"], 30.0)
        self.assertEqual(p["result_var"], "r")
        self.assertEqual(p["pos_var"], "p")


# ---------- tasks.run_wait_image_step ----------

class TestRunWaitImageStep(unittest.TestCase):
    def test_found_fullscreen_writes_variables(self):
        """全屏找图：locate 命中，写矩形区域 + 中心坐标，返回成功。"""
        template, screen = _img(4, 3), _img(20, 20)
        variables = {}
        with mock.patch.object(finder, "load_template", return_value=template), \
             mock.patch.object(finder, "grab_full_screen", return_value=screen), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)) as loc:
            ok, why = run_wait_image_step(dict(PARAMS, region=""), variables)
        self.assertTrue(ok)
        loc.assert_called_once_with(template, screen, 0.85)
        # 模板 4x3，中心 (11,9) -> 左上 (9,8)、右下 (13,11)
        self.assertEqual(variables["rect"], "9,8,13,11")
        self.assertEqual(variables["center"], "11,9")
        self.assertIn("图片已出现", why)

    def test_found_region_uses_locate_in_region(self):
        """指定区域：走 locate_in_region，命中同样写变量。"""
        template, screen = _img(4, 3), _img(20, 20)
        variables = {}
        with mock.patch.object(finder, "load_template", return_value=template), \
             mock.patch.object(finder, "grab_full_screen", return_value=screen), \
             mock.patch.object(finder, "locate_in_region",
                               return_value=(5, 5, 0.9)) as loc:
            ok, _ = run_wait_image_step(
                dict(PARAMS, region="10,20,100,50"), variables)
        self.assertTrue(ok)
        loc.assert_called_once_with(template, screen, 0.85, (10, 20, 100, 50))
        # 模板 4x3，中心 (5,5) -> 左上 (3,4)、右下 (7,7)
        self.assertEqual(variables["rect"], "3,4,7,7")
        self.assertEqual(variables["center"], "5,5")

    def test_found_after_retry(self):
        """第一次未找到、第二次命中：循环检测直到出现。"""
        state = {"n": 0}

        def fake_locate(template, screen, confidence):
            state["n"] += 1
            return None if state["n"] == 1 else (11, 9, 0.95)

        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", side_effect=fake_locate), \
             mock.patch("app.tasks.time.sleep") as slp:
            ok, why = run_wait_image_step(
                dict(PARAMS, interval_sec=0.05), {})
        self.assertTrue(ok)
        self.assertEqual(state["n"], 2)
        slp.assert_called_once_with(0.05)   # 第一次未命中后才间隔等待

    def test_no_vars_writes_nothing(self):
        """未设置结果变量：命中后不写任何变量，步骤仍成功。"""
        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=(11, 9, 0.95)):
            variables = {}
            ok, _ = run_wait_image_step(
                dict(PARAMS, result_var="", pos_var=""), variables)
        self.assertTrue(ok)
        self.assertEqual(variables, {})

    def test_timeout_fails(self):
        """一直未找到：超过 timeout_sec 判失败。"""
        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=None), \
             mock.patch("app.tasks.time.sleep") as slp, \
             mock.patch("app.tasks.time.time", side_effect=[0.0, 0.0, 0.11]):
            ok, why = run_wait_image_step(
                dict(PARAMS, timeout_sec=0.1, interval_sec=0.05), {})
        self.assertFalse(ok)
        self.assertIn("超时", why)
        slp.assert_called()   # 超时前至少循环等待过一次

    def test_stopped(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(finder, "load_template") as lt:
            ok, why = run_wait_image_step(PARAMS, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")
        lt.assert_not_called()

    def test_template_load_failure(self):
        with mock.patch.object(finder, "load_template", return_value=None):
            ok, why = run_wait_image_step(PARAMS, {})
        self.assertFalse(ok)
        self.assertIn("模板图加载失败", why)

    def test_grab_failure(self):
        with mock.patch.object(finder, "load_template", return_value=_img(2, 2)), \
             mock.patch.object(finder, "grab_full_screen",
                               side_effect=OSError("no screen")):
            ok, why = run_wait_image_step(PARAMS, {})
        self.assertFalse(ok)
        self.assertIn("找图失败", why)

    def test_stop_during_wait_breaks(self):
        """等待间隔期间收到停止信号：下一轮循环判定 stop 后判失败。"""
        class FakeStop:
            def __init__(self):
                self._set = False
            def is_set(self):
                return self._set
            def wait(self, interval=None):
                self._set = True            # 模拟等待间隔期间收到手动停止

        stop = FakeStop()
        with mock.patch.object(finder, "load_template", return_value=_img(4, 3)), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img(20, 20)), \
             mock.patch.object(finder, "locate", return_value=None):
            ok, why = run_wait_image_step(
                dict(PARAMS, interval_sec=0.05), {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")


# ---------- 对话框 ----------

class TestWaitImageDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="wait_image", params=params))

    def test_defaults(self):
        dlg = self._open({})
        self.assertEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")
        self.assertAlmostEqual(dlg.confidence.value(), 0.85)
        self.assertAlmostEqual(dlg.wi_interval.value(), 1.0)
        self.assertAlmostEqual(dlg.wi_timeout.value(), 0.0)

    def test_apply_roundtrip(self):
        dlg = self._open({"image": "tpl.png", "image_path": "",
                          "confidence": 0.9, "region": "10,20,100,50",
                          "interval_sec": 2.0, "timeout_sec": 30.0,
                          "result_var": "", "pos_var": ""})
        self.assertEqual(dlg.region_edit.text(), "10, 20, 100 x 50")
        self.assertAlmostEqual(dlg.confidence.value(), 0.9)
        self.assertAlmostEqual(dlg.wi_interval.value(), 2.0)
        self.assertAlmostEqual(dlg.wi_timeout.value(), 30.0)
        dlg._set_combo_value(dlg.wi_result_var, "rect")
        dlg._set_combo_value(dlg.wi_pos_var, "center")
        step = FlowStep(type="wait_image")
        dlg.apply_to(step)
        p = step.params
        self.assertEqual(p["image"], "tpl.png")
        self.assertAlmostEqual(p["confidence"], 0.9)
        self.assertEqual(p["region"], "10,20,100,50")
        self.assertAlmostEqual(p["interval_sec"], 2.0)
        self.assertAlmostEqual(p["timeout_sec"], 30.0)
        self.assertEqual(p["result_var"], "rect")
        self.assertEqual(p["pos_var"], "center")

    def test_accept_requires_template(self):
        dlg = self._open({"image": "", "result_var": "rect"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()

    def test_accept_passes_with_template(self):
        dlg = self._open({"image": "tpl.png"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_not_called()

    def test_region_clear_restores_full_screen(self):
        dlg = self._open({"image": "tpl.png", "region": "10,20,100,50"})
        self.assertNotEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")
        dlg._set_region_text(None)
        self.assertEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")
        step = FlowStep(type="wait_image")
        dlg.apply_to(step)
        self.assertEqual(step.params["region"], "")


if __name__ == "__main__":
    unittest.main()
