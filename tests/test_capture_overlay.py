"""屏幕取色遮罩（ColorPickerOverlay）放大镜核心逻辑测试。

覆盖：_grab_around 越界时中心像素恒等于光标像素（不再整体错位）、
_place_box 默认右上 + 贴边自动翻转、_track 光标跟踪与跨屏判定。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np


def _grid():
    from app.capture_overlay import _COLOR_GRID
    return _COLOR_GRID


class TestColorPickerMagnifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _make_overlay(self, pw=1920, ph=1080):
        from PySide6.QtCore import QRect
        from app.capture_overlay import ColorPickerOverlay

        class FakeScreen:
            def geometry(self):
                return QRect(0, 0, pw, ph)

        with mock.patch("app.capture_overlay._physical_geometry",
                        return_value=(0, 0, pw, ph)):
            ov = ColorPickerOverlay(FakeScreen())
        ov._timer.stop()                     # 单元测试不跑定时器
        return ov

    # ---------- _grab_around ----------

    def test_grab_center_pixel_is_cursor(self):
        """光标在屏内：中心像素 = 光标物理像素。"""
        ov = self._make_overlay()
        frame = np.zeros((_grid(), _grid(), 3), dtype=np.uint8)
        frame[:, :, 1] = 255                  # BGR：G=255 → RGB 纯绿
        ov._mss = mock.MagicMock()
        ov._mss.grab.return_value = frame
        bgr = ov._grab_around(100, 100)
        b, g, r = (int(v) for v in bgr[ov._half, ov._half])
        self.assertEqual((r, g, b), (0, 255, 0))

    def test_grab_near_left_edge_keeps_center(self):
        """光标贴最左缘：抓图区域被钳到屏内，中心像素仍对准光标（不整体偏移）。"""
        ov = self._make_overlay()
        frame = np.zeros((_grid(), 8, 3), dtype=np.uint8)   # mss 只返回 x=0 起的 8 列
        frame[:, :, 0] = 255                  # B=255 → RGB 纯蓝（光标所在列）
        ov._mss = mock.MagicMock()
        ov._mss.grab.return_value = frame
        bgr = ov._grab_around(0, 100)         # 光标在最左
        b, g, r = (int(v) for v in bgr[ov._half, ov._half])
        self.assertEqual((r, g, b), (0, 0, 255))   # 中心=光标列(蓝)，而非黑

    def test_grab_fully_out_of_screen_gives_black(self):
        """光标完全越出本屏（不可能由 _refresh 触发，仅防御）：返回全黑。"""
        ov = self._make_overlay(pw=10, ph=10)
        ov._mss = mock.MagicMock()
        ov._mss.grab.side_effect = AssertionError("不应调用抓屏")
        bgr = ov._grab_around(5000, 5000)
        self.assertEqual(int(bgr.sum()), 0)

    # ---------- _place_box ----------

    def test_place_box_default_upper_right(self):
        from app.capture_overlay import _COLOR_ZOOM, _MAG_GAP
        ov = self._make_overlay()
        left, top = ov._place_box(500, 500)
        self.assertEqual(left, 500 + _MAG_GAP)
        self.assertEqual(top, 500 - _COLOR_ZOOM - _MAG_GAP)

    def test_place_box_flips_down_near_top(self):
        from app.capture_overlay import _MAG_GAP
        ov = self._make_overlay()
        left, top = ov._place_box(500, 0)     # 贴近顶部 → 翻到下方
        self.assertEqual(top, 0 + _MAG_GAP)
        self.assertEqual(left, 500 + _MAG_GAP)

    def test_place_box_flips_left_near_right(self):
        from app.capture_overlay import _COLOR_ZOOM, _MAG_GAP
        ov = self._make_overlay(pw=800, ph=600)
        left, top = ov._place_box(790, 300)   # 贴近右缘 → 翻到左侧
        self.assertEqual(left, 790 - _COLOR_ZOOM - _MAG_GAP)

    def test_place_box_always_on_screen(self):
        from app.capture_overlay import _COLOR_ZOOM
        ov = self._make_overlay(pw=800, ph=600)
        for cx in (0, 1, 400, 799, 800):
            for cy in (0, 1, 300, 599, 600):
                left, top = ov._place_box(cx, cy)
                self.assertGreaterEqual(left, 4)
                self.assertGreaterEqual(top, 4)
                self.assertLessEqual(left + _COLOR_ZOOM, 800 - 4 + 1)
                self.assertLessEqual(top + _COLOR_ZOOM, 600 - 4 + 1)

    # ---------- _track ----------

    def test_track_updates_cursor_and_in_me(self):
        ov = self._make_overlay()
        with mock.patch("app.win_actors.cursor_pos", return_value=(500, 400)):
            ov._track()
        self.assertTrue(ov._in_me)
        self.assertEqual(ov._cursor, (500, 400))
        with mock.patch("app.win_actors.cursor_pos", return_value=(-1, -1)):
            ov._track()
        self.assertFalse(ov._in_me)

    # ---------- 方向键微调 ----------

    def test_nudge_moves_cursor_and_refreshes(self):
        ov = self._make_overlay()
        ov._cursor = (100, 100)
        frame = np.zeros((_grid(), _grid(), 3), dtype=np.uint8)
        with mock.patch.object(ov, "_grab_around", return_value=frame), \
             mock.patch("app.win_actors.set_cursor_pos", return_value=True) as setp, \
             mock.patch("app.win_actors.cursor_pos", return_value=(101, 100)):
            ov._nudge(1, 0)
        setp.assert_called_once_with(101, 100)
        self.assertEqual(ov._cursor, (101, 100))
        self.assertEqual(ov._rgb, (0, 0, 0))   # 全黑帧 → 中心色黑

    def test_key_arrows_nudge_1px(self):
        ov = self._make_overlay()
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        with mock.patch.object(ov, "_nudge") as nudge:
            ov.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
            nudge.assert_called_once_with(1, 0)
        with mock.patch.object(ov, "_nudge") as nudge:
            ov.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.NoModifier))
            nudge.assert_called_once_with(0, -1)

    def test_key_shift_arrows_nudge_10px(self):
        ov = self._make_overlay()
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        with mock.patch.object(ov, "_nudge") as nudge:
            ov.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Left, Qt.ShiftModifier))
            nudge.assert_called_once_with(-10, 0)

    def test_key_enter_confirms(self):
        """回车 / 小键盘回车 = 确认取色。"""
        ov = self._make_overlay()
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        for key in (Qt.Key_Return, Qt.Key_Enter):
            with mock.patch.object(ov, "_confirm_pick") as confirm:
                ov.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
                confirm.assert_called_once()

    def test_key_escape_cancels(self):
        ov = self._make_overlay()
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        with mock.patch.object(ov, "_cancel") as cancel:
            ov.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
            cancel.assert_called_once()

    # ---------- 确认取色 ----------

    def test_confirm_pick_grabs_fresh_after_hide(self):
        """确认取色：隐藏遮罩后抓屏，颜色取自抓屏结果（而非 _bgr 缓存）。"""
        ov = self._make_overlay()
        ov._in_me = True
        ov._cursor = (100, 100)
        frame = np.zeros((_grid(), _grid(), 3), dtype=np.uint8)
        frame[:, :, 2] = 255                  # R=255 → RGB 纯红
        grabbed_while_hidden = []

        def fake_grab(px, py):
            grabbed_while_hidden.append(not ov.isVisible())
            return frame

        ov._grab_around = fake_grab
        picked = []
        ov.colorPicked.connect(lambda r, g, b: picked.append((r, g, b)))
        ov._confirm_pick()
        self.assertEqual(picked, [(255, 0, 0)])
        self.assertTrue(len(grabbed_while_hidden) >= 1)

    def test_confirm_pick_ignores_cancel_when_out(self):
        ov = self._make_overlay()
        ov._in_me = False
        ov._grab_around = mock.MagicMock()
        picked = []
        ov.colorPicked.connect(lambda r, g, b: picked.append((r, g, b)))
        ov._confirm_pick()
        self.assertEqual(picked, [])
        ov._grab_around.assert_not_called()


if __name__ == "__main__":
    unittest.main()
