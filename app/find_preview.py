"""找图步骤的「效果预览」：命中后在被找到的图片区域画一个红框。

找图步骤在 FlowRunner 的后台线程里执行，而红框窗口是 QWidget，
只能在主线程创建，因此复用 screenshot_actor.ui_call 桥接到主线程。
窗口本身是全屏透明置顶的 QWidget，用 QPainter 画红色矩形边框，
QTimer 到期自动关闭（不阻塞找图步骤）。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import screenshot_actor

# 当前红框窗口的强引用：无 parent 的 QWidget 若没有 Python 引用会被立即回收，
# 窗口闪没；新的预览会先关掉旧窗口再替换，窗口销毁时清空（避免悬空）。
_current: "_HighlightOverlay | None" = None


class _HighlightOverlay(QWidget):
    """全屏透明置顶窗口，在 rect（虚拟桌面坐标）画红色边框，到期自动关闭。

    窗口覆盖整个虚拟桌面（多显示器），边框画在虚拟桌面物理像素坐标上，
    与 finder 返回的坐标系一致（mss monitors[0] 也是虚拟桌面）。
    """

    def __init__(self, rect: tuple[int, int, int, int], duration_ms: int,
                 color: QColor = QColor(255, 0, 0), width: int = 3):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        x1, y1, x2, y2 = rect
        self._rect = QRect(QPoint(x1, y1), QPoint(x2, y2))
        self._color = color
        self._width = width
        self.setGeometry(QGuiApplication.primaryScreen().virtualGeometry())
        self.show()
        QTimer.singleShot(max(duration_ms, 50), self.close)

    def closeEvent(self, event):
        # 此时 C++ 对象仍有效，可以安全清理模块级引用（destroyed 信号在删除后
        # 触发，届时 Python 包装器可能已失效，不能依赖它清理）
        global _current
        if _current is self:
            _current = None
        super().closeEvent(event)
        self.deleteLater()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(QPen(self._color, self._width))
        p.drawRect(self._rect)


def show_find_highlight(rect: tuple[int, int, int, int], duration: float = 1.0) -> None:
    """在屏幕指定区域画红色边框，持续 duration 秒（默认 1 秒）。

    rect = (x1, y1, x2, y2)，虚拟桌面物理像素坐标（找图命中矩形）。
    任意线程可调用；没有 Qt 应用实例时静默跳过（纯测试环境不炸）。
    """
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        return
    duration_ms = max(int(duration * 1000), 50)

    def _show():
        global _current
        if _current is not None:
            _current.close()
        _current = _HighlightOverlay(rect, duration_ms)

    screenshot_actor.ui_call(_show)
