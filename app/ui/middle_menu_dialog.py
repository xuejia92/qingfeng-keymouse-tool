"""中键菜单项编辑对话框：显示名称 + 图标 + 关联流程 + 分隔线。

菜单项必须关联一个「已经实现好的流程」；显示名称留空时直接用流程名，
这样流程改名后菜单文字自动跟随，无需再回来改菜单项。图标可选，用**宫格**
从预设里点选一格，或选第一格「无图标」留空。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QLabel, QLineEdit, QMessageBox,
                               QVBoxLayout, QWidget)

from ..config import Flow, MiddleMenuItem
from .icon_grid import IconPicker

# 编辑一个「原流程已被删除」的菜单项时，流程下拉里插入的占位项
_MISSING_FLOW_TEXT = "⚠ 原流程已删除，请重新选择"
# 新建菜单项时「在该项上方显示一条分隔线」默认勾选
_NEW_ITEM_SEPARATOR_DEFAULT = True


class MiddleMenuDialog(QDialog):
    """新建 / 编辑单个中键菜单项。"""

    def __init__(self, item: MiddleMenuItem | None, flows: list[Flow], parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建菜单项" if item is None else "编辑菜单项")
        self.setMinimumWidth(580)
        # 新建项默认带分隔线（用户偏好）；编辑时完全按已有值回填
        self._item = item or MiddleMenuItem(separator_before=_NEW_ITEM_SEPARATOR_DEFAULT)
        self._flows = list(flows or [])
        self._build()
        self._fill()

    # ---------- UI ----------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空则直接显示所关联流程的名称")
        self.name_edit.setMaxLength(50)
        form.addRow("显示名称", self.name_edit)

        # 图标用宫格选，比下拉列表更直观：全部预设一次铺开，点一下就选中。
        self.icon_picker = IconPicker()
        self.icon_picker.selectionChanged.connect(self._on_icon_changed)
        icon_box = QWidget()
        icon_lay = QVBoxLayout(icon_box)
        icon_lay.setContentsMargins(0, 0, 0, 0)
        icon_lay.setSpacing(4)
        icon_lay.addWidget(self.icon_picker)
        self.icon_hint = QLabel()
        self.icon_hint.setStyleSheet("color:#8a939c;")
        icon_lay.addWidget(self.icon_hint)
        form.addRow("菜单图标", icon_box)

        self.flow_combo = QComboBox()
        for f in self._flows:
            label = f"{f.group} · {f.name}" if f.group else f.name
            self.flow_combo.addItem(label, f.id)
        form.addRow("关联流程", self.flow_combo)
        root.addLayout(form)

        self.sep_check = QCheckBox("在该项上方显示一条分隔线")
        root.addWidget(self.sep_check)

        tip = QLabel("点击该菜单项会运行所关联的流程；请确保该流程已有步骤。")
        tip.setStyleSheet("color:#8a939c;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("确定")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    def _fill(self) -> None:
        self.name_edit.setText(self._item.label)
        self.icon_picker.set_selected_key(self._item.icon)
        self._on_icon_changed(self.icon_picker.selected_key())
        idx = self.flow_combo.findData(self._item.flow_id)
        if idx >= 0:
            self.flow_combo.setCurrentIndex(idx)
        elif self._item.flow_id:
            # 原关联流程已不存在：插一个空数据的占位项，逼用户重新选，
            # 避免「静默落到下拉第一项」把菜单项悄悄改绑到别的流程。
            self.flow_combo.insertItem(0, _MISSING_FLOW_TEXT, "")
            self.flow_combo.setCurrentIndex(0)
        self.sep_check.setChecked(bool(self._item.separator_before))

    def _on_icon_changed(self, _key: str = "") -> None:
        """宫格里选中的项在格子上有高亮，但格子小、名字也短，再给一行文字提示落定。"""
        if self.icon_picker.selected_key():
            self.icon_hint.setText(f"当前选择：{self.icon_picker.selected_name()}")
        else:
            self.icon_hint.setText("当前选择：无图标（该菜单项左侧留空）")

    # ---------- 校验与结果 ----------
    def accept(self) -> None:   # noqa: N802 (Qt 命名)
        if not self._flows:
            QMessageBox.information(self, "还没有流程",
                                    "请先在「🚀 自动化流程」页创建流程，再来添加菜单项。")
            return
        if not self.flow_combo.currentData():
            QMessageBox.information(self, "请选择流程",
                                    "每个菜单项都需要关联一个流程。")
            return
        super().accept()

    def result_item(self) -> MiddleMenuItem:
        """把界面上的值写回菜单项对象（就地修改并返回）。"""
        item = self._item
        item.label = self.name_edit.text().strip()[:50]
        item.icon = str(self.icon_picker.selected_key() or "")[:20]
        idx = self.flow_combo.currentIndex()
        item.flow_id = str(self.flow_combo.currentData() or "")[:32]
        if 0 <= idx < self.flow_combo.count():
            # 只有真实流程项才更新冗余流程名（占位项 data 为空，不覆盖）
            if item.flow_id:
                item.flow_name = self._flow_name_of(item.flow_id)
        item.separator_before = bool(self.sep_check.isChecked())
        return item

    def _flow_name_of(self, flow_id: str) -> str:
        for f in self._flows:
            if f.id == flow_id:
                return f.name
        return self._item.flow_name
