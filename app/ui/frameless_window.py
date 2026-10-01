# -*- coding: utf-8 -*-
"""无边框主窗口基类：整窗圆角卡片 + 自绘标题栏（2026-10-01）。

为什么不用系统标题栏
------------------
- 本机是 **Windows 10**：DWM 给系统标题栏上色（`DWMWA_CAPTION_COLOR`）和四角圆角
  （`DWMWA_WINDOW_CORNER_PREFERENCE`）都是 **Windows 11 22000+** 才有的；
  Win10 只能用 `DWMWA_USE_IMMERSIVE_DARK_MODE` 在黑白之间切，做不到
  「标题栏跟着主题换色」。所以"圆角 + 标题栏随主题"只能自绘。
- 全部编辑弹窗早就改成 `FramelessDialog` 自绘标题栏了（2026-09-27），主窗口再顶着
  一条系统标题栏，整个程序反而是两种观感。

拖动与缩放
----------
一律交给系统：`QWindow.startSystemMove()` / `startSystemResize()`。原生行为因此都保留
（拖到屏幕边缘的 Aero Snap、贴边缩放、多显示器/DPI），自己用鼠标事件模拟很容易翻车。
`startSystemMove()` 返回 False 时退回手动拖动（少数平台不支持）。

缩放热区用**应用级事件过滤器**判定：无边框窗口的内容区全是子控件，而子控件会吃掉
鼠标事件（标签页、列表、输入框都自己处理 move），只看窗口自己的 mouseMoveEvent
在子控件上方根本收不到，光标也就不会变成缩放箭头。过滤器第一句就按
`w.window() is self` 过滤，开销可忽略。

用法
----
继承 `FramelessMainWindow`，内容挂到 `self.body_layout()` 上。
状态栏若命名为 **`winStatus`**，会自动带上卡片底部圆角（见 `window_qss`）。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QPushButton, QStatusBar,
                               QVBoxLayout, QWidget)

from . import theme

BORDER = 1                  # 卡片自绘边框宽度
CORNER_RADIUS = 12          # 四角圆角半径（最大化时归 0）
RESIZE_BAND = 6             # 贴边多少像素内算缩放热区
TITLE_BAR_H = 40            # 自绘标题栏高度
CAPTION_SIZE = (38, 26)     # 标题栏按钮尺寸


def window_qss(maximized: bool = False) -> str:
    """圆角卡片 + 自绘标题栏的样式（颜色全部走主题令牌，切主题时由 hook 自动重映射）。

    圆角是**两段拼**出来的，因为 Qt 的 QSS `border-radius` **不裁剪子控件**：
    卡片自己画外圈 1px 边框 + 圆角；真正盖住四角的是标题栏（上）和状态栏（下），
    所以它们各自带一个 `radius - BORDER` 的内圆角，正好嵌在卡片边框里侧。
    中间的标签页/日志面板都是方角，但它们不落在窗口的四角上，不会露馅。
    """
    t = theme.current()
    g = t.get
    radius = 0 if maximized else CORNER_RADIUS
    inner = max(0, radius - BORDER)
    border = "none" if maximized else f"{BORDER}px solid {g('dialog_border')}"
    return f"""
    QFrame#winCard {{
        background: {g('window_bg')};
        border: {border};
        border-radius: {radius}px;
    }}
    QWidget#winTitleBar {{
        background: {g('panel_bg')};
        border-top-left-radius: {inner}px;
        border-top-right-radius: {inner}px;
    }}
    QLabel#winTitle {{ color: {g('text')}; font-weight: 600; }}
    QLabel#winTitleIcon {{ background: transparent; }}
    QWidget#winBody {{ background: transparent; }}
    /* 标题栏按钮的图形由 CaptionButton.paintEvent 自绘，这里只把通用按钮样式清干净 */
    QPushButton#winMin, QPushButton#winMax, QPushButton#winClose {{
        background: transparent; border: none; padding: 0px; margin: 0px;
    }}
    QStatusBar#winStatus {{
        border-bottom-left-radius: {inner}px;
        border-bottom-right-radius: {inner}px;
    }}
    """


class CaptionButton(QPushButton):
    """标题栏的最小化 / 最大化 / 还原 / 关闭按钮（图形用 QPainter 画）。

    不用字体图标：Windows 的 "Segoe MDL2 Assets" 一旦缺字就是一个方框，
    中文 Windows 上尤其容易踩；自己画 10x10 的矢量图形，任何环境都一致。
    颜色在 paintEvent 里**现取** `theme.token(...)`，所以主题一切换、
    重画一次就跟着变色（不需要重新生成 QSS）。
    """

    def __init__(self, glyph: str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self.setFixedSize(*CAPTION_SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)      # 别抢 Tab 焦点，标题栏按钮不在表单流里
        self.setFlat(True)
        if tooltip:
            self.setToolTip(tooltip)

    def glyph(self) -> str:
        return self._glyph

    def set_glyph(self, glyph: str) -> None:
        if glyph != self._glyph:
            self._glyph = glyph
            self.update()

    def enterEvent(self, ev):
        super().enterEvent(ev)
        self.update()                        # 悬停底色是自绘的，要自己触发重画

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        self.update()

    def paintEvent(self, _ev) -> None:
        # 不调 super().paintEvent()：通用按钮样式（圆角/边框/文字）一律不要，
        # 整个按钮由这里画完。
        hovered = self.underMouse() or self.isDown()
        is_close = self._glyph == "close"
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            if hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.token("danger" if is_close else "hover_bg")))
                p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2),
                                  5, 5)
            p.setPen(QPen(QColor("#ffffff" if (is_close and hovered)
                                 else theme.token("text")), 1.2))
            p.setBrush(Qt.NoBrush)
            cx, cy, s = self.width() / 2.0, self.height() / 2.0, 5.0
            if self._glyph == "min":
                p.drawLine(QPointF(cx - s, cy), QPointF(cx + s, cy))
            elif self._glyph == "max":
                p.drawRect(QRectF(cx - s, cy - s, 2 * s, 2 * s))
            elif self._glyph == "restore":
                # 后面那个方框只画露出来的上边和右边
                p.drawLine(QPointF(cx - s + 3, cy - s), QPointF(cx + s, cy - s))
                p.drawLine(QPointF(cx + s, cy - s), QPointF(cx + s, cy + s - 3))
                p.drawRect(QRectF(cx - s, cy - s + 3, 2 * s - 3, 2 * s - 3))
            else:                       # close
                p.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
                p.drawLine(QPointF(cx + s, cy - s), QPointF(cx - s, cy + s))
        finally:
            p.end()


class _TitleBar(QWidget):
    """自绘标题栏：按住拖动窗口、双击最大化/还原（内容区不响应，避免误拖）。"""

    def __init__(self, win: "FramelessMainWindow"):
        super().__init__(win)
        self.setObjectName("winTitleBar")
        self.setFixedHeight(TITLE_BAR_H)
        self._win = win
        self._drag_offset = None

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        wh = self._win.windowHandle()
        if wh is not None and wh.startSystemMove():
            # 交给系统：原生拖动 + 拖到屏幕边缘的 Aero Snap 都归 Windows 管
            ev.accept()
            return
        self._drag_offset = (ev.globalPosition().toPoint()
                             - self._win.frameGeometry().topLeft())
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag_offset is not None and (ev.buttons() & Qt.LeftButton):
            self._win.move(ev.globalPosition().toPoint() - self._drag_offset)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_offset = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        """双击最大化/还原。

        无边框窗口系统不认识"标题栏双击"（我们没有把 WM_NCHITTEST 交出去），
        所以这里自己处理；也正因为没交出去，不会和系统行为重复触发。
        """
        if ev.button() == Qt.LeftButton:
            self._win.toggle_maximized()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)


def _cursor_for(edges: Qt.Edges):
    """缩放热区 -> 鼠标指针形状。"""
    if not edges:
        return None
    left = bool(edges & Qt.LeftEdge)
    right = bool(edges & Qt.RightEdge)
    top = bool(edges & Qt.TopEdge)
    bottom = bool(edges & Qt.BottomEdge)
    if (left and top) or (right and bottom):
        return Qt.SizeFDiagCursor          # ↖↘
    if (right and top) or (left and bottom):
        return Qt.SizeBDiagCursor          # ↗↙
    if left or right:
        return Qt.SizeHorCursor
    return Qt.SizeVerCursor


class FramelessMainWindow(QMainWindow):
    """无边框主窗口：整窗圆角卡片 + 自绘标题栏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)   # 圆角外的四角透明
        self._chrome_ready = False
        self._resize_edges = Qt.Edges()
        self._build_chrome()
        self._chrome_ready = True
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)     # 缩放热区要能看见子控件上的鼠标事件

    # ------------------------------------------------------------------
    # 窗口骨架
    # ------------------------------------------------------------------
    def _build_chrome(self) -> None:
        self._card = QFrame()
        self._card.setObjectName("winCard")
        self.setCentralWidget(self._card)
        card = QVBoxLayout(self._card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(0)

        # ---- 自绘标题栏：图标 + 标题 + 最小化/最大化/关闭 ----
        self._title_bar = _TitleBar(self)
        tb = QHBoxLayout(self._title_bar)
        tb.setContentsMargins(12, 0, 6, 0)
        tb.setSpacing(8)

        self._icon_label = QLabel()
        self._icon_label.setObjectName("winTitleIcon")
        self._icon_label.setFixedSize(16, 16)
        tb.addWidget(self._icon_label)

        self._title_label = QLabel(self.windowTitle())
        self._title_label.setObjectName("winTitle")
        tb.addWidget(self._title_label)
        tb.addStretch(1)
        # 图标与标题都不吃鼠标：否则按在文字上时事件要等 QLabel 忽略后
        # 才冒泡到标题栏，拖动会有一点点"抓不住"的感觉
        for lbl in (self._icon_label, self._title_label):
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._btn_min = CaptionButton("min", "最小化", self._title_bar)
        self._btn_min.clicked.connect(self.showMinimized)
        self._btn_max = CaptionButton("max", "最大化", self._title_bar)
        self._btn_max.clicked.connect(self.toggle_maximized)
        self._btn_close = CaptionButton("close", "关闭（隐藏到托盘）", self._title_bar)
        self._btn_close.clicked.connect(self.close)
        self._caption_btns = (self._btn_min, self._btn_max, self._btn_close)
        for btn in self._caption_btns:
            tb.addWidget(btn)
        card.addWidget(self._title_bar)

        # ---- 内容区（子类往 body_layout() 里加东西）----
        self._body = QWidget()
        self._body.setObjectName("winBody")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(0)
        card.addWidget(self._body, 1)

        self._status_bar = QStatusBar()
        self._status_bar.setObjectName("winStatus")   # 底部圆角靠这个 objectName
        self._status_bar.setSizeGripEnabled(False)    # 右下角系统抓手会破坏圆角
        self._sync_icon()

    def body_layout(self) -> QVBoxLayout:
        """内容容器的布局：子类把标签页/日志面板/状态栏加到这里。"""
        return self._body_lay

    def status_bar(self) -> QStatusBar:
        """卡片内的状态栏（子类负责把它加进 body_layout 的**最后**）。"""
        return self._status_bar

    def title_bar(self) -> QWidget:
        return self._title_bar

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    def window_qss(self) -> str:
        """窗口级 QSS。子类要追加自己的规则就覆盖它（记得 `super().window_qss()`）。"""
        return window_qss(self.isMaximized())

    def apply_window_qss(self) -> None:
        """套上窗口级 QSS 并重画自绘元素（主题切换 / 最大化状态变化时调）。"""
        self.setStyleSheet(self.window_qss())
        self.refresh_chrome()

    def refresh_chrome(self) -> None:
        """重画不走 QSS 的部分：标题栏按钮图形、窗口图标。"""
        for btn in getattr(self, "_caption_btns", ()):
            btn.update()
        self._sync_icon()

    def _sync_icon(self) -> None:
        """标题栏图标跟着窗口图标走（应用图标在 main.py 里设，这里取到就用）。"""
        if not hasattr(self, "_icon_label"):
            return
        icon = self.windowIcon()
        if icon.isNull():
            self._icon_label.clear()
        else:
            self._icon_label.setPixmap(icon.pixmap(16, 16))

    # ------------------------------------------------------------------
    # 最大化 / 最小化
    # ------------------------------------------------------------------
    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _sync_max_button(self) -> None:
        if not hasattr(self, "_btn_max"):
            return
        self._btn_max.set_glyph("restore" if self.isMaximized() else "max")
        self._btn_max.setToolTip("向下还原" if self.isMaximized() else "最大化")

    def changeEvent(self, ev) -> None:
        super().changeEvent(ev)
        if ev.type() != QEvent.WindowStateChange or not self._chrome_ready:
            return
        # 最大化时圆角/边框归 0，否则屏幕四角会露出缝隙
        self._sync_max_button()
        self.apply_window_qss()

    def setWindowTitle(self, title: str) -> None:
        super().setWindowTitle(title)
        if hasattr(self, "_title_label"):
            self._title_label.setText(title)

    # ------------------------------------------------------------------
    # 贴边缩放（应用级事件过滤器）
    # ------------------------------------------------------------------
    def edges_at(self, pos) -> Qt.Edges:
        """窗口坐标 pos 落在哪几条边上（不在热区内返回空）。"""
        if self.isMaximized() or not self.isVisible():
            return Qt.Edges()
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()
        if x < 0 or y < 0 or x >= w or y >= h:
            return Qt.Edges()
        edges = Qt.Edges()
        if x < RESIZE_BAND:
            edges |= Qt.LeftEdge
        elif x >= w - RESIZE_BAND:
            edges |= Qt.RightEdge
        if y < RESIZE_BAND:
            edges |= Qt.TopEdge
        elif y >= h - RESIZE_BAND:
            edges |= Qt.BottomEdge
        return edges

    def eventFilter(self, obj, ev):
        if ev.type() not in (QEvent.MouseMove, QEvent.MouseButtonPress):
            return False
        if not isinstance(obj, QWidget) or obj.window() is not self:
            return False            # 只关心本窗口里的事件
        if self.isMaximized():
            return False
        pos = self.mapFromGlobal(ev.globalPosition().toPoint())
        edges = self.edges_at(pos)
        if ev.type() == QEvent.MouseMove:
            if edges != self._resize_edges:
                self._resize_edges = edges
                cursor = _cursor_for(edges)
                if cursor is None:
                    self.unsetCursor()
                else:
                    self.setCursor(cursor)
            return False
        if ev.button() != Qt.LeftButton or not edges:
            return False
        wh = self.windowHandle()
        if wh is not None and wh.startSystemResize(edges):
            return True             # 交给系统缩放，后续事件不用再给控件
        return False
