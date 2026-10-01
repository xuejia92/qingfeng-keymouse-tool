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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
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
        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("dlgClose")
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
