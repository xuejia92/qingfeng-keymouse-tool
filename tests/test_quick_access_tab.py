# -*- coding: utf-8 -*-
"""「⚡ 快捷操作」页（左导航 + 右内容）的测试。

覆盖：
- 容器本身：页面登记、导航项、初始选中、点击切换、`currentChanged`、
  越界夹取、重复选中不重复发信号、页面对象**原样托管**（不复制不重建）；
- 与四个真实页面（中键菜单 / 鼠标连点 / 键盘连按 / 找图点击）的集成：
  切换导航时右侧 stack 的当前页确实跟着换；
- 主窗口接线契约（源码级）：四个标签已合并成一个「⚡ 快捷操作」，
  且四个控件仍被构造并交给本页托管（信号接线因此不用改）。
"""
from __future__ import annotations

import inspect
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel, QWidget  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.clicker_tab import ClickerTab  # noqa: E402
from app.ui.finder_tab import FinderTab  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.middle_menu_tab import MiddleMenuTab  # noqa: E402
from app.ui.presser_tab import PresserTab  # noqa: E402
from app.ui.quick_access_tab import NAV_W, QuickAccessTab  # noqa: E402
from tests._env import TempConfigPaths  # noqa: E402


class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self):
        theme.clear_registry()      # 少留一批样式登记，别拖慢别的用例


def _pages(n: int = 4):
    return [(f"功能{i}", QWidget()) for i in range(n)]


class TestQuickAccessTab(_QtCase):

    def test_registers_pages_in_order(self):
        pages = _pages()
        tab = QuickAccessTab(pages)
        self.assertEqual(tab.page_count(), 4)
        self.assertEqual(tab.titles(), [t for t, _ in pages])
        self.assertEqual(tab.pages(), [w for _, w in pages])
        self.assertEqual(len(tab.nav_buttons()), 4)
        self.assertEqual(tab.stack.count(), 4)

    def test_pages_are_hosted_not_rebuilt(self):
        """传进来的控件必须**原样**进 stack（重建就等于丢掉既有接线）。"""
        pages = _pages()
        tab = QuickAccessTab(pages)
        for _, widget in pages:
            self.assertIs(widget.parent(), tab.stack)
        self.assertIs(tab.stack.widget(0), pages[0][1])

    def test_initial_selection_is_first_page(self):
        tab = QuickAccessTab(_pages())
        self.assertEqual(tab.current_index(), 0)
        self.assertIs(tab.current_widget(), tab.pages()[0])
        self.assertIn(theme.token("primary"), tab.nav_buttons()[0].styleSheet())

    def test_clicking_nav_switches_content(self):
        tab = QuickAccessTab(_pages())
        seen = []
        tab.currentChanged.connect(seen.append)
        tab.nav_buttons()[2].click()
        self.assertEqual(tab.current_index(), 2)
        self.assertIs(tab.current_widget(), tab.pages()[2])
        self.assertEqual(seen, [2])
        tab.nav_buttons()[0].click()
        self.assertEqual(seen, [2, 0], "切换到另一个页面要再发一次信号")

    def test_only_one_nav_item_active(self):
        tab = QuickAccessTab(_pages())
        tab.set_current_index(3)
        active = [i for i, btn in enumerate(tab.nav_buttons())
                  if theme.token("primary") in btn.styleSheet()]
        self.assertEqual(active, [3], "同一时刻只应有一个导航项处于选中态")

    def test_reselect_same_index_does_not_emit_again(self):
        tab = QuickAccessTab(_pages())
        seen = []
        tab.currentChanged.connect(seen.append)
        tab.set_current_index(1)
        tab.set_current_index(1)
        tab.nav_buttons()[1].click()
        self.assertEqual(seen, [1])

    def test_index_is_clamped(self):
        tab = QuickAccessTab(_pages())
        tab.set_current_index(99)
        self.assertEqual(tab.current_index(), 3)
        tab.set_current_index(-5)
        self.assertEqual(tab.current_index(), 0)

    def test_set_current_widget(self):
        tab = QuickAccessTab(_pages())
        self.assertEqual(tab.set_current_widget(tab.pages()[2]), 2)
        self.assertEqual(tab.current_index(), 2)
        stranger = QWidget()
        self.assertEqual(tab.set_current_widget(stranger), -1)
        self.assertEqual(tab.current_index(), 2, "没登记过的控件不该改变当前页")

    def test_empty_pages_do_not_crash(self):
        tab = QuickAccessTab()
        self.assertEqual(tab.page_count(), 0)
        tab.set_current_index(0)            # 不该抛
        self.assertIsNone(tab.current_widget())

    def test_add_page_after_construction(self):
        tab = QuickAccessTab(_pages(1))
        extra = QWidget()
        tab.add_page("补充页", extra)
        self.assertEqual(tab.page_count(), 2)
        tab.set_current_widget(extra)
        self.assertIs(tab.current_widget(), extra)

    def test_nav_is_fixed_width_column(self):
        tab = QuickAccessTab(_pages())
        self.assertEqual(tab._nav_frame.width(), NAV_W)
        self.assertTrue(tab._nav_frame.objectName())

    def test_nav_width_and_item_height_are_positive(self):
        self.assertGreater(NAV_W, 0)
        for btn in QuickAccessTab(_pages()).nav_buttons():
            self.assertGreater(btn.height(), 0)


class TestRealPagesIntegration(_QtCase):
    """用真实的四个页面跑一遍（不构造整个主窗口）。"""

    def _tab(self):
        tmp = TempConfigPaths()
        tmp.__enter__()
        self.addCleanup(tmp.__exit__)
        cfg = AppConfig()
        raw = [("中键菜单", MiddleMenuTab(cfg)),
               ("鼠标连点", ClickerTab(cfg.clicker)),
               ("键盘连按", PresserTab(cfg.presser)),
               ("找图点击", FinderTab(cfg.find_tasks))]
        tab = QuickAccessTab(raw)
        self.addCleanup(tab.deleteLater)
        return raw, tab

    def test_switch_through_all_four_real_pages(self):
        raw, tab = self._tab()
        self.assertEqual(tab.titles(),
                         ["中键菜单", "鼠标连点", "键盘连按", "找图点击"])
        for i, (_title, widget) in enumerate(raw):
            tab.nav_buttons()[i].click()
            self.assertIs(tab.stack.currentWidget(), widget)
            self.assertIs(tab.current_widget(), widget)

    def test_signals_of_hosted_pages_still_work(self):
        """托管不改变页面行为：四大页面的对外信号照旧可用。"""
        raw, tab = self._tab()
        middle = raw[0][1]
        seen = []
        middle.changed.connect(lambda *_: seen.append(1))
        middle.enable_check.setChecked(not middle.enable_check.isChecked())
        self.assertTrue(seen, "中键菜单页的 changed 信号应当照常发出")

    def test_page_content_has_no_duplicate_title(self):
        """页内不再重复写标题（左侧导航已经写着功能名）。"""
        raw, _tab = self._tab()
        middle = raw[0][1]
        texts = [w.text() for w in middle.findChildren(QLabel)]
        self.assertNotIn("📋 中键菜单", texts)
        self.assertTrue(any("鼠标中键" in t for t in texts), "提示文字要保留")


class TestMainWindowWiring(unittest.TestCase):
    """构造整个 MainWindow 太重，这里钉住写法（与其它页面的做法一致）。"""

    def setUp(self):
        self.src = inspect.getsource(MainWindow.__init__)

    def test_top_level_tabs(self):
        lines = [ln.strip() for ln in self.src.splitlines()
                 if "tabs.addTab(" in ln]
        self.assertEqual(len(lines), 5, lines)
        for label in ("🚀 自动化流程", "⏰ 定时任务", "⚡ 快捷操作", "🧰 小工具"):
            self.assertTrue(any(label in ln for ln in lines), label)
        self.assertTrue(any('tabs.addTab(self.settings_tab, "")' in ln
                            for ln in lines),
                        "设置页签不带文字（入口已改成标签栏最右侧的齿轮图标）")

    def test_four_old_tabs_are_gone(self):
        joined = "\n".join(ln for ln in self.src.splitlines()
                           if "tabs.addTab(" in ln)
        for label in ("中键菜单", "鼠标连点", "键盘连按", "找图点击"):
            self.assertNotIn(label, joined, f"{label} 不该再单独占一个标签")

    def test_four_widgets_are_still_built_and_hosted(self):
        for expr in ("self.middle_menu_tab = MiddleMenuTab(cfg)",
                     "self.clicker_tab = ClickerTab(cfg.clicker)",
                     "self.presser_tab = PresserTab(cfg.presser)",
                     "self.finder_tab = FinderTab(cfg.find_tasks)"):
            self.assertIn(expr, self.src, expr)
        for expr in ('("中键菜单", self.middle_menu_tab)',
                     '("鼠标连点", self.clicker_tab)',
                     '("键盘连按", self.presser_tab)',
                     '("找图点击", self.finder_tab)'):
            self.assertIn(expr, self.src, expr)

    def test_nav_titles_have_no_emoji(self):
        """导航标题不要带 emoji：🖱/⌨/🖼 在按钮文本里会渲染成缺字方块（实测）。"""
        joined = self.src
        for bad in ("📋", "🖱", "⌨", "🖼"):
            self.assertNotIn(f'("{bad} ', joined)
        self.assertIn('"中键菜单"', joined)

    def test_quick_tab_order(self):
        joined = self.src
        self.assertLess(joined.index('tabs.addTab(self.schedule_tab'),
                        joined.index('tabs.addTab(self.quick_tab'))
        self.assertLess(joined.index('tabs.addTab(self.quick_tab'),
                        joined.index('tabs.addTab(self.tools_tab'))


if __name__ == "__main__":
    unittest.main()
