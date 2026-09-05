"""屏幕截图选区遮罩：冻结屏幕 -> 拖拽框选 -> 手动调整 -> 双击确认。

两种模式：
- template：确认后裁剪保存为找图模板 PNG（captured 信号）
- region：确认后返回找图区域 (x, y, w, h)（虚拟桌面物理像素，regionSelected 信号）

坐标系统一用**虚拟桌面物理像素**，与 mss 抓屏、以及本进程下的 pynput 点击坐标
保持一致：Qt 创建 QApplication 后会把进程提升为 per-monitor DPI aware，此时
SetCursorPos 接受的也是物理坐标，两边同一套坐标才不会错位。

交互：按下拖拽出选区 -> 松开后进入调整态（内部拖动移位 / 边角拖拽改大小 /
框外重新框选）-> 双击确认 -> Esc 取消。
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np
from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .config import TEMPLATE_DIR

_MIN_SIZE = 8        # 选区最小逻辑尺寸
_HANDLE = 10         # 角点手柄判定半径
_EDGE = 6            # 边缘判定厚度
_DIM = 140           # 选区外遮罩透明度


def _physical_geometry(screen) -> tuple[int, int, int, int]:
    """屏幕在虚拟桌面中的物理像素几何 (x, y, w, h)。

    为什么不能简单写 geometry() * devicePixelRatio()：
    Qt 的 screen.geometry() 返回的是「Qt 逻辑虚拟桌面」坐标。多屏混合缩放时，
    某屏的逻辑 origin 是它左边所有屏的**逻辑**宽度之和，而物理 origin 是左边
    所有屏的**物理**宽度之和，两者差的正是前面那些屏的缩放倍数。

    例：左屏 1920 宽 100%，右屏 2560 逻辑宽 150%
        右屏逻辑 origin = 1920，其物理 origin 也应是 1920
        但用本屏 dpr=1.5 去乘 1920 会得到 2880 —— 整整偏了 960 像素

    单屏时 origin 恒为 0，这个错误不会暴露，所以只在多屏异构缩放下才会炸。

    mss 的 monitors[i+1] 直接给出物理坐标（monitors[0] 是虚拟桌面），用它最可靠；
    再做一次尺寸校验，对不上就回退到 dpr 换算。
    """
    geo = screen.geometry()
    dpr = screen.devicePixelRatio() or 1.0
    fallback = (int(geo.x() * dpr), int(geo.y() * dpr),
                int(geo.width() * dpr), int(geo.height() * dpr))
    try:
        import mss
        from PySide6.QtWidgets import QApplication
        with mss.mss() as sct:
            idx = QApplication.screens().index(screen)
            mon = sct.monitors[idx + 1]           # monitors[0] 是虚拟桌面
            w, h = int(mon["width"]), int(mon["height"])
            # 校验：该屏物理尺寸应约等于 逻辑尺寸 × 本屏 dpr
            if abs(w - geo.width() * dpr) <= 2 and abs(h - geo.height() * dpr) <= 2:
                return int(mon["left"]), int(mon["top"]), w, h
    except (ValueError, IndexError, KeyError, OSError, ImportError):
        pass
    return fallback


class CaptureOverlay(QWidget):
    captured = Signal(str)                       # 已保存的模板路径
    regionSelected = Signal(int, int, int, int)  # x, y, w, h（虚拟桌面物理像素）
    pointSelected = Signal(int, int)             # x, y（虚拟桌面物理像素）
    cancelled = Signal()

    def __init__(self, screen, mode: str = "template", parent=None):
        super().__init__(parent)
        self._mode = mode
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        geo = screen.geometry()
        self.setGeometry(geo)
        # 物理几何（多屏混合缩放下不能用 dpr 乘逻辑 origin，见 _physical_geometry）
        self._phys_origin_x, self._phys_origin_y, phys_w, phys_h = \
            _physical_geometry(screen)

        import mss
        with mss.mss() as sct:
            monitor = {
                "left": self._phys_origin_x,
                "top": self._phys_origin_y,
                "width": phys_w,
                "height": phys_h,
            }
            shot = np.asarray(sct.grab(monitor))
        self._img = np.ascontiguousarray(shot[:, :, :3])  # BGR 物理像素
        h, w, ch = self._img.shape
        qimg = QImage(self._img.data, w, h, ch * w, QImage.Format_BGR888)
        self._pixmap = QPixmap.fromImage(qimg)

        self._state = "idle"      # idle / dragging / adjusting
        self._sel: QRect | None = None
        self._origin = None       # 拖拽起点
        self._drag_mode = None    # move / nw / n / ne / e / se / s / sw / w
        self._move_offset = None  # move 模式下按点相对选区左上角的偏移
        self._cursor_pt = None    # point 模式：十字线跟随位置

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._pixmap)
        if self._mode == "point":
            p.fillRect(self.rect(), QColor(0, 0, 0, 90))
            if self._cursor_pt:
                pt = self._cursor_pt
                pen = QPen(QColor(0, 200, 255), 1)
                p.setPen(pen)
                p.drawLine(0, pt.y(), self.width(), pt.y())
                p.drawLine(pt.x(), 0, pt.x(), self.height())
                px = self._phys_origin_x + int(round(pt.x() * self._img.shape[1] / max(self.width(), 1)))
                py = self._phys_origin_y + int(round(pt.y() * self._img.shape[0] / max(self.height(), 1)))
                p.setPen(QPen(QColor(0, 200, 255), 2))
                p.drawText(pt.x() + 14, pt.y() - 10, f"({px}, {py})")
            self._draw_hint(p, "点击屏幕任意位置取坐标 · Esc 取消")
            return
        sel = self._sel
        if sel is None or self._state == "idle":
            p.fillRect(self.rect(), QColor(0, 0, 0, _DIM))
            self._draw_hint(p, "按住鼠标左键拖拽框选，Esc 取消")
            return
        # 选区外变暗（四块矩形，不重复绘制选区内容）
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, sel.top(), QColor(0, 0, 0, _DIM))
        p.fillRect(0, sel.bottom() + 1, W, H - sel.bottom() - 1, QColor(0, 0, 0, _DIM))
        p.fillRect(0, sel.top(), sel.left(), sel.height(), QColor(0, 0, 0, _DIM))
        p.fillRect(sel.right() + 1, sel.top(), W - sel.right() - 1, sel.height(),
                   QColor(0, 0, 0, _DIM))
        # 边框
        p.setPen(QPen(QColor(0, 200, 255), 2))
        p.drawRect(sel)
        # 调整态画 8 个手柄
        if self._state == "adjusting":
            p.setPen(QPen(QColor(0, 200, 255), 1))
            p.setBrush(QColor(255, 255, 255, 230))
            for pt in (sel.topLeft(), sel.topRight(), sel.bottomLeft(), sel.bottomRight(),
                       self._mid(sel.top(), sel.bottom(), sel.left(), 0),
                       self._mid(sel.top(), sel.bottom(), sel.right(), 0),
                       self._mid(sel.left(), sel.right(), sel.top(), 1),
                       self._mid(sel.left(), sel.right(), sel.bottom(), 1)):
                p.drawRect(pt.x() - 3, pt.y() - 3, 6, 6)
            p.setBrush(Qt.NoBrush)
            self._draw_hint(p, "拖动选区移位 · 拖边角调大小 · 框外重新框选 · 双击确认 · Esc 取消")
        else:
            self._draw_hint(p, "松开后可调整 · 双击确认 · Esc 取消")

    @staticmethod
    def _mid(a: int, b: int, fixed: int, vertical: bool):
        """边中点手柄：vertical=True 返回上/下边中点 (mid_x, fixed_y)，否则左/右边中点 (fixed_x, mid_y)。"""
        from PySide6.QtCore import QPoint
        return QPoint((a + b) // 2, fixed) if vertical else QPoint(fixed, (a + b) // 2)

    def _draw_hint(self, p: QPainter, text: str):
        p.setPen(QPen(QColor(255, 255, 255)))
        p.drawText(QRect(0, self.height() - 36, self.width(), 26), Qt.AlignCenter, text)

    # ---------- 鼠标 ----------
    def mousePressEvent(self, ev):
        if self._mode == "point":
            if ev.button() == Qt.LeftButton:
                self._pick_point(ev.position().toPoint())
            return
        if ev.button() != Qt.LeftButton:
            return
        pos = ev.position().toPoint()
        if self._state == "adjusting" and self._sel is not None:
            zone = self._hit_zone(pos)
            if zone == "move":
                self._drag_mode = "move"
                self._move_offset = pos - self._sel.topLeft()
                self.update()
                return
            if zone:
                self._drag_mode = zone
                self.update()
                return
        # 空白处 / 初始态：重新开始拖拽
        self._state = "dragging"
        self._sel = None
        self._origin = pos
        self.update()

    def mouseMoveEvent(self, ev):
        if self._mode == "point":
            self._cursor_pt = ev.position().toPoint()
            self.update()
            return
        pos = ev.position().toPoint()
        if self._state == "dragging" and self._origin is not None:
            self._sel = QRect(self._origin, pos).normalized().intersected(self.rect())
        elif self._state == "adjusting":
            if self._drag_mode == "move" and self._sel is not None:
                r = QRect(pos - self._move_offset, self._sel.size())
                r.moveLeft(max(0, min(r.x(), self.width() - r.width())))
                r.moveTop(max(0, min(r.y(), self.height() - r.height())))
                self._sel = r
            elif self._drag_mode:
                self._resize_to(pos)
            else:
                self._update_cursor(pos)
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._mode == "point":
            return  # point 模式按下即取点，无松开逻辑
        if ev.button() != Qt.LeftButton:
            return
        if self._state == "dragging":
            if self._sel is not None and self._sel.width() >= _MIN_SIZE \
                    and self._sel.height() >= _MIN_SIZE:
                self._state = "adjusting"   # 松开后进入可调整状态
            else:
                self._sel = None
                self._state = "idle"
        elif self._state == "adjusting":
            self._drag_mode = None
        self.update()

    def mouseDoubleClickEvent(self, ev):
        if self._mode == "point":
            return  # point 模式单击即取点
        if ev.button() == Qt.LeftButton and self._sel is not None and \
                self._state in ("dragging", "adjusting"):
            self._confirm()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()

    # ---------- 确认 ----------
    def _pick_point(self, pt):
        """point 模式：把点击位置换算为虚拟桌面物理像素并返回。"""
        sx = self._img.shape[1] / max(self.width(), 1)
        sy = self._img.shape[0] / max(self.height(), 1)
        px = self._phys_origin_x + int(round(pt.x() * sx))
        py = self._phys_origin_y + int(round(pt.y() * sy))
        from .logbus import log
        log(f"已选取坐标 ({px}, {py})")
        self.pointSelected.emit(px, py)
        self.close()

    def _confirm(self):
        sel = self._sel
        if sel is None or sel.width() < 2 or sel.height() < 2:
            return
        if self._mode == "region":
            sx = self._img.shape[1] / max(self.width(), 1)
            sy = self._img.shape[0] / max(self.height(), 1)
            x0 = self._phys_origin_x + int(round(sel.x() * sx))
            y0 = self._phys_origin_y + int(round(sel.y() * sy))
            w = int(round(sel.width() * sx))
            h = int(round(sel.height() * sy))
            from .logbus import log
            log(f"已选择找图区域 {w} x {h}（@{x0},{y0}）")
            self.regionSelected.emit(x0, y0, w, h)
        else:
            try:
                path = self._save_crop(sel)
            except Exception:
                self.cancelled.emit()
                self.close()
                return
            from .logbus import log
            log(f"模板已保存：{os.path.basename(path)}（{sel.width()} x {sel.height()}）")
            self.captured.emit(path)
        self.close()

    def _save_crop(self, sel: QRect) -> str:
        """把窗口逻辑选区映射回物理像素并保存。"""
        sx = self._img.shape[1] / max(self.width(), 1)
        sy = self._img.shape[0] / max(self.height(), 1)
        x0 = int(round(sel.x() * sx))
        y0 = int(round(sel.y() * sy))
        x1 = int(round((sel.x() + sel.width()) * sx))
        y1 = int(round((sel.y() + sel.height()) * sy))
        crop = np.ascontiguousarray(self._img[y0:y1, x0:x1])
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        name = f"tpl_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path = os.path.join(TEMPLATE_DIR, name)
        cv2.imwrite(path, crop)
        return path

    # ---------- 调整辅助 ----------
    def _resize_to(self, pos):
        r = QRect(self._sel)
        z = self._drag_mode
        if "n" in z:
            r.setTop(pos.y())
        if "s" in z:
            r.setBottom(pos.y())
        if "w" in z:
            r.setLeft(pos.x())
        if "e" in z:
            r.setRight(pos.x())
        r = r.normalized()
        if r.width() < _MIN_SIZE or r.height() < _MIN_SIZE:
            return  # 防止拖没
        self._sel = r.intersected(self.rect())

    def _hit_zone(self, pos):
        r = self._sel
        for name, pt in (("nw", r.topLeft()), ("ne", r.topRight()),
                         ("sw", r.bottomLeft()), ("se", r.bottomRight())):
            if (pos - pt).manhattanLength() <= _HANDLE:
                return name
        if abs(pos.x() - r.left()) <= _EDGE and r.top() <= pos.y() <= r.bottom():
            return "w"
        if abs(pos.x() - r.right()) <= _EDGE and r.top() <= pos.y() <= r.bottom():
            return "e"
        if abs(pos.y() - r.top()) <= _EDGE and r.left() <= pos.x() <= r.right():
            return "n"
        if abs(pos.y() - r.bottom()) <= _EDGE and r.left() <= pos.x() <= r.right():
            return "s"
        if r.contains(pos):
            return "move"
        return None

    def _update_cursor(self, pos):
        if self._sel is None:
            self.setCursor(Qt.CrossCursor)
            return
        zone = self._hit_zone(pos)
        cursors = {
            "move": Qt.SizeAllCursor, "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
            "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
            "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
            "w": Qt.SizeHorCursor, "e": Qt.SizeHorCursor,
        }
        self.setCursor(cursors.get(zone, Qt.CrossCursor))


def run_screen_capture(on_saved=None, on_cancelled=None, on_region=None,
                       on_point=None) -> None:
    """在所有屏幕上启动遮罩。

    - on_saved 提供时为模板模式（双击确认后保存 PNG，回调收到路径）
    - on_region 提供时为区域模式（双击确认后回调收到 (x, y, w, h) 物理坐标）
    - on_point 提供时为取点模式（单击即确认，回调收到 (x, y) 物理坐标）
    任一屏幕确认或取消后关闭全部遮罩。
    """
    if on_point is not None:
        mode = "point"
    elif on_region is not None:
        mode = "region"
    else:
        mode = "template"
    state = {"done": False}
    overlays: list[CaptureOverlay] = []

    def close_all():
        for ov in overlays:
            try:
                ov.close()
            except RuntimeError:
                pass

    def _finish(result_cb, *args):
        if state["done"]:
            return
        state["done"] = True
        close_all()
        if result_cb:
            result_cb(*args)

    for screen in QApplication.screens():
        ov = CaptureOverlay(screen, mode=mode)
        ov.captured.connect(lambda p: _finish(on_saved, p))
        ov.regionSelected.connect(lambda x, y, w, h: _finish(on_region, (x, y, w, h)))
        ov.pointSelected.connect(lambda x, y: _finish(on_point, (x, y)))
        ov.cancelled.connect(lambda: _finish(on_cancelled))
        overlays.append(ov)
    for ov in overlays:
        ov.show()
        ov.raise_()
        ov.activateWindow()


# ---------------------------------------------------------------------------
# 窗口识别遮罩
# ---------------------------------------------------------------------------

class WindowPickerOverlay(QWidget):
    """窗口识别遮罩：实时高亮鼠标所指窗口，单击确认句柄。

    与截图遮罩不同，这里**不冻结屏幕**——屏幕保持"活"的，遮罩半透明变暗，
    鼠标指向的窗口矩形被点亮并描边，鼠标旁浮显窗口标题，单击确认，右键或
    Esc 取消。用户移动鼠标时能实时看到「现在指到哪个窗口了」，体验直观。

    关键点：遮罩自身盖在最上层，WindowFromPoint 会命中遮罩自己，所以查询
    前临时给遮罩加 WS_EX_TRANSPARENT 让它穿透，查完恢复（见 win_actors 的
    cursor_window_info）。
    """

    windowPicked = Signal(int, str)   # hwnd, title
    cancelled = Signal()

    def __init__(self, screen, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        geo = screen.geometry()
        self.setGeometry(geo)
        self._phys_origin_x, self._phys_origin_y, self._phys_w, self._phys_h = \
            _physical_geometry(screen)
        self._logic_w = max(geo.width(), 1)
        self._logic_h = max(geo.height(), 1)
        self._self_hwnd = int(self.winId())

        # 当前指向的窗口（缓存，paintEvent 只读）
        self._cur_hwnd = 0
        self._cur_title = ""
        self._cur_cls = ""
        self._cur_rect = None          # 物理矩形 (x, y, w, h)
        self._cursor = (0, 0)          # 物理坐标
        self._in_me = False            # 鼠标是否在本屏

        # 定时刷新：鼠标不动时窗口状态变化也能跟上
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ---------- 坐标换算（物理 -> 遮罩内逻辑） ----------
    def _to_logic(self, px: int, py: int) -> tuple[int, int]:
        x = (px - self._phys_origin_x) * self._logic_w / self._phys_w
        y = (py - self._phys_origin_y) * self._logic_h / self._phys_h
        return int(round(x)), int(round(y))

    def _rect_to_logic(self, r) -> QRect:
        x, y, w, h = r
        lx, ly = self._to_logic(x, y)
        lw = int(round(w * self._logic_w / self._phys_w))
        lh = int(round(h * self._logic_h / self._phys_h))
        return QRect(lx, ly, lw, lh)

    # ---------- 刷新 ----------
    def _refresh(self):
        from . import win_actors
        x, y = win_actors.cursor_pos()
        self._cursor = (x, y)
        self._in_me = (self._phys_origin_x <= x < self._phys_origin_x + self._phys_w and
                       self._phys_origin_y <= y < self._phys_origin_y + self._phys_h)
        if self._in_me:
            hwnd, title, rect = win_actors.cursor_window_info(skip_hwnd=self._self_hwnd)
            self._cur_hwnd, self._cur_title, self._cur_rect = hwnd, title, rect
            self._cur_cls = win_actors.window_class(hwnd)
        else:
            self._cur_hwnd, self._cur_title, self._cur_rect = 0, "", None
            self._cur_cls = ""
        self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))

        if self._in_me and self._cur_hwnd and self._cur_rect:
            lr = self._rect_to_logic(self._cur_rect).intersected(self.rect())
            # 点亮窗口（擦除遮罩，露出真实窗口）
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(lr, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            # 描边 + 四角角标
            p.setPen(QPen(QColor(0, 200, 255), 2))
            p.drawRect(lr)
            self._draw_corners(p, lr)
            self._draw_label(p, lr)

        # 十字线 + 中心点
        if self._in_me:
            cx, cy = self._to_logic(self._cursor[0], self._cursor[1])
            p.setPen(QPen(QColor(0, 200, 255, 180), 1))
            p.drawLine(0, cy, self.width(), cy)
            p.drawLine(cx, 0, cx, self.height())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 200, 255))
            p.drawEllipse(QPoint(cx, cy), 4, 4)

        self._draw_hint(p)

    def _draw_corners(self, p: QPainter, lr: QRect):
        L = 18
        pen = QPen(QColor(0, 200, 255), 4)
        p.setPen(pen)
        # 左上
        p.drawLine(lr.left(), lr.top() + L, lr.left(), lr.top())
        p.drawLine(lr.left(), lr.top(), lr.left() + L, lr.top())
        # 右上
        p.drawLine(lr.right() - L, lr.top(), lr.right(), lr.top())
        p.drawLine(lr.right(), lr.top(), lr.right(), lr.top() + L)
        # 左下
        p.drawLine(lr.left(), lr.bottom() - L, lr.left(), lr.bottom())
        p.drawLine(lr.left(), lr.bottom(), lr.left() + L, lr.bottom())
        # 右下
        p.drawLine(lr.right() - L, lr.bottom(), lr.right(), lr.bottom())
        p.drawLine(lr.right(), lr.bottom(), lr.right(), lr.bottom() - L)

    def _draw_label(self, p: QPainter, lr: QRect):
        title = self._cur_title or self._cur_cls or "无标题窗口"
        text = f"{title}  ·  0x{self._cur_hwnd:X}"
        fm = p.fontMetrics()
        max_w = max(lr.width(), 180)
        elided = fm.elidedText(text, Qt.ElideMiddle, max_w - 24)
        tw = fm.horizontalAdvance(elided) + 20
        th = fm.height() + 10
        x = lr.left()
        y = lr.top() - th - 6
        if y < 2:
            y = lr.top() + 6
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 205))
        p.drawRoundedRect(QRect(x, y, tw, th), 6, 6)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(x + 10, y, tw - 20, th), Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _draw_hint(self, p: QPainter):
        if self._in_me and self._cur_hwnd:
            text = "移动鼠标选择窗口 · 单击确认 · 右键或 Esc 取消"
        else:
            text = "移动鼠标指向目标窗口 · 右键或 Esc 取消"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 180))
        bar = QRect(0, self.height() - 44, self.width(), 44)
        p.drawRect(bar)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(0, self.height() - 44, self.width(), 44), Qt.AlignCenter, text)

    # ---------- 交互 ----------
    def mouseMoveEvent(self, ev):
        self._refresh()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._confirm()
        elif ev.button() == Qt.RightButton:
            self._cancel()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self._cancel()

    def _confirm(self):
        self._refresh()
        if self._cur_hwnd:
            from .logbus import log
            name = self._cur_title or self._cur_cls or "无标题窗口"
            log(f"已识别窗口：{name}（0x{self._cur_hwnd:X}）")
            self.windowPicked.emit(self._cur_hwnd, self._cur_title)
        else:
            self.cancelled.emit()
        self.close()

    def _cancel(self):
        self.cancelled.emit()
        self.close()


def run_window_picker(on_picked, on_cancelled) -> None:
    """在所有屏幕上启动窗口识别遮罩。

    on_picked(hwnd, title)：单击确认目标窗口后回调。
    on_cancelled()：右键 / Esc 取消后回调。任一屏结束即关闭全部遮罩。
    """
    state = {"done": False}
    overlays: list[WindowPickerOverlay] = []

    def close_all():
        for ov in overlays:
            try:
                ov.close()
            except RuntimeError:
                pass

    def _finish(cb, *args):
        if state["done"]:
            return
        state["done"] = True
        close_all()
        if cb:
            cb(*args)

    for screen in QApplication.screens():
        ov = WindowPickerOverlay(screen)
        ov.windowPicked.connect(lambda hwnd, title: _finish(on_picked, hwnd, title))
        ov.cancelled.connect(lambda: _finish(on_cancelled))
        overlays.append(ov)
    for ov in overlays:
        ov.show()
        ov.raise_()
        ov.activateWindow()
        ov.setFocus()


# ---------------------------------------------------------------------------
# 屏幕取色遮罩
# ---------------------------------------------------------------------------

_COLOR_GRID = 15        # 放大像素网格边长（奇数：中心像素 = 鼠标所指）
_COLOR_CELL = 14        # 每个取色像素放大后的边长（逻辑像素）
_COLOR_ZOOM = _COLOR_GRID * _COLOR_CELL
_MAG_GAP = 18           # 放大镜与光标的间距（逻辑像素），避免遮挡取色目标


class ColorPickerOverlay(QWidget):
    """屏幕取色遮罩：鼠标旁实时放大屏幕像素，单击取中心颜色。

    与窗口识别遮罩一致走「不冻结屏幕」路线：遮罩窗口全透明（WA_TranslucentBackground），
    用 QTimer 定时抓取鼠标周围一小块物理像素（mss 复用单实例），放大绘制在光标附近
    （右上优先、靠边自动翻转，不遮挡目标像素）。透明背景保证抓屏结果是真实屏幕内容——
    遮罩不掺色，取色才准。

    单击确认返回鼠标所指物理像素的 RGB；右键 / Esc 取消。
    """

    colorPicked = Signal(int, int, int)   # r, g, b
    cancelled = Signal()

    def __init__(self, screen, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        geo = screen.geometry()
        self.setGeometry(geo)
        self._phys_origin_x, self._phys_origin_y, self._phys_w, self._phys_h = \
            _physical_geometry(screen)
        self._logic_w = max(geo.width(), 1)
        self._logic_h = max(geo.height(), 1)
        self._half = (_COLOR_GRID - 1) // 2      # 取色区域中心到边缘的像素数

        self._cursor = (0, 0)      # 当前鼠标物理坐标
        self._in_me = False        # 鼠标是否在本屏
        self._bgr = None           # 最新抓到的取色区 (grid, grid, 3) BGR
        self._rgb = (0, 0, 0)      # 中心像素颜色（同步跟随 _bgr）
        self._box_top = (0, 0)     # 放大板左上逻辑坐标（上一帧计算，供信息条跟随）
        self._mss = None           # mss 抓屏实例（惰性创建 + 复用，避免每帧枚举显示器）

        self._timer = QTimer(self)
        self._timer.setInterval(30)          # 约 33fps，抓 15x15 像素开销可忽略
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ---------- 坐标换算 ----------
    def _to_logic(self, px: int, py: int) -> tuple[int, int]:
        x = (px - self._phys_origin_x) * self._logic_w / self._phys_w
        y = (py - self._phys_origin_y) * self._logic_h / self._phys_h
        return int(round(x)), int(round(y))

    # ---------- 刷新与抓屏 ----------
    def _track(self):
        """只更新光标位置与「是否在本屏」，不做抓屏（廉价，供鼠标移动即时刷新）。"""
        from . import win_actors
        x, y = win_actors.cursor_pos()
        self._cursor = (x, y)
        self._in_me = (self._phys_origin_x <= x < self._phys_origin_x + self._phys_w and
                       self._phys_origin_y <= y < self._phys_origin_y + self._phys_h)

    def _refresh(self):
        """定时器驱动的完整刷新：更新光标 -> 抓取放大区 -> 重绘。"""
        self._track()
        if not self._in_me:
            self._bgr = None
            self.update()
            return
        try:
            self._bgr = self._grab_around(*self._cursor)
        except Exception:
            self._bgr = None
        if self._bgr is not None:
            b, g, r = (int(v) for v in self._bgr[self._half, self._half])
            self._rgb = (r, g, b)
        self.update()

    def _get_mss(self):
        """复用单个 mss 实例：mss.mss() 每次创建都会重新枚举显示器，开销不小。"""
        import mss
        if self._mss is None:
            self._mss = mss.mss()
        return self._mss

    def _grab_around(self, px: int, py: int):
        """抓取光标周围 grid x grid 物理像素；越出本屏的部分补黑。

        抓取矩形先钳制到本屏物理范围内，再把结果按正确偏移放回 canvas，
        保证中心像素（index=half）恒等于光标所在像素——即使光标贴近屏幕边缘，
        也不会因为 mss 返回缩水图而整体错位（旧实现按「居中补黑」会错位）。
        """
        half = self._half
        left = px - half
        top = py - half
        x0 = max(left, self._phys_origin_x)
        y0 = max(top, self._phys_origin_y)
        x1 = min(left + _COLOR_GRID, self._phys_origin_x + self._phys_w)
        y1 = min(top + _COLOR_GRID, self._phys_origin_y + self._phys_h)
        canvas = np.zeros((_COLOR_GRID, _COLOR_GRID, 3), dtype=np.uint8)
        if x0 < x1 and y0 < y1:
            shot = np.asarray(self._get_mss().grab({
                "left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0,
            }))
            img = np.ascontiguousarray(shot[:, :, :3])
            dx = x0 - left
            dy = y0 - top
            canvas[dy:dy + img.shape[0], dx:dx + img.shape[1]] = img
        return canvas

    def _place_box(self, cx: int, cy: int) -> tuple[int, int]:
        """确定放大板左上逻辑坐标：默认放光标右上方（不遮挡取色目标），
        上方/右侧放不下时自动翻转到其它象限，尽量保持完整可见。"""
        zw = zh = _COLOR_ZOOM
        w, h = self.width(), self.height()
        left = cx + _MAG_GAP
        top = cy - zh - _MAG_GAP
        if top < 4:                       # 上方放不下 -> 放下方
            top = cy + _MAG_GAP
        if left + zw > w - 4:             # 右侧放不下 -> 放左侧
            left = cx - zw - _MAG_GAP
        left = max(4, min(left, w - zw - 4))
        top = max(4, min(top, h - zh - 4))
        return left, top

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        # 全屏铺一层几乎透明的底色（alpha=1）：让透明遮罩整窗都能命中鼠标/键盘。
        # 否则纯透明区（alpha=0）会被 Windows 做 hit-test 穿透，导致十字光标不显示、
        # 单击取色 / Esc / 方向键全部失效。alpha=1 肉眼不可见，最终取色在 hide 后抓屏不受影响。
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        if self._in_me and self._bgr is not None:
            cx, cy = self._to_logic(*self._cursor)
            left, top = self._place_box(cx, cy)
            self._box_top = (left, top)
            # 逐像素放大色块
            for row in range(_COLOR_GRID):
                for col in range(_COLOR_GRID):
                    b, g, r = (int(v) for v in self._bgr[row, col])
                    p.fillRect(QRect(left + col * _COLOR_CELL,
                                     top + row * _COLOR_CELL,
                                     _COLOR_CELL, _COLOR_CELL),
                               QColor(r, g, b))
            # 网格细线（淡色，辅助数像素）
            p.setPen(QPen(QColor(0, 0, 0, 36), 1))
            for i in range(_COLOR_GRID + 1):
                x = left + i * _COLOR_CELL
                p.drawLine(x, top, x, top + _COLOR_ZOOM)
            for i in range(_COLOR_GRID + 1):
                y = top + i * _COLOR_CELL
                p.drawLine(left, y, left + _COLOR_ZOOM, y)
            # 中心像素（= 光标所指，即实际取色点）用高亮框标出
            ccx0 = left + self._half * _COLOR_CELL
            ccy0 = top + self._half * _COLOR_CELL
            p.setPen(QPen(QColor(255, 255, 255, 235), 1))
            p.drawRect(ccx0 - 1, ccy0 - 1, _COLOR_CELL + 2, _COLOR_CELL + 2)
            # 放大板外框
            p.setPen(QPen(QColor(255, 255, 255, 220), 1))
            p.drawRect(left - 1, top - 1, _COLOR_ZOOM + 2, _COLOR_ZOOM + 2)
            # 十字准线（贯穿整块，交点即中心像素中心）
            ccx = ccx0 + _COLOR_CELL // 2
            ccy = ccy0 + _COLOR_CELL // 2
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawLine(left, ccy, left + _COLOR_ZOOM, ccy)
            p.drawLine(ccx, top, ccx, top + _COLOR_ZOOM)
            self._draw_color_bar(p)
        self._draw_hint(p)

    def _draw_color_bar(self, p: QPainter):
        """放大板正下方的颜色信息条：色块 + HEX + RGB。"""
        r, g, b = self._rgb
        left, top = self._box_top
        bar_w = _COLOR_ZOOM + 2
        bar_h = 30
        by = top + _COLOR_ZOOM + 8
        if by + bar_h > self.height() - 46:      # 底部没位置就放到放大板上方
            by = top - bar_h - 8
        hex_s = f"#{r:02X}{g:02X}{b:02X}"
        rgb_s = f"RGB({r}, {g}, {b})"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 200))
        p.drawRoundedRect(QRect(left, by, bar_w, bar_h), 6, 6)
        p.drawRect(QRect(left + 6, by + 5, 20, 20))     # 色块底色先铺黑
        p.fillRect(QRect(left + 6, by + 5, 20, 20), QColor(r, g, b))
        p.setPen(QPen(QColor(255, 255, 255, 160), 1))
        p.drawRect(left + 6, by + 5, 20, 20)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(left + 34, by, bar_w - 40, bar_h),
                   Qt.AlignVCenter | Qt.AlignLeft, f"{hex_s}    {rgb_s}")

    def _draw_hint(self, p: QPainter):
        text = ("移动鼠标或方向键（Shift=10px）微调取色点 · 单击或回车确认 · 右键或 Esc 取消"
                if self._in_me else "移动鼠标到目标屏幕取色 · 右键或 Esc 取消")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 180))
        p.drawRect(0, self.height() - 44, self.width(), 44)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(0, self.height() - 44, self.width(), 44), Qt.AlignCenter, text)

    # ---------- 交互 ----------
    def mouseMoveEvent(self, ev):
        # 只做廉价的光标跟踪 + 重绘（用上一帧抓图），真正的抓屏交给 30ms 定时器，
        # 避免每次鼠标移动都同步抓屏导致放大镜卡顿/滞后。
        self._track()
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._confirm_pick()
        elif ev.button() == Qt.RightButton:
            self._cancel()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self._cancel()
            return
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm_pick()               # 回车 / 小键盘回车 = 确认取色
            return
        # 方向键微调取色点（1 物理像素）；按住 Shift 大步进 10 像素
        step = 10 if (ev.modifiers() & Qt.ShiftModifier) else 1
        if ev.key() == Qt.Key_Left:
            self._nudge(-step, 0)
        elif ev.key() == Qt.Key_Right:
            self._nudge(step, 0)
        elif ev.key() == Qt.Key_Up:
            self._nudge(0, -step)
        elif ev.key() == Qt.Key_Down:
            self._nudge(0, step)
        else:
            super().keyPressEvent(ev)

    def _nudge(self, dx: int, dy: int) -> None:
        """把取色点（系统光标）移动 ±N 物理像素并同步刷新放大镜。"""
        from . import win_actors
        x, y = self._cursor
        win_actors.set_cursor_pos(x + dx, y + dy)
        self._refresh()      # 内部 _track 读回真实光标（系统可能钳制）+ 抓屏 + 重绘

    def _confirm_pick(self):
        if not self._in_me:
            return
        # 隐藏遮罩再抓屏：确保取到真实屏幕像素，而非遮罩自身绘制的内容
        # （此前取色偶发取到辅助线，正是因为遮罩画的东西被一起抓进了截图）。
        x, y = self._cursor
        self.hide()
        try:
            bgr = self._grab_around(x, y)
        except Exception:
            bgr = None
        finally:
            self.show()
        if bgr is None:
            return
        b, g, r = (int(v) for v in bgr[self._half, self._half])
        from .logbus import log
        log(f"已取色 #{r:02X}{g:02X}{b:02X} (RGB {r}, {g}, {b})")
        self.colorPicked.emit(r, g, b)
        self.close()

    def _cancel(self):
        self.cancelled.emit()
        self.close()


def run_color_picker(on_picked, on_cancelled) -> None:
    """在所有屏幕上启动屏幕取色遮罩。

    on_picked(r, g, b)：单击确认后回调颜色（0~255）。
    on_cancelled()：右键 / Esc 取消后回调。任一屏确认即关闭全部遮罩。
    """
    state = {"done": False}
    overlays: list[ColorPickerOverlay] = []
    # 全局覆盖十字光标：不依赖遮罩窗口的鼠标命中（透明区穿透也能保持十字），
    # 取色结束后在 _finish 里恢复。
    QApplication.setOverrideCursor(Qt.CrossCursor)

    def close_all():
        for ov in overlays:
            try:
                ov.close()
            except RuntimeError:
                pass

    def _finish(cb, *args):
        if state["done"]:
            return
        state["done"] = True
        QApplication.restoreOverrideCursor()
        close_all()
        if cb:
            cb(*args)

    for screen in QApplication.screens():
        ov = ColorPickerOverlay(screen)
        ov.colorPicked.connect(lambda r, g, b: _finish(on_picked, r, g, b))
        ov.cancelled.connect(lambda: _finish(on_cancelled))
        overlays.append(ov)
    for ov in overlays:
        ov.show()
        ov.raise_()
        ov.activateWindow()
        ov.setFocus(Qt.ActiveWindowFocusReason)   # 主动请求键盘焦点，保证 Esc/方向键生效
