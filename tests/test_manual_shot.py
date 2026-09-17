"""「手动截图」步骤（app/tasks.run_manual_shot_step）的测试。

这个模块的特点：截图区域和保存位置**都不在编辑期预设**，运行到该步骤时才由用户
先框选、再自选保存位置。因此测试重点是「两个交互环节的编排」：

- 框选 -> 抓图 -> 选保存位置 的顺序，以及 region 字符串是否按物理像素正确传递；
- 两个交互都必须走 ui_call（主线程桥接），否则后台线程直接建 QWidget 会崩；
- 任一环节取消（Esc / 取消对话框）都判失败，**不能静默跳过**继续跑后续步骤；
- 抓图异常 / 写盘失败 / 手动停止的错误分支；
- 结果变量可选：不填就不写变量。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

import app.screenshot_actor as shot_actor
import app.tasks as tasks_mod
from app.tasks import run_manual_shot_step


class TestManualShotStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="manual_shot_test_")
        self.vars: dict = {}
        self.img = np.zeros((6, 8, 3), dtype=np.uint8)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _out(self, name: str = "out.png") -> str:
        return os.path.join(self.tmp, name)

    def _run(self, params=None, rect=(10, 20, 100, 50), save_path=None,
             img=None, grab_error=None, stop=None):
        """跑一次步骤，返回 (result, grab_mock, ask_mock, ui_call_mock)。"""
        params = {"variable": ""} if params is None else params
        img = self.img if img is None else img
        grab_kw = {"side_effect": grab_error} if grab_error else {"return_value": img}
        with mock.patch.object(shot_actor, "ui_call",
                               side_effect=lambda fn: fn()) as ui_call, \
                mock.patch.object(shot_actor, "select_region",
                                  return_value=rect), \
                mock.patch.object(shot_actor, "grab_image", **grab_kw) as grab, \
                mock.patch.object(shot_actor, "ask_save_path",
                                  return_value=save_path) as ask, \
                mock.patch.object(tasks_mod, "MANUAL_SHOT_SETTLE_SEC", 0):
            result = run_manual_shot_step(params, self.vars, stop)
        return result, grab, ask, ui_call

    # ---------- 正常流程 ----------
    def test_success_saves_file_and_writes_variable(self):
        target = self._out()
        (ok, msg), _grab, _ask, _ui = self._run(
            {"variable": "shot_path"}, save_path=target)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(target), "截图文件没有落盘")
        self.assertEqual(self.vars["shot_path"], target)
        self.assertIn(target, msg)

    def test_captures_the_region_the_user_selected(self):
        """框选返回的 (x,y,w,h) 必须原样变成 "x,y,w,h" 传给抓图。"""
        (_ok, _msg), grab, _ask, _ui = self._run(
            {"variable": ""}, rect=(11, 22, 333, 44), save_path=self._out())
        grab.assert_called_once_with("region", "11,22,333,44")

    def test_float_region_is_truncated_to_int(self):
        (_ok, _msg), grab, _ask, _ui = self._run(
            {"variable": ""}, rect=(10.9, 20.1, 100.7, 50.2), save_path=self._out())
        grab.assert_called_once_with("region", "10,20,100,50")

    def test_both_interactions_go_through_the_ui_bridge(self):
        """框选遮罩与另存为对话框都是 QWidget，必须经 ui_call 回主线程执行。"""
        (_ok, _msg), _grab, _ask, ui = self._run(
            {"variable": ""}, save_path=self._out())
        self.assertEqual(ui.call_count, 2, "两个交互环节都应经 ui_call")

    def test_variable_is_optional(self):
        target = self._out()
        (ok, _msg), _grab, _ask, _ui = self._run({"variable": ""}, save_path=target)
        self.assertTrue(ok)
        self.assertEqual(self.vars, {}, "没填结果变量时不应写入任何变量")

    # ---------- 取消与失败分支 ----------
    def test_cancel_region_fails_without_capturing(self):
        (ok, msg), grab, ask, _ui = self._run(
            {"variable": ""}, rect=None, save_path=self._out())
        self.assertFalse(ok)
        self.assertIn("取消框选", msg)
        grab.assert_not_called()
        ask.assert_not_called()
        self.assertEqual(self.vars, {})

    def test_too_small_region_fails(self):
        (ok, msg), grab, ask, _ui = self._run(
            {"variable": ""}, rect=(5, 5, 0, 0), save_path=self._out())
        self.assertFalse(ok)
        self.assertIn("过小", msg)
        grab.assert_not_called()
        ask.assert_not_called()

    def test_cancel_save_dialog_fails_and_writes_nothing(self):
        (ok, msg), _grab, ask, _ui = self._run(
            {"variable": "shot_path"}, save_path=None)
        self.assertFalse(ok)
        self.assertIn("取消保存", msg)
        ask.assert_called_once()
        self.assertEqual(self.vars, {})

    def test_grab_error_is_reported(self):
        (ok, msg), _grab, ask, _ui = self._run(
            {"variable": ""}, grab_error=RuntimeError("boom"), save_path=self._out())
        self.assertFalse(ok)
        self.assertIn("截图失败", msg)
        self.assertIn("RuntimeError", msg)
        ask.assert_not_called()

    def test_write_failure_is_reported(self):
        """写盘失败：报「图片写入失败」且不写变量。

        打桩的是 app.imgio.imwrite（磁盘图片读写的统一入口），不是 cv2.imwrite——
        cv2 在中文路径下本来就写不进去，拿它当桩会让这个用例失去意义。
        """
        target = self._out()
        with mock.patch("app.imgio.imwrite", return_value=False):
            (ok, msg), _grab, _ask, _ui = self._run({"variable": ""}, save_path=target)
        self.assertFalse(ok)
        self.assertIn("写入失败", msg)
        self.assertEqual(self.vars, {})

    def test_stop_before_region_selection(self):
        stop = threading.Event()
        stop.set()
        (ok, msg), grab, ask, _ui = self._run({"variable": ""}, stop=stop)
        self.assertFalse(ok)
        self.assertIn("停止", msg)
        grab.assert_not_called()
        ask.assert_not_called()

    # ---------- 默认文件名 ----------
    def test_default_name_uses_builtin_prefix_when_blank(self):
        _res, _grab, ask, _ui = self._run({"variable": ""}, save_path=self._out())
        name = os.path.basename(ask.call_args[0][0])
        self.assertRegex(name, r"^手动截图_\d{8}_\d{6}\.png$")

    def test_default_name_uses_configured_prefix(self):
        _res, _grab, ask, _ui = self._run(
            {"variable": "", "default_name": "  报表  "}, save_path=self._out())
        name = os.path.basename(ask.call_args[0][0])
        self.assertRegex(name, r"^报表_\d{8}_\d{6}\.png$")

    def test_default_name_timestamp_is_current(self):
        """预填名里的时间戳取自当前时刻（同秒内两次运行会同名，属预期）。"""
        _res, _grab, ask, _ui = self._run({"variable": ""}, save_path=self._out())
        name = os.path.basename(ask.call_args[0][0])
        m = re.match(r"^手动截图_(\d{8})_(\d{6})\.png$", name)
        self.assertIsNotNone(m, f"预填名格式不符: {name}")
        self.assertEqual(m.group(1), time.strftime("%Y%m%d"))


if __name__ == "__main__":
    unittest.main()
