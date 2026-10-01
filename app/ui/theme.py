# -*- coding: utf-8 -*-
"""统一主题引擎：全程序配色令牌 + 全局基线 QSS + 实时切换（2026-09-27）。

设计目标（用户需求：整体界面美化 + 设置页切换主题 + 持久化）
- **令牌化**：所有界面颜色写成语义令牌（primary / border / text_dim ...），
  主题 = 一组令牌值。切换主题 = 换一组值，界面整体跟着变。
- **浅色简洁**为默认主题，其令牌值与本项目原有配色逐一对应（#1668a8 主蓝、
  #24292f 主文字、#f7f9fb 面板底、#d8dee4 边框…），所以默认外观延续既有观感，
  只是把散落各处的颜色统一收编、并补上控件级基线样式（圆角/悬停/选中/滚动条）。
- **零侵入**：`install_hook()` 拦截 `QWidget.setStyleSheet`，把历史遗留的
  内联样式（全项目 140+ 处硬编码颜色）自动登记 + 按当前主题映射颜色；
  切换主题时重放这些样式。UI 代码一行不用改就整体跟随主题。
- **可回退**：默认主题下映射结果为原值（恒等映射），行为与改造前一致。
- **浅色家族（2026-10-01 扩充）**：除默认「浅色简洁」外另有 6 套浅色主题——
  暖阳米白 / 薄荷青竹 / 樱粉柔光 / 海盐青蓝 / 紫罗兰 / 石墨灰白。它们只是
  另一组令牌值（同一套键、同样走 QSS 生成器），不新增任何代码路径；
  新增色值刻意避开了 UI 源码里已出现的字面量，保证历史内联样式的
  颜色映射结果与扩充前**逐字相同**（默认主题观感不受影响）。

**全局字号缩放（2026-10-01）**：除配色外，本模块还统一管**排版尺度**——
`set_font_scale(pct)` 按百分比缩放**应用字体**与**所有 QSS 里的 font-size**。
因为全部内联样式都走同一个 hook，140+ 处 UI 代码一行不用改，
「设置页 -> 界面外观 -> 界面文字大小」就能一次性调全程序所有页面的文字大小，
各页面之间的大小比例保持不变（不会出现「这个页面变了、那个页面没变」）。

三层样式优先级（Qt 原生规则，本模块利用这一点）：
    应用级  QApplication.setStyleSheet(base_qss)     ← build_base_qss，通用控件基线
    窗口级  main_window.setStyleSheet(...)            ← 走 hook 自动登记/映射
    控件级  widget.setStyleSheet(...)                 ← 走 hook 自动登记/映射

铁律（沿用本项目 UI 约定）：QSS 只用**控件类型选择器**，不用 objectName ID 选择器，
避免后代选择器跨 widget 树级联污染（历史事故：schedule_tab 右栏红染）。
"""
from __future__ import annotations

import logging
import re
import weakref

log = logging.getLogger(__name__)

from PySide6.QtCore import QEvent, QObject

# 全局字体百分比的取值范围与默认值定义在 config（纯 Python，供 gui 之外的代码读取），
# 这里只做引用，避免两处各写一份而漂移。
from ..config import (UI_FONT_SCALE_DEFAULT, UI_FONT_SCALE_MAX,
                      UI_FONT_SCALE_MIN)

# ---------------------------------------------------------------------------
# 主题令牌
# ---------------------------------------------------------------------------
# 令牌名 -> 用途。所有主题必须提供同一组键（有测试兜底）。
TOKEN_KEYS = (
    "window_bg", "panel_bg", "card_bg", "border", "border_light",
    "text", "text_dim", "text_muted", "text_disabled",
    "primary", "primary_hover", "primary_pressed", "primary_border", "primary_soft",
    "success", "success_hover", "success_pressed", "success_border",
    "danger", "danger_hover", "danger_pressed", "danger_border",
    "warn", "ok", "dot_off", "run_marker",
    "danger_soft", "success_soft", "warn_soft", "dialog_border",
    "disabled_bg", "disabled_fg",
    "input_bg", "input_border",
    "hover_bg", "pressed_bg", "sel_bg", "sel_fg",
    "scrollbar", "scrollbar_hover",
    "header_bg", "menu_bg", "menu_sel",
)

# 主题显示顺序即设置页下拉顺序；第一项为默认。
THEMES: dict[str, dict] = {
    # ① 浅色简洁（默认）：延续本项目原有配色
    "light": {
        "label": "浅色简洁",
        "dark": False,
        "window_bg": "#ffffff", "panel_bg": "#f7f9fb", "card_bg": "#ffffff",
        "border": "#d8dee4", "border_light": "#e6eaef",
        "text": "#24292f", "text_dim": "#57606a", "text_muted": "#8a939c",
        "text_disabled": "#aab2bb",
        "primary": "#1668a8", "primary_hover": "#1d78c0",
        "primary_pressed": "#125a93", "primary_border": "#125a93",
        "primary_soft": "#e8f1fa",
        "success": "#2f9e5b", "success_hover": "#35b168",
        "success_pressed": "#278a4f", "success_border": "#278a4f",
        "danger": "#d64541", "danger_hover": "#e2544f",
        "danger_pressed": "#c0392b", "danger_border": "#c0392b",
        "warn": "#c0392b", "ok": "#2ecc71", "dot_off": "#95a5a6",
        "run_marker": "#27ae60",
        "danger_soft": "#fdecea", "success_soft": "#e6f4ea", "warn_soft": "#fdf3e2",
        "dialog_border": "#93a3b0",
        "disabled_bg": "#b9c2cb", "disabled_fg": "#f0f3f6",
        "input_bg": "#ffffff", "input_border": "#c9d1d9",
        "hover_bg": "#f2f4f6", "pressed_bg": "#e3edf7",
        "sel_bg": "#e3edf7", "sel_fg": "#24292f",
        "scrollbar": "#c9d1d9", "scrollbar_hover": "#aab2bb",
        "header_bg": "#f2f4f6", "menu_bg": "#ffffff", "menu_sel": "#e8f1fa",
    },
    # ② 暖阳米白：米黄纸感底 + 琥珀棕强调（长时间阅读护眼，偏暖）
    "warm_paper": {
        "label": "暖阳米白",
        "dark": False,
        "window_bg": "#fbf7f0", "panel_bg": "#f5efe3", "card_bg": "#fffdf8",
        "border": "#e6dbc7", "border_light": "#efe7d7",
        "text": "#3b342b", "text_dim": "#6b6152", "text_muted": "#9c9184",
        "text_disabled": "#c0b6a5",
        "primary": "#b3612a", "primary_hover": "#c77235",
        "primary_pressed": "#9a5021", "primary_border": "#9a5021",
        "primary_soft": "#f7e8d8",
        "success": "#4e8c3f", "success_hover": "#5c9f4a",
        "success_pressed": "#417530", "success_border": "#417530",
        "danger": "#c0503f", "danger_hover": "#d15f4c",
        "danger_pressed": "#a3422f", "danger_border": "#a3422f",
        "warn": "#b8791c", "ok": "#5aa84a", "dot_off": "#a89e90",
        "run_marker": "#4e8c3f",
        "danger_soft": "#fbe9e4", "success_soft": "#eaf3e2", "warn_soft": "#fdf1dc",
        "dialog_border": "#bfae94",
        "disabled_bg": "#d8cdb9", "disabled_fg": "#f6f1e7",
        "input_bg": "#fffefb", "input_border": "#dccfb8",
        "hover_bg": "#f2ebdd", "pressed_bg": "#ecdfc9",
        "sel_bg": "#f3e3cd", "sel_fg": "#3b342b",
        "scrollbar": "#d9cdb6", "scrollbar_hover": "#c1b298",
        "header_bg": "#f3ece0", "menu_bg": "#fffdf8", "menu_sel": "#f7e8d8",
    },
    # ③ 薄荷青竹：清透淡绿底 + 竹青强调（清爽自然）
    "mint": {
        "label": "薄荷青竹",
        "dark": False,
        "window_bg": "#f7fcf9", "panel_bg": "#edf7f1", "card_bg": "#ffffff",
        "border": "#d2e6da", "border_light": "#e2f1e8",
        "text": "#1e2f27", "text_dim": "#4d6a5c", "text_muted": "#88a396",
        "text_disabled": "#b2c8bd",
        "primary": "#17805b", "primary_hover": "#1e9a6e",
        "primary_pressed": "#12694a", "primary_border": "#12694a",
        "primary_soft": "#e0f3ea",
        "success": "#2a9556", "success_hover": "#34a862",
        "success_pressed": "#237c47", "success_border": "#237c47",
        "danger": "#cf4a4a", "danger_hover": "#e05a5a",
        "danger_pressed": "#b03c3c", "danger_border": "#b03c3c",
        "warn": "#c07a2a", "ok": "#34c07a", "dot_off": "#9db3a8",
        "run_marker": "#2a9556",
        "danger_soft": "#fceceb", "success_soft": "#e3f5ec", "warn_soft": "#fdf2e0",
        "dialog_border": "#9fbdb0",
        "disabled_bg": "#bdd2c7", "disabled_fg": "#eef5f1",
        "input_bg": "#ffffff", "input_border": "#c6ddd2",
        "hover_bg": "#eaf5ef", "pressed_bg": "#dcefe5",
        "sel_bg": "#d9f0e5", "sel_fg": "#1e2f27",
        "scrollbar": "#c3dacf", "scrollbar_hover": "#a9c7ba",
        "header_bg": "#eaf5ef", "menu_bg": "#ffffff", "menu_sel": "#e0f3ea",
    },
    # ④ 樱粉柔光：淡粉底 + 玫瑰强调（柔和轻盈）
    "sakura": {
        "label": "樱粉柔光",
        "dark": False,
        "window_bg": "#fdf8fa", "panel_bg": "#fbeef3", "card_bg": "#fffcfd",
        "border": "#f0d9e2", "border_light": "#f7e6ec",
        "text": "#382a30", "text_dim": "#6b525c", "text_muted": "#a48d97",
        "text_disabled": "#c9b6be",
        "primary": "#c04a7d", "primary_hover": "#d25c8e",
        "primary_pressed": "#a53a68", "primary_border": "#a53a68",
        "primary_soft": "#fbe4ee",
        "success": "#3f9e6f", "success_hover": "#4ab37f",
        "success_pressed": "#338059", "success_border": "#338059",
        "danger": "#d14b58", "danger_hover": "#e05d69",
        "danger_pressed": "#b13a46", "danger_border": "#b13a46",
        "warn": "#c98a2e", "ok": "#3fbf85", "dot_off": "#b4a0a8",
        "run_marker": "#3f9e6f",
        "danger_soft": "#fdeaee", "success_soft": "#e6f5ef", "warn_soft": "#fdf3e4",
        "dialog_border": "#c9a7b5",
        "disabled_bg": "#dcc7cf", "disabled_fg": "#f7eef1",
        "input_bg": "#ffffff", "input_border": "#e6ccd7",
        "hover_bg": "#f9ecf1", "pressed_bg": "#f3dde6",
        "sel_bg": "#f8dfea", "sel_fg": "#382a30",
        "scrollbar": "#e0c9d2", "scrollbar_hover": "#c9aab7",
        "header_bg": "#f9ecf1", "menu_bg": "#ffffff", "menu_sel": "#fbe4ee",
    },
    # ⑤ 海盐青蓝：清透海青底 + 湖水青强调（干净通透）
    "sea_salt": {
        "label": "海盐青蓝",
        "dark": False,
        "window_bg": "#f6fafb", "panel_bg": "#eaf4f6", "card_bg": "#ffffff",
        "border": "#cfe3e8", "border_light": "#e0eff3",
        "text": "#1c2f34", "text_dim": "#4a6a72", "text_muted": "#86a2aa",
        "text_disabled": "#b0c6cc",
        "primary": "#0d7d8a", "primary_hover": "#1294a3",
        "primary_pressed": "#0a6a76", "primary_border": "#0a6a76",
        "primary_soft": "#ddf1f4",
        "success": "#2f9d7a", "success_hover": "#38b08a",
        "success_pressed": "#268063", "success_border": "#268063",
        "danger": "#d0524a", "danger_hover": "#e06159",
        "danger_pressed": "#b0403a", "danger_border": "#b0403a",
        "warn": "#c2861f", "ok": "#34bf8f", "dot_off": "#9cb2b8",
        "run_marker": "#2f9d7a",
        "danger_soft": "#fcebe8", "success_soft": "#e2f4ee", "warn_soft": "#fdf2de",
        "dialog_border": "#9dbcc4",
        "disabled_bg": "#bcd0d5", "disabled_fg": "#edf4f6",
        "input_bg": "#ffffff", "input_border": "#c3dbe1",
        "hover_bg": "#e8f3f6", "pressed_bg": "#d9ebef",
        "sel_bg": "#d6eef2", "sel_fg": "#1c2f34",
        "scrollbar": "#c1d8de", "scrollbar_hover": "#a6c3cb",
        "header_bg": "#e8f3f6", "menu_bg": "#ffffff", "menu_sel": "#ddf1f4",
    },
    # ⑥ 紫罗兰：浅紫底 + 紫罗兰强调（优雅安静）
    "lavender": {
        "label": "紫罗兰",
        "dark": False,
        "window_bg": "#faf9fe", "panel_bg": "#f2f0fb", "card_bg": "#ffffff",
        "border": "#ddd8f0", "border_light": "#eae6f8",
        "text": "#2a2740", "text_dim": "#575070", "text_muted": "#9089a8",
        "text_disabled": "#bab4cb",
        "primary": "#6a52c4", "primary_hover": "#7b63d6",
        "primary_pressed": "#57409f", "primary_border": "#57409f",
        "primary_soft": "#ebe6fa",
        "success": "#35986c", "success_hover": "#3fae7c",
        "success_pressed": "#2a7c57", "success_border": "#2a7c57",
        "danger": "#cf4d5f", "danger_hover": "#e05e70",
        "danger_pressed": "#ae3c4d", "danger_border": "#ae3c4d",
        "warn": "#c08420", "ok": "#4bbf8a", "dot_off": "#a9a3bb",
        "run_marker": "#35986c",
        "danger_soft": "#fcecef", "success_soft": "#e4f4ec", "warn_soft": "#fdf2df",
        "dialog_border": "#a99fc8",
        "disabled_bg": "#c8c2da", "disabled_fg": "#f1eff8",
        "input_bg": "#ffffff", "input_border": "#d5cfeb",
        "hover_bg": "#f1effa", "pressed_bg": "#e5e0f6",
        "sel_bg": "#e6e1f8", "sel_fg": "#2a2740",
        "scrollbar": "#cdc7e0", "scrollbar_hover": "#b3abc9",
        "header_bg": "#f1effa", "menu_bg": "#ffffff", "menu_sel": "#ebe6fa",
    },
    # ⑦ 石墨灰白：中性灰底 + 石板蓝强调（冷静商务，低饱和）
    "graphite": {
        "label": "石墨灰白",
        "dark": False,
        "window_bg": "#fafbfc", "panel_bg": "#f0f2f5", "card_bg": "#ffffff",
        "border": "#d5dae0", "border_light": "#e5e9ed",
        "text": "#1c2126", "text_dim": "#525a63", "text_muted": "#868f99",
        "text_disabled": "#b0b7bf",
        "primary": "#44546e", "primary_hover": "#526484",
        "primary_pressed": "#35435a", "primary_border": "#35435a",
        "primary_soft": "#e6eaf1",
        "success": "#3d8f63", "success_hover": "#479f70",
        "success_pressed": "#327650", "success_border": "#327650",
        "danger": "#c44b4b", "danger_hover": "#d55a5a",
        "danger_pressed": "#a63c3c", "danger_border": "#a63c3c",
        "warn": "#b9791f", "ok": "#3fae7d", "dot_off": "#9aa3ac",
        "run_marker": "#3d8f63",
        "danger_soft": "#faecec", "success_soft": "#e5f2ea", "warn_soft": "#fcf2e1",
        "dialog_border": "#9aa5b1",
        "disabled_bg": "#c3cad1", "disabled_fg": "#eef0f3",
        "input_bg": "#ffffff", "input_border": "#cdd4db",
        "hover_bg": "#eef1f5", "pressed_bg": "#e2e7ec",
        "sel_bg": "#e3e9f0", "sel_fg": "#1c2126",
        "scrollbar": "#c7ced5", "scrollbar_hover": "#aeb7c0",
        "header_bg": "#eef1f5", "menu_bg": "#ffffff", "menu_sel": "#e6eaf1",
    },
    # ⑧ 深色硅基：参照 SiliconUI 深色观感（近黑面板 + 亮蓝强调）
    "silicon_dark": {
        "label": "深色硅基",
        "dark": True,
        "window_bg": "#17181b", "panel_bg": "#1f2126", "card_bg": "#24262b",
        "border": "#34373d", "border_light": "#2c2f34",
        "text": "#e6e8eb", "text_dim": "#aab2bb", "text_muted": "#8a939c",
        "text_disabled": "#6b7379",
        "primary": "#4a9eff", "primary_hover": "#6bb0ff",
        "primary_pressed": "#3a8ae6", "primary_border": "#3a8ae6",
        "primary_soft": "#23303d",
        "success": "#3fb96b", "success_hover": "#4fca7c",
        "success_pressed": "#35a05b", "success_border": "#35a05b",
        "danger": "#e05a56", "danger_hover": "#ee6b66",
        "danger_pressed": "#c94a46", "danger_border": "#c94a46",
        "warn": "#ef6b62", "ok": "#4ec97a", "dot_off": "#6b7379",
        "run_marker": "#4ec97a",
        "danger_soft": "#3a2626", "success_soft": "#1f3327", "warn_soft": "#3a3226",
        "dialog_border": "#6e7684",
        "disabled_bg": "#3a3d42", "disabled_fg": "#8a939c",
        "input_bg": "#1c1e22", "input_border": "#3a3d42",
        "hover_bg": "#2a2d33", "pressed_bg": "#31353c",
        "sel_bg": "#2b3a4a", "sel_fg": "#e6e8eb",
        "scrollbar": "#3f434a", "scrollbar_hover": "#565b63",
        "header_bg": "#24262b", "menu_bg": "#24262b", "menu_sel": "#2b3a4a",
    },
    # ⑨ 蓝色科技：深蓝底 + 青蓝强调
    "blue_tech": {
        "label": "蓝色科技",
        "dark": True,
        "window_bg": "#0f1720", "panel_bg": "#152230", "card_bg": "#1a2a3a",
        "border": "#26445c", "border_light": "#1f3648",
        "text": "#e3eef7", "text_dim": "#a8c2d6", "text_muted": "#7f9cb3",
        "text_disabled": "#5d768c",
        "primary": "#25a8f5", "primary_hover": "#48bcfb",
        "primary_pressed": "#1b8ed4", "primary_border": "#1b8ed4",
        "primary_soft": "#12324a",
        "success": "#2fbf8f", "success_hover": "#43d2a2",
        "success_pressed": "#28a67b", "success_border": "#28a67b",
        "danger": "#e2574f", "danger_hover": "#f0685f",
        "danger_pressed": "#c94943", "danger_border": "#c94943",
        "warn": "#f0883e", "ok": "#37d39b", "dot_off": "#5d768c",
        "run_marker": "#37d39b",
        "danger_soft": "#3a2323", "success_soft": "#16352c", "warn_soft": "#3a2f1c",
        "dialog_border": "#567fa3",
        "disabled_bg": "#22384a", "disabled_fg": "#7f9cb3",
        "input_bg": "#121e2a", "input_border": "#26445c",
        "hover_bg": "#1b2c3d", "pressed_bg": "#20374b",
        "sel_bg": "#17405c", "sel_fg": "#e3eef7",
        "scrollbar": "#2b4a63", "scrollbar_hover": "#3c6383",
        "header_bg": "#1a2a3a", "menu_bg": "#1a2a3a", "menu_sel": "#17405c",
    },
    # ⑩ 暗夜护眼：低亮度暖灰绿
    "night": {
        "label": "暗夜护眼",
        "dark": True,
        "window_bg": "#1b1d1a", "panel_bg": "#22251f", "card_bg": "#272a24",
        "border": "#3a3f36", "border_light": "#31352d",
        "text": "#dfe3d8", "text_dim": "#a9b0a0", "text_muted": "#87907c",
        "text_disabled": "#6b7361",
        "primary": "#7fb069", "primary_hover": "#93c47d",
        "primary_pressed": "#6b9a58", "primary_border": "#6b9a58",
        "primary_soft": "#2c3626",
        "success": "#7fb069", "success_hover": "#93c47d",
        "success_pressed": "#6b9a58", "success_border": "#6b9a58",
        "danger": "#d9705f", "danger_hover": "#e58573",
        "danger_pressed": "#bf5c4d", "danger_border": "#bf5c4d",
        "warn": "#d9a05f", "ok": "#8cc97a", "dot_off": "#6b7361",
        "run_marker": "#8cc97a",
        "danger_soft": "#3a2a26", "success_soft": "#26352a", "warn_soft": "#3a3327",
        "dialog_border": "#6e7a5f",
        "disabled_bg": "#3a3f36", "disabled_fg": "#87907c",
        "input_bg": "#1e211c", "input_border": "#3a3f36",
        "hover_bg": "#2d312a", "pressed_bg": "#343930",
        "sel_bg": "#37422f", "sel_fg": "#dfe3d8",
        "scrollbar": "#434a3d", "scrollbar_hover": "#565f4d",
        "header_bg": "#272a24", "menu_bg": "#272a24", "menu_sel": "#37422f",
    },
}

DEFAULT_THEME = "light"
DEFAULT_FONT_PT = 10        # 应用基准字号（未缩放）；实际字号 = 基准 × 字体百分比

# 历史遗留字面量 -> 令牌（不在表里的颜色原样保留：例如用户自定义的浮层颜色）。
# 这些值是改造前散落在各 UI 文件里的硬编码色，收编后由主题统一管理。
LEGACY_COLOR_TOKENS: dict[str, str] = {
    # 主色系
    "#1668a8": "primary", "#1d78c0": "primary_hover", "#125a93": "primary_pressed",
    "#e8f1fa": "primary_soft", "#eef5fb": "primary_soft", "#f3f8fd": "primary_soft",
    "#e3edf7": "pressed_bg",
    # 语义色
    "#2f9e5b": "success", "#35b168": "success_hover", "#278a4f": "success_pressed",
    "#27ae60": "run_marker", "#2ecc71": "ok",
    "#d64541": "danger", "#e2544f": "danger_hover", "#c0392b": "danger_pressed",
    "#e53935": "danger", "#95a5a6": "dot_off",
    # 文字
    "#24292f": "text", "#57606a": "text_dim", "#8a939c": "text_muted",
    "#aab2bb": "text_disabled", "#a7afb8": "text_muted", "#888": "text_muted",
    # 面板与边框
    "#f7f9fb": "panel_bg", "#f2f4f6": "hover_bg", "#d8dee4": "border",
    "#c9d1d9": "input_border", "#e1e4e8": "border_light", "#e6eaef": "border_light",
    "#bbb": "input_border", "#f5f5f5": "hover_bg", "#fbfcfd": "input_bg",
    "#ddd": "border", "#f0f3f6": "disabled_fg", "#b9c2cb": "disabled_bg",
    "#f2f3f5": "hover_bg",
    # 少量深色主题下会「浅底配浅字」的组合（模块自绘 / 选标识）
    "#dcf5e7": "sel_bg", "#177a45": "success_pressed",
    "#125a92": "primary_pressed", "#8ab8d8": "primary_hover",
    "#b9d3e8": "primary_hover", "#e5484d": "danger",
    # 选中 / 悬停 / 提示底色（深色主题下必须一起变深，否则浅底配浅字）
    "#e9f0f8": "sel_bg", "#dce8f4": "sel_bg", "#d5e6f5": "sel_bg",
    "#eaf3fb": "primary_soft", "#eaf2fa": "primary_soft",
    "#f2f5f8": "hover_bg", "#e3e8ee": "hover_bg", "#e8eef4": "hover_bg",
    "#eef1f4": "hover_bg", "#eeeeee": "hover_bg",
    "#fdeeee": "danger_soft", "#fbe9e7": "danger_soft", "#fdecea": "danger_soft",
    "#e3f2ea": "success_soft", "#e6f4ea": "success_soft",
    "#fdf3e2": "warn_soft",
}

# 「白色背景」单独处理：白色在 QSS 里既可能是底色（深色主题下要变深）、
# 也可能是彩色按钮上的白字（必须保持白色），所以只在 background 上下文里替换。
_WHITE_BG_RE = re.compile(
    r"(background(?:-color)?\s*:\s*)(white|#fff(?:fff)?)(\s*[;}]|\s*$)",
    re.IGNORECASE)

_THEME = DEFAULT_THEME           # 当前生效的主题名
_FONT_SCALE = UI_FONT_SCALE_DEFAULT   # 全局字体百分比（100 = 原始大小，见 set_font_scale）
_APPLIED = False                 # 是否至少完整应用过一次（供同名跳过判断）
_REVERSE: dict[str, str] = {}    # 颜色值(小写) -> 令牌名（跨主题反查，用于重放时重映射）
_REGISTRY: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
# 切换主题时「当前不可见」的控件：不立刻重放（对隐藏控件逐个 setStyleSheet 是
# 纯浪费，而且会连带触发 Qt 全量重新 polish），等它随窗口显示时再补上（见 _flush_pending）。
_PENDING: "weakref.WeakSet" = weakref.WeakSet()
_FILTER = None                   # Show 事件过滤器（换 QApplication 时重建）
_BASELINE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # 顶层窗口 -> 已用主题
# 主题变化回调：绑定方法用 WeakMethod（窗口/面板被销毁后自动失效，
# 不会因为全局列表持有强引用而拖住它们），普通函数退化为强引用。
_LISTENER_METHODS: list = []
_LISTENER_FUNCS: list = []
_HOOKED = False
_ORIG_SET_STYLE = None

# 同时匹配 6 位与 3 位色值；6 位优先，且用先行断言保证不吃掉更长色值的前缀
# （如 #eef5fb 不能被当成 #eef）。
_COLOR_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")

# QSS 里的字号：`font-size: 9pt` / `font-size:10px`。只匹配 font-size 这一条属性，
# 其它尺寸（padding/margin/height/width）不参与缩放。
_FONT_SIZE_RE = re.compile(r"(font-size\s*:\s*)(\d+(?:\.\d+)?)\s*(pt|px)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 基础查询
# ---------------------------------------------------------------------------

def theme_names() -> list[tuple[str, str]]:
    """[(主题键, 显示名), ...]，顺序即设置页下拉顺序。"""
    return [(k, v["label"]) for k, v in THEMES.items()]


def theme_label(name: str) -> str:
    return THEMES.get(name, THEMES[DEFAULT_THEME])["label"]


def preview_colors(name: str) -> tuple[str, str, str]:
    """设置页色卡用的三个代表色：(面板底, 主色, 正文色)。

    直接取令牌值，**不经过 map_colors**——色卡要展示的是「这套主题真实的颜色」，
    与当前生效的主题无关（否则选中暖阳米白时会显示成当前主题的颜色）。
    """
    t = THEMES[normalize_theme(name)]
    return (t["panel_bg"], t["primary"], t["text"])


def normalize_theme(name) -> str:
    """非法/空主题名回退默认（手改 config.json 也不会让程序起不来）。"""
    return name if name in THEMES else DEFAULT_THEME


def current_name() -> str:
    return _THEME


def current() -> dict:
    return THEMES[_THEME]


def token(name: str) -> str:
    """取当前主题的令牌颜色；未知令牌返回原样（便于排错时一眼看出）。"""
    return THEMES[_THEME].get(name, name)


def normalize_font_scale(value) -> int:
    """把任意输入规整成合法百分比（越界 clamp，非法回落默认）。

    手改 config.json 写了个 500 也不会把界面撑爆——加载时由 config.clamp 兜一次，
    这里是第二道（设置页/调用方直接传值时也走这条）。
    """
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return UI_FONT_SCALE_DEFAULT
    return max(UI_FONT_SCALE_MIN, min(UI_FONT_SCALE_MAX, v))


def font_scale() -> int:
    """当前全局字体百分比。"""
    return _FONT_SCALE


def set_font_scale(value) -> int:
    """设置全局字体百分比。

    只改状态、**不触发布局重算**（那一步很贵）：调用方随后应执行
    `apply_theme(current_name(), force=True)` 把新字号重放到所有界面。
    拆开是为了让「先设值、再统一重放」成为唯一路径，避免顺序写反导致漏刷。
    """
    global _FONT_SCALE
    _FONT_SCALE = normalize_font_scale(value)
    return _FONT_SCALE


def scaled_pt(base_pt: float) -> float:
    """按当前百分比缩放一个基准字号（供**不走 QSS**的 QFont 使用，如日志面板）。"""
    return base_pt * _FONT_SCALE / 100.0


def _fmt_font_size(value: float) -> str:
    """字号格式化：0.5pt 一档，整数不带小数点（QSS 文本干净、好读也好断言）。"""
    v = round(value * 2) / 2
    return f"{int(v)}" if float(v).is_integer() else f"{v:.1f}"


def scale_qss(text: str, scale: int | None = None) -> str:
    """把 QSS 文本里的 `font-size: Npt|Npx` 按全局百分比缩放。

    这是「一个百分比管全程序文字大小」的关键：所有内联样式（含主题引擎自己生成的
    基线/按钮/标签页样式）在写入前都过一遍这里，所以 140+ 处 UI 代码不用改一行。
    只认 `font-size`，**不碰 padding/margin/width 等其它数值**——间距由各处显式定的
    像素负责，跟着字号一起缩放反而会让布局跑偏（如 item 行高）。
    """
    pct = _FONT_SCALE if scale is None else normalize_font_scale(scale)
    if pct == 100 or not text:
        return text        # 100% 时原样返回：默认外观与加这个功能之前逐字相同

    def _sub(m: "re.Match") -> str:
        return m.group(1) + _fmt_font_size(float(m.group(2)) * pct / 100.0) + m.group(3)

    return _FONT_SIZE_RE.sub(_sub, text)


def render_style(text: str, theme_name: str | None = None) -> str:
    """内联样式原文 -> 落到控件上的最终文本（先按主题映射颜色，再按百分比缩放字号）。

    主题切换重放、hook 拦截写入、窗口基线拼接都走这一个入口，
    保证「颜色 + 字号」两条变换永远同进同出，不会出现某条路径只做了颜色映射。
    """
    return scale_qss(map_colors(text, theme_name))


def is_dark() -> bool:
    return bool(THEMES[_THEME].get("dark"))


# ---------------------------------------------------------------------------
# 颜色映射
# ---------------------------------------------------------------------------

def rebuild_reverse_map() -> None:
    """重建「颜色值 -> 令牌」反查表。

    优先级：历史字面量表（语义明确）> 各主题令牌值（跨主题兜底，
    用于把「已是某个主题色」的文本重新映射到目标主题——重放时必须有这层，
    因为 main_window 会 `styleSheet() + 新片段` 拼接，拼接结果里已是当前主题色）。
    同色多令牌时取先登记者，保证跨主题切换稳定。
    """
    rev: dict[str, str] = {}
    for hexv, tok in LEGACY_COLOR_TOKENS.items():
        rev.setdefault(hexv.lower(), tok)
    for theme in THEMES.values():
        for k, v in theme.items():
            if k == "label" or not isinstance(v, str) or not v.startswith("#"):
                continue
            rev.setdefault(v.lower(), k)
    _REVERSE.clear()
    _REVERSE.update(rev)


def map_colors(text: str, theme_name: str | None = None) -> str:
    """把文本里「能识别出令牌」的十六进制颜色换成目标主题对应色。

    识别不出的一律原样保留（用户自定义颜色、纯红警示色等不该被主题改掉）。
    `background: white` 这类**白色底色**额外处理成 card_bg，
    否则深色主题下会出现「白底 + 浅字」看不清的组合。
    """
    theme = THEMES[normalize_theme(theme_name or _THEME)]
    if not _REVERSE:
        rebuild_reverse_map()
    if not text:
        return text
    if "background" in text.lower():
        text = _WHITE_BG_RE.sub(
            lambda m: m.group(1) + theme.get("card_bg", m.group(2)) + m.group(3),
            text)
    if "#" not in text:
        return text

    def _sub(m: "re.Match") -> str:
        tok = _REVERSE.get("#" + m.group(1).lower())
        if tok is None:
            return m.group(0)
        return theme.get(tok, m.group(0))

    return _COLOR_RE.sub(_sub, text)


# ---------------------------------------------------------------------------
# 全局基线 QSS（通用控件外观）
# ---------------------------------------------------------------------------

def build_base_qss(theme_name: str | None = None) -> str:
    """通用控件基线样式（应用级）：输入控件、列表/表格、滚动条、菜单等。

    只用控件**类型**选择器——不用 objectName ID 选择器（防级联污染，见模块注释）。
    按钮与标签页另有窗口级规则（main_window），控件级内联样式优先级最高。
    """
    t = THEMES[normalize_theme(theme_name or _THEME)]
    g = t.get
    return f"""\
/* ===== 基础 =====
   性能约定：这里**不写 QWidget{{...}} 这种通配规则**——它会让每个控件构造时都
   参与一次样式匹配，实测把大界面的构造成本抬高数倍（整套测试从 50s 变 3 分钟）。
   文字色与字号改由 QPalette + QApplication.setFont 承担（见 build_palette / apply_theme）。*/
QDialog {{ background: {g('window_bg')}; }}

/* ===== 按钮（全局基线）=====
   对话框不再继承主窗口的按钮规则（各自窗口级 QSS 独立），按钮规则必须在
   基线里——否则深色主题下对话框里的按钮会回退成 Windows 原生浅灰按钮，
   浅底配浅字完全看不清（2026-09-27 实测踩坑）。 */
QPushButton {{
    background: {g('card_bg')}; color: {g('text')};
    border: 1px solid {g('input_border')}; border-radius: 5px;
    padding: 4px 12px; font-size: 10pt;
}}
QPushButton:hover {{
    border-color: {g('primary')}; color: {g('primary')};
    background: {g('primary_soft')};
}}
QPushButton:pressed {{ background: {g('pressed_bg')}; }}
QPushButton:disabled {{
    color: {g('text_disabled')}; background: {g('hover_bg')};
    border-color: {g('border_light')};
}}
QPushButton#btnPrimary {{
    background: {g('primary')}; color: white;
    border: 1px solid {g('primary_border')};
}}
QPushButton#btnPrimary:hover {{ background: {g('primary_hover')}; color: white; }}
QPushButton#btnPrimary:pressed {{ background: {g('primary_pressed')}; }}
QPushButton#btnSuccess {{
    background: {g('success')}; color: white;
    border: 1px solid {g('success_border')};
}}
QPushButton#btnSuccess:hover {{ background: {g('success_hover')}; color: white; }}
QPushButton#btnSuccess:pressed {{ background: {g('success_pressed')}; }}
QPushButton#btnDanger {{
    background: {g('danger')}; color: white;
    border: 1px solid {g('danger_border')};
}}
QPushButton#btnDanger:hover {{ background: {g('danger_hover')}; color: white; }}
QPushButton#btnDanger:pressed {{ background: {g('danger_pressed')}; }}
QPushButton#btnDanger:disabled, QPushButton#btnSuccess:disabled,
QPushButton#btnPrimary:disabled {{
    color: {g('disabled_fg')}; background: {g('disabled_bg')};
    border-color: {g('disabled_bg')};
}}

QLabel {{ color: {g('text')}; background: transparent; }}
QLabel:disabled {{ color: {g('text_disabled')}; }}

/* ===== 输入控件 ===== */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {g('input_bg')}; color: {g('text')};
    border: 1px solid {g('input_border')}; border-radius: 5px;
    padding: 3px 6px; selection-background-color: {g('primary')};
    selection-color: #ffffff;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {g('primary')};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    background: {g('hover_bg')}; color: {g('text_disabled')};
}}
/* 数值/日期时间输入：用基类选择器一条覆盖（QSpinBox/QDoubleSpinBox/QDateEdit…） */
QAbstractSpinBox {{
    background: {g('input_bg')}; color: {g('text')};
    border: 1px solid {g('input_border')}; border-radius: 5px;
    padding: 3px 6px; selection-background-color: {g('primary')};
    selection-color: #ffffff;
}}
QAbstractSpinBox:focus {{ border: 1px solid {g('primary')}; }}
QAbstractSpinBox:disabled {{
    background: {g('hover_bg')}; color: {g('text_disabled')};
}}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    background: {g('hover_bg')}; border: none; width: 16px;
}}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: {g('sel_bg')};
}}
QAbstractSpinBox::up-arrow {{
    image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-bottom: 5px solid {g('text_dim')};
    width: 0; height: 0;
}}
QAbstractSpinBox::down-arrow {{
    image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {g('text_dim')};
    width: 0; height: 0;
}}

/* ===== 下拉框 ===== */
QComboBox {{
    background: {g('input_bg')}; color: {g('text')};
    border: 1px solid {g('input_border')}; border-radius: 5px;
    padding: 3px 8px; min-height: 18px;
}}
QComboBox:hover {{ border-color: {g('primary')}; }}
QComboBox:focus {{ border: 1px solid {g('primary')}; }}
QComboBox:disabled {{ background: {g('hover_bg')}; color: {g('text_disabled')}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{
    image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {g('text_dim')};
    width: 0; height: 0; margin-right: 4px;
}}
QComboBox QAbstractItemView {{
    background: {g('menu_bg')}; color: {g('text')};
    border: 1px solid {g('border')}; border-radius: 5px;
    selection-background-color: {g('menu_sel')};
    selection-color: {g('primary')}; outline: none;
}}

/* ===== 复选 / 单选 ===== */
QCheckBox, QRadioButton {{ color: {g('text')}; spacing: 6px; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {g('text_disabled')}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QCheckBox::indicator {{
    border: 1px solid {g('input_border')}; border-radius: 4px;
    background: {g('input_bg')};
}}
QCheckBox::indicator:hover {{ border-color: {g('primary')}; }}
QCheckBox::indicator:checked {{
    background: {g('primary')}; border-color: {g('primary')};
}}
QRadioButton::indicator {{
    border: 1px solid {g('input_border')}; border-radius: 7px;
    background: {g('input_bg')};
}}
QRadioButton::indicator:hover {{ border-color: {g('primary')}; }}
QRadioButton::indicator:checked {{
    background: {g('primary')}; border: 4px solid {g('input_bg')};
}}

/* ===== 分组框 ===== */
QGroupBox {{
    border: 1px solid {g('border')}; border-radius: 8px;
    margin-top: 10px; padding-top: 6px;
    background: {g('card_bg')};
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 10px; padding: 0 4px; color: {g('text_dim')};
}}

/* ===== 列表 / 树 / 表格 ===== */
QListWidget, QListView, QTreeWidget, QTreeView, QTableWidget, QTableView {{
    background: {g('card_bg')}; color: {g('text')};
    border: 1px solid {g('border')}; border-radius: 6px;
    alternate-background-color: {g('panel_bg')};
    selection-background-color: {g('sel_bg')};
    selection-color: {g('primary')}; outline: none;
}}
QListWidget::item, QListView::item, QTreeWidget::item, QTreeView::item {{
    padding: 3px 4px;
}}
QListWidget::item:hover, QListView::item:hover,
QTreeWidget::item:hover, QTreeView::item:hover {{
    background: {g('hover_bg')};
}}
QListWidget::item:selected, QListView::item:selected,
QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {g('sel_bg')}; color: {g('primary')};
}}
QHeaderView::section {{
    background: {g('header_bg')}; color: {g('text_dim')};
    border: none; border-right: 1px solid {g('border')};
    border-bottom: 1px solid {g('border')}; padding: 4px 6px;
}}
QHeaderView::section:hover {{ background: {g('hover_bg')}; }}
QTableCornerButton::section {{
    background: {g('header_bg')}; border: none;
    border-right: 1px solid {g('border')};
    border-bottom: 1px solid {g('border')};
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {g('scrollbar')}; border-radius: 4px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {g('scrollbar_hover')}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {g('scrollbar')}; border-radius: 4px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {g('scrollbar_hover')}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ===== 菜单 ===== */
QMenu {{
    background: {g('menu_bg')}; color: {g('text')};
    border: 1px solid {g('border')}; border-radius: 6px; padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {g('menu_sel')}; color: {g('primary')}; }}
QMenu::item:disabled {{ color: {g('text_disabled')}; }}
QMenu::separator {{ height: 1px; background: {g('border')}; margin: 4px 8px; }}

/* ===== 提示气泡 ===== */
QToolTip {{
    background: {g('menu_bg')}; color: {g('text')};
    border: 1px solid {g('border')}; border-radius: 4px; padding: 4px 6px;
}}

/* ===== 进度条 ===== */
QProgressBar {{
    background: {g('hover_bg')}; color: {g('text')};
    border: 1px solid {g('border')}; border-radius: 6px;
    text-align: center; height: 14px;
}}
QProgressBar::chunk {{ background: {g('primary')}; border-radius: 5px; }}

/* ===== 状态栏 / 分割条 ===== */
QStatusBar {{ background: {g('panel_bg')}; color: {g('text_dim')}; }}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background: {g('border_light')}; }}
QSplitter::handle:hover {{ background: {g('primary')}; }}

/* ===== 标签页（控件级，供对话框内的 QTabWidget 使用）===== */
QTabWidget::pane {{
    border: 1px solid {g('border')}; border-radius: 6px;
    background: {g('card_bg')};
}}
QTabBar::tab {{
    background: transparent; color: {g('text_dim')};
    border: none; padding: 4px 14px; margin: 2px;
    border-radius: 5px;
}}
QTabBar::tab:hover {{ color: {g('primary')}; background: {g('primary_soft')}; }}
QTabBar::tab:selected {{ color: #ffffff; background: {g('primary')}; }}
"""


def build_variant_qss(variant: str, theme_name: str | None = None,
                      font_pt: float | None = None) -> str:
    """按钮变体的**控件级**内联样式（含 hover / pressed / disabled 全状态）。

    供 widgets.set_variant 调用：内联样式优先级最高，外部 QSS 怎么级联都覆盖不到，
    也避免 objectName ID 选择器跨 widget 树污染（历史事故，见 widgets.py 注释）。
    颜色全部取自主题令牌，所以按钮会跟随主题切换。

    `font_pt`：基准字号（默认 DEFAULT_FONT_PT）。控件自带样式表**拿不到页面级
    QSS 的字号规则**，所以密集页面（设置页）想整页小一号，必须在这里显式传。
    """
    t = THEMES[normalize_theme(theme_name or _THEME)]
    g = t.get
    pt = DEFAULT_FONT_PT if font_pt is None else font_pt
    base = f"border-radius:5px;padding:4px 12px;font-size:{_fmt_font_size(pt)}pt;"
    if variant == "primary":
        c, h, p = g("primary"), g("primary_hover"), g("primary_pressed")
        b = g("primary_border")
    elif variant == "success":
        c, h, p = g("success"), g("success_hover"), g("success_pressed")
        b = g("success_border")
    elif variant == "danger":
        c, h, p = g("danger"), g("danger_hover"), g("danger_pressed")
        b = g("danger_border")
    else:
        return ""      # neutral 等：交给窗口级/全局 QSS
    return (
        f"QPushButton{{background:{c};color:white;border:1px solid {b};{base}}}"
        f"QPushButton:hover{{background:{h};color:white;border-color:{h};}}"
        f"QPushButton:pressed{{background:{p};color:white;border-color:{p};}}"
        f"QPushButton:disabled{{background:{g('disabled_bg')};"
        f"color:{g('disabled_fg')};border-color:{g('disabled_bg')};}}")


def build_palette(theme_name: str | None = None):
    """按主题构造 QPalette。

    为什么不能只靠 QSS：有些控件/代码直接读 `palette()` 取色（例如流程列表用
    `palette().color(foregroundRole())` 恢复默认字色），系统 palette 是浅色的，
    深色主题下就会「黑字压深底」。设置应用级 palette 一并解决，
    顺带让系统对话框、菜单、提示气泡等原生绘制的部分也跟着换色。
    """
    from PySide6.QtGui import QColor, QPalette
    t = THEMES[normalize_theme(theme_name or _THEME)]
    g = t.get
    p = QPalette()
    p.setColor(QPalette.Window, QColor(g("window_bg")))
    p.setColor(QPalette.WindowText, QColor(g("text")))
    p.setColor(QPalette.Base, QColor(g("input_bg")))
    p.setColor(QPalette.AlternateBase, QColor(g("panel_bg")))
    p.setColor(QPalette.Text, QColor(g("text")))
    p.setColor(QPalette.Button, QColor(g("card_bg")))
    p.setColor(QPalette.ButtonText, QColor(g("text")))
    p.setColor(QPalette.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.Highlight, QColor(g("primary")))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, QColor(g("menu_bg")))
    p.setColor(QPalette.ToolTipText, QColor(g("text")))
    p.setColor(QPalette.PlaceholderText, QColor(g("text_muted")))
    p.setColor(QPalette.Link, QColor(g("primary")))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(g("text_disabled")))
    return p


def build_button_qss(theme_name: str | None = None) -> str:
    """全局按钮配色（窗口级）：默认灰白 + 蓝/绿/红三个变体。

    变体走 objectName 是**窗口级**作用域（历史上一直在用、且只在主窗口内部），
    不放到应用级基线里，避免跨窗口级联。
    """
    t = THEMES[normalize_theme(theme_name or _THEME)]
    g = t.get
    return f"""\
QPushButton {{
    background: {g('card_bg')}; color: {g('text')};
    border: 1px solid {g('input_border')}; border-radius: 5px;
    padding: 4px 12px; font-size: 10pt;
}}
QPushButton:hover {{
    border-color: {g('primary')}; color: {g('primary')};
    background: {g('primary_soft')};
}}
QPushButton:pressed {{ background: {g('pressed_bg')}; }}
QPushButton:disabled {{
    color: {g('text_disabled')}; background: {g('hover_bg')};
    border-color: {g('border_light')};
}}
QPushButton#btnPrimary {{
    background: {g('primary')}; color: white;
    border: 1px solid {g('primary_border')};
}}
QPushButton#btnPrimary:hover {{ background: {g('primary_hover')}; color: white; }}
QPushButton#btnPrimary:pressed {{ background: {g('primary_pressed')}; }}
QPushButton#btnSuccess {{
    background: {g('success')}; color: white;
    border: 1px solid {g('success_border')};
}}
QPushButton#btnSuccess:hover {{ background: {g('success_hover')}; color: white; }}
QPushButton#btnSuccess:pressed {{ background: {g('success_pressed')}; }}
QPushButton#btnDanger {{
    background: {g('danger')}; color: white;
    border: 1px solid {g('danger_border')};
}}
QPushButton#btnDanger:hover {{ background: {g('danger_hover')}; color: white; }}
QPushButton#btnDanger:pressed {{ background: {g('danger_pressed')}; }}
QPushButton#btnDanger:disabled, QPushButton#btnSuccess:disabled,
QPushButton#btnPrimary:disabled {{
    color: {g('disabled_fg')}; background: {g('disabled_bg')};
    border-color: {g('disabled_bg')};
}}
"""


def build_tab_qss(theme_name: str | None = None) -> str:
    """顶部导航栏配色（窗口级）：选中项主色底 + 白字，hover 浅色反馈。"""
    t = THEMES[normalize_theme(theme_name or _THEME)]
    g = t.get
    return f"""\
QTabWidget::pane {{
    border: none; border-top: 1px solid {g('border_light')};
    background: {g('panel_bg')}; top: -1px;
}}
QTabBar {{ background: {g('window_bg')}; border: none; }}
QTabBar::tab {{
    background: transparent; color: {g('text_dim')};
    border: none; border-radius: 6px;
    padding: 4px 16px; margin: 5px 2px;
    font-size: 10pt; font-weight: 500;
}}
QTabBar::tab:hover {{ color: {g('primary')}; background: {g('primary_soft')}; }}
QTabBar::tab:selected {{
    color: #ffffff; background: {g('primary')}; font-weight: 600;
}}
"""


# ---------------------------------------------------------------------------
# setStyleSheet 拦截：让历史内联样式自动跟随主题
# ---------------------------------------------------------------------------

def install_hook() -> None:
    """拦截 QWidget.setStyleSheet（幂等）：登记原文 + 按当前主题映射颜色。

    这是「不改 140+ 处 UI 代码就实现整体主题化」的关键：
    - 每个控件最后一次设置的原始样式被记住（弱引用，不阻止对象回收）；
    - 写入前按当前主题做颜色映射（默认主题下是恒等映射，行为不变）；
    - 切换主题时 `apply_theme` 用**原文本**重新映射并重放。
    另外拦截 `show()`：顶层窗口显示时把它的子树里「待重放」的样式补上
    （切换主题时隐藏的控件不会当场重放，避免无谓开销）。
    """
    global _HOOKED, _ORIG_SET_STYLE
    if _HOOKED:
        return
    from PySide6.QtWidgets import QWidget
    _ORIG_SET_STYLE = QWidget.setStyleSheet

    def _patched(self, text):
        if isinstance(text, str) and text:
            try:
                _REGISTRY[self] = text      # 记住原文，切主题时重放
            except TypeError:               # 极端情况下对象不可弱引用
                pass
            _PENDING.discard(self)          # 新样式就是最新的，无需再补
            out = render_style(text)        # 颜色映射 + 字号缩放（两条变换同一入口）
            if _is_toplevel(self):
                # 顶层窗口：把自己的样式接在全局基线之后（窗口级 QSS 只影响本窗口树，
                # 不在应用级设置基线，见 apply_theme 里的性能说明）
                out = scale_qss(build_base_qss(_THEME)) + out
                try:
                    _BASELINE[self] = _THEME
                except TypeError:
                    pass
            text = out
        return _ORIG_SET_STYLE(self, text)

    QWidget.setStyleSheet = _patched
    _HOOKED = True


def _is_toplevel(widget) -> bool:
    try:
        return bool(widget.isWindow())
    except (RuntimeError, AttributeError):
        return False


def _apply_window_baseline(win, theme_name: str | None = None, force: bool = False) -> None:
    """给顶层窗口套上全局基线样式（幂等：同一主题下不重复设置）。

    基线放在**窗口级**而不是应用级，是为了性能：应用级 QSS 会让每个新建控件
    都参与一次样式匹配（实测 500 个控件的构造成本从 ~80ms 涨到 ~600ms，
    打开流程编辑对话框能多等 1~2 秒）。窗口级只在窗口显示时解析一次。
    """
    tn = normalize_theme(theme_name or _THEME)
    try:
        if not force and _BASELINE.get(win) == tn:
            return
    except TypeError:
        return
    own = ""
    try:
        own = _REGISTRY.get(win, "")
        set_style_raw(win, scale_qss(build_base_qss(tn)) + render_style(own, tn))
        _BASELINE[win] = tn
    except RuntimeError:
        return


class _ShowReplayFilter(QObject):
    """全局事件过滤器：控件被显示时补齐「待重放」的样式 / 给窗口套上基线。

    为什么不用 QWidget.show()：切标签页、父窗口显示、`setVisible(True)` 都
    不会调用子控件的 show()，而 Qt 会给每个变为可见的控件发 Show 事件——
    挂事件过滤器才覆盖得全（否则切主题后隐藏页面会一直停在旧配色）。
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show:
            try:
                if _is_toplevel(obj):
                    _apply_window_baseline(obj)
            except RuntimeError:
                pass
            if _PENDING:
                try:
                    if obj in _PENDING:
                        _PENDING.discard(obj)
                        text = _REGISTRY.get(obj)
                        if text:
                            set_style_raw(obj, render_style(text, _THEME))
                except (RuntimeError, TypeError):
                    pass
        return False        # 不拦截事件，交给控件自己处理


def _ensure_show_filter(app) -> None:
    """把事件过滤器挂到 QApplication（import 时还没有 app，故延迟到这里）。"""
    global _FILTER
    if app is None:
        return
    try:
        if _FILTER is not None and _FILTER.parent() is app:
            return
        _FILTER = _ShowReplayFilter(app)
        app.installEventFilter(_FILTER)
    except RuntimeError:
        pass


def clear_registry() -> None:
    """清空样式登记表与待重放集合。

    仅供测试/诊断：登记的语义是「当前活着的 UI 的样式」，
    单测之间没有界面延续关系，清掉可避免上一批用例的控件拖慢主题切换。
    """
    _REGISTRY.clear()
    _PENDING.clear()


def set_style_raw(widget, text: str) -> None:
    """绕过登记直接写样式（主题重放内部用）。"""
    (_ORIG_SET_STYLE or type(widget).setStyleSheet)(widget, text)


def registered_styles() -> list[tuple[object, str]]:
    """当前登记在册的 (控件, 原始样式) 列表（测试/调试用）。"""
    out = []
    for w, text in list(_REGISTRY.items()):
        out.append((w, text))
    return out


# ---------------------------------------------------------------------------
# 应用与切换
# ---------------------------------------------------------------------------

def register_listener(cb) -> None:
    """主题变化回调：需要按新主题重刷**非 QSS 样式**或窗口级样式的地方注册进来。

    绑定方法用弱引用（对象销毁后自动失效）：主窗口、日志面板等都是窗口级对象，
    被全局列表强引用就永远回收不掉。
    """
    try:
        ref = weakref.WeakMethod(cb)
    except TypeError:                 # 普通函数/可调用对象：没有绑定语义
        if cb not in _LISTENER_FUNCS:
            _LISTENER_FUNCS.append(cb)
        return
    if any(r() == cb for r in _LISTENER_METHODS):
        return                        # 已注册过
    _LISTENER_METHODS.append(ref)


def _fire_listeners() -> None:
    for ref in list(_LISTENER_METHODS):
        cb = ref()
        if cb is None:                # 对象已销毁：顺手清理
            try:
                _LISTENER_METHODS.remove(ref)
            except ValueError:
                pass
            continue
        try:
            cb()
        except Exception:             # 某个回调出错不该让整体切换失败
            log.exception("主题变化回调失败")
    for fn in list(_LISTENER_FUNCS):
        try:
            fn()
        except Exception:
            log.exception("主题变化回调失败")


def clear_listeners() -> None:
    """清空所有主题监听（测试用：避免用例之间互相污染）。"""
    _LISTENER_METHODS.clear()
    _LISTENER_FUNCS.clear()


def apply_theme(name: str, app=None, force: bool = False) -> None:
    """切换到指定主题并**立即**应用：全局基线 + 重放所有已登记的内联样式。

    性能约定（重要）：主题名没变且已经应用过一次时**直接返回**——
    重放会对每个已登记控件重新 setStyleSheet，而 setStyleSheet/setPalette
    会让 Qt 对整个应用重新 polish（几千个控件就是可感知的卡顿）。
    主窗口每次构造都会调用本函数，同名时跳过才不会累积成 O(N²)。
    需要强制重放（例如手工改了令牌）时传 force=True。
    """
    global _THEME, _APPLIED
    target = normalize_theme(name)
    from PySide6.QtWidgets import QApplication
    app = app or QApplication.instance()
    same = (target == _THEME) and _APPLIED
    _THEME = target
    rebuild_reverse_map()

    if same and not force:
        # 同一个主题重复应用：基线、调色板、内联样式都已就位。
        # 这里连 palette/字体都不必再设——它们同样会触发全量重算。
        return

    if app is not None:
        # 调色板 + 字体（承担文字色/选中色/禁用色与统一字号）。
        # 注意：**不调用 app.setStyleSheet**——应用级 QSS 会让每个控件构造时
        # 都参与样式匹配，成本实测翻数倍；基线改由顶层窗口在显示时套上
        # （见 _apply_window_baseline）。
        app.setPalette(build_palette(_THEME))
        # 统一字号走应用字体（比在 QSS 里写 QWidget{font-size} 便宜得多）；
        # 字号只在**基准 × 百分比**变了之后才设——setFont 会让所有控件重算尺寸，
        # 反复调同一档字号纯属浪费（主窗口每次构造都会走到这里）。
        want_pt = scaled_pt(DEFAULT_FONT_PT)
        if getattr(app, "_qf_font_pt", None) != want_pt:
            from PySide6.QtGui import QFont
            font = QFont(app.font())
            font.setPointSizeF(want_pt)
            app.setFont(font)
            app._qf_font_pt = want_pt
        _ensure_show_filter(app)

    _APPLIED = True
    _BASELINE.clear()               # 所有窗口的基线都要按新主题重套
    for win in app.topLevelWidgets() if app is not None else []:
        try:
            if win.isVisible():
                _apply_window_baseline(win, _THEME, force=True)
            else:
                _PENDING.add(win)   # 隐藏窗口等显示时再套
        except RuntimeError:
            continue

    for widget, text in list(_REGISTRY.items()):
        if _is_toplevel(widget):
            continue                # 顶层窗口的样式（含基线）上面已处理
        try:
            if force or widget.isVisible():
                set_style_raw(widget, render_style(text, _THEME))
            else:
                _PENDING.add(widget)    # 隐藏控件等显示时再补（省下大量无谓重绘）
        except RuntimeError:
            continue        # 控件已被销毁

    _fire_listeners()


# 模块导入即安装 hook（幂等）：任何 UI 代码只要走到 setStyleSheet 就被登记，
# 不需要各文件显式 import 本模块。
install_hook()
rebuild_reverse_map()
