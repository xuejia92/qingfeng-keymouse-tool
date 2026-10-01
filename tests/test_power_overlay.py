# -*- coding: utf-8 -*-
"""「定时关机」提醒倒计时的屏幕浮层（app/power_overlay.py）的测试。

分两层：
· **纯函数**（format_remain / countdown_text / clamp_font_size）：倒计时文案与字号
  的边界——向上取整、非法值兜底、越界夹紧，全部不需要 Qt。
· **窗口**（PowerCountdown）：无边框 / 置顶 / 鼠标穿透三个标志、红色样式、字号与
  加粗、底部居中定位、单例复用、收起与销毁，走 offscreen 冒烟。

窗口用例一律在 setUpClass 里先建 QApplication：没有它去构造 QWidget 不是抛异常，
而是直接把进程 abort 掉（同 QPixmap 的老坑）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt

import app.power_overlay as overlay
from app.power_overlay import (BOTTOM_MARGIN, DEFAULT_FONT_SIZE, MAX_FONT_SIZE,
                               MIN_FONT_SIZE, NOTE, NOTE_NO_ESC, PowerCountdown,
                               clamp_font_size, countdown_text, format_remain)


class TestCountdownText(unittest.TestCase):
    def test_format_remain_rounds_up(self):
        """向上取整：还剩 0.4 秒要显示「1 秒」，显示成 0 秒像是在骗人。"""
        self.assertEqual(format_remain(0.4), "1 秒")
        self.assertEqual(format_remain(1), "1 秒")
        self.assertEqual(format_remain(1.01), "2 秒")

    def test_format_remain_units(self):
        self.assertEqual(format_remain(15), "15 秒")
        self.assertEqual(format_remain(90), "1 分 30 秒")
        self.assertEqual(format_remain(600), "10 分")
        self.assertEqual(format_remain(3600), "1 小时")

    def test_format_remain_tolerates_bad_input(self):
        for bad in (None, "", "abc", [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(format_remain(bad), "0 秒")

    def test_format_remain_never_negative(self):
        self.assertEqual(format_remain(-5), "0 秒")

    def test_countdown_text_uses_action_label(self):
        self.assertEqual(countdown_text(15, "关机"), "15 秒后关机")
        self.assertEqual(countdown_text(90, "重启"), "1 分 30 秒后重启")
        self.assertEqual(countdown_text(5, "锁定屏幕"), "5 秒后锁定屏幕")

    def test_countdown_text_falls_back_to_shutdown(self):
        self.assertEqual(countdown_text(3, ""), "3 秒后关机")
        self.assertEqual(countdown_text(3, None), "3 秒后关机")


class TestClampFontSize(unittest.TestCase):
    def test_in_range_kept(self):
        self.assertEqual(clamp_font_size(48), 48)
        self.assertEqual(clamp_font_size("36"), 36)

    def test_out_of_range_clamped(self):
        self.assertEqual(clamp_font_size(0), MIN_FONT_SIZE)
        self.assertEqual(clamp_font_size(-20), MIN_FONT_SIZE)
        self.assertEqual(clamp_font_size(9999), MAX_FONT_SIZE)

    def test_invalid_falls_back_to_default(self):
        for bad in (None, "", "big", object()):
            with self.subTest(bad=bad):
                self.assertEqual(clamp_font_size(bad), DEFAULT_FONT_SIZE)


class _OverlayTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        overlay.close_all()
        self.addCleanup(overlay.close_all)


class TestWindowFlags(_OverlayTestBase):
    def test_look_and_feel(self):
        win = overlay.show_countdown(15, "关机", font_size=48)
        self.assertIsNotNone(win)
        flags = win.windowFlags()
        self.assertTrue(flags & Qt.FramelessWindowHint)          # 无边框
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)         # 置顶
        self.assertTrue(flags & Qt.WindowTransparentForInput)    # 鼠标穿透，不挡操作
        self.assertTrue(win.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_does_not_steal_focus_by_design(self):
        """无焦点策略：倒计时期间用户正在打的字不能跑到浮层上去。"""
        win = overlay.show_countdown(15, "关机")
        self.assertEqual(win.focusPolicy(), Qt.NoFocus)
        self.assertTrue(win.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_never_quits_the_app_when_hidden(self):
        """收起浮层不能被 Qt 当成「最后一个窗口关了」而退出事件循环。

        真踩过的坑：脚本化调用时没有主窗口，浮层就是唯一的窗口，
        `hide()` 触发 quitOnLastWindowClosed → 主线程退出事件循环 →
        后台步骤线程还卡在 ui_call 里等那条永远不会送达的回复。
        """
        win = overlay.show_countdown(15, "关机")
        self.assertFalse(win.testAttribute(Qt.WA_QuitOnClose))


class TestContent(_OverlayTestBase):
    def test_labels_and_note(self):
        win = overlay.show_countdown(15, "关机")
        self.assertEqual(win.main_label.text(), "15 秒后关机")
        self.assertEqual(win.sub_label.text(), NOTE)
        self.assertIn("停止", NOTE)
        self.assertTrue(win.sub_label.isVisible() or not win.isVisible())

    def test_note_tells_the_user_to_press_esc(self):
        """副行是「想反悔按哪里」的唯一提示：Esc 最快，必须写在最前面。"""
        self.assertIn("Esc", NOTE)
        self.assertLess(NOTE.index("Esc"), NOTE.index("停止"))

    def test_fallback_note_has_no_esc(self):
        """Esc 监听没挂上时的退路文案：宁可少提示一句，也别提示一句做不到的。"""
        self.assertIn("停止", NOTE_NO_ESC)
        self.assertNotIn("Esc", NOTE_NO_ESC)

    def test_note_can_be_cleared(self):
        win = overlay.show_countdown(15, "关机", note="")
        self.assertFalse(win.sub_label.isVisible())

    def test_main_text_is_red(self):
        win = overlay.show_countdown(15, "关机")
        style = win._card.styleSheet()
        self.assertIn(overlay.MAIN_COLOR, style)
        self.assertIn(overlay.SUB_COLOR, style)
        # 红色是写死的（用户要求），别被主题色改掉
        self.assertEqual(overlay.MAIN_COLOR.lower(), "#d81e06")

    def test_font_size_and_bold(self):
        win = overlay.show_countdown(15, "关机", font_size=64)
        self.assertEqual(win.font_size, 64)
        self.assertEqual(win.main_label.font().pointSize(), 64)
        self.assertTrue(win.main_label.font().bold())
        # 副行约为主行的 40%，永远比主行小
        self.assertLess(win.sub_label.font().pointSize(), 64)
        self.assertFalse(win.sub_label.font().bold())

    def test_font_size_clamped_on_show(self):
        win = overlay.show_countdown(15, "关机", font_size=99999)
        self.assertEqual(win.font_size, MAX_FONT_SIZE)
        self.assertEqual(win.main_label.font().pointSize(), MAX_FONT_SIZE)


class TestPlacement(_OverlayTestBase):
    def _geo(self):
        return self._app.primaryScreen().availableGeometry()

    def test_bottom_centered(self):
        win = overlay.show_countdown(15, "关机")
        geo = self._geo()
        expected_x = geo.left() + (geo.width() - win.width()) // 2
        self.assertEqual(win.x(), expected_x)                       # 水平居中
        gap = geo.bottom() - (win.y() + win.height())               # 距屏幕底边
        self.assertGreaterEqual(gap, BOTTOM_MARGIN - 2)
        self.assertLess(gap, BOTTOM_MARGIN + 8)
        # 确实在屏幕下方（下半屏）
        self.assertGreater(win.y(), geo.top() + geo.height() // 2)

    def test_recentered_after_font_change(self):
        win = overlay.show_countdown(15, "关机", font_size=16)
        win.set_font_size(48)
        geo = self._geo()
        self.assertEqual(win.x(), geo.left() + (geo.width() - win.width()) // 2)
        self.assertGreaterEqual(geo.bottom() - (win.y() + win.height()),
                                BOTTOM_MARGIN - 2)

    def test_oversized_overlay_aligns_to_left_edge(self):
        """浮层比屏幕还宽时贴左边缘，而不是居中——倒计时的数字在开头，
        居中会把数字截掉，只露出「秒后关机」这种没用的半截。"""
        win = overlay.show_countdown(600, "关机", font_size=MAX_FONT_SIZE)
        geo = self._geo()
        self.assertGreaterEqual(win.x(), geo.left())
        if win.width() > geo.width():
            self.assertEqual(win.x(), geo.left())


class TestLifecycle(_OverlayTestBase):
    def test_singleton_is_reused_and_updated(self):
        first = overlay.show_countdown(15, "关机")
        second = overlay.show_countdown(5, "关机")
        self.assertIs(first, second)
        self.assertEqual(first.main_label.text(), "5 秒后关机")
        self.assertEqual(overlay.active_count(), 1)

    def test_hide_keeps_window_for_reuse(self):
        win = overlay.show_countdown(15, "关机")
        overlay.hide_countdown()
        self.assertEqual(overlay.active_count(), 0)
        self.assertIs(overlay.current(), win)          # 收起 ≠ 销毁
        again = overlay.show_countdown(9, "重启")
        self.assertIs(again, win)
        self.assertEqual(win.main_label.text(), "9 秒后重启")
        self.assertEqual(overlay.active_count(), 1)

    def test_hide_is_idempotent(self):
        overlay.hide_countdown()                        # 没显示过也不该炸
        overlay.show_countdown(3, "关机")
        overlay.hide_countdown()
        overlay.hide_countdown()
        self.assertEqual(overlay.active_count(), 0)

    def test_close_all_drops_reference(self):
        overlay.show_countdown(15, "关机")
        overlay.close_all()
        self.assertIsNone(overlay.current())
        self.assertEqual(overlay.active_count(), 0)
        overlay.close_all()                             # 再来一次也安全

    def test_survives_window_closed_behind_our_back(self):
        """窗口被别处 close 掉后，下次 show 要能重新建一个，而不是往死对象上画。"""
        win = overlay.show_countdown(15, "关机")
        win.close()
        again = overlay.show_countdown(7, "关机")
        self.assertIsNotNone(again)
        self.assertEqual(again.main_label.text(), "7 秒后关机")
        self.assertEqual(overlay.active_count(), 1)

    def test_direct_construction_and_setters(self):
        win = PowerCountdown(24)
        self.assertEqual(win.font_size, 24)
        win.set_countdown(8, "睡眠")
        self.assertEqual(win.main_label.text(), "8 秒后睡眠")
        win.set_font_size(MIN_FONT_SIZE)
        self.assertEqual(win.font_size, MIN_FONT_SIZE)
        win.deleteLater()


class TestDegradation(_OverlayTestBase):
    def test_returns_none_without_application(self):
        """没有 QApplication 时必须干净地返回 None（构造 QWidget 会 abort 进程）。"""
        with mock.patch.object(overlay.QApplication, "instance", return_value=None):
            self.assertIsNone(overlay.show_countdown(15, "关机"))

    def test_returns_none_when_ui_call_explodes(self):
        with mock.patch("app.screenshot_actor.ui_call", side_effect=RuntimeError("桥断了")):
            self.assertIsNone(overlay.show_countdown(15, "关机"))

    def test_hide_swallows_ui_call_errors(self):
        with mock.patch("app.screenshot_actor.ui_call", side_effect=RuntimeError("桥断了")):
            overlay.hide_countdown()        # 不抛即通过


class TestUsageContract(unittest.TestCase):
    def test_default_font_size_is_big(self):
        """用户要求「字大一点」：默认字号得明显大于正文（9~10 pt）。"""
        self.assertGreaterEqual(DEFAULT_FONT_SIZE, 36)


if __name__ == "__main__":
    unittest.main()
