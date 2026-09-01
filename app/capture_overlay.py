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
