"""自动化流程页：左栏流程列表（新建/编辑只管元信息），右栏实时编排模块。

右栏即编辑器：模块面板拖入步骤列表、列表内拖拽排序、双击改参数、删除步骤，
全部实时写回所选流程并自动保存；每行右侧「▶ 执行」可单步试运行该模块；
所选流程运行中时右栏锁定（防执行中改动）。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFileDialog, QFrame, QGroupBox, QHBoxLayout, QInputDialog,
                               QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..config import (AppConfig, BASE_DIR, FLOW_STEP_TYPES, FLOWS_DIR, Flow,
                      FlowStep, default_step_params, flow_from_file, flow_to_dict,
                      repair_web_pairs, safe_filename, web_action)
from ..flows import FlowRunner
from ..keymap import hotkey_display
from ..logbus import log
from .flow_dialog import (FlowMetaDialog, ModuleButton, StepList, StepRunDelegate,
                          StepParamsDialog, _TYPE_ICONS)
from .widgets import set_variant


# 模块面板分组：组 id -> (分组标题, 模块类型列表)。顺序即面板显示顺序，
# 组 id 同时用于持久化收起状态（config.json 的 collapsed_module_groups）。
MODULE_GROUPS = [
    ("input",    "键鼠操作",   ["click", "press", "find"]),
    ("perceive", "目标识别",   ["ocr", "text_find", "screenshot"]),
    ("app_web",  "应用与网页", ["app", "close_app", "web"]),
    ("logic",    "变量与日志", ["var", "wait", "log", "clip_set", "clip_get"]),
]


def _clone_flow(flow: Flow) -> Flow:
    """流程深拷贝，交给后台执行线程。

    为什么不能直接 dataclasses.replace(flow)：它只做浅拷贝，flow.steps 这个列表
    对象仍与界面共享。现在是靠「运行中锁定右栏编辑」的约定在保护，属于约定而非
    机制；一旦将来出现任何后台改动流程的路径（在线同步、定时触发、导入覆盖），
    界面动一步，正在跑的流程就跟着变了。这里深拷贝一次，把边界钉死。
    """
    return copy.deepcopy(flow)


class FlowTab(QWidget):
    changed = Signal()               # 流程配置增删改
    runningStateChanged = Signal()   # 任一流程运行状态变化
    flowStarted = Signal()           # 有流程开始运行（含单步执行），供主窗口清空日志

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._flows = cfg.flows
        # _runners 只登记「正在运行」的 runner：流程一结束就摘掉。
        # 原来只增不减，删掉流程后条目还留着，反复增删会让它一直变大。
        self._runners: dict[str, FlowRunner] = {}
        self._single_rows: dict[str, int] = {}   # flow_id -> 单步执行中的真实步骤行号
        self._capture_step_dlg = None
        self._ratio_applied = False
        self._build_ui()
        self.refresh_list()

    # ---------- UI ----------
    def _build_ui(self):
        self.setObjectName("flowTab")
        self.setStyleSheet("""
            QWidget#flowTab { background: #f7f9fb; }
            QWidget#flowTab QListWidget {
                font-size: 11pt; outline: none;
                border: 1px solid #d8dee4; border-radius: 6px;
                background: white;
            }
            QWidget#flowTab QListWidget::item { padding: 4px 8px; }
            QWidget#flowTab QListWidget::item:hover { background-color: #e8f1fa; }
            QWidget#flowTab QListWidget::item:selected {
                background-color: #1668a8; color: white;
            }
            QWidget#flowTab QTreeWidget#flowList {
                font-size: 10pt; outline: none;
                border: 1px solid #d8dee4; border-radius: 6px;
                background: white;
            }
            QWidget#flowTab QTreeWidget#flowList::item {
                height: 30px; padding: 2px 6px;
                border-bottom: 1px solid #f0f3f6;
            }
            QWidget#flowTab QTreeWidget#flowList::item:hover { background-color: #e8f1fa; }
            QWidget#flowTab QTreeWidget#flowList::item:selected {
                background-color: #1668a8; color: white;
            }
            QWidget#flowTab QTreeWidget#flowList::branch { background: transparent; }
            /* 分组头：浅灰蓝底、左对齐蓝色加粗，与流程条目区分 */
            QWidget#flowTab QTreeWidget#flowList QPushButton[groupHeader="true"] {
                text-align: left; padding: 3px 8px;
                font-size: 10pt; font-weight: 600; color: #1668a8;
                border: none; border-radius: 4px; background: #e9f0f8;
            }
            QWidget#flowTab QTreeWidget#flowList QPushButton[groupHeader="true"]:hover {
                background: #dce8f4;
            }
            QWidget#flowTab QTreeWidget#flowList QPushButton {
                text-align: center; padding: 3px 8px;
                font-size: 10pt; color: #1668a8;
                border: none; border-radius: 4px; background: transparent;
            }
            QWidget#flowTab QTreeWidget#flowList QPushButton:hover { background: #e9f0f8; }
            QWidget#flowTab QListWidget#stepView::item { height: 38px; }
            QWidget#flowTab QGroupBox {
                border: 1px solid #d8dee4; border-radius: 6px;
                background: white; margin-top: 10px; font-weight: 600;
            }
            QWidget#flowTab QGroupBox::title { subcontrol-origin: margin; left: 10px; }
            QWidget#flowTab QGroupBox#modulePanel QPushButton {
                text-align: left; padding: 5px 12px;
                font-size: 10pt; border: 1px solid #d8dee4;
                border-radius: 6px; background: white; color: #24292f;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton:hover {
                border-color: #1668a8; color: #1668a8; background: #f3f8fd;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton:disabled {
                color: #aab2bb; background: #f2f4f6; border-color: #e1e4e8;
            }
            /* 模块分组标题：浅灰蓝底与按钮区区分，左对齐蓝色加粗，点击即收起/展开。
               选择器必须比上方 QGroupBox#modulePanel QPushButton 更具体（含 id+属性），
               否则 modulePanel 的 padding/背景会压过本规则导致样式失效。 */
            QWidget#flowTab QGroupBox#modulePanel QPushButton[groupHeader="true"] {
                text-align: left; padding: 3px 10px;
                font-size: 10pt; font-weight: 600; color: #1668a8;
                border: none; border-radius: 6px; background: #e9f0f8;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton[groupHeader="true"]:hover {
                background: #dce8f4;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton[groupHeader="true"]:disabled {
                color: #aab2bb; background: #f2f4f6;
            }
            /* 「全部收起」图标按钮：无文字、小方框、浅蓝描边 */
            QWidget#flowTab QGroupBox#modulePanel QPushButton#collapseAllBtn {
                text-align: center; padding: 0;
                font-size: 11pt; color: #1668a8;
                border: 1px solid #b9d3e8; border-radius: 5px;
                background: #f3f8fd;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton#collapseAllBtn:hover {
                background: #dce8f4;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton#collapseAllBtn:disabled {
                color: #aab2bb; background: #f2f4f6; border-color: #e1e4e8;
            }
            QWidget#flowTab QScrollArea#moduleScroll {
                background: transparent; border: none;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        # ===== 左栏：流程列表（按分组组织，右键菜单管理流程） =====
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(6)
        lbar1 = QHBoxLayout()
        lbar1.setSpacing(4)
        self.new_group_btn = QPushButton("➕ 添加分组")
        self.new_group_btn.setToolTip("新建流程分组（可在分组下添加流程）")
        set_variant(self.new_group_btn, "primary")
        lbar1.addWidget(self.new_group_btn, 1)
        llay.addLayout(lbar1)
        self.list = QTreeWidget()
        self.list.setObjectName("flowList")
        self.list.setHeaderHidden(True)
        self.list.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.list.setRootIsDecorated(False)      # 分组头自带按钮，不需要系统展开箭头
        self.list.setIndentation(14)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._flow_context_menu)
        llay.addWidget(self.list, 1)
        left.setMinimumWidth(200)
        splitter.addWidget(left)

        # ===== 中栏：步骤编排（实时编辑） =====
        center = QWidget()
        clay = QVBoxLayout(center)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(6)

        self.right_title = QLabel("流程模块（选中左侧流程后编排）")
        self.right_title.setStyleSheet(
            "font-size: 12pt; font-weight: 600; color: #24292f; padding: 2px;")
        clay.addWidget(self.right_title)

        self.step_list = StepList()
        self.step_list.setObjectName("stepView")
        self.step_list.setStyleSheet(
            "QWidget#flowTab QListWidget#stepView::item { height: 38px; }")
        self.step_list.setItemDelegate(StepRunDelegate(self.step_list))
        self.step_list.stepDropped.connect(self._on_step_dropped)
        self.step_list.orderChanged.connect(self._on_order_changed)
        self.step_list.stepRunRequested.connect(self._run_single_step)
        self.step_list.itemDoubleClicked.connect(lambda *_: self._edit_step_param())
        self.step_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.step_list.customContextMenuRequested.connect(self._step_context_menu)
        clay.addWidget(self.step_list, 1)

        sbtn_row = QHBoxLayout()
        self.run_btn = QPushButton("▶ 运行/停止")
        self.run_btn.setToolTip("运行/停止选中流程")
        self.step_edit_btn = QPushButton("✎ 编辑参数")
        self.step_edit_btn.clicked.connect(self._edit_step_param)
        set_variant(self.step_edit_btn, "primary")
        self.step_del_btn = QPushButton("🗑 删除步骤")
        self.step_del_btn.clicked.connect(self._del_step)
        set_variant(self.step_del_btn, "danger")
        sbtn_row.addWidget(self.run_btn)
        sbtn_row.addWidget(self.step_edit_btn)
        sbtn_row.addWidget(self.step_del_btn)
        sbtn_row.addStretch(1)
        clay.addLayout(sbtn_row)
        splitter.addWidget(center)

        # ===== 右栏：模块面板（按功能分组，点击分组标题收起/展开） =====
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(6)

        self.panel_box = QGroupBox()
        self.panel_box.setObjectName("modulePanel")
        panel = QVBoxLayout(self.panel_box)
        panel.setContentsMargins(8, 6, 8, 8)
        panel.setSpacing(2)

        # 顶部工具行：右侧仅图标的收起按钮（面板标题已移除）
        panel_top = QHBoxLayout()
        panel_top.setSpacing(4)
        panel_top.addStretch(1)
        self.collapse_all_btn = QPushButton("⏫")
        self.collapse_all_btn.setObjectName("collapseAllBtn")
        self.collapse_all_btn.setToolTip("一键收起所有模块分组（再次点击各分组标题可展开）")
        self.collapse_all_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_all_btn.setFixedSize(26, 24)
        self.collapse_all_btn.clicked.connect(self._collapse_all_groups)
        panel_top.addWidget(self.collapse_all_btn)
        panel.addLayout(panel_top)

        # 分组内容放进滚动区：全部展开时高度不够可滚动，折叠后自动收缩
        scroll = QScrollArea()
        scroll.setObjectName("moduleScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_box = QWidget()
        scroll_lay = QVBoxLayout(scroll_box)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(2)

        self._module_btns = []                       # 全部模块按钮（锁定/解锁统一遍历）
        self._group_headers: dict[str, QPushButton] = {}   # gid -> 分组标题按钮
        self._group_wrappers: dict[str, QWidget] = {}      # gid -> 模块按钮容器
        self._group_titles = {gid: title for gid, title, _ in MODULE_GROUPS}
        collapsed = set(self.cfg.collapsed_module_groups)
        for gid, title, types in MODULE_GROUPS:
            header = QPushButton(f"▾ {title}" if gid not in collapsed else f"▸ {title}")
            header.setProperty("groupHeader", True)
            header.setCursor(Qt.PointingHandCursor)
            header.setToolTip("点击收起/展开")
            header.setCheckable(True)
            header.setChecked(gid not in collapsed)
            header.toggled.connect(lambda on, g=gid: self._on_group_toggled(g, on))
            self._group_headers[gid] = header
            scroll_lay.addWidget(header)

            wrapper = QWidget()
            # 不让分组容器被布局拉高：展开时组内按钮紧凑排列，不留空白
            wrapper.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            wrap_lay = QVBoxLayout(wrapper)
            wrap_lay.setContentsMargins(0, 0, 0, 0)
            wrap_lay.setSpacing(3)
            for t in types:
                label = FLOW_STEP_TYPES[t]
                btn = ModuleButton(t, label)
                btn.setMinimumHeight(32)
                self._module_btns.append(btn)
                wrap_lay.addWidget(btn)
            wrapper.setVisible(gid not in collapsed)
            self._group_wrappers[gid] = wrapper
            scroll_lay.addWidget(wrapper)

        scroll_lay.addStretch(1)   # 多余空间收到底部：全收起时标题紧凑，全展开时按钮顶对齐
        scroll.setWidget(scroll_box)
        panel.addWidget(scroll, 1)
        rlay.addWidget(self.panel_box, 1)
        right.setMinimumWidth(150)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([210, 600, 150])
        self._splitter = splitter
        root.addWidget(splitter, 1)

        # ---- 信号 ----
        self.new_group_btn.clicked.connect(self._add_group)
        self.run_btn.clicked.connect(self._toggle_selected)
        self.list.itemDoubleClicked.connect(lambda *_: self._on_flow_double_clicked())
        self.list.itemSelectionChanged.connect(self.refresh_steps_view)

    def showEvent(self, ev):
        """首次显示时按比例应用三栏宽度（构造期 setSizes 会被布局覆盖）。"""
        super().showEvent(ev)
        if not self._ratio_applied and self.isVisible():
            self._ratio_applied = True
            total = sum(self._splitter.sizes()) or 1
            self._splitter.setSizes([int(total * 0.22), int(total * 0.62),
                                     int(total * 0.16)])

    # ---------- 左栏列表（分组树） ----------
    def refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        collapsed = set(self.cfg.collapsed_flow_groups)
        # 分组顺序：flow_groups 定义的分组在前，「未分组」兜底放最后
        groups = [g for g in self.cfg.flow_groups if g.strip()]
        by_group: dict[str, list[Flow]] = {g: [] for g in groups}
        for f in self._flows:
            g = f.group if f.group in by_group else ""
            by_group.setdefault(g, []).append(f)
        for g in groups + [""]:
            gitem = QTreeWidgetItem()
            gitem.setData(0, Qt.UserRole, ("group", g))
            gitem.setFlags(Qt.ItemIsEnabled)          # 分组头不可选中
            self.list.addTopLevelItem(gitem)
            expanded = g not in collapsed
            self.list.setItemWidget(gitem, 0, self._group_header_widget(g, expanded))
            for f in by_group.get(g, []):
                citem = QTreeWidgetItem([self._flow_item_text(f)])
                citem.setData(0, Qt.UserRole, ("flow", f.id))
                runner = self._runners.get(f.id)
                if runner and runner.is_running:
                    citem.setForeground(0, QColor("#27ae60"))
                gitem.addChild(citem)
            gitem.setExpanded(expanded)
        self.list.blockSignals(False)
        self._restore_selection()
        self.refresh_steps_view()

    def _restore_selection(self):
        """重建后恢复选中：优先选中运行中的流程，否则第一个流程。"""
        for f in self._flows:
            runner = self._runners.get(f.id)
            if runner and runner.is_running:
                self._select_flow_item(f.id)
                return
        if self._flows:
            self._select_flow_item(self._flows[0].id)

    def _select_flow_item(self, flow_id: str):
        item = self._flow_item(flow_id)
        if item is not None:
            self.list.setCurrentItem(item)

    def _group_item(self, g: str) -> QTreeWidgetItem | None:
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            if it.data(0, Qt.UserRole) == ("group", g):
                return it
        return None

    def _flow_item(self, flow_id: str) -> QTreeWidgetItem | None:
        for i in range(self.list.topLevelItemCount()):
            g = self.list.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                if c.data(0, Qt.UserRole) == ("flow", flow_id):
                    return c
        return None

    def _group_header_widget(self, g: str, expanded: bool) -> QWidget:
        """分组头：展开/收起按钮 + 在当前分组下新建流程的加号按钮。"""
        name = g if g else "未分组"
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        title = QPushButton(("▾ " if expanded else "▸ ") + name)
        title.setObjectName("groupTitle")
        title.setProperty("groupHeader", True)
        title.setCursor(Qt.PointingHandCursor)
        title.setToolTip("点击展开/收起分组")
        title.setContextMenuPolicy(Qt.CustomContextMenu)
        title.customContextMenuRequested.connect(
            lambda pos, g=g: self._group_context_menu(g, title.mapToGlobal(pos)))
        title.clicked.connect(lambda _, g=g: self._toggle_group(g))
        h.addWidget(title, 1)
        plus = QPushButton("＋")
        plus.setCursor(Qt.PointingHandCursor)
        plus.setToolTip(f"在「{name}」分组下新建流程")
        plus.clicked.connect(lambda _, g=g: self._new_flow(g))
        plus.setMaximumWidth(30)
        h.addWidget(plus)
        return w

    def _toggle_group(self, g: str):
        """点击分组头：切换展开/收起，并把状态持久化到 config。"""
        item = self._group_item(g)
        if item is None:
            return
        expanded = not item.isExpanded()
        item.setExpanded(expanded)
        header = self.list.itemWidget(item, 0)
        if header is not None:
            btn = header.findChild(QPushButton, "groupTitle")
            if btn is not None:
                btn.setText(("▾ " if expanded else "▸ ") + (g if g else "未分组"))
        collapsed = set(self.cfg.collapsed_flow_groups)
        if expanded:
            collapsed.discard(g)
        else:
            collapsed.add(g)
        self.cfg.collapsed_flow_groups = sorted(collapsed)
        self.cfg.save()

    def _flow_item_text(self, f: Flow, failed: bool = False) -> str:
        """左栏条目只显示流程名（运行中加 ▶ 前缀；失败标注红色由调用方处理）。"""
        runner = self._runners.get(f.id)
        if runner and runner.is_running:
            return f"▶ {f.name}"
        if failed:
            return f"{f.name}（上次失败）"
        return f.name

    def _update_left_item(self, flow_id: str, failed: bool = False):
        """按流程当前状态刷新左栏对应条目。"""
        flow = next((f for f in self._flows if f.id == flow_id), None)
        item = self._flow_item(flow_id)
        if flow is None or item is None:
            return
        item.setText(0, self._flow_item_text(flow, failed=failed))
        runner = self._runners.get(flow_id)
        if runner and runner.is_running:
            item.setForeground(0, QColor("#27ae60"))
        elif failed:
            item.setForeground(0, QColor("#c0392b"))
        else:
            item.setForeground(0, self.list.palette().color(self.list.foregroundRole()))

    @staticmethod
    def _loops_text(f: Flow) -> str:
        return "无限循环" if f.loops == 0 else f"{f.loops} 轮"

    def _selected_flow(self) -> Flow | None:
        item = self.list.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != "flow":
            return None
        return next((f for f in self._flows if f.id == data[1]), None)

    def _selected_running(self) -> bool:
        flow = self._selected_flow()
        if flow is None:
            return False
        runner = self._runners.get(flow.id)
        return bool(runner and runner.is_running)

    # ---------- 右栏：步骤编排 ----------
    def refresh_steps_view(self):
        self._reload_steps()

    def _collapse_all_groups(self):
        """一键收起全部模块分组（状态经 toggled 信号持久化到 config）。"""
        for gid, header in self._group_headers.items():
            if header.isChecked():
                header.setChecked(False)   # 触发 toggled(False) -> _on_group_toggled 收起+持久化

    def _on_group_toggled(self, gid: str, expanded: bool):
        """分组标题点击：切换模块按钮区显示/隐藏，并把状态持久化到 config。"""
        header = self._group_headers.get(gid)
        wrapper = self._group_wrappers.get(gid)
        if header is None or wrapper is None:
            return
        header.setText(("▾ " if expanded else "▸ ") + self._group_titles[gid])
        wrapper.setVisible(expanded)
        collapsed = set(self.cfg.collapsed_module_groups)
        if expanded:
            collapsed.discard(gid)
        else:
            collapsed.add(gid)
        self.cfg.collapsed_module_groups = sorted(collapsed)
        self.cfg.save()

    def _reload_steps(self):
        flow = self._selected_flow()
        running = self._selected_running()
        editable = flow is not None and not running

        # 锁定/解锁编辑控件
        for b in self._module_btns:
            b.setEnabled(editable)
        for h in self._group_headers.values():
            h.setEnabled(editable)
        self.collapse_all_btn.setEnabled(editable)
        self.step_list.setEnabled(editable)
        self.step_edit_btn.setEnabled(editable)
        self.step_del_btn.setEnabled(editable)
        self.panel_box.setTitle("模块面板" if editable
                                else "模块面板（流程运行中，已锁定编辑）")
        self._update_run_button()

    def _update_run_button(self):
        """运行按钮随选中流程状态切换文案。"""
        flow = self._selected_flow()
        runner = self._runners.get(flow.id) if flow else None
        running = bool(runner and runner.is_running)
        self.run_btn.setText("■ 停止" if running else "▶ 运行/停止")
        set_variant(self.run_btn, "danger" if running else "success")

        self.step_list.blockSignals(True)
        self.step_list.clear()
        if flow is None:
            self.right_title.setText("流程模块（选中左侧流程后编排）")
            self.step_list.blockSignals(False)
            return
        runner = self._runners.get(flow.id)
        is_running = bool(runner and runner.is_running)
        single_row = self._single_rows.get(flow.id) if is_running else None
        if single_row is not None:
            running_idx = single_row  # 单步执行：高亮被单独运行的那一步
        else:
            running_idx = runner.current_step_index if is_running else None
        hk = f"【{hotkey_display(flow.hotkey)}】" if flow.hotkey else ""
        if single_row is not None:
            step_name = (flow.steps[single_row].name
                         if 0 <= single_row < len(flow.steps) else "")
            self.right_title.setText(f"「{flow.name}」单步执行中：{step_name}{hk}")
        elif running_idx is not None and running_idx >= 0:
            step_name = (flow.steps[running_idx].name
                         if 0 <= running_idx < len(flow.steps) else "")
            self.right_title.setText(f"「{flow.name}」运行中 · 步骤 "
                                     f"{running_idx + 1}/{len(flow.steps)}：{step_name}{hk}")
        else:
            self.right_title.setText(f"「{flow.name}」要执行的模块"
                                     f"（{len(flow.steps)} 步 · {self._loops_text(flow)}）{hk}")
        for i, s in enumerate(flow.steps):
            mark = "（失败继续）" if s.continue_on_fail else ""
            pair = " 🔗成对" if s.pair_id else ""
            item = QListWidgetItem(f"{i + 1}. {_TYPE_ICONS.get(s.type, '')} {s.name} · "
                                   f"{s.summary()}{mark}{pair}")
            item.setData(Qt.UserRole, i)
            if running_idx is not None and i == running_idx:
                item.setBackground(QColor("#dcf5e7"))
                item.setForeground(QColor("#177a45"))
                item.setText(f"▶ {item.text()}")
            self.step_list.addItem(item)
        if not flow.steps:
            empty = QListWidgetItem("（流程为空：把上方模块拖进来）")
            empty.setFlags(Qt.ItemIsEnabled)
            self.step_list.addItem(empty)
        self.step_list.blockSignals(False)

    def _current_step(self) -> FlowStep | None:
        flow = self._selected_flow()
        row = self.step_list.currentRow()
        if flow is not None and 0 <= row < len(flow.steps):
            return flow.steps[row]
        return None

    def _on_step_dropped(self, step_type: str, row: int):
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            self._reload_steps()
            return
        row = max(0, min(row, len(flow.steps)))
        if step_type == "web":
            # 网页操作 = 打开网址 + 关闭浏览器 成对出现（共享 pair_id）
            pid = uuid.uuid4().hex[:12]
            open_step = FlowStep(type="web", params=dict(
                default_step_params("web", self.cfg.clicker, self.cfg.presser)))
            open_step.params["action"] = "open"
            open_step.pair_id = pid
            close_step = FlowStep(type="web", params=dict(
                default_step_params("web", self.cfg.clicker, self.cfg.presser)))
            close_step.params["action"] = "close_browser"
            close_step.pair_id = pid
            flow.steps.insert(row, open_step)
            flow.steps.insert(row + 1, close_step)
            self._status_msg("已生成「打开网址 + 关闭浏览器」一对（删除时同步删除）", 4000)
        else:
            step = FlowStep(type=step_type,
                            params=default_step_params(step_type, self.cfg.clicker,
                                                       self.cfg.presser))
            flow.steps.insert(row, step)
        self._reload_steps()
        self.changed.emit()

    def _on_order_changed(self):
        flow = self._selected_flow()
        if flow is None:
            return
        # 延迟重建，避免在 dropEvent 过程中改动列表结构
        def apply():
            order = []
            for i in range(self.step_list.count()):
                idx = self.step_list.item(i).data(Qt.UserRole)
                if idx is not None and 0 <= idx < len(flow.steps):
                    order.append(flow.steps[idx])
            if len(order) == len(flow.steps):
                flow.steps[:] = order
                self._fix_pair_order(flow)
                self.changed.emit()
            self._reload_steps()
        QTimer.singleShot(0, apply)

    @staticmethod
    def _fix_pair_order(flow: Flow) -> bool:
        """保证配对的「打开网址」排在「关闭浏览器」之前；顺序颠倒则交换。

        配对的步骤允许被其它步骤隔开（打开网址 → 找图点击 → 关闭浏览器 是常见编排），
        但「关闭浏览器」跑到「打开网址」前面就失去了成对的意义，这里纠正回来。
        """
        changed = False
        for s in flow.steps:
            if not s.pair_id:
                continue
            idxs = [i for i, x in enumerate(flow.steps) if x.pair_id == s.pair_id]
            if len(idxs) != 2:
                continue
            a, b = idxs
            if web_action(flow.steps[a]) == "close_browser" and web_action(flow.steps[b]) == "open":
                flow.steps[a], flow.steps[b] = flow.steps[b], flow.steps[a]
                changed = True
        return changed

    def _edit_step_param(self):
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        step = self._current_step()
        if step is None:
            return
        dlg = StepParamsDialog(step, self)
        dlg.regionCaptureRequested.connect(lambda: self._capture_region_for_step(dlg))
        dlg.templateCaptureRequested.connect(lambda: self._capture_template_for_step(dlg))
        dlg.pointCaptureRequested.connect(lambda: self._capture_point_for_step(dlg))
        dlg.windowCaptureRequested.connect(lambda: self._capture_window_for_step(dlg))

        # 非模态 + finished 信号保存：截图选区期间对话框 hide() 不会像 exec() 那样
        # 立刻以 Rejected 结束编辑会话（那是 image 保存丢失的根因）
        def _finished(result):
            if result == StepParamsDialog.Accepted:
                dlg.apply_to(step)
                if repair_web_pairs(flow.steps):
                    self._status_msg("步骤动作已修改，不再与配对的网页步骤成对", 4000)
                self._reload_steps()
                self.changed.emit()

        dlg.finished.connect(_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _del_step(self):
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        row = self.step_list.currentRow()
        if 0 <= row < len(flow.steps):
            step = flow.steps[row]
            if step.pair_id:
                # 成对步骤：删除当前步骤时同步删除配对的另一个
                flow.steps[:] = [s for s in flow.steps if s.pair_id != step.pair_id]
                self._status_msg("已删除网页步骤及其配对的「打开网址/关闭浏览器」", 4000)
            else:
                del flow.steps[row]
            self._reload_steps()
            self.changed.emit()

    def _step_context_menu(self, pos):
        """步骤列表右键菜单：编辑参数 / 删除步骤（复用底部按钮的逻辑）。"""
        item = self.step_list.itemAt(pos)
        if item is None or item.data(Qt.UserRole) is None:   # 空流程占位行不弹菜单
            return
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        self.step_list.setCurrentItem(item)                  # 右键即选中该步骤
        menu = QMenu(self)
        # 显示效果：文字靠左、行高更大、字号更大，悬停/预选蓝色高亮
        menu.setStyleSheet("""
            QMenu {
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 8px;
                padding: 6px;
                font-size: 12pt;
            }
            QMenu::item {
                color: #24292f;
                padding: 9px 32px 9px 14px;
                margin: 2px 4px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background: #1668a8;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #a7afb8;
                background: transparent;
            }
        """)
        edit_act = menu.addAction("✎ 编辑参数")
        del_act = menu.addAction("🗑 删除步骤")
        act = menu.exec(self.step_list.viewport().mapToGlobal(pos))
        if act == edit_act:
            self._edit_step_param()
        elif act == del_act:
            self._del_step()

    # ---------- 区域框选链（步骤参数对话框 -> 主窗口隐藏 -> 遮罩 -> 回写） ----------
    def _capture_region_for_step(self, dlg: StepParamsDialog):
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, self._start_region_capture)

    def _start_region_capture(self):
        from ..capture_overlay import run_screen_capture
        dlg = self._capture_step_dlg

        def done(rect=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if rect is not None:
                    dlg.set_region(rect)
                dlg.finish_region_capture()
            self._capture_step_dlg = None

        try:
            run_screen_capture(on_region=lambda r: done(r), on_cancelled=lambda: done())
        except Exception:
            done()

    def _capture_template_for_step(self, dlg):
        """模板图屏幕截图选区：与「添加模板」相同流程，成功后回写步骤模板。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_template_capture(dlg))

    def _start_point_capture(self, dlg):
        from ..capture_overlay import run_screen_capture

        def done(point=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if point:
                    dlg.set_point(point[0], point[1])
                dlg.finish_point_capture()
            self._capture_step_dlg = None

        try:
            run_screen_capture(on_point=lambda pt: done(pt),
                               on_cancelled=lambda: done())
        except Exception:
            done()

    def _capture_point_for_step(self, dlg):
        """屏幕点选坐标：主窗口隐藏 -> 遮罩单击取点 -> 回写步骤坐标。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_point_capture(dlg))

    def _capture_window_for_step(self, dlg):
        """拖动识别窗口：主窗口隐藏 -> 实时高亮遮罩 -> 单击确认句柄。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_window_capture(dlg))

    def _start_window_capture(self, dlg):
        """窗口识别遮罩：移动鼠标实时高亮目标窗口，单击确认句柄回填。"""
        from ..capture_overlay import run_window_picker

        def done(hwnd=0, title=""):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if hwnd:
                    dlg.set_window(hwnd, title)
                dlg.finish_window_capture()
            self._capture_step_dlg = None

        try:
            run_window_picker(on_picked=lambda hwnd, title: done(hwnd, title),
                              on_cancelled=lambda: done())
        except Exception:
            done()

    def _start_template_capture(self, dlg):
        from ..capture_overlay import run_screen_capture

        def done(path=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if path:
                    dlg.set_template_image(os.path.basename(path), path)
                dlg.finish_template_capture()
            self._capture_step_dlg = None

        try:
            run_screen_capture(on_saved=lambda p: done(p), on_cancelled=lambda: done())
        except Exception:
            done()

    # ---------- 流程元信息（新建/编辑） ----------
    def _new_flow(self, group: str = ""):
        flow = Flow(name=f"流程 {len(self._flows) + 1}", group=group)
        dlg = FlowMetaDialog(flow, create=True, parent=self, groups=self.cfg.flow_groups)
        if dlg.exec() == FlowMetaDialog.Accepted:
            dlg.apply_to(flow)
            self._flows.append(flow)
            self.refresh_list()
            self._select_flow_item(flow.id)
            self.changed.emit()

    def _edit_flow(self):
        flow = self._selected_flow()
        if flow is None:
            return
        dlg = FlowMetaDialog(flow, create=False, parent=self, groups=self.cfg.flow_groups)
        if dlg.exec() == FlowMetaDialog.Accepted:
            dlg.apply_to(flow)
            self.refresh_list()
            self.changed.emit()

    def _del_flow(self):
        flow = self._selected_flow()
        if flow is None:
            return
        if QMessageBox.question(self, "删除流程", f"确定删除流程「{flow.name}」吗？"
                                ) != QMessageBox.Yes:
            return
        self.stop_flow(flow.id)
        self._runners.pop(flow.id, None)      # 流程都删了，别再留着它的 runner
        self._single_rows.pop(flow.id, None)
        self._flows.remove(flow)
        self.refresh_list()
        self.changed.emit()

    # ---------- 分组管理 ----------
    def _add_group(self):
        name, ok = QInputDialog.getText(self, "添加分组", "请输入分组名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.cfg.flow_groups:
            QMessageBox.information(self, "分组已存在", f"分组「{name}」已经存在。")
            return
        self.cfg.flow_groups.append(name)
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    def _rename_group(self, g: str):
        name, ok = QInputDialog.getText(self, "重命名分组", "新的分组名称：", text=g)
        name = (name or "").strip()
        if not ok or not name or name == g:
            return
        if name in self.cfg.flow_groups:
            QMessageBox.information(self, "分组已存在", f"分组「{name}」已经存在。")
            return
        self.cfg.flow_groups = [name if x == g else x for x in self.cfg.flow_groups]
        for f in self._flows:
            if f.group == g:
                f.group = name
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    def _del_group(self, g: str):
        if QMessageBox.question(self, "删除分组",
                                f"确定删除分组「{g}」吗？\n组内流程将移到「未分组」。"
                                ) != QMessageBox.Yes:
            return
        self.cfg.flow_groups = [x for x in self.cfg.flow_groups if x != g]
        self.cfg.collapsed_flow_groups = [x for x in self.cfg.collapsed_flow_groups if x != g]
        for f in self._flows:
            if f.group == g:
                f.group = ""
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    # ---------- 左栏右键菜单 ----------
    def _flow_context_menu(self, pos):
        """流程列表右键：流程项 = 编辑/删除/导出；分组头 = 分组管理。"""
        item = self.list.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "group":
            self._group_context_menu(data[1], self.list.viewport().mapToGlobal(pos))
            return
        flow = next((f for f in self._flows if f.id == data[1]), None)
        if flow is None:
            return
        self.list.setCurrentItem(item)               # 右键即选中该流程
        menu = QMenu(self)
        self._style_menu(menu)
        edit_act = menu.addAction("✎ 编辑流程")
        del_act = menu.addAction("🗑 删除流程")
        menu.addSeparator()
        export_act = menu.addAction("📤 导出流程")
        act = menu.exec(self.list.viewport().mapToGlobal(pos))
        if act == edit_act:
            self._edit_flow()
        elif act == del_act:
            self._del_flow()
        elif act == export_act:
            self._export_flow()

    def _group_context_menu(self, g: str, pos):
        """分组头右键：重命名/删除分组（「未分组」不可操作）。"""
        if not g:
            return
        menu = QMenu(self)
        self._style_menu(menu)
        rename_act = menu.addAction("✎ 重命名分组")
        del_act = menu.addAction("🗑 删除分组")
        act = menu.exec(pos)
        if act == rename_act:
            self._rename_group(g)
        elif act == del_act:
            self._del_group(g)

    @staticmethod
    def _style_menu(menu: QMenu):
        menu.setStyleSheet("""
            QMenu {
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 8px;
                padding: 6px;
                font-size: 12pt;
            }
            QMenu::item {
                color: #24292f;
                padding: 9px 32px 9px 14px;
                margin: 2px 4px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background: #1668a8;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #a7afb8;
                background: transparent;
            }
        """)

    def _on_flow_double_clicked(self):
        """双击流程条目 = 编辑流程（分组头双击由按钮自身处理）。"""
        item = self.list.currentItem()
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if data and data[0] == "flow":
            self._edit_flow()

    # ---------- 流程导入 / 导出 ----------
    def _import_flow(self):
        start = FLOWS_DIR if os.path.isdir(FLOWS_DIR) else BASE_DIR
        path, _ = QFileDialog.getOpenFileName(self, "导入流程", start,
                                              "流程文件 (*.json);;所有文件 (*)")
        if not path:
            return
        self.import_flow_file(path)

    def import_flow_file(self, path: str) -> bool:
        """从文件导入一个流程；格式非法时提示并返回 False。

        供「导入流程」按钮与外部文件拖放共用。
        """
        flow = flow_from_file(path)
        if flow is None:
            QMessageBox.warning(self, "导入失败",
                                "所选文件不是有效的流程文件（版本不兼容或内容已损坏）。")
            return False
        flow.id = uuid.uuid4().hex[:12]   # 分配新 id，避免与现有流程/流程文件冲突
        self._flows.append(flow)
        self.refresh_list()
        self._select_flow_item(flow.id)
        self.changed.emit()
        self._status_msg(f"已导入流程「{flow.name}」（{len(flow.steps)} 步）", 5000)
        return True

    def _export_flow(self):
        flow = self._selected_flow()
        if flow is None:
            self._status_msg("请先在左侧列表选择要导出的流程", 4000)
            return
        default = os.path.join(BASE_DIR, f"{safe_filename(flow.name)}.json")
        path, _ = QFileDialog.getSaveFileName(self, "导出流程", default,
                                              "流程文件 (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(flow_to_dict(flow), f, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{e}")
            return
        self._status_msg(f"已导出「{flow.name}」→ {path}", 6000)

    def _toggle_selected(self):
        flow = self._selected_flow()
        if flow is None:
            sb = self.window().statusBar()
            if hasattr(sb, "showMessage"):
                sb.showMessage("请先在左侧列表选择一个流程", 4000)
            return
        self.toggle_flow(flow.id)

    # ---------- 运行控制 ----------
    def toggle_flow(self, flow_id: str):
        flow = next((f for f in self._flows if f.id == flow_id), None)
        if flow is None:
            return
        runner = self._runners.get(flow_id)
        if runner and runner.is_running:
            runner.stop()
            return
        if not flow.steps:
            QMessageBox.information(self, "无法运行", "流程还没有步骤，请先在右侧拖入模块。")
            return
        runner = FlowRunner(_clone_flow(flow))   # 深拷贝：steps 不能和界面共享
        runner.stateChanged.connect(lambda state, reason, ok, fid=flow_id:
                                    self._on_state(fid, state, reason, ok))
        runner.stepStarted.connect(lambda idx, name, fid=flow_id:
                                   self._on_step_started(fid, idx, name))
        self._runners[flow_id] = runner
        self.flowStarted.emit()
        runner.start()

    def stop_flow(self, flow_id: str):
        runner = self._runners.get(flow_id)
        if runner and runner.is_running:
            runner.stop()

    def _status_msg(self, text: str, ms: int):
        """向主窗口状态栏发轻提示（无状态栏时静默跳过）。"""
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, ms)

    def _run_single_step(self, row: int):
        """单步执行：只运行步骤列表中的某一步。

        构造一个只含该步骤、loops=1 的临时流程复用 FlowRunner 线程，
        并登记到 _runners —— 运行期间左栏状态/右栏锁定/停止按钮/全停热键全部照常生效。
        """
        flow = self._selected_flow()
        if flow is None or not (0 <= row < len(flow.steps)):
            return
        if self._selected_running():
            self._status_msg("流程运行中，请先停止后再单步执行", 4000)
            return
        step = flow.steps[row]
        # 浅拷贝参数字典，避免执行线程与编辑中的步骤共享同一 dict
        step_copy = FlowStep(type=step.type, name=step.name, params=dict(step.params),
                             continue_on_fail=step.continue_on_fail, pair_id=step.pair_id)
        single_flow = Flow(id=flow.id, name=flow.name, steps=[step_copy], loops=1)
        runner = FlowRunner(single_flow)
        runner.stateChanged.connect(lambda state, reason, ok, fid=flow.id, name=step.name:
                                    self._on_single_state(fid, state, reason, ok, name))
        runner.stepStarted.connect(lambda idx, name, fid=flow.id:
                                   self._on_step_started(fid, idx, name))
        self._single_rows[flow.id] = row
        self._runners[flow.id] = runner
        log(f"单步执行「{flow.name}」步骤 {row + 1}：{step.name}")
        self.flowStarted.emit()
        runner.start()

    def _on_single_state(self, flow_id: str, state: str, reason: str,
                         ok: bool, step_name: str):
        """单步执行的状态回调：成败以步骤本身结果为准（勾选“失败继续”的超时也算失败）。"""
        runner = self._runners.get(flow_id)
        if state == "running":
            self._update_left_item(flow_id)
        else:
            self._single_rows.pop(flow_id, None)
            failed = reason != "已手动停止" and not (runner and runner.last_step_ok)
            self._update_left_item(flow_id, failed=failed)
            flow = next((f for f in self._flows if f.id == flow_id), None)
            if flow is not None:
                if reason == "已手动停止":
                    self._status_msg(f"「{flow.name}」单步执行已停止", 4000)
                elif failed:
                    why = runner.last_step_reason if runner else reason
                    QMessageBox.information(self, "单步执行失败",
                                            f"「{flow.name}」· {step_name}\n{why}")
                else:
                    self._status_msg(f"「{flow.name}」单步执行完成：{step_name}", 6000)
            self._runners.pop(flow_id, None)   # 单步跑完，摘掉 runner
        self.runningStateChanged.emit()
        self._reload_steps()

    def stop_all(self):
        for r in self._runners.values():
            if r.is_running:
                r.stop()

    def any_running(self) -> bool:
        return any(r.is_running for r in self._runners.values())

    def running_names(self) -> list[str]:
        """当前正在运行的流程名（给主窗口等外部模块查状态用）。

        外部不要直接读 _runners：那是本 tab 的内部实现细节，依赖它会让以后
        调整 runner 存储方式时牵连到别的模块。
        """
        names = []
        for f in self._flows:
            runner = self._runners.get(f.id)
            if runner is not None and runner.is_running:
                names.append(f.name)
        return names

    # ---------- 状态回调 ----------
    def _on_step_started(self, flow_id: str, idx: int, name: str):
        self._update_left_item(flow_id)
        self._reload_steps()

    def _on_state(self, flow_id: str, state: str, reason: str, ok: bool = True):
        flow = next((f for f in self._flows if f.id == flow_id), None)
        if state == "running":
            self._update_left_item(flow_id)
        else:
            self._runners.pop(flow_id, None)   # 跑完就摘掉，别让字典越攒越大
            failed = bool(reason) and not ok and reason != "已手动停止"
            self._update_left_item(flow_id, failed=failed)
            if flow is None:
                pass
            elif failed:
                QMessageBox.information(self, "流程结束", f"「{flow.name}」：{reason}")
            elif reason:
                # 正常完成：底部状态栏轻提示（流程步骤很快跑完时也能看到结果）
                sb = self.window().statusBar()
                if hasattr(sb, "showMessage"):
                    sb.showMessage(f"「{flow.name}」{reason}", 6000)
        self.runningStateChanged.emit()
        self._reload_steps()

    # ---------- 退出 ----------
    def shutdown(self):
        self.stop_all()
