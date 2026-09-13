"""中键菜单页：管理「鼠标中键弹出的快捷菜单」里的菜单项。

作用范围与触发条件：程序运行期间**系统范围内**监听鼠标，用户松开鼠标中键
时在光标处弹出本页配置的菜单；每个菜单项关联一个已实现好的流程，点击条目
即运行对应流程（走 FlowTab.start_flow_if_idle，已在运行则跳过）。

本页负责：菜单项的增删改与排序、菜单总开关、是否拦截中键。
数据存于 AppConfig.middle_menu_items；运行时的菜单构造见 build_menu()。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from ..config import AppConfig, Flow, MiddleMenuItem
from .middle_menu_dialog import MiddleMenuDialog
from .middle_menu_icons import ICON_SIZE, blank_icon, icon_for
from .widgets import set_variant

_ROLE_ID = Qt.UserRole

# 弹出菜单样式：文字左对齐、只留一点左边距。
# 为什么要显式指定：QMenu 默认（Qt 会在左侧预留图标/勾选列）会把文字推到约 28px 处，
# 菜单又窄，看上去像居中而不是靠左。这里用内联样式接管渲染，让文字紧贴左侧
# 12px 处，并统一 hover 高亮与分隔线，与主界面配色（#1668a8 / #e8f1fa）保持一致。
# QMenu::icon 的 left 必须与 QMenu::item 的 padding-left 相同：图标占的就是这块空隙，
# 两者不一致时「有图标的条目」和「留空的条目」文字左边缘会错开。
_MENU_QSS = """
QMenu {
    background-color: #ffffff;
    border: 1px solid #d8dee4;
    border-radius: 6px;
    padding: 4px 0px;
}
QMenu::item {
    padding: 6px 24px 6px 12px;
    color: #24292f;
    background-color: transparent;
}
QMenu::icon {
    left: 12px;
    width: 16px;
    height: 16px;
}
QMenu::item:selected {
    background-color: #e8f1fa;
    color: #1668a8;
}
QMenu::item:disabled {
    color: #aab2bb;
}
QMenu::separator {
    height: 1px;
    background-color: #eef1f4;
    margin: 4px 10px;
}
"""


def build_menu(items, flows, parent=None) -> "QMenu | None":
    """按菜单项列表构造运行时弹出的 QMenu；无可展示条目时返回 None。

    关联流程已被删除的菜单项在运行时直接跳过（管理页会用红字提示去修）；
    返回的 QMenu 只把条目摆好，动作连接由调用方负责。
    """
    by_id = {f.id: f for f in flows or []}
    entries = [(it, by_id[it.flow_id]) for it in (items or []) if it.flow_id in by_id]
    if not entries:
        return None
    # 图标是「可选」的：先把 key 解析成真实图标（认不出的 key 会解析成空图标），
    # 只要有**一条真能画出图标**，就给没图标的条目补一个透明占位图标，否则 Qt 只让
    # 带图标的那几条让出图标列，两行文字左边缘会对不齐。
    # 注意这里必须按「解析后的图标」判断，不能按 it.icon 字符串判断：配置里写了
    # 个不认识的 key 时字符串非空但画不出图标，若以此判定会把整份菜单带进图标
    # 模式，白白把文字右移一列。
    resolved = [icon_for(it.icon) for it, _ in entries]
    mixed_icons = any(not ic.isNull() for ic in resolved)
    menu = QMenu(parent)
    menu.setStyleSheet(_MENU_QSS)
    menu.setToolTipsVisible(True)      # 悬停显示所关联流程名（自定义名称时尤其有用）
    for idx, ((it, flow), icon) in enumerate(zip(entries, resolved)):
        if it.separator_before and idx > 0:      # 首条目的分隔线没有意义，跳过
            menu.addSeparator()
        action = menu.addAction(it.display_label(flow))
        if icon.isNull() and mixed_icons:
            icon = blank_icon()
        if not icon.isNull():
            action.setIcon(icon)
        action.setData(flow.id)
        action.setToolTip(f"运行流程：{flow.name}")
    return menu


class MiddleMenuTab(QWidget):
    """中键菜单管理页。"""

    changed = Signal()          # 菜单项或开关发生变化，主窗口据此保存并重配置

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._items = cfg.middle_menu_items
        self._build_ui()
        self.refresh_list()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.setObjectName("middleMenuTab")
        self.setStyleSheet("""
            QWidget#middleMenuTab { background: #f7f9fb; }
            QWidget#middleMenuTab QListWidget#middleMenuList {
                font-size: 10pt; outline: none;
                border: 1px solid #d8dee4; border-radius: 6px; background: white;
            }
            QWidget#middleMenuTab QListWidget#middleMenuList::item {
                height: 32px; padding: 2px 8px;
                border-bottom: 1px solid #f0f3f6;
            }
            QWidget#middleMenuTab QListWidget#middleMenuList::item:hover {
                background-color: #e8f1fa;
            }
            QWidget#middleMenuTab QListWidget#middleMenuList::item:selected {
                background-color: #1668a8; color: white;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        top = QHBoxLayout()
        title = QLabel("📋 中键菜单")
        title.setStyleSheet("font-size: 13pt; font-weight: 600; color: #24292f;")
        top.addWidget(title)
        hint = QLabel("在任意位置按鼠标中键弹出下面的菜单，点击条目即运行对应流程。")
        hint.setStyleSheet("color:#8a939c;")
        top.addWidget(hint)
        top.addStretch(1)

        # 开关先设好初值再连信号，避免构造期误发 changed
        self.enable_check = QCheckBox("启用中键菜单")
        self.enable_check.setToolTip("关闭后按中键不再弹出菜单")
        self.enable_check.setChecked(bool(self.cfg.middle_menu_enabled))
        self.suppress_check = QCheckBox("拦截中键")
        self.suppress_check.setToolTip(
            "勾选后中键不会传给目标窗口（例如浏览器不再触发中键自动滚动）")
        self.suppress_check.setChecked(bool(self.cfg.middle_menu_suppress))
        top.addWidget(self.enable_check)
        top.addWidget(self.suppress_check)
        root.addLayout(top)

        bar = QHBoxLayout()
        self.add_btn = QPushButton("＋ 添加菜单项")
        self.add_btn.setToolTip("新增一个菜单项，并关联一个已实现好的流程")
        set_variant(self.add_btn, "primary")
        self.add_btn.clicked.connect(self._add_item)
        bar.addWidget(self.add_btn)

        self.edit_btn = QPushButton("编辑")
        self.edit_btn.setToolTip("修改所选菜单项的名称 / 关联流程 / 分隔线")
        set_variant(self.edit_btn, "primary")
        self.edit_btn.clicked.connect(self._edit_item)
        bar.addWidget(self.edit_btn)

        self.del_btn = QPushButton("删除")
        set_variant(self.del_btn, "danger")
        self.del_btn.clicked.connect(self._del_item)
        bar.addWidget(self.del_btn)

        bar.addSpacing(12)
        self.up_btn = QPushButton("↑ 上移")
        self.up_btn.setToolTip("在菜单里往上挪一位")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        bar.addWidget(self.up_btn)
        self.down_btn = QPushButton("↓ 下移")
        self.down_btn.setToolTip("在菜单里往下挪一位")
        self.down_btn.clicked.connect(lambda: self._move(1))
        bar.addWidget(self.down_btn)
        bar.addStretch(1)

        self.preview_btn = QPushButton("预览菜单")
        self.preview_btn.setToolTip("在鼠标位置预览菜单外观（预览中点击条目不会运行流程）")
        self.preview_btn.clicked.connect(self._preview)
        bar.addWidget(self.preview_btn)
        root.addLayout(bar)

        self.list = QListWidget()
        self.list.setObjectName("middleMenuList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setContextMenuPolicy(Qt.NoContextMenu)
        self.list.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.list.itemDoubleClicked.connect(lambda *_: self._edit_item())
        self.list.currentRowChanged.connect(lambda *_: self._sync_buttons())
        root.addWidget(self.list, 1)

        self.empty_hint = QLabel(
            "还没有菜单项。点「＋ 添加菜单项」，为每个菜单项选择一个流程即可。")
        self.empty_hint.setStyleSheet("color:#8a939c; padding:4px 2px;")
        root.addWidget(self.empty_hint)

        # 信号在初值设置之后才连接
        self.enable_check.toggled.connect(self._on_enable_toggled)
        self.suppress_check.toggled.connect(self._on_suppress_toggled)

    # ---------- 列表 ----------
    def refresh_list(self) -> None:
        row = self.list.currentRow()
        by_id = {f.id: f for f in self.cfg.flows}
        # 同 build_menu：按解析后的图标判断是否需要占位列，认不出的 key 不该把
        # 整列图标撑出来（否则文字会整体右移一列）。
        icons = [icon_for(it.icon) for it in self._items]
        mixed_icons = any(not ic.isNull() for ic in icons)
        self.list.blockSignals(True)
        self.list.clear()
        for it, icon in zip(self._items, icons):
            flow = by_id.get(it.flow_id)
            entry = QListWidgetItem(self._item_text(it, flow))
            if icon.isNull() and mixed_icons:
                icon = blank_icon()
            if not icon.isNull():
                entry.setIcon(icon)
            if flow is None:
                entry.setForeground(QColor("#d64541"))
                entry.setToolTip("原关联流程已被删除，请点「编辑」重新选择流程")
            else:
                tips = [f"点击后运行流程：{flow.name}"]
                if it.separator_before:
                    tips.append("该菜单项上方显示一条分隔线")
                entry.setToolTip("\n".join(tips))
            entry.setData(_ROLE_ID, it.id)
            self.list.addItem(entry)
        self.list.blockSignals(False)

        if self.list.count():
            self.list.setCurrentRow(row if 0 <= row < self.list.count() else 0)
        self.empty_hint.setVisible(self.list.count() == 0)
        self._sync_buttons()

    @staticmethod
    def _item_text(it: MiddleMenuItem, flow: "Flow | None") -> str:
        """列表行文本：自定义名称 + 流程名；断链时红字提示。"""
        if flow is None:
            return f"⚠ {it.display_label(None)}    ·    关联流程已不存在，请编辑重新选择"
        text = it.display_label(flow)
        prefix = "— " if it.separator_before else ""
        if it.label.strip():
            return f"{prefix}{text}    ·    流程：{flow.name}"
        return f"{prefix}{flow.name}"

    def _sync_buttons(self) -> None:
        row = self.list.currentRow()
        has = row >= 0
        self.edit_btn.setEnabled(has)
        self.del_btn.setEnabled(has)
        self.up_btn.setEnabled(has and row > 0)
        self.down_btn.setEnabled(has and row < self.list.count() - 1)
        self.preview_btn.setEnabled(bool(build_menu(self._items, self.cfg.flows)))

    def _selected(self) -> "MiddleMenuItem | None":
        entry = self.list.currentItem()
        if entry is None:
            return None
        iid = entry.data(_ROLE_ID)
        return next((x for x in self._items if x.id == iid), None)

    def _select_by_id(self, item_id: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(_ROLE_ID) == item_id:
                self.list.setCurrentRow(i)
                return

    # ---------- 增删改与排序 ----------
    def _add_item(self) -> None:
        if not self.cfg.flows:
            QMessageBox.information(
                self, "还没有流程",
                "请先在「🚀 自动化流程」页创建流程，再回来添加菜单项。")
            return
        dlg = MiddleMenuDialog(None, self.cfg.flows, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        item = dlg.result_item()
        self._items.append(item)
        self._after_change(item.id)

    def _edit_item(self) -> None:
        cur = self._selected()
        if cur is None:
            self._status("请先选择一个菜单项")
            return
        dlg = MiddleMenuDialog(cur, self.cfg.flows, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dlg.result_item()       # 就地写回 cur
        self._after_change(cur.id)

    def _del_item(self) -> None:
        cur = self._selected()
        if cur is None:
            self._status("请先选择一个菜单项")
            return
        by_id = {f.id: f for f in self.cfg.flows}
        name = cur.display_label(by_id.get(cur.flow_id))
        if QMessageBox.question(self, "删除菜单项",
                                f"确定删除菜单项「{name}」吗？") != QMessageBox.Yes:
            return
        self._items.remove(cur)
        self._after_change()

    def _move(self, delta: int) -> None:
        cur = self._selected()
        if cur is None:
            return
        i = self._items.index(cur)
        j = i + delta
        if not (0 <= j < len(self._items)):
            return
        self._items[i], self._items[j] = self._items[j], self._items[i]
        self._after_change(cur.id)

    def _after_change(self, select_id: str = "") -> None:
        self.cfg.middle_menu_items = self._items
        self.refresh_list()
        if select_id:
            self._select_by_id(select_id)
        self._sync_buttons()
        self.changed.emit()

    # ---------- 开关 ----------
    def _on_enable_toggled(self, checked: bool) -> None:
        self.cfg.middle_menu_enabled = bool(checked)
        self.changed.emit()

    def _on_suppress_toggled(self, checked: bool) -> None:
        self.cfg.middle_menu_suppress = bool(checked)
        self.changed.emit()

    # ---------- 预览 / 外部联动 ----------
    def _preview(self) -> None:
        menu = build_menu(self._items, self.cfg.flows, self)
        if menu is None:
            self._status("还没有可用的菜单项（关联流程可能已被删除）")
            return
        menu.exec(QCursor.pos())

    def on_flows_changed(self) -> None:
        """流程增删改后刷新：菜单项名称与断链提示都要跟着更新。"""
        self.refresh_list()

    def _status(self, text: str) -> None:
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, 4000)
