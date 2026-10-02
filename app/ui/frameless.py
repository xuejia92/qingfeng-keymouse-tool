# -*- coding: utf-8 -*-
"""无边框编辑弹窗基类：自绘标题栏 + 整窗统一边框（2026-09-27）。

系统标题栏由 Windows 原生绘制，QSS 加不了边框——之前给 QDialog 加的
border 只包住内容区，标题栏一行"裸"着，视觉不统一。所以编辑弹窗改为
无边框窗口 + 自绘标题栏，让边框完整包裹标题栏和内容区。

用法：继承 `FramelessDialog`，布局挂到 `self.body()` 上（而不是 self），
其余（setWindowTitle / 控件 / accept / reject）照旧。
关闭按钮 = reject()；Esc 键仍由 QDialog 默认处理；按住标题栏可拖动。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from . import theme


def _build_qss() -> str:
    """边框卡片的样式（颜色走主题令牌，主题切换时由 hook 自动重映射）。"""
    t = theme.current()
    g = t.get
    return f"""
    QDialog#framelessDialog {{ background: transparent; }}
    QFrame#dlgCard {{
        background: {g('card_bg')};
        border: 1px solid {g('dialog_border')};
        border-radius: 10px;
    }}
    QWidget#dlgTitleBar {{
        background: {g('panel_bg')};
        border-top-left-radius: 10px;
        border-top-right-radius: 10px;
    }}
    QLabel#dlgTitle {{ color: {g('text')}; font-weight: 600; }}
    QWidget#dlgBody {{ background: transparent; }}
    QPushButton#dlgClose {{
        background: transparent; border: none; color: {g('text_muted')};
        font-size: 11pt; border-radius: 4px;
    }}
    QPushButton#dlgClose:hover {{ background: {g('danger')}; color: white; }}
    """


class FramelessDialog(QDialog):
    """无边框弹窗：顶部自绘标题栏，整窗统一边框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)   # 圆角外的四角透明
        self.setObjectName("framelessDialog")
        self._drag_offset = None
        self._centered = False            # showEvent 里只居中一次（见下方注释）

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("dlgCard")
        outer.addWidget(self._card)
        card = QVBoxLayout(self._card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(0)

        # 自绘标题栏：标题 + 关闭按钮
        self._title_bar = QWidget()
        self._title_bar.setObjectName("dlgTitleBar")
        tb = QHBoxLayout(self._title_bar)
        tb.setContentsMargins(16, 9, 6, 9)
        tb.setSpacing(0)
        self._title_label = QLabel("")
        self._title_label.setObjectName("dlgTitle")
        tb.addWidget(self._title_label, 1)
        # 关闭叉用 **QPainter 自绘图标**：字体方案两头堵——×（U+00D7）字形太小，
        # ✕（U+2715）在雅黑下缺字形渲染成小点（2026-10-02 用户两次反馈）。
        # 自绘则大小/粗细可控；Normal=次要文字色，Active(hover)=白色（配 danger 红底）。
        self._close_btn = QPushButton()
        self._close_btn.setObjectName("dlgClose")
        self._close_btn.setIcon(self._close_icon())
        self._close_btn.setIconSize(QSize(16, 16))
        self._close_btn.setFixedSize(26, 22)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self.reject)
        tb.addWidget(self._close_btn)
        card.addWidget(self._title_bar)

        self._body = QWidget()
        self._body.setObjectName("dlgBody")
        card.addWidget(self._body, 1)

        self.setStyleSheet(_build_qss())

    def body(self) -> QWidget:
        """内容容器：子类的布局挂到这里（`QVBoxLayout(self.body())`）。"""
        return self._body

    def setWindowTitle(self, title: str) -> None:
        super().setWindowTitle(title)
        self._title_label.setText(title)

    @staticmethod
    def _close_icon() -> QIcon:
        """自绘关闭叉：两条圆头线，16px，2x 超采样抗锯齿。

        Normal 态 = 正文色（明显可见）；Active(hover) 态 = 白色——QSS 里 hover
        背景是 danger 红，白叉才看得清。颜色取构造时的当前主题（弹窗生命周期
        短，不跟随中途换主题）。
        """
        t = theme.current().get
        icon = QIcon()
        for mode, color in ((QIcon.Mode.Normal, QColor(t("text"))),
                            (QIcon.Mode.Active, QColor("white"))):
            pm = QPixmap(32, 32)
            pm.setDevicePixelRatio(2)        # 2x 超采样：高分屏线条不糊
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            try:
                p.setRenderHint(QPainter.Antialiasing, True)
                pen = QPen(color, 2.2)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(2, 2, 14, 14)     # 16px 内的叉，两端留 2px 圆头
                p.drawLine(14, 2, 2, 14)
            finally:
                p.end()
            icon.addPixmap(pm, mode)
        return icon

    # ---------- 居中显示 ----------
    def showEvent(self, event) -> None:     # noqa: N802（Qt 命名）
        super().showEvent(event)
        # QDialog 首次显示会被定位到「父窗口中心」（Qt 内建行为）——父窗口被拖到
        # 屏幕外时弹窗也会跟着跑出去（Windows 报 Unable to set geometry ...-213，
        # 2026-10-02 用户反馈定时关机编辑弹窗在界面外）。且 showEvent 里直接 move
        # 会被随后的初始定位覆盖，所以**延迟一拍**（等 Qt 完成首次布局/定位）再居中，
        # 并用 _centered 标志只做一次（exec 期间用户拖动弹窗不被拽回）。
        if not self._centered:
            self._centered = True
            QTimer.singleShot(0, self._center_into_screen)

    def _center_into_screen(self) -> None:
        """居中于主窗口所在屏幕的可用区域中心，并限位在屏内。

        注意是「所在屏」而不是「父窗口矩形」：无边框主窗口能被拖出屏幕，
        跟着父窗口矩形居中会把弹窗也顶出屏幕。
        """
        parent = self.parentWidget()
        parent_win = parent.window() if parent is not None else None
        screen = None
        if parent_win is not None and parent_win is not self:
            screen = parent_win.screen()
        if screen is None:
            screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        geo = self.frameGeometry()
        if geo.width() > avail.width():
            geo.setWidth(avail.width())
        if geo.height() > avail.height():
            geo.setHeight(avail.height())
        geo.moveCenter(avail.center())
        # 居中天然在屏内；防取整误差再夹一次
        geo.moveLeft(max(avail.left(),
                         min(geo.left(), avail.right() - geo.width() + 1)))
        geo.moveTop(max(avail.top(),
                        min(geo.top(), avail.bottom() - geo.height() + 1)))
        self.move(geo.topLeft())

    # ---------- 拖动（只响应标题栏区域，点内容不拖） ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._in_title_bar(e.position().toPoint()):
            self._drag_offset = (e.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_offset = None
        super().mouseReleaseEvent(e)

    def _in_title_bar(self, pos) -> bool:
        return self._title_bar.geometry().contains(pos)
