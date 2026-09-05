"""消息通知步骤：屏幕浮窗通知 + 后台线程→主线程桥接。

流程步骤在 FlowRunner 的后台线程执行，但通知浮窗是 QWidget，只能在主线程
（QApplication 事件循环所在线程）创建和展示。这里复用 screenshot_actor.ui_call
把浮窗构建调度到主线程执行（后台线程阻塞等待返回，主线程用事件循环完成展示）。

浮窗：无边框、置顶、圆角、按消息类型着色、宽度可配、高度随内容自适应、
自动消失 + 手动关闭。通知是锦上添花，展示失败不应打断流程（返回 None，由
调用方决定是否判失败）。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

# 消息类型 -> 主题配色（浅色主题：背景/边框按类型着色，文字统一深色保证可读）
NOTIFY_TYPES = {
    "info":    {"bg": "#eaf2fa", "border": "#1668a8", "icon": "ℹ", "label": "信息"},
    "success": {"bg": "#e6f4ea", "border": "#1a7f37", "icon": "✓", "label": "成功"},
    "warning": {"bg": "#fdf3e2", "border": "#b85c00", "icon": "⚠", "label": "警告"},
    "error":   {"bg": "#fdecea", "border": "#c62828", "icon": "✕", "label": "错误"},
}

# 显示位置 -> 显示名（表单下拉用；默认 bottom = 屏幕中间底部）
NOTIFY_POSITIONS = {
    "bottom":       "屏幕中间底部",
    "center":       "屏幕中间",
    "top":          "屏幕中间上部",
    "top_left":     "左上",
    "left_center":  "左中",
    "bottom_left":  "左下",
    "top_right":    "右上",
    "right_center": "右中",
    "bottom_right": "右下",
}

# 活动通知列表：持有引用防止父级为 None 的浮窗被 GC 回收；关闭时移除。
_active: list["_Notification"] = []

_MARGIN = 16   # 通知与屏幕边缘的间距（像素）


def _theme(msg_type: str) -> dict:
    return NOTIFY_TYPES.get((msg_type or "").strip(), NOTIFY_TYPES["info"])


class _Notification(QWidget):
    """单条通知浮窗：无边框置顶、圆角着色、宽度固定、高度随内容自适应。"""

    def __init__(self, content: str, msg_type: str, duration: float, width: int):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # 展示时不抢占焦点（自动化场景关键）
        self.setFocusPolicy(Qt.NoFocus)

        theme = _theme(msg_type)
        width = max(120, min(int(width or 320), 1200))

        self._card = QWidget(self)
        self._card.setStyleSheet(
            f"background: {theme['bg']};"
            f"border: 1px solid {theme['border']};"
            "border-radius: 10px;"
        )
        card_lay = QHBoxLayout(self._card)
        card_lay.setContentsMargins(14, 12, 10, 12)
        card_lay.setSpacing(10)

        # 类型图标（按类型着色）
        self._icon = QLabel(theme["icon"])
        self._icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._icon.setFixedWidth(22)
        self._icon.setStyleSheet(
            f"color: {theme['border']}; font-size: 15pt; font-weight: bold;"
            "border: none; background: transparent;")

        # 消息内容：多行、自动换行、可选中复制
        self._label = QLabel(content)
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.PlainText)
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._label.setStyleSheet(
            "color: #1f2d3d; font-size: 10pt; border: none; background: transparent;")

        # 手动关闭按钮
        self._close = QPushButton("✕")
        self._close.setFixedSize(20, 20)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setToolTip("关闭")
        self._close.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #8a939c;"
            " font-size: 10pt; border-radius: 10px; }"
            "QPushButton:hover { background: #0000001a; color: #1f2d3d; }")
        self._close.clicked.connect(self.close)

        card_lay.addWidget(self._icon, 0, Qt.AlignTop)
        card_lay.addWidget(self._label, 1, Qt.AlignTop)
        card_lay.addWidget(self._close, 0, Qt.AlignTop)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._card)

        self.setFixedWidth(width)
        self.adjustSize()   # 高度随内容（含换行）自适应

        if float(duration or 0) > 0:
            QTimer.singleShot(int(float(duration) * 1000), self.close)

    def closeEvent(self, event):
        try:
            if self in _active:
                _active.remove(self)
        except (ValueError, TypeError):
            pass
        super().closeEvent(event)


def _place(widget: "_Notification", position: str) -> None:
    """按 position 把通知定位到主屏相应锚点（可用区域，避开任务栏）。"""
    screen = QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    w, h = widget.width(), widget.height()
    m = _MARGIN
    position = (position or "bottom").strip()

    # 水平锚点
    if position in ("bottom", "center", "top"):
        x = geo.left() + (geo.width() - w) // 2
    elif position in ("top_right", "right_center", "bottom_right"):
        x = geo.right() - w - m
    else:   # top_left / left_center / bottom_left
        x = geo.left() + m

    # 垂直锚点
    if position in ("bottom", "bottom_left", "bottom_right"):
        y = geo.bottom() - h - m
    elif position in ("center", "left_center", "right_center"):
        y = geo.top() + (geo.height() - h) // 2
    else:   # top / top_left / top_right
        y = geo.top() + m

    widget.move(max(x, geo.left()), max(y, geo.top()))


def show_notification(content: str, msg_type: str = "info", duration: float = 2.0,
                      width: int = 320, position: str = "bottom") -> "_Notification | None":
    """弹出一条通知，任意线程可调用（自动调度到主线程展示）。

    content：消息文本（调用方负责 $变量名 解析）；msg_type：info/success/warning/error；
    duration：自动消失秒数，<=0 表示不自动消失（仅手动关闭）；width：通知宽度（像素）；
    position：显示位置（见 NOTIFY_POSITIONS）。
    返回创建的浮窗对象（主线程）或 None（内容为空 / 展示失败）。
    """
    content = (content or "").strip()
    if not content:
        return None

    def _show() -> "_Notification":
        n = _Notification(content, msg_type, duration, width)
        _active.append(n)
        n.show()
        n.raise_()
        _place(n, position)
        return n

    # 复用 screenshot_actor 的 ui_call：主线程直接执行，后台线程经信号桥调度。
    from .screenshot_actor import ui_call
    try:
        return ui_call(_show)
    except Exception:
        return None


def active_count() -> int:
    """当前仍显示中的通知条数（测试用）。"""
    return len([n for n in _active if n.isVisible()])


def close_all() -> None:
    """关闭全部仍在显示的通知（测试/清理用）。"""
    for n in list(_active):
        try:
            n.close()
        except Exception:
            pass
    _active.clear()
