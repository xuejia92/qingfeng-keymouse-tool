"""定时任务编辑对话框：名称/分组/运行流程 + 调度规则 + 实时预览下次运行时间。

调度模式通过下拉切换，参数区用 QStackedWidget 按模式显示对应控件；
任一参数变化都会实时刷新「接下来 5 次运行」预览。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QSpinBox,
                               QStackedWidget, QTimeEdit, QDateTimeEdit, QVBoxLayout,
                               QWidget)

from ..config import SCHEDULE_MODES, WEEKDAY_NAMES, Flow, ScheduleTask
from ..scheduler import (describe_schedule, format_dt, next_run_times,
                         parse_cron)

_PREVIEW_N = 5


class ScheduleDialog(QDialog):
    def __init__(self, task: ScheduleTask | None, flows: list[Flow],
                 groups: list[str], parent=None, default_group: str = ""):
        super().__init__(parent)
        self.setWindowTitle("新建定时任务" if task is None else "编辑定时任务")
        self.setMinimumWidth(520)
        self._task = task or ScheduleTask()
        # 从某分组标题的「＋」新建时，默认归属到该分组（编辑模式不受影响）
        if task is None and default_group:
            self._task.group = default_group
        self._flows = list(flows or [])
        self._groups = list(groups or [])
        self._build()
        self._fill()
        self._connect_preview()
        self._on_mode_changed()   # 按初始模式切换到对应参数页
        self._refresh_preview()

    # ---------- UI ----------
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("任务名称", self.name_edit)

        self.group_combo = QComboBox()
        self.group_combo.addItem("未分组", "")
        for g in self._groups:
            self.group_combo.addItem(g, g)
        form.addRow("所属分组", self.group_combo)

        self.flow_combo = QComboBox()
        for f in self._flows:
            label = f"{f.group} · {f.name}" if f.group else f.name
            self.flow_combo.addItem(label, f.id)
        if not self._flows:
            self.flow_combo.addItem("（暂无可用流程，请先在自动化流程页创建）", "")
            self.flow_combo.setEnabled(False)
        form.addRow("要运行的流程", self.flow_combo)

        self.mode_combo = QComboBox()
        for k, v in SCHEDULE_MODES.items():
            self.mode_combo.addItem(v, k)
        form.addRow("调度规则", self.mode_combo)
        root.addLayout(form)

        # 参数区：按模式切换
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_second())
        self.stack.addWidget(self._page_minute())
        self.stack.addWidget(self._page_hour())
        self.stack.addWidget(self._page_day())
        self.stack.addWidget(self._page_week())
        self.stack.addWidget(self._page_month())
        self.stack.addWidget(self._page_once())
        self.stack.addWidget(self._page_cron())
        root.addWidget(self.stack)

        # 预览面板
        self.preview_box = QGroupBox("接下来 5 次运行时间")
        pv = QVBoxLayout(self.preview_box)
        self.summary_label = QLabel()
        self.summary_label.setStyleSheet("color: #1668a8; font-weight: 600;")
        pv.addWidget(self.summary_label)
        self.preview_label = QLabel()
        self.preview_label.setTextFormat(Qt.RichText)
        self.preview_label.setWordWrap(False)
        pv.addWidget(self.preview_label)
        root.addWidget(self.preview_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _spin(self) -> QSpinBox:
        s = QSpinBox()
        s.setRange(1, 99999)
        s.setSuffix(" 秒")
        return s

    def _time_edit(self) -> QTimeEdit:
        t = QTimeEdit()
        t.setDisplayFormat("HH:mm")
        return t

    def _wrap(self, title: str, w: QWidget) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 4)
        lab = QLabel(title)
        lab.setStyleSheet("color: #57606a;")
        lay.addWidget(lab)
        lay.addWidget(w)
        lay.addStretch(1)
        return page

    def _page_second(self) -> QWidget:
        self.second_spin = QSpinBox()
        self.second_spin.setRange(1, 86400)
        self.second_spin.setSuffix(" 秒")
        return self._wrap("每隔多少秒执行一次", self.second_spin)

    def _page_minute(self) -> QWidget:
        self.minute_spin = QSpinBox()
        self.minute_spin.setRange(1, 1440)
        self.minute_spin.setSuffix(" 分钟")
        return self._wrap("每隔多少分钟执行一次", self.minute_spin)

    def _page_hour(self) -> QWidget:
        self.hour_spin = QSpinBox()
        self.hour_spin.setRange(1, 24)
        self.hour_spin.setSuffix(" 小时")
        return self._wrap("每隔多少小时执行一次", self.hour_spin)

    def _page_day(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.day_time = self._time_edit()
        lay.addWidget(QLabel("每天触发时刻"))
        lay.addWidget(self.day_time)
        lay.addWidget(QLabel("  每隔"))
        self.day_interval = QSpinBox()
        self.day_interval.setRange(1, 365)
        self.day_interval.setSuffix(" 天")
        lay.addWidget(self.day_interval)
        lay.addStretch(1)
        return self._wrap("每天在指定时刻执行（可设每隔 N 天）", w)

    def _page_week(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        day_row = QHBoxLayout()
        self.week_checks: dict[int, QCheckBox] = {}
        for d in range(1, 8):
            cb = QCheckBox(WEEKDAY_NAMES[d])
            self.week_checks[d] = cb
            day_row.addWidget(cb)
        day_row.addStretch(1)
        lay.addLayout(day_row)
        time_row = QHBoxLayout()
        self.week_time = self._time_edit()
        time_row.addWidget(QLabel("触发时刻"))
        time_row.addWidget(self.week_time)
        time_row.addStretch(1)
        lay.addLayout(time_row)
        return self._wrap("选择每周的哪些天执行", w)

    def _page_month(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        self.month_checks: dict[int, QCheckBox] = {}
        for d in range(1, 32):
            cb = QCheckBox(str(d))
            cb.setFixedWidth(38)
            self.month_checks[d] = cb
            grid.addWidget(cb, (d - 1) // 8, (d - 1) % 8)
        lay.addLayout(grid)
        time_row = QHBoxLayout()
        self.month_time = self._time_edit()
        time_row.addWidget(QLabel("触发时刻"))
        time_row.addWidget(self.month_time)
        time_row.addStretch(1)
        lay.addLayout(time_row)
        return self._wrap("选择每月的哪些天执行（未选中的日期当月不执行）", w)

    def _page_once(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.once_edit = QDateTimeEdit()
        self.once_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.once_edit.setCalendarPopup(True)
        self.once_edit.setMinimumDateTime(QDateTime.currentDateTime().addSecs(60))
        lay.addWidget(QLabel("执行一次的时间"))
        lay.addWidget(self.once_edit, 1)
        return self._wrap("在指定日期时间执行一次（执行后自动停用）", w)

    def _page_cron(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.cron_edit = QLineEdit()
        self.cron_edit.setPlaceholderText("如 */5 * * * *（每 5 分钟）")
        lay.addWidget(self.cron_edit)
        hint = QLabel("标准 5 段：分 时 日 月 周；也支持 6 段（秒在最前）。\n"
                      "示例：0 9 * * 1-5 每个工作日 09:00 · 0 0 1 * * 每月 1 号 00:00 · "
                      "*/30 * * * * 每 30 分钟。")
        hint.setStyleSheet("color: #8a939c; font-size: 9pt;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return self._wrap("Cron 表达式", w)

    # ---------- 数据 ----------
    def _fill(self):
        t = self._task
        self.name_edit.setText(t.name)
        idx = self.group_combo.findData(t.group)
        self.group_combo.setCurrentIndex(max(0, idx))
        fidx = self.flow_combo.findData(t.flow_id)
        self.flow_combo.setCurrentIndex(max(0, fidx))
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(t.mode)))

        self.second_spin.setValue(max(1, int(t.interval or 1)))
        self.minute_spin.setValue(max(1, int(t.interval or 1)))
        self.hour_spin.setValue(max(1, int(t.interval or 1)))
        self.day_interval.setValue(max(1, int(t.interval or 1)))
        self._set_time(self.day_time, t.at_time)
        self._set_time(self.week_time, t.at_time)
        self._set_time(self.month_time, t.at_time)
        for d in range(1, 8):
            self.week_checks[d].setChecked(d in (t.weekdays or []))
        for d in range(1, 32):
            self.month_checks[d].setChecked(d in (t.monthdays or []))
        self._set_datetime(self.once_edit, t.once_at)
        self.cron_edit.setText(t.cron or "")

    @staticmethod
    def _set_time(edit: QTimeEdit, s: str):
        from PySide6.QtCore import QTime
        try:
            h, m = (str(s or "09:00").split(":") + ["0"])[:2]
            edit.setTime(QTime(int(h), int(m)))
        except (ValueError, AttributeError):
            pass

    @staticmethod
    def _set_datetime(edit: QDateTimeEdit, s: str):
        dt = None
        for fmt in ("yyyy-MM-dd HH:mm", "yyyy-MM-dd HH:mm:ss"):
            dt = QDateTime.fromString(s or "", fmt)
            if dt.isValid():
                break
        if dt is None or not dt.isValid():
            dt = QDateTime.currentDateTime().addSecs(3600)
        edit.setDateTime(dt)

    def _build_task(self) -> ScheduleTask:
        t = ScheduleTask()
        t.name = self.name_edit.text().strip()
        t.group = self.group_combo.currentData() or ""
        t.flow_id = self.flow_combo.currentData() or ""
        t.mode = self.mode_combo.currentData() or "day"
        t.interval = int(self._interval_for_mode())
        t.at_time = self._time_text(self._time_edit_for_mode())
        t.weekdays = [d for d in range(1, 8) if self.week_checks[d].isChecked()]
        t.monthdays = [d for d in range(1, 32) if self.month_checks[d].isChecked()]
        t.once_at = self.once_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        t.cron = self.cron_edit.text().strip()
        return t

    def _time_edit_for_mode(self) -> QTimeEdit:
        mode = self.mode_combo.currentData()
        if mode == "week":
            return self.week_time
        if mode == "month":
            return self.month_time
        return self.day_time

    def _interval_for_mode(self) -> int:
        mode = self.mode_combo.currentData()
        return {"second": self.second_spin.value(),
                "minute": self.minute_spin.value(),
                "hour": self.hour_spin.value(),
                "day": self.day_interval.value()}.get(mode, 1)

    @staticmethod
    def _time_text(edit: QTimeEdit) -> str:
        return edit.time().toString("HH:mm")

    # ---------- 预览 ----------
    def _connect_preview(self):
        self.name_edit.textChanged.connect(self._refresh_preview)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.flow_combo.currentIndexChanged.connect(self._refresh_preview)
        for w in (self.second_spin, self.minute_spin, self.hour_spin,
                  self.day_interval):
            w.valueChanged.connect(self._refresh_preview)
        for e in (self.day_time, self.week_time, self.month_time):
            e.timeChanged.connect(self._refresh_preview)
        for cb in list(self.week_checks.values()) + list(self.month_checks.values()):
            cb.toggled.connect(self._refresh_preview)
        self.once_edit.dateTimeChanged.connect(self._refresh_preview)
        self.cron_edit.textChanged.connect(self._refresh_preview)

    def _on_mode_changed(self):
        idx = self.mode_combo.currentIndex()
        self.stack.setCurrentIndex(idx)
        self._refresh_preview()

    def _refresh_preview(self, *_):
        task = self._build_task()
        self.summary_label.setText(describe_schedule(task))
        times = next_run_times(task, datetime.now(), _PREVIEW_N)
        if not times:
            self.preview_label.setText(
                '<span style="color:#c0392b;">当前规则没有可计算的运行时间（请检查参数）</span>')
            return
        lines = []
        for i, dt in enumerate(times, 1):
            lines.append(f'<span style="color:#57606a;">第 {i} 次</span>'
                         f'&nbsp;&nbsp;<b>{format_dt(dt)}</b>')
        self.preview_label.setText("<br>".join(lines))

    # ---------- 校验与回写 ----------
    def accept(self):
        task = self._build_task()
        if not task.name:
            QMessageBox.warning(self, "请填写名称", "请填写任务名称。")
            return
        if not task.flow_id:
            QMessageBox.warning(self, "请选择流程",
                                "请选择要运行的流程（若列表为空，请先到自动化流程页创建）。")
            return
        if task.mode == "week" and not task.weekdays:
            QMessageBox.warning(self, "请选择星期", "请至少勾选一个执行星期。")
            return
        if task.mode == "month" and not task.monthdays:
            QMessageBox.warning(self, "请选择日期", "请至少勾选一个执行日期。")
            return
        if task.mode == "cron":
            if parse_cron(task.cron) is None:
                QMessageBox.warning(
                    self, "Cron 表达式无效",
                    "无法解析该 Cron 表达式。请使用标准格式：分 时 日 月 周（如 0 9 * * 1-5），\n"
                    "或 6 段含秒（如 */10 * * * * *）。")
                return
        if task.mode == "once":
            dt = self.once_edit.dateTime()
            if dt <= QDateTime.currentDateTime():
                QMessageBox.warning(self, "时间已过期", "请选择一个晚于当前的时间。")
                return
        self._apply(task)
        super().accept()

    def _apply(self, task: ScheduleTask):
        t = self._task
        t.name = task.name
        t.group = task.group
        t.flow_id = task.flow_id
        t.mode = task.mode
        t.interval = task.interval
        t.at_time = task.at_time
        t.weekdays = task.weekdays
        t.monthdays = task.monthdays
        t.once_at = task.once_at
        t.cron = task.cron
        # 记录冗余流程名，便于流程改名后兜底显示
        flow = next((f for f in self._flows if f.id == t.flow_id), None)
        if flow is not None:
            t.flow_name = flow.name
