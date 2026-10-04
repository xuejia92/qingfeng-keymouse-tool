# -*- coding: utf-8 -*-
"""「🧰 小工具」页：以宫格展示可独立运行的小程序。

布局
----
整页就是一块可滚动的宫格（**没有页内标题和说明文字**：标签页名已经说明了
这是什么页，再加一行文字纯属占地方）。里面是网格卡片，每张卡片是一个
`QToolButton`（图标在上、名称在下），悬停提示写的是小程序的用途说明。

**列数按可用宽度自动算**（2026-10-04 用户要求）：窗口宽就多排几列、窄就少排几列，
不再写死三列。触发点是卡片容器的 `resizeEvent`（不是页面的 resizeEvent，
原因见 `GridHolder`）。列数没变时不碰任何控件，所以常规拖动窗口不会反复重排。
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
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..mini_apps import MiniApp, all_apps
from . import theme
from .mini_window import open_mini_app
from .widgets import emoji_icon

GRID_COLUMNS = 3            # 列数初值（真正排版时按可用宽度算，见 columns_for_width）
MAX_COLUMNS = 12            # 列数上限：再宽也不排成一条长龙
CARD_W, CARD_H = 136, 104   # 单张卡片尺寸（2026-10-04 用户要求"每个宫格宽高都小一点"）
CARD_ICON = 36              # 卡片里图标画多大
CARD_GAP = 8                # 卡片间距
PAGE_MARGIN = 16            # 页面留白


def _emoji_icon(emoji: str, size: int):
    """兼容旧调用点：emoji -> QIcon 的实现已提升到 widgets.emoji_icon。"""
    return emoji_icon(emoji, size)


class GridHolder(QWidget):
    """承载卡片的容器：宽度一变就把新宽度回调出去。

    为什么要这一层：滚动区是 `setWidgetResizable(True)`，容器的宽度由**视口**决定，
    页面（ToolsTab）resize 时它未必已经跟着变，在页面的 resizeEvent 里量宽度会慢一拍。
    装在容器自己的 resizeEvent 上才是"宽度真的变了"这个时机。
    """

    def __init__(self, on_width_changed, parent=None):
        super().__init__(parent)
        self._on_width_changed = on_width_changed

    def resizeEvent(self, event):       # noqa: N802（Qt 命名）
        super().resizeEvent(event)
        # 用事件里的新尺寸而不是 self.width()：两者在真实 resize 时相同，
        # 但显式取事件参数后，测试投一个 QResizeEvent 就能确定性地验证这条契约。
        self._on_width_changed(event.size().width())


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
        border-radius: 8px;
        padding: 8px 4px 6px 4px;
        font-size: 9pt;
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
        self._columns = GRID_COLUMNS       # 当前列数（按宽度算出来，见 _apply_width）
        self._stretch_row: int | None = None
        self._build_ui()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        # 页面直接就是宫格：不放页内标题/说明（标签页名已经说明了这是什么页）
        root.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        root.setSpacing(10)

        self.grid_holder = GridHolder(self._apply_width)
        self.grid_holder.setObjectName("toolsGrid")
        self.grid_holder.setStyleSheet(_grid_qss())
        # ⚠️ 横向必须是 Ignored：卡片是 setFixedSize 的，于是 QGridLayout 的
        # **最小宽度**= 当前列数 × 卡片宽 + 间距。而 setWidgetResizable 的滚动区
        # 不会把容器压到它自己的最小宽度以下 —— 结果是「列数锁死」：窗口拖窄了，
        # 容器宽度不跟着变小（实测宽 560 的页面里容器仍是 568），回调永远算不出更少的
        # 列，卡片直接被裁掉。设成 Ignored 后容器宽度完全跟随视口，列数才能减少。
        self.grid_holder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(CARD_GAP)
        # 末尾留一条弹性列吸收多余宽度：否则只有一两个小程序时，QGridLayout 会把
        # 空白平摊到「有内容的列」上，卡片会被推到页面中间（2026-10-03 离屏核验发现）。
        self.grid.setColumnStretch(self._columns, 1)
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

    # ---------- 自适应列数 ----------
    @staticmethod
    def columns_for_width(width: int) -> int:
        """按可用宽度算列数（至少 1 列、至多 MAX_COLUMNS）。

        纯函数，便于直接测边界：宽度不够放一张卡片时也得给 1 列，
        否则卡片会被压扁或跑到可视区外。
        """
        usable = max(0, int(width))
        columns = (usable + CARD_GAP) // (CARD_W + CARD_GAP)
        return int(max(1, min(MAX_COLUMNS, columns)))

    def columns(self) -> int:
        """当前列数（测试与排版都以它为准）。"""
        return self._columns

    def _apply_width(self, width: int) -> None:
        """容器宽度变化：列数真的变了才重排（列数没变时一个控件都不动）。"""
        columns = self.columns_for_width(width)
        if columns == self._columns:
            return
        self._columns = columns
        self._layout_cards()

    def _layout_cards(self) -> None:
        """按当前列数把卡片重摆一遍（先 removeWidget 再 addWidget，控件不销毁）。"""
        columns = self._columns
        for index, card in enumerate(self._cards):
            self.grid.removeWidget(card)
            self.grid.addWidget(card, index // columns, index % columns)
        self._reset_stretch()

    def _reset_stretch(self) -> None:
        """重设弹性行列：卡片整齐贴左上角，多余宽度/高度都留在右下方。"""
        for col in range(0, MAX_COLUMNS + 1):
            self.grid.setColumnStretch(col, 0)
        self.grid.setColumnStretch(self._columns, 1)
        if self._stretch_row is not None:
            self.grid.setRowStretch(self._stretch_row, 0)
        rows = max(1, (max(1, len(self._cards)) + self._columns - 1)
                   // self._columns)
        self._stretch_row = rows
        self.grid.setRowStretch(rows, 1)

    def reload(self) -> None:
        """按注册表重建全部卡片（新增小程序后调一次即可看到）。"""
        for card in self._cards:
            self.grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        if self._stretch_row is not None:       # 上一轮的弹性行先撤掉，免得串位
            self.grid.setRowStretch(self._stretch_row, 0)
            self._stretch_row = None

        apps = all_apps()
        self._empty_label.setVisible(not apps)
        for app in apps:
            self._cards.append(self._make_card(app))

        # 卡片位置统一由 _layout_cards 按**当前可用宽度**决定，别再写死 3 列
        self._columns = self.columns_for_width(self.grid_holder.width())
        self._layout_cards()

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
