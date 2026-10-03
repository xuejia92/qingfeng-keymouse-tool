# -*- coding: utf-8 -*-
"""「🧰 小工具」页：以九宫格展示可独立运行的小程序。

布局
----
整页就是一块可滚动的九宫格（**没有页内标题和说明文字**：标签页名已经说明了
这是什么页，再加一行文字纯属占地方）。里面是三列网格，每张卡片是一个
`QToolButton`（图标在上、名称在下），悬停提示写的是小程序的用途说明。
卡片数量由 `app.mini_apps` 注册表决定，**加小程序不用改本文件**
（见 `app/mini_apps/__init__.py` 顶部的两步说明）。

点击行为
--------
点击任一张卡片 -> `mini_window.open_mini_app()` -> 在**与主程序相互独立**的
单独窗口里把它跑起来（同一小程序重复点击只是把已有窗口置前，不会攒一堆）。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QFrame, QGridLayout, QLabel, QScrollArea,
                               QToolButton, QVBoxLayout, QWidget)

from ..mini_apps import MiniApp, all_apps
from . import theme
from .mini_window import open_mini_app
from .widgets import emoji_icon

GRID_COLUMNS = 3            # 「九宫格」= 三列
CARD_W, CARD_H = 168, 130   # 单张卡片尺寸
CARD_ICON = 46              # 卡片里图标画多大
CARD_GAP = 10               # 卡片间距
PAGE_MARGIN = 16            # 页面留白


def _emoji_icon(emoji: str, size: int):
    """兼容旧调用点：emoji -> QIcon 的实现已提升到 widgets.emoji_icon。"""
    return emoji_icon(emoji, size)


def _grid_qss() -> str:
    """卡片配色（页面级内联样式，颜色全部走主题令牌 -> 切主题自动跟随）。

    ⚠️ QToolButton **不在**全局按钮基线（那里只有 QPushButton）里，所以这里
    必须把 border/背景/文字色写全，否则卡片会是 Windows 原生灰按钮的样子。
    """
    t = theme.token
    return f"""
    /* 网格容器透明：底色交给标签页自己的面板底（QTabWidget::pane），
       否则容器只在第一行卡片那一带铺出灰底、下方露出白色，看着像一条横幅 */
    QWidget#toolsGrid {{
        background: transparent;
    }}
    QWidget#toolsGrid QToolButton {{
        background: {t('card_bg')};
        color: {t('text')};
        border: 1px solid {t('border')};
        border-radius: 10px;
        padding: 10px 4px 8px 4px;
        font-size: 10pt;
    }}
    QWidget#toolsGrid QToolButton:hover {{
        border: 1px solid {t('primary')};
        background: {t('primary_soft')};
        color: {t('primary')};
    }}
    QWidget#toolsGrid QToolButton:pressed {{
        background: {t('pressed_bg')};
    }}
    """


class ToolsTab(QWidget):
    """小工具九宫格页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toolsPage")
        self._cards: list[QToolButton] = []
        self._stretch_row: int | None = None
        self._build_ui()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        # 页面直接就是九宫格：不放页内标题/说明（标签页名已经说明了这是什么页）
        root.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        root.setSpacing(10)

        self.grid_holder = QWidget()
        self.grid_holder.setObjectName("toolsGrid")
        self.grid_holder.setStyleSheet(_grid_qss())
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(CARD_GAP)
        # 末尾留一条弹性列吸收多余宽度：否则只有一两个小程序时，QGridLayout 会把
        # 空白平摊到「有内容的列」上，卡片会被推到页面中间（2026-10-03 离屏核验发现）。
        self.grid.setColumnStretch(GRID_COLUMNS, 1)
        self._empty_label = QLabel("还没有登记任何小程序")
        self._empty_label.setStyleSheet(
            f"color:{theme.token('text_muted')};font-size:10pt;padding:24px;")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(self._empty_label, 0, 0)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(self.grid_holder)
        # 滚动区与视口都要透明：它们默认按 palette 的 Base 角色铺白底，
        # 会把标签页的面板底色盖掉（和上面网格容器那条注释是同一个问题）
        area.setStyleSheet("background: transparent;")
        area.viewport().setStyleSheet("background: transparent;")
        root.addWidget(area, 1)

        self.reload()

    def reload(self) -> None:
        """按注册表重建全部卡片（新增小程序后调一次即可看到）。"""
        for card in self._cards:
            self.grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        if self._stretch_row is not None:       # 上一轮的弹性行先撤掉，免得串位
            self.grid.setRowStretch(self._stretch_row, 0)

        apps = all_apps()
        self._empty_label.setVisible(not apps)
        for idx, app in enumerate(apps):
            card = self._make_card(app)
            self._cards.append(card)
            self.grid.addWidget(card, idx // GRID_COLUMNS, idx % GRID_COLUMNS)

        # 行方向的弹性行（同列方向）：卡片整齐贴左上角，多余高度留在最下方
        rows = max(1, (len(apps) + GRID_COLUMNS - 1) // GRID_COLUMNS)
        self._stretch_row = rows
        self.grid.setRowStretch(rows, 1)

    def cards(self) -> list[QToolButton]:
        """当前全部卡片（顺序 = 注册顺序），测试用。"""
        return list(self._cards)

    def _make_card(self, app: MiniApp) -> QToolButton:
        card = QToolButton()
        card.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        card.setIconSize(QSize(CARD_ICON, CARD_ICON))
        card.setIcon(_emoji_icon(app.icon, CARD_ICON))
        card.setText(app.name)
        card.setToolTip(f"{app.name}\n{app.desc}" if app.desc else app.name)
        card.setFixedSize(CARD_W, CARD_H)
        card.setCursor(Qt.PointingHandCursor)
        card.setAutoRaise(True)
        card.clicked.connect(lambda _=False, a=app: self.open_app(a))
        return card

    # ---------- 打开 ----------
    def open_app(self, app: MiniApp) -> None:
        """在某张卡片被点击时调用：在独立窗口中打开该小程序。"""
        # anchor 只用来挑显示器，不作为父对象（见 mini_window 注释）
        open_mini_app(app, anchor=self.window())
