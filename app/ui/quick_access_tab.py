# -*- coding: utf-8 -*-
"""「⚡ 快捷操作」页：把四个轻量功能合成一页（左导航 + 右内容）。

合并的是**中键菜单 / 鼠标连点 / 键盘连按 / 找图点击**——这四个各自都只有一屏内容，
却各占一个顶部标签，把真正的主功能（自动化流程 / 定时任务 / 小工具 / 设置）挤到后面。
合并后顶部标签从 8 个降到 5 个，功能一个没少。

做法（关键取舍）
----------------
**不重写这四个页面**：主窗口照旧构造 `MiddleMenuTab / ClickerTab / PresserTab /
FinderTab`，连同既有的信号接线（changed / toggleRequested / captureAboutToStart…）
一起原样保留，只是不再各自 `addTab`，而是交给本页托管。这样：
- 主窗口里十几处对这些控件的引用与接线一行不用改；
- 四个页面各自的内部行为、测试、热键、取模隐藏主窗口等逻辑全部不变；
- 将来要拆回去或调整顺序，改的也只是本页的 `pages` 列表。

布局：左栏是定宽竖排导航按钮（选中 = 主色浅底 + 左侧竖条），右栏是 `QStackedWidget`；
点导航 -> `setCurrentIndex` -> 右侧内容同步切换，并发出 `currentChanged`。
导航样式刻意与设置页左侧导航（settings_tab）保持同一套：同样的宽度/行高/选中态画法。

⚠️ **导航标题不要带 emoji**：实测 🖱(U+1F5B1) / ⌨(U+2328) / 🖼(U+1F5BC) 在按钮文本里
渲染成缺字方块（只有 📋 有字形），设置页左侧导航也是纯文字——两页保持一致。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import theme

# 与设置页左侧导航同一套尺寸（两页的导航观感要一致）
NAV_W = 132                # 导航栏宽度
NAV_ITEM_H = 30            # 单个导航项高度
NAV_TITLE = "快捷操作"


class QuickAccessTab(QWidget):
    """左导航 + 右内容的多页面容器。

    `pages` 是 `[(导航标题, 页面控件), ...]`，顺序即导航顺序；同一个控件只应被
    托管一次（Qt 会自动把它从原父级摘走）。
    """

    currentChanged = Signal(int)        # 切换后发出（参数为新的索引）

    def __init__(self, pages=None, parent=None):
        super().__init__(parent)
        self.setObjectName("quickAccessPage")
        self._entries: list[tuple[str, QWidget, QPushButton]] = []
        self._active = -1
        self._build_ui()
        for title, widget in (pages or []):
            self.add_page(title, widget)
        if self._entries:
            self.set_current_index(0)

    # ---------- 骨架 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body)

        nav = QFrame()
        nav.setObjectName("quickAccessNav")
        nav.setFixedWidth(NAV_W)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(8, 12, 8, 12)
        nav_lay.setSpacing(2)
        nav_title = QLabel(NAV_TITLE)
        nav_lay.addWidget(nav_title)
        nav_lay.addSpacing(6)
        nav_lay.addStretch(1)       # 导航项 insertWidget(count-1) 插在这个 stretch 之前
        nav.setStyleSheet(self._nav_qss())
        self._nav_layout = nav_lay
        self._nav_frame = nav
        body.addWidget(nav, 0)

        self.stack = QStackedWidget()
        self.stack.setObjectName("quickAccessStack")
        body.addWidget(self.stack, 1)

    # ---------- 页面登记 ----------
    def add_page(self, title: str, widget: QWidget) -> None:
        """登记一个页面：右侧加进 stack，左侧加一个导航项。"""
        btn = QPushButton(title)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(NAV_ITEM_H)
        btn.setFocusPolicy(Qt.NoFocus)      # 不让导航项抢焦点、不留虚线框
        btn.setToolTip(title)
        btn.setStyleSheet(self._nav_item_qss(False))
        btn.clicked.connect(lambda _=False, w=widget: self.set_current_widget(w))
        self._nav_layout.insertWidget(self._nav_layout.count() - 1, btn)
        self.stack.addWidget(widget)
        self._entries.append((title, widget, btn))
        if self._active < 0:
            self._set_nav_active(0)

    # ---------- 查询 ----------
    def page_count(self) -> int:
        return len(self._entries)

    def titles(self) -> list[str]:
        return [title for title, _w, _b in self._entries]

    def pages(self) -> list[QWidget]:
        return [widget for _t, widget, _b in self._entries]

    def nav_buttons(self) -> list[QPushButton]:
        return [btn for _t, _w, btn in self._entries]

    def current_index(self) -> int:
        return self.stack.currentIndex()

    def current_widget(self) -> QWidget | None:
        return self.stack.currentWidget()

    def index_of(self, widget: QWidget) -> int:
        for i, (_t, other, _b) in enumerate(self._entries):
            if other is widget:
                return i
        return -1

    # ---------- 切换 ----------
    def set_current_index(self, index: int) -> None:
        """切到第 index 个页面（越界会被夹到合法范围）。"""
        if not self._entries:
            return
        index = max(0, min(int(index), len(self._entries) - 1))
        self.stack.setCurrentIndex(index)
        self._set_nav_active(index)

    def set_current_widget(self, widget: QWidget) -> int:
        """切到指定页面控件，返回它的索引（没登记过则返回 -1、什么也不做）。"""
        index = self.index_of(widget)
        if index >= 0:
            self.set_current_index(index)
        return index

    def _set_nav_active(self, index: int) -> None:
        if index < 0 or index == self._active:
            return
        self._active = index
        for i, (_title, _w, btn) in enumerate(self._entries):
            btn.setStyleSheet(self._nav_item_qss(i == index))
        self.currentChanged.emit(index)

    # ---------- 样式 ----------
    def _nav_qss(self) -> str:
        """导航栏底板与标题样式（颜色走主题令牌，切主题自动重映射）。"""
        tk = theme.token
        pt = theme.scaled_pt(theme.DEFAULT_FONT_PT)
        return (f"QFrame{{background:{tk('card_bg')};"
                f"border-right:1px solid {tk('border_light')};}}"
                f"QLabel{{color:{tk('text_muted')};font-weight:700;"
                f"font-size:{pt:g}pt;}}")

    def _nav_item_qss(self, active: bool) -> str:
        """导航项样式：选中 = 主题色浅底 + 左侧竖条；未选中 = 透明底 + 次要文字色。

        两条都用 `border-left:3px solid ...`（未选中时是 transparent），
        这样切换选中态时文字不会左右跳动。
        """
        tk = theme.token
        pt = theme.scaled_pt(theme.DEFAULT_FONT_PT)
        if active:
            bar, bg, fg, weight = (tk("primary"), tk("primary_soft"),
                                   tk("primary"), 700)
            hover_bg, hover_fg = tk("primary_soft"), tk("primary")
        else:
            bar = bg = "transparent"
            fg, weight = tk("text_dim"), 400
            hover_bg, hover_fg = tk("hover_bg"), tk("text")
        return (f"QPushButton{{background:{bg};color:{fg};border:none;"
                f"border-left:3px solid {bar};padding:6px 8px;text-align:left;"
                f"font-size:{pt:g}pt;font-weight:{weight};}}"
                f"QPushButton:hover{{background:{hover_bg};color:{hover_fg};}}")
