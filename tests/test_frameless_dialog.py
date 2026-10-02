# -*- coding: utf-8 -*-
"""无边框编辑弹窗「居中于所在屏幕」契约（2026-10-02，第三轮收敛）。

**用户反馈链**：
1. 「定时模块编辑弹窗没在界面中央」→ 加居中（居中于父窗口矩形）；
2. 「弹窗跑到界面外，报 Unable to set geometry 545x809+1008-213」→ 父窗口（无边框
   主窗口）被拖到屏幕外，跟着父窗口矩形居中把弹窗也顶了出去；加限位；
3. 「还是不行」→ showEvent 里的 move 被 Qt 首次显示的初始定位**覆盖**（QDialog
   首次 show 会定位到父窗口中心），且「跟随父窗口矩形」这个策略本身就不对。

**最终方案**：居中于**主窗口所在屏幕**的可用区域中心（不跟父窗口矩形走）；
showEvent 里 `QTimer.singleShot(0)` **延迟一拍**执行（等 Qt 完成首次布局/定位后再
move，否则会被覆盖）；居中后限位在屏内。

本文件只测纯逻辑 `_center_into_screen`（`__new__` 替身 + mock 几何）。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from app.ui import frameless as frameless_mod
from app.ui.frameless import FramelessDialog

_AVAIL = QRect(0, 0, 1920, 1040)          # center = (960, 520)


def _dlg(frame: QRect = QRect(0, 0, 400, 200)) -> FramelessDialog:
    """不跑 __init__ 的替身：只测纯逻辑方法，widget 方法全部 mock。"""
    dlg = FramelessDialog.__new__(FramelessDialog)
    dlg.frameGeometry = mock.Mock(return_value=frame)
    dlg.move = mock.Mock()
    fake_screen = mock.Mock()
    fake_screen.availableGeometry.return_value = _AVAIL
    dlg.screen = mock.Mock(return_value=fake_screen)
    return dlg


def _parent(visible: bool = True,
            frame: QRect = QRect(100, 100, 800, 600)) -> mock.Mock:
    p = mock.Mock()
    p.window.return_value = p
    p.isVisible.return_value = visible
    p.frameGeometry.return_value = frame
    p.screen.return_value = None          # 屏幕改走 dlg.screen()
    return p


class CenterCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    @staticmethod
    def _moved_pos(dlg) -> tuple[int, int]:
        dlg.move.assert_called_once()
        p = dlg.move.call_args[0][0]
        return p.x(), p.y()


class TestCenterIntoScreen(CenterCase):
    def test_centers_on_screen(self):
        """正常情况：居中于父窗口所在屏的可用区域中心。"""
        dlg = _dlg()
        dlg.parentWidget = mock.Mock(return_value=_parent())
        dlg._center_into_screen()
        self.assertEqual(self._moved_pos(dlg), (760, 420))

    def test_parent_offscreen_still_centers_on_screen(self):
        """★ 用户场景：父窗口被拖到屏幕上方外 -> 弹窗仍居中于屏幕，不跟着出屏。"""
        dlg = _dlg()
        dlg.parentWidget = mock.Mock(
            return_value=_parent(True, QRect(100, -500, 800, 600)))
        dlg._center_into_screen()
        self.assertEqual(self._moved_pos(dlg), (760, 420))

    def test_parent_far_right_still_centers_on_screen(self):
        """父窗口在屏幕右下外 -> 同样居中于屏幕。"""
        dlg = _dlg()
        dlg.parentWidget = mock.Mock(
            return_value=_parent(True, QRect(2000, 2000, 800, 600)))
        dlg._center_into_screen()
        self.assertEqual(self._moved_pos(dlg), (760, 420))

    def test_parent_hidden_centers_on_screen(self):
        dlg = _dlg()
        dlg.parentWidget = mock.Mock(return_value=_parent(False))
        dlg._center_into_screen()
        self.assertEqual(self._moved_pos(dlg), (760, 420))

    def test_no_parent_centers_on_screen(self):
        dlg = _dlg()
        dlg.parentWidget = mock.Mock(return_value=None)
        dlg._center_into_screen()
        self.assertEqual(self._moved_pos(dlg), (760, 420))

    def test_oversized_dialog_is_shrunk_into_screen(self):
        """弹窗比屏幕还大：压到屏幕尺寸再居中（定时关机表单很高，小屏会触发）。"""
        dlg = _dlg(frame=QRect(0, 0, 3000, 2000))
        dlg.parentWidget = mock.Mock(return_value=_parent())
        dlg._center_into_screen()
        # 压到 1920x1040，中心对齐 (960,520) -> 左上角 (0,0)
        self.assertEqual(self._moved_pos(dlg), (0, 0))

    def test_no_screen_does_not_move(self):
        dlg = FramelessDialog.__new__(FramelessDialog)
        dlg.frameGeometry = mock.Mock(return_value=QRect(0, 0, 400, 200))
        dlg.move = mock.Mock()
        dlg.parentWidget = mock.Mock(return_value=None)
        dlg.screen = mock.Mock(return_value=None)
        with mock.patch("app.ui.frameless.QApplication.primaryScreen",
                        return_value=None):
            dlg._center_into_screen()
        dlg.move.assert_not_called()


class TestSourceContract(unittest.TestCase):
    def test_show_event_defers_centering(self):
        """居中必须延迟一拍：showEvent 里直接 move 会被 Qt 初始定位覆盖。"""
        src = Path(frameless_mod.__file__).read_text(encoding="utf-8")
        body = src.split("def showEvent")[1].split("\n    def ")[0]
        self.assertIn("_centered", body)
        self.assertIn("QTimer.singleShot(0", body,
                      "showEvent 里必须用 singleShot(0) 延迟居中")

    def test_centering_does_not_follow_parent_rect(self):
        """不许再「居中于父窗口矩形」——那是弹窗跑出屏幕的根因。"""
        src = Path(frameless_mod.__file__).read_text(encoding="utf-8")
        body = src.split("def _center_into_screen")[1].split("\n    def ")[0]
        self.assertNotIn("moveCenter(ref.center())", body,
                         "别退回「居中于父窗口矩形」的写法")
        self.assertIn("availableGeometry", body)
        self.assertIn("moveLeft", body)
        self.assertIn("moveTop", body)


    def test_close_button_uses_drawn_icon_not_font_glyph(self):
        """关闭叉必须自绘图标：× 字形太小、✕ 缺字形渲染成点（用户两次反馈）。"""
        src = Path(frameless_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("def _close_icon", src)
        body = src.split("def _close_icon")[1].split("\n    def ")[0]
        self.assertIn("QPainter", body)
        self.assertNotIn('QPushButton("×")', src,
                         "× 字形太小，别退回字体方案")
        self.assertNotIn('QPushButton("✕")', src,
                         "✕ 在雅黑下缺字形渲染成点")
        self.assertIn("setIcon", src)


if __name__ == "__main__":
    unittest.main()
