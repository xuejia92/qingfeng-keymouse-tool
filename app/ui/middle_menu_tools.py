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

import logging

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                               QMenu, QMessageBox, QToolButton, QVBoxLayout,
                               QWidget)

from .. import mouse_menu, physical_hotkeys
from ..config import MIDDLE_MENU_MAX_TOOLS, clean_tool_keys
from ..mini_apps import all_apps, find_app
from ..tool_entries import (ToolEntry, all_entries, file_icon, find_entry,
                            open_entry, resolve_entries)
from . import theme
from .mini_window import open_mini_app
from .widgets import emoji_icon

log = logging.getLogger(__name__)

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
    """所有可加入宫格的**内置小程序**（按注册表顺序）。"""
    return list(all_apps())


def available_entries(custom_tools=None) -> list[ToolEntry]:
    """所有可加入宫格的条目：内置小程序在内、用户自定义程序在后。

    （2026-10-04 用户要求：小工具页加进来的电脑里的程序，也要能放进九宫格。）
    """
    return all_entries(custom_tools)


def entry_icon(entry: ToolEntry, size: int):
    """条目在卡片/槽位上的图标：自定义程序用它自己的文件图标。"""
    if entry.path:
        return file_icon(entry.path, size)
    return emoji_icon(entry.icon, size)


def resolve_tools(keys) -> list[tuple[str, object]]:
    """把配置里的 key 列表解析成 [(key, MiniApp), ...]（只认内置小程序）。

    保留这个老接口给调用方与既有用例；九宫格本身用 `resolve_entries`
    （它还会认自定义程序）。
    """
    out: list[tuple[str, object]] = []
    for key in clean_tool_keys(keys):
        app = find_app(key)
        if app is not None:
            out.append((key, app))
    return out


def _normalize(keys, custom_tools=None) -> list[str]:
    """编辑区持有的 key 列表：先按配置层规则清洗（去重/截断），再去掉不认识的。"""
    return [key for key in clean_tool_keys(keys)
            if find_entry(key, custom_tools) is not None]


# 拖动换位用的私有剪贴板格式（只在本程序内部流转，不放到系统剪贴板语义上）
SLOT_MIME = "application/x-qingfeng-tool-slot"


class _SlotButton(QToolButton):
    """九宫格的一格：**可拖动**，也可作为放置目标（用来调位置）。

    拖动交互（2026-10-05 用户要求「九宫格可以随意调整每个格子的位置」）：
    - 按住已填的格子拖到**另一个已填格子** → 两者**交换**；
    - 拖到**空格子** → 挪到列表末尾（宫格始终紧凑，不留空洞）；
    - 按住空格子拖没有意义（没有内容可搬），直接不触发。
    发起拖拽要越过系统的"开始拖动"距离阈值，否则单击（打开/移除菜单）会被吃掉。
    """

    dropped = Signal(int, int)      # (来源格, 目标格)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.slot_index = int(index)
        self._press_pos: QPoint | None = None
        self.setAcceptDrops(True)

    # ---------- 发起拖动 ----------
    def mousePressEvent(self, event):       # noqa: N802（Qt 命名）
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):     # noqa: N802
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):        # noqa: N802
        if self._press_pos is None or not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < QApplication.startDragDistance():
            return                          # 还没到拖动阈值：交给父类处理（保持单击可用）
        if not self._draggable:
            return
        mime = QMimeData()
        mime.setData(SLOT_MIME, str(self.slot_index).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.setHotSpot(QPoint(self.width() // 2, self.height() // 2))
        # ⚠️ 拖拽期间让全局钩子让位（同 flow_dialog.StepList / group_tree 的处理）：
        # 钩子的 Python 回调抢不到 GIL 会把整条鼠标输入通路卡住（实测一次拖动
        # 只推进 2~4 个事件、单次卡 1000ms 以上）；键盘钩子还要防被 Windows 超时摘掉。
        paused = mouse_menu.pause()
        physical_hotkeys.pause()
        try:
            drag.exec(Qt.MoveAction)
        finally:
            if paused:
                mouse_menu.unpause()
            physical_hotkeys.unpause()

    @property
    def _draggable(self) -> bool:
        """空的格子没东西可搬，不发起拖动（否则在空格上按住拖会白闪一下）。"""
        return bool(self.property("slotFilled"))

    # ---------- 作为放置目标 ----------
    def dragEnterEvent(self, event):        # noqa: N802
        if event.mimeData().hasFormat(SLOT_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):         # noqa: N802
        if event.mimeData().hasFormat(SLOT_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event):             # noqa: N802
        if not event.mimeData().hasFormat(SLOT_MIME):
            return
        raw = bytes(event.mimeData().data(SLOT_MIME)).decode("ascii", "ignore")
        event.acceptProposedAction()
        try:
            source = int(raw)
        except ValueError:
            return
        self.dropped.emit(source, self.slot_index)


class ToolGridWidget(QWidget):
    """弹出菜单顶部的工具宫格（只画**实际配置了**的那些格子）。"""

    def __init__(self, keys, on_pick, parent=None, custom_tools=None):
        super().__init__(parent)
        self.setObjectName("middleMenuTools")
        self.setStyleSheet(popup_grid_qss())
        self._entries = resolve_entries(clean_tool_keys(keys), custom_tools)
        self._buttons: list[QToolButton] = []

        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 6)
        grid.setSpacing(6)
        for index, entry in enumerate(self._entries):
            button = QToolButton()
            button.setObjectName("middleMenuToolButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIcon(entry_icon(entry, POPUP_ICON))
            button.setIconSize(QSize(POPUP_ICON, POPUP_ICON))
            button.setText(entry.name)
            button.setFixedSize(POPUP_BTN_W, POPUP_BTN_H)
            button.setToolTip(entry.desc or entry.name)
            button.clicked.connect(lambda _=False, k=entry.key: on_pick(k))
            grid.addWidget(button, index // GRID_COLUMNS, index % GRID_COLUMNS)
            self._buttons.append(button)

    def keys(self) -> list[str]:
        return [entry.key for entry in self._entries]

    def entries(self) -> list[ToolEntry]:
        return list(self._entries)

    def buttons(self) -> list[QToolButton]:
        return list(self._buttons)

    def click_at(self, index: int) -> None:
        """点第 index 个格子（测试用；等价于鼠标点击）。"""
        self._buttons[index].click()


class ToolGridEditor(QWidget):
    """管理页的九宫格编辑区：**固定 9 个槽位**，空的显示「＋」。

    - 空槽点击 -> 列出还没加入的工具（**内置小程序 + 用户自定义程序**），
      选中即加入（追加到末尾，顺序即宫格顺序）；
    - 已填槽点击 -> 「打开 / 移除」；
    - 所有变更通过 `changed` 信号交给调用方写回 `cfg.middle_menu_tools` 并保存。
    """

    changed = Signal()

    def __init__(self, keys=None, parent=None, custom_tools=None,
                 custom_tools_provider=None):
        super().__init__(parent)
        self._custom_tools = list(custom_tools or [])
        # 实时取值用（见 custom_tools()）：小工具页运行期随时会加/删程序
        self._custom_provider = custom_tools_provider
        self._keys = _normalize(keys, self.custom_tools())
        self._slots: list[QToolButton] = []
        self._build_ui()
        self.refresh()

    def custom_tools(self) -> list:
        """当前可选的自定义程序（用户在小工具页加进来的电脑里的程序）。

        ⚠️ **优先用 provider 实时取值**，不要只在构造时抄一份列表：小工具页运行期
        随时可能新增程序，抄一份的话编辑区会一直拿旧数据 —— 用户反馈的
        「动态新添加的电脑程序，中键菜单里无法实时获取到」就是它。
        provider 传的是 `lambda: cfg.custom_tools`，每次读都是最新的。
        """
        if self._custom_provider is not None:
            try:
                return list(self._custom_provider() or [])
            except Exception:       # noqa: BLE001（取不到就当没有，别让编辑区崩）
                log.exception("读取自定义小工具失败")
                return []
        return list(self._custom_tools)

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
            button = _SlotButton(index)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(SLOT_ICON, SLOT_ICON))
            button.setFixedSize(SLOT_W, SLOT_H)
            button.clicked.connect(lambda _=False, i=index: self._on_slot(i))
            button.dropped.connect(self._on_slot_dropped)
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
        self._keys = _normalize(keys, self.custom_tools())
        self.refresh()

    def set_custom_tools(self, custom_tools) -> None:
        """静态指定可选的自定义程序（测试用；真实运行时走 provider 实时取值）。"""
        self._custom_tools = list(custom_tools or [])
        self._keys = _normalize(self._keys, self.custom_tools())
        self.refresh()

    def on_tools_changed(self) -> None:
        """小工具页加/删/改了程序：重读列表，并把已失效的格子清掉。

        小工具页删掉某个程序后，九宫格里那一格就没有对应的条目了 —— 与其留个
        点不动的格子，不如直接清掉。
        """
        self.set_keys(self.keys())      # set_keys 会重新规范化（含清掉失效格子）并刷新

    def slots(self) -> list[QToolButton]:
        return list(self._slots)

    def refresh(self) -> None:
        for index, button in enumerate(self._slots):
            if index < len(self._keys):
                entry = find_entry(self._keys[index], self.custom_tools())
                button.setIcon(entry_icon(entry, SLOT_ICON))
                button.setText(entry.name)
                button.setToolTip(f"{entry.name}：{entry.desc}\n"
                                  "点击可打开或移除；**按住拖到别的格子可以换位置**")
                button.setStyleSheet(self._slot_qss(True))
                button.setObjectName("middleMenuToolSlot")
                button.setProperty("slotFilled", True)
            else:
                button.setIcon(emoji_icon("", SLOT_ICON))
                button.setText("＋")
                button.setToolTip("点击往宫格末尾加一个工具"
                                  "（内置小程序和你添加的程序都在里面）"
                                  if len(self._keys) < len(self.entries())
                                  else "没有更多可用的工具了")
                button.setStyleSheet(self._slot_qss(False))
                button.setObjectName("middleMenuToolSlotEmpty")
                button.setProperty("slotFilled", False)
        self.count_label.setText(
            f"九宫格工具（{len(self._keys)} / {MAX_TOOLS}）"
            "：点空格子加工具（内置小程序和你在小工具页添加的程序都在里面）；"
            "**按住格子拖到别的格子即可调整位置**（拖到已填的格子=交换）；"
            "点已填的格子可打开或移除。")

    def entries(self) -> list[ToolEntry]:
        """当前可选的条目（内置小程序 + 自定义程序）。"""
        return available_entries(self.custom_tools())

    # ---------- 增删 ----------
    def assign(self, key: str) -> bool:
        """把一个工具加入宫格末尾（满了 / 已有 / 认不出 都返回 False）。"""
        key = str(key or "")
        if not key or key in self._keys or len(self._keys) >= MAX_TOOLS:
            return False
        if find_entry(key, self.custom_tools()) is None:
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

    # ---------- 调位置（拖动）----------
    def move_slot(self, source: int, target: int) -> bool:
        """把第 source 格的内容挪到第 target 格。

        - 目标格**有内容** → 两者**交换**（宫格里换位置就是换条目，列表保持紧凑）；
        - 目标格**是空的** → 挪到列表末尾（宫格始终紧凑，不留空洞）；
        - 来源格是空的 / 越界 / 原地不动 → 返回 False，不发信号。

        为什么不做"留空洞"的稀疏模型：配置里 `middle_menu_tools` 是一个**紧凑的
        key 列表**（`clean_tool_keys` 会丢掉空项），宫格按顺序从左到右填 ——
        交换足以到达任何排列，而留空洞要改动配置层的语义与既有校验。
        """
        count = len(self._keys)
        if source == target:
            return False
        if not (0 <= source < count) or not (0 <= target < MAX_TOOLS):
            return False
        if target < count:
            self._keys[source], self._keys[target] = \
                self._keys[target], self._keys[source]
        else:
            self._keys.append(self._keys.pop(source))
        self.refresh()
        self.changed.emit()
        return True

    def _on_slot_dropped(self, source: int, target: int) -> None:
        """格子被拖到另一格上（`_SlotButton.dropped`，已在主线程）。"""
        self.move_slot(source, target)

    # ---------- 交互 ----------
    def _on_slot(self, index: int) -> None:
        if index < len(self._keys):
            self._slot_menu(self._keys[index])
        else:
            self._pick_menu()

    def _slot_menu(self, key: str) -> None:
        entry = find_entry(key, self.custom_tools())
        if entry is None:
            self.remove(key)            # 兜底：认不出的 key 直接清掉
            return
        menu = QMenu(self)
        label = ("打开「%s」" % entry.name) if not entry.is_custom else \
            ("运行「%s」" % entry.name)
        open_action = menu.addAction(label)
        remove_action = menu.addAction("从九宫格移除")
        chosen = menu.exec(self._slots[self._keys.index(key)].mapToGlobal(
            self._slots[self._keys.index(key)].rect().bottomLeft()))
        if chosen is open_action:
            open_entry(entry, anchor=self.window())
        elif chosen is remove_action:
            self.remove(key)

    def _pick_menu(self) -> None:
        used = set(self._keys)
        candidates = [entry for entry in self.entries() if entry.key not in used]
        if not candidates:
            QMessageBox.information(self, "九宫格工具", "没有更多可添加的工具了。")
            return
        menu = QMenu(self)
        actions = {}
        for entry in candidates:
            action = menu.addAction(entry_icon(entry, 16), entry.name)
            actions[action] = entry.key
        chosen = menu.exec(self._slots[min(len(self._keys), MAX_TOOLS - 1)]
                           .mapToGlobal(
            self._slots[min(len(self._keys), MAX_TOOLS - 1)].rect().bottomLeft()))
        if chosen in actions:
            self.assign(actions[chosen])
