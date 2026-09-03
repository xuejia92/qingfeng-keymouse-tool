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


class TestUpdateManualFlow(unittest.TestCase):
    """手动更新状态机：检测到新版本只提示，点「下载更新」才开始下载。

    状态流转 idle -> available（下载更新）-> downloading（下载中…，点按钮
    收起/展开进度条）-> ready（重启升级）/ failed（重新下载）。下载由用户
    触发，任何分支都不允许自动开始——这是行为约定，必须钉测试。
    """

    def _win(self):
        """不跑 __init__ 的 MainWindow，只挂状态机用到的属性（widget 全 Mock）。"""
        from app.ui.main_window import MainWindow
        win = MainWindow.__new__(MainWindow)
        win.cfg = mock.Mock(version="1.0.0")
        win.update_dot = mock.Mock()
        win.update_hint = mock.Mock()
        win.update_btn = mock.Mock()
        win.update_progress = mock.Mock()
        win._progress_visible = True
        win._pending_update = None
        win._downloaded_file = None
        win._update_state = "idle"
        return win

    def test_fetched_shows_hint_without_downloading(self):
        """检测到新版本：进 available，提示 +「下载更新」按钮，不启动下载。"""
        win = self._win()
        with mock.patch.object(type(win), "_start_download") as dl:
            win._on_version_fetched(("v9.9.9", ["https://x/1.exe"]))
            self.assertEqual(win._update_state, "available")
            dl.assert_not_called()                       # 手动模式：绝不自动下载
        win.update_hint.setText.assert_called_with("发现新版本 v9.9.9")
        win.update_btn.setText.assert_called_with("下载更新")

    def test_older_remote_stays_idle(self):
        """远端版本不比本地新：保持 idle，不提示不下载。"""
        win = self._win()
        win.cfg = mock.Mock(version="3.0.2")
        with mock.patch.object(type(win), "_start_download") as dl:
            win._on_version_fetched(("v3.0.2", ["https://x/1.exe"]))
            self.assertEqual(win._update_state, "idle")
            dl.assert_not_called()

    def test_click_available_starts_download(self):
        """available 状态点击按钮：进入 downloading 并真正启动下载。"""
        win = self._win()
        win._pending_update = ("v9.9.9", ["https://x/1.exe"])
        win._update_state = "available"
        with mock.patch.object(type(win), "_start_download") as dl:
            win._restart_upgrade()
            dl.assert_called_once_with(["https://x/1.exe"])
        self.assertEqual(win._update_state, "downloading")
        win.update_btn.setText.assert_called_with("下载中…")

    def test_click_downloading_toggles_progress(self):
        """下载中点击按钮：只切换进度条显隐，不影响下载。"""
        win = self._win()
        win._update_state = "downloading"
        win._progress_visible = True
        win._restart_upgrade()
        self.assertFalse(win._progress_visible)
        win.update_progress.setVisible.assert_called_with(False)

    def test_download_completed_ready(self):
        """下载完成：ready 状态 +「重启升级」按钮。"""
        win = self._win()
        win._pending_update = ("v9.9.9", [])
        win._on_download_completed(r"C:\tmp\x.exe")
        self.assertEqual(win._update_state, "ready")
        self.assertEqual(win._downloaded_file, r"C:\tmp\x.exe")
        win.update_btn.setText.assert_called_with("重启升级")

    def test_failed_click_retries_download(self):
        """下载失败后点击「重新下载」：重新进入 downloading 并重试。"""
        win = self._win()
        win._pending_update = ("v9.9.9", ["https://x/1.exe"])
        win._update_state = "failed"
        with mock.patch.object(type(win), "_start_download") as dl:
            win._restart_upgrade()
            dl.assert_called_once()
        self.assertEqual(win._update_state, "downloading")

    def test_non_idle_not_retriggered(self):
        """非 idle 状态下再次检测到新版本：不重复触发、不覆盖在途流程。"""
        win = self._win()
        win._update_state = "downloading"
        win._pending_update = ("v9.9.9", ["u1"])
        win._on_version_fetched(("v9.9.9", ["u1"]))
        self.assertEqual(win._update_state, "downloading")
        self.assertEqual(win._pending_update, ("v9.9.9", ["u1"]))


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
        panel.print_only = False   # 关闭「只显示打印输出」，让普通日志可见
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
        panel.print_only = False
        panel.append("要清掉的日志")
        panel.clear()
        self.assertEqual(panel._text.toPlainText(), "")

    def test_clear_on_run_property_and_signal(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        states = []
        panel.clearOnRunChanged.connect(states.append)
        self.assertTrue(panel.clear_on_run)   # 默认勾选

        panel.clear_on_run = False
        self.assertFalse(panel.clear_on_run)
        self.assertFalse(panel.clear_on_run_box.isChecked())
        self.assertEqual(states, [False])

        panel.clear_on_run = True
        self.assertTrue(panel.clear_on_run)
        self.assertEqual(states, [False, True])

    def test_print_only_property_and_signal(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        states = []
        panel.printOnlyChanged.connect(states.append)
        self.assertTrue(panel.print_only)   # 默认只显示打印输出

        panel.print_only = False
        self.assertFalse(panel.print_only)
        self.assertEqual(states, [False])

        panel.print_only = True
        self.assertTrue(panel.print_only)
        self.assertEqual(states, [False, True])

    def test_print_only_filter_hides_normal_logs(self):
        from app.ui.log_panel import LogPanel
        panel = LogPanel()
        self.assertTrue(panel.print_only)

        panel.append("系统日志", kind="log")
        panel.append("打印内容", kind="print")
        text = panel._text.toPlainText()
        self.assertNotIn("系统日志", text)
        self.assertIn("打印内容", text)

        panel.print_only = False
        text = panel._text.toPlainText()
        self.assertIn("系统日志", text)
        self.assertIn("打印内容", text)

    def test_print_output_rendered_blue(self):
        from app.ui.log_panel import LogPanel
        from PySide6.QtGui import QTextCursor
        panel = LogPanel()
        panel.print_only = False   # 关闭过滤，让普通日志也渲染出来
        panel.append("普通日志", kind="log")
        panel.append("打印的值", kind="print")

        # 逐字符收集颜色：普通日志默认色，打印输出蓝色
        colors = {}
        doc = panel._text.document()
        cursor = panel._text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        while True:
            ch = doc.characterAt(cursor.position())
            if ch and ch != "\u2029":
                colors.setdefault(ch, cursor.charFormat().foreground().color().name())
            if not cursor.movePosition(QTextCursor.NextCharacter):
                break
        self.assertEqual(colors["普"], "#24292f")
        self.assertEqual(colors["打"], "#1668a8")

    def test_print_raw_rendered_without_newline_and_blue(self):
        """「原始输出」：不自动换行、蓝色显示、不被「只显示打印输出」过滤。"""
        from app.ui.log_panel import LogPanel
        from PySide6.QtGui import QTextCursor
        panel = LogPanel()
        self.assertTrue(panel.print_only)
        panel.append("A", kind="print_raw")
        panel.append("B", kind="print_raw")
        panel.append("系统日志", kind="log")

        text = panel._text.toPlainText()
        self.assertEqual(text, "AB")            # 连续原始输出拼接，不换行
        self.assertNotIn("系统日志", text)      # print_only 仍过滤普通日志

        cursor = panel._text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        self.assertEqual(cursor.charFormat().foreground().color().name(), "#1668a8")


if __name__ == "__main__":
    unittest.main()
