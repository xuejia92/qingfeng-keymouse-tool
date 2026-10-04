"""「手动截图」步骤（app/tasks.run_manual_shot_step）的测试。

这个模块的特点：截图区域和保存位置**都不在编辑期预设**，运行到该步骤时才由用户
先框选、再自选保存位置。因此测试重点是「两个交互环节的编排」：

- 框选（含抓图）-> 选保存位置 的顺序，以及抓到的图要原样落盘；
  ⚠️ 抓图**必须发生在「主窗口仍隐藏」的那一段里**——这条在
  tests/test_screenshot_steps.py 里针对 `select_region_and_grab` 单独钉着（2026-10-04
  用户反馈「截的图有时候不对」就是这里漏了）；
- 两个交互都必须走 ui_call（主线程桥接），否则后台线程直接建 QWidget 会崩；
- 任一环节取消（Esc / 取消对话框）都判失败，**不能静默跳过**继续跑后续步骤；
- 框选异常 / 写盘失败 / 手动停止的错误分支；
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

_UNSET = object()          # 区分「没传」和「显式传 None」


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
             img=_UNSET, select_error=None, stop=None):
        """跑一次步骤，返回 (result, select_mock, ask_mock, ui_call_mock)。"""
        params = {"variable": ""} if params is None else params
        # img 用哨兵而不是 None：`img=None` 要能表达「框选没带回图」这个分支
        img = self.img if img is _UNSET else img
        # 框选与抓图现在是**同一个动作**（select_region_and_grab 返回 (rect, img)），
        # 这里整体打桩；region 字符串与"抓图时窗口还藏着"由 screenshot_actor 的用例覆盖。
        if select_error is not None:
            select_kw = {"side_effect": select_error}
        else:
            select_kw = {"return_value": (rect, img if rect else None)}
        with mock.patch.object(shot_actor, "ui_call",
                               side_effect=lambda fn: fn()) as ui_call, \
                mock.patch.object(shot_actor, "select_region_and_grab",
                                  **select_kw) as select, \
                mock.patch.object(shot_actor, "ask_save_path",
                                  return_value=save_path) as ask, \
                mock.patch.object(tasks_mod, "MANUAL_SHOT_SETTLE_SEC", 0):
            result = run_manual_shot_step(params, self.vars, stop)
        return result, select, ask, ui_call

    # ---------- 正常流程 ----------
    def test_success_saves_file_and_writes_variable(self):
        target = self._out()
        (ok, msg), _sel, _ask, _ui = self._run(
            {"variable": "shot_path"}, save_path=target)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(target), "截图文件没有落盘")
        self.assertEqual(self.vars["shot_path"], target)
        self.assertIn(target, msg)

    def test_saves_exactly_the_image_from_selection(self):
        """落盘的必须是框选那一步抓回来的那张图（不能再自己去抓一次屏）。"""
        target = self._out()
        mine = np.full((4, 5, 3), 7, dtype=np.uint8)
        (ok, msg), _sel, _ask, _ui = self._run(
            {"variable": ""}, img=mine, save_path=target)
        self.assertTrue(ok, msg)
        from app import imgio
        saved = imgio.imread(target)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.shape, mine.shape)
        self.assertTrue((saved == 7).all())

    def test_both_interactions_go_through_the_ui_bridge(self):
        """框选遮罩与另存为对话框都是 QWidget，必须经 ui_call 回主线程执行。"""
        (_ok, _msg), _sel, _ask, ui = self._run(
            {"variable": ""}, save_path=self._out())
        self.assertEqual(ui.call_count, 2, "两个交互环节都应经 ui_call")

    def test_variable_is_optional(self):
        target = self._out()
        (ok, _msg), _sel, _ask, _ui = self._run({"variable": ""}, save_path=target)
        self.assertTrue(ok)
        self.assertEqual(self.vars, {}, "没填结果变量时不应写入任何变量")

    # ---------- 取消与失败分支 ----------
    def test_cancel_region_fails_without_saving(self):
        (ok, msg), _sel, ask, _ui = self._run(
            {"variable": ""}, rect=None, save_path=self._out())
        self.assertFalse(ok)
        self.assertIn("取消框选", msg)
        ask.assert_not_called()
        self.assertEqual(self.vars, {})

    def test_too_small_region_fails(self):
        (ok, msg), _sel, ask, _ui = self._run(
            {"variable": ""}, rect=(5, 5, 0, 0), save_path=self._out())
        self.assertFalse(ok)
        self.assertIn("过小", msg)
        ask.assert_not_called()

    def test_missing_image_is_reported(self):
        """框选返回了区域却没带回图（不该发生）：明确报错，别落一张空的。"""
        (ok, msg), _sel, ask, _ui = self._run(
            {"variable": ""}, img=None, save_path=self._out())
        self.assertFalse(ok)
        self.assertIn("没取到画面", msg)
        ask.assert_not_called()

    def test_cancel_save_dialog_fails_and_writes_nothing(self):
        (ok, msg), _sel, ask, _ui = self._run(
            {"variable": "shot_path"}, save_path=None)
        self.assertFalse(ok)
        self.assertIn("取消保存", msg)
        ask.assert_called_once()
        self.assertEqual(self.vars, {})

    def test_selection_error_is_reported(self):
        (ok, msg), _sel, ask, _ui = self._run(
            {"variable": ""}, select_error=RuntimeError("boom"),
            save_path=self._out())
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
            (ok, msg), _sel, _ask, _ui = self._run({"variable": ""}, save_path=target)
        self.assertFalse(ok)
        self.assertIn("写入失败", msg)
        self.assertEqual(self.vars, {})

    def test_stop_before_region_selection(self):
        stop = threading.Event()
        stop.set()
        (ok, msg), select, ask, _ui = self._run({"variable": ""}, stop=stop)
        self.assertFalse(ok)
        self.assertIn("停止", msg)
        select.assert_not_called()
        ask.assert_not_called()

    # ---------- 默认文件名 ----------
    def test_default_name_uses_builtin_prefix_when_blank(self):
        _res, _sel, ask, _ui = self._run({"variable": ""}, save_path=self._out())
        name = os.path.basename(ask.call_args[0][0])
        self.assertRegex(name, r"^手动截图_\d{8}_\d{6}\.png$")

    def test_default_name_uses_configured_prefix(self):
        _res, _sel, ask, _ui = self._run(
            {"variable": "", "default_name": "  报表  "}, save_path=self._out())
        name = os.path.basename(ask.call_args[0][0])
        self.assertRegex(name, r"^报表_\d{8}_\d{6}\.png$")

    def test_default_name_timestamp_is_current(self):
        """预填名里的时间戳取自当前时刻（同秒内两次运行会同名，属预期）。"""
        _res, _sel, ask, _ui = self._run({"variable": ""}, save_path=self._out())
        name = os.path.basename(ask.call_args[0][0])
        m = re.match(r"^手动截图_(\d{8})_(\d{6})\.png$", name)
        self.assertIsNotNone(m, f"预填名格式不符: {name}")
        self.assertEqual(m.group(1), time.strftime("%Y%m%d"))


if __name__ == "__main__":
    unittest.main()
