"""定时任务页：左栏分组任务列表，右栏任务详情卡片。

顶部可新建定时任务；任务按分组组织（右键管理分组），调度规则支持
每秒/每分/每时/每天/每周/每月/指定时间/Cron 表达式，编辑对话框内可
实时预览接下来 5 次运行时间。后台调度线程到期后自动触发对应流程。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFormLayout, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..config import AppConfig, Flow, ScheduleTask
from ..logbus import log
from ..scheduler import (ScheduleRunner, describe_schedule, format_dt,
                         next_run_time, next_run_times)
from .schedule_dialog import ScheduleDialog
from .widgets import set_variant

_PREVIEW_N = 5


def _parse_dt(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


class ScheduleTab(QWidget):
    changed = Signal()   # 定时任务增删改

    def __init__(self, cfg: AppConfig, flow_tab, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.flow_tab = flow_tab
        self._tasks = cfg.schedule_tasks
        self.runner = ScheduleRunner(lambda: self._tasks)
        self.runner.due.connect(self._on_due)
        self._build_ui()
        self.refresh_list()
        self.runner.start()

    # ---------- UI ----------
    def _build_ui(self):
        self.setObjectName("scheduleTab")
        # 本页 QSS 只管「页面骨架 + 列表」两部分；按钮变体已由 widgets.set_variant
        # 通过 setStyleSheet 内联注入，不在本页 QSS 中再覆盖，避免互相打架。
        self.setStyleSheet("""
            QWidget#scheduleTab { background: #f7f9fb; }
            QWidget#scheduleTab QTreeWidget#scheduleList {
                font-size: 10pt; outline: none;
                border: 1px solid #d8dee4; border-radius: 6px; background: white;
            }
            QWidget#scheduleTab QTreeWidget#scheduleList::item {
                height: 30px; padding: 2px 6px;
                border-bottom: 1px solid #f0f3f6;
            }
            QWidget#scheduleTab QTreeWidget#scheduleList::item:hover { background-color: #e8f1fa; }
            QWidget#scheduleTab QTreeWidget#scheduleList::item:selected {
                background-color: #1668a8; color: white;
            }
            QWidget#scheduleTab QTreeWidget#scheduleList::branch { background: transparent; }
            QWidget#scheduleTab QTreeWidget#scheduleList QPushButton[groupHeader="true"] {
                text-align: left; padding: 3px 8px;
                font-size: 10pt; font-weight: 600; color: #1668a8;
                border: none; border-radius: 4px; background: #e9f0f8;
            }
            QWidget#scheduleTab QTreeWidget#scheduleList QPushButton[groupHeader="true"]:hover {
                background: #dce8f4;
            }
            QWidget#scheduleTab QTreeWidget#scheduleList QPushButton {
                text-align: center; padding: 3px 8px;
                font-size: 10pt; color: #1668a8;
                border: none; border-radius: 4px; background: transparent;
            }
            QWidget#scheduleTab QTreeWidget#scheduleList QPushButton:hover { background: #e9f0f8; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        top = QHBoxLayout()
        title = QLabel("⏰ 定时任务")
        title.setStyleSheet("font-size: 13pt; font-weight: 600; color: #24292f;")
        top.addWidget(title)
        top.addStretch(1)
        self.new_btn = QPushButton("＋ 新建定时任务")
        self.new_btn.setToolTip("创建一个定时任务：指定调度规则与要运行的流程")
        set_variant(self.new_btn, "primary")
        self.new_btn.clicked.connect(lambda: self._new_task(""))
        top.addWidget(self.new_btn)
        root.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        # ===== 左栏：分组任务列表 =====
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(6)
        self.add_group_btn = QPushButton("➕ 添加分组")
        self.add_group_btn.setToolTip("新建定时任务分组")
        set_variant(self.add_group_btn, "primary")
        self.add_group_btn.clicked.connect(self._add_group)
        llay.addWidget(self.add_group_btn)
        self.list = QTreeWidget()
        self.list.setObjectName("scheduleList")
        self.list.setHeaderHidden(True)
        self.list.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.list.setRootIsDecorated(False)
        self.list.setIndentation(14)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemSelectionChanged.connect(self.refresh_detail)
        llay.addWidget(self.list, 1)
        left.setMinimumWidth(220)
        splitter.addWidget(left)

        # ===== 右栏：任务详情 =====
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setFrameShape(QFrame.NoFrame)
        # 右栏背景与 tab 背景同色，过渡更顺滑；按钮变体走 set_variant 内联样式，
        # 不会与本样式冲突。
        right.setStyleSheet("QScrollArea{background:#f7f9fb;border:none;}")
        self.detail = QWidget()
        self.detail.setStyleSheet("background:#f7f9fb;")
        self.detail_lay = QVBoxLayout(self.detail)
        self.detail_lay.setContentsMargins(8, 0, 8, 0)
        self.detail_lay.setSpacing(10)
        self.detail_lay.addStretch(1)
        right.setWidget(self.detail)
        right.setMinimumWidth(360)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 560])
        self._splitter = splitter
        root.addWidget(splitter, 1)

        self.list.itemDoubleClicked.connect(lambda *_: self._edit_task())

    def showEvent(self, ev):
        super().showEvent(ev)
        if not getattr(self, "_ratio_applied", False) and self.isVisible():
            self._ratio_applied = True
            total = sum(self._splitter.sizes()) or 1
            self._splitter.setSizes([int(total * 0.34), int(total * 0.66)])

    # ---------- 左栏列表 ----------
    def refresh_list(self):
        sel = self._selected_task_id()
        self.list.blockSignals(True)
        self.list.clear()
        collapsed = set(self.cfg.collapsed_schedule_groups)
        groups = [g for g in self.cfg.schedule_groups if g.strip()]
        by_group: dict[str, list[ScheduleTask]] = {g: [] for g in groups}
        for t in self._tasks:
            g = t.group if t.group in by_group else ""
            by_group.setdefault(g, []).append(t)
        for g in groups + [""]:
            gitem = QTreeWidgetItem()
            gitem.setData(0, Qt.UserRole, ("group", g))
            gitem.setFlags(Qt.ItemIsEnabled)
            self.list.addTopLevelItem(gitem)
            expanded = g not in collapsed
            self.list.setItemWidget(gitem, 0, self._group_header_widget(g, expanded))
            for t in by_group.get(g, []):
                citem = QTreeWidgetItem([self._item_text(t)])
                citem.setData(0, Qt.UserRole, ("task", t.id))
                citem.setToolTip(0, self._item_tooltip(t))
                if not t.enabled:
                    citem.setForeground(0, QColor("#a7afb8"))
                gitem.addChild(citem)
            gitem.setExpanded(expanded)
        self.list.blockSignals(False)
        if sel:
            self._select_task_item(sel)
        else:
            self._restore_selection()
        self.refresh_detail()

    def _selected_task_id(self) -> str | None:
        item = self.list.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        return data[1] if data and data[0] == "task" else None

    def _restore_selection(self):
        if self._tasks:
            self._select_task_item(self._tasks[0].id)

    def _select_task_item(self, task_id: str):
        item = self._task_item(task_id)
        if item is not None:
            self.list.setCurrentItem(item)

    def _group_item(self, g: str) -> QTreeWidgetItem | None:
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            if it.data(0, Qt.UserRole) == ("group", g):
                return it
        return None

    def _task_item(self, task_id: str) -> QTreeWidgetItem | None:
        for i in range(self.list.topLevelItemCount()):
            g = self.list.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                if c.data(0, Qt.UserRole) == ("task", task_id):
                    return c
        return None

    def _selected_task(self) -> ScheduleTask | None:
        tid = self._selected_task_id()
        return next((t for t in self._tasks if t.id == tid), None)

    def _flow_of(self, task: ScheduleTask) -> Flow | None:
        return next((f for f in self.cfg.flows if f.id == task.flow_id), None)

    def _item_text(self, t: ScheduleTask) -> str:
        dot = "●" if t.enabled else "○"
        flow = self._flow_name(t)
        text = f"{dot}  {t.name}"
        if flow:
            text += f"  ·  {flow}"
        if t.enabled:
            nt = _parse_dt(t.next_run)
            if nt:
                text += f"  ·  下次 {nt.strftime('%m-%d %H:%M')}"
        return text

    def _item_tooltip(self, t: ScheduleTask) -> str:
        lines = [t.name, describe_schedule(t)]
        flow = self._flow_name(t)
        lines.append(f"流程：{flow or '（未选择）'}")
        if t.next_run:
            lines.append(f"下次运行：{t.next_run}")
        if t.last_run:
            lines.append(f"上次运行：{t.last_run}")
        return "\n".join(lines)

    def _flow_name(self, t: ScheduleTask) -> str:
        flow = self._flow_of(t)
        if flow is not None:
            return flow.name
        return t.flow_name or "（流程已删除）"

    def _group_header_widget(self, g: str, expanded: bool) -> QWidget:
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
        plus.setToolTip(f"在「{name}」分组下新建定时任务")
        plus.clicked.connect(lambda _, g=g: self._new_task(g))
        plus.setMaximumWidth(30)
        h.addWidget(plus)
        return w

    def _toggle_group(self, g: str):
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
        collapsed = set(self.cfg.collapsed_schedule_groups)
        if expanded:
            collapsed.discard(g)
        else:
            collapsed.add(g)
        self.cfg.collapsed_schedule_groups = sorted(collapsed)
        self.cfg.save()

    # ---------- 右栏详情 ----------
    @staticmethod
    def _clear_layout(lay):
        """彻底清空布局：递归收集所有 widget 后立即 setParent(None) + deleteLater。

        之前的实现只处理直接子 widget。嵌套布局（head / btns 等）里的按钮和标签
        takeAt 后仍挂在 detail 下，下次刷新就出现「上一份残留 + 当前份」的
        双重渲染（最显眼的就是一个超大红色删除按钮盖住整页）。
        setParent(None) 立即切断父子关系、findChildren 找不到、不再参与绘制；
        deleteLater 再释放内存。
        """
        widgets: list = []
        def collect(l):
            while l.count():
                it = l.takeAt(0)
                w = it.widget()
                if w is not None:
                    widgets.append(w)
                else:
                    sub = it.layout()
                    if sub is not None:
                        collect(sub)
        collect(lay)
        for w in widgets:
            w.setParent(None)
            w.deleteLater()

    def _badge(self, text: str, color: str) -> QLabel:
        b = QLabel(text)
        b.setStyleSheet(
            f"background: {color}; color: white; border-radius: 9px;"
            "padding: 1px 10px; font-size: 9pt; font-weight: 600;")
        return b

    def _detail_label(self, text: str, value_color: str = "#24292f") -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet(f"color: {value_color}; font-size: 10pt;")
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setWordWrap(True)
        return lab

    def _info_row(self, form, label: str, value: str, color: str = "#24292f"):
        form.addRow(label, self._detail_label(value, color))

    def refresh_detail(self):
        task = self._selected_task()
        self._clear_layout(self.detail_lay)
        if task is None:
            empty = QLabel("← 左侧选择一个定时任务查看详情\n\n"
                           "点击「＋ 新建定时任务」开始创建。")
            empty.setStyleSheet("color: #8a939c; font-size: 11pt;")
            empty.setAlignment(Qt.AlignCenter)
            self.detail_lay.addWidget(empty)
            self.detail_lay.addStretch(1)
            return

        # 标题 + 状态徽章
        head = QHBoxLayout()
        name_lab = QLabel(task.name)
        name_lab.setStyleSheet("font-size: 14pt; font-weight: 600; color: #24292f;")
        head.addWidget(name_lab)
        head.addStretch(1)
        badge = self._badge("已启用", "#2f9e5b") if task.enabled else self._badge("已停用", "#a7afb8")
        head.addWidget(badge)
        self.detail_lay.addLayout(head)

        self.detail_lay.addWidget(self._divider())

        # 信息
        info = QWidget()
        form = QFormLayout(info)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        flow = self._flow_of(task)
        flow_txt = self._flow_name(task)
        if flow is not None and flow.group:
            flow_txt = f"{flow.group} · {flow.name}"
        self._info_row(form, "运行流程", flow_txt)
        self._info_row(form, "调度规则", describe_schedule(task), "#1668a8")
        self._info_row(form, "下次运行", task.next_run or "—")
        self._info_row(form, "上次运行", task.last_run or "—")
        self._info_row(form, "所属分组", task.group or "未分组")
        self.detail_lay.addWidget(info)

        # 预览
        prev_box = QWidget()
        prev_box.setStyleSheet("background: #eef5fb; border: 1px solid #d5e6f5;"
                               "border-radius: 6px;")
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(10, 8, 10, 8)
        pv.setSpacing(2)
        pv.addWidget(self._detail_label("接下来 5 次运行时间", "#1668a8"))
        times = next_run_times(task, datetime.now(), _PREVIEW_N)
        if not times:
            pv.addWidget(self._detail_label("当前规则无可用运行时间", "#c0392b"))
        else:
            for i, dt in enumerate(times, 1):
                pv.addWidget(self._detail_label(f"第 {i} 次   {format_dt(dt)}", "#57606a"))
        self.detail_lay.addWidget(prev_box)

        self.detail_lay.addStretch(1)

        # 操作按钮
        btns = QHBoxLayout()
        run_btn = QPushButton("▶ 立即运行")
        run_btn.setToolTip("立即执行一次该任务对应的流程")
        run_btn.clicked.connect(self._run_now)
        set_variant(run_btn, "success")
        edit_btn = QPushButton("✎ 编辑")
        edit_btn.clicked.connect(self._edit_task)
        set_variant(edit_btn, "primary")
        toggle_btn = QPushButton("■ 停用" if task.enabled else "▶ 启用")
        toggle_btn.clicked.connect(self._toggle_enabled)
        del_btn = QPushButton("🗑 删除")
        del_btn.clicked.connect(self._del_task)
        set_variant(del_btn, "danger")
        btns.addWidget(run_btn)
        btns.addWidget(edit_btn)
        btns.addWidget(toggle_btn)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        self.detail_lay.addLayout(btns)

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e3e8ee; background: #e3e8ee; max-height: 1px; border: none;")
        return line

    # ---------- 任务操作 ----------
    def _new_task(self, group: str = ""):
        dlg = ScheduleDialog(None, self.cfg.flows, self.cfg.schedule_groups, self)
        if dlg.exec() == ScheduleDialog.Accepted:
            self._tasks.append(dlg._task)
            self._after_task_change(dlg._task, select=True)
            log(f"已创建定时任务「{dlg._task.name}」")

    def _edit_task(self):
        task = self._selected_task()
        if task is None:
            return
        dlg = ScheduleDialog(task, self.cfg.flows, self.cfg.schedule_groups, self)
        if dlg.exec() == ScheduleDialog.Accepted:
            self.runner.invalidate(task.id)
            self._after_task_change(task, select=True)
            log(f"已更新定时任务「{task.name}」")

    def _del_task(self):
        task = self._selected_task()
        if task is None:
            return
        if QMessageBox.question(self, "删除定时任务",
                                f"确定删除定时任务「{task.name}」吗？") != QMessageBox.Yes:
            return
        self._tasks.remove(task)
        self.runner.invalidate(task.id)
        self._after_task_change(None, select=False)

    def _toggle_enabled(self):
        task = self._selected_task()
        if task is None:
            return
        task.enabled = not task.enabled
        if task.enabled:
            task.next_run = format_dt(next_run_time(task, datetime.now()))
        else:
            task.next_run = ""
        self.runner.invalidate(task.id)
        self._after_task_change(task, select=True)

    def _run_now(self):
        task = self._selected_task()
        if task is None:
            return
        self._fire(task)

    def _after_task_change(self, task, select: bool):
        self.cfg.save()
        self.refresh_list()
        if select and task is not None:
            self._select_task_item(task.id)
        self.changed.emit()

    # ---------- 分组管理 ----------
    def _add_group(self):
        name, ok = QInputDialog.getText(self, "添加分组", "请输入分组名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.cfg.schedule_groups:
            QMessageBox.information(self, "分组已存在", f"分组「{name}」已经存在。")
            return
        self.cfg.schedule_groups.append(name)
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    def _rename_group(self, g: str):
        name, ok = QInputDialog.getText(self, "重命名分组", "新的分组名称：", text=g)
        name = (name or "").strip()
        if not ok or not name or name == g:
            return
        if name in self.cfg.schedule_groups:
            QMessageBox.information(self, "分组已存在", f"分组「{name}」已经存在。")
            return
        self.cfg.schedule_groups = [name if x == g else x for x in self.cfg.schedule_groups]
        # 同步更新收起状态：否则重命名后分组会「被意外展开」。
        self.cfg.collapsed_schedule_groups = [
            name if x == g else x for x in self.cfg.collapsed_schedule_groups
        ]
        for t in self._tasks:
            if t.group == g:
                t.group = name
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    def _del_group(self, g: str):
        if QMessageBox.question(self, "删除分组",
                                f"确定删除分组「{g}」吗？\n组内任务将移到「未分组」。"
                                ) != QMessageBox.Yes:
            return
        self.cfg.schedule_groups = [x for x in self.cfg.schedule_groups if x != g]
        self.cfg.collapsed_schedule_groups = [x for x in self.cfg.collapsed_schedule_groups if x != g]
        for t in self._tasks:
            if t.group == g:
                t.group = ""
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    # ---------- 右键菜单 ----------
    def _context_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "group":
            self._group_context_menu(data[1], self.list.viewport().mapToGlobal(pos))
            return
        task = next((t for t in self._tasks if t.id == data[1]), None)
        if task is None:
            return
        self.list.setCurrentItem(item)
        menu = QMenu(self)
        self._style_menu(menu)
        run_act = menu.addAction("▶ 立即运行")
        edit_act = menu.addAction("✎ 编辑")
        toggle_act = menu.addAction("■ 停用" if task.enabled else "▶ 启用")
        menu.addSeparator()
        del_act = menu.addAction("🗑 删除")
        act = menu.exec(self.list.viewport().mapToGlobal(pos))
        if act == run_act:
            self._run_now()
        elif act == edit_act:
            self._edit_task()
        elif act == toggle_act:
            self._toggle_enabled()
        elif act == del_act:
            self._del_task()

    def _group_context_menu(self, g: str, pos):
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
                background: #ffffff; border: 1px solid #d8dee4;
                border-radius: 8px; padding: 6px; font-size: 12pt;
            }
            QMenu::item {
                color: #24292f; padding: 9px 32px 9px 14px;
                margin: 2px 4px; border-radius: 6px;
            }
            QMenu::item:selected { background: #1668a8; color: #ffffff; }
            QMenu::item:disabled { color: #a7afb8; background: transparent; }
        """)

    # ---------- 调度触发 ----------
    def _on_due(self, task_id: str, flow_id: str):
        """调度线程到期回调（经信号排队已在主线程）。

        无人值守触发：silent=True，流程结束失败时不弹模态框打扰用户，
        只走状态栏提示与日志（用户手动「立即运行」仍保留弹窗反馈）。
        """
        task = next((t for t in self._tasks if t.id == task_id), None)
        if task is None:
            return
        self._fire(task, silent=True)

    def _fire(self, task: ScheduleTask, silent: bool = False):
        now = datetime.now()
        flow = self._flow_of(task)
        if flow is None or not flow.steps:
            log(f"定时任务「{task.name}」未执行：找不到流程或流程为空，已自动停用")
            task.enabled = False
            task.next_run = ""
            self.cfg.save()
            self.refresh_list()
            return
        started = self.flow_tab.start_flow_if_idle(task.flow_id, silent=silent)
        if started:
            task.missed_fires = 0
            task.last_run = now.strftime("%Y-%m-%d %H:%M:%S")
            log(f"定时任务「{task.name}」触发：运行流程「{flow.name}」")
            if task.mode == "once":
                task.enabled = False
                task.next_run = ""
                log(f"定时任务「{task.name}」为一次性任务，执行后已自动停用")
            else:
                task.next_run = format_dt(next_run_time(task, now))
        else:
            log(f"定时任务「{task.name}」触发：流程「{flow.name}」已在运行，跳过本次")
            # last_run 不更新：流程没真正启动，记录保持原状更准确；
            # 下面 next_run 仍要刷新，循环型任务按规则继续排期。
            if task.mode == "once":
                # 一次性任务遇流程繁忙：60 秒后自动重试，最多 3 次后停用（避免死循环）。
                task.missed_fires += 1
                if task.missed_fires > 3:
                    task.enabled = False
                    task.next_run = ""
                    log(f"一次性任务「{task.name}」连续 {task.missed_fires} 次因流程繁忙未执行，已放弃")
                else:
                    new_once = now + timedelta(seconds=60)
                    task.once_at = new_once.strftime("%Y-%m-%d %H:%M:%S")
                    task.next_run = format_dt(new_once)
                    log(f"一次性任务「{task.name}」流程繁忙，60 秒后重试"
                        f"（第 {task.missed_fires}/3 次）")
            else:
                task.next_run = format_dt(next_run_time(task, now))
        self.runner.invalidate(task.id)
        self.cfg.save()
        self.refresh_list()

    # ---------- 外部联动 ----------
    def on_flows_changed(self):
        """流程增删改后：刷新流程名兜底显示，并重建列表。

        流程被删除时，对应定时任务立即停用并清空下次时间——
        避免任务一直「启用」状态显示在下一次触发时才发现「流程没了」。
        """
        changed = False
        for t in self._tasks:
            flow = self._flow_of(t)
            if flow is not None:
                if t.flow_name != flow.name:
                    t.flow_name = flow.name
                    changed = True
                continue
            # 流程已删除：立即停用，避免下次触发时才暴露
            if t.enabled:
                t.enabled = False
                t.next_run = ""
                self.runner.invalidate(t.id)
                changed = True
                log(f"定时任务「{t.name}」的流程已被删除，已自动停用")
        if changed:
            self.cfg.save()
        self.refresh_list()

    def shutdown(self):
        self.runner.stop()
