"""中键菜单页：管理「快捷菜单」里的菜单项，并设置它的触发方式。

触发方式有两种，互相独立、可只开一种也可都开（见本页顶部提示）：
1. **鼠标中键**——程序运行期间系统范围内监听，松开中键时在光标处弹出菜单；
   关闭后不装鼠标钩子（鼠标拖动更顺，见 mouse_menu.py 的 GIL 说明）。
2. **全局快捷键**——用户自己录制的组合键，按下即在光标处弹出菜单。

每个菜单项关联一个已实现好的流程，点击条目即运行对应流程
（走 FlowTab.start_flow_if_idle，已在运行则跳过）。

本页负责：菜单项的增删改与排序、两种触发方式的开关与快捷键。
数据存于 AppConfig.middle_menu_*；运行时的菜单构造见 build_menu()。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget, QWidgetAction)

from .. import hotkey_policy
from . import theme
from ..config import AppConfig, Flow, MiddleMenuItem
from ..keymap import hotkey_display
from .hotkey_edit import HotkeyEdit
from .middle_menu_dialog import MiddleMenuDialog
from .middle_menu_icons import ICON_SIZE, blank_icon, icon_for
from .middle_menu_tools import ToolGridEditor, ToolGridWidget, resolve_tools
from .widgets import set_variant

_ROLE_ID = Qt.UserRole

# 弹出菜单样式：文字左对齐、只留一点左边距。
# 为什么要显式指定：QMenu 默认（Qt 会在左侧预留图标/勾选列）会把文字推到约 28px 处，
# 菜单又窄，看上去像居中而不是靠左。这里用内联样式接管渲染，让文字紧贴左侧
# 12px 处，并统一 hover 高亮与分隔线。
# QMenu::icon 的 left 必须与 QMenu::item 的 padding-left 相同：图标占的就是这块空隙，
# 两者不一致时「有图标的条目」和「留空的条目」文字左边缘会错开。
# 颜色走**主题令牌**（2026-10-03 改）：以前是写死的浅色，深色主题下菜单会白得刺眼；
# 令牌在弹菜单那一刻求值（不是模块导入时），所以换主题后下一次弹出就是新配色。
def menu_qss() -> str:
    tk = theme.token
    return f"""
QMenu {{
    background-color: {tk('card_bg')};
    border: 1px solid {tk('border')};
    border-radius: 6px;
    padding: 4px 0px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    color: {tk('text')};
    background-color: transparent;
}}
QMenu::icon {{
    left: 12px;
    width: 16px;
    height: 16px;
}}
QMenu::item:selected {{
    background-color: {tk('primary_soft')};
    color: {tk('primary')};
}}
QMenu::item:disabled {{
    color: {tk('text_muted')};
}}
QMenu::separator {{
    height: 1px;
    background-color: {tk('border_light')};
    margin: 4px 10px;
}}
"""


def build_menu(items, flows, parent=None, tools=None, on_tool=None) -> "QMenu | None":
    """按菜单项列表构造运行时弹出的 QMenu；无可展示条目时返回 None。

    菜单分两段（2026-10-03 重新设计）：
    - **上面**：九宫格工具（`tools` 里的小程序 key，最多 9 格，3 列）——
      `on_tool(key)` 由调用方负责打开小程序并关菜单；
    - **下面**：关联流程的菜单项，点一下运行对应流程。

    关联流程已被删除的菜单项在运行时直接跳过（管理页会用红字提示去修）；
    返回的 QMenu 只把条目摆好，动作连接由调用方负责。

    `tools` 为空（或全部 key 都认不出来）时**整段不插入**，菜单与改造前完全一致。
    """
    by_id = {f.id: f for f in flows or []}
    entries = [(it, by_id[it.flow_id]) for it in (items or []) if it.flow_id in by_id]
    grid_keys = [key for key, _app in resolve_tools(tools or [])]
    if not entries and not grid_keys:
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
    menu.setStyleSheet(menu_qss())
    menu.setToolTipsVisible(True)      # 悬停显示所关联流程名（自定义名称时尤其有用）

    # ---- 上段：九宫格工具 ----
    if grid_keys:
        grid = ToolGridWidget(grid_keys, on_tool or (lambda _key: None))
        holder = QWidgetAction(menu)
        holder.setDefaultWidget(grid)
        menu.addAction(holder)
        if entries:
            menu.addSeparator()        # 与下面的流程条目之间拉一条分隔线

    # ---- 下段：关联流程 ----
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
        # 页内不再重复标题：本页已被「⚡ 快捷操作」页托管，左侧导航里就写着「📋 中键菜单」
        # （与「小工具」页去掉页内标题同一处理，2026-10-03）
        hint = QLabel("按鼠标中键或下面设置的快捷键，即可在鼠标位置弹出菜单；点击条目运行对应流程。")
        hint.setStyleSheet("color:#8a939c;")
        top.addWidget(hint)
        top.addStretch(1)

        # 开关先设好初值再连信号，避免构造期误发 changed
        self.enable_check = QCheckBox("鼠标中键触发")
        self.enable_check.setToolTip(
            "勾选后按鼠标中键弹出菜单；取消勾选则只认快捷键。\n"
            "取消勾选会卸掉鼠标钩子——鼠标输入更顺滑（拖动不再发涩）。")
        self.enable_check.setChecked(bool(self.cfg.middle_menu_enabled))
        self.suppress_check = QCheckBox("拦截中键")
        self.suppress_check.setToolTip(
            "勾选后中键不会传给目标窗口（例如浏览器不再触发中键自动滚动）。\n"
            "仅在勾选了「鼠标中键触发」时有意义。")
        self.suppress_check.setChecked(bool(self.cfg.middle_menu_suppress))
        top.addWidget(self.enable_check)
        top.addWidget(self.suppress_check)
        root.addLayout(top)

        # 快捷键触发：用户自己录制；Esc 清除。走统一的冲突检测（槽位 middle_menu）
        trigger = QHBoxLayout()
        trigger.addWidget(QLabel("快捷键触发："))
        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setMaximumWidth(180)
        self.hotkey_edit.set_conflict_checker(
            lambda hk: hotkey_policy.check(hk, "middle_menu"))
        self.hotkey_edit.set_hotkey(self.cfg.middle_menu_hotkey)
        self.hotkey_edit.setToolTip(
            "点击输入框后按下组合键即完成设置；按 Esc 清除。\n"
            "留空则只能用鼠标中键触发。与其它页的热键重复时会提示。")
        trigger.addWidget(self.hotkey_edit)
        self.trigger_state = QLabel()
        self.trigger_state.setWordWrap(True)
        trigger.addWidget(self.trigger_state, 1)
        root.addLayout(trigger)

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

        # 九宫格工具（2026-10-03）：中键菜单最上面那 3×3 格，最多 9 个小工具。
        # 放在流程列表**上方**，与弹出菜单里「上面宫格、下面流程」的顺序一致。
        self.tools_editor = ToolGridEditor(self.cfg.middle_menu_tools)
        self.tools_editor.changed.connect(self._on_tools_changed)
        root.addWidget(self.tools_editor)

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
        self.hotkey_edit.hotkeyChanged.connect(self._on_hotkey_changed)
        self._refresh_trigger_state()

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
                entry.setForeground(QColor(theme.token("danger")))
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

    def _on_tools_changed(self) -> None:
        """九宫格工具增删：写回配置并落盘（菜单下次弹出即生效，无需重启）。"""
        self.cfg.middle_menu_tools = self.tools_editor.keys()
        self.changed.emit()

    # ---------- 开关 / 触发方式 ----------
    def _on_enable_toggled(self, checked: bool) -> None:
        self.cfg.middle_menu_enabled = bool(checked)
        self._refresh_trigger_state()
        self.changed.emit()

    def _on_suppress_toggled(self, checked: bool) -> None:
        self.cfg.middle_menu_suppress = bool(checked)
        self.changed.emit()

    def _on_hotkey_changed(self, hotkey: str) -> None:
        self.cfg.middle_menu_hotkey = (hotkey or "").strip().lower()
        self._refresh_trigger_state()
        self.changed.emit()

    def _refresh_trigger_state(self) -> None:
        """触发方式说明：两种方式各自独立，必须让用户看清「现在到底怎么弹菜单」。

        最容易踩的坑是两种方式都没开——菜单静默失效、按什么都没反应，
        所以这种情况用红字明确警告。
        """
        by_mouse = bool(self.cfg.middle_menu_enabled)
        hotkey = (self.cfg.middle_menu_hotkey or "").strip()
        shown = hotkey_display(hotkey)
        if by_mouse and hotkey:
            text, color = f"鼠标中键与 {shown} 都可以弹出菜单", "#8a939c"
        elif by_mouse:
            text, color = "当前只用鼠标中键触发（未设快捷键）", "#8a939c"
        elif hotkey:
            text, color = (f"当前只用 {shown} 触发；"
                           "已卸掉鼠标钩子，鼠标输入更顺滑"), "#8a939c"
        else:
            text, color = ("⚠ 两种触发方式都没启用，菜单不会弹出——"
                           "请勾选「鼠标中键触发」或设置一个快捷键"), "#d64541"
        self.trigger_state.setText(text)
        self.trigger_state.setStyleSheet(f"color: {color};")
        self.suppress_check.setEnabled(by_mouse)

    # ---------- 预览 / 外部联动 ----------
    def _preview(self) -> None:
        """预览菜单外观（提示里已说明：预览中点条目不会运行流程，工具也不会打开）。"""
        menu = build_menu(self._items, self.cfg.flows, self,
                          tools=self.cfg.middle_menu_tools,
                          on_tool=lambda _key: menu.close())
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
