# -*- coding: utf-8 -*-
"""「设置」入口：标签栏最右侧的齿轮图标（2026-10-03）。

用户要求：设置标签放到最右侧、只要图标不要文字。

`QTabBar` 的页签只能从左往右排、多余空间一律留在右边，没有"某个页签右对齐"的接口，
所以实现是：页签**照常加进 QTabWidget**（`setCurrentWidget`、页面信号接线、
`settings_tab` 属性全不受影响），但把它的页签按钮**隐藏**，用 `cornerWidget`
在标签栏最右侧放一个齿轮图标按钮当入口。

覆盖：
- `add_settings_corner`：角标建在 TopRightCorner、图标非空、点一下切到设置页；
- `sync_settings_corner`：在设置页时高亮、离开时恢复透明；
- 主窗口接线契约（源码级）：设置页签无文字 + 图标 + 隐藏页签按钮 + 角标 + 切换时同步。
"""
from __future__ import annotations

import inspect
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QLabel, QTabWidget, QWidget  # noqa: E402

from app.ui import theme  # noqa: E402
from app.ui.main_window import (MainWindow, add_settings_corner,  # noqa: E402
                                gear_icon, sync_settings_corner)


class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self):
        theme.clear_registry()


class TestSettingsCorner(_QtCase):

    def _tabs(self):
        """一个最小可用的 QTabWidget：两个普通页 + 一个"设置"页。"""
        tabs = QTabWidget()
        settings = QWidget()
        tabs.addTab(QWidget(), "甲")
        tabs.addTab(QWidget(), "乙")
        tabs.addTab(settings, "")
        button = add_settings_corner(tabs, settings)
        self.addCleanup(tabs.deleteLater)
        return tabs, settings, button

    def test_corner_button_is_created(self):
        _tabs, _settings, button = self._tabs()
        self.assertIsNotNone(button)
        self.assertFalse(button.icon().isNull(), "齿轮图标不能为空")
        self.assertEqual(button.toolTip(), "设置")
        self.assertEqual(button.objectName(), "settingsCorner")

    def test_corner_button_sits_at_top_right(self):
        tabs, _settings, button = self._tabs()
        holder = tabs.cornerWidget(Qt.TopRightCorner)
        self.assertIsNotNone(holder, "角标必须放在右上角")
        # 按钮套在一层容器里（容器负责右侧留白），所以比的是父子关系
        self.assertIs(button.parent(), holder)
        self.assertIsNone(tabs.cornerWidget(Qt.TopLeftCorner))

    def test_corner_is_not_flush_against_the_edge(self):
        """留出右侧呼吸位：贴着窗口边缘 + 悬停底色被切一半 = 上一版"太丑"的原因。"""
        from app.ui.main_window import CORNER_RIGHT_PAD

        tabs, _settings, _button = self._tabs()
        holder = tabs.cornerWidget(Qt.TopRightCorner)
        margins = holder.layout().contentsMargins()
        self.assertEqual(margins.right(), CORNER_RIGHT_PAD)
        self.assertGreater(CORNER_RIGHT_PAD, 0)

    def test_clicking_corner_switches_to_settings(self):
        tabs, settings, button = self._tabs()
        self.assertIsNot(tabs.currentWidget(), settings)
        button.click()
        self.assertIs(tabs.currentWidget(), settings, "点齿轮应当切到设置页")
        tabs.setCurrentIndex(0)
        button.click()
        self.assertIs(tabs.currentWidget(), settings, "切走后再点还能回去")

    def test_corner_has_no_text(self):
        _tabs, _settings, button = self._tabs()
        self.assertEqual(button.text(), "", "只要图标、不要文字")

    def test_sync_highlights_only_on_settings_page(self):
        tabs, settings, button = self._tabs()
        main = theme.token("primary_soft")
        tabs.setCurrentIndex(0)
        sync_settings_corner(button, tabs, settings)
        self.assertNotIn(main, button.styleSheet())
        tabs.setCurrentWidget(settings)
        sync_settings_corner(button, tabs, settings)
        self.assertIn(main, button.styleSheet(), "在设置页时齿轮要有高亮底")
        self.assertIn("hover", button.styleSheet())

    def test_button_is_square_and_rounded(self):
        """正方形 + 圆角才像"图标按钮"，而不是一条被压扁的页签。"""
        from app.ui.main_window import CORNER_BTN, CORNER_RADIUS

        _tabs, _settings, button = self._tabs()
        self.assertEqual(button.width(), button.height())
        self.assertEqual(button.width(), CORNER_BTN)
        self.assertIn(f"border-radius:{CORNER_RADIUS}px", button.styleSheet())

    def _icon_colors(self, button) -> set[str]:
        """图标里所有不透明像素的颜色（自绘齿轮是纯色，取色要跳过透明的中心孔）。"""
        image = button.icon().pixmap(18, 18).toImage()
        out = set()
        for x in range(image.width()):
            for y in range(image.height()):
                color = image.pixelColor(x, y)
                if color.alpha() > 0:
                    out.add(color.name())
        return out

    def test_icon_color_follows_active_state(self):
        """常态用次要文字色（不跟页签抢视线），停在设置页时换成主色。"""
        tabs, settings, button = self._tabs()
        idle = self._icon_colors(button)
        self.assertIn(theme.token("text_dim").lower(), idle)
        tabs.setCurrentWidget(settings)
        sync_settings_corner(button, tabs, settings)
        active = self._icon_colors(button)
        self.assertIn(theme.token("primary").lower(), active)
        self.assertNotIn(theme.token("text_dim").lower(), active)

    def test_sync_is_cheap_and_idempotent(self):
        tabs, settings, button = self._tabs()
        tabs.setCurrentWidget(settings)
        for _ in range(3):
            sync_settings_corner(button, tabs, settings)
        self.assertIn(theme.token("primary_soft"), button.styleSheet())

    def test_icon_renders_something_visible(self):
        """图标得真画出东西来。

        齿轮是**自绘**的（QPainter），不走 emoji 字体——离屏/精简系统上 emoji 字体
        缺失时，字体图标会得到一张全透明的图（实测 `icon_for("gear")` 就是这样），
        所以这里用"颜色数 > 1"把这种退化钉死。
        """
        _tabs, _settings, button = self._tabs()
        image = button.icon().pixmap(16, 16).toImage()
        colors = {image.pixelColor(x, y).name()
                  for x in range(image.width()) for y in range(image.height())}
        self.assertGreater(len(colors), 1, "图标不应当是一张纯色/空图")


class TestMainWindowWiring(unittest.TestCase):

    def setUp(self):
        self.src = inspect.getsource(MainWindow.__init__)

    def test_settings_tab_is_blank_and_hidden(self):
        self.assertIn('self.settings_tab_index = tabs.addTab(self.settings_tab, "")',
                      self.src)
        self.assertIn("tabs.tabBar().setTabVisible(self.settings_tab_index, False)",
                      self.src)

    def test_gear_is_drawn_not_borrowed_from_a_font(self):
        """常驻图标必须自绘：字体图标在缺字形的环境会变成一张空白图（实测过）。"""
        source = inspect.getsource(gear_icon)
        self.assertIn("QPainter", source)
        self.assertNotIn("drawText", source, "自绘就不要再走字体绘制")
        # 图标由 sync_settings_corner 统一生成（颜色随选中态/主题变），不借字体
        self.assertIn("gear_icon", inspect.getsource(sync_settings_corner))

    def test_corner_button_is_added_and_synced(self):
        self.assertIn("self.settings_btn = add_settings_corner(tabs, self.settings_tab)",
                      self.src)
        self.assertIn("sync_settings_corner(self.settings_btn", self.src)
        self.assertIn("tabs.currentChanged.connect", self.src,
                      "切页后要同步齿轮高亮")

    def test_settings_is_the_last_tab(self):
        lines = [ln.strip() for ln in self.src.splitlines()
                 if "tabs.addTab(" in ln]
        self.assertIn("self.settings_tab", lines[-1], lines)

    def test_no_text_left_on_settings_tab(self):
        for line in self.src.splitlines():
            if "tabs.addTab(self.settings_tab" in line:
                self.assertNotIn("设置\"", line.replace("self.settings_tab", ""),
                                 f"设置页签不该还有文字：{line.strip()}")

    def test_helpers_are_testable_without_main_window(self):
        """三个辅助函数做成模块级，测试才能不构造整个主窗口就验证行为。"""
        for fn in (add_settings_corner, sync_settings_corner, gear_icon):
            self.assertTrue(callable(fn), fn)
        self.assertIn("tabs.setCornerWidget(holder, Qt.TopRightCorner)",
                      inspect.getsource(add_settings_corner))


if __name__ == "__main__":
    unittest.main()
