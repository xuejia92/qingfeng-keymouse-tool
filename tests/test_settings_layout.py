# -*- coding: utf-8 -*-
"""设置页排版契约（2026-10-01：用户反馈「运行状态浮层里的文字和表单框太紧凑」）。

当时这块有三个毛病，本文件逐条钉住，防止以后改回去：

1. `QFormLayout` 的默认 horizontalSpacing/verticalSpacing 只有 **6px**，
   中文标签"分组标题文字"紧贴着数字框；四种分组的内边距也只有 9px。
2. 运行浮层那个**色块按钮**写死 `padding: 2px 10px`，比同一行的下拉框矮 7px
   （实测 19 vs 26），整行看着歪歪扭扭。
3. 设置页**没有滚动区**：高度不够时 Qt 只能把分组框压扁（内容自然高度需要
   767px，在 660px 下「运行状态浮层」被压到 177px），行距和内边距全被吃掉。

⚠️ 这些是几何断言，跑在 offscreen 平台（无中文字体、字形是方框），所以只断言
**由布局/样式决定的间距与等高关系**，不断言任何跟字体度量相关的绝对像素值。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QScrollArea

from app.config import AppConfig
from app.ui import theme
from app.ui.settings_tab import SettingsTab


class SettingsLayoutCase(unittest.TestCase):
    """基类：保证有 QApplication，并且每个用例都从浅色默认主题开始。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        theme.clear_registry()
        theme.apply_theme("light")

    def _tab(self, w: int = 1360, h: int = 900) -> SettingsTab:
        tab = SettingsTab(AppConfig())
        tab.resize(w, h)
        tab.show()
        self._app.processEvents()
        self.addCleanup(tab.close)
        return tab

    @staticmethod
    def _pos(widget, ancestor) -> QPoint:
        """widget 左上角在 ancestor 坐标系里的位置。"""
        return widget.mapTo(ancestor, QPoint(0, 0))


class TestFormSpacing(SettingsLayoutCase):
    def test_all_forms_are_roomy_and_labels_right_aligned(self):
        forms = self._tab().findChildren(QFormLayout)
        self.assertTrue(forms, "设置页应该有若干 QFormLayout")
        for form in forms:
            self.assertGreaterEqual(form.horizontalSpacing(), 12)
            self.assertGreaterEqual(form.verticalSpacing(), 8)
            self.assertTrue(form.labelAlignment() & Qt.AlignRight,
                            "标签右对齐才能让「标签 -> 控件」的间隙处处一致")

    def test_label_has_gutter_before_field(self):
        """标签右边缘到控件左边缘至少 14px（QFormLayout 默认只有 6px）。"""
        tab = self._tab()
        box = tab.run_overlay_box
        label = next(l for l in box.findChildren(QLabel) if l.text() == "浮层文字")
        gap = (self._pos(tab.run_overlay_flow_size, box).x()
               - (self._pos(label, box).x() + label.width()))
        self.assertGreaterEqual(gap, 14, "标签和数字框又贴一起了")

    def test_rows_have_vertical_gap(self):
        """同一分组里上下两行至少隔 8px（默认只有 6px）。"""
        tab = self._tab()
        box = tab.run_overlay_box
        top, bottom = tab.run_overlay_flow_size, tab.run_overlay_log_size
        gap = self._pos(bottom, box).y() - (self._pos(top, box).y() + top.height())
        self.assertGreaterEqual(gap, 8)


class TestOverlayRowAlignment(SettingsLayoutCase):
    def test_color_button_matches_combo_height(self):
        """色块按钮与同行的下拉框**等高**（原来矮 7px，是整行看着歪的主因）。"""
        tab = self._tab()
        # 文字设置只剩一组（分组标题与流程名称已合并）
        for kind in ("flow",):
            btn = getattr(tab, f"run_overlay_{kind}_btn")
            combo = getattr(tab, f"run_overlay_{kind}_font")
            self.assertEqual(btn.height(), combo.height(),
                             f"{kind}: 色块按钮 {btn.height()}px vs 下拉框 {combo.height()}px")

    def test_color_button_uses_input_box_model(self):
        """等高的根因是盒模型：必须照抄输入控件那套（min-height 18 + padding 3px）。

        照抄全局 QPushButton 规则（padding 4px 12px、无 min-height）会差 3px。
        """
        tab = self._tab()
        ss = tab.run_overlay_flow_btn.styleSheet()
        self.assertIn("min-height: 18px", ss)
        self.assertIn("padding: 3px 10px", ss)

    def test_text_rows_are_column_aligned(self):
        """「浮层文字」与「状态日志」两行的字号/字体要列对齐。"""
        tab = self._tab()
        box = tab.run_overlay_box
        for attr in ("size", "font"):
            text_x = self._pos(getattr(tab, f"run_overlay_flow_{attr}"), box).x()
            log_x = self._pos(getattr(tab, f"run_overlay_log_{attr}"), box).x()
            self.assertEqual(text_x, log_x, f"{attr} 两行没对齐")
        # 最大尺寸行的「宽」「高」两个输入框不能重叠（中间还隔着「高」字标签）
        w_box, h_box = tab.run_overlay_log_max_w, tab.run_overlay_log_max_h
        gap = (self._pos(h_box, box).x()
               - (self._pos(w_box, box).x() + w_box.width()))
        self.assertGreater(gap, 0, "宽/高两个输入框叠在一起了")

    def test_widgets_in_a_row_do_not_touch(self):
        """一行里的控件之间要留缝（原来肩并肩贴着）。"""
        tab = self._tab()
        box = tab.run_overlay_box
        spin, combo = tab.run_overlay_flow_size, tab.run_overlay_flow_font
        gap = self._pos(combo, box).x() - (self._pos(spin, box).x() + spin.width())
        self.assertGreaterEqual(gap, 8)

    def test_position_combo_does_not_stretch_full_width(self):
        """「显示位置」下拉不能拉满整行（原来宽到 1200+px，与上面的窄控件不成比例）。"""
        tab = self._tab(1400, 900)
        self.assertLessEqual(tab.run_overlay_pos.width(), 320)

    def test_row_widgets_do_not_overlap_on_narrow_window(self):
        """窗口很窄时同一行的控件也不能互相压住（三个控件都是定宽，靠留缝排开）。"""
        tab = self._tab(900, 900)
        box = tab.run_overlay_box
        widgets = [tab.run_overlay_flow_size, tab.run_overlay_flow_font,
                   tab.run_overlay_flow_btn]
        for left, right in zip(widgets, widgets[1:]):
            gap = (self._pos(right, box).x()
                   - (self._pos(left, box).x() + left.width()))
            self.assertGreaterEqual(
                gap, 0, f"{type(left).__name__} 和 {type(right).__name__} 叠在一起了")


class TestSettingsPageScrolls(SettingsLayoutCase):
    def test_page_is_wrapped_in_scroll_area(self):
        tab = self._tab()
        areas = tab.findChildren(QScrollArea)
        self.assertEqual(len(areas), 1, "设置页应该有且只有一个滚动区")
        self.assertTrue(areas[0].widgetResizable())
        self.assertTrue(areas[0].widget().layout() is not None)

    def test_groups_keep_natural_height_on_short_window(self):
        """窗口矮时靠滚动条，不能把分组框压扁（原来浮层分组被压到 177px）。"""
        tab = self._tab(1200, 560)
        box = tab.run_overlay_box
        self.assertGreaterEqual(
            box.height(), box.sizeHint().height(),
            f"分组被压扁了：实得 {box.height()}px < 自然高度 {box.sizeHint().height()}px")

    def test_page_background_matches_tab_pane(self):
        """滚动区的视口要透明：设置页空白处必须是 panel_bg，和其它标签页一致。

        QAbstractScrollArea 的视口**即使 autoFillBackground=False 也会自己刷一层**
        （实测刷的是 palette 的 Window 色）——不处理的话整页变成 window_bg，
        在浅色主题下几乎看不出，换个暖色/深色主题就露馅了。
        """
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QTabWidget

        theme.apply_theme("warm_paper")
        tabs = QTabWidget()
        tabs.setStyleSheet(theme.build_button_qss() + theme.build_tab_qss())
        tabs.addTab(SettingsTab(AppConfig()), "设置")
        tabs.resize(1200, 1600)          # 远高于内容自然高度 -> 底部必然是空白
        tabs.show()
        self._app.processEvents()
        self.addCleanup(tabs.close)
        blank = QColor(tabs.grab().toImage().pixel(600, 1590)).name()
        self.assertEqual(blank, theme.THEMES["warm_paper"]["panel_bg"])

    def test_scroll_viewport_is_transparent(self):
        """上一条的静态依据：视口的 autoFill 关掉 + 样式里显式 transparent。"""
        area = self._tab().findChildren(QScrollArea)[0]
        self.assertFalse(area.viewport().autoFillBackground())
        self.assertIn("transparent", area.viewport().styleSheet())


if __name__ == "__main__":
    unittest.main()
