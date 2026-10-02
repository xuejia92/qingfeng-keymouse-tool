# -*- coding: utf-8 -*-
"""设置页左侧导航契约（2026-10-02：用户要求「在左侧加一个设置的导航，快速滑动到对应区域」）。

本文件钉住四件事：

1. 每个可见分区都在导航里有一条，顺序与页面从上到下一致；
2. 导航栏**不在**滚动区里（否则会跟着内容一起滚走）；
3. 点击导航项 -> 平滑滚动到该分区；分区在页面末尾、因触底而滚不到顶时，
   高亮仍然是**用户点的那一项**（不许被 scroll-spy 抢走）；
4. 手动滚动时高亮跟随（scroll-spy），滚到最底部高亮最后一项。

⚠️ 跑在 offscreen 平台（无中文字体），所以只断言由布局/滚动条决定的几何关系，
不断言任何跟字体度量相关的绝对像素值。动画用 `setCurrentTime(duration)` 直接
推到终点，**不 sleep**（跑全套件时 sleep 会拖慢并有历史崩进程事故）。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractAnimation, QPoint
from PySide6.QtWidgets import QApplication, QScrollArea

from app.config import AppConfig
from app.ui import settings_tab as settings_mod
from app.ui import theme
from app.ui.settings_tab import SettingsTab


def _expected_titles() -> list[str]:
    titles = ["全局热键", "界面外观", "运行状态浮层"]
    if settings_mod.SHOW_CAPTURE_SECTION:
        titles.append("截屏上报")
    titles += ["文件位置", "关于"]
    return titles


class NavCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        theme.clear_registry()
        theme.apply_theme("light")

    def tearDown(self):
        # 主题与全局字体百分比都是**模块级全局状态**，用完必须还原，
        # 否则会污染其它测试文件（浅色断言 / 字号断言）。见项目记忆 §2、§33。
        theme.set_font_scale(theme.UI_FONT_SCALE_DEFAULT)
        theme.apply_theme("light", force=True)

    def _tab(self, w: int = 1000, h: int = 560) -> SettingsTab:
        """默认给一个**矮窗口**：内容必然超出，导航才有的可滚。"""
        tab = SettingsTab(AppConfig())
        tab.resize(w, h)
        tab.show()
        self._settle()
        self.addCleanup(tab.close)
        return tab

    def _settle(self, n: int = 5) -> None:
        for _ in range(n):
            self._app.processEvents()

    def _finish_anim(self, tab: SettingsTab) -> None:
        """把平滑滚动动画直接推到终点（等价于等它自然结束，但不用 sleep）。"""
        if tab._nav_anim.state() == QAbstractAnimation.Running:
            tab._nav_anim.setCurrentTime(tab._nav_anim.duration())
        self._settle()

    @staticmethod
    def _top_in_content(widget, content) -> int:
        return widget.mapTo(content, QPoint(0, 0)).y()


class TestNavStructure(NavCase):
    def test_every_section_has_a_nav_item_in_page_order(self):
        tab = self._tab()
        self.assertEqual([t for t, _w, _b in tab._nav_entries], _expected_titles())

    def test_nav_items_map_to_real_page_widgets(self):
        """每条导航都指向页面里真实存在、且已摆进滚动内容的控件。"""
        tab = self._tab()
        for _title, widget, _btn in tab._nav_entries:
            # 分区必须是滚动内容的子孙（mapTo 才能算出有效的 y）
            self.assertGreaterEqual(self._top_in_content(widget, tab._content), 0)

    def test_sections_are_ordered_top_to_bottom_like_the_nav(self):
        tab = self._tab()
        ys = [self._top_in_content(w, tab._content) for _t, w, _b in tab._nav_entries]
        self.assertEqual(ys, sorted(ys), "导航顺序必须与页面从上到下的顺序一致")

    def test_nav_is_not_inside_the_scroll_area(self):
        """导航栏放在滚动区之外——否则滑动内容时导航自己也滚没了。"""
        tab = self._tab()
        self.assertIsNot(tab._nav_frame.parent(), tab._content)
        areas = tab.findChildren(QScrollArea)
        self.assertEqual(len(areas), 1, "设置页仍应只有一个滚动区（导航不是滚动区）")
        self.assertIs(areas[0], tab._scroll)

    def test_nav_items_are_fixed_size_and_left_aligned(self):
        tab = self._tab()
        self.assertEqual(tab._nav_frame.width(), settings_mod._NAV_W)
        for _title, _w, btn in tab._nav_entries:
            self.assertEqual(btn.height(), settings_mod._NAV_ITEM_H)
            self.assertIn("text-align:left", btn.styleSheet())


class TestNavJump(NavCase):
    def test_click_scrolls_to_the_section(self):
        tab = self._tab()
        sb = tab._scroll.verticalScrollBar()
        self.assertGreater(sb.maximum(), 0, "这条用例需要内容确实可滚动，请给更矮的窗口")
        for title, widget, _btn in tab._nav_entries:
            tab._scroll_to_section(widget)
            self._finish_anim(tab)
            y = self._top_in_content(widget, tab._content)
            target = max(0, min(y - settings_mod._NAV_TOP_PAD, sb.maximum()))
            self.assertLessEqual(abs(sb.value() - target), 1,
                                 f"[{title}] 没有滚到目标位置")

    def test_scroll_is_animated_not_teleporting(self):
        """必须是补间动画（用户要的是「滑动」），不是瞬间跳过去。"""
        tab = self._tab()
        sb = tab._scroll.verticalScrollBar()
        last = tab._nav_entries[-1][1]
        sb.setValue(0)
        tab._scroll_to_section(last)
        self.assertEqual(tab._nav_anim.state(), QAbstractAnimation.Running)
        self.assertEqual(tab._nav_anim.duration(), settings_mod._NAV_ANIM_MS)
        self._finish_anim(tab)

    def test_clicked_item_stays_highlighted_when_clamped_at_bottom(self):
        """末尾分区（「关于」）滚不到顶：高亮要停在用户点的那一项，不能被反查抢走。"""
        tab = self._tab()
        sb = tab._scroll.verticalScrollBar()
        for idx in (3, 4):                      # 文件位置 / 关于：都会因触底而 clamp
            title, widget, _btn = tab._nav_entries[idx]
            tab._scroll_to_section(widget)
            self._finish_anim(tab)
            self.assertEqual(tab._nav_entries[tab._nav_active][0], title)
        self.assertEqual(sb.value(), sb.maximum())

    def test_exactly_one_item_is_highlighted(self):
        """选中态是「主题色浅底 + 左侧竖条」，未选中是透明竖条——同时只能有一个选中。"""
        tab = self._tab()
        tab._scroll_to_section(tab._nav_entries[1][1])
        self._finish_anim(tab)
        active_soft = theme.token("primary_soft")
        marked = [t for t, _w, b in tab._nav_entries if active_soft in b.styleSheet()]
        self.assertEqual(marked, [tab._nav_entries[tab._nav_active][0]])


class TestNavScrollSpy(NavCase):
    def test_highlight_follows_manual_scroll(self):
        tab = self._tab()
        sb = tab._scroll.verticalScrollBar()
        tab._nav_locked = False
        for idx in (1, 2, 0):
            y = self._top_in_content(tab._nav_entries[idx][1], tab._content)
            sb.setValue(max(0, min(y + 4, sb.maximum())))
            self._settle()
            self.assertEqual(tab._nav_active, idx,
                             f"滚到第 {idx} 个分区时高亮没跟上")

    def test_bottom_of_page_highlights_the_last_item(self):
        """末尾的「关于」够不到判定线，触底时要兜底高亮最后一项。"""
        tab = self._tab()
        sb = tab._scroll.verticalScrollBar()
        tab._nav_locked = False
        sb.setValue(sb.maximum())
        self._settle()
        self.assertEqual(tab._nav_active, len(tab._nav_entries) - 1)

    def test_spy_is_suspended_while_animating(self):
        """动画期间不准反查，否则滚动途中高亮会沿路乱跳。"""
        tab = self._tab()
        sb = tab._scroll.verticalScrollBar()
        sb.setValue(0)
        tab._scroll_to_section(tab._nav_entries[-1][1])
        self.assertTrue(tab._nav_locked)
        self._finish_anim(tab)
        self.assertFalse(tab._nav_locked, "动画结束后要解锁滚轮跟随")


class TestNavThemeFollow(NavCase):
    def test_item_qss_is_built_from_current_theme_tokens(self):
        """导航配色取自主题令牌，换主题时样式的色值跟着换（不是写死的 hex）。"""
        tab = self._tab()
        light_qss = tab._nav_item_qss(True)
        self.assertIn(theme.THEMES["light"]["primary_soft"], light_qss)
        theme.apply_theme("night")
        try:
            night_qss = tab._nav_item_qss(True)
            self.assertIn(theme.THEMES["night"]["primary_soft"], night_qss)
            self.assertNotEqual(light_qss, night_qss)
        finally:
            theme.apply_theme("light")

    def test_item_qss_font_follows_global_font_scale(self):
        """全局字体百分比要能缩放导航文字（控件自带样式表拿不到页面 QSS 字号）。"""
        tab = self._tab()
        base = tab._nav_item_qss(False)
        theme.set_font_scale(140)
        try:
            scaled = tab._nav_item_qss(False)
            self.assertNotEqual(base, scaled)
        finally:
            theme.set_font_scale(theme.UI_FONT_SCALE_DEFAULT)


if __name__ == "__main__":
    unittest.main()
