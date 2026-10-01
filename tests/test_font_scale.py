# -*- coding: utf-8 -*-
"""全局字体百分比（设置页「界面外观 -> 界面文字大小」，2026-10-01）。

需求原话：「设置页面的字体小一点，紧凑一点，节约空间，设置页面的界面外观里面添加一个
字体百分比设置，可以修改所有页面的文字大小，保持所有页面的文字大小，页面间距风格统一，
还有自动化流程页面的编辑步骤的弹出菜单里面的字体和风格，紧凑程度也要整体保持风格一致统一」。

实现要点（本文件逐条钉住）：
1. 百分比同时作用于**应用字体**与**所有 QSS 里的 font-size**——后者靠主题引擎
   `render_style()`（颜色映射 + 字号缩放）在 hook / 基线 / 重放三条路径统一处理，
   所以 140+ 处 UI 代码一行不用改。
2. 只缩放 `font-size`，**不碰 padding/margin/height/width**：间距由各处显式像素负责，
   跟着字号一起缩放反而会把行高、色块按钮盒模型之类搞跑偏。
3. 100% 时 `scale_qss` 原样返回（默认外观与加这个功能之前逐字相同）。
4. 设置页整页压到 9pt（与其他密集面板同档），表单留白走 `widgets.polish_form`，
   流程步骤编辑弹窗共用同一组间距常量——「页面间距风格统一」。

⚠️ 字体百分比是**模块级全局状态**（和主题一样），任何改它的用例都必须在 tearDown
里还原成 100%，否则后续测试文件会跟着被放大（本项目已被主题全局状态坑过一次）。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QPushButton

from app.config import (AppConfig, UI_FONT_SCALE_DEFAULT, UI_FONT_SCALE_MAX,
                        UI_FONT_SCALE_MIN, default_step_params, Flow, FlowStep)
from app.ui import theme, widgets
from app.ui.settings_tab import SettingsTab
from tests._env import TempConfigPaths


class FontScaleCase(unittest.TestCase):
    """基类：保证有 QApplication，并在每个用例前后把字号还原成 100%。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        theme.clear_registry()
        theme.set_font_scale(UI_FONT_SCALE_DEFAULT)
        theme.apply_theme("light", force=True)

    def tearDown(self):
        # 全局状态必须还原：否则下一个测试文件里的界面会被放大/缩小
        theme.clear_registry()
        theme.set_font_scale(UI_FONT_SCALE_DEFAULT)
        theme.apply_theme("light", force=True)


class TestScaleQss(unittest.TestCase):
    """纯字符串变换（不需要 QApplication）。"""

    def tearDown(self):
        theme.set_font_scale(UI_FONT_SCALE_DEFAULT)

    def test_hundred_percent_is_identity(self):
        text = "QPushButton { font-size: 10pt; padding: 4px 12px; }"
        theme.set_font_scale(100)
        self.assertEqual(theme.scale_qss(text), text, "100% 必须逐字返回，默认外观不变")

    def test_scales_pt_and_px(self):
        theme.set_font_scale(120)
        self.assertIn("font-size: 12pt", theme.scale_qss("font-size: 10pt;"))
        self.assertIn("font-size: 12px", theme.scale_qss("font-size: 10px;"))

    def test_only_font_size_is_touched(self):
        """padding/margin/height/width 一律不动（行高、盒模型由各处显式像素负责）。"""
        theme.set_font_scale(150)
        out = theme.scale_qss(
            "font-size: 10pt; padding: 4px 12px; margin: 2px;"
            " height: 30px; min-height: 18px; width: 104px;")
        for keep in ("padding: 4px 12px", "margin: 2px", "height: 30px",
                     "min-height: 18px", "width: 104px"):
            self.assertIn(keep, out, f"{keep} 不该被字号缩放改掉")
        self.assertIn("font-size: 15pt", out)

    def test_fractional_and_rounding(self):
        """0.5pt 一档：9pt x 105% = 9.45 -> 9.5；9pt x 110% = 9.9 -> 10。"""
        self.assertIn("font-size: 9.5pt", theme.scale_qss("font-size: 9pt;", 105))
        self.assertIn("font-size: 10pt", theme.scale_qss("font-size: 9pt;", 110))

    def test_no_font_size_means_same_text(self):
        theme.set_font_scale(130)
        self.assertEqual(theme.scale_qss("QLabel { color: #24292f; }"),
                         "QLabel { color: #24292f; }")

    def test_empty_text_is_safe(self):
        theme.set_font_scale(130)
        self.assertEqual(theme.scale_qss(""), "")

    def test_normalize_clamps_and_falls_back(self):
        self.assertEqual(theme.normalize_font_scale(120), 120)
        self.assertEqual(theme.normalize_font_scale("115"), 115)
        self.assertEqual(theme.normalize_font_scale(999), UI_FONT_SCALE_MAX)
        self.assertEqual(theme.normalize_font_scale(0), UI_FONT_SCALE_MIN)
        self.assertEqual(theme.normalize_font_scale("abc"), UI_FONT_SCALE_DEFAULT)
        self.assertEqual(theme.normalize_font_scale(None), UI_FONT_SCALE_DEFAULT)

    def test_scaled_pt(self):
        theme.set_font_scale(150)
        self.assertEqual(theme.scaled_pt(10), 15.0)
        self.assertEqual(theme.scaled_pt(9), 13.5)

    def test_render_style_maps_colour_and_size_together(self):
        """两条变换必须同进同出：不能出现某条路径只映射了颜色。"""
        theme.set_font_scale(120)
        out = theme.render_style("QLabel { color: #24292f; font-size: 9pt; }", "light")
        self.assertIn("#24292f", out)          # light 主题下恒等映射
        self.assertIn("font-size: 11pt", out)


class TestAppFontFollowsScale(FontScaleCase):
    def test_application_font_scales(self):
        theme.set_font_scale(120)
        theme.apply_theme("light", force=True)
        self.assertAlmostEqual(self._app.font().pointSizeF(), 12.0, places=1)

        theme.set_font_scale(80)
        theme.apply_theme("light", force=True)
        self.assertAlmostEqual(self._app.font().pointSizeF(), 8.0, places=1)

    def test_registered_inline_style_is_replayed_scaled(self):
        """已存在的控件在改百分比后要被重放成新字号。

        ⚠️ 按钮要**有父级**：没有父级的 QWidget 本身就是顶层窗口，主题重放时会走
        窗口基线那条分支（隐藏时不重放），拿到的不是内联样式。
        """
        from PySide6.QtWidgets import QWidget
        holder = QWidget()
        self.addCleanup(holder.close)
        btn = QPushButton(holder)
        btn.setStyleSheet("QPushButton { font-size: 10pt; }")
        theme.set_font_scale(130)
        theme.apply_theme("light", force=True)
        self.assertIn("font-size: 13pt", btn.styleSheet())

    def test_window_baseline_is_scaled(self):
        """窗口级基线（含全局 QPushButton 规则）也要跟着缩放。"""
        from PySide6.QtWidgets import QWidget
        win = QWidget()
        win.setStyleSheet("QWidget { background: #f7f9fb; }")
        win.show()                      # 顶层窗口显示时才会套基线
        self.addCleanup(win.close)
        self._app.processEvents()
        theme.set_font_scale(120)
        theme.apply_theme("light", force=True)
        sheet = win.styleSheet()
        self.assertIn("font-size: 12pt", sheet, "基线里的按钮字号没跟着缩放")
        self.assertNotIn("font-size: 10pt", sheet)


class TestSettingsTabFontScale(FontScaleCase):
    def _tab(self) -> SettingsTab:
        tab = SettingsTab(AppConfig())
        tab.resize(1360, 900)
        tab.show()
        self._app.processEvents()
        self.addCleanup(tab.close)
        return tab

    def test_spin_defaults_and_range(self):
        tab = self._tab()
        self.assertEqual(tab.font_scale_spin.value(), UI_FONT_SCALE_DEFAULT)
        self.assertEqual(tab.font_scale_spin.minimum(), UI_FONT_SCALE_MIN)
        self.assertEqual(tab.font_scale_spin.maximum(), UI_FONT_SCALE_MAX)
        self.assertEqual(tab.font_scale_spin.suffix().strip(), "%")
        self.assertEqual(tab.font_scale_value(), UI_FONT_SCALE_DEFAULT)

    def test_spin_reads_configured_value(self):
        cfg = AppConfig()
        cfg.ui_font_scale = 125
        tab = SettingsTab(cfg)
        self.addCleanup(tab.close)
        self.assertEqual(tab.font_scale_value(), 125)

    def test_changing_spin_applies_immediately(self):
        """改百分比要**立即**生效（不用重启、不用重开页面）。"""
        tab = self._tab()
        tab.font_scale_spin.setValue(130)
        self._app.processEvents()
        self.assertEqual(theme.font_scale(), 130)
        self.assertAlmostEqual(self._app.font().pointSizeF(), 13.0, places=1)

    def test_page_font_is_compact(self):
        """设置页整页压到 9pt（用户要求「字体小一点、紧凑一点」）。"""
        tab = self._tab()
        label = next(l for l in tab.findChildren(QLabel) if l.text() == "配色主题")
        self.assertAlmostEqual(label.font().pointSizeF(), 9.0, places=1)
        self.assertAlmostEqual(tab.theme_combo.font().pointSizeF(), 9.0, places=1)
        # 自带内联样式的按钮拿不到页面 QSS，必须显式传同一档字号
        open_log = next(b for b in tab.findChildren(QPushButton)
                        if b.text() == "打开日志文件")
        self.assertAlmostEqual(open_log.font().pointSizeF(), 9.0, places=1)

    def test_page_font_scales_with_percentage(self):
        tab = self._tab()
        tab.font_scale_spin.setValue(120)
        self._app.processEvents()
        label = next(l for l in tab.findChildren(QLabel) if l.text() == "配色主题")
        self.assertGreater(label.font().pointSizeF(), 9.0)


class TestSharedFormRhythm(FontScaleCase):
    """「页面间距风格统一」：设置页与步骤编辑弹窗共用同一组间距常量。"""

    def test_constants_are_roomier_than_qt_defaults(self):
        self.assertGreaterEqual(widgets.FORM_H_SPACING, 12, "QFormLayout 默认只有 6px")
        self.assertGreaterEqual(widgets.FORM_V_SPACING, 8)

    def test_settings_forms_use_the_shared_values(self):
        tab = SettingsTab(AppConfig())
        tab.resize(1360, 900)
        self.addCleanup(tab.close)
        forms = tab.findChildren(QFormLayout)
        self.assertTrue(forms)
        for form in forms:
            self.assertEqual(form.horizontalSpacing(), widgets.FORM_H_SPACING)
            self.assertEqual(form.verticalSpacing(), widgets.FORM_V_SPACING)

    def test_step_dialog_uses_the_shared_values(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="click", name="点击",
                        params=dict(default_step_params("click")))
        dlg = StepParamsDialog(step)
        self.addCleanup(dlg.close)
        forms = dlg.findChildren(QFormLayout)
        self.assertTrue(forms, "步骤编辑弹窗应该有参数表单")
        matched = [f for f in forms if f.horizontalSpacing() == widgets.FORM_H_SPACING]
        self.assertTrue(matched, "步骤编辑弹窗的主表单没有走统一的间距节奏")
        for form in matched:
            self.assertEqual(form.verticalSpacing(), widgets.FORM_V_SPACING)

    def test_flow_meta_dialog_uses_the_shared_values(self):
        from app.ui.flow_dialog import FlowMetaDialog
        dlg = FlowMetaDialog(Flow(name="测试", steps=[]), True)
        self.addCleanup(dlg.close)
        form = dlg.findChild(QFormLayout)
        self.assertIsNotNone(form)
        self.assertEqual(form.horizontalSpacing(), widgets.FORM_H_SPACING)
        self.assertEqual(form.verticalSpacing(), widgets.FORM_V_SPACING)

    def test_settings_tab_delegates_to_shared_helper(self):
        """_polish_form 只是薄封装——数值只有一处（避免两边各写一份而漂移）。"""
        from app.ui.settings_tab import _polish_form
        form = _polish_form(QFormLayout())
        self.assertEqual(form.horizontalSpacing(), widgets.FORM_H_SPACING)
        self.assertEqual(form.verticalSpacing(), widgets.FORM_V_SPACING)


class TestConfigPersistence(unittest.TestCase):
    def test_round_trip(self):
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.ui_font_scale = 120
            cfg.save()
            self.assertEqual(AppConfig.load().ui_font_scale, 120)

    def test_out_of_range_is_clamped_on_load(self):
        with TempConfigPaths() as tmp:
            from tests._env import write_json
            write_json(os.path.join(tmp, "config.json"), {"ui_font_scale": 9999})
            self.assertEqual(AppConfig.load().ui_font_scale, UI_FONT_SCALE_MAX)

    def test_garbage_falls_back_to_default(self):
        with TempConfigPaths() as tmp:
            from tests._env import write_json
            write_json(os.path.join(tmp, "config.json"), {"ui_font_scale": "巨大"})
            self.assertEqual(AppConfig.load().ui_font_scale, UI_FONT_SCALE_MIN)

    def test_missing_key_defaults(self):
        with TempConfigPaths() as tmp:
            from tests._env import write_json
            write_json(os.path.join(tmp, "config.json"), {})
            self.assertEqual(AppConfig.load().ui_font_scale, UI_FONT_SCALE_DEFAULT)


class TestWiringSourceContract(unittest.TestCase):
    """源码级兜底：接线断了（漏调用）时立刻红。"""

    @classmethod
    def setUpClass(cls):
        import app.ui.main_window as mw
        with open(mw.__file__, encoding="utf-8") as f:
            cls.mw = f.read()

    def test_font_scale_set_before_theme_applied(self):
        i_font = self.mw.find("theme.set_font_scale")
        i_theme = self.mw.find("theme.apply_theme")
        self.assertNotEqual(i_font, -1, "主窗口启动时没有设置全局字体百分比")
        self.assertLess(i_font, i_theme, "必须先设字号再应用主题（基线/内联样式按当时字号生成）")

    def test_settings_changed_writes_back(self):
        self.assertIn("cfg.ui_font_scale = self.settings_tab.font_scale_value()", self.mw)


if __name__ == "__main__":
    unittest.main()
