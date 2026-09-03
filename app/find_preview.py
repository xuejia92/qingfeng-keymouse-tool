"""找图/目标检测步骤的「效果预览」：命中后在目标区域画红框。

找图步骤在 FlowRunner 的后台线程里执行，而红框窗口是 QWidget，
只能在主线程创建，因此复用 screenshot_actor.ui_call 桥接到主线程。
窗口本身是全屏透明置顶的 QWidget，用 QPainter 画红色矩形边框，
QTimer 到期自动关闭（不阻塞找图步骤）。

show_find_highlight：找图单框；show_boxes_highlight：目标检测多框
（红框左上角类别徽标、右上角置信度徽标）。两种预览共用 _current 引用，
新预览会先关掉旧窗口再替换。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
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
        # 关键：DPI 转换。窗口 setGeometry 用 logical 像素（设备无关），
        # Qt 渲染时按 devicePixelRatioF() 自动放大到物理像素；
        # 但 paintEvent 的 QPainter 坐标是 logical（与窗口 geometry 一致），
        # 而 finder 返回的 rect 是 mss 物理像素坐标。直接画会让红框偏
        # DPR 倍（125% 缩放时偏 25%）。这里除以 DPR 转成 logical 坐标。
        dpr = self.devicePixelRatioF() or 1.0
        x1 = self._rect.left() / dpr
        y1 = self._rect.top() / dpr
        x2 = self._rect.right() / dpr
        y2 = self._rect.bottom() / dpr
        p = QPainter(self)
        p.setPen(QPen(self._color, self._width))
        p.drawRect(QRectF(QPointF(x1, y1), QPointF(x2, y2)))


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


class _BoxesOverlay(QWidget):
    """全屏透明置顶窗口：画多个检测红框 + 文字徽标（目标检测「效果预览」）。

    boxes = [((x1, y1, x2, y2), 左上角标签, 右上角标签), ...]，坐标为虚拟桌面
    物理像素。每个红框：左上角外侧显示检测类别、右上角外侧显示置信度
    （空间不够时收进框内）。QTimer 到期自动关闭。
    """

    def __init__(self, boxes: list, duration_ms: int):
        super().__init__(None,
                         Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._boxes = boxes
        self.setGeometry(QGuiApplication.primaryScreen().virtualGeometry())
        self.show()
        QTimer.singleShot(max(duration_ms, 50), self.close)

    def closeEvent(self, event):
        global _current
        if _current is self:
            _current = None
        super().closeEvent(event)
        self.deleteLater()

    def _draw_badge(self, p: QPainter, text: str, cx: float, top: float,
                    align_right: bool) -> None:
        """在 (cx, top) 画一个红底白字徽标；align_right=True 时右对齐到 cx。"""
        fm = p.fontMetrics()
        pad = 3
        bw = fm.horizontalAdvance(text) + pad * 2
        bh = fm.height() + pad
        x = cx - bw if align_right else cx
        # 顶部放不下就收进框内（top 已经是框顶上方时才会 < 0 的相对判断由调用方处理）
        p.fillRect(QRectF(x, top, bw, bh), QColor(210, 25, 25, 225))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(x, top, bw, bh), Qt.AlignCenter, text)

    def paintEvent(self, event):
        # 与 _HighlightOverlay 相同的 DPI 约定：传入坐标是 mss 物理像素，
        # QPainter 坐标是 logical，需除以 DPR（见 _HighlightOverlay.paintEvent 注释）。
        dpr = self.devicePixelRatioF() or 1.0
        p = QPainter(self)
        font = p.font()
        font.setPointSizeF(9.0)
        font.setBold(True)
        p.setFont(font)
        badge_h = p.fontMetrics().height() + 3
        for rect, tl_label, tr_label in self._boxes:
            x1, y1, x2, y2 = rect
            x1, y1, x2, y2 = x1 / dpr, y1 / dpr, x2 / dpr, y2 / dpr
            p.setPen(QPen(QColor(255, 0, 0), 3))
            p.drawRect(QRectF(QPointF(x1, y1), QPointF(x2, y2)))
            # 徽标优先放框顶上方，放不下则收进框内顶边
            badge_top = y1 - badge_h if y1 - badge_h >= 0 else y1
            if tl_label:
                self._draw_badge(p, tl_label, x1, badge_top, align_right=False)
            if tr_label:
                self._draw_badge(p, tr_label, x2, badge_top, align_right=True)


def show_boxes_highlight(boxes: list, duration: float = 1.0) -> None:
    """在屏幕上画多个检测红框，持续 duration 秒（默认 1 秒）。

    boxes = [((x1, y1, x2, y2), 左上角标签, 右上角标签), ...]，
    虚拟桌面物理像素坐标。任意线程可调用；无 Qt 应用实例时静默跳过。
    """
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None or not boxes:
        return
    duration_ms = max(int(duration * 1000), 50)

    def _show():
        global _current
        if _current is not None:
            _current.close()
        _current = _BoxesOverlay(list(boxes), duration_ms)

    screenshot_actor.ui_call(_show)
