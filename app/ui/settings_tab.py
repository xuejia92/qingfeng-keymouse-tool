"""设置页：全局热键 + 界面外观（主题色卡）+ 运行状态浮层外观 + 文件位置。

整页包在 `QScrollArea` 里（2026-10-01）：设置项多、分组框自然高度合计接近 800px，
不滚动的话窗口一矮 Qt 就把分组框压扁，行距/内边距全被吃掉（用户反馈「太紧凑」）。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, QUrl, Qt, Signal
from PySide6.QtGui import (QDesktopServices, QColor, QFontDatabase, QIcon,
                           QPainter, QPainterPath, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import capture_report
from ..config import (APP_NAME, BASE_DIR, CONFIG_PATH, LOG_PATH, TEMPLATE_DIR,
                      RUN_OVERLAY_POSITIONS, RUN_OVERLAY_POS_LABELS,
                      RUN_OVERLAY_DEFAULT_TEXT_COLOR,
                      RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX,
                      RUN_OVERLAY_LOG_MAX_LINES_MIN, RUN_OVERLAY_LOG_MAX_LINES_MAX,
                      RUN_OVERLAY_LOG_WIDTH_MIN, RUN_OVERLAY_LOG_WIDTH_MAX,
                      RUN_OVERLAY_LOG_HEIGHT_MIN, RUN_OVERLAY_LOG_HEIGHT_MAX,
                      RUN_OVERLAY_LOG_AUTO_HIDE_MIN, RUN_OVERLAY_LOG_AUTO_HIDE_MAX,
                      UI_FONT_SCALE_DEFAULT, UI_FONT_SCALE_MIN,
                      UI_FONT_SCALE_MAX, UI_FONT_SCALE_STEP,
                      normalize_hex_color)
# 状态日志浮层的默认色/默认位置定义在浮层模块里（避开主题令牌色值，见 running_overlay 注释）
from ..running_overlay import (LOG_DEFAULT_BG, LOG_DEFAULT_COLOR,
                               LOG_DEFAULT_POS, LOG_ERROR_COLOR,
                               LOG_WARN_COLOR)
from .. import hotkey_policy
from . import theme
from .hotkey_edit import HotkeyEdit
from .widgets import polish_form, set_variant

# 设置页是否显示「截屏上报」区块（用户要求隐藏，2026-09-24）。
# 只影响**界面**：capture_report 仍照常按 config.json 运行，排除名单也照旧生效；
# 改成 True 即可把这一块显示回来（控件构建与相关方法都保留着）。
SHOW_CAPTURE_SECTION = False

# 运行状态浮层那几行的控件宽度（2026-10-01）：定宽才能让「字号/字体/颜色」两行**列对齐**，
# 也让「显示位置」不再被 QFormLayout 拉成整行宽（原来宽到 1200+px，与上面的窄控件完全不成比例）。
_OVERLAY_SIZE_W = 104                     # 字号数字框："16 px" 两个字宽也放得下
_OVERLAY_FIELD_W = (200, 240)             # 字体 / 显示位置下拉：给个字宽区间，不拉满整行
_OVERLAY_COLOR_BTN_W = 112                # 色块按钮（显示 hex，宽度要能放下 #aarrggbb）

# 设置页整页字号（2026-10-01 用户要求「字体小一点、紧凑一点、节约空间」）。
# 走页面级 QSS 统一压到 9pt（与左栏流程树 / 右侧模块面板同一档），再按全局字体百分比缩放；
# 页面里那几个自带内联样式的按钮（set_variant / 色块按钮）拿不到页面规则，
# 必须显式传同一档字号，否则整页就它们大一号。
_SETTINGS_FONT_PT = 9


def _theme_swatch(key: str) -> QIcon:
    """下拉项左侧的主题色卡：主色 / 面板底 / 正文色 三段色带。

    刻意**用 QPainter 直接画**而不是给色卡控件写 QSS：任何 setStyleSheet 都会被
    主题引擎登记、并按**当前**主题做颜色映射，色卡就会显示成别的颜色——
    这里要展示的恰恰是「所选主题自己的配色」。
    """
    panel, primary, text = theme.preview_colors(key)
    w, h = 34.0, 16.0
    pm = QPixmap(int(w), int(h))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3.5, 3.5)
        p.setClipPath(clip)                       # 圆角内三段色，边缘不外溢
        p.fillRect(QRectF(0, 0, w * 0.46, h), QColor(primary))
        p.fillRect(QRectF(w * 0.46, 0, w * 0.33, h), QColor(panel))
        p.fillRect(QRectF(w * 0.79, 0, w * 0.21, h), QColor(text))
        p.setClipping(False)
        edge = QColor(text)                       # 正文色调淡当描边，深浅主题都协调
        edge.setAlpha(60)
        p.setBrush(Qt.NoBrush)
        p.setPen(edge)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3.5, 3.5)
    finally:
        p.end()
    return QIcon(pm)


def _polish_form(form: QFormLayout) -> QFormLayout:
    """统一设置页表单的留白：标签右对齐 + 明确的列间距/行距/内边距。

    间距数值现由 `widgets.polish_form` 统一提供（设置页与流程步骤编辑弹窗共用同一组，
    两个地方的疏密才一致）。本函数保留成薄封装，是因为页面里 5 处调用点都写的是它。
    """
    return polish_form(form)


class SettingsTab(QWidget):
    changed = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._loading = True
        self._build_ui()
        self.hotkey_edit.set_hotkey(cfg.show_hide_hotkey)
        self.stop_edit.set_hotkey(cfg.stop_all_hotkey)
        self._loading = False
        self.hotkey_edit.hotkeyChanged.connect(self._ui_changed)
        self.stop_edit.hotkeyChanged.connect(self._ui_changed)

    def _build_ui(self) -> None:
        # 设置项较多（热键 / 主题 / 运行浮层 / 文件位置 / 关于），本来是把它们直接塞进
        # 本页布局。但本页**没有滚动区**，窗口不高时 Qt 只能把各分组框**压扁**
        # （实测：内容自然高度需要 767px，在 660px 下「运行状态浮层」被压到 177px，
        # 行距和内边距全被吃掉 —— 这正是用户反馈「文字和表单框太紧凑」的另一半原因）。
        # 改为「内容保自然高度 + 放不下就出滚动条」，小窗口下也不会再挤压排版。
        #
        # 整页字号统一压到 _SETTINGS_FONT_PT（2026-10-01 用户要求「字体小一点、紧凑」）：
        # 只列本页真正用到的控件类型，不写 QWidget 通配规则（那会让每个控件都参与
        # 样式匹配，见 theme.py 的性能约定）。font-size 会随后被全局字体百分比缩放。
        self.setObjectName("settingsTab")
        self.setStyleSheet(
            "QWidget#settingsTab QLabel, QWidget#settingsTab QCheckBox,"
            "QWidget#settingsTab QGroupBox, QWidget#settingsTab QPushButton,"
            "QWidget#settingsTab QComboBox, QWidget#settingsTab QSpinBox,"
            "QWidget#settingsTab QLineEdit"
            f" {{ font-size: {_SETTINGS_FONT_PT}pt; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)          # 不要系统凹边框，与其它页观感一致
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 视口必须**显式**透明：QAbstractScrollArea 的视口即使
        # autoFillBackground=False 也会自己刷一层（实测刷的是 palette 的 Window 色），
        # 于是设置页底色变成 window_bg，和其它标签页的 panel_bg 对不上。
        # 只对这种视口套一条最窄的规则（不写后代选择器，避免跨控件树级联）。
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(scroll)

        content = QWidget()
        content.setAutoFillBackground(False)
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)                           # 分组框之间留出呼吸感（整页紧凑，2026-10-01）

        hotkey_box = QGroupBox("全局热键")
        form = _polish_form(QFormLayout(hotkey_box))
        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setMaximumWidth(220)
        self.hotkey_edit.set_conflict_checker(lambda hk: hotkey_policy.check(hk, "show_hide"))
        form.addRow("显示 / 隐藏主窗口（切换）", self.hotkey_edit)
        self.stop_edit = HotkeyEdit()
        self.stop_edit.setMaximumWidth(220)
        self.stop_edit.set_conflict_checker(lambda hk: hotkey_policy.check(hk, "stop_all"))
        form.addRow("紧急停止全部任务", self.stop_edit)
        warn = QLabel("⚠ 任务可能在后台持续点击/按键，失控时请立刻按紧急停止热键，"
                      "或用鼠标右键托盘图标选择「全部停止」。")
        warn.setStyleSheet("color: #c0392b;")
        warn.setWordWrap(True)
        form.addRow("", warn)
        root.addWidget(hotkey_box)

        # 界面主题：统一样式库（令牌化配色），切换后立即生效并自动记住
        look_box = QGroupBox("界面外观")
        lform = _polish_form(QFormLayout(look_box))
        self.theme_combo = QComboBox()
        self.theme_combo.setMaximumWidth(220)
        self.theme_combo.setIconSize(QSize(34, 16))
        for key, label in theme.theme_names():
            self.theme_combo.addItem(_theme_swatch(key), label, key)
        idx = self.theme_combo.findData(theme.normalize_theme(
            getattr(self.cfg, "ui_theme", "light")))
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_picked)
        lform.addRow("配色主题", self.theme_combo)

        # 全局字体百分比（2026-10-01）：一个值管全程序所有页面的文字大小。
        # 实现是「应用字体 × 百分比 + 所有 QSS 的 font-size 同比例缩放」，
        # 所以各页面之间的大小比例保持不变，页面间距节奏也不会被拉散。
        self.font_scale_spin = QSpinBox()
        self.font_scale_spin.setRange(UI_FONT_SCALE_MIN, UI_FONT_SCALE_MAX)
        self.font_scale_spin.setSingleStep(UI_FONT_SCALE_STEP)
        self.font_scale_spin.setSuffix(" %")
        self.font_scale_spin.setFixedWidth(_OVERLAY_SIZE_W)
        self.font_scale_spin.setValue(theme.normalize_font_scale(
            getattr(self.cfg, "ui_font_scale", UI_FONT_SCALE_DEFAULT)))
        self.font_scale_spin.setToolTip(
            "全局文字大小：所有页面的文字按这个百分比缩放（100% 为默认）。\n"
            "只改字号，不改页面留白，各页面之间的大小比例保持一致。")
        self.font_scale_spin.valueChanged.connect(self._on_font_scale_changed)
        lform.addRow("界面文字大小", self.font_scale_spin)

        light_n = sum(1 for t in theme.THEMES.values() if not t.get("dark"))
        look_hint = QLabel(f"共 {len(theme.THEMES)} 套主题（{light_n} 套浅色 / "
                           f"{len(theme.THEMES) - light_n} 套深色），左侧色卡预览整套配色；"
                           "切换后界面立即换色（按钮、输入框、列表、表格、菜单等整体统一），"
                           "「界面文字大小」按百分比整体缩放所有页面的文字；"
                           "两项都立即生效并自动保存到 config.json，下次启动沿用。")
        look_hint.setStyleSheet("color: #888;")
        look_hint.setWordWrap(True)
        lform.addRow("", look_hint)
        root.addWidget(look_box)

        # 运行状态浮层：左上角（位置可调）显示正在运行的分组和流程
        ro_box = QGroupBox("运行状态浮层（有流程运行时显示在屏幕上）")
        self.run_overlay_box = ro_box
        roform = _polish_form(QFormLayout(ro_box))
        self.run_overlay_check = QCheckBox("显示正在运行的分组和流程")
        self.run_overlay_check.setChecked(bool(getattr(self.cfg, "run_overlay_enabled", True)))
        self.run_overlay_check.setToolTip("关掉后屏幕上不再显示这个浮层")
        self.run_overlay_check.toggled.connect(self._sync_overlay_enabled)
        self.run_overlay_check.toggled.connect(self._ui_changed)
        roform.addRow(self.run_overlay_check)

        # 浮层文字：分组名与流程名显示在同一行（「分组 - 流程 [热键]」），
        # 所以「分组标题文字」「流程名称文字」合并为一组设置（2026-10-01 用户要求）。
        self.run_overlay_flow_size, self.run_overlay_flow_font, \
            self.run_overlay_flow_btn = self._build_overlay_text_row("flow")
        roform.addRow("浮层文字",
                      self._overlay_text_row_layout("run_overlay_flow_size",
                                                    "run_overlay_flow_font",
                                                    "run_overlay_flow_btn"))

        self.run_overlay_bg_transparent = QCheckBox("透明背景")
        self.run_overlay_bg_transparent.setChecked(
            not str(getattr(self.cfg, "run_overlay_bg_color", "") or ""))
        self.run_overlay_bg_transparent.setToolTip("勾选后浮层无底色（只有文字）")
        self.run_overlay_bg_transparent.toggled.connect(self._sync_overlay_bg)
        self.run_overlay_bg_transparent.toggled.connect(self._ui_changed)
        self.run_overlay_bg_btn = QPushButton()
        self.run_overlay_bg_btn.setFixedWidth(_OVERLAY_COLOR_BTN_W)
        self.run_overlay_bg_btn.setToolTip("浮层背景色；在拾色器里还能调不透明度（半透明）")
        self.run_overlay_bg_btn.clicked.connect(
            lambda: self._pick_overlay_color("bg"))
        bg_row = QHBoxLayout()
        bg_row.setContentsMargins(0, 0, 0, 0)
        bg_row.setSpacing(10)
        bg_row.addWidget(self.run_overlay_bg_btn)
        bg_row.addWidget(self.run_overlay_bg_transparent, 1)
        roform.addRow("背景颜色", bg_row)

        self.run_overlay_pos = QComboBox()
        self.run_overlay_pos.setMinimumWidth(_OVERLAY_FIELD_W[0])
        self.run_overlay_pos.setMaximumWidth(_OVERLAY_FIELD_W[1])
        for key in RUN_OVERLAY_POSITIONS:
            self.run_overlay_pos.addItem(RUN_OVERLAY_POS_LABELS[key], key)
        self.run_overlay_pos.setCurrentIndex(max(
            0, RUN_OVERLAY_POSITIONS.index(
                getattr(self.cfg, "run_overlay_pos", "top_left"))))
        self.run_overlay_pos.setToolTip("浮层贴在屏幕的哪个角")
        self.run_overlay_pos.currentIndexChanged.connect(self._ui_changed)
        roform.addRow("显示位置", self.run_overlay_pos)

        ro_hint = QLabel("改动立即生效：正在运行时浮层马上换新样式；"
                         "背景色支持半透明（拾色器里调不透明度）。")
        ro_hint.setStyleSheet("color: #888;")
        ro_hint.setWordWrap(True)
        roform.addRow("", ro_hint)

        # ---- 状态日志（**独立浮层**：透明控制台，2026-10-01）----
        # 用户要求：与「分组/流程」概览分开显示、可单独设位置、默认黑色半透明背景、默认左下角。
        self.run_overlay_log_check = QCheckBox("展示状态日志（独立浮层，不与流程概览放一起）")
        self.run_overlay_log_check.setChecked(
            bool(getattr(self.cfg, "run_overlay_log_enabled", True)))
        self.run_overlay_log_check.setToolTip(
            "勾选后「状态日志」步骤的输出显示在屏幕上的透明控制台；取消则不显示")
        self.run_overlay_log_check.toggled.connect(self._ui_changed)
        self.run_overlay_log_check.toggled.connect(self._sync_overlay_log_enabled)
        roform.addRow(self.run_overlay_log_check)

        log_size = QSpinBox()
        log_size.setRange(RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX)
        log_size.setSuffix(" px")
        log_size.setValue(int(getattr(self.cfg, "run_overlay_log_font_size", 12)))
        log_size.setFixedWidth(_OVERLAY_SIZE_W)
        log_size.setToolTip("状态日志消息的字号（像素）")
        log_size.valueChanged.connect(self._ui_changed)
        self.run_overlay_log_size = log_size

        log_font = QComboBox()
        log_font.setMinimumWidth(_OVERLAY_FIELD_W[0])
        log_font.setMaximumWidth(_OVERLAY_FIELD_W[1])
        # 默认等宽字体：控制台风格的观感（Consolas 在 Windows 上一定存在）
        self._fill_overlay_fonts(log_font, "run_overlay_log_font_family")
        log_font.setToolTip("状态日志消息的字体（默认 Consolas，等宽更像控制台）")
        log_font.currentIndexChanged.connect(self._ui_changed)
        self.run_overlay_log_font = log_font

        self.run_overlay_log_line = QSpinBox()
        self.run_overlay_log_line.setRange(RUN_OVERLAY_LOG_MAX_LINES_MIN,
                                           RUN_OVERLAY_LOG_MAX_LINES_MAX)
        self.run_overlay_log_line.setSuffix(" 行")
        self.run_overlay_log_line.setFixedWidth(_OVERLAY_SIZE_W)
        self.run_overlay_log_line.setValue(
            int(getattr(self.cfg, "run_overlay_log_max_lines", 8)))
        self.run_overlay_log_line.setToolTip("浮层最多保留多少条状态日志（超出丢弃最旧的）")
        self.run_overlay_log_line.valueChanged.connect(self._ui_changed)

        roform.addRow("状态日志",
                      self._overlay_text_row_layout("run_overlay_log_size",
                                                    "run_overlay_log_font",
                                                    "run_overlay_log_line"))

        # 最大宽/高：宽度决定多长换行，高度是窗口上限（都有默认值）
        self.run_overlay_log_max_w = QSpinBox()
        self.run_overlay_log_max_w.setRange(RUN_OVERLAY_LOG_WIDTH_MIN,
                                            RUN_OVERLAY_LOG_WIDTH_MAX)
        self.run_overlay_log_max_w.setSuffix(" px")
        self.run_overlay_log_max_w.setFixedWidth(_OVERLAY_SIZE_W)
        self.run_overlay_log_max_w.setValue(
            int(getattr(self.cfg, "run_overlay_log_max_width", 320)))
        self.run_overlay_log_max_w.setToolTip(
            "浮层的固定宽度：文字超过它自动换行（窗口不随内容变化）")
        self.run_overlay_log_max_w.valueChanged.connect(self._ui_changed)

        self.run_overlay_log_max_h = QSpinBox()
        self.run_overlay_log_max_h.setRange(RUN_OVERLAY_LOG_HEIGHT_MIN,
                                            RUN_OVERLAY_LOG_HEIGHT_MAX)
        self.run_overlay_log_max_h.setSuffix(" px")
        self.run_overlay_log_max_h.setFixedWidth(_OVERLAY_SIZE_W)
        self.run_overlay_log_max_h.setValue(
            int(getattr(self.cfg, "run_overlay_log_max_height", 180)))
        self.run_overlay_log_max_h.setToolTip(
            "浮层的固定高度：内容超出时裁掉上面的旧行，最新消息始终可见")
        self.run_overlay_log_max_h.valueChanged.connect(self._ui_changed)

        size_row = QHBoxLayout()
        size_row.setContentsMargins(0, 0, 0, 0)
        size_row.setSpacing(10)
        size_row.addWidget(QLabel("宽"))
        size_row.addWidget(self.run_overlay_log_max_w)
        size_row.addWidget(QLabel("高"))
        size_row.addWidget(self.run_overlay_log_max_h)
        size_row.addStretch(1)
        roform.addRow("固定尺寸", size_row)

        # 自动隐藏：多久没有新消息就自动收起（默认 60 秒；最后 10 秒才显示倒计时）
        self.run_overlay_log_auto_hide = QSpinBox()
        self.run_overlay_log_auto_hide.setRange(RUN_OVERLAY_LOG_AUTO_HIDE_MIN,
                                                RUN_OVERLAY_LOG_AUTO_HIDE_MAX)
        self.run_overlay_log_auto_hide.setSuffix(" 秒")
        self.run_overlay_log_auto_hide.setFixedWidth(_OVERLAY_SIZE_W)
        self.run_overlay_log_auto_hide.setValue(
            int(getattr(self.cfg, "run_overlay_log_auto_hide_sec", 60)))
        self.run_overlay_log_auto_hide.setToolTip(
            "多久没有新消息就自动收起浮层（默认 60 秒）；\n"
            "剩余最后 10 秒时，关闭按钮左边才会显示倒计时数字")
        self.run_overlay_log_auto_hide.valueChanged.connect(self._ui_changed)
        roform.addRow("自动隐藏", self.run_overlay_log_auto_hide)

        self.run_overlay_log_btn = QPushButton()
        self.run_overlay_log_btn.setFixedWidth(_OVERLAY_COLOR_BTN_W)
        self.run_overlay_log_btn.setToolTip("普通消息的颜色")
        self.run_overlay_log_btn.clicked.connect(
            lambda: self._pick_overlay_color("log"))
        self.run_overlay_log_warn_btn = QPushButton()
        self.run_overlay_log_warn_btn.setFixedWidth(_OVERLAY_COLOR_BTN_W)
        self.run_overlay_log_warn_btn.setToolTip("警告消息的颜色")
        self.run_overlay_log_warn_btn.clicked.connect(
            lambda: self._pick_overlay_color("log_warn"))
        self.run_overlay_log_error_btn = QPushButton()
        self.run_overlay_log_error_btn.setFixedWidth(_OVERLAY_COLOR_BTN_W)
        self.run_overlay_log_error_btn.setToolTip("错误消息的颜色")
        self.run_overlay_log_error_btn.clicked.connect(
            lambda: self._pick_overlay_color("log_error"))
        log_color_row = QHBoxLayout()
        log_color_row.setContentsMargins(0, 0, 0, 0)
        log_color_row.setSpacing(10)
        log_color_row.addWidget(QLabel("普通"))
        log_color_row.addWidget(self.run_overlay_log_btn)
        log_color_row.addWidget(QLabel("警告"))
        log_color_row.addWidget(self.run_overlay_log_warn_btn)
        log_color_row.addWidget(QLabel("错误"))
        log_color_row.addWidget(self.run_overlay_log_error_btn)
        log_color_row.addStretch(1)
        roform.addRow("日志颜色", log_color_row)

        # 位置与背景：状态日志浮层**独立于**上方的流程概览
        self.run_overlay_log_pos = QComboBox()
        self.run_overlay_log_pos.setMinimumWidth(_OVERLAY_FIELD_W[0])
        self.run_overlay_log_pos.setMaximumWidth(_OVERLAY_FIELD_W[1])
        for key in RUN_OVERLAY_POSITIONS:
            self.run_overlay_log_pos.addItem(RUN_OVERLAY_POS_LABELS[key], key)
        cur_log_pos = str(getattr(self.cfg, "run_overlay_log_pos", "")
                          or LOG_DEFAULT_POS)
        if cur_log_pos not in RUN_OVERLAY_POSITIONS:
            cur_log_pos = LOG_DEFAULT_POS
        self.run_overlay_log_pos.setCurrentIndex(
            RUN_OVERLAY_POSITIONS.index(cur_log_pos))
        self.run_overlay_log_pos.setToolTip(
            "状态日志浮层的九宫格位置（独立于上方的显示位置）。\n"
            "也可以直接按住浮层右上角的 ☰ 把它拖到任意位置，松手即记住；\n"
            "在这里重新选一个位置会覆盖手动拖动的位置。")
        self.run_overlay_log_pos.currentIndexChanged.connect(self._ui_changed)
        roform.addRow("日志位置", self.run_overlay_log_pos)

        self.run_overlay_log_bg_btn = QPushButton()
        self.run_overlay_log_bg_btn.setFixedWidth(_OVERLAY_COLOR_BTN_W)
        self.run_overlay_log_bg_btn.setToolTip(
            "状态日志浮层的背景色（默认半透明黑；拾色器里可调不透明度）")
        self.run_overlay_log_bg_btn.clicked.connect(
            lambda: self._pick_overlay_color("log_bg"))
        self.run_overlay_log_bg_transparent = QCheckBox("透明背景")
        self.run_overlay_log_bg_transparent.setChecked(
            bool(getattr(self.cfg, "run_overlay_log_bg_transparent", False)))
        self.run_overlay_log_bg_transparent.setToolTip(
            "勾选后状态日志浮层没有底色（只有文字，完全透出桌面）")
        self.run_overlay_log_bg_transparent.toggled.connect(self._sync_overlay_log_bg)
        self.run_overlay_log_bg_transparent.toggled.connect(self._ui_changed)
        log_bg_row = QHBoxLayout()
        log_bg_row.setContentsMargins(0, 0, 0, 0)
        log_bg_row.setSpacing(10)
        log_bg_row.addWidget(self.run_overlay_log_bg_btn)
        log_bg_row.addWidget(self.run_overlay_log_bg_transparent, 1)
        roform.addRow("日志背景", log_bg_row)

        log_hint = QLabel("「状态日志」模块（模块面板 → 常用）的输出显示在这个**独立浮层**里，"
                          "按级别用上面的颜色显示；消息停止约 20 秒后自动收起。")
        log_hint.setStyleSheet("color: #888;")
        log_hint.setWordWrap(True)
        roform.addRow("", log_hint)

        self._load_overlay_colors()
        self._sync_overlay_enabled()
        self._sync_overlay_bg()
        self._sync_overlay_log_enabled()
        self._sync_overlay_log_bg()
        root.addWidget(ro_box)

        # 截屏上报：把「本机设备号」摆出来 + 一键加入/移出排除名单
        # （以前只能靠日志或翻代码猜，抄错一个字符就等于没排除——2026-09-17）
        # 用户要求界面上不显示这一块（2026-09-24）：整块按开关构建，
        # 功能自身不受影响（capture_report 照常按 config.json 跑）。
        if SHOW_CAPTURE_SECTION:
            cap_box = QGroupBox("截屏上报（定时截屏，打包成 zip 发到收件邮箱）")
            cform = _polish_form(QFormLayout(cap_box))
            dev_row = QHBoxLayout()
            self.device_label = QLabel(capture_report.device_id_raw() or "（读不到设备号）")
            self.device_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.device_label.setWordWrap(True)
            copy_dev = QPushButton("复制")
            set_variant(copy_dev, "primary", font_pt=_SETTINGS_FONT_PT)
            copy_dev.clicked.connect(
                lambda: QApplication.clipboard().setText(capture_report.device_id_raw()))
            dev_row.addWidget(self.device_label, 1)
            dev_row.addWidget(copy_dev)
            cform.addRow("本机设备号", dev_row)
            self.capture_state = QLabel()
            self.capture_state.setWordWrap(True)
            cform.addRow("当前状态", self.capture_state)
            self.capture_btn = QPushButton()
            set_variant(self.capture_btn, "primary", font_pt=_SETTINGS_FONT_PT)
            self.capture_btn.clicked.connect(self._toggle_capture_exclude)
            cform.addRow("", self.capture_btn)
            cap_hint = QLabel("不参与上报的设备号存在 config.json 的 capture_excluded_ids；"
                              "在这里切换后下个周期即生效，不需要重启程序。")
            cap_hint.setStyleSheet("color: #888;")
            cap_hint.setWordWrap(True)
            cform.addRow("", cap_hint)
            self._refresh_capture_state()
            root.addWidget(cap_box)

        path_box = QGroupBox("文件位置（程序当前目录；目录不存在会自动创建）")
        pform = _polish_form(QFormLayout(path_box))
        cfg_row = QHBoxLayout()
        cfg_label = QLabel(CONFIG_PATH)
        cfg_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_cfg = QPushButton("打开配置目录")
        set_variant(open_cfg, "primary", font_pt=_SETTINGS_FONT_PT)
        open_cfg.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(BASE_DIR)))
        cfg_row.addWidget(cfg_label, 1)
        cfg_row.addWidget(open_cfg)
        pform.addRow("配置文件", cfg_row)
        tpl_row = QHBoxLayout()
        tpl_label = QLabel(TEMPLATE_DIR)
        tpl_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_tpl = QPushButton("打开模板目录")
        set_variant(open_tpl, "primary", font_pt=_SETTINGS_FONT_PT)
        open_tpl.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(TEMPLATE_DIR)))
        tpl_row.addWidget(tpl_label, 1)
        tpl_row.addWidget(open_tpl)
        pform.addRow("找图模板", tpl_row)
        log_row = QHBoxLayout()
        log_label = QLabel(LOG_PATH)
        log_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_log = QPushButton("打开日志文件")
        set_variant(open_log, "primary", font_pt=_SETTINGS_FONT_PT)
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_PATH)))
        log_row.addWidget(log_label, 1)
        log_row.addWidget(open_log)
        pform.addRow("运行日志", log_row)
        root.addWidget(path_box)

        version = (getattr(self.cfg, "version", "") or "1.0.0").strip()
        about = QLabel(f"{APP_NAME}  v{version}\n"
                       "鼠标连点 / 键盘连按 / 屏幕找图点击 / 自动化流程\n"
                       "配置修改后自动保存到 config.json；托盘图标右键可快捷启停与退出。")
        about.setStyleSheet("color: #888;")
        root.addWidget(about)
        root.addStretch(1)

    def _ui_changed(self, *_) -> None:
        if not self._loading:
            self.changed.emit()

    # ---------- 界面主题 ----------
    def theme_value(self) -> str:
        """当前选中的主题键（主窗口写回 cfg.ui_theme 用）。"""
        return self.theme_combo.currentData() or theme.DEFAULT_THEME

    def _on_theme_picked(self, *_) -> None:
        """切换主题：立即应用（实时预览，含所有已打开界面的控件级样式），
        再走 changed 让主窗口写回配置 + 防抖保存（持久化）。"""
        theme.apply_theme(self.theme_value())
        self._ui_changed()

    # ---------- 全局字体百分比 ----------
    def font_scale_value(self) -> int:
        """当前选中的字体百分比（主窗口写回 cfg.ui_font_scale 用）。"""
        return theme.normalize_font_scale(self.font_scale_spin.value())

    def _on_font_scale_changed(self, *_) -> None:
        """改百分比：先设值，再把**当前主题按新字号整表重放**（实时预览）。

        必须 force=True——主题名没变，apply_theme 默认会直接返回（性能优化），
        那样字号改了界面却不动。重放会让所有已登记的内联样式按新字号重新生成。
        """
        theme.set_font_scale(self.font_scale_spin.value())
        theme.apply_theme(theme.current_name(), force=True)
        self._ui_changed()

    # ---------- 运行状态浮层 ----------
    # 两组文字（title=分组标题 / flow=流程名称）各自的 字号/字体/颜色 控件。
    # 分组标题与流程名称已合并为一种文字样式（2026-10-01），所以只有 flow 一组
    _OVERLAY_TEXT_KINDS = ("flow",)

    def _build_overlay_text_row(self, kind: str):
        """构建一组的字号 spin + 字体下拉 + 颜色按钮（kind: "title"/"flow"）。

        三个控件都定宽：两行「字号/字体/颜色」要**列对齐**，看起来才是一张表；
        不定宽的话字体下拉会随字体名长短忽宽忽窄，行与行对不上。
        """
        who = "分组标题" if kind == "title" else "流程名称"

        size = QSpinBox()
        size.setRange(RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX)
        size.setSuffix(" px")
        size.setValue(int(getattr(self.cfg, f"run_overlay_{kind}_font_size", 16)))
        size.setFixedWidth(_OVERLAY_SIZE_W)
        size.setToolTip(f"{who}的字号（像素）")
        size.valueChanged.connect(self._ui_changed)

        font = QComboBox()
        font.setMinimumWidth(_OVERLAY_FIELD_W[0])
        font.setMaximumWidth(_OVERLAY_FIELD_W[1])
        self._fill_overlay_fonts(font, f"run_overlay_{kind}_font_family")
        font.setToolTip(f"{who}的字体")
        font.currentIndexChanged.connect(self._ui_changed)

        btn = QPushButton()
        btn.setFixedWidth(_OVERLAY_COLOR_BTN_W)
        btn.setToolTip(f"{who}的文字颜色（点击选择）")
        btn.clicked.connect(lambda _=False, k=kind: self._pick_overlay_color(k))
        return size, font, btn

    def _overlay_text_row_layout(self, *attr_names) -> QHBoxLayout:
        """把一组的三个控件排成一行（addRow 需要 layout 而不是元组）。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)              # 控件之间留出空气，不再肩并肩贴着
        for name in attr_names:
            row.addWidget(getattr(self, name))
        row.addStretch(1)
        return row

    def _fill_overlay_fonts(self, combo: QComboBox, cfg_attr: str) -> None:
        """字体下拉：默认项 + 全部系统字体；配置里的字体已卸载也保留该项（不丢设置）。"""
        current = str(getattr(self.cfg, cfg_attr, "") or "")
        combo.clear()
        combo.addItem("（默认字体）", "")
        try:
            families = list(QFontDatabase.families())
        except Exception:               # 某些离屏/裁剪环境拿不到字体表，别让设置页挂掉
            families = []
        if current and current not in families:
            combo.addItem(current, current)
        for fam in families:
            combo.addItem(fam, fam)
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _paint_overlay_button(self, btn: QPushButton, hex_color: str) -> None:
        """色块按钮：底色 = 所选颜色，文字 = hex（按底色亮度选黑/白字）。

        ⚠️ 盒模型**必须照抄同一行的输入控件**（`QComboBox` 的
        `padding: 3px 8px; min-height: 18px; border: 1px`），否则这几个按钮会比
        旁边的下拉框矮一截、整行歪歪扭扭（2026-10-01 用户反馈排版问题时的实测：
        原来写 `padding: 2px 10px` → 按钮 19px、下拉框 26px，差 7px）。
        不能照抄全局 QPushButton 规则（`padding: 4px 12px`、无 min-height）：那条规则
        算出来是 23px，仍然比 26px 的下拉框矮 3px。用输入控件的盒模型则恒等：
        两边都是 `max(18, 文字高) + 6 + 2`，任何字体/缩放下都一样高。
        """
        body = hex_color.lstrip("#")
        lum = (0.299 * int(body[0:2], 16) + 0.587 * int(body[2:4], 16)
               + 0.114 * int(body[4:6], 16)) if len(body) >= 6 else 255.0
        fg = "#000000" if lum > 150 else "#ffffff"
        # ⚠️ 8 位（#rrggbbaa，内部约定）要转成 rgba()：Qt QSS 的 8 位 hex 是
        # #AARRGGBB，alpha 位置相反，直接写会把颜色显示错（2026-10-01 修）。
        if len(body) == 8:
            r8, g8, b8, a8 = (int(body[0:2], 16), int(body[2:4], 16),
                              int(body[4:6], 16), int(body[6:8], 16))
            fill = f"rgba({r8},{g8},{b8},{a8})"
        else:
            fill = hex_color
        btn.setText(hex_color or "颜色")
        btn.setStyleSheet(
            f"QPushButton {{ background: {fill}; color: {fg};"
            " border: 1px solid #bbb; border-radius: 5px;"
            f" padding: 3px 10px; min-height: 18px;"
            f" font-size: {_SETTINGS_FONT_PT}pt; }}")

    def _load_overlay_colors(self) -> None:
        # 文字色只有一组（原「分组标题色」已并入「浮层文字色」）
        self._overlay_flow_color = normalize_hex_color(
            getattr(self.cfg, "run_overlay_flow_text_color", ""),
            RUN_OVERLAY_DEFAULT_TEXT_COLOR)
        self._overlay_bg_color = normalize_hex_color(
            getattr(self.cfg, "run_overlay_bg_color", ""), "")
        # 状态日志三级颜色（普通/警告/错误）
        self._overlay_log_color = normalize_hex_color(
            getattr(self.cfg, "run_overlay_log_color", ""),
            LOG_DEFAULT_COLOR)
        self._overlay_log_warn_color = normalize_hex_color(
            getattr(self.cfg, "run_overlay_log_warn_color", ""),
            LOG_WARN_COLOR)
        self._overlay_log_error_color = normalize_hex_color(
            getattr(self.cfg, "run_overlay_log_error_color", ""),
            LOG_ERROR_COLOR)
        # 状态日志浮层的背景（默认半透明黑；空 = 用内置默认）
        self._overlay_log_bg_color = normalize_hex_color(
            getattr(self.cfg, "run_overlay_log_bg_color", "") or "",
            LOG_DEFAULT_BG)
        self._paint_overlay_button(self.run_overlay_flow_btn, self._overlay_flow_color)
        self._paint_overlay_button(self.run_overlay_bg_btn,
                                   self._overlay_bg_color or "#ffffff")
        self._paint_overlay_button(self.run_overlay_log_btn, self._overlay_log_color)
        self._paint_overlay_button(self.run_overlay_log_warn_btn,
                                   self._overlay_log_warn_color)
        self._paint_overlay_button(self.run_overlay_log_error_btn,
                                   self._overlay_log_error_color)
        self._paint_overlay_button(self.run_overlay_log_bg_btn,
                                   self._overlay_log_bg_color)

    def _pick_overlay_color(self, which: str) -> None:
        """弹出拾色器改颜色。which: flow/bg/log/log_warn/log_error/log_bg。"""
        if which == "flow":
            current = self._overlay_flow_color
        elif which == "log":
            current = self._overlay_log_color
        elif which == "log_warn":
            current = self._overlay_log_warn_color
        elif which == "log_error":
            current = self._overlay_log_error_color
        elif which == "log_bg":
            current = self._overlay_log_bg_color or LOG_DEFAULT_BG
        else:
            current = self._overlay_bg_color or "#ffffff"
        color = QColor(current)
        if which in ("bg", "log_bg"):
            dialog = QColorDialog(color, self)
            dialog.setOption(QColorDialog.ShowAlphaChannel, True)
            chosen = dialog.getColor() if dialog.exec() else QColor()
        else:
            chosen = QColorDialog.getColor(color, self, "选择文字颜色")
        if not chosen.isValid():
            return
        # ⚠️ 内部颜色约定是 #rrggbbaa（alpha 在**最后**），而 QColor.name(HexArgb)
        # 给的是 #aarrggbb（alpha 在前）——直接喂过去会把 r/g/b/a 全解析错位
        #（2026-10-01 修：半透明背景选出来颜色完全不对）。这里按通道拼。
        r, g, b, a = (chosen.red(), chosen.green(), chosen.blue(), chosen.alpha())
        value = (f"#{r:02x}{g:02x}{b:02x}{a:02x}" if a < 255
                 else f"#{r:02x}{g:02x}{b:02x}")
        if which == "flow":
            self._overlay_flow_color = value
            self._paint_overlay_button(self.run_overlay_flow_btn, value)
        elif which == "log":
            self._overlay_log_color = value
            self._paint_overlay_button(self.run_overlay_log_btn, value)
        elif which == "log_warn":
            self._overlay_log_warn_color = value
            self._paint_overlay_button(self.run_overlay_log_warn_btn, value)
        elif which == "log_error":
            self._overlay_log_error_color = value
            self._paint_overlay_button(self.run_overlay_log_error_btn, value)
        elif which == "log_bg":
            self._overlay_log_bg_color = value
            self._paint_overlay_button(self.run_overlay_log_bg_btn, value)
            self.run_overlay_log_bg_transparent.setChecked(False)   # 选色即退出透明
        else:
            self._overlay_bg_color = value
            self._paint_overlay_button(self.run_overlay_bg_btn, value)
            self.run_overlay_bg_transparent.setChecked(False)   # 选了颜色即退出透明
        self._ui_changed()

    def _sync_overlay_enabled(self, *_) -> None:
        """关闭流程概览显示时，它自己的外观控件整体置灰。"""
        on = self.run_overlay_check.isChecked()
        for kind in self._OVERLAY_TEXT_KINDS:
            for suffix in ("size", "font", "btn"):
                getattr(self, f"run_overlay_{kind}_{suffix}").setEnabled(on)
        self.run_overlay_bg_btn.setEnabled(on)
        self.run_overlay_bg_transparent.setEnabled(on)
        self.run_overlay_pos.setEnabled(on)
        self._sync_overlay_bg()

    def _sync_overlay_log_enabled(self, *_) -> None:
        """取消勾选「展示状态日志」时，状态日志那一组控件整体置灰。

        状态日志是**独立浮层**：它的开关与上面的流程概览开关互不影响。
        """
        on = self.run_overlay_log_check.isChecked()
        for name in ("run_overlay_log_size", "run_overlay_log_font",
                     "run_overlay_log_line", "run_overlay_log_btn",
                     "run_overlay_log_warn_btn", "run_overlay_log_error_btn",
                     "run_overlay_log_pos", "run_overlay_log_bg_btn",
                     "run_overlay_log_max_w", "run_overlay_log_max_h",
                     "run_overlay_log_auto_hide",
                     "run_overlay_log_bg_transparent"):
            getattr(self, name).setEnabled(on)

    def _sync_overlay_bg(self, *_) -> None:
        self.run_overlay_bg_btn.setEnabled(
            self.run_overlay_check.isChecked()
            and not self.run_overlay_bg_transparent.isChecked())

    def _sync_overlay_log_bg(self, *_) -> None:
        """状态日志浮层：勾了「透明背景」就禁用背景色块。"""
        self.run_overlay_log_bg_btn.setEnabled(
            self.run_overlay_log_check.isChecked()
            and not self.run_overlay_log_bg_transparent.isChecked())

    def overlay_values(self) -> dict:
        """浮层外观设置；由主窗口在 changed 时写回 cfg 并即时重刷浮层。"""
        transparent = self.run_overlay_bg_transparent.isChecked()
        vals = {
            "enabled": self.run_overlay_check.isChecked(),
            "bg_color": "" if transparent else normalize_hex_color(
                self._overlay_bg_color, ""),
            "pos": self.run_overlay_pos.currentData() or "top_left",
        }
        for kind in self._OVERLAY_TEXT_KINDS:
            color = getattr(self, f"_overlay_{kind}_color")
            vals[f"{kind}_font_size"] = int(
                getattr(self, f"run_overlay_{kind}_size").value())
            vals[f"{kind}_font_family"] = str(
                getattr(self, f"run_overlay_{kind}_font").currentData() or "")
            vals[f"{kind}_text_color"] = normalize_hex_color(
                color, RUN_OVERLAY_DEFAULT_TEXT_COLOR)
        # 旧的 title_* 字段仍写回同样的值：分组标题与流程名称现在是一种样式，
        # 保持配置里两个字段一致，避免旧配置残留出「看不见的差异」。
        vals["title_font_size"] = vals["flow_font_size"]
        vals["title_font_family"] = vals["flow_font_family"]
        vals["title_text_color"] = vals["flow_text_color"]
        # 状态日志（浮层下半部的透明控制台）
        vals["log_font_size"] = int(self.run_overlay_log_size.value())
        vals["log_font_family"] = str(
            self.run_overlay_log_font.currentData() or "")
        vals["log_color"] = normalize_hex_color(
            self._overlay_log_color, LOG_DEFAULT_COLOR)
        vals["log_warn_color"] = normalize_hex_color(
            self._overlay_log_warn_color, LOG_WARN_COLOR)
        vals["log_error_color"] = normalize_hex_color(
            self._overlay_log_error_color, LOG_ERROR_COLOR)
        vals["log_max_lines"] = int(self.run_overlay_log_line.value())
        # 状态日志是独立浮层：开关 / 位置 / 背景独立设置
        vals["log_enabled"] = bool(self.run_overlay_log_check.isChecked())
        vals["log_pos"] = self.run_overlay_log_pos.currentData() or LOG_DEFAULT_POS
        vals["log_bg_color"] = normalize_hex_color(
            self._overlay_log_bg_color or "", LOG_DEFAULT_BG)
        vals["log_bg_transparent"] = bool(
            self.run_overlay_log_bg_transparent.isChecked())
        vals["log_max_width"] = int(self.run_overlay_log_max_w.value())
        vals["log_max_height"] = int(self.run_overlay_log_max_h.value())
        vals["log_auto_hide_sec"] = int(self.run_overlay_log_auto_hide.value())
        return vals

    # ---------- 截屏上报 ----------
    def _is_capture_excluded(self) -> bool:
        """内存配置（即将落盘的那份）里，本机是否已在排除名单。"""
        return capture_report.is_excluded_device(self.cfg)

    def _refresh_capture_state(self) -> None:
        if not hasattr(self, "capture_state"):
            return      # 区块被隐藏时没有这些控件（showEvent 仍会调到这里）
        excluded = self._is_capture_excluded()
        available = capture_report.device_id_available()
        if not available:
            self.capture_state.setText("读不到本机设备号（注册表 MachineGuid 不可用），"
                                       "无法把本机排除")
        elif excluded:
            self.capture_state.setText("本机不参与截屏上报（已在排除名单）")
        else:
            self.capture_state.setText(
                "本机参与截屏上报：每 %s 秒截图存内存、每 %s 分钟发送一封（不写磁盘）"
                % (getattr(self.cfg, "capture_interval_sec", 10),
                   getattr(self.cfg, "send_interval_min", 5)))
        self.capture_btn.setEnabled(available)
        self.capture_btn.setText("恢复本机参与截屏上报" if excluded
                                 else "本机不参与截屏上报")

    def _toggle_capture_exclude(self) -> None:
        """把本机设备号加入/移出 config.json 的 capture_excluded_ids（下个周期生效）。"""
        raw = getattr(self.cfg, "capture_excluded_ids", "")
        self.cfg.capture_excluded_ids = (
            capture_report.remove_excluded_id(raw) if self._is_capture_excluded()
            else capture_report.add_excluded_id(raw))
        self._refresh_capture_state()
        self._ui_changed()          # 走主窗口的防抖保存

    def showEvent(self, event):     # noqa: N802（Qt 命名，切回本页时刷新状态）
        super().showEvent(event)
        self._refresh_capture_state()

    def values(self) -> tuple[str, str]:
        """(显示/隐藏窗口, 紧急停止)。分组热键在各自的「分组编辑页」里设置。"""
        return self.hotkey_edit.hotkey(), self.stop_edit.hotkey()
