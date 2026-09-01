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

PARAMS = {"region": "10,20,100,50", "save_mode": "variable", "variable": ""}


def _img(w=8, h=6):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ---------- config ----------

class TestScreenshotConfig(unittest.TestCase):
    def test_registered(self):
        from app.config import FLOW_STEP_TYPES
        self.assertEqual(FLOW_STEP_TYPES.get("screenshot"), "截图")

    def test_default_params(self):
        p = default_step_params("screenshot")
        self.assertEqual(set(p), {"region", "save_mode", "variable"})
        self.assertEqual(p["region"], "")
        self.assertEqual(p["save_mode"], "variable")
        self.assertEqual(p["variable"], "")

    def test_summary_variable_mode(self):
        s = FlowStep(type="screenshot")
        self.assertIn("截图", s.summary())
        self.assertIn("变量保存", s.summary())
        s2 = FlowStep(type="screenshot", params={"variable": "path"})
        self.assertIn("截图", s2.summary())
        self.assertIn("path", s2.summary())

    def test_summary_choose_mode(self):
        s = FlowStep(type="screenshot", params={"save_mode": "choose"})
        self.assertIn("自选保存", s.summary())
        self.assertNotIn("变量保存", s.summary())
        s2 = FlowStep(type="screenshot", params={"save_mode": "choose", "variable": "shot"})
        self.assertIn("自选保存", s2.summary())
        self.assertIn("shot", s2.summary())


# ---------- tasks.run_screenshot_step ----------

class TestRunScreenshotStep(unittest.TestCase):
    def test_region_variable_mode(self):
        """指定区域 + 变量保存：按区域抓图，保存到 jietu 目录并写入结果变量。"""
        variables = {}
        path = r"C:\prog\templates\jietu\截图_1.png"
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab, \
             mock.patch.object(screenshot_actor, "save_jietu", return_value=path) as save:
            ok, why = run_screenshot_step(dict(PARAMS, variable="shot"), variables)
        self.assertTrue(ok)
        grab.assert_called_once_with("region", "10,20,100,50")
        save.assert_called_once()
        self.assertEqual(variables["shot"], path)
        self.assertIn(path, why)

    def test_region_empty_falls_back_fullscreen(self):
        """region 为空（旧版全屏配置）：仍按区域调用抓图，内部回退全屏保底。"""
        variables = {}
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab, \
             mock.patch.object(screenshot_actor, "save_jietu", return_value="/x.png"):
            ok, _ = run_screenshot_step(dict(PARAMS, region="", variable="shot"), variables)
        self.assertTrue(ok)
        grab.assert_called_once_with("region", "")

    def test_choose_mode(self):
        """自选保存：ui_call 弹窗得到路径，cv2 写该路径，并把路径写入结果变量。"""
        user_path = r"D:\pics\my.png"
        img = _img()
        variables = {}
        with mock.patch.object(screenshot_actor, "grab_image", return_value=img), \
             mock.patch.object(screenshot_actor, "ui_call", return_value=user_path) as ui, \
             mock.patch("cv2.imwrite") as imw:
            ok, why = run_screenshot_step(dict(PARAMS, save_mode="choose", variable="shot"),
                                          variables)
        self.assertTrue(ok)
        ui.assert_called_once()
        imw.assert_called_once_with(user_path, img)
        self.assertEqual(variables["shot"], user_path)
        self.assertIn(user_path, why)

    def test_choose_cancelled(self):
        """自选保存被取消：步骤失败。"""
        with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()), \
             mock.patch.object(screenshot_actor, "ui_call", return_value=None):
            ok, why = run_screenshot_step(dict(PARAMS, save_mode="choose", variable="shot"), {})
        self.assertFalse(ok)
        self.assertIn("取消", why)

    def test_variable_required_for_both_save_modes(self):
        """变量保存/自选保存都没选结果变量：失败（校验先于抓图）。"""
        for sm in ("variable", "choose"):
            with mock.patch.object(screenshot_actor, "grab_image", return_value=_img()) as grab:
                ok, why = run_screenshot_step(dict(PARAMS, save_mode=sm, variable=""), {})
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
        dlg = self._open({"region": "10,20,100,50",
                          "save_mode": "variable", "variable": "shot"})
        self.assertEqual(dlg.region_edit.text(), "10, 20, 100 x 50")
        self.assertTrue(dlg.save_var_radio.isChecked())
        step = FlowStep(type="screenshot")
        dlg.apply_to(step)
        self.assertEqual(step.params["region"], "10,20,100,50")
        self.assertEqual(step.params["save_mode"], "variable")
        self.assertEqual(step.params["variable"], "shot")

    def test_choose_roundtrip(self):
        dlg = self._open({"region": "10,20,100,50", "save_mode": "choose",
                          "variable": "x"})
        self.assertTrue(dlg.save_choose_radio.isChecked())
        step = FlowStep(type="screenshot")
        dlg.apply_to(step)
        self.assertEqual(step.params["save_mode"], "choose")
        self.assertEqual(step.params["variable"], "x")   # 自选保存也保留结果变量

    def test_rows_visibility(self):
        """区域行与结果变量行常显；只有自选保存说明行随模式切换。"""
        dlg = self._open({"region": "10,20,100,50", "save_mode": "variable",
                          "variable": ""})
        self.assertFalse(dlg._shot_region_widget.isHidden())   # 固定指定区域，常显
        self.assertFalse(dlg._shot_var_widget.isHidden())      # 结果变量常显
        self.assertTrue(dlg._shot_choose_hint_widget.isHidden())

        dlg.save_choose_radio.setChecked(True)
        self.assertFalse(dlg._shot_var_widget.isHidden())      # 自选保存也显示变量行
        self.assertFalse(dlg._shot_choose_hint_widget.isHidden())

        dlg.save_var_radio.setChecked(True)
        self.assertTrue(dlg._shot_choose_hint_widget.isHidden())

    def test_region_required(self):
        """未框选区域：确定被拦截并提示。"""
        dlg = self._open({"region": "", "save_mode": "variable", "variable": "shot"})
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()

    def test_variable_required_for_both_save_modes(self):
        """变量保存/自选保存都没选结果变量：确定都被拦截并提示。"""
        for sm in ("variable", "choose"):
            dlg = self._open({"region": "10,20,100,50", "save_mode": sm, "variable": ""})
            with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
                dlg.accept()
            warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
