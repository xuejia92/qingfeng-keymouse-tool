"""运行状态浮层：两个**互相独立**的置顶悬浮窗（2026-09 ~ 2026-10）。

1) **流程概览浮层**（`RunningOverlay`）—— 显示正在运行的分组和流程。
   区分三种执行来源（分组异步 / 分组全部 / 单个流程）：标题分别为
   「分组「名」· 异步流程」「分组「名」· 全部流程」「流程」；每条流程一行
   「流程名 [热键]」（没有热键写「[无]」）。外观（标题/流程名称各自的字号、
   字体、颜色 + 背景、位置）在设置页可配。没有任何流程运行时自动隐藏。

2) **状态日志浮层**（`StatusLogOverlay`）—— 类似「透明控制台」。
   显示「状态日志」步骤输出的消息流，每条一行「[HH:MM:SS] ⚠/✕ 文本」，
   按级别（普通 / 警告 / 错误）着色，等宽字体。
   **默认黑色半透明背景（#000000aa）、默认屏幕左下角**；字号/字体/三级颜色/
   最多行数/背景/位置都可在设置页配置；消息停止 配置的秒数后自动收起并清空。

两者**完全独立**（2026-10-01 用户要求「状态日志单独显示，不要和分组、流程放一起，
可以单独设置位置」）：各自的位置、背景、显示与隐藏时机，互不影响。

线程约定：QWidget 只能在主线程碰，这里只提供「主线程刷新」的接口
（流程概览由 MainWindow 订阅 FlowTab.runningStateChanged 驱动）；
后台流程线程里输出状态日志请走 `screenshot_actor.ui_call`。
"""
from __future__ import annotations

import html
import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QPushButton,
                               QTextEdit, QVBoxLayout, QWidget)

from .config import (RUN_OVERLAY_DEFAULT_TEXT_COLOR, RUN_OVERLAY_FONT_SIZE_MAX,
                     RUN_OVERLAY_FONT_SIZE_MIN,
                     RUN_OVERLAY_LOG_AUTO_HIDE_MAX, RUN_OVERLAY_LOG_AUTO_HIDE_MIN,
                     RUN_OVERLAY_LOG_HEIGHT_MAX, RUN_OVERLAY_LOG_HEIGHT_MIN,
                     RUN_OVERLAY_LOG_MAX_LINES_MAX, RUN_OVERLAY_LOG_MAX_LINES_MIN,
                     RUN_OVERLAY_LOG_WIDTH_MAX, RUN_OVERLAY_LOG_WIDTH_MIN,
                     STATUS_LOG_LEVELS, normalize_hex_color,
                     normalize_xy_pos)

log = logging.getLogger(__name__)

EDGE_MARGIN = 24          # 距屏幕可用区域边缘的留白
MAX_VISIBLE = 20          # 流程概览最多列多少条（再多就截断 + 省略号）
MAX_STATUS_CHARS = 200    # 单条状态日志最长字符数（超出截断，避免浮层撑破屏幕）
COUNTDOWN_W = 30          # 倒计时标签的固定宽度（px，避免窗口宽度随秒数跳动）
AUTO_HIDE_DEFAULT_SEC = 60   # 状态日志：默认「无新消息多少秒后自动收起」（用户可配）
COUNTDOWN_SHOW_SEC = 10      # 倒计时数字只在剩余最后这么多秒时才出现（用户要求）

# 状态日志浮层的默认外观（2026-10-01 用户要求：默认黑色透明背景、左下角）
LOG_DEFAULT_BG = "#000000cc"     # 半透明黑（约 80% 不透明），像终端底色；浅色桌面上也压得住
LOG_DEFAULT_POS = "bottom_left"  # 默认左下角
# 三级默认颜色。⚠️ 取值必须**避开所有主题令牌色值**：浮层的 setStyleSheet 会过主题
# 引擎，撞上令牌色的字面量切主题时会被重映射，浮层这种「用户自定义外观」就变色了
# （test_theme 的契约测试会拦住）。
LOG_DEFAULT_COLOR = "#dcdfe3"    # 普通消息（浅灰，深色底上可读）
LOG_WARN_COLOR = "#f0b429"       # 警告（琥珀）
LOG_ERROR_COLOR = "#ff5d5d"      # 错误（红）
LOG_LEVEL_PREFIX = {"normal": "", "warn": "⚠ ", "error": "✕ "}

# 位置九宫格：键 -> (水平锚点, 垂直锚点)；顺序与 config.RUN_OVERLAY_POSITIONS 一致
_ANCHORS = {
    "top_left": ("left", "top"),
    "top_center": ("center", "top"),
    "top_right": ("right", "top"),
    "middle_left": ("left", "middle"),
    "center": ("center", "middle"),
    "middle_right": ("right", "middle"),
    "bottom_left": ("left", "bottom"),
    "bottom_center": ("center", "bottom"),
    "bottom_right": ("right", "bottom"),
}

# 模块级强引用：窗口没有父对象，只靠 Qt 留不住 Python 侧引用（同 overlay_actor）。
_overlay: "RunningOverlay | None" = None            # 流程概览浮层
_status_overlay: "StatusLogOverlay | None" = None   # 状态日志浮层
# 当前生效的配置（AppConfig 或含 run_overlay_* 字段的替身）；None = 全部用默认值。
_cfg = None


def _hotkey_display(hk: str) -> str:
    """热键显示名：每段首字母大写（ctrl+alt+q → Ctrl+Alt+Q）；空段忽略。"""
    parts = [p.strip() for p in str(hk or "").split("+") if p.strip()]
    return "+".join(p.capitalize() for p in parts)


def clamp_font_size(value) -> int:
    """字号收敛到合法区间（设置页/配置都可能给出越界值）。"""
    try:
        fs = int(value)
    except (TypeError, ValueError):
        fs = 16
    return max(RUN_OVERLAY_FONT_SIZE_MIN, min(RUN_OVERLAY_FONT_SIZE_MAX, fs))


def _css_color(hex_color: str) -> str:
    """'#rrggbb' -> 'rgb(r,g,b)'；'#rrggbbaa' -> 'rgba(r,g,b,a)'（alpha 0~255）。"""
    body = (hex_color or "").lstrip("#")
    r, g, b = int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16)
    if len(body) == 8:
        return f"rgba({r},{g},{b},{int(body[6:8], 16)})"
    return f"rgb({r},{g},{b})"


def _qss_font_family(font_family: str) -> str:
    """字体族名转 QSS 片段；空串返回空（用系统默认）。名称含空格需引号包裹。"""
    name = (font_family or "").strip().replace('"', "")
    return f' font-family: "{name}";' if name else ""


def _label_qss(root: str, prop: str, size: int, color: str,
               family: str, bold: bool) -> str:
    """单个 QLabel 规则的 QSS 片段（root 为窗口 objectName）。"""
    fam = _qss_font_family(family)
    weight = " font-weight: 700;" if bold else ""
    return (f"QWidget#{root} QLabel#{prop} {{ color: {color};"
            f" font-size: {size}px;{weight}{fam} }}")


def build_stylesheet(text_style: dict, bg_color: str) -> str:
    """流程概览浮层的 QSS；纯函数便于测试。

    text_style：{"size": px, "family": 字体族名或"", "color": 文字色}。
    2026-10-01 起「分组标题」与「流程名称」合并成一种文字样式（都显示在一行）。
    """
    bg = _css_color(bg_color) if bg_color else "transparent"
    return (
        f"QFrame#runningOverlayCard {{ background: {bg}; border-radius: 10px; }}"
        + _label_qss("runningOverlayCard", "flow", text_style["size"],
                     text_style["color"], text_style["family"], bold=False))


def build_log_stylesheet(log_style: dict, bg_color: str) -> str:
    """状态日志浮层（透明控制台）的 QSS；纯函数便于测试。

    log_style：{"size": px, "family": 字体族名或"", "color": 默认字色}。
    各级别的实际颜色在 HTML 里内联（见 StatusLogOverlay._render_log）。
    """
    bg = _css_color(bg_color) if bg_color else "transparent"
    # 关闭叉的配色一律用 rgba()：hex 字面量会被主题引擎按令牌重映射，
    # 浮层这种「用户自定义外观」要保持固定（见 §27 / §32 的颜色约定）。
    return (
        f"QFrame#statusLogOverlayCard {{ background: {bg}; border-radius: 10px; }}"
        + (f"QTextEdit#statusLog {{ color: {log_style['color']};"
           f" font-size: {log_style['size']}px;"
           f"{_qss_font_family(log_style['family'])}"
           " background: transparent; border: none; padding: 0px; }"
)
        + "QPushButton#statusLogClose {"
          " color: rgba(220,223,227,150); background: transparent;"
          " border: none; padding: 0px; font-size: 13px; }"
          "QPushButton#statusLogClose:hover {"
          " color: rgba(255,255,255,235);"
          " background: rgba(255,255,255,45); border-radius: 4px; }"
          # 拖动把手（☰）：和 ✕ 一样用 rgba，避免被主题令牌重映射
          "QLabel#statusLogDrag { color: rgba(220,223,227,170);"
          " background: transparent; font-size: 12px; }"
          # 滚动条：细（6px）、贴最右、无箭头、无无效空间（按钮已移到上方，
          # 消息区占满整框，滚动条自然贴到框的最右缘）。颜色一律 rgba 防主题重映射。
          "QTextEdit#statusLog QScrollBar:vertical {"
          " background: rgba(255,255,255,18); border: none;"
          " width: 6px; margin: 0px; border-radius: 3px; }"
          "QTextEdit#statusLog QScrollBar::handle:vertical {"
          " background: rgba(255,255,255,90); border-radius: 3px; min-height: 24px; }"
          "QTextEdit#statusLog QScrollBar::handle:vertical:hover {"
          " background: rgba(255,255,255,150); }"
          "QTextEdit#statusLog QScrollBar::add-line:vertical,"
          "QTextEdit#statusLog QScrollBar::sub-line:vertical {"
          " height: 0px; width: 0px; border: none; background: none; }"
          "QTextEdit#statusLog QScrollBar::up-arrow:vertical,"
          "QTextEdit#statusLog QScrollBar::down-arrow:vertical {"
          " image: none; border: none; background: none; width: 0px; height: 0px; }"
          "QTextEdit#statusLog QScrollBar::add-page:vertical,"
          "QTextEdit#statusLog QScrollBar::sub-page:vertical {"
          " background: transparent; }"
          # 自动收起倒计时（关闭叉左边）
          f"QLabel#statusLogCountdown {{ color: rgba(220,223,227,195);"
          f" font-size: {max(10, int(log_style['size']) - 2)}px; }}")


def content_margins(bg_color: str) -> tuple[int, int, int, int]:
    """有底色卡片时给一点内边距，文字不贴圆角边；透明时贴边更省空间。"""
    return (12, 8, 12, 9) if bg_color else (0, 0, 0, 0)


def position_for(pos_key: str, w: int, h: int, area) -> tuple[int, int]:
    """九宫格定位：返回浮层左上角应在的屏幕坐标（area 为可用区 QRect）。"""
    m = EDGE_MARGIN
    hx, vy = _ANCHORS.get(pos_key, _ANCHORS["top_left"])
    if hx == "left":
        x = area.left() + m
    elif hx == "center":
        x = area.left() + (area.width() - w) // 2
    else:
        x = area.left() + area.width() - w - m
    if vy == "top":
        y = area.top() + m
    elif vy == "middle":
        y = area.top() + (area.height() - h) // 2
    else:
        y = area.top() + area.height() - h - m
    return x, y


def _text_style(prefix: str, fallback_color: str) -> dict:
    """取一组（标题/流程名称）的字号/字体/颜色；缺失或非法走默认。"""
    return {
        "size": clamp_font_size(getattr(_cfg, f"run_overlay_{prefix}_font_size", 16)),
        "family": str(getattr(_cfg, f"run_overlay_{prefix}_font_family", "") or ""),
        "color": normalize_hex_color(
            getattr(_cfg, f"run_overlay_{prefix}_text_color", fallback_color),
            fallback_color),
    }


def _live_values() -> dict:
    """流程概览浮层的外观值；配置缺失/非法时走默认。

    返回 {"title": {size,family,color}, "flow": {...}, "bg": str, "pos": str}。
    标题与流程名称两组字互相独立；背景与位置整体共用。
    """
    pos = str(getattr(_cfg, "run_overlay_pos", "") or "")
    if pos not in _ANCHORS:
        pos = "top_left"
    return {
        # 分组标题与流程名称合并成一种文字样式（2026-10-01 用户要求）
        "text": _text_style("flow", RUN_OVERLAY_DEFAULT_TEXT_COLOR),
        "bg": normalize_hex_color(getattr(_cfg, "run_overlay_bg_color", ""), ""),
        "pos": pos,
    }


def _log_live_values() -> dict:
    """状态日志浮层的外观值（独立于流程概览：自己的位置与背景）。"""
    pos = str(getattr(_cfg, "run_overlay_log_pos", "") or "")
    if pos not in _ANCHORS:
        pos = LOG_DEFAULT_POS
    # 勾了「透明背景」→ 完全透明；否则用配置色，没配就用内置半透明黑
    if bool(getattr(_cfg, "run_overlay_log_bg_transparent", False)):
        bg = ""
    else:
        bg = normalize_hex_color(
            getattr(_cfg, "run_overlay_log_bg_color", "") or "", LOG_DEFAULT_BG)
    colors = {
        "normal": normalize_hex_color(
            getattr(_cfg, "run_overlay_log_color", "") or "", LOG_DEFAULT_COLOR),
        "warn": normalize_hex_color(
            getattr(_cfg, "run_overlay_log_warn_color", "") or "", LOG_WARN_COLOR),
        "error": normalize_hex_color(
            getattr(_cfg, "run_overlay_log_error_color", "") or "", LOG_ERROR_COLOR),
    }
    try:
        max_lines = int(getattr(_cfg, "run_overlay_log_max_lines", 8))
    except (TypeError, ValueError):
        max_lines = 8
    max_lines = max(RUN_OVERLAY_LOG_MAX_LINES_MIN,
                    min(RUN_OVERLAY_LOG_MAX_LINES_MAX, max_lines))
    try:
        max_w = int(getattr(_cfg, "run_overlay_log_max_width", 320))
    except (TypeError, ValueError):
        max_w = 320
    max_w = max(RUN_OVERLAY_LOG_WIDTH_MIN, min(RUN_OVERLAY_LOG_WIDTH_MAX, max_w))
    try:
        max_h = int(getattr(_cfg, "run_overlay_log_max_height", 180))
    except (TypeError, ValueError):
        max_h = 180
    max_h = max(RUN_OVERLAY_LOG_HEIGHT_MIN,
                min(RUN_OVERLAY_LOG_HEIGHT_MAX, max_h))
    try:
        auto_hide = int(getattr(_cfg, "run_overlay_log_auto_hide_sec",
                                AUTO_HIDE_DEFAULT_SEC))
    except (TypeError, ValueError):
        auto_hide = AUTO_HIDE_DEFAULT_SEC
    auto_hide = max(RUN_OVERLAY_LOG_AUTO_HIDE_MIN,
                    min(RUN_OVERLAY_LOG_AUTO_HIDE_MAX, auto_hide))
    return {
        "size": clamp_font_size(getattr(_cfg, "run_overlay_log_font_size", 12)),
        "family": str(getattr(_cfg, "run_overlay_log_font_family", "") or ""),
        "color": colors["normal"],
        "colors": colors,
        "max_lines": max_lines,
        "max_width": max_w,
        "max_height": max_h,
        "auto_hide_sec": auto_hide,
        "bg": bg,
        "pos": pos,
    }


class _DragHandle(QLabel):
    """浮层上的拖动把手：按住它能把窗口拖到任意位置（松手记住）。

    只有把手和 ✕ 不设鼠标穿透——窗口其余部分仍然穿透，不会挡住自动化操作。
    """

    def __init__(self, overlay: QWidget):
        super().__init__("☰", overlay)
        self._overlay = overlay
        self._grab_delta = None
        self.setObjectName("statusLogDrag")
        self.setFixedSize(18, 18)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.SizeAllCursor)
        self.setToolTip("按住拖动，把浮层放到任意位置（松手后记住）")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._grab_delta = (ev.globalPosition().toPoint()
                                - self._overlay.frameGeometry().topLeft())
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._grab_delta is not None and (ev.buttons() & Qt.LeftButton):
            self._overlay.move(ev.globalPosition().toPoint() - self._grab_delta)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._grab_delta is not None:
            self._grab_delta = None
            self._overlay.on_drag_finished()
            ev.accept()


class _BaseOverlay(QWidget):
    """浮层窗口的公共部分：无边框 + 置顶 + 鼠标穿透 + 不抢焦点 + 九宫格定位。"""

    def __init__(self, object_name: str, title: str, lay_cls=QVBoxLayout):
        super().__init__(None, Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # 鼠标穿透
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 透明背景：底色由 QSS 的 background 决定（透明或带透明度的圆角卡片）
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # ⚠️ 背景必须画在**内层 QFrame 卡片**上：顶层 QWidget 自身即使设了
        # WA_StyledBackground，QSS 的 background 也画不出来（实测 grab 出来
        # alpha=0，只有文字）——与 app/ui/frameless.py 的做法一致。
        self.setObjectName(object_name)
        self.setWindowTitle(title)
        self._pos_key = "top_left"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._card = QFrame()
        self._card.setObjectName(f"{object_name}Card")
        outer.addWidget(self._card)
        self._lay = lay_cls(self._card)
        self._lay.setSpacing(1)

    def _place(self) -> None:
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(EDGE_MARGIN, EDGE_MARGIN)
            return
        area = screen.availableGeometry()
        x, y = position_for(self._pos_key, self.width(), self.height(), area)
        self.move(x, y)

    def _show_front(self) -> None:
        """按内容尺寸贴好位置并显示置顶（尺寸上限由子类设置的 maximum* 夹住）。

        ⚠️ 这里**不能用 `adjustSize()`**：内容变化后它仍按上一次的尺寸收敛
        （实测 3 条消息时给 286×115，而正确的 `sizeHint()` 是 342×163），
        结果就是浮层只显示前几行、后面的被裁掉（2026-10-01 排查）。
        """
        lay = self.layout()
        if lay is not None:
            # invalidate() 才会丢掉**缓存的 sizeHint**（只 activate 不够：实测
            # 内容变了以后仍拿到旧的 286×115），否则窗口按旧尺寸裁掉后面的行。
            lay.invalidate()
            lay.activate()
        hint = self.sizeHint()
        w = min(hint.width(), self.maximumWidth())
        h = min(hint.height(), self.maximumHeight())
        self.resize(max(w, self.minimumWidth()), max(h, self.minimumHeight()))
        self._place()
        self.show()
        self.raise_()


class RunningOverlay(_BaseOverlay):
    """流程概览浮层：列示正在运行的流程（每行「分组 - 流程 [热键]」）。

    2026-10-01 用户要求：去掉单独的「分组标题」行、不再只显示流程名 ——
    现在**每一行都是「分组名 - 流程名  [热键]」**（没分组写「未分组」），
    分组名因此总能看见。
    """

    def __init__(self):
        super().__init__("runningOverlay", "运行中")
        self.last_overview: dict | None = None   # 最近一次内容，设置变化后按它重刷
        self._rows: list[QLabel] = []
        self._apply_style()

    def _apply_style(self) -> None:
        """按当前配置重设样式与内边距（每次刷新都调，保证设置即时生效）。"""
        vals = _live_values()
        self._pos_key = vals["pos"]
        self.setStyleSheet(build_stylesheet(vals["text"], vals["bg"]))
        self._lay.setContentsMargins(*content_margins(vals["bg"]))

    def row_text(self, name: str, hotkey: str, group: str) -> str:
        """一行显示文案：「分组 - 流程  [热键]」（无分组写「未分组」）。纯逻辑便于测试。"""
        grp = (group or "").strip() or "未分组"
        hk = _hotkey_display(hotkey) or "无"
        return f"{grp} - {name}  [{hk}]"

    def set_overview(self, source: str, group: str,
                     flows: list[tuple]) -> None:
        """刷新内容（主线程）。flows = [(流程名, 热键, 是否异步[, 分组名]), ...]；空 → 隐藏。

        source/group 只用于记录（显示上不再区分来源：每行自带分组名）。
        兼容 3 元组（旧数据/测试）——缺分组名时回退到 overview 里的 group。
        """
        self.last_overview = {"source": source, "group": group,
                              "flows": list(flows)}
        if not flows:
            self.hide()
            return

        texts: list[str] = []
        for item in flows:
            name, hk = item[0], item[1]
            grp = item[3] if len(item) > 3 else group
            texts.append(self.row_text(name, hk, grp))

        need = min(len(texts), MAX_VISIBLE)
        while len(self._rows) < need:
            lab = QLabel()
            lab.setObjectName("flow")
            self._rows.append(lab)
            self._lay.addWidget(lab)
        for i, lab in enumerate(self._rows):
            if i < need:
                lab.setText(texts[i])
                lab.show()
            else:
                lab.hide()
        extra = len(texts) - need
        if extra > 0:
            self._rows[need - 1].setText(
                f"{self._rows[need - 1].text()}  …等 {extra + 1} 个")

        self._apply_style()
        self._show_front()


class StatusLogOverlay(_BaseOverlay):
    """状态日志浮层：透明控制台，独立位置 / 独立背景（默认左下角 + 半透明黑）。

    交互（2026-10-01 用户要求「添加一个关闭的叉，可隐藏状态日志」）：
    - 窗口本身仍**鼠标穿透**（不挡桌面操作），消息文字也设了穿透；
    - 只有右上角的 ✕ 按钮**不穿透**，所以它能被点到（Qt 的命中测试会跳过
      设了 WA_TransparentForMouseEvents 的控件，未设的子控件照常接收事件）；
    - 点 ✕ 只隐藏窗口，消息保留——下一条状态日志到来时会重新显示。
    """

    def __init__(self):
        super().__init__("statusLogOverlay", "状态日志")
        # 消息区用只读 QTextEdit 而不是 QLabel：QLabel 在**固定尺寸**下内容会被裁、
        # 且无法自动滚到最新一行（实测只显示得出一条消息，2026-10-01 用户反馈）。
        # QTextEdit 天然支持「固定框 + 内部滚动」，底部对齐交给滚动条即可。
        self._log_box = QTextEdit()
        self._log_box.setObjectName("statusLog")
        self._log_box.setReadOnly(True)
        self._log_box.setFrameShape(QFrame.NoFrame)
        # 滚动条按需显示、贴最右（按钮已移到上方工具条，消息区占满整框）。
        # ⚠️ 不设鼠标穿透——否则收不到滚轮事件、用户没法翻看旧消息。
        self._log_box.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._log_box.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._log_box.setCursor(Qt.IBeamCursor)
        # 自动收起倒计时（显示在关闭叉左边；用户要求「如果是自动关闭，
        # 关闭按钮左边显示倒计时」）
        self._countdown = QLabel("")
        self._countdown.setObjectName("statusLogCountdown")
        self._countdown.setToolTip("自动收起倒计时")
        self._countdown.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 固定宽度：倒计时文字在「20s → 9s」之间变化，不固定的话整窗宽度会跳
        self._countdown.setFixedWidth(COUNTDOWN_W)
        # 关闭叉：唯一可点击的部件（窗口其余部分穿透，不挡自动化操作）
        # 叉用 ×（U+00D7）而不是 ✕（U+2715）：后者在部分中文字体下缺字形会显示成方框
        self._close_btn = QPushButton("×")
        self._close_btn.setObjectName("statusLogClose")
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("隐藏状态日志（有新消息时会再次显示）")
        self._close_btn.clicked.connect(self._on_close_clicked)
        # 拖动把手（☰）：只有它和 ✕ 接收鼠标，其余区域仍穿透
        self._drag_handle = _DragHandle(self)
        # 顶部工具条：倒计时 + ☰ 拖动 + × 关闭，放在消息区**上方外侧**
        #（2026-10-01 用户要求：把按钮移出消息框，让消息区占满、滚动条贴最右）。
        self._toolbar = QHBoxLayout()
        self._toolbar.setSpacing(4)
        self._toolbar.setContentsMargins(0, 0, 0, 0)
        self._toolbar.addStretch(1)
        self._toolbar.addWidget(self._countdown, 0)
        self._toolbar.addWidget(self._drag_handle, 0)
        self._toolbar.addWidget(self._close_btn, 0)
        self._lay.setSpacing(2)
        self._lay.addLayout(self._toolbar)
        self._lay.addWidget(self._log_box, 1)
        # [(时间, 文本, 级别, 颜色)]，只保留最近 N 条
        self._log_lines: list[tuple[str, str, str, str]] = []
        # 用户手动点过 ✕：本轮内不再自动弹出（流程重新执行时解除，见 reset_for_new_run）
        self._closed_by_user = False
        # 消息停止 配置的秒数后自动收起（消息保留）
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self._on_auto_hide)
        # 倒计时刷新（每 200ms 一次，够平滑又不费）
        self._tick = QTimer(self)
        self._tick.setInterval(200)
        self._tick.timeout.connect(self._update_countdown)
        self._apply_style()

    def _apply_style(self) -> None:
        """按当前配置重设样式、**固定尺寸**与内边距（设置改动后立即生效）。

        尺寸取设置里的「固定尺寸」（宽×高）：**不随文字多少变化**（2026-10-01 用户要求），
        内容超出就以底部对齐裁掉上面的旧行，最新消息始终可见（像终端）。
        """
        vals = _log_live_values()
        self._pos_key = vals["pos"]
        self.setStyleSheet(build_log_stylesheet(
            {"size": vals["size"], "family": vals["family"], "color": vals["color"]},
            vals["bg"]))
        margins = content_margins(vals["bg"])
        self._lay.setContentsMargins(*margins)
        self.setFixedSize(vals["max_width"], vals["max_height"])
        inner_w = max(40, vals["max_width"] - margins[0] - margins[2])
        inner_h = max(20, vals["max_height"] - margins[1] - margins[3]
                      - self._toolbar.sizeHint().height())
        self._log_box.setFixedSize(inner_w, inner_h)   # 固定框，内部滚动
        # 透明背景时给文字加一层暗阴影：浅色桌面/白底窗口上也能看清
        #（透明模式下没有底色可依托，光靠浅色文字会糊在浅背景里，2026-10-01 实测）
        if not vals["bg"]:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(3)
            shadow.setColor(QColor(0, 0, 0, 210))
            shadow.setOffset(1, 1)
            self._log_box.setGraphicsEffect(shadow)
        else:
            self._log_box.setGraphicsEffect(None)
        self._render_log()

    def append_status(self, text: str, level: str = "normal") -> None:
        """追加一条状态日志：按级别着色，只保留最近 N 条（主线程调用）。

        消息**只累加**，不在这里清空：一次流程运行期间各个模块的输出要连起来看。
        清空由「流程重新执行」触发（`main_window._on_flow_started` →
        `running_overlay.clear_status()`），所以浮层中途被 ✕ 关掉、或被自动收起过，
        都不会把这一次运行已经输出的内容弄丢（2026-10-01 用户两次纠正后的语义）。
        """
        vals = _log_live_values()
        if level not in STATUS_LOG_LEVELS:
            level = "normal"
        body = str(text).strip() or "（空）"
        if len(body) > MAX_STATUS_CHARS:
            body = body[:MAX_STATUS_CHARS - 1] + "…"
        self._log_lines.append((time.strftime("%H:%M:%S"), body, level,
                                vals["colors"][level]))
        keep = vals["max_lines"]
        if len(self._log_lines) > keep:
            del self._log_lines[:-keep]        # 只留最近 keep 条
        self._render_log()
        if self._closed_by_user:
            return                  # 用户手动关过：本轮只记录，不再弹出
        self._cancel_auto_hide()
        self._show_front()
        self._schedule_auto_hide()             # 消息停了就自动收起

    def _render_log(self) -> None:
        """把消息流渲染成 HTML：每条一行「[时间] 级别符号 文本」+ 级别颜色。

        尺寸是固定的（见 _apply_style），内容超出时靠**底部对齐**裁掉最上面的旧行，
        最新消息始终在框里可见（2026-10-01 用户要求「不要随文字自动变化」）。
        """
        if not self._log_lines:
            self._log_box.clear()
            return
        parts = []
        for ts, body, level, color in self._log_lines:
            parts.append(
                f'<div style="color:{color};">'
                f'[{ts}] {LOG_LEVEL_PREFIX.get(level, "")}{html.escape(body)}</div>')
        # 智能滚动：**只有原本就停在最底部时才跟随最新消息**；用户往上翻了就不打扰
        #（保持当前浏览位置，让他安心看旧消息）。重新滚到底部后恢复自动跟随。
        sb = self._log_box.verticalScrollBar()
        was_at_bottom = sb.maximum() <= 0 or sb.value() >= sb.maximum() - 4
        prev = sb.value()
        self._log_box.setHtml("".join(parts))
        if was_at_bottom:
            self._log_box.moveCursor(QTextCursor.End)
        else:
            sb.setValue(prev)

    def clear(self) -> None:
        """清空消息并收起浮层（下次有新消息时再显示）。

        流程重新执行时调用：让新一轮从空白开始，不会和上一轮的输出混在一起。
        """
        self._cancel_auto_hide()
        self._log_lines.clear()
        self._render_log()
        self.hide()

    def _on_close_clicked(self) -> None:
        """点 ✕：隐藏并**在本次运行内保持关闭**（后续消息只记录、不再弹出）。

        重新显示的唯一时机是「流程重新执行」（`reset_for_new_run`）——2026-10-01
        用户要求：「如果手动关闭了状态日志，就不要再显示了，下次重新运行日志再显示」。
        """
        self._closed_by_user = True
        self._cancel_auto_hide()
        self.hide()

    def _place(self) -> None:
        """定位：手动拖过的位置优先，否则用设置里的九宫格位置。"""
        custom = normalize_xy_pos(
            getattr(_cfg, "run_overlay_log_custom_pos", "") if _cfg else "")
        if custom:
            x, y = (int(v) for v in custom.split(","))
            self.move(x, y)
            return
        super()._place()

    def on_drag_finished(self) -> None:
        """拖动结束：把新位置写进配置（下次启动还在这儿）。"""
        if _cfg is None:
            return
        try:
            _cfg.run_overlay_log_custom_pos = f"{self.x()},{self.y()}"
            _cfg.save(save_flows=False)      # 只写 config.json，不碰 flows
        except Exception:
            log.debug("保存状态日志浮层位置失败", exc_info=True)

    def reset_for_new_run(self) -> None:
        """流程重新执行：解除「手动关闭」，已有消息的话重新显示出来。"""
        self._closed_by_user = False
        if self._log_lines:
            self._show_front()
            self._schedule_auto_hide()

    def suppress(self) -> None:
        """本轮流程里没有「状态日志」模块：隐藏浮层，别显示旧的空框。"""
        self._cancel_auto_hide()
        self.hide()

    def _schedule_auto_hide(self) -> None:
        sec = int(_log_live_values()["auto_hide_sec"])   # 用户可配（默认 60 秒）
        self._auto_hide.start(sec * 1000)
        self._update_countdown()
        self._tick.start()

    def _cancel_auto_hide(self) -> None:
        if self._auto_hide.isActive():
            self._auto_hide.stop()
        if self._tick.isActive():
            self._tick.stop()
        self._countdown.clear()

    def _update_countdown(self) -> None:
        """关闭叉左边的倒计时数字：**只在剩余最后 10 秒时才出现**（2026-10-01 用户要求）。

        剩余时间还多时保持空白（不要一直挂个「52s」晃眼）；没有倒计时也清空。
        """
        left = self._auto_hide.remainingTime()
        if left < 0:
            self._countdown.clear()
            return
        sec = (left + 999) // 1000        # 向上取整：10.5s 显示 11s（>10 则不显示）
        self._countdown.setText(f"{sec}s" if sec <= COUNTDOWN_SHOW_SEC else "")

    def _on_auto_hide(self) -> None:
        """停留时间到：收起浮层——**消息保留**。

        清空只发生在「流程重新执行」时（`clear()`）；自动收起只是把窗口藏起来，
        下次有新消息会带着之前的记录一起显示，不会被拦腰截断。
        """
        self._cancel_auto_hide()
        self.hide()


# ---------------------------------------------------------------------------
# 对外接口（主线程）
# ---------------------------------------------------------------------------

def set_config(cfg) -> None:
    """注入配置对象（AppConfig）；设置页改动后调用，可见的浮层立即换新样式。"""
    global _cfg
    _cfg = cfg
    if _overlay is not None and _overlay.isVisible():
        if _overlay.last_overview:
            refresh(_overlay.last_overview)     # 有流程概览：整体刷新
        else:
            _overlay._apply_style()
    if _status_overlay is not None and _status_overlay.isVisible():
        _status_overlay._apply_style()
        _status_overlay._show_front()


def refresh(overview: dict) -> None:
    """刷新**流程概览**浮层；无运行流程或设置里关闭显示则隐藏。"""
    global _overlay
    flows = overview.get("flows") or []
    if _cfg is not None and not getattr(_cfg, "run_overlay_enabled", True):
        flows = []          # 设置页关了「显示正在运行的分组和流程」
    if _overlay is None:
        if not flows:
            return
        _overlay = RunningOverlay()
    _overlay.set_overview(overview.get("source", ""),
                          overview.get("group", ""),
                          flows)


def append_status(text: str, level: str = "normal") -> None:
    """往**状态日志浮层**（透明控制台）追加一条消息（主线程调用）。

    级别：normal（普通）/ warn（警告）/ error（错误），按级别用不同颜色显示。
    流程在后台线程里跑，那里请这样调度过来：

        from . import running_overlay
        from .screenshot_actor import ui_call
        ui_call(lambda: running_overlay.append_status(text, level))
    """
    global _status_overlay
    if _cfg is not None and not getattr(_cfg, "run_overlay_log_enabled", True):
        return          # 设置页取消勾选「展示状态日志」
    if _status_overlay is None:
        _status_overlay = StatusLogOverlay()
    _status_overlay.append_status(text, level)


def clear_status() -> None:
    """清空状态日志（主线程；新一轮运行开始时调用）。"""
    if _status_overlay is not None:
        try:
            _status_overlay.clear()
        except RuntimeError:
            pass


def suppress_status() -> None:
    """本轮流程没有「状态日志」模块：隐藏浮层（避免显示上一轮留下的空框）。"""
    if _status_overlay is not None:
        try:
            _status_overlay.suppress()
        except RuntimeError:
            pass


def reset_for_new_run() -> None:
    """流程重新执行时调用：让状态日志重新可以显示（解除用户的手动关闭）。

    与清空无关——**内容不再自动清除**（2026-10-01 用户要求）；
    这个方法只负责「手动关掉的浮层，下次运行流程时重新显示」。
    """
    if _status_overlay is not None:
        try:
            _status_overlay.reset_for_new_run()
        except RuntimeError:
            pass


def close() -> None:
    """销毁两个浮层（程序退出时调用）。"""
    global _overlay, _status_overlay
    for name in ("_overlay", "_status_overlay"):
        win = globals()[name]
        if win is not None:
            try:
                win.close()
            except RuntimeError:
                pass
            globals()[name] = None


def is_visible() -> bool:
    """流程概览浮层是否可见（保持原语义）。"""
    return _overlay is not None and _overlay.isVisible()


def is_status_visible() -> bool:
    """状态日志浮层是否可见。"""
    return _status_overlay is not None and _status_overlay.isVisible()
