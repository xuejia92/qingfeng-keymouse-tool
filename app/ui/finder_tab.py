"""找图点击页：多图任务列表 + 截图取模 + 每任务独立热键启停。"""
from __future__ import annotations

import dataclasses
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..config import FindTask, resolve_template_path
from ..capture_overlay import run_screen_capture
from ..keymap import hotkey_display
from ..tasks import FindTaskRunner
from .finder_dialog import FindTaskDialog
from .widgets import set_variant

_COLS = ["启用", "预览", "名称", "热键", "间隔", "置信度", "次数上限", "状态"]
_COL_ENABLE, _COL_PREVIEW, _COL_NAME, _COL_HOTKEY, _COL_INTERVAL, _COL_CONF, _COL_COUNT, _COL_STATUS = range(8)

_DEFAULT_HOTKEYS = ["f8", "f9", "f10", "f11", "f12",
                    "ctrl+alt+1", "ctrl+alt+2", "ctrl+alt+3", "ctrl+alt+4", "ctrl+alt+5"]


class FinderTab(QWidget):
    changed = Signal()                 # 配置发生增删改
    runningStateChanged = Signal()     # 任一任务运行状态变化（用于托盘同步）
    captureAboutToStart = Signal()     # 即将截屏取模（主窗口应先隐藏）
    captureFinished = Signal()         # 截屏取模结束（主窗口可恢复）

    def __init__(self, tasks: list[FindTask], parent=None):
        super().__init__(parent)
        self._tasks = tasks            # 直接引用 AppConfig.find_tasks
        # _runners 只登记「正在运行」的 runner：任务一结束就摘掉。
        # 原来只增不减——删掉任务后条目还留着，主窗口每 500ms 遍历一次它，
        # 反复增删任务就会让这个字典一直变大。
        self._runners: dict[str, FindTaskRunner] = {}
        self._last_results: dict[str, str] = {}   # task_id -> 上次运行结束原因
        self._updating = False
        self._build_ui()
        self.refresh_table()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)

        bar = QHBoxLayout()
        self.add_btn = QPushButton("📷 添加模板（屏幕截图选区）")
        set_variant(self.add_btn, "success")
        self.edit_btn = QPushButton("编辑")
        set_variant(self.edit_btn, "primary")
        self.del_btn = QPushButton("删除")
        set_variant(self.del_btn, "danger")
        self.toggle_btn = QPushButton("▶ 启动/停止选中")
        set_variant(self.toggle_btn, "success")
        self.start_all_btn = QPushButton("▶ 启动全部启用任务")
        set_variant(self.start_all_btn, "success")
        self.stop_all_btn = QPushButton("■ 停止全部")
        set_variant(self.stop_all_btn, "danger")
        for b in (self.add_btn, self.edit_btn, self.del_btn, self.toggle_btn,
                  self.start_all_btn, self.stop_all_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(_COL_NAME, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(_COL_STATUS, QHeaderView.Stretch)
        self.table.setColumnWidth(_COL_ENABLE, 46)
        self.table.setColumnWidth(_COL_PREVIEW, 64)
        self.table.setColumnWidth(_COL_HOTKEY, 110)
        self.table.setColumnWidth(_COL_INTERVAL, 80)
        self.table.setColumnWidth(_COL_CONF, 70)
        self.table.setColumnWidth(_COL_COUNT, 80)
        self.table.setWordWrap(False)
        root.addWidget(self.table, 1)

        tip = QLabel("提示：双击行可编辑；间隔为命中点击后的轮询节奏；置信度越低越容易误判，"
                     "建议 0.8~0.95。模板截图时的屏幕缩放/分辨率需与运行时一致。")
        tip.setStyleSheet("color: #888;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.add_btn.clicked.connect(self.add_template)
        self.edit_btn.clicked.connect(self.edit_selected)
        self.del_btn.clicked.connect(self.delete_selected)
        self.toggle_btn.clicked.connect(self.toggle_selected)
        self.start_all_btn.clicked.connect(self.start_enabled_all)
        self.stop_all_btn.clicked.connect(self.stop_all)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        self.table.itemChanged.connect(self._on_item_changed)

    # ---------- 表格 ----------
    def refresh_table(self) -> None:
        self._updating = True
        self.table.setRowCount(len(self._tasks))
        for row, t in enumerate(self._tasks):
            # 启用
            it = QTableWidgetItem()
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            it.setCheckState(Qt.Checked if t.enabled else Qt.Unchecked)
            it.setData(Qt.UserRole, t.id)
            self.table.setItem(row, _COL_ENABLE, it)
            # 预览
            pm = QPixmap(resolve_template_path(t.image, t.image_path) or "")
            if not pm.isNull():
                self.table.setItem(row, _COL_PREVIEW,
                                   QTableWidgetItem(QIcon(pm.scaled(48, 48, Qt.KeepAspectRatio,
                                                                    Qt.SmoothTransformation)), ""))
            else:
                self.table.setItem(row, _COL_PREVIEW, QTableWidgetItem("缺失"))
            # 名称/热键/间隔/置信度/次数
            self.table.setItem(row, _COL_NAME, QTableWidgetItem(t.name))
            self.table.setItem(row, _COL_HOTKEY, QTableWidgetItem(hotkey_display(t.hotkey)))
            self.table.setItem(row, _COL_INTERVAL, QTableWidgetItem(f"{t.interval_ms} ms"))
            self.table.setItem(row, _COL_CONF, QTableWidgetItem(f"{t.confidence:.2f}"))
            self.table.setItem(row, _COL_COUNT,
                               QTableWidgetItem("∞" if t.count == 0 else str(t.count)))
            # 状态：运行中看 runner，已结束看上次结束原因
            runner = self._runners.get(t.id)
            running = bool(runner and runner.is_running)
            if running:
                status_text = "运行中"
            else:
                last = self._last_results.get(t.id, "")
                status_text = "空闲" if not last else f"停止（{last}）"
            status_item = QTableWidgetItem(status_text)
            if running:
                status_item.setForeground(QColor("#27ae60"))
            self.table.setItem(row, _COL_STATUS, status_item)
        self._updating = False
        # 默认选中第一行，避免"启动/停止选中"无对象可用
        if self.table.rowCount() > 0 and self.table.currentRow() < 0:
            self.table.selectRow(0)

    def _row_of(self, task_id: str) -> int | None:
        for row in range(self.table.rowCount()):
            it = self.table.item(row, _COL_ENABLE)
            if it and it.data(Qt.UserRole) == task_id:
                return row
        return None

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != _COL_ENABLE:
            return
        task_id = item.data(Qt.UserRole)
        task = self._find(task_id)
        if task is not None and task.enabled != (item.checkState() == Qt.Checked):
            task.enabled = item.checkState() == Qt.Checked
            self.changed.emit()

    def _find(self, task_id: str) -> FindTask | None:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def _selected_task(self) -> FindTask | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._tasks):
            return None
        return self._tasks[row]

    # ---------- 增删改 ----------
    def add_template(self) -> None:
        # 主窗口会先隐藏；留 250ms 等它真正从屏幕消失后再抓屏，避免截进自己
        self.captureAboutToStart.emit()
        QTimer.singleShot(250, self._start_capture)

    def _start_capture(self) -> None:
        try:
            run_screen_capture(self._on_template_saved, self._on_capture_cancelled)
        except Exception:
            # 遮罩构造失败也要恢复主窗口，避免界面"消失"
            self.captureFinished.emit()

    def _on_template_saved(self, path: str) -> None:
        self.captureFinished.emit()
        name, ok = QInputDialog.getText(self, "任务名称", "给这个找图任务起个名字：",
                                        text=f"找图任务 {len(self._tasks) + 1}")
        if not ok:
            name = f"找图任务 {len(self._tasks) + 1}"
        task = FindTask(name=name.strip() or f"找图任务 {len(self._tasks) + 1}",
                        image=os.path.basename(path),
                        image_path=os.path.abspath(path),
                        hotkey=self._next_hotkey())
        self._tasks.append(task)
        self.refresh_table()
        self.changed.emit()

    def _on_capture_cancelled(self) -> None:
        self.captureFinished.emit()

    def _next_hotkey(self) -> str:
        used = {t.hotkey for t in self._tasks}
        for cand in _DEFAULT_HOTKEYS:
            if cand not in used:
                return cand
        return ""

    def edit_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            sb = self.window().statusBar()
            if hasattr(sb, "showMessage"):
                sb.showMessage("请先在列表中点选一个找图任务", 4000)
            return
        dlg = FindTaskDialog(task, self)
        dlg.regionCaptureRequested.connect(lambda: self._capture_region_for_dialog(dlg))
        dlg.templateCaptureRequested.connect(lambda: self._capture_template_for_dialog(dlg))

        # 非模态 + finished 保存：框选区域时 hide() 不会像 exec() 那样立刻
        # 以 Rejected 结束编辑会话导致修改丢失
        def _finished(result):
            if result == FindTaskDialog.Accepted:
                dlg.apply_to(task)
                self.refresh_table()
                self.changed.emit()

        dlg.finished.connect(_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _capture_region_for_dialog(self, dlg: FindTaskDialog) -> None:
        """框选找图区域：主窗口与对话框都先隐藏，选完把区域写回对话框。"""
        self.captureAboutToStart.emit()  # 主窗口隐藏
        QTimer.singleShot(250, lambda: self._start_region_capture(dlg))

    def _start_region_capture(self, dlg: FindTaskDialog) -> None:
        try:
            run_screen_capture(
                on_region=lambda rect: self._region_picked(dlg, rect),
                on_cancelled=lambda: self._region_capture_done(dlg),
            )
        except Exception:
            self._region_capture_done(dlg)

    def _region_picked(self, dlg: FindTaskDialog, rect: tuple[int, int, int, int]) -> None:
        self._region_capture_done(dlg)
        dlg.set_region(rect)

    def _region_capture_done(self, dlg: FindTaskDialog) -> None:
        self.captureFinished.emit()  # 主窗口恢复
        dlg.finish_region_capture()  # 对话框恢复

    def _capture_template_for_dialog(self, dlg: FindTaskDialog) -> None:
        """重新截图模板：主窗口隐藏 -> 遮罩 -> 新模板回写对话框（点确定后生效）。"""
        self.captureAboutToStart.emit()
        QTimer.singleShot(250, lambda: self._start_template_capture(dlg))

    def _start_template_capture(self, dlg: FindTaskDialog) -> None:
        def done(path=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if path:
                    dlg.set_template_image(os.path.basename(path), os.path.abspath(path))
                dlg.finish_template_capture()
        try:
            run_screen_capture(on_saved=lambda p: done(p), on_cancelled=lambda: done())
        except Exception:
            done()

    def delete_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            sb = self.window().statusBar()
            if hasattr(sb, "showMessage"):
                sb.showMessage("请先在列表中点选一个找图任务", 4000)
            return
        if QMessageBox.question(self, "删除任务",
                                f"确定删除任务「{task.name}」吗？（模板文件保留在 templates 目录）"
                                ) != QMessageBox.Yes:
            return
        self.stop_task(task.id)
        self._runners.pop(task.id, None)      # 任务都删了，别再留着它的 runner
        self._last_results.pop(task.id, None)
        if task.image:
            pass  # 保留模板文件，用户可手动清理
        self._tasks.remove(task)
        self.refresh_table()
        self.changed.emit()

    # ---------- 运行控制 ----------
    def toggle_task(self, task_id: str) -> None:
        task = self._find(task_id)
        if task is None:
            return
        runner = self._runners.get(task.id)
        if runner and runner.is_running:
            runner.stop()
            return
        if not task.enabled:
            task.enabled = True
            self.refresh_table()
            self.changed.emit()
        runner = FindTaskRunner(dataclasses.replace(task))
        runner.stateChanged.connect(lambda state, reason, tid=task.id:
                                    self._on_runner_state(tid, state, reason))
        runner.progress.connect(lambda done, elapsed, tid=task.id:
                                self._on_runner_progress(tid, done, elapsed))
        self._runners[task.id] = runner
        runner.start()

    def toggle_selected(self) -> None:
        task = self._selected_task()
        if task is None:
            sb = self.window().statusBar()
            if hasattr(sb, "showMessage"):
                sb.showMessage("请先在列表中点选一个找图任务", 4000)
            return
        self.toggle_task(task.id)

    def start_enabled_all(self) -> None:
        for t in list(self._tasks):
            if t.enabled and not self._is_running(t.id):
                self.toggle_task(t.id)

    def stop_all(self) -> None:
        for tid, runner in list(self._runners.items()):
            if runner.is_running:
                runner.stop()

    def stop_task(self, task_id: str) -> None:
        runner = self._runners.get(task_id)
        if runner and runner.is_running:
            runner.stop()

    def stop_all_and_clear(self) -> None:
        self.stop_all()

    def _is_running(self, task_id: str) -> bool:
        runner = self._runners.get(task_id)
        return bool(runner and runner.is_running)

    def any_running(self) -> bool:
        return any(r.is_running for r in self._runners.values())

    def running_names(self) -> list[str]:
        """当前正在运行的找图任务名（给主窗口等外部模块查状态用）。

        外部不要直接读 _runners：那是本 tab 的内部实现细节，依赖它会让以后
        调整 runner 存储方式时牵连到别的模块。
        """
        names = []
        for t in self._tasks:
            runner = self._runners.get(t.id)
            if runner is not None and runner.is_running:
                names.append(t.name)
        return names

    def shutdown(self) -> None:
        self.stop_all()

    # ---------- 状态回调 ----------
    def _on_runner_state(self, task_id: str, state: str, reason: str) -> None:
        if state != "running":
            # 跑完了就把 runner 摘掉，别让字典越攒越大；结束原因另存，
            # 这样列表刷新时还能显示「停止（原因）」。
            self._runners.pop(task_id, None)
            self._last_results[task_id] = reason or ""
        row = self._row_of(task_id)
        if row is not None:
            if state == "running":
                item = QTableWidgetItem("运行中")
                item.setForeground(QColor("#27ae60"))
            else:
                text = "空闲" if not reason else f"停止（{reason}）"
                item = QTableWidgetItem(text)
            self.table.setItem(row, _COL_STATUS, item)
        self.runningStateChanged.emit()

    def _on_runner_progress(self, task_id: str, done: int, elapsed: float) -> None:
        row = self._row_of(task_id)
        if row is not None:
            self.table.setItem(row, _COL_STATUS,
                               QTableWidgetItem(f"运行中 · 命中 {done} 次 · {elapsed:.0f}s"))
