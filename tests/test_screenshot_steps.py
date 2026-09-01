"""截图步骤（screenshot）的测试。

覆盖：config 默认参数与摘要；tasks.run_screenshot_step 的四种分支
（全屏/区域/自己框选/自选保存，mock 抓图与保存）；screenshot_actor 的
保存目录自动创建；步骤编辑对话框表单（方式/区域/保存位置联动、变量校验）。
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app import screenshot_actor
from app.config import FlowStep, default_step_params
from app.tasks import run_screenshot_step

PARAMS = {"mode": "fullscreen", "region": "", "save_mode": "variable", "variable": ""}


def _img(w=8, h=6):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ---------- config ----------

class TestScreenshotConfig(unittest.TestCase):
    def test_registered(self):
        from app.config import FLOW_STEP_TYPES
        self.assertEqual(FLOW_STEP_TYPES.get("screenshot"), "截图")

    def test_default_params(self):
        p = default_step_params("screenshot")
        self.assertEqual(set(p), {"mode", "region", "save_mode", "variable"})
        self.assertEqual(p["mode"], "fullscreen")
        self.assertEqual(p["save_mode"], "variable")
        self.assertEqual(p["variable"], "")

    def test_summary_variable_mode(self):
        s = FlowStep(type="screenshot")
        self.assertIn("全屏截图", s.summary())
        self.assertIn("变量保存", s.summary())
        s2 = FlowStep(type="screenshot", params={"mode": "region", "variable": "path"})
        self.assertIn("指定区域截图", s2.summary())
        self.assertIn("path", s2.summary())

    def test_summary_choose_mode(self):
        s = FlowStep(type="screenshot", params={"save_mode": "choose"})
        self.assertIn("自选保存", s.summary())
        self.assertNotIn("变量保存", s.summary())
        s2 = FlowStep(type="screenshot", params={"mode": "select"})
        self.assertIn("自己框选", s2.summary())


# ---------- tasks.run_screenshot_step ----------

class TestRunScreenshotStep(unittest.TestCase):
    def test_fullscreen_variable_mode(self):
        """全屏 + 变量保存：保存到 jietu 目录并把绝对路径写入结果变量。"""
        variables = {}
        path = r"C:\prog\templates\jietu\截图_1.png"
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab, \
             mock.patch.object(screenshot_actor, "save_jietu", return_value=path) as save:
            ok, why = run_screenshot_step(dict(PARAMS, variable="shot"), variables)
        self.assertTrue(ok)
        grab.assert_called_once_with("fullscreen", "")
        save.assert_called_once()
        self.assertEqual(variables["shot"], path)
        self.assertIn(path, why)

    def test_region_mode(self):
        """指定区域：按 region 抓图。"""
        variables = {}
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab, \
             mock.patch.object(screenshot_actor, "save_jietu", return_value="/x.png"):
            ok, _ = run_screenshot_step(dict(PARAMS, mode="region", region="10,20,100,50",
                                             variable="shot"), variables)
        self.assertTrue(ok)
        grab.assert_called_once_with("region", "10,20,100,50")

    def test_select_mode(self):
        """自己框选：ui_call 拿到区域后按区域抓图。"""
        variables = {}
        with mock.patch.object(screenshot_actor, "ui_call",
                               return_value=(10, 20, 30, 40)) as ui, \
             mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab, \
             mock.patch.object(screenshot_actor, "save_jietu", return_value="/x.png"):
            ok, why = run_screenshot_step(dict(PARAMS, mode="select", variable="shot"),
                                          variables)
        self.assertTrue(ok)
        ui.assert_called_once()                     # select_region 交主线程
        grab.assert_called_once_with("region", "10,20,30,40")

    def test_select_cancelled(self):
        """框选被取消：步骤失败，不抓图不保存。"""
        with mock.patch.object(screenshot_actor, "ui_call", return_value=None), \
             mock.patch.object(screenshot_actor, "grab_image") as grab:
            ok, why = run_screenshot_step(dict(PARAMS, mode="select", variable="shot"), {})
        self.assertFalse(ok)
        self.assertIn("取消", why)
        grab.assert_not_called()

    def test_choose_mode(self):
        """自选保存：ui_call 弹窗得到路径，cv2 写该路径；不写结果变量。"""
        user_path = r"D:\pics\my.png"
        img = _img()
        with mock.patch.object(screenshot_actor, "grab_image", return_value=img), \
             mock.patch.object(screenshot_actor, "ui_call", return_value=user_path) as ui, \
             mock.patch("cv2.imwrite") as imw:
            ok, why = run_screenshot_step(dict(PARAMS, save_mode="choose"), {})
        self.assertTrue(ok)
        ui.assert_called_once()
        imw.assert_called_once_with(user_path, img)
        self.assertIn(user_path, why)

    def test_choose_cancelled(self):
        """自选保存被取消：步骤失败。"""
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()), \
             mock.patch.object(screenshot_actor, "ui_call", return_value=None):
            ok, why = run_screenshot_step(dict(PARAMS, save_mode="choose"), {})
        self.assertFalse(ok)
        self.assertIn("取消", why)

    def test_variable_mode_requires_variable(self):
        """变量保存但没选结果变量：失败。"""
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab:
            ok, why = run_screenshot_step(dict(PARAMS, variable=""), {})
        self.assertFalse(ok)
        self.assertIn("结果变量", why)
        grab.assert_not_called()    # 参数校验先于抓图

    def test_stopped(self):
        stop = threading.Event()
        stop.set()
        ok, why = run_screenshot_step(PARAMS, {}, stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")

    def test_grab_failure(self):
        with mock.patch.object(screenshot_actor, "grab_image",
                               side_effect=OSError("no screen")):
            ok, why = run_screenshot_step(dict(PARAMS, variable="shot"), {})
        self.assertFalse(ok)
        self.assertIn("截图失败", why)


# ---------- screenshot_actor ----------

class TestScreenshotActor(unittest.TestCase):
    def test_save_jietu_creates_dir(self):
        """save_jietu：目录不存在自动创建，返回绝对路径且文件真实存在。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(screenshot_actor, "JIETU_DIR", tmp):
                path = screenshot_actor.save_jietu(_img())
            self.assertTrue(os.path.isdir(tmp))
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.isabs(path))
            self.assertTrue(path.startswith(tmp))

    def test_grab_image_fullscreen(self):
        """grab_image 全屏：抓虚拟桌面 monitors[0]。"""
        arr = np.zeros((4, 6, 4), dtype=np.uint8)
        sct = mock.MagicMock()
        sct.__enter__.return_value = sct          # with mss.mss() 拿到同一实例
        sct.monitors = [{"left": -100, "top": -50, "width": 1600, "height": 900}]
        sct.grab.return_value = arr
        with mock.patch("mss.mss", return_value=sct):
            img = screenshot_actor.grab_image("fullscreen")
        self.assertEqual(img.shape, (4, 6, 3))
        sct.grab.assert_called_once_with(sct.monitors[0])

    def test_grab_image_region(self):
        """grab_image 指定区域：按 "x,y,w,h" 抓图。"""
        arr = np.zeros((3, 5, 4), dtype=np.uint8)
        sct = mock.MagicMock()
        sct.__enter__.return_value = sct
        sct.monitors = [{"left": 0, "top": 0, "width": 100, "height": 100}]
        sct.grab.return_value = arr
        with mock.patch("mss.mss", return_value=sct):
            img = screenshot_actor.grab_image("region", "10,20,30,40")
        self.assertEqual(img.shape, (3, 5, 3))
        mon = sct.grab.call_args.args[0]
        self.assertEqual((mon["left"], mon["top"], mon["width"], mon["height"]),
                         (10, 20, 30, 40))

    def test_grab_image_bad_region_falls_back_fullscreen(self):
        """指定区域但 region 无效（空/坏格式）：退回全屏。"""
        arr = np.zeros((2, 2, 4), dtype=np.uint8)
        sct = mock.MagicMock()
        sct.__enter__.return_value = sct
        sct.monitors = [{"left": 0, "top": 0, "width": 100, "height": 100}]
        sct.grab.return_value = arr
        with mock.patch("mss.mss", return_value=sct):
            screenshot_actor.grab_image("region", "abc")
        self.assertEqual(sct.grab.call_args.args[0], sct.monitors[0])


# ---------- 对话框 ----------

class TestScreenshotDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="screenshot", params=params))

    def test_form_roundtrip(self):
        dlg = self._open({"mode": "region", "region": "10,20,100,50",
                          "save_mode": "variable", "variable": ""})
        self.assertEqual(dlg.shot_mode.currentData(), "region")
        self.assertTrue(dlg.save_var_radio.isChecked())
        step = FlowStep(type="screenshot")
        dlg.apply_to(step)
        self.assertEqual(step.params["mode"], "region")
        self.assertEqual(step.params["region"], "10,20,100,50")
        self.assertEqual(step.params["save_mode"], "variable")

    def test_choose_roundtrip(self):
        dlg = self._open({"mode": "fullscreen", "save_mode": "choose",
                          "variable": "x"})
        self.assertTrue(dlg.save_choose_radio.isChecked())
        step = FlowStep(type="screenshot")
        dlg.apply_to(step)
        self.assertEqual(step.params["save_mode"], "choose")
        self.assertEqual(step.params["variable"], "")   # 自选保存不保留结果变量

    def test_rows_visibility(self):
        """方式联动：只有「指定区域」显示区域行；保存位置联动变量行显隐。"""
        dlg = self._open({"mode": "fullscreen", "save_mode": "variable",
                          "variable": ""})
        self.assertTrue(dlg._shot_region_widget.isHidden())
        self.assertFalse(dlg._shot_var_widget.isHidden())
        self.assertTrue(dlg._shot_choose_hint_widget.isHidden())

        dlg.shot_mode.setCurrentIndex(dlg.shot_mode.findData("region"))
        self.assertFalse(dlg._shot_region_widget.isHidden())

        dlg.save_choose_radio.setChecked(True)
        self.assertTrue(dlg._shot_var_widget.isHidden())
        self.assertFalse(dlg._shot_choose_hint_widget.isHidden())

    def test_variable_save_requires_variable(self):
        """变量保存但没选结果变量：确定被拦截并提示。"""
        dlg = self._open({"mode": "fullscreen", "save_mode": "variable",
                          "variable": ""})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()
        self.assertTrue(dlg.save_var_radio.isChecked())   # 对话框未关闭

    def test_choose_mode_no_validation(self):
        """自选保存不要求结果变量：确定直接通过。"""
        dlg = self._open({"mode": "fullscreen", "save_mode": "choose",
                          "variable": ""})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
