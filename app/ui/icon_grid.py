"""菜单图标的「宫格」选择器。

为什么不用 QComboBox：图标是**视觉信息**，下拉列表一次只露出一项，用户得逐个翻着找；
宫格把全部预设一次铺开、点一下就选中，更贴合「挑图标」这个动作。

实现要点：
- 每格是 checkable 的 QToolButton（图标在上、名称在下），交给 QButtonGroup 互斥，
  选中态用 `:checked` 样式表现，不需要自己记账。
- 「无图标」也是其中一格（透明占位图标 + 名称），保证用户能明确地选回「不设图标」，
  而不是靠「清空」这种隐晦操作。
- 预设会持续增加，所以外面套一层 QScrollArea 并限高，避免把对话框越撑越高；
  高度按行数算，够放就全放，放不下才滚动。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QScrollArea,
                               QToolButton, QVBoxLayout, QWidget)

from .middle_menu_icons import PRESET_ICONS, blank_icon, icon_for

ICON_GRID_COLUMNS = 8          # 每行几个格子
TILE_W, TILE_H = 52, 50        # 单个格子尺寸
TILE_ICON = 18                 # 格子里的图标画多大
TILE_GAP = 3                   # 格子间距
MAX_GRID_HEIGHT = 318          # 宫格区域最大高度（约 6 行），超出就滚动

# 选中态用项目主色（#1668a8），与其余页面保持一致
_PICKER_QSS = """
QWidget#iconPicker QToolButton {
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 0px;
    color: #24292f;
}
QWidget#iconPicker QToolButton:hover {
    background-color: #e8f1fa;
}
QWidget#iconPicker QToolButton:checked {
    background-color: #e8f1fa;
    border: 1px solid #1668a8;
    color: #1668a8;
}
"""


class IconPicker(QWidget):
    """宫格图标选择器。

    对外只暴露三件事：`selected_key()` 取 key、`set_selected_key()` 回填、
    `selectionChanged` 通知变化。key 为空串表示「无图标」。
    """

    selectionChanged = Signal(str)

    def __init__(self, parent=None, columns: int = ICON_GRID_COLUMNS):
        super().__init__(parent)
        self.setObjectName("iconPicker")
        self.setStyleSheet(_PICKER_QSS)
        self._key_of_btn: dict[QToolButton, str] = {}
        self._btn_of_key: dict[str, QToolButton] = {}
        self.buttons: list[QToolButton] = []      # 宫格里的全部格子，按预设顺序
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._build(max(1, int(columns)))

    # ---------- 构建 ----------
    def _build(self, columns: int) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(TILE_GAP)

        for idx, (key, _emoji, name) in enumerate(PRESET_ICONS):
            btn = QToolButton()
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            btn.setIconSize(QSize(TILE_ICON, TILE_ICON))
            btn.setFixedSize(TILE_W, TILE_H)
            btn.setText(name)
            btn.setToolTip(name)
            # 「无图标」用透明占位图标：既留出图标位保证各格文字对齐，
            # 又不会真的显示一个图标。
            btn.setIcon(icon_for(key) if key else blank_icon())
            btn.toggled.connect(self._toggle_handler(key))
            self._group.addButton(btn)
            self._key_of_btn[btn] = key
            self._btn_of_key.setdefault(key, btn)
            self.buttons.append(btn)
            grid.addWidget(btn, idx // columns, idx % columns)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(holder)
        area.setFixedHeight(self._height_for(len(PRESET_ICONS), columns))
        self.area = area
        root.addWidget(area)

    def _toggle_handler(self, key: str):
        def handler(checked: bool) -> None:
            if checked:
                self.selectionChanged.emit(key)
        return handler

    @staticmethod
    def _height_for(count: int, columns: int) -> int:
        rows = max(1, math.ceil(count / columns))
        want = rows * TILE_H + (rows - 1) * TILE_GAP
        return min(want, MAX_GRID_HEIGHT)

    # ---------- 取值 / 回填 ----------
    def icon_keys(self) -> list[str]:
        """宫格里的全部图标 key，顺序与预设一致。"""
        return [self._key_of_btn[b] for b in self.buttons]

    def selected_key(self) -> str:
        btn = self._group.checkedButton()
        return self._key_of_btn.get(btn, "") if btn is not None else ""

    def selected_name(self) -> str:
        """当前选中项的中文名（含「无图标」）；无选中时返回空串。"""
        btn = self._group.checkedButton()
        return btn.text() if btn is not None else ""

    def set_selected_key(self, key: str) -> None:
        """回填选中项；配置里是个已不存在的 key 时退回「无图标」。"""
        btn = self._btn_of_key.get((key or "").strip())
        if btn is None:
            btn = self._btn_of_key[""]
        btn.setChecked(True)
