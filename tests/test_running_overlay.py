# -*- coding: utf-8 -*-
"""运行中流程的悬浮窗（app/running_overlay.py）测试。

验证：refresh(overview) 区分三种来源（分组异步/分组全部/单个流程）；
每条「流程名 [热键]」、无热键写 [无]；鼠标穿透；空列表隐藏；截断；
行复用；close 销毁；主窗口接线契约。
外观可配置（2026-09-27，设置页：字号/文字色/背景色/九宫格位置/显示开关）：
set_config 注入替身配置后样式与位置跟随，非法值回退默认。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import running_overlay


def _ov(source="", group="", flows=()):
    return {"source": source, "group": group, "flows": list(flows)}


class TestRunningOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self):
        running_overlay.close()
        running_overlay.set_config(None)   # 替身配置不跨用例泄漏

    def test_refresh_creates_and_shows(self):
        self.assertFalse(running_overlay.is_visible())
        running_overlay.refresh(_ov("group_async", "流放之路2",
                                    [("自动放技能", "ctrl+q", True)]))
        self.assertTrue(running_overlay.is_visible())

    def test_row_shows_group_and_flow_on_one_line(self):
        """每行 = 「分组名 - 流程名  [热键]」：分组名和流程名在同一行、用 - 分隔。"""
        running_overlay.refresh(_ov("group_async", "流放之路2",
                                    [("自动放技能", "ctrl+q", True)]))
        self.assertEqual(running_overlay._overlay._rows[0].text(),
                         "流放之路2 - 自动放技能  [Ctrl+Q]")

    def test_row_without_group_shows_weifenzu(self):
        """没有分组的流程显示「未分组 - 流程名」。"""
        running_overlay.refresh(_ov("single", "", [("截图", "", False)]))
        self.assertEqual(running_overlay._overlay._rows[0].text(),
                         "未分组 - 截图  [无]")

    def test_no_separate_title_line(self):
        """不再有单独的分组标题行（2026-10-01 用户要求去掉，改成每行自带分组名）。"""
        running_overlay.refresh(_ov("group_all", "组", [("甲", "f6", True)]))
        self.assertFalse(hasattr(running_overlay._overlay, "_title"))

    def test_group_rows_each_show_group_name(self):
        """分组来源：每行都带分组名（原来只写流程名，用户反馈"没有显示分组名称"）。"""
        running_overlay.refresh(_ov("group_all", "流放之路2",
                                    [("自动放技能", "ctrl+q", True),
                                     ("半血回血", "ctrl+q", True)]))
        texts = [r.text() for r in running_overlay._overlay._rows
                 if not r.isHidden()]
        self.assertEqual(texts, ["流放之路2 - 自动放技能  [Ctrl+Q]",
                                 "流放之路2 - 半血回血  [Ctrl+Q]"])

    def test_empty_flows_hides(self):
        running_overlay.refresh(_ov("group_all", "流放之路2", [("甲", "x", True)]))
        running_overlay.refresh(_ov())
        self.assertFalse(running_overlay.is_visible())

    def test_empty_when_none_created_does_nothing(self):
        running_overlay.refresh(_ov())
        self.assertIsNone(running_overlay._overlay)

    def test_truncates_beyond_max(self):
        names = [(f"流程{i}", "", False) for i in range(30)]
        running_overlay.refresh(_ov("group_all", "组", names))
        rows = [r for r in running_overlay._overlay._rows if not r.isHidden()]
        self.assertEqual(len(rows), running_overlay.MAX_VISIBLE)
        self.assertIn("…等", rows[-1].text())

    def test_row_widgets_reused(self):
        running_overlay.refresh(_ov("single", "", [("甲", "", False),
                                                   ("乙", "", False)]))
        win = running_overlay._overlay
        first = win._rows[0]
        running_overlay.refresh(_ov("single", "", [("乙", "", False)]))
        self.assertIs(win._rows[0], first)

    def test_translucent_background(self):
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        self.assertTrue(win.testAttribute(Qt.WA_TranslucentBackground))

    def test_default_style_in_stylesheet(self):
        """默认配置：透明背景 + 默认高亮绿 + 16px（只有一种文字样式）。"""
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        ss = win.styleSheet()
        self.assertIn("background: transparent", ss)
        self.assertEqual(ss.count("#00e676"), 1)        # 分组名与流程名共用一种样式
        self.assertEqual(ss.count("font-size: 16px"), 1)
        self.assertNotIn("font-family", ss)             # 默认不指定字体
        # 透明背景不加内边距
        self.assertEqual(win._lay.contentsMargins().left(), 0)

    def test_single_text_style_from_config(self):
        """分组标题与流程名称已合并成一种文字样式（设置页也只剩一组）。"""
        class _Cfg:
            run_overlay_enabled = True
            run_overlay_title_font_size = 24        # 旧字段仍在，但不再参与显示
            run_overlay_title_text_color = "#ff5500"
            run_overlay_flow_font_size = 18
            run_overlay_flow_font_family = "Microsoft YaHei"
            run_overlay_flow_text_color = "#00e676"
            run_overlay_bg_color = ""
            run_overlay_pos = "top_left"
        running_overlay.set_config(_Cfg())
        running_overlay.refresh(_ov("group_async", "游戏组",
                                    [("自动放技能", "", True)]))
        ss = running_overlay._overlay.styleSheet()
        self.assertIn("QLabel#flow { color: #00e676; font-size: 18px;"
                      ' font-family: "Microsoft YaHei"; }', ss)
        self.assertNotIn("#ff5500", ss)     # 旧的标题色不再使用
        running_overlay.set_config(None)

    def test_style_follows_config(self):
        """设置页改了字号/字体/颜色/背景：样式跟着变（set_config 注入替身配置）。"""
        class _Cfg:
            run_overlay_enabled = True
            run_overlay_flow_font_size = 28
            run_overlay_flow_font_family = "Microsoft YaHei"
            run_overlay_flow_text_color = "#ff5500"
            run_overlay_bg_color = "#11223344"
            run_overlay_pos = "bottom_right"
        running_overlay.set_config(_Cfg())
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        ss = win.styleSheet()
        self.assertEqual(ss.count("font-size: 28px"), 1)
        self.assertEqual(ss.count('font-family: "Microsoft YaHei"'), 1)
        self.assertEqual(ss.count("#ff5500"), 1)
        self.assertIn("background: rgba(17,34,51,68)", ss)   # #11223344 -> rgba
        # 有底色卡片时带内边距
        self.assertGreater(win._lay.contentsMargins().left(), 0)
        running_overlay.set_config(None)

    def test_no_font_family_omits_declaration(self):
        """没选字体（默认）时 QSS 里不出现 font-family，走系统默认。"""
        style = {"size": 16, "family": "", "color": "#00e676"}
        ss = running_overlay.build_stylesheet(style, "")
        self.assertNotIn("font-family", ss)

    def test_qss_font_family_quotes_name(self):
        """字体名含空格要加引号；注入的引号被剥掉，防 QSS 破坏。"""
        self.assertEqual(running_overlay._qss_font_family("Microsoft YaHei"),
                         ' font-family: "Microsoft YaHei";')
        self.assertEqual(running_overlay._qss_font_family('Mi"cro'),
                         ' font-family: "Micro";')
        self.assertEqual(running_overlay._qss_font_family(""), "")
        self.assertEqual(running_overlay._qss_font_family(None), "")

    def test_config_change_refreshes_visible_overlay(self):
        """浮层正在显示时 set_config 立即按新样式重刷（不用等下次状态变化）。"""
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        class _Cfg:
            run_overlay_enabled = True
            run_overlay_flow_font_size = 40
            run_overlay_flow_text_color = "#00e676"
            run_overlay_bg_color = ""
            run_overlay_pos = "top_left"
        running_overlay.set_config(_Cfg())
        self.assertIn("font-size: 40px", win.styleSheet())
        self.assertTrue(running_overlay.is_visible())
        running_overlay.set_config(None)

    def test_disabled_in_config_hides(self):
        """设置里关闭「显示正在运行的分组和流程」：有流程运行也不显示。"""
        class _Cfg:
            run_overlay_enabled = False
            run_overlay_bg_color = ""
            run_overlay_pos = "top_left"
        running_overlay.set_config(_Cfg())
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        self.assertFalse(running_overlay.is_visible())
        running_overlay.set_config(None)

    def test_invalid_config_values_fall_back(self):
        """手改配置填了非法值：回退默认，不让浮层挂掉（只有一种文字样式）。"""
        class _Cfg:
            run_overlay_enabled = True
            run_overlay_flow_font_size = "abc"           # 非法字号 -> 16
            run_overlay_flow_text_color = "#ff8800"      # 合法，保留
            run_overlay_bg_color = "#zzz"
            run_overlay_pos = "somewhere"                # 非法位置
        running_overlay.set_config(_Cfg())
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        ss = win.styleSheet()
        self.assertIn("QLabel#flow { color: #ff8800; font-size: 16px;", ss)
        self.assertIn("background: transparent", ss)  # 非法背景回退透明
        self.assertEqual(win._pos_key, "top_left")
        running_overlay.set_config(None)

    def test_invalid_color_falls_back_to_default(self):
        """非法文字色回退默认高亮绿。"""
        class _Cfg:
            run_overlay_enabled = True
            run_overlay_flow_font_size = 999             # 越界 -> 夹到 72
            run_overlay_flow_text_color = "javascript:alert(1)"   # 非法 -> 默认
            run_overlay_bg_color = ""
            run_overlay_pos = "bottom_right"
        running_overlay.set_config(_Cfg())
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        ss = win.styleSheet()
        self.assertIn("QLabel#flow { color: #00e676; font-size: 72px;", ss)
        self.assertEqual(win._pos_key, "bottom_right")
        running_overlay.set_config(None)

    def test_flags_frameless_tool_on_top_mouse_transparent(self):
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        win = running_overlay._overlay
        flags = win.windowFlags()
        self.assertTrue(flags & Qt.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.Tool)
        self.assertTrue(win.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_close_clears_ref(self):
        running_overlay.refresh(_ov("single", "", [("甲", "", False)]))
        running_overlay.close()
        self.assertIsNone(running_overlay._overlay)


class TestPositionFor(unittest.TestCase):
    """九宫格定位：area 为可用区，m=EDGE_MARGIN。"""

    def _area(self):
        from PySide6.QtCore import QRect
        return QRect(0, 0, 1000, 800)

    def test_nine_positions(self):
        from app import running_overlay as ro
        area = self._area()
        w, h, m = 100, 50, ro.EDGE_MARGIN
        cases = {
            "top_left": (m, m),
            "top_center": ((1000 - w) // 2, m),
            "top_right": (1000 - w - m, m),
            "middle_left": (m, (800 - h) // 2),
            "center": ((1000 - w) // 2, (800 - h) // 2),
            "middle_right": (1000 - w - m, (800 - h) // 2),
            "bottom_left": (m, 800 - h - m),
            "bottom_center": ((1000 - w) // 2, 800 - h - m),
            "bottom_right": (1000 - w - m, 800 - h - m),
        }
        for key, expect in cases.items():
            self.assertEqual(ro.position_for(key, w, h, area), expect, key)

    def test_unknown_key_falls_back_top_left(self):
        from app import running_overlay as ro
        m = ro.EDGE_MARGIN
        self.assertEqual(ro.position_for("bogus", 100, 50, self._area()),
                         (m, m))

    def test_font_size_clamped(self):
        from app import running_overlay as ro
        self.assertEqual(ro.clamp_font_size(3), 8)
        self.assertEqual(ro.clamp_font_size(999), 72)
        self.assertEqual(ro.clamp_font_size("abc"), 16)
        self.assertEqual(ro.clamp_font_size(None), 16)


class TestHotkeyDisplay(unittest.TestCase):
    def test_display(self):
        self.assertEqual(running_overlay._hotkey_display("ctrl+alt+q"), "Ctrl+Alt+Q")
        self.assertEqual(running_overlay._hotkey_display("shift+f3"), "Shift+F3")
        self.assertEqual(running_overlay._hotkey_display(""), "")


class TestMainWindowWiring(unittest.TestCase):
    """主窗口把 runningStateChanged 接到浮层刷新，且用 running_overview（源码契约）。"""

    def test_signal_connected_to_refresh(self):
        import app.ui.main_window as mw_mod
        src = _read(mw_mod.__file__)
        self.assertIn("runningStateChanged.connect(self._refresh_running_overlay)", src)

    def test_refresh_uses_running_overview(self):
        import app.ui.main_window as mw_mod
        src = _read(mw_mod.__file__)
        self.assertIn("running_overlay.refresh(self.flow_tab.running_overview())", src)

    def test_refresh_does_not_clear_status_log(self):
        """刷新流程概览不碰状态日志（清空只发生在流程重新执行时）。"""
        import app.ui.main_window as mw_mod
        body = _read(mw_mod.__file__).split("def _refresh_running_overlay")[1]
        body = body.split("def ")[0]
        self.assertNotIn("clear_status", body)

    def test_flow_start_reshows_status_log(self):
        """流程含状态日志模块：重新执行时让状态日志重新显示（不清内容）。"""
        import app.ui.main_window as mw_mod
        body = _read(mw_mod.__file__).split("def _on_flow_started")[1]
        body = body.split("def ")[0]
        self.assertIn("running_overlay.reset_for_new_run()", body)
        self.assertIn("running_overlay.suppress_status()", body)
        self.assertNotIn("clear_status", body)      # 内容不再自动清除

    def test_flow_start_signal_carries_has_status_log(self):
        """启动流程时 signal 带「是否含状态日志模块」，据此决定浮层显隐。"""
        import app.ui.flow_tab as ft_mod
        src = _read(ft_mod.__file__)
        self.assertIn('flowStarted = Signal(bool)', src)
        self.assertIn('self.flowStarted.emit(any(st.type == "status_log"', src)
        self.assertIn('self.flowStarted.emit(step.type == "status_log")', src)

    def test_shutdown_closes_overlay(self):
        import app.ui.main_window as mw_mod
        body = _read(mw_mod.__file__).split("def shutdown")[1]
        self.assertIn("running_overlay.close()", body)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    unittest.main()
