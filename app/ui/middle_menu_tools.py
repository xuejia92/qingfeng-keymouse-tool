# -*- coding: utf-8 -*-
"""中键菜单的「九宫格工具」：弹出菜单里的宫格 + 管理页的宫格编辑区。

菜单结构（2026-10-03 用户要求重新设计）
--------------------------------------
中键菜单弹出来分两段：

- **上面**：九宫格小工具——把「🧰 小工具」里的那些小程序摆进最多 9 格（3 列），
  点一下直接打开它（走 `mini_window.open_mini_app`，与在小工具页点击完全一致）；
- **下面**：关联流程的菜单项——原有行为，点一下运行对应流程。

宫格容量的唯一来源是 `config.MIDDLE_MENU_MAX_TOOLS`（3×3 = 9），存的是小程序 key。

实现要点
--------
**宫格用 QWidgetAction 嵌进 QMenu，而不是另起一个自定义弹窗。** 理由：`QMenu.exec()`
自带模态、Esc、点外部关闭、屏幕边界收拢，主窗口的中键触发循环（含「菜单开着时再次
触发就换个位置重开」）整套逻辑都建立在它之上；换成自定义弹窗等于把这些全部重写一遍。
**一个工具都没配时整段不插入**，弹出的菜单与改造前完全一致。

点击怎么回传：宫格按钮不调 `action.trigger()`，而是把 key 交给调用方给的 `on_pick`
回调、由回调负责关菜单。这样 `menu.actions()` 里只多一个承载宫格的 QWidgetAction，
不掺假的「工具 action」，既有用例与语义都不受影响。

配色：弹出菜单与宫格都用**主题令牌**（见 middle_menu_tab.menu_qss 与本模块的
popup_grid_qss），两边必须同一套——否则深色主题下会在菜单里嵌出一块浅色区域。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QMenu,
                               QMessageBox, QToolButton, QVBoxLayout, QWidget)

from ..config import MIDDLE_MENU_MAX_TOOLS, clean_tool_keys
from ..mini_apps import all_apps, find_app
from . import theme
from .mini_window import open_mini_app
from .widgets import emoji_icon

MAX_TOOLS = MIDDLE_MENU_MAX_TOOLS
GRID_COLUMNS = 3                    # 九宫格 = 3 列
POPUP_ICON = 24                     # 弹出菜单里图标画多大
POPUP_BTN_W, POPUP_BTN_H = 84, 66   # 弹出菜单里每格尺寸（够放 6 个中文字）
SLOT_ICON = 18                      # 管理页槽位图标
SLOT_W, SLOT_H = 88, 46             # 管理页每格尺寸


def popup_grid_qss() -> str:
    """弹出菜单里那块宫格的样式。

    颜色走主题令牌：弹出菜单的样式（middle_menu_tab.menu_qss）也是令牌化的，
    两边必须同一套，否则深色主题下会在菜单里嵌出一块浅色区域。
    """
    tk = theme.token
    return f"""
QWidget#middleMenuTools {{ background: transparent; }}
QToolButton#middleMenuToolButton {{
    background: {tk('card_bg')}; color: {tk('text')};
    border: 1px solid {tk('border_light')}; border-radius: 6px;
    padding: 4px 2px; font-size: 8.5pt;
}}
QToolButton#middleMenuToolButton:hover {{
    background: {tk('primary_soft')}; border-color: {tk('primary')};
    color: {tk('primary')};
}}
QToolButton#middleMenuToolButton:pressed {{ background: {tk('pressed_bg')}; }}
"""


def available_tools():
    """所有可加入宫格的小程序（按注册表顺序）。"""
    return list(all_apps())


def resolve_tools(keys) -> list[tuple[str, object]]:
    """把配置里的 key 列表解析成 [(key, MiniApp), ...]。

    认不出的 key（小程序被删掉/改名、手改配置写错）直接跳过——与图标 key
    同一策略，坏数据不该让菜单弹不出来。
    """
    out: list[tuple[str, object]] = []
    for key in clean_tool_keys(keys):
        app = find_app(key)
        if app is not None:
            out.append((key, app))
    return out


def _normalize(keys) -> list[str]:
    """编辑区持有的 key 列表：先按配置层规则清洗（去重/截断），再去掉不认识的。"""
    return [key for key in clean_tool_keys(keys) if find_app(key) is not None]


class ToolGridWidget(QWidget):
    """弹出菜单顶部的工具宫格（只画**实际配置了**的那些格子）。"""

    def __init__(self, keys, on_pick, parent=None):
        super().__init__(parent)
        self.setObjectName("middleMenuTools")
        self.setStyleSheet(popup_grid_qss())
        self._entries = resolve_tools(keys)
        self._buttons: list[QToolButton] = []

        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 6)
        grid.setSpacing(6)
        for index, (key, app) in enumerate(self._entries):
            button = QToolButton()
            button.setObjectName("middleMenuToolButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIcon(emoji_icon(app.icon, POPUP_ICON))
            button.setIconSize(QSize(POPUP_ICON, POPUP_ICON))
            button.setText(app.name)
            button.setFixedSize(POPUP_BTN_W, POPUP_BTN_H)
            button.setToolTip(app.desc or app.name)
            button.clicked.connect(lambda _=False, k=key: on_pick(k))
            grid.addWidget(button, index // GRID_COLUMNS, index % GRID_COLUMNS)
            self._buttons.append(button)

    def keys(self) -> list[str]:
        return [key for key, _app in self._entries]

    def buttons(self) -> list[QToolButton]:
        return list(self._buttons)

    def click_at(self, index: int) -> None:
        """点第 index 个格子（测试用；等价于鼠标点击）。"""
        self._buttons[index].click()


class ToolGridEditor(QWidget):
    """管理页的九宫格编辑区：**固定 9 个槽位**，空的显示「＋」。

    - 空槽点击 -> 列出还没加入的小工具，选中即加入（追加到末尾，顺序即宫格顺序）；
    - 已填槽点击 -> 「打开工具 / 移除」；
    - 所有变更通过 `changed` 信号交给调用方写回 `cfg.middle_menu_tools` 并保存。
    """

    changed = Signal()

    def __init__(self, keys=None, parent=None):
        super().__init__(parent)
        self._keys = _normalize(keys)
        self._slots: list[QToolButton] = []
        self._build_ui()
        self.refresh()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        head = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setStyleSheet("color:#8a939c;")
        head.addWidget(self.count_label)
        head.addStretch(1)
        root.addLayout(head)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for index in range(MAX_TOOLS):
            button = QToolButton()
            button.setCursor(Qt.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(SLOT_ICON, SLOT_ICON))
            button.setFixedSize(SLOT_W, SLOT_H)
            button.clicked.connect(lambda _=False, i=index: self._on_slot(i))
            grid.addWidget(button, index // GRID_COLUMNS, index % GRID_COLUMNS)
            self._slots.append(button)
        grid.setColumnStretch(GRID_COLUMNS, 1)      # 末尾弹性列，槽位贴左上角
        root.addLayout(grid)

    def _slot_qss(self, filled: bool) -> str:
        """槽位样式：空的走虚线框，已填的走实线框 + 主色悬停。"""
        tk = theme.token
        border = (f"1px solid {tk('border')}" if filled
                  else f"1px dashed {tk('border')}")
        return (f"QToolButton{{background:{tk('card_bg')};color:{tk('text')};"
                f"border:{border};border-radius:6px;padding:2px;font-size:8pt;}}"
                f"QToolButton:hover{{background:{tk('primary_soft')};"
                f"border-color:{tk('primary')};color:{tk('primary')};}}")

    # ---------- 状态 ----------
    def keys(self) -> list[str]:
        return list(self._keys)

    def set_keys(self, keys) -> None:
        self._keys = _normalize(keys)
        self.refresh()

    def slots(self) -> list[QToolButton]:
        return list(self._slots)

    def refresh(self) -> None:
        for index, button in enumerate(self._slots):
            if index < len(self._keys):
                app = find_app(self._keys[index])
                button.setIcon(emoji_icon(app.icon, SLOT_ICON))
                button.setText(app.name)
                button.setToolTip(f"{app.name}：{app.desc}\n点击可打开或移除")
                button.setStyleSheet(self._slot_qss(True))
                button.setObjectName("middleMenuToolSlot")
            else:
                button.setIcon(emoji_icon("", SLOT_ICON))
                button.setText("＋")
                button.setToolTip("点击添加一个小工具" if
                                  len(self._keys) < len(available_tools())
                                  else "没有更多可用的小工具了")
                button.setStyleSheet(self._slot_qss(False))
                button.setObjectName("middleMenuToolSlotEmpty")
        self.count_label.setText(
            f"九宫格工具（{len(self._keys)} / {MAX_TOOLS}）"
            "：点空格子加工具，点已填的格子可打开或移除；"
            "它们会显示在中键菜单的最上面。")

    # ---------- 增删 ----------
    def assign(self, key: str) -> bool:
        """把一个工具加入宫格末尾（满了 / 已有 / 认不出 都返回 False）。"""
        key = str(key or "")
        if not key or key in self._keys or len(self._keys) >= MAX_TOOLS:
            return False
        if find_app(key) is None:
            return False
        self._keys.append(key)
        self.refresh()
        self.changed.emit()
        return True

    def remove(self, key: str) -> bool:
        if key not in self._keys:
            return False
        self._keys.remove(key)
        self.refresh()
        self.changed.emit()
        return True

    # ---------- 交互 ----------
    def _on_slot(self, index: int) -> None:
        if index < len(self._keys):
            self._slot_menu(self._keys[index])
        else:
            self._pick_menu()

    def _slot_menu(self, key: str) -> None:
        app = find_app(key)
        if app is None:
            self.remove(key)            # 兜底：认不出的 key 直接清掉
            return
        menu = QMenu(self)
        open_action = menu.addAction(f"打开「{app.name}」")
        remove_action = menu.addAction("从九宫格移除")
        chosen = menu.exec(self._slots[self._keys.index(key)].mapToGlobal(
            self._slots[self._keys.index(key)].rect().bottomLeft()))
        if chosen is open_action:
            open_mini_app(app, anchor=self.window())
        elif chosen is remove_action:
            self.remove(key)

    def _pick_menu(self) -> None:
        used = set(self._keys)
        candidates = [app for app in available_tools() if app.key not in used]
        if not candidates:
            QMessageBox.information(self, "九宫格工具", "没有更多可添加的小工具了。")
            return
        menu = QMenu(self)
        actions = {}
        for app in candidates:
            action = menu.addAction(emoji_icon(app.icon, 16), app.name)
            actions[action] = app.key
        chosen = menu.exec(self._slots[min(len(self._keys), MAX_TOOLS - 1)]
                           .mapToGlobal(
            self._slots[min(len(self._keys), MAX_TOOLS - 1)].rect().bottomLeft()))
        if chosen in actions:
            self.assign(actions[chosen])
