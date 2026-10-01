# -*- coding: utf-8 -*-
"""无边框主窗口（app/ui/frameless_window.py）测试（2026-10-01）。

对应用户需求：「整个客户端四个角变为圆角，客户端的标题栏也要收到主题影响」。
本机是 Windows 10，DWM 既不能给系统标题栏上色、也没有 Win11 的圆角开关，
所以主窗口改成无边框 + 自绘标题栏，这里把关键契约钉住：

- 窗口无边框 + 背景透明（圆角外的四角必须真的透明，否则会出现四个方块）；
- 圆角是**两段拼**的：卡片管外圈边框 + 圆角，标题栏/状态栏管内侧圆角
  （Qt 的 QSS border-radius 不裁剪子控件，这一点很容易被改坏）；
- 标题栏颜色全部来自主题令牌，切主题必须跟着变；
- 最大化时圆角/边框归 0，否则屏幕四角会露缝；
- 贴边缩放热区的几何判定与光标形状；
- 标题栏按钮是自绘图形（不是文字/字体图标），点最小化/关闭要真的生效。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QStatusBar,
                               QVBoxLayout, QWidget)

from app.ui import theme
from app.ui import frameless_window as fw


class FramelessCase(unittest.TestCase):
    """基类：保证有 QApplication，每个用例从浅色默认主题开始。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        theme.clear_registry()
        theme.apply_theme("light")

    def tearDown(self):
        # 本文件有用例会把主题切到 silicon_dark 验证标题栏跟随；
        # 全局主题是模块级状态，不还原就会污染后面跑的用例
        # （踩过：test_main_window 的日志面板用例断言的是浅色主色/正文色）。
        theme.clear_registry()
        theme.apply_theme("light")

    def _win(self, w: int = 900, h: int = 600) -> fw.FramelessMainWindow:
        win = fw.FramelessMainWindow()
        win.setWindowTitle("测试窗口")
        win.resize(w, h)
        win.show()
        self._app.processEvents()
        self.addCleanup(win.close)
        return win


class TestChromeStructure(FramelessCase):
    def test_window_is_frameless_and_translucent(self):
        win = self._win()
        self.assertTrue(win.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(win.testAttribute(Qt.WA_TranslucentBackground))

    def test_card_title_bar_body_present(self):
        win = self._win()
        self.assertEqual(win._card.objectName(), "winCard")
        self.assertIs(win.centralWidget(), win._card)
        self.assertEqual(win.title_bar().objectName(), "winTitleBar")
        self.assertEqual(win.title_bar().height(), fw.TITLE_BAR_H)
        self.assertEqual(win.body_layout().parentWidget().objectName(), "winBody")

    def test_title_bar_has_icon_title_and_three_caption_buttons(self):
        win = self._win()
        self.assertEqual(win._title_label.text(), "测试窗口")
        self.assertEqual([b.glyph() for b in win._caption_btns],
                         ["min", "max", "close"])
        for btn in win._caption_btns:
            self.assertIsInstance(btn, fw.CaptionButton)
            self.assertEqual(btn.text(), "", "标题栏按钮不该用文字/字体图标")

    def test_caption_buttons_use_own_paint(self):
        """自绘按钮：图形不依赖字体（缺字就是方框，中文 Windows 上尤其容易踩）。"""
        win = self._win()
        self.assertTrue(type(win._btn_close).paintEvent is fw.CaptionButton.paintEvent)

    def test_body_layout_is_where_content_goes(self):
        win = self._win()
        marker = QWidget()
        win.body_layout().addWidget(marker)
        self._app.processEvents()
        self.assertIs(win.body_layout().itemAt(0).widget(), marker)

    def test_status_bar_contract(self):
        """状态栏由基类提供、命名为 winStatus（底部圆角靠这个 objectName）。"""
        win = self._win()
        sb = win.status_bar()
        self.assertIsInstance(sb, QStatusBar)
        self.assertEqual(sb.objectName(), "winStatus")
        self.assertFalse(sb.isSizeGripEnabled(), "系统抓手会破坏圆角")

    def test_set_window_title_syncs_label(self):
        win = self._win()
        win.setWindowTitle("新标题")
        self.assertEqual(win._title_label.text(), "新标题")

    def test_title_labels_do_not_eat_mouse(self):
        """图标/标题不吃鼠标事件，按在文字上也能直接拖窗口。"""
        win = self._win()
        for lbl in (win._icon_label, win._title_label):
            self.assertTrue(lbl.testAttribute(Qt.WA_TransparentForMouseEvents))


class TestRoundedCorners(FramelessCase):
    def test_card_qss_has_radius_and_border(self):
        qss = fw.window_qss(False)
        self.assertIn(f"border-radius: {fw.CORNER_RADIUS}px", qss)
        self.assertIn(f"{fw.BORDER}px solid", qss)
        self.assertIn("QFrame#winCard", qss)

    def test_top_and_bottom_children_carry_inner_radius(self):
        """圆角是两段拼的：Qt 的 QSS border-radius **不裁剪子控件**。

        卡片画外圈边框 + 圆角；真正盖住四角的是标题栏（上）和状态栏（下），
        所以它们必须各带一个 `CORNER_RADIUS - BORDER` 的内圆角嵌在边框里侧。
        少了任何一段，窗口四角就会露出方角。
        """
        qss = fw.window_qss(False)
        inner = fw.CORNER_RADIUS - fw.BORDER
        self.assertIn(f"border-top-left-radius: {inner}px", qss)
        self.assertIn(f"border-top-right-radius: {inner}px", qss)
        self.assertIn(f"border-bottom-left-radius: {inner}px", qss)
        self.assertIn(f"border-bottom-right-radius: {inner}px", qss)

    def test_maximized_drops_radius_and_border(self):
        """最大化时圆角/边框归 0，否则贴着屏幕边缘会露出一圈缝。"""
        qss = fw.window_qss(True)
        self.assertIn("border-radius: 0px", qss)
        self.assertNotIn("border-radius: 12px", qss)
        self.assertIn("border: none", qss)

    def test_window_qss_uses_live_maximized_state(self):
        win = self._win()
        self.assertIn("border-radius: 12px", win.window_qss())


class TestThemeFollows(FramelessCase):
    def test_title_bar_colors_come_from_theme_tokens(self):
        for key in ("light", "warm_paper", "silicon_dark"):
            theme.apply_theme(key)
            t = theme.THEMES[key]
            qss = fw.window_qss(False)
            self.assertIn(t["panel_bg"], qss, f"{key}: 标题栏底色没跟主题")
            self.assertIn(t["window_bg"], qss, f"{key}: 卡片底色没跟主题")
            self.assertIn(t["dialog_border"], qss, f"{key}: 边框没跟主题")
            self.assertIn(t["text"], qss, f"{key}: 标题文字没跟主题")

    def test_switching_theme_changes_title_bar_color(self):
        theme.apply_theme("light")
        light_qss = fw.window_qss(False)
        theme.apply_theme("night")
        night_qss = fw.window_qss(False)
        self.assertNotEqual(light_qss, night_qss)
        self.assertNotIn(theme.THEMES["light"]["panel_bg"], night_qss)

    def test_caption_button_glyph_color_is_live(self):
        """按钮图形不走 QSS，paintEvent 里现取 theme.token，所以切主题只需 update()。"""
        import inspect
        src = inspect.getsource(fw.CaptionButton.paintEvent)
        self.assertIn('theme.token("text")', src)
        self.assertIn('theme.token("danger" if is_close else "hover_bg")', src)


class TestResizeEdges(FramelessCase):
    def test_edges_at_borders(self):
        win = self._win(900, 600)
        w, h, b = win.width(), win.height(), fw.RESIZE_BAND
        self.assertEqual(win.edges_at(QPoint(3, h // 2)), Qt.LeftEdge)
        self.assertEqual(win.edges_at(QPoint(w - 3, h // 2)), Qt.RightEdge)
        self.assertEqual(win.edges_at(QPoint(w // 2, 3)), Qt.TopEdge)
        self.assertEqual(win.edges_at(QPoint(w // 2, h - 3)), Qt.BottomEdge)
        self.assertEqual(win.edges_at(QPoint(3, 3)),
                         Qt.LeftEdge | Qt.TopEdge)
        self.assertEqual(win.edges_at(QPoint(w - 3, h - 3)),
                         Qt.RightEdge | Qt.BottomEdge)
        self.assertEqual(win.edges_at(QPoint(w // 2, h // 2)), Qt.Edges())
        self.assertEqual(win.edges_at(QPoint(-5, 10)), Qt.Edges())
        self.assertEqual(win.edges_at(QPoint(w + 5, 10)), Qt.Edges())

    def test_edges_empty_when_maximized(self):
        win = self._win(900, 600)
        win.showMaximized()
        self._app.processEvents()
        self.assertEqual(win.edges_at(QPoint(2, 2)), Qt.Edges())

    def test_cursor_shapes(self):
        self.assertIsNone(fw._cursor_for(Qt.Edges()))
        self.assertEqual(fw._cursor_for(Qt.LeftEdge), Qt.SizeHorCursor)
        self.assertEqual(fw._cursor_for(Qt.BottomEdge), Qt.SizeVerCursor)
        self.assertEqual(fw._cursor_for(Qt.LeftEdge | Qt.TopEdge),
                         Qt.SizeFDiagCursor)
        self.assertEqual(fw._cursor_for(Qt.RightEdge | Qt.TopEdge),
                         Qt.SizeBDiagCursor)
        self.assertEqual(fw._cursor_for(Qt.RightEdge | Qt.BottomEdge),
                         Qt.SizeFDiagCursor)
        self.assertEqual(fw._cursor_for(Qt.LeftEdge | Qt.BottomEdge),
                         Qt.SizeBDiagCursor)

    def test_event_filter_ignores_foreign_events(self):
        win = self._win()
        # 不是鼠标事件 / 不是本窗口的控件：都要原样放行
        self.assertFalse(win.eventFilter(win, QEvent(QEvent.KeyPress)))
        other = QWidget()
        other.show()
        self.addCleanup(other.close)
        self.assertFalse(win.eventFilter(other, QEvent(QEvent.MouseMove)))


class TestCaptionActions(FramelessCase):
    def test_close_button_closes_window(self):
        class Recorder(fw.FramelessMainWindow):
            closed = False

            def closeEvent(self, ev):
                Recorder.closed = True
                ev.accept()

        win = Recorder()
        win.show()
        self.addCleanup(win.close)
        Recorder.closed = False
        win._btn_close.click()
        self.assertTrue(Recorder.closed)

    def test_max_button_toggles_state(self):
        win = self._win()
        win._btn_max.click()
        self._app.processEvents()
        self.assertTrue(win.isMaximized())
        self.assertEqual(win._btn_max.glyph(), "restore")
        win._btn_max.click()
        self._app.processEvents()
        self.assertFalse(win.isMaximized())
        self.assertEqual(win._btn_max.glyph(), "max")

    def test_double_click_title_bar_toggles_maximize(self):
        win = self._win()
        win.setWindowTitle("x")
        win._title_bar.mouseDoubleClickEvent(_DblClick())
        self._app.processEvents()
        self.assertTrue(win.isMaximized())

    def test_show_minimized_is_wired(self):
        """最小化按钮接到 showMinimized（信号连的是绑定方法，所以要用子类替身）。"""
        class Recorder(fw.FramelessMainWindow):
            calls = 0

            def showMinimized(self):
                Recorder.calls += 1

        win = Recorder()
        win.show()
        self.addCleanup(win.close)
        Recorder.calls = 0
        win._btn_min.click()
        self.assertEqual(Recorder.calls, 1)


class _DblClick:
    """双击事件替身（只用到 button()/accept()）。"""

    def button(self):
        return Qt.LeftButton

    def accept(self):
        pass


class _PointF:
    def __init__(self, p):
        self._p = p

    def toPoint(self):
        return self._p


class _MouseEv:
    """鼠标事件替身：只实现 FramelessMainWindow / _TitleBar 真正用到的那几个方法。"""

    def __init__(self, kind, global_pos, button=Qt.LeftButton,
                 buttons=Qt.NoButton):
        self._kind = kind
        self._pos = global_pos
        self._button = button
        self._buttons = buttons
        self.accepted = False

    def type(self):
        return self._kind

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def globalPosition(self):
        return _PointF(self._pos)

    def accept(self):
        self.accepted = True

    def ignore(self):
        pass


class _FakeWindowHandle:
    """windowHandle() 替身：记录 startSystemMove / startSystemResize 的调用。"""

    def __init__(self):
        self.moves = 0
        self.resizes = []

    def startSystemMove(self):
        self.moves += 1
        return True

    def startSystemResize(self, edges):
        self.resizes.append(edges)
        return True


class TestSystemMoveResize(FramelessCase):
    """拖动/缩放一律交给系统（startSystemMove / startSystemResize），原生行为才保得住。"""

    def test_title_bar_press_starts_system_move(self):
        win = self._win()
        fake = _FakeWindowHandle()
        win.windowHandle = lambda: fake
        win._title_bar.mousePressEvent(
            _MouseEv(QEvent.MouseButtonPress, win.mapToGlobal(QPoint(40, 20))))
        self.assertEqual(fake.moves, 1)

    def test_title_bar_falls_back_to_manual_drag(self):
        """startSystemMove 不可用时退回自己拖（少数平台/环境会返回 False）。"""
        win = self._win()
        fake = _FakeWindowHandle()
        fake.startSystemMove = lambda: False
        win.windowHandle = lambda: fake
        win._title_bar.mousePressEvent(
            _MouseEv(QEvent.MouseButtonPress, win.mapToGlobal(QPoint(40, 20))))
        self.assertIsNotNone(win._title_bar._drag_offset)

    def test_border_press_starts_system_resize(self):
        win = self._win(900, 600)
        fake = _FakeWindowHandle()
        win.windowHandle = lambda: fake
        ev = _MouseEv(QEvent.MouseButtonPress,
                      win.mapToGlobal(QPoint(2, win.height() // 2)))
        self.assertTrue(win.eventFilter(win, ev), "贴边按下要被过滤器吃掉")
        self.assertEqual(fake.resizes, [Qt.LeftEdge])

    def test_center_press_is_not_resize(self):
        win = self._win(900, 600)
        fake = _FakeWindowHandle()
        win.windowHandle = lambda: fake
        ev = _MouseEv(QEvent.MouseButtonPress,
                      win.mapToGlobal(QPoint(win.width() // 2, win.height() // 2)))
        self.assertFalse(win.eventFilter(win, ev))
        self.assertEqual(fake.resizes, [])

    def test_hover_near_border_sets_resize_cursor(self):
        win = self._win(900, 600)
        win.eventFilter(win, _MouseEv(QEvent.MouseMove,
                                      win.mapToGlobal(QPoint(2, win.height() // 2))))
        self.assertEqual(win.cursor().shape(), Qt.SizeHorCursor)
        win.eventFilter(win, _MouseEv(QEvent.MouseMove,
                                      win.mapToGlobal(QPoint(win.width() // 2,
                                                            win.height() // 2))))
        self.assertEqual(win.cursor().shape(), Qt.ArrowCursor)


class TestMainWindowWiring(unittest.TestCase):
    """MainWindow 的接线契约（源码级：主窗口构造会拉线程/托盘，单测不实例化）。"""

    def _src(self) -> str:
        import app.ui.main_window as mw
        with open(mw.__file__, encoding="utf-8") as fh:
            return fh.read()

    def test_main_window_is_frameless(self):
        src = self._src()
        self.assertIn("class MainWindow(FramelessMainWindow)", src)
        self.assertNotIn("class MainWindow(QMainWindow)", src)

    def test_status_bar_hook_returns_card_bar(self):
        src = self._src()
        self.assertIn("def statusBar(self) -> QStatusBar:", src)
        self.assertIn("return self.status_bar()", src)

    def test_content_goes_into_body_layout_with_status_bar_last(self):
        src = self._src()
        self.assertIn("body = self.body_layout()", src)
        self.assertIn("body.addWidget(self.status_bar())", src)

    def test_window_qss_appends_button_and_tab_rules(self):
        src = self._src()
        self.assertIn("theme.build_button_qss() + theme.build_tab_qss()", src)
        self.assertIn("super().window_qss()", src)

    def test_log_expand_skips_resize_when_maximized(self):
        src = self._src()
        body = src.split("def _on_log_expanded")[1].split("def ")[0]
        self.assertIn("if self.isMaximized():", body)


if __name__ == "__main__":
    unittest.main()
