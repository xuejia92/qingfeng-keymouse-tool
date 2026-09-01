"""主窗口尺寸自适应（按显示器分辨率动态缩放）的测试。

只测纯函数 auto_window_size 与 MainWindow._window_size 在模拟屏幕下的输出，
不启动完整窗口（避免 Qt 平台依赖）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui.main_window import auto_window_size


class TestAutoWindowSize(unittest.TestCase):
    def test_2k_base(self):
        """2560x1440（基准）→ 1300x900。"""
        self.assertEqual(auto_window_size(2560, 1440), (1300, 900))

    def test_4k_capped(self):
        """4K 大屏不放大，保持设计尺寸。"""
        self.assertEqual(auto_window_size(3840, 2160), (1300, 900))

    def test_1080p_scaled(self):
        """1080p 等比缩小且不超屏。"""
        w, h = auto_window_size(1920, 1080)
        self.assertLessEqual(w, 1920)
        self.assertLessEqual(h, 1080)
        self.assertGreaterEqual(w, 980)      # 不小于最小宽度
        self.assertGreaterEqual(h, 660)      # 不小于最小高度

    def test_small_screen_floor(self):
        """小屏不缩到最小尺寸以下。"""
        w, h = auto_window_size(1280, 720)
        self.assertEqual((w, h), (980, 660))

    def test_never_exceeds_screen(self):
        """任何合法分辨率下窗口都不超过屏幕。"""
        for sw, sh in [(1920, 1080), (1600, 900), (1366, 768),
                       (1280, 720), (1024, 768), (2560, 1440)]:
            w, h = auto_window_size(sw, sh)
            self.assertLessEqual(w, sw)
            self.assertLessEqual(h, sh)

    def test_invalid_input_falls_back(self):
        """异常输入（0 或负）回退到设计尺寸。"""
        self.assertEqual(auto_window_size(0, 0), (1300, 900))
        self.assertEqual(auto_window_size(-1, 500), (1300, 900))


class TestMainWindowScreenSize(unittest.TestCase):
    def test_uses_available_geometry(self):
        """_window_size 应取屏幕可用区域（排除任务栏）而非全屏尺寸。"""
        from app.ui.main_window import MainWindow

        fake_screen = mock.Mock()
        fake_screen.availableGeometry.return_value = mock.Mock(
            width=mock.Mock(return_value=1920), height=mock.Mock(return_value=1040))
        win = MainWindow.__new__(MainWindow)   # 不跑 __init__，只测尺寸计算
        with mock.patch.object(MainWindow, "screen", return_value=fake_screen):
            w, h = win._window_size()
        self.assertLessEqual(w, 1920)
        self.assertLessEqual(h, 1040)


class TestLogPanel(unittest.TestCase):
    """日志面板：展开/收缩切换文本区显隐、文本追加、摘要显示。"""

    @classmethod
    def setUpClass(cls):
        """LogPanel 是 QWidget，需要 QApplication 实例。"""
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_toggle_expands_and_collapses(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        self.assertFalse(panel.is_expanded())
        self.assertTrue(panel._text.isHidden())            # 初始收缩，文本区隐藏

        panel.set_expanded(True)
        self.assertTrue(panel.is_expanded())
        self.assertFalse(panel._text.isHidden())           # 展开后文本区显示

        panel.set_expanded(False)
        self.assertFalse(panel.is_expanded())
        self.assertTrue(panel._text.isHidden())

    def test_expanded_changed_signal(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        states = []
        panel.expandedChanged.connect(states.append)
        panel.set_expanded(True)
        panel.set_expanded(False)
        self.assertEqual(states, [True, False])

    def test_append_and_summary(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        panel.append("第一条日志")
        panel.append("第二条日志")
        self.assertIn("第一条日志", panel._text.toPlainText())
        self.assertIn("第二条日志", panel._text.toPlainText())

        panel.set_summary("鼠标连点 · 找图:登录")
        self.assertIn("鼠标连点", panel._header.text())

    def test_clear_button_and_method(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        self.assertEqual(panel.clear_btn.text(), "🗑 清空日志")
        panel.append("要清掉的日志")
        panel.clear()
        self.assertEqual(panel._text.toPlainText(), "")

    def test_clear_on_run_property_and_signal(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        states = []
        panel.clearOnRunChanged.connect(states.append)
        self.assertFalse(panel.clear_on_run)

        panel.clear_on_run = True
        self.assertTrue(panel.clear_on_run)
        self.assertTrue(panel.clear_on_run_box.isChecked())
        self.assertEqual(states, [True])

        panel.clear_on_run = False
        self.assertFalse(panel.clear_on_run)
        self.assertEqual(states, [True, False])


if __name__ == "__main__":
    unittest.main()
