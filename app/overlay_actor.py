"""桌面悬浮图片：把指定图片贴在最前端，用户手动关闭。

与流程里其它「执行完就结束」的步骤不同，这个模块是**异步**的：
步骤执行时只在主线程创建并显示悬浮窗，随即返回成功，**不等用户关闭**，
后续步骤照常往下跑。悬浮窗一直留在桌面上，直到用户手动关掉它。

关闭方式（都可用）：右上角 ✕ 按钮、Esc、以及可选的「单击图片即关闭」。

显示尺寸：默认按**原图尺寸**（100%）显示；只有比屏幕还大时才会收敛到能完整显示，
免得窗口溢出屏幕连关闭按钮都够不着。运行时可以手动缩放：
滚轮上/下 = 放大/缩小（每次 10 个百分点），+ / - 快捷键同理，按 0 回到原图尺寸，
范围 10%~400%，缩放以**光标所指的位置**为锚点（光标下的内容不会跑掉），
左上角会短暂浮出当前百分比。窗口被缩到比屏幕还大时，会优先保证 ✕ 仍留在屏内。

清晰度：Windows 显示缩放 125%/150% 时（屏的 devicePixelRatio > 1），若还给
QPixmap 留默认的 dpr=1，Qt 会认定「这是给 100% 屏用的图」而把它放大着绘制，
每个图片像素被插值成 1.5 个 → 发糊。所以渲染前一律给图打上真实 dpr，并按
deviceIndependentSize（= 图片像素 / dpr）折算窗口的逻辑尺寸，这样 100% 时
1 个图片像素正好落在 1 个屏幕物理像素上，1:1 清晰。详见 `_render_pixmap`。

因为窗口生命周期跨越了「步骤执行」这个瞬间，所以必须：
- 用模块级 `_live` 持有强引用（否则 Python GC 会把窗口收走，窗口一闪就没）；
- 程序退出时 `close_all()` 统一销毁（见 MainWindow.shutdown）；
- 窗口自己关闭时反注册（见 `_forget`）。

线程约定：`show_image/close_image/close_all` 都只能在**主线程**调用——
QPixmap 和 QWidget 都不能跨线程碰。流程在后台线程里跑，所以要经
`screenshot_actor.ui_call` 调度过来。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import (QApplication, QLabel, QPushButton, QWidget)

# 预设角落位置距屏幕可用边缘的留白
EDGE_MARGIN = 24
_CLOSE_SIZE = 22
# 拖拽超过这个距离就认为用户在「移动窗口」而不是「单击」
_DRAG_THRESHOLD = 4

# ---- 运行时手动缩放 ----
DEFAULT_SCALE = 100       # 默认 100% = 原图尺寸（不放大也不缩小）
MIN_SCALE = 10            # 最小 10%，再小就看不清了
MAX_SCALE = 400           # 最大 400%
ZOOM_STEP = 10            # 滚轮每格 / 快捷键每次的缩放步长（百分点）
# 步长用「固定百分点」而不是「乘 1.1」：等比步长在 100% 往下滚会算出 90.9 → 91%，
# 百分比跳得没有规律；固定 10 点则永远是 100/110/120…、90/80/70… 这样的整数梯子。
_BADGE_HIDE_MS = 1200     # 缩放百分比浮标自动隐藏时间（毫秒）

POSITIONS = (
    ("right_bottom", "右下角"),
    ("right_top", "右上角"),
    ("left_top", "左上角"),
    ("left_bottom", "左下角"),
    ("center", "屏幕居中"),
    ("custom", "自定义坐标"),
)
_DEFAULT_POS = "right_bottom"

# 图片路径 -> 窗口。必须持强引用：窗口没有父对象，
# 只靠 Qt 自己是留不住 Python 侧的引用的。
_live: dict[str, FloatingImage] = {}


class FloatingImage(QWidget):
    """无边框、置顶、可拖动的图片悬浮窗。"""

    def __init__(self, pixmap: QPixmap, *, scale_pct: int = DEFAULT_SCALE,
                 click_to_close: bool = False):
        super().__init__(None, Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("floatImage")
        self.setWindowTitle("图片悬浮")
        self.setToolTip("滚轮上/下 = 放大/缩小 · 按 0 还原原图尺寸 · Esc 关闭 · 按住左键可拖动")
        # 父级只给「画布」这一种 QLabel 定样式，别用 `QLabel` 通配——
        # 那样会把缩放百分比浮标也一起刷成透明底（选择器优先级还压不过它）。
        self.setStyleSheet(
            "QWidget#floatImage { background: #ffffff; border: 1px solid #c9d1d9; }"
            "QWidget#floatImage QLabel#floatImageCanvas { border: none; background: transparent; }")
        self._click_to_close = bool(click_to_close)
        self._press_pos: QPoint | None = None
        self._moved = False
        # 缩放永远基于**原图**重算，而不是在上一次结果上再缩——
        # 否则反复放大缩小时会把图糊掉。
        self._src = pixmap
        # 本屏的像素比：显示缩放 150% 时是 1.5。渲染时要把这个倍率交给 Qt，
        # 并把窗口尺寸按它折算回逻辑像素，图片才会 1:1 清晰（详见 _screen_dpr）。
        self._dpr = _screen_dpr()
        self._scale_pct = _clamp_scale(scale_pct)

        self.image_label = QLabel(self)
        self.image_label.setObjectName("floatImageCanvas")
        # 让鼠标事件穿透到窗口本身，拖动/单击关闭/滚轮缩放才不会被图片吃掉
        self.image_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(_CLOSE_SIZE, _CLOSE_SIZE)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("关闭悬浮图片")
        self.close_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 11px;"
            " background: rgba(255,255,255,0.82); color: #24292f; font-size: 13px; }"
            "QPushButton:hover { background: #1668a8; color: #ffffff; }")
        self.close_btn.clicked.connect(self.close)

        # 缩放百分比浮标：缩放时短暂浮现，让用户知道现在是原图尺寸还是被放大了
        self.zoom_badge = QLabel(self)
        self.zoom_badge.setObjectName("floatZoomBadge")
        self.zoom_badge.setStyleSheet(
            "QLabel#floatZoomBadge { background: rgba(36,41,47,0.80); color: #ffffff;"
            " border: none; border-radius: 9px; padding: 2px 8px; font-size: 12px; }")
        self.zoom_badge.hide()
        self._badge_timer = QTimer(self)
        self._badge_timer.setSingleShot(True)
        self._badge_timer.timeout.connect(self.zoom_badge.hide)

        self._apply_zoom(self._scale_pct, show_badge=False)
        self.close_btn.hide()        # 默认不显示，免得挡住图片内容；鼠标移入再出来

    # ---------- 缩放 ----------
    @property
    def scale_pct(self) -> int:
        """当前显示缩放百分比（100 = 原图尺寸）。"""
        return self._scale_pct

    def _apply_zoom(self, pct, *, anchor: QPoint | None = None,
                    show_badge: bool = True) -> None:
        """按百分比重新渲染图片。

        anchor 是全局坐标（滚轮所在处）：缩放时让**光标底下的那块内容原地不动**，
        比「固定左上角」自然得多；拖动滚动条式的观感不会「跑图」。
        """
        old = self._scale_pct
        pct = _clamp_scale(pct)
        self._scale_pct = pct

        pix = _render_pixmap(self._src, pct, self._dpr)
        shown = pix.deviceIndependentSize().toSize()
        self.image_label.setPixmap(pix)
        self.image_label.setFixedSize(shown)
        self.resize(shown)

        if anchor is not None and old > 0 and old != pct:
            rel_x = (anchor.x() - self.x()) / old
            rel_y = (anchor.y() - self.y()) / old
            self.move(int(anchor.x() - rel_x * pct),
                      int(anchor.y() - rel_y * pct))

        self._place_close_btn()
        self._place_badge()
        self._keep_close_reachable()
        if show_badge:
            self.zoom_badge.setText(f"{pct}%")
            self.zoom_badge.show()
            self.zoom_badge.raise_()
            self._badge_timer.start(_BADGE_HIDE_MS)

    def _place_close_btn(self) -> None:
        self.close_btn.move(self.width() - _CLOSE_SIZE - 4, 4)
        self.close_btn.raise_()

    def _place_badge(self) -> None:
        self.zoom_badge.adjustSize()
        self.zoom_badge.move(4, 4)   # 左上角：让开右上角的 ✕，且缩到多大都在屏内
        if self.zoom_badge.isVisible():
            self.zoom_badge.raise_()

    def _keep_close_reachable(self) -> None:
        """窗口比屏幕还大时，也要把右上角的 ✕ 留在屏内。

        放大到超过屏幕后，若还任由左上角固定，✕ 会被挤到屏幕外，
        用户就只能靠 Esc 关了。这里宁可让左上角露出去，也要保住那颗按钮。
        """
        area = _available_area()
        if area is None:
            return
        # ✕ 占 [x+w-_CLOSE_SIZE-4, x+w-4]，要整颗落在屏内：x <= right - w + 5
        x = min(self.x(), area.right() - self.width() + 5)
        y = min(max(self.y(), area.top() - 4),
                area.bottom() - _CLOSE_SIZE - 4)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def wheelEvent(self, ev):         # noqa: N802 (Qt 命名)
        delta = ev.angleDelta().y()
        if not delta:
            ev.ignore()
            return
        step = ZOOM_STEP if delta > 0 else -ZOOM_STEP
        self._apply_zoom(self._scale_pct + step,
                         anchor=ev.globalPosition().toPoint())
        ev.accept()

    # ---------- 悬停显示关闭按钮 ----------
    def enterEvent(self, ev):    # noqa: N802 (Qt 命名)
        self.close_btn.show()
        self.close_btn.raise_()
        super().enterEvent(ev)

    def leaveEvent(self, ev):    # noqa: N802 (Qt 命名)
        # 鼠标移到子控件（关闭按钮）上时，父窗口同样会收到 Leave，
        # 所以要用「光标是否仍在窗口矩形内」判断，否则按钮会一闪一闪。
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.close_btn.hide()
        super().leaveEvent(ev)

    # ---------- 拖动 / 单击关闭 ----------
    def mousePressEvent(self, ev):    # noqa: N802 (Qt 命名)
        if ev.button() == Qt.LeftButton:
            self._press_pos = (ev.globalPosition().toPoint()
                               - self.frameGeometry().topLeft())
            self._moved = False
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):     # noqa: N802 (Qt 命名)
        if self._press_pos is not None and (ev.buttons() & Qt.LeftButton):
            new_pos = ev.globalPosition().toPoint() - self._press_pos
            if (new_pos - self.pos()).manhattanLength() > _DRAG_THRESHOLD:
                self._moved = True
            self.move(new_pos)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):  # noqa: N802 (Qt 命名)
        if ev.button() == Qt.LeftButton:
            if self._click_to_close and not self._moved:
                self.close()
            elif self._moved:
                self._keep_close_reachable()   # 别把 ✕ 拖到屏幕外
            self._press_pos = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):      # noqa: N802 (Qt 命名)
        key = ev.key()
        if key == Qt.Key_Escape:
            self.close()
            return
        if key in (Qt.Key_Plus, Qt.Key_Equal):        # 「+」与「=」同键位，不按 Shift 也能放大
            self._apply_zoom(self._scale_pct + ZOOM_STEP)
            return
        if key in (Qt.Key_Minus, Qt.Key_Underscore):
            self._apply_zoom(self._scale_pct - ZOOM_STEP)
            return
        if key == Qt.Key_0:                           # 一键回到原图尺寸
            self._apply_zoom(DEFAULT_SCALE)
            return
        super().keyPressEvent(ev)


# ---------------------------------------------------------------------------
# 缩放与定位
# ---------------------------------------------------------------------------

def _clamp_scale(pct) -> int:
    """把缩放百分比收进 [MIN_SCALE, MAX_SCALE]；非法值退回原图尺寸。"""
    try:
        value = int(round(float(pct)))
    except (TypeError, ValueError):
        value = DEFAULT_SCALE
    return max(MIN_SCALE, min(MAX_SCALE, value))


def _apply_scale(pixmap: QPixmap, scale) -> QPixmap:
    pct = _clamp_scale(scale)
    if pct == DEFAULT_SCALE:
        return pixmap
    return pixmap.scaled(max(1, pixmap.width() * pct // 100),
                         max(1, pixmap.height() * pct // 100),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _render_pixmap(pixmap: QPixmap, pct, dpr: float) -> QPixmap:
    """按 pct% 缩放并打上屏幕像素比，返回**可直接交给 QLabel 绘制**的图。

    打 dpr 是关键：QPixmap 默认 devicePixelRatio=1，Qt 会把它当成「100% 屏专用
    图」，在 150% 的屏上自动拉伸 1.5 倍去画——每个图片像素被插值成 1.5 个，于是
    发糊。打上真实 dpr 后，Qt 改用 deviceIndependentSize（= 图片像素 / dpr）作为
    逻辑尺寸原样绘制，图片像素与屏幕物理像素 1:1，清晰度不再打折。

    必须先 `.copy()` 再改：`_apply_scale` 在 100% 时会原样返回入参，直接调
    `setDevicePixelRatio` 会污染调用方的图（`_src`），之后再缩放就全乱了。
    """
    out = _apply_scale(pixmap, pct).copy()
    out.setDevicePixelRatio(dpr)
    return out


def _available_area():
    screen = QApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def _screen_dpr() -> float:
    """主屏的像素比（Windows 显示缩放 150% → 1.5）。

    为什么到处都要它：Windows 上把显示缩放调到 125%/150% 时，Qt 的逻辑坐标
    和物理像素不再是 1:1。一张 QPixmap 默认 devicePixelRatio=1，Qt 会认为
    「这是给 100% 屏用的图」，于是**把它放大 1.5 倍去画**——图上每个像素被拉成
    1.5 个，看起来就是糊的。要清晰就得：
      ① 给 pixmap 打上真实 dpr（告诉 Qt 这张图本来就是给该倍率屏用的）；
      ② 窗口尺寸按「图片像素 / dpr」折算成逻辑尺寸。
    这样 100% 时正好 1 个图片像素 = 1 个屏幕物理像素，1:1 不打折扣。
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return 1.0
    try:
        dpr = float(screen.devicePixelRatio())
    except (TypeError, ValueError):
        dpr = 1.0
    return dpr if dpr > 0 else 1.0


def _display_size(pixmap: QPixmap, pct, dpr: float) -> QSize:
    """pct% 时窗口要占的**逻辑**尺寸（= 图片设备像素 × pct / dpr）。

    屏上一切几何比较（是否超出屏幕、角落定位）都在逻辑坐标里，所以必须先折算。
    """
    try:
        scale = float(pct)
    except (TypeError, ValueError):
        scale = float(DEFAULT_SCALE)
    return QSize(max(1, round(pixmap.width() * scale / 100 / dpr)),
                 max(1, round(pixmap.height() * scale / 100 / dpr)))


def _fit_to_screen(pixmap: QPixmap) -> QPixmap:
    """图片按原图尺寸显示时若比可用屏幕还大，等比缩小。

    否则窗口会超出屏幕边界，连右上角的关闭按钮都点不到，用户就关不掉了。
    比较用的是**折算后的逻辑尺寸**——在 150% 缩放的屏上，2000 像素宽的图只占
    1333 逻辑像素，拿 2000 直接跟屏幕宽比会误判成「必须缩小」。
    """
    area = _available_area()
    if area is None:
        return pixmap
    max_w = max(1, area.width() - EDGE_MARGIN * 2)
    max_h = max(1, area.height() - EDGE_MARGIN * 2)
    shown = _display_size(pixmap, DEFAULT_SCALE, _screen_dpr())
    if shown.width() <= max_w and shown.height() <= max_h:
        return pixmap
    ratio = min(max_w / shown.width(), max_h / shown.height())
    return pixmap.scaled(max(1, int(pixmap.width() * ratio)),
                         max(1, int(pixmap.height() * ratio)),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _initial_pct(pixmap: QPixmap, requested) -> int:
    """算初始缩放比例：默认原图尺寸（100%），只有比屏幕还大时才收敛到能完整显示。

    收敛是必要的——否则窗口一上来就溢出屏幕，用户连 ✕ 都够不着。
    它只是**初始值**：显示之后用户还能用滚轮/快捷键手动放大回去。
    """
    pct = _clamp_scale(requested)
    scaled = _apply_scale(pixmap, pct)
    fitted = _fit_to_screen(scaled)
    if fitted.size() == scaled.size() or scaled.width() <= 0:
        return pct
    # 乘个 0.98 留余量，免得取整后又刚好超出一两个像素
    return max(MIN_SCALE, int(pct * fitted.width() / scaled.width() * 0.98))


def _pos_for(win: QWidget, pos: str, x, y) -> tuple[int, int]:
    """算出窗口左上角坐标。多显示器时按主屏的可用区域对齐。"""
    if pos == "custom" and x is not None and y is not None:
        try:
            return int(x), int(y)
        except (TypeError, ValueError):
            pass
    area = _available_area()
    if area is None:
        return 80, 80
    w, h, m = win.width(), win.height(), EDGE_MARGIN
    if pos == "right_top":
        return area.right() - w - m + 1, area.top() + m
    if pos == "left_top":
        return area.left() + m, area.top() + m
    if pos == "left_bottom":
        return area.left() + m, area.bottom() - h - m + 1
    if pos == "center":
        return area.center().x() - w // 2, area.center().y() - h // 2
    return area.right() - w - m + 1, area.bottom() - h - m + 1


# ---------------------------------------------------------------------------
# 对外接口（主线程）
# ---------------------------------------------------------------------------

def show_image(path: str, *, pos: str = _DEFAULT_POS, x=None, y=None,
               scale: int = DEFAULT_SCALE, click_to_close: bool = False):
    """创建并显示悬浮图片，**立即返回**（不阻塞等待用户关闭）。

    图片默认按**原图尺寸**（scale=100）显示；比屏幕还大时会收敛到能完整显示，
    免得连关闭按钮都够不着。显示后用户还能用滚轮 / +、- / 0 手动缩放。

    同一个图片文件已经有悬浮窗时，先关掉旧的再显示新的——流程重复运行
    不会在桌面上叠出一堆一模一样的窗口。

    返回窗口对象；图片加载失败返回 None。
    """
    if not path:
        return None
    path = os.path.abspath(path)
    pixmap = QPixmap(path)
    if pixmap.isNull():
        return None

    close_image(path)
    win = FloatingImage(pixmap, scale_pct=_initial_pct(pixmap, scale),
                        click_to_close=click_to_close)
    win.destroyed.connect(lambda *_a, p=path, w=win: _forget(p, w))
    _live[path] = win
    win.move(*_pos_for(win, str(pos or _DEFAULT_POS), x, y))
    win.show()
    win.raise_()
    return win


def _forget(path: str, win) -> None:
    # 只有当映射里存的还是这个窗口时才删——否则会把「同名的后来者」误删
    if _live.get(path) is win:
        _live.pop(path, None)


def close_image(path: str) -> None:
    """关闭某个图片的悬浮窗（没有就什么都不做）。"""
    key = os.path.abspath(path) if path else ""
    win = _live.get(key)
    if win is None:
        return
    _live.pop(key, None)
    try:
        win.close()
    except RuntimeError:
        pass


def live_count() -> int:
    """当前还开着的悬浮窗数量（测试与状态栏用）。"""
    return len(_live)


def close_all() -> None:
    """关闭全部悬浮窗（程序退出时调用）。"""
    for win in list(_live.values()):
        try:
            win.close()
        except RuntimeError:
            pass
    _live.clear()
