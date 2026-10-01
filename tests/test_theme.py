# -*- coding: utf-8 -*-
"""统一主题引擎（app/ui/theme.py）测试。

覆盖：令牌完整性、颜色映射（含历史内联样式、白色背景特判、自定义色不被改）、
setStyleSheet 拦截与主题切换重放、全局基线与调色板、监听器（弱引用/清理）、
按钮变体与窗口级 QSS、非法主题回退、与设置页/配置的接线契约。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from app.ui import theme


def _contrast(fg: str, bg: str) -> float:
    """WCAG 相对对比度（1 ~ 21）。"""
    def _lum(value: str) -> float:
        h = value.lstrip("#").lower()
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        ch = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    a, b = _lum(fg), _lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


class ThemeCase(unittest.TestCase):
    """基类：保证有 QApplication，且每个用例结束后回到默认主题。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 单测之间没有界面延续关系：清掉上一批用例登记的控件，
        # 否则切主题时会把它们全部重放一遍（真实场景里这些界面早就不存在了）。
        theme.clear_registry()
        theme.apply_theme("light")

    def tearDown(self):
        theme.clear_listeners()
        theme.clear_registry()
        theme.apply_theme("light")


class TestTokens(ThemeCase):
    def test_all_themes_have_all_tokens(self):
        for name, t in theme.THEMES.items():
            missing = [k for k in theme.TOKEN_KEYS if k not in t]
            self.assertEqual(missing, [], f"{name} 缺令牌: {missing}")

    def test_token_values_are_colors(self):
        for name, t in theme.THEMES.items():
            for k in theme.TOKEN_KEYS:
                v = t[k]
                self.assertTrue(isinstance(v, str) and v.startswith("#")
                                and len(v) in (4, 7, 9), f"{name}.{k}={v!r}")

    def test_theme_names_and_labels(self):
        names = [k for k, _ in theme.theme_names()]
        self.assertEqual(names[0], theme.DEFAULT_THEME)      # 默认主题排第一
        self.assertIn("silicon_dark", names)
        for _, label in theme.theme_names():
            self.assertTrue(label)

    def test_normalize_falls_back(self):
        self.assertEqual(theme.normalize_theme("nope"), "light")
        self.assertEqual(theme.normalize_theme(None), "light")
        self.assertEqual(theme.normalize_theme(""), "light")
        self.assertEqual(theme.normalize_theme("night"), "night")

    def test_token_lookup_and_unknown(self):
        self.assertEqual(theme.token("primary"), "#1668a8")   # 浅色默认 = 原配色
        self.assertEqual(theme.token("no_such_token"), "no_such_token")

    def test_legacy_map_targets_exist_in_all_themes(self):
        """映射表的目标令牌必须在每套主题里都有值。

        缺值会让 map_colors 里 `theme.get(tok, 原值)` 回退成原色——深色主题下
        就是「浅色残留」（本次就踩过：danger_soft 等只进了映射表、没进主题定义）。
        """
        targets = set(theme.LEGACY_COLOR_TOKENS.values())
        for name, t in theme.THEMES.items():
            missing = [k for k in targets if k not in t]
            self.assertEqual(missing, [], f"{name} 映射目标缺值: {missing}")

    def test_dialog_border_not_in_base_qss(self):
        """基线不再给 QDialog 加边框——系统标题栏无法用 QSS 加框，
        只在内容区画一圈会和标题栏割裂；边框改由 FramelessDialog 自绘卡片承担。"""
        qss = theme.build_base_qss("light")
        self.assertNotIn("border: 2px solid", qss)      # 之前的 QDialog 边框已移除
        self.assertNotIn("#93a3b0", qss)                # dialog_border 也不在基线里出现

    def test_light_matches_legacy_palette(self):
        """默认主题必须与改造前配色一致（否则等于偷偷改了界面观感）。"""
        t = theme.THEMES["light"]
        self.assertEqual(t["primary"], "#1668a8")
        self.assertEqual(t["text"], "#24292f")
        self.assertEqual(t["panel_bg"], "#f7f9fb")
        self.assertEqual(t["border"], "#d8dee4")
        self.assertEqual(t["input_border"], "#c9d1d9")

    def test_is_dark(self):
        theme.apply_theme("light")
        self.assertFalse(theme.is_dark())
        theme.apply_theme("silicon_dark")
        self.assertTrue(theme.is_dark())


class TestLightThemes(ThemeCase):
    """浅色主题家族（2026-10-01 扩充 6 套）与「不改变既有映射」契约。"""

    NEW_LIGHT = ("warm_paper", "mint", "sakura", "sea_salt", "lavender", "graphite")

    def test_light_family_present_and_marked_light(self):
        lights = [k for k, t in theme.THEMES.items() if not t.get("dark")]
        self.assertGreaterEqual(len(lights), 1 + len(self.NEW_LIGHT))
        for key in self.NEW_LIGHT:
            self.assertIn(key, theme.THEMES)
            self.assertFalse(theme.THEMES[key]["dark"], key)

    def test_is_dark_follows_dark_flag(self):
        for key, t in theme.THEMES.items():
            theme.apply_theme(key)
            self.assertEqual(theme.is_dark(), bool(t.get("dark")), key)

    def test_light_accents_are_distinct(self):
        """浅色主题主色两两不同，否则「多几款」只是重复。"""
        accents = [t["primary"].lower() for t in theme.THEMES.values()
                   if not t.get("dark")]
        self.assertEqual(len(set(accents)), len(accents))

    def test_body_text_contrast_is_readable(self):
        """正文色对各层底色的对比度 >= 7:1（WCAG AAA），不允许灰糊糊的字。"""
        for name, t in theme.THEMES.items():
            for bg in ("card_bg", "panel_bg", "window_bg"):
                self.assertGreaterEqual(
                    _contrast(t["text"], t[bg]), 7.0, f"{name}: text on {bg}")

    def test_non_default_themes_do_not_remap_existing_literals(self):
        """契约：非默认主题的令牌色值不得与 UI 源码里已出现的字面量重合。

        `_REVERSE`（颜色 -> 令牌）是先登记者优先，新主题一旦撞上既有字面量就会
        改写这条映射，从而**悄悄改变默认主题**下 map_colors 的结果
        （默认外观必须与改造前逐字一致）。#ffffff 例外：它早被
        light.window_bg 占位，且白色**背景**另有 _WHITE_BG_RE 特判。
        """
        import re
        from app.ui import theme as th
        color = re.compile(r"#([0-9a-fA-F]{6})(?![0-9a-fA-F])")
        base = os.path.dirname(os.path.dirname(os.path.abspath(th.__file__)))
        used = set()
        for folder in ("ui", ""):
            d = os.path.join(base, folder)
            for fn in os.listdir(d):
                if not fn.endswith(".py") or fn == "theme.py":
                    continue
                with open(os.path.join(d, fn), encoding="utf-8",
                          errors="replace") as fh:
                    used |= {"#" + m.group(1).lower()
                             for m in color.finditer(fh.read())}
        legacy = {k.lower() for k in th.LEGACY_COLOR_TOKENS}
        clashes = [f"{name}.{k}={t[k]}"
                   for name, t in th.THEMES.items() if name != th.DEFAULT_THEME
                   for k in th.TOKEN_KEYS
                   if t[k].lower() != "#ffffff" and t[k].lower() in used
                   and t[k].lower() not in legacy]
        self.assertEqual(clashes, [],
                         "非默认主题引入了对既有字面量的新映射：" + ", ".join(clashes))

    def test_preview_colors_come_from_that_theme(self):
        for key, t in theme.THEMES.items():
            self.assertEqual(theme.preview_colors(key),
                             (t["panel_bg"], t["primary"], t["text"]))

    def test_preview_colors_fall_back_to_default(self):
        d = theme.THEMES[theme.DEFAULT_THEME]
        self.assertEqual(theme.preview_colors("removed_theme"),
                         (d["panel_bg"], d["primary"], d["text"]))


class TestColorMapping(ThemeCase):
    def test_identity_under_light(self):
        """浅色主题：历史颜色原样保留（恒等映射，行为与改造前一致）。"""
        text = "QPushButton{background:#1668a8;color:#24292f;border:1px solid #d8dee4;}"
        self.assertEqual(theme.map_colors(text, "light"), text)

    def test_maps_to_dark(self):
        out = theme.map_colors("QPushButton{background:#1668a8;}", "silicon_dark")
        self.assertIn("#4a9eff", out)
        self.assertNotIn("#1668a8", out)

    def test_unknown_color_kept(self):
        # 用户自定义颜色（运行浮层等）不该被主题改掉
        out = theme.map_colors("QLabel{color:#00e676;}", "silicon_dark")
        self.assertIn("#00e676", out)

    def test_white_background_becomes_card(self):
        """白色**背景**在深色主题下要变深（否则白底浅字看不清）。"""
        out = theme.map_colors("QWidget{background: white;}", "silicon_dark")
        self.assertIn("#24262b", out)
        out2 = theme.map_colors("QWidget{background:#ffffff;}", "night")
        self.assertIn("#272a24", out2)

    def test_white_text_stays_white(self):
        """白色**文字**必须保持白色（彩色按钮上的白字）。"""
        out = theme.map_colors("QPushButton{color: white;background:#d64541;}",
                               "silicon_dark")
        self.assertIn("color: white", out)
        self.assertIn("#e05a56", out)

    def test_three_digit_colors(self):
        out = theme.map_colors("QLabel{color:#888;border:1px solid #bbb;}",
                               "silicon_dark")
        self.assertNotIn("#888", out)
        self.assertNotIn("1px solid #bbb", out)

    def test_does_not_eat_longer_hex(self):
        """3 位色值匹配不能吃掉 6 位色值的前缀（#eef0f1 不是 #eef + 0f1）。"""
        out = theme.map_colors("QLabel{color:#eef0f1;}", "light")
        self.assertIn("#eef0f1", out)

    def test_round_trip_between_themes(self):
        """任意主题之间来回切换都能回到原值（重放依赖这一点）。"""
        original = "QPushButton{background:#1668a8;color:white;}"
        dark = theme.map_colors(original, "silicon_dark")
        back = theme.map_colors(dark, "light")
        self.assertEqual(back, original)


class TestStyleHook(ThemeCase):
    def test_inline_style_registered_and_follows_theme(self):
        btn = QPushButton("运行")
        btn.setStyleSheet("QPushButton{background:#1668a8;color:white;}")
        btn.show()                                     # 可见控件才会立即重放
        self.assertIn("#1668a8", btn.styleSheet())     # 浅色下不变

        theme.apply_theme("silicon_dark")
        self.assertIn("#4a9eff", btn.styleSheet())     # 自动跟随

        theme.apply_theme("light")
        self.assertIn("#1668a8", btn.styleSheet())     # 切回来

    def test_concatenated_stylesheet_remaps(self):
        """main_window 那种「styleSheet() + 新片段」的拼接也要能重映射。"""
        w = QWidget()
        w.setStyleSheet("QPushButton{border:1px solid #c9d1d9;}")
        w.setStyleSheet(w.styleSheet() + "QTabBar::tab{color:#57606a;}")
        w.show()
        theme.apply_theme("night")
        ss = w.styleSheet()
        self.assertIn("#3a3f36", ss)
        self.assertIn("#a9b0a0", ss)

    def test_registry_is_weak(self):
        """注册表用弱引用：控件销毁后不应残留。"""
        w = QWidget()
        w.setStyleSheet("QLabel{color:#1668a8;}")
        before = len(theme.registered_styles())
        del w
        import gc
        gc.collect()
        self.assertLessEqual(len(theme.registered_styles()), before)

    def test_same_theme_reapply_skips_replay(self):
        """性能约定：同名主题重复应用不再重放（主窗口每次构造都会调用它，
        否则测试里反复构造窗口会累积成 O(N²)，真机也会白卡）。"""
        btn = QPushButton()
        btn.setStyleSheet("QLabel{color:#1668a8;}")
        theme.apply_theme("light")                  # 首次应用
        btn.setStyleSheet("QLabel{color:#00e676;}")  # 模拟「刚构造的新控件」
        theme.apply_theme("light")                  # 同名：不该把它回退成登记原文
        self.assertIn("#00e676", btn.styleSheet())

    def test_force_replays_registered_styles(self):
        btn = QPushButton()
        btn.setStyleSheet("QLabel{color:#1668a8;}")
        theme.apply_theme("silicon_dark")
        theme.apply_theme("light", force=True)
        self.assertIn("#1668a8", btn.styleSheet())

    def test_switching_theme_always_replays(self):
        btn = QPushButton()
        btn.setStyleSheet("QLabel{color:#1668a8;}")
        btn.show()
        theme.apply_theme("light")
        theme.apply_theme("night")
        self.assertIn("#7fb069", btn.styleSheet())

    def test_hidden_widget_replays_when_shown(self):
        """性能约定：切主题时隐藏的控件不当场重放，等它随窗口显示时补齐
        （否则隐藏的对话框/页面会白挨一遍 setStyleSheet 和重新 polish）。"""
        from PySide6.QtWidgets import QDialog, QLabel
        dlg = QDialog()
        lab = QLabel("x", dlg)
        lab.setStyleSheet("QLabel{color:#1668a8;}")
        self.assertFalse(dlg.isVisible())
        theme.apply_theme("silicon_dark")
        self.assertIn("#1668a8", lab.styleSheet())      # 隐藏中：暂未重放
        dlg.show()                                      # 显示时补齐
        self.assertIn("#4a9eff", lab.styleSheet())
        self.assertNotIn("#1668a8", lab.styleSheet())
        dlg.close()

    def test_visible_widget_replays_immediately(self):
        btn = QPushButton()
        btn.setStyleSheet("QLabel{color:#1668a8;}")
        btn.show()
        theme.apply_theme("silicon_dark")
        self.assertIn("#4a9eff", btn.styleSheet())
        btn.close()

    def test_window_baseline_applied(self):
        """基线 QSS 挂在**顶层窗口**上而不是应用级。

        性能原因：应用级 QSS 会让每个新建控件都参与样式匹配，
        实测 500 个控件的构造成本 +520ms（打开流程编辑对话框要多等 1~2 秒）。
        """
        from PySide6.QtWidgets import QDialog
        theme.apply_theme("silicon_dark")
        self.assertEqual(self._app.styleSheet(), "")     # 应用级不再设 QSS
        dlg = QDialog()
        dlg.show()                                       # 显示时套上基线
        ss = dlg.styleSheet()
        self.assertIn("#1c1e22", ss)                     # 深色输入框底
        self.assertIn("QComboBox", ss)
        self.assertIn("QScrollBar::handle", ss)
        self.assertIn("QHeaderView::section", ss)
        dlg.close()

    def test_window_own_styles_keep_baseline(self):
        """窗口自己设样式时，基线要一起带上（否则窗口级 QSS 会把基线顶掉）。"""
        from PySide6.QtWidgets import QDialog
        w = QDialog()
        w.setStyleSheet("QTabWidget::pane { border: none; }")
        ss = w.styleSheet()
        self.assertIn("QScrollBar::handle", ss)          # 基线在
        self.assertIn("QTabWidget::pane", ss)            # 自己的样式也在

    def test_baseline_follows_theme_switch(self):
        from PySide6.QtWidgets import QDialog
        dlg = QDialog()
        dlg.show()
        self.assertIn("#ffffff", dlg.styleSheet())       # 浅色输入框底
        theme.apply_theme("silicon_dark")
        self.assertIn("#1c1e22", dlg.styleSheet())
        dlg.close()

    def test_palette_follows_theme(self):
        theme.apply_theme("silicon_dark")
        from PySide6.QtGui import QPalette
        self.assertEqual(self._app.palette().color(QPalette.Window).name(),
                         "#17181b")
        theme.apply_theme("light")
        self.assertEqual(self._app.palette().color(QPalette.Window).name(),
                         "#ffffff")


class TestWindowQss(ThemeCase):
    def test_button_qss_uses_tokens(self):
        light = theme.build_button_qss("light")
        self.assertIn("#1668a8", light)
        self.assertIn("QPushButton#btnPrimary", light)
        dark = theme.build_button_qss("silicon_dark")
        self.assertIn("#4a9eff", dark)
        self.assertNotIn("#1668a8", dark)

    def test_tab_qss_uses_tokens(self):
        self.assertIn("#1668a8", theme.build_tab_qss("light"))
        self.assertIn("#4a9eff", theme.build_tab_qss("silicon_dark"))

    def test_variant_qss(self):
        qss = theme.build_variant_qss("primary", "light")
        self.assertIn("background:#1668a8", qss)
        self.assertIn(":hover", qss)
        self.assertIn(":pressed", qss)
        self.assertIn(":disabled", qss)
        self.assertEqual(theme.build_variant_qss("neutral", "light"), "")
        self.assertIn("#e05a56", theme.build_variant_qss("danger", "silicon_dark"))

    def test_set_variant_follows_theme(self):
        """187 个按钮都走 set_variant：切主题后颜色必须跟着变。"""
        from app.ui.widgets import set_variant
        btn = QPushButton("停止")
        set_variant(btn, "danger")
        btn.show()
        self.assertIn("#d64541", btn.styleSheet())
        theme.apply_theme("silicon_dark")
        self.assertIn("#e05a56", btn.styleSheet())


class TestListeners(ThemeCase):
    def test_listener_called_on_switch(self):
        calls = []
        theme.register_listener(lambda: calls.append(1))
        theme.apply_theme("night")
        self.assertEqual(len(calls), 1)

    def test_bound_method_listener_is_weak(self):
        class Holder:
            called = False

            def on_theme(self):
                Holder.called = True

        h = Holder()
        theme.register_listener(h.on_theme)
        theme.apply_theme("night")
        self.assertTrue(Holder.called)

        Holder.called = False
        del h
        import gc
        gc.collect()
        theme.apply_theme("light")          # 已销毁对象不该再被调用（也不该报错）
        self.assertFalse(Holder.called)

    def test_listener_exception_does_not_break_switch(self):
        def boom():
            raise RuntimeError("boom")

        theme.register_listener(boom)
        with self.assertLogs("app.ui.theme", level="ERROR"):   # 回调出错只记日志
            theme.apply_theme("night")                        # 不应抛出
        self.assertEqual(theme.current_name(), "night")

    def test_duplicate_registration_ignored(self):
        calls = []

        def cb():
            calls.append(1)

        theme.register_listener(cb)
        theme.register_listener(cb)
        theme.apply_theme("night")
        self.assertEqual(len(calls), 1)


class TestNoUnmappedLightColors(ThemeCase):
    """契约：UI 代码里的**浅色**硬编码必须登记进主题映射表。

    浅色是深色主题的杀手——残留一个浅底就会变成「深底浅字」或「浅底浅字」
    的看不清组合（本次开发就踩到 #e9f0f8 等 16 个）。
    浮层类模块例外：那些颜色是用户可自行配置的外观（运行浮层/倒计时/通知）。
    """

    # 用户自定义外观的模块：颜色不算主题色
    FLOATING = {
        "running_overlay.py", "power_overlay.py", "capture_overlay.py",
        "notify_actor.py", "overlay_actor.py", "theme.py",
    }

    def test_light_hardcoded_colors_are_mapped(self):
        import os
        import re
        from app.ui import theme as th
        color = re.compile(r"#([0-9a-fA-F]{6})(?![0-9a-fA-F])")
        base = os.path.dirname(os.path.dirname(os.path.abspath(th.__file__)))
        mapped = {k.lower() for k in th.LEGACY_COLOR_TOKENS}
        # 纯白单独特判：背景里的 white/#ffffff 会变深（_WHITE_BG_RE），
        # 彩色按钮上的白字则必须保持白色，所以不进映射表。
        mapped.add("#ffffff")
        offenders = []
        for folder in ("ui", ""):
            d = os.path.join(base, folder)
            for fn in os.listdir(d):
                if not fn.endswith(".py") or fn in self.FLOATING:
                    continue
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
                for m in color.finditer(src):
                    value = "#" + m.group(1).lower()
                    r, g, b = (int(value[1:3], 16), int(value[3:5], 16),
                               int(value[5:7], 16))
                    if 0.299 * r + 0.587 * g + 0.114 * b > 190 and value not in mapped:
                        offenders.append(f"{fn}: {value}")
        self.assertEqual(sorted(set(offenders)), [],
                         "以下浅色未纳入主题映射（深色主题下会残留浅底）："
                         + ", ".join(sorted(set(offenders))))


class TestWiring(ThemeCase):
    """接线契约：启动应用主题、设置页可切换、配置持久化。"""

    def _read(self, rel):
        import app.ui
        base = os.path.dirname(os.path.abspath(app.ui.__file__))
        with open(os.path.join(base, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_main_window_applies_saved_theme(self):
        src = self._read("main_window.py")
        self.assertIn("theme.apply_theme(getattr(cfg, \"ui_theme\", \"light\"))", src)
        self.assertIn("theme.register_listener(self._apply_window_theme)", src)

    def test_window_theme_uses_theme_builders(self):
        src = self._read("main_window.py")
        self.assertIn("theme.build_button_qss() + theme.build_tab_qss()", src)

    def test_settings_tab_has_theme_combo(self):
        src = self._read("settings_tab.py")
        self.assertIn("theme.theme_names()", src)
        self.assertIn("theme.apply_theme(self.theme_value())", src)

    def test_settings_changed_persists_theme(self):
        src = self._read("main_window.py")
        self.assertIn("self.cfg.ui_theme = self.settings_tab.theme_value()", src)

    def test_config_field_default_and_load(self):
        from app.config import AppConfig
        self.assertEqual(AppConfig().ui_theme, "light")

    def test_config_load_reads_and_sanitizes(self):
        from app.config import AppConfig
        from tests._env import TempConfigPaths, write_json
        with TempConfigPaths() as tmp:
            write_json(os.path.join(tmp, "config.json"), {"ui_theme": "  SILICON_DARK "})
            self.assertEqual(AppConfig.load().ui_theme, "silicon_dark")
        with TempConfigPaths() as tmp:
            write_json(os.path.join(tmp, "config.json"), {"ui_theme": ""})
            self.assertEqual(AppConfig.load().ui_theme, "light")


class TestSettingsTabTheme(ThemeCase):
    def _tab(self, cfg=None):
        from app.config import AppConfig
        from app.ui.settings_tab import SettingsTab
        cfg = cfg or AppConfig()
        return SettingsTab(cfg), cfg

    def test_combo_lists_all_themes_and_selects_saved(self):
        from app.config import AppConfig
        cfg = AppConfig()
        cfg.ui_theme = "blue_tech"
        tab, _ = self._tab(cfg)
        self.assertEqual(tab.theme_combo.count(), len(theme.THEMES))
        self.assertEqual(tab.theme_value(), "blue_tech")

    def test_picking_theme_applies_immediately(self):
        tab, _ = self._tab()
        idx = tab.theme_combo.findData("night")
        tab.theme_combo.setCurrentIndex(idx)
        self.assertEqual(theme.current_name(), "night")
        self.assertEqual(tab.theme_value(), "night")

    def test_unknown_saved_theme_falls_back_to_default_in_combo(self):
        from app.config import AppConfig
        cfg = AppConfig()
        cfg.ui_theme = "removed_theme"
        tab, _ = self._tab(cfg)
        self.assertEqual(tab.theme_value(), theme.DEFAULT_THEME)

    def test_combo_items_have_swatch_icons(self):
        """每一项都带色卡（QPainter 画的图，不是 QSS），否则下拉只剩文字看不出配色。"""
        tab, _ = self._tab()
        self.assertEqual(tab.theme_combo.iconSize().width(), 34)
        for i in range(tab.theme_combo.count()):
            icon = tab.theme_combo.itemIcon(i)
            self.assertFalse(icon.isNull(), tab.theme_combo.itemText(i))
            self.assertFalse(icon.pixmap(34, 16).isNull())

    def test_swatch_shows_its_own_theme_not_the_active_one(self):
        """色卡画的必须是「该项主题自己的颜色」——若走了主题映射就会显示成当前主题色。"""
        from app.ui.settings_tab import _theme_swatch
        theme.apply_theme("silicon_dark")               # 当前生效主题 = 深色
        img = _theme_swatch("warm_paper").pixmap(34, 16).toImage()
        self.assertEqual(QColor(img.pixel(3, 8)).name(),
                         theme.THEMES["warm_paper"]["primary"])
        self.assertEqual(QColor(img.pixel(30, 8)).name(),
                         theme.THEMES["warm_paper"]["text"])


class TestFramelessDialog(ThemeCase):
    """无边框编辑弹窗（app/ui/frameless.py）：自绘标题栏 + 整窗统一边框。"""

    def _dlg(self):
        from app.ui.frameless import FramelessDialog
        d = FramelessDialog()
        d.setWindowTitle("测试标题")
        return d

    def test_frameless_flags(self):
        d = self._dlg()
        self.assertTrue(d.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(d.testAttribute(Qt.WA_TranslucentBackground))

    def test_title_label_syncs_with_window_title(self):
        d = self._dlg()
        self.assertEqual(d._title_label.text(), "测试标题")

    def test_body_is_layout_container(self):
        from PySide6.QtWidgets import QPushButton, QVBoxLayout
        d = self._dlg()
        lay = QVBoxLayout(d.body())
        lay.addWidget(QPushButton("x"))
        self.assertIsNotNone(d.body().layout())

    def test_close_button_rejects_without_error(self):
        d = self._dlg()
        d.show()
        d._close_btn.click()          # 触发 reject（隐藏窗口），不抛异常
        d.close()

    def test_card_qss_has_border_and_titlebar(self):
        from app.ui import frameless
        qss = frameless._build_qss()
        self.assertIn("QFrame#dlgCard", qss)
        self.assertIn("border: 1px solid", qss)
        self.assertIn("QWidget#dlgTitleBar", qss)
        self.assertIn("#93a3b0", qss)        # 浅色边框（用户要求调浅）


if __name__ == "__main__":
    unittest.main()
