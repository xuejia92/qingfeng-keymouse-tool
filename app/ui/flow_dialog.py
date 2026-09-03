"""流程编辑相关控件：可拖模块按钮、步骤列表、步骤参数对话框、流程元信息对话框。

编排主界面在 FlowTab 的右栏完成（拖入 / 排序 / 双击改参数）；
FlowMetaDialog 只负责流程名 / 运行轮数 / 启停热键。
"""
from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt, QPoint, QRect, Signal, QMimeData
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QToolTip, QVBoxLayout, QWidget)

from ..config import VARIABLE_TYPES, WEB_ACTIONS, Flow, FlowStep
from ..conditions import check_condition_variables
from ..web_actors import LAUNCH_MODES, TAB_SCOPES
from .hotkey_edit import HotkeyEdit

MIME_TYPE = "application/x-qf-flow-type"

_TYPE_ICONS = {"var": "📦", "log": "📄", "ocr": "🔎", "text_find": "🔍",
               "screenshot": "📷", "find_image": "🎯", "yolo_detect": "🧠",
               "click": "🖱", "press": "⌨", "find": "🖼",
               "wait": "⏱", "web": "🌐", "app": "🚀", "close_app": "⏹",
               "clip_set": "📤", "clip_get": "📥", "py_func": "🐍",
               "if": "🔀", "elseif": "🔁", "else": "↩️", "endif": "🏁",
               "foreach": "🔄", "while": "♻️",
               "endForeach": "🏁", "endWhile": "🏁",
               "break": "🛑", "continue": "⏭️", "exit": "🔚"}


class ModuleButton(QPushButton):
    """可拖拽的模块按钮：拖动时携带步骤类型 MIME。"""

    def __init__(self, step_type: str, text: str, parent=None):
        super().__init__(f"{_TYPE_ICONS[step_type]} {text}", parent)
        self._type = step_type
        self.setToolTip("拖到左侧步骤列表中")

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(MIME_TYPE, self._type.encode())
            drag.setMimeData(mime)
            drag.exec(Qt.CopyAction)
        else:
            super().mouseMoveEvent(ev)


class StepRunDelegate(QStyledItemDelegate):
    """在步骤行右侧绘制「▶ 执行」单步按钮。

    按钮跟随行绘制（拖拽排序、增删步骤时自动跟随，不会像 setItemWidget 那样丢失）；
    点击检测由 StepList 完成，命中后发射 stepRunRequested 信号。
    """

    BTN_W, BTN_H, RIGHT_GAP = 58, 22, 8

    def __init__(self, list_view, parent=None):
        super().__init__(parent)
        self._list = list_view

    @classmethod
    def button_rect(cls, item_rect: QRect) -> QRect:
        return QRect(item_rect.right() - cls.RIGHT_GAP - cls.BTN_W + 1,
                     item_rect.center().y() - cls.BTN_H // 2, cls.BTN_W, cls.BTN_H)

    def paint(self, painter, option, index):
        # 正文可用宽度收窄，避免长文案压到按钮下面
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(0, 0, -(self.RIGHT_GAP * 2 + self.BTN_W), 0)
        super().paint(painter, opt, index)
        if index.data(Qt.UserRole) is None:      # 空流程占位行不画按钮
            return
        r = self.button_rect(option.rect)
        enabled = bool(option.state & QStyle.State_Enabled)
        pressed = getattr(self._list, "_press_run_row", None) == index.row()
        hovered = getattr(self._list, "_hover_row", -1) == index.row()
        if pressed:
            bg, border, text = QColor("#1668a8"), QColor("#125a92"), QColor("white")
        elif not enabled:
            bg, border, text = QColor("#f2f3f5"), QColor("#d8dee4"), QColor("#a7afb8")
        elif hovered:
            bg, border, text = QColor("#e8f1fa"), QColor("#8ab8d8"), QColor("#125a92")
        else:
            bg, border, text = QColor("#ffffff"), QColor("#b9d3e8"), QColor("#1668a8")
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(r, 4, 4)
        f = painter.font()
        f.setPointSizeF(8.5)
        painter.setFont(f)
        painter.setPen(text)
        painter.drawText(r, Qt.AlignCenter, "▶ 执行")
        painter.restore()

    def helpEvent(self, ev, view, option, index) -> bool:
        if (index.data(Qt.UserRole) is not None
                and self.button_rect(option.rect).contains(ev.pos())):
            QToolTip.showText(ev.globalPos(), "单步执行：只运行这一步", view)
            return True
        return super().helpEvent(ev, view, option, index)


class StepList(QListWidget):
    """步骤列表：接受模块拖入 + 内部拖拽排序，拖动时显示插入位置辅助线；
    每行右侧「▶ 执行」按钮点击后发射 stepRunRequested（单步执行）。"""

    stepDropped = Signal(str, int)   # (步骤类型, 插入行)
    orderChanged = Signal()
    stepRunRequested = Signal(int)   # 单步执行：步骤行号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setDefaultDropAction(Qt.MoveAction)
        self._drop_row_hint: int | None = None
        self._hover_row = -1
        self._press_run_row: int | None = None

    # ---------- 「▶ 执行」单步按钮 ----------
    def _run_btn_rect_at(self, pos):
        """pos 落在某行的「▶ 执行」按钮上时返回该行号，否则 None。"""
        idx = self.indexAt(pos)
        if not idx.isValid() or idx.data(Qt.UserRole) is None:
            return None
        rect = self.visualItemRect(self.itemFromIndex(idx))
        return idx.row() if StepRunDelegate.button_rect(rect).contains(pos) else None

    def mousePressEvent(self, ev):
        self._press_run_row = None
        if ev.button() == Qt.LeftButton:
            row = self._run_btn_rect_at(ev.position().toPoint())
            if row is not None:
                self._press_run_row = row
                self.viewport().update()
                ev.accept()          # 不进入选择/拖拽，视为按钮按下
                return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        row = self._press_run_row
        self._press_run_row = None
        self.viewport().update()
        if (ev.button() == Qt.LeftButton and row is not None
                and self._run_btn_rect_at(ev.position().toPoint()) == row):
            self.stepRunRequested.emit(row)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def mouseMoveEvent(self, ev):
        idx = self.indexAt(ev.position().toPoint())
        row = idx.row() if idx.isValid() else -1
        if row != self._hover_row:
            self._hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if self._hover_row != -1:
            self._hover_row = -1
            self.viewport().update()
        super().leaveEvent(ev)

    # QListWidget 默认只接受模型数据/URL 格式，自定义 MIME 必须显式放行
    def _accepted(self, ev) -> bool:
        return ev.source() is self or ev.mimeData().hasFormat(MIME_TYPE)

    def dragEnterEvent(self, ev):
        if self._accepted(ev):
            ev.accept()
            self._update_drop_hint(ev)
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if self._accepted(ev):
            ev.accept()
            self._update_drop_hint(ev)
        else:
            super().dragMoveEvent(ev)

    def dragLeaveEvent(self, ev):
        self._clear_drop_hint()
        super().dragLeaveEvent(ev)

    def _clear_drop_hint(self):
        if self._drop_row_hint is not None:
            self._drop_row_hint = None
            self.viewport().update()

    def _update_drop_hint(self, ev):
        """记录插入位置：落点在条目上半行插到其前，下半行插到其后。"""
        idx = self.indexAt(ev.position().toPoint())
        row = idx.row() if idx.isValid() else self.count()
        if idx.isValid():
            rect = self.visualItemRect(self.itemFromIndex(idx))
            if ev.position().y() > rect.center().y():
                row += 1
        if row != self._drop_row_hint:
            self._drop_row_hint = row
            self.viewport().update()

    def _drop_row(self, ev) -> int:
        if self._drop_row_hint is not None:
            return self._drop_row_hint
        idx = self.indexAt(ev.position().toPoint())
        row = idx.row() if idx.isValid() else self.count()
        if idx.isValid():
            rect = self.visualItemRect(self.itemFromIndex(idx))
            if ev.position().y() > rect.center().y():
                row += 1
        return row

    def dropEvent(self, ev):
        self._clear_drop_hint()
        if ev.source() is self:
            super().dropEvent(ev)
            ev.accept()
            self.orderChanged.emit()
            return
        if ev.mimeData().hasFormat(MIME_TYPE):
            step_type = bytes(ev.mimeData().data(MIME_TYPE)).decode()
            self.stepDropped.emit(step_type, self._drop_row(ev))
            ev.accept()
            return
        ev.ignore()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        row = self._drop_row_hint
        if row is None:
            return
        # 绘制插入辅助线：蓝色横线 + 两端箭头
        from PySide6.QtGui import QColor, QPainter, QPen
        p = QPainter(self.viewport())
        w = self.viewport().width()
        if self.count() == 0 or row >= self.count():
            y = (self.visualItemRect(self.item(self.count() - 1)).bottom() + 1
                 if self.count() else 6)
        else:
            y = self.visualItemRect(self.item(row)).top()
        p.setPen(QPen(QColor(0, 145, 255), 3))
        p.drawLine(6, y, w - 6, y)
        p.setBrush(QColor(0, 145, 255))
        p.setPen(Qt.NoPen)
        p.drawPolygon([QPoint(2, y - 6), QPoint(2, y + 6), QPoint(12, y)])
        p.drawPolygon([QPoint(w - 2, y - 6), QPoint(w - 2, y + 6), QPoint(w - 12, y)])


class FlowMetaDialog(QDialog):
    """新建/编辑流程：流程名、所属分组、运行轮数、启停热键。

    groups 为 None 时不显示分组行（外部未提供分组列表的场景）。
    """

    def __init__(self, flow: Flow, create: bool, parent=None, groups: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("新建流程" if create else "编辑流程")
        self.setMinimumWidth(380)
        self._flow = flow
        self._groups = list(groups or [])
        self._build()
        self._fill(flow)

    def _build(self):
        form = QFormLayout(self)
        self.name_edit = QLineEdit()
        form.addRow("流程名称", self.name_edit)
        if self._groups:
            self.group_combo = QComboBox()
            self.group_combo.addItem("未分组", "")
            for g in self._groups:
                self.group_combo.addItem(g, g)
            form.addRow("所属分组", self.group_combo)
        self.loops_spin = QSpinBox()
        self.loops_spin.setRange(0, 9999)
        self.loops_spin.setSpecialValueText("0 = 无限循环")
        form.addRow("运行轮数", self.loops_spin)
        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setMaximumWidth(220)
        form.addRow("启停热键（可选）", self.hotkey_edit)
        tip = QLabel("创建后在右侧把模块拖入步骤列表；参数双击步骤即可修改。")
        tip.setStyleSheet("color: #8a939c;")
        form.addRow("", tip)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _fill(self, flow: Flow):
        self.name_edit.setText(flow.name)
        if self._groups:
            idx = self.group_combo.findData(flow.group)
            self.group_combo.setCurrentIndex(max(0, idx))
        self.loops_spin.setValue(int(flow.loops))
        self.hotkey_edit.set_hotkey(flow.hotkey)

    def apply_to(self, flow: Flow) -> None:
        flow.name = self.name_edit.text().strip() or flow.name
        if self._groups:
            flow.group = self.group_combo.currentData() or ""
        flow.loops = self.loops_spin.value()
        flow.hotkey = self.hotkey_edit.hotkey()


class ProcessPickerDialog(QDialog):
    """选择正在运行的进程（供「关闭应用」用）。

    每条显示「应用名 — 进程名 — 窗口标题」，窗口标题用于区分多开实例；
    支持关键字过滤，双击或确定返回选中的进程名。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要关闭的进程")
        self.setMinimumSize(460, 500)

        layout = QVBoxLayout(self)
        top_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入关键字过滤（进程名 / 应用名 / 窗口标题）…")
        self.search.textChanged.connect(self._reload)
        top_row.addWidget(self.search, 1)
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.setToolTip("重新读取当前正在运行的进程列表")
        self.refresh_btn.clicked.connect(self._refresh)
        top_row.addWidget(self.refresh_btn)
        layout.addLayout(top_row)

        self.list = QListWidget()
        self._processes = self._load_processes()
        self._reload()
        layout.addWidget(self.list, 1)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #8a939c; font-size: 9pt;")
        layout.addWidget(self.hint)
        self._update_hint()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("关闭所选进程")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        self.search.setFocus()

    def _update_hint(self):
        self.hint.setText(
            f"共 {len(self._processes)} 个正在运行的应用"
            "（仅列有窗口的程序，后台子进程已过滤）。")

    def _refresh(self):
        """重新读取进程列表并重建列表（点了刷新按钮）。"""
        self._processes = self._load_processes()
        self._reload()
        self._update_hint()

    def _load_processes(self) -> list[dict]:
        from .. import win_actors
        return win_actors.list_processes()

    def _display(self, item: dict) -> str:
        app = item.get("app_name") or item.get("name", "")
        name = item.get("name", "")
        title = item.get("title", "")
        if title:
            return f"{app} — {name}「{title}」"
        return f"{app} — {name}"

    def _reload(self, *_):
        self.list.clear()
        kw = self.search.text().strip().lower()
        for item in self._processes:
            hay = " ".join(str(item.get(k, "")) for k in ("name", "app_name", "title")).lower()
            if kw and kw not in hay:
                continue
            row = QListWidgetItem(self._display(item))
            row.setData(Qt.UserRole, item)
            self.list.addItem(row)

    def selected_item(self) -> dict | None:
        """返回当前选中进程的完整信息 dict（None = 未选中）。"""
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def selected_process(self) -> str:
        data = self.selected_item()
        return data.get("name", "") if data else ""


class StepParamsDialog(QDialog):
    """按步骤类型编辑参数。"""

    regionCaptureRequested = Signal()    # find 步骤点"框选区域"
    templateCaptureRequested = Signal()  # find 步骤点"屏幕截图选区"
    pointCaptureRequested = Signal()     # click 步骤点"屏幕点选坐标"
    windowCaptureRequested = Signal()    # app/click/press 步骤点"拖动识别窗口"

    def __init__(self, step: FlowStep, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑步骤：{step.name}")
        self.setMinimumWidth(440)
        self._step = step
        self._build(step)
        self._fill(step)

    def accept(self) -> None:
        """确定前校验：选中「变量坐标」但没选变量时拦截并提示。"""
        if self._step.type == "click" and getattr(self, "var_radio", None) is not None \
                and self.var_radio.isChecked() and not self._combo_value(self.pos_var):
            QMessageBox.warning(self, "请设置变量坐标",
                                "已选择「变量坐标」，请选择流程中声明的坐标变量。")
            return
        # 截图步骤：必须框选区域；「默认保存」必须选择结果变量（自选保存可选）
        if self._step.type == "screenshot" and getattr(self, "save_var_radio", None) is not None:
            if not getattr(self, "_region", ""):
                QMessageBox.warning(self, "请设置截图区域",
                                    "请先点击「框选区域…」选择截图区域。")
                return
            if self.save_var_radio.isChecked() and not self._combo_value(self.shot_variable):
                QMessageBox.warning(self, "请设置结果变量",
                                    "已选择「默认保存」，请选择接收截图路径的结果变量。")
                return
        # 找图步骤：必须设置模板图与结果变量
        if self._step.type == "find_image" and getattr(self, "find_var", None) is not None:
            if not getattr(self, "_image", ""):
                QMessageBox.warning(self, "请设置模板图",
                                    "请先「屏幕截图选区」或「上传图片」设置找图模板。")
                return
            if not self._combo_value(self.find_var):
                QMessageBox.warning(self, "请设置结果变量",
                                    "请选择接收找图坐标的结果变量。")
                return
        # 目标检测步骤：模型路径必填且文件必须存在；必须选择结果变量
        if self._step.type == "yolo_detect" and getattr(self, "model_path_edit", None) is not None:
            path = self.model_path_edit.text().strip()
            if not path:
                QMessageBox.warning(self, "请设置模型路径",
                                    "请输入 YOLOv5 模型文件路径，或点击「浏览…」选择模型文件。")
                return
            if not os.path.isfile(path):
                QMessageBox.warning(self, "模型路径无效",
                                    f"模型文件不存在：\n{path}\n\n请检查路径是否正确。")
                return
            if not self._combo_value(self.yolo_var):
                QMessageBox.warning(self, "请设置结果变量",
                                    "请选择接收检测结果列表的结果变量。")
                return
        # python函数：函数名必填；运行结果必须保存到指定变量
        if self._step.type == "py_func" and getattr(self, "py_result_var", None) is not None:
            if not self.func_edit.text().strip():
                QMessageBox.warning(self, "请填写调用函数名",
                                    "python函数步骤固定调用一个函数并取返回值，"
                                    "请填写代码中定义的函数名。")
                return
            if not self._combo_value(self.py_result_var):
                QMessageBox.warning(self, "请设置结果变量",
                                    "请选择接收函数运行结果的流程变量。")
                return
        # if / elseif / while：条件必填，且引用的变量必须已在此分支之前定义
        if self._step.type in ("if", "elseif", "while") and getattr(self, "cond_edit", None) is not None:
            cond = self.cond_edit.text().strip()
            if not cond:
                QMessageBox.warning(self, "请填写条件表达式",
                                    "请输入条件表达式，例如：x >= 1 && y <= 10")
                return
            flow, idx = self._flow_context()
            if flow is not None and idx >= 0:
                ok, missing = check_condition_variables(
                    flow.steps, flow.variables, idx, cond)
                if not ok:
                    names = "、".join(missing)
                    QMessageBox.warning(
                        self, "变量未定义",
                        f"第 {idx + 1} 步条件引用了尚未定义的变量：{names}\n\n"
                        "请确保这些变量已在此分支之前通过「变量」步骤或其它输出步骤定义，"
                        "或修正条件表达式。")
                    return
        # foreach：必须选择要遍历的数据源变量
        if self._step.type == "foreach" and getattr(self, "foreach_items", None) is not None:
            if not self._combo_value(self.foreach_items):
                QMessageBox.warning(self, "请选择数据源",
                                    "请选择要遍历的数据源变量（其值须为列表/字典/字符串）。")
                return
        super().accept()

    def _flow_context(self):
        """返回 (当前流程, 本步骤在 flow.steps 中的索引)；获取不到返回 (None, -1)。

        供条件分支变量校验使用：需要知道本步骤位于流程中的位置，才能判断
        「此分支之前」已定义了哪些变量。
        """
        try:
            tab = self.parent()
            flow = tab._selected_flow() if tab is not None else None
            if flow is None:
                return None, -1
            for i, s in enumerate(flow.steps):
                if s is self._step:
                    return flow, i
            return flow, -1
        except Exception:
            return None, -1

    def _flow_var_names(self) -> list[str]:
        """当前流程中已声明的变量名（按 var 步骤出现顺序去重）。

        供打印输出/文字识别的变量下拉、变量步骤的重名校验使用。
        获取不到流程（无 parent 或非 FlowTab）时返回空列表。
        """
        try:
            tab = self.parent()
            if tab is None:
                return []
            flow = tab._selected_flow()
            if flow is None:
                return []
            names: list[str] = []
            for s in flow.steps:
                if s.type == "var":
                    name = (s.params.get("name") or "").strip()
                    if name and name not in names:
                        names.append(name)
            return names
        except Exception:
            return []

    def _var_combo(self, placeholder: str = "") -> QComboBox:
        """构建可编辑的变量下拉/输入框：既列流程中已声明的变量（「变量」步骤声明），
        也支持手动输入。

        首项为空占位；旧配置里保存的变量名/表达式不在列表时，由 _set_combo_value
        动态补项保留，避免回填丢失。可编辑：既能下拉选已声明变量，也能直接输入
        任意变量名或 Python 下标表达式（如 aaa['a']、arr[0]）。
        """
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItem(placeholder, "")
        for name in self._flow_var_names():
            combo.addItem(name, name)
        combo.lineEdit().setPlaceholderText(placeholder or "变量名或下标表达式")
        return combo

    def _var_combo_hint(self, form: QFormLayout) -> None:
        """流程中还没有任何变量时，在表单里补一行引导提示。"""
        if self._flow_var_names():
            return
        hint = QLabel("流程中暂无变量：可直接输入变量名（读取需变量已由前面的步骤产生，"
                      "或先添加「变量」步骤声明）。")
        hint.setStyleSheet("color: #8a939c; font-size: 9pt;")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _sync_pos_rows(self) -> None:
        """点击位置三选一：跟随/固定坐标/变量坐标，切换时联动控件行显隐。"""
        self._fixed_pos_widget.setVisible(self.fixed_radio.isChecked())
        self._var_pos_widget.setVisible(self.var_radio.isChecked())
        self.adjustSize()

    def _sync_shot_save_rows(self) -> None:
        """保存位置二选一：自选保存时显示说明行（结果变量行两种方式都常显）。"""
        var_mode = self.save_var_radio.isChecked()
        self._shot_choose_hint_widget.setVisible(not var_mode)
        self.adjustSize()

    # ---------- UI ----------
    def _build(self, step: FlowStep):
        root = QVBoxLayout(self)
        form = QFormLayout()
        p = step.params
        t = step.type

        if t == "var":
            self.var_name = QLineEdit()
            self.var_name.setPlaceholderText("例如：name、count、total（必填，流程内唯一）")
            form.addRow("变量名", self.var_name)
            self.var_type = QComboBox()
            for vt, label in VARIABLE_TYPES.items():
                self.var_type.addItem(label, vt)
            form.addRow("变量类型", self.var_type)
            self.var_default = QLineEdit()
            self.var_default.setPlaceholderText("值或表达式：$a + 1、$a + \"!\"、len($s)、[1, 2]")
            self.var_default.setToolTip(
                "默认值支持：\n"
                "· 普通字面量：字符串 / 数字 / true·false / [..] / {..} / 任意文本\n"
                "· 表达式：$变量名 引用、数学运算（+ - * / %）、字符串拼接（+）、\n"
                "  下标（arr[0]）、比较（$n > 2）、len()/int()/str()/abs() 等白名单函数\n"
                "示例：$count + 1、$name + \"先生\"、len($text)")
            form.addRow("默认值", self.var_default)
            hint = QLabel("变量步骤会声明/覆盖一个运行时变量；默认值可用 $变量名 引用前面的变量，\n"
                          "也支持数学运算、字符串拼接与 len() 等基本函数。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            # 变量名重复实时提示：失焦或输入时检查流程里是否已有同名变量
            self.var_name.editingFinished.connect(self._check_var_name)

        elif t in ("if", "elseif"):
            self.cond_edit = QLineEdit()
            self.cond_edit.setPlaceholderText("例如：x >= 1 && y <= 10（支持 && || ! 与比较运算）")
            self.cond_edit.setToolTip("支持：&& 与、|| 或、! 非、==/!=/</<=/>/>= 比较、"
                                      "数字/字符串字面量、len() 等少量内置函数。"
                                      "引用变量需已在此分支之前定义。")
            form.addRow("条件表达式", self.cond_edit)
            if t == "if":
                hint = QLabel("「if」是条件块的起点，成对生成「endif 条件结束」。\n"
                              "条件成立则执行 if 与 endif 之间的步骤；不成立则跳到"
                              "下一个「否则如果 / 否则」，都没有则跳过整块。")
            else:
                hint = QLabel("「否则如果」在上一条件不成立时继续判断；可多次添加，"
                              "须位于「否则」之前。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "else":
            hint = QLabel("「否则」表示上方所有条件都不成立时执行的分支，无需配置。\n"
                          "同一 if 块只能有一个「否则」，且必须位于 endif 之前。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "endif":
            hint = QLabel("「条件结束」是随 if 自动生成的结构标记，与 if 成对出现，"
                          "无需配置；删除 if 时会同步删除。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "foreach":
            self.foreach_items = self._var_combo("变量或表达式")
            self.foreach_items.setToolTip(
                "数据源支持表达式：\n"
                "· 变量/下标：arr、data['nums']、arr[0]\n"
                "· $变量引用：$arr、$i + 1\n"
                "· 函数与字面量：range(0, 3)、sorted($arr)、slice(0, $k)、\n"
                "  $arr[slice(0, $k)]、len($s)、[1, 2, 3]\n"
                "结果须可遍历（列表/字典/字符串/range 等）")
            form.addRow("数据源", self.foreach_items)
            self._var_combo_hint(form)
            self.foreach_item_var = QLineEdit("item")
            self.foreach_item_var.setToolTip("每轮把当前元素写入该变量（字典=值，列表/字符串=元素；"
                                             "循环结束保留最后值）")
            form.addRow("元素变量名", self.foreach_item_var)
            self.foreach_index_var = QLineEdit("index")
            self.foreach_index_var.setToolTip("每轮把下标写入该变量（字典=键，列表/字符串=数字下标）")
            form.addRow("下标变量名", self.foreach_index_var)
            hint = QLabel("「Foreach 循环」遍历数据源的每个元素，每轮把元素与下标写入上面两个变量，\n"
                          "再执行 foreach 与「Foreach 循环结束」之间的步骤。数据源为列表/字符串按元素\n"
                          "遍历、字典按「值→元素、键→下标」遍历；空数据直接跳过整块。\n"
                          "数据源也支持直接填表达式：range(0, 3)、sorted($arr)、$arr[slice(0, $k)] 等。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "while":
            self.cond_edit = QLineEdit()
            self.cond_edit.setPlaceholderText("例如：i < 3；直接填 true 可无限循环")
            self.cond_edit.setToolTip("支持：&& 与、|| 或、! 非、==/!=/</<=/>/>= 比较、"
                                      "数字/字符串字面量、len() 等白名单函数；"
                                      "小写 true / false 表示恒真/恒假。\n"
                                      "直接填 true 构成无限循环，需在循环体内用 "
                                      "break 中断或手动停止（无 break 时达上限自动终止）。\n"
                                      "引用变量需已在此循环之前定义。")
            form.addRow("循环条件", self.cond_edit)
            hint = QLabel("「while 循环」在条件成立时反复执行 while 与「while 循环结束」之间的步骤，\n"
                          "直到条件不成立。循环体内可用 break 中断；请在体内修改条件引用的变量，\n"
                          "否则会一直循环（达到上限会自动终止）。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "endForeach":
            hint = QLabel("「Foreach 循环结束」是随 foreach 自动生成的结构标记，与 foreach 成对出现，"
                          "无需配置；删除 foreach 时会同步删除。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "endWhile":
            hint = QLabel("「while 循环结束」是随 while 自动生成的结构标记，与 while 成对出现，"
                          "无需配置；删除 while 时会同步删除。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "break":
            hint = QLabel("「break 中断循环」立即跳出最内层的 Foreach/while 循环，继续执行该循环"
                          "结束标记之后的步骤。无需配置，且只能放在循环体内部。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "continue":
            hint = QLabel("「continue 继续循环」跳过本次循环体的剩余步骤，直接进入下一次迭代。"
                          "无需配置，且只能放在循环体内部。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "exit":
            self.exit_var = self._var_combo("（不打印变量）")
            self.exit_var.setToolTip("可选：退出流程前在日志中打印该变量的值，便于定位退出时状态")
            form.addRow("退出前打印变量（可选）", self.exit_var)
            self._var_combo_hint(form)
            hint = QLabel("「退出流程」执行到这一步时立即终止整个流程，后续步骤不再执行。\n"
                          "可选在上方选择一个变量：退出前会先把它的值打印到日志。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "log":
            self.log_vars = QComboBox()
            self.log_vars.setEditable(True)   # 可编辑：既能下拉选已声明变量，也能手动输入（多个用逗号分隔）
            self.log_vars.addItem("无", "")
            for name in self._flow_var_names():
                self.log_vars.addItem(name, name)
            self.log_vars.setToolTip("下拉选择或直接输入要打印的变量名；多个用逗号分隔；"
                                     "选「无」（留空）则不打印任何变量。\n"
                                     "原始输出模式下，变量后加 \\n 换行、加 \\b 空格，"
                                     "否则多个变量直接拼接（不换行）")
            self.log_vars.lineEdit().setPlaceholderText("变量名，多个用逗号分隔（空=不打印；原始输出可加 \\n/\\b 控制分隔）")
            form.addRow("打印变量", self.log_vars)
            self.log_text = QLineEdit()
            self.log_text.setPlaceholderText("附加文本，可用 $变量名 引用；\\n 换行、\\b 空格（可选）")
            self.log_text.setToolTip("附加文本支持 $变量名 引用；字面量 \\n 会转成换行、\\b 转成空格")
            form.addRow("附加文本", self.log_text)
            self.raw_check = QCheckBox("原始输出")
            self.raw_check.setToolTip("勾选后，输出内容原样写入日志：不加时间戳、不自动换行（适合拼接连续内容）")
            form.addRow("原始输出", self.raw_check)
            hint = QLabel("运行时会打印到底部日志（蓝色字体显示），便于调试流程。\n"
                          "勾选「原始输出」后不加时间戳、不自动换行，内容原样显示。")
            hint.setStyleSheet("color: #8a939c;")
            form.addRow("", hint)

        elif t == "clip_set":
            self.clip_name = self._var_combo("（不使用变量）")
            self.clip_name.setToolTip("选择变量：把该变量的值（转为文本）写入剪贴板（优先）")
            form.addRow("变量", self.clip_name)
            self.clip_text = QLineEdit()
            self.clip_text.setPlaceholderText("或直接输入要写入剪贴板的文本，可用 $变量名 引用")
            self.clip_text.setToolTip("选变量时此项忽略；留空且未选变量则步骤失败")
            form.addRow("自定义文本", self.clip_text)
            self._var_combo_hint(form)
            hint = QLabel("把变量值或自定义文本写入剪贴板，供其他程序粘贴使用。\n"
                          "两种来源二选一：选了变量用变量值；否则用下面的文本。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "clip_get":
            self.clip_variable = self._var_combo("（选择变量）")
            self.clip_variable.setToolTip("读取剪贴板文本并赋值给该变量（字符串类型）")
            form.addRow("变量名", self.clip_variable)
            self._var_combo_hint(form)
            hint = QLabel("读取系统剪贴板的文本内容，赋值给指定变量。\n"
                          "若剪贴板里没有文本，变量会被设为空字符串。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "ocr":
            self.ocr_lang = QComboBox()
            self.ocr_lang.addItem("中文（默认）", "ch")
            self.ocr_lang.addItem("英文", "en")
            form.addRow("识别语言", self.ocr_lang)
            self.ocr_multi = QCheckBox("多行结果保存为列表；不勾选则拼接为一个字符串")
            form.addRow("", self.ocr_multi)
            region_row = QHBoxLayout()
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            pick_region.setToolTip("隐藏本窗口后框选识别区域（与找图区域一致）")
            clear_region = QPushButton("恢复全屏")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            region_row.addWidget(self.region_edit, 1)
            region_row.addWidget(pick_region)
            region_row.addWidget(clear_region)
            form.addRow("识别区域", region_row)
            hint = QLabel("需要 RapidOCR 才能运行：pip install rapidocr_onnxruntime\n"
                          "（模型随包内置，打包版 exe 同样支持文字识别）")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            # 结果变量放最下面：下拉选择已有变量（由「变量」步骤声明）
            self.ocr_variable = self._var_combo("（结果变量）")
            self.ocr_variable.setToolTip("识别结果保存到该变量（多行=列表，单行=字符串）")
            form.addRow("结果变量", self.ocr_variable)
            self._var_combo_hint(form)

        elif t == "text_find":
            self.tf_text = QLineEdit()
            self.tf_text.setPlaceholderText("要查找的文字，可用 $变量名 引用（必填）")
            form.addRow("查找文字", self.tf_text)
            region_row = QHBoxLayout()
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            pick_region.setToolTip("隐藏本窗口后框选查找区域（与找图区域一致）")
            clear_region = QPushButton("恢复全屏")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            region_row.addWidget(self.region_edit, 1)
            region_row.addWidget(pick_region)
            region_row.addWidget(clear_region)
            form.addRow("查找区域", region_row)
            self.tf_click = QCheckBox("找到后点击该文字")
            self.tf_click.setToolTip("勾选后找到文字直接用鼠标点击；不勾选则把坐标写入结果变量")
            form.addRow("", self.tf_click)
            self.tf_button = QComboBox()
            self.tf_button.addItem("鼠标左键", "left")
            self.tf_button.addItem("鼠标右键", "right")
            self.tf_button.setEnabled(False)
            form.addRow("点击按键", self.tf_button)
            self.tf_click.toggled.connect(self.tf_button.setEnabled)
            self.tf_variable = self._var_combo("（可不设置）")
            self.tf_variable.setToolTip("可选。未勾选点击时写入坐标 \"x,y\"；未找到文字写入 false。\n"
                                        "仅查找/点击时不设置也可以")
            form.addRow("结果变量（可选）", self.tf_variable)
            hint = QLabel("在屏幕/指定区域查找文字：勾选点击则找到即点击；不勾选则把坐标\n"
                          "写入结果变量，未找到时写入 false（步骤不会因未找到而失败）。\n"
                          "结果变量可留空不设置，仅执行查找/点击即可。\n"
                          "需要 RapidOCR 才能运行：pip install rapidocr_onnxruntime")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "screenshot":
            # 截图区域：固定「指定区域截图」（无方式选择），必须框选
            self._shot_region_widget = QWidget()
            region_row = QHBoxLayout(self._shot_region_widget)
            region_row.setContentsMargins(0, 0, 0, 0)
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            pick_region.setToolTip("隐藏本窗口后框选截图区域（与找图/文字识别区域一致）")
            clear_region = QPushButton("清除")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            region_row.addWidget(self.region_edit, 1)
            region_row.addWidget(pick_region)
            region_row.addWidget(clear_region)
            form.addRow("截图区域", self._shot_region_widget)

            # 保存位置：默认保存 / 自选保存（二选一）
            save_row = QHBoxLayout()
            self.save_var_radio = QRadioButton("默认保存")
            self.save_choose_radio = QRadioButton("自选保存")
            self.save_var_radio.setToolTip(
                "保存到程序目录 templates/jietu/，并把图片绝对路径写入结果变量")
            self.save_choose_radio.setToolTip(
                "运行时弹出「另存为」对话框，由你指定保存位置（结果变量可选）")
            save_row.addWidget(self.save_var_radio)
            save_row.addWidget(self.save_choose_radio)
            save_row.addStretch(1)
            form.addRow("保存位置", save_row)

            # 结果变量下拉行（默认保存必填；自选保存可选，选了也会写入路径）
            self._shot_var_widget = QWidget()
            var_row = QHBoxLayout(self._shot_var_widget)
            var_row.setContentsMargins(0, 0, 0, 0)
            self.shot_variable = self._var_combo("（可选，自选保存可不选）")
            self.shot_variable.setToolTip(
                "截图保存后，把图片的绝对路径写入该变量（后续日志/剪贴板/点击等步骤可用）")
            var_row.addWidget(self.shot_variable, 1)
            form.addRow("结果变量", self._shot_var_widget)
            self._var_combo_hint(form)

            # 自选保存：说明行（运行时弹窗）
            self._shot_choose_hint = QLabel(
                "运行时弹出「另存为」对话框，选择保存位置；\n取消对话框则本步骤失败。")
            self._shot_choose_hint.setStyleSheet("color: #8a939c;")
            self._shot_choose_hint.setWordWrap(True)
            self._shot_choose_hint_widget = QWidget()
            hint_lay = QHBoxLayout(self._shot_choose_hint_widget)
            hint_lay.setContentsMargins(0, 0, 0, 0)
            hint_lay.addWidget(self._shot_choose_hint)
            form.addRow("", self._shot_choose_hint_widget)

            self.save_var_radio.toggled.connect(self._sync_shot_save_rows)
            self._sync_shot_save_rows()

        elif t == "find_image":
            # 模板图：预览 + 屏幕截图选区 + 上传本地图片
            img_row = QHBoxLayout()
            self.preview = QLabel()
            self.preview.setFixedSize(230, 150)
            self.preview.setAlignment(Qt.AlignCenter)
            self.preview.setStyleSheet(
                "border: 1px solid #c9d1d9; border-radius: 6px; background: #f7f9fb;")
            img_row.addWidget(self.preview)

            side = QVBoxLayout()
            side.setSpacing(6)
            self.capture_btn = QPushButton("📷 屏幕截图选区")
            self.capture_btn.setToolTip("冻结屏幕 -> 框选 -> 双击确认，生成找图模板")
            self.capture_btn.clicked.connect(self._request_capture)
            side.addWidget(self.capture_btn)
            self.upload_btn = QPushButton("📁 上传图片")
            self.upload_btn.setToolTip("从本地选择一张图片作为找图模板（自动复制到程序模板目录）")
            self.upload_btn.clicked.connect(self._pick_image)
            side.addWidget(self.upload_btn)
            self.image_edit = QLineEdit()
            self.image_edit.setReadOnly(True)
            self.image_edit.setStyleSheet("color: #8a939c; border: none; background: transparent;")
            side.addWidget(self.image_edit)
            side.addStretch(1)
            img_row.addLayout(side, 1)
            form.addRow("模板图", img_row)

            self.confidence = QDoubleSpinBox()
            self.confidence.setRange(0.5, 0.99)
            self.confidence.setDecimals(2)
            self.confidence.setSingleStep(0.01)
            form.addRow("匹配置信度", self.confidence)

            # 查找区域：空=全屏；可框选或手动输入左上/右下角坐标
            region_row = QHBoxLayout()
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            self.region_edit.setToolTip("左上角 x,y 与宽高；空=全屏（整个虚拟桌面）")
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            pick_region.setToolTip("隐藏本窗口后框选查找区域（与找图/文字识别区域一致）")
            clear_region = QPushButton("恢复全屏")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            region_row.addWidget(self.region_edit, 1)
            region_row.addWidget(pick_region)
            region_row.addWidget(clear_region)
            form.addRow("查找区域", region_row)

            manual_row = QHBoxLayout()
            self.manual_edit = QLineEdit()
            self.manual_edit.setPlaceholderText("左上x,左上y,右下x,右下y（如 100,200,400,500）")
            self.manual_edit.setToolTip("直接输入矩形区域左上角与右下角坐标，4 个数字逗号分隔")
            apply_btn = QPushButton("应用")
            apply_btn.setToolTip("解析输入坐标并设为查找区域")
            apply_btn.clicked.connect(self._apply_manual_region)
            manual_row.addWidget(self.manual_edit, 1)
            manual_row.addWidget(apply_btn)
            form.addRow("坐标输入", manual_row)

            self.find_var = self._var_combo("（选择变量）")
            self.find_var.setToolTip("找到后把目标矩形区域 \"左上x,左上y,右下x,右下y\" 写入该变量；未找到写入 false")
            form.addRow("结果变量", self.find_var)
            self._var_combo_hint(form)

            # 效果预览：找到后在目标区域画红框，可设持续时间
            preview_row = QHBoxLayout()
            self.preview_check = QCheckBox("找到后红框高亮")
            self.preview_check.setToolTip("勾选后，找到目标时在被找到的图片区域画一个红框")
            self.preview_spin = QDoubleSpinBox()
            self.preview_spin.setRange(0.5, 10.0)
            self.preview_spin.setDecimals(1)
            self.preview_spin.setSingleStep(0.5)
            self.preview_spin.setValue(1.0)
            self.preview_spin.setSuffix(" 秒")
            self.preview_spin.setEnabled(False)
            self.preview_check.toggled.connect(self.preview_spin.setEnabled)
            preview_row.addWidget(self.preview_check)
            preview_row.addWidget(self.preview_spin)
            preview_row.addStretch(1)
            form.addRow("效果预览", preview_row)

            hint = QLabel("在屏幕 / 指定区域用模板匹配找图：找到则把矩形区域坐标 \"左上x,左上y,右下x,右下y\" 写入结果变量，\n"
                          "未找到写入 false（步骤不会因未找到而失败，可据此分支）。\n"
                          "区域为空=全屏；也可点击「框选区域…」或输入左上/右下角坐标。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "yolo_detect":
            # 模型路径：手动输入或文件浏览（浏览选取后也可再改）
            model_row = QHBoxLayout()
            self.model_path_edit = QLineEdit()
            self.model_path_edit.setPlaceholderText("YOLOv5 模型文件路径（.pt），可手动输入或点击「浏览…」")
            self.model_path_edit.setToolTip("YOLOv5 训练得到的 .pt 模型文件；路径不存在时保存会被拦截提示")
            self.model_path_edit.editingFinished.connect(self._check_model_path)
            browse_btn = QPushButton("📁 浏览…")
            browse_btn.setToolTip("从本地选择 YOLOv5 模型文件")
            browse_btn.clicked.connect(self._pick_model_file)
            model_row.addWidget(self.model_path_edit, 1)
            model_row.addWidget(browse_btn)
            form.addRow("模型路径", model_row)

            # 检测范围：空=全屏；可框选或手动输入左上/右下角坐标（与找图一致）
            region_row = QHBoxLayout()
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            self.region_edit.setToolTip("左上角 x,y 与宽高；空=全屏（整个虚拟桌面）")
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            pick_region.setToolTip("隐藏本窗口后框选检测区域（与找图/文字识别区域一致）")
            clear_region = QPushButton("恢复全屏")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            region_row.addWidget(self.region_edit, 1)
            region_row.addWidget(pick_region)
            region_row.addWidget(clear_region)
            form.addRow("检测范围", region_row)

            manual_row = QHBoxLayout()
            self.manual_edit = QLineEdit()
            self.manual_edit.setPlaceholderText("左上x,左上y,右下x,右下y（如 100,200,400,500）")
            self.manual_edit.setToolTip("直接输入矩形区域左上角与右下角坐标，4 个数字逗号分隔")
            apply_btn = QPushButton("应用")
            apply_btn.setToolTip("解析输入坐标并设为检测范围")
            apply_btn.clicked.connect(self._apply_manual_region)
            manual_row.addWidget(self.manual_edit, 1)
            manual_row.addWidget(apply_btn)
            form.addRow("坐标输入", manual_row)

            # 检测类别：留空=全部；逗号分隔多个类别名；支持 $变量名 引用
            self.classes_edit = QLineEdit()
            self.classes_edit.setPlaceholderText("留空=检测全部类别；多个用逗号分隔，如 person,car；支持 $变量名")
            self.classes_edit.setToolTip("只返回此处列出的类别（名称需与模型类别名一致）；留空则返回全部检测到的类别")
            form.addRow("检测类别", self.classes_edit)

            # 置信度阈值：默认 0.5（自训练模型建议 0.2~0.5，设太高会什么都检不出）
            self.confidence = QDoubleSpinBox()
            self.confidence.setRange(0.05, 0.99)
            self.confidence.setDecimals(2)
            self.confidence.setSingleStep(0.01)
            self.confidence.setValue(0.5)
            self.confidence.setToolTip("只保留置信度不低于该值的检测结果；自训练模型建议 0.2~0.5，过高会检不出")
            form.addRow("置信度阈值", self.confidence)

            # 推理设备：默认 cuda
            self.device_combo = QComboBox()
            self.device_combo.addItem("cuda（显卡加速）", "cuda")
            self.device_combo.addItem("cpu", "cpu")
            self.device_combo.setToolTip("默认 cuda；无 NVIDIA 显卡 / CUDA 不可用时请选 cpu")
            form.addRow("推理设备", self.device_combo)

            # 附加动作：检测成功（有目标）后对最高置信度目标中心执行鼠标操作
            self.action_combo = QComboBox()
            for k, v in (("无操作", "none"), ("左键单击", "left"),
                         ("右键单击", "right"), ("左键双击", "double")):
                self.action_combo.addItem(k, v)
            self.action_combo.setToolTip("检测到目标后，对置信度最高的目标中心执行所选鼠标操作")
            form.addRow("附加动作", self.action_combo)

            # 结果变量：list[dict]，每项含 class / confidence / region
            self.yolo_var = self._var_combo("（选择变量）")
            self.yolo_var.setToolTip(
                "检测结果列表写入该变量：每项为字典，含 class（类别）、confidence（置信度）、\n"
                "region（\"左上x,左上y,右下x,右下y\"）；未检测到目标时写入空列表 []")
            form.addRow("结果变量", self.yolo_var)
            self._var_combo_hint(form)

            # 效果预览：红框标注检测目标（左上类别、右上置信度），可设持续时间
            preview_row = QHBoxLayout()
            self.preview_check = QCheckBox("检测到目标后红框高亮")
            self.preview_check.setToolTip("勾选后，检测到的目标用红框标出：左上角显示类别，右上角显示置信度")
            self.preview_spin = QDoubleSpinBox()
            self.preview_spin.setRange(0.1, 10.0)
            self.preview_spin.setDecimals(1)
            self.preview_spin.setSingleStep(0.1)
            self.preview_spin.setValue(1.0)
            self.preview_spin.setSuffix(" 秒")
            self.preview_spin.setEnabled(False)
            self.preview_check.toggled.connect(self.preview_spin.setEnabled)
            preview_row.addWidget(self.preview_check)
            preview_row.addWidget(self.preview_spin)
            preview_row.addStretch(1)
            form.addRow("效果预览", preview_row)

            hint = QLabel("用 YOLOv5 模型在屏幕 / 指定区域做目标检测：结果以列表写入结果变量（按置信度从高到低），\n"
                          "未检测到写入空列表 []（步骤不会因未检测到而失败，可据此分支）。\n"
                          "置信度阈值建议 0.2~0.5：自训练模型置信度普遍偏低，阈值设太高会什么都检不出。\n"
                          "模型支持 .pt 与 .onnx：.pt 需 pip install torch ultralytics（旧版 v5~v7 模型会自动改用\n"
                          "yolov5 仓库加载，本地仓库免联网）；.onnx 仅需 pip install onnxruntime，最轻量且完全离线。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "click":
            self.btn_combo = QComboBox()
            for k, v in (("鼠标左键", "left"), ("鼠标右键", "right"), ("鼠标中键", "middle")):
                self.btn_combo.addItem(k, v)
            form.addRow("鼠标按键", self.btn_combo)
            self.type_combo = QComboBox()
            self.type_combo.addItem("单击", "single")
            self.type_combo.addItem("双击", "double")
            form.addRow("点击方式", self.type_combo)
            self.interval = self._spin(20, 3_600_000, " 毫秒")
            form.addRow("点击间隔", self.interval)
            pos_row = QHBoxLayout()
            self.follow_radio = QRadioButton("跟随当前鼠标")
            self.fixed_radio = QRadioButton("固定坐标")
            self.var_radio = QRadioButton("变量坐标")
            self.var_radio.setToolTip("选变量后以变量值作为坐标：\"64,63\"（x,y），"
                                      "或 \"100,200,400,500\" 区域（自动取中心）")
            pos_row.addWidget(self.follow_radio)
            pos_row.addWidget(self.fixed_radio)
            pos_row.addWidget(self.var_radio)
            pos_row.addStretch(1)
            form.addRow("点击位置", pos_row)

            # 固定坐标控件行：X Y + 屏幕点选（仅固定坐标模式显示）
            self.pos_x = self._spin(-99999, 99999)
            self.pos_y = self._spin(-99999, 99999)
            pick = QPushButton("📍 屏幕点选")
            pick.setToolTip("隐藏本程序后，点击屏幕任意位置取坐标（Esc 取消）")
            pick.clicked.connect(self._request_point)
            self.pick_btn = pick
            self._fixed_pos_widget = QWidget()
            fixed_row = QHBoxLayout(self._fixed_pos_widget)
            fixed_row.setContentsMargins(0, 0, 0, 0)
            fixed_row.addWidget(self.pos_x)
            fixed_row.addWidget(QLabel("Y"))
            fixed_row.addWidget(self.pos_y)
            fixed_row.addWidget(pick)
            fixed_row.addStretch(1)
            form.addRow("", self._fixed_pos_widget)

            # 变量坐标控件行：变量下拉（仅变量坐标模式显示）
            self.pos_var = self._var_combo("（请选择变量）")
            self.pos_var.setToolTip("必须选择一个流程变量，其值为坐标字符串 \"64,63\"（x,y）"
                                    "或区域 \"100,200,400,500\"（自动取中心点）")
            self._var_pos_widget = QWidget()
            var_row = QHBoxLayout(self._var_pos_widget)
            var_row.setContentsMargins(0, 0, 0, 0)
            var_row.addWidget(self.pos_var)
            var_row.addStretch(1)
            form.addRow("", self._var_pos_widget)

            self.follow_radio.toggled.connect(self._sync_pos_rows)
            self.fixed_radio.toggled.connect(self._sync_pos_rows)
            self.var_radio.toggled.connect(self._sync_pos_rows)
            self._var_combo_hint(form)
            self.count = self._spin(0, 999_999_999)
            self.count.setSpecialValueText("0 = 无限（流程中按 1 次执行）")
            form.addRow("点击次数", self.count)
            self.duration = self._dspin(0, 604800, " 秒")
            self.duration.setSpecialValueText("0 = 不限")
            form.addRow("持续时长", self.duration)
            self._build_background_row(form)
            self.btn_combo.setCurrentIndex(max(0, self.btn_combo.findData(p.get("mouse_button"))))
            self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(p.get("click_type"))))

        elif t == "press":
            self.keys_edit = HotkeyEdit()
            self.keys_edit.setMaximumWidth(220)
            form.addRow("连按按键", self.keys_edit)
            self.interval = self._spin(20, 3_600_000, " 毫秒")
            form.addRow("按下间隔", self.interval)
            self.count = self._spin(0, 999_999_999)
            self.count.setSpecialValueText("0 = 无限（流程中按 1 次执行）")
            form.addRow("按压次数", self.count)
            self.duration = self._dspin(0, 604800, " 秒")
            self.duration.setSpecialValueText("0 = 不限")
            form.addRow("持续时长", self.duration)
            self._build_background_row(form)

        elif t == "find":
            img_row = QHBoxLayout()
            self.preview = QLabel()
            self.preview.setFixedSize(230, 150)
            self.preview.setAlignment(Qt.AlignCenter)
            self.preview.setStyleSheet(
                "border: 1px solid #c9d1d9; border-radius: 6px; background: #f7f9fb;")
            img_row.addWidget(self.preview)

            side = QVBoxLayout()
            side.setSpacing(6)
            self.capture_btn = QPushButton("📷 屏幕截图选区")
            self.capture_btn.setToolTip("与「添加模板」相同：冻结屏幕 -> 框选 -> 调整 -> 双击确认")
            self.capture_btn.clicked.connect(self._request_capture)
            side.addWidget(self.capture_btn)
            self.image_edit = QLineEdit()
            self.image_edit.setReadOnly(True)
            self.image_edit.setStyleSheet("color: #8a939c; border: none; background: transparent;")
            side.addWidget(self.image_edit)
            side.addStretch(1)
            img_row.addLayout(side, 1)
            form.addRow("模板图", img_row)
            self.confidence = QDoubleSpinBox()
            self.confidence.setRange(0.5, 0.99)
            self.confidence.setDecimals(2)
            self.confidence.setSingleStep(0.01)
            form.addRow("匹配置信度", self.confidence)
            self.interval = self._spin(50, 3_600_000, " 毫秒")
            form.addRow("命中后间隔", self.interval)
            click_row = QHBoxLayout()
            self.click_combo = QComboBox()
            self.click_combo.addItem("单击", "single")
            self.click_combo.addItem("双击", "double")
            self.click_combo.addItem("右键", "right")
            click_row.addWidget(self.click_combo)
            click_row.addWidget(QLabel("偏移 X"))
            self.offset_x = self._spin(-9999, 9999)
            click_row.addWidget(self.offset_x)
            click_row.addWidget(QLabel("Y"))
            self.offset_y = self._spin(-9999, 9999)
            click_row.addWidget(self.offset_y)
            click_row.addStretch(1)
            form.addRow("命中动作", click_row)
            self.timeout = self._dspin(0, 86400, " 秒")
            self.timeout.setSpecialValueText("0 = 一直等到找到")
            form.addRow("搜索超时", self.timeout)
            region_row = QHBoxLayout()
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            clear_region = QPushButton("恢复全屏")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            region_row.addWidget(self.region_edit, 1)
            region_row.addWidget(pick_region)
            region_row.addWidget(clear_region)
            form.addRow("找图区域", region_row)

        elif t == "wait":
            self.seconds = self._dspin(0.1, 3600, " 秒")
            self.seconds.setDecimals(1)
            form.addRow("等待时长", self.seconds)

        elif t == "app":
            path_row = QHBoxLayout()
            self.path_edit = QLineEdit()
            self.path_edit.setPlaceholderText("选择要打开的应用程序（.exe / 快捷方式 / 文档）")
            browse = QPushButton("浏览…")
            browse.clicked.connect(self._browse_app)
            path_row.addWidget(self.path_edit, 1)
            path_row.addWidget(browse)
            form.addRow("应用路径", path_row)
            self.wait_sec = self._dspin(0, 300, " 秒")
            self.wait_sec.setSpecialValueText("0 = 启动后立刻下一步")
            form.addRow("启动后等待", self.wait_sec)

        elif t == "close_app":
            target_row = QHBoxLayout()
            self.target_edit = QLineEdit()
            self.target_edit.setPlaceholderText("如 notepad.exe（可点右侧从进程列表选择）")
            browse2 = QPushButton("从进程列表选择…")
            browse2.setToolTip("打开正在运行的进程列表，选择要关闭的进程")
            browse2.clicked.connect(self._browse_close_app)
            target_row.addWidget(self.target_edit, 1)
            target_row.addWidget(browse2)
            form.addRow("要关闭的应用", target_row)
            self.wait_sec = self._dspin(0, 60, " 秒")
            self.wait_sec.setSpecialValueText("0 = 不等待")
            form.addRow("关闭后等待", self.wait_sec)
            hint = QLabel("说明：按进程名结束该应用的所有实例（含子进程）。")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #8a939c; font-size: 9pt;")
            hint_row = QHBoxLayout()
            hint_row.addWidget(hint)
            form.addRow("", hint_row)

        elif t == "web":
            self._web_rows = []          # [(form, 行号)] 供显隐切换
            if step.pair_id:
                pair_hint = QLabel("🔗 本步骤与另一网页步骤成对出现（打开网址 ↔ 关闭浏览器），"
                                   "删除任一个会同步删除另一个；\n修改下方「操作」类型将解除配对。")
                pair_hint.setWordWrap(True)
                pair_hint.setStyleSheet("color: #1668a8; background: #e8f1fa;"
                                        "border: 1px solid #b9d3e8; border-radius: 4px;"
                                        "padding: 4px 6px;")
                form.addRow("", pair_hint)

            self.web_action = QComboBox()
            for k, v in WEB_ACTIONS.items():
                self.web_action.addItem(v, k)
            form.addRow("操作", self.web_action)

            self.url_edit = QLineEdit()
            self.url_edit.setPlaceholderText("如 https://www.baidu.com（不带 http 前缀会自动补 https://）")
            self._web_row(form, "url", "网址", self.url_edit)

            self.launch_combo = QComboBox()
            for k, v in LAUNCH_MODES.items():
                self.launch_combo.addItem(v, k)
            self.launch_combo.setToolTip(
                "只在首次启动浏览器时生效。\n"
                "浏览器已经开着时，会沿用现有实例（避免丢登录态），不会为了换模式把它杀掉。")
            self._web_row(form, "launch", "打开方式", self.launch_combo)

            self.tab_target_combo = QComboBox()
            self.tab_target_combo.addItem("在当前标签打开", "reuse")
            self.tab_target_combo.addItem("新开一个标签", "new")
            self._web_row(form, "tab_target", "标签", self.tab_target_combo)

            self.load_timeout = self._dspin(1, 300, " 秒")
            self._web_row(form, "load_timeout", "加载超时", self.load_timeout)

            self.wait_after = self._dspin(0, 300, " 秒")
            self.wait_after.setSpecialValueText("0 = 不等待")
            self._web_row(form, "wait_after", "打开后等待", self.wait_after)

            self.tab_scope_combo = QComboBox()
            for k, v in TAB_SCOPES.items():
                self.tab_scope_combo.addItem(v, k)
            self._web_row(form, "scope", "关闭范围", self.tab_scope_combo)

            self.match_edit = QLineEdit()
            self.match_edit.setPlaceholderText("匹配标签的网址或标题（不区分大小写）")
            self._web_row(form, "match", "匹配文字", self.match_edit)

            self.web_close_hint = QLabel(
                "退出整个浏览器，并释放流程持有的浏览器会话。\n"
                "关掉之后，下一个「打开网址」步骤会重新开一个浏览器（登录态不保留）。")
            self.web_close_hint.setWordWrap(True)
            self.web_close_hint.setStyleSheet("color: #6a737d;")
            self._web_row(form, "close_hint", "说明", self.web_close_hint)

            self.web_action.currentIndexChanged.connect(self._sync_web_rows)
            self.tab_scope_combo.currentIndexChanged.connect(self._sync_web_rows)

        elif t == "py_func":
            # 代码编辑框：等宽字体，placeholder 给出常用示例
            self.code_edit = QPlainTextEdit()
            code_font = QFont("Consolas")
            code_font.setPointSize(10)
            self.code_edit.setFont(code_font)
            self.code_edit.setPlaceholderText(
                "把 def 函数定义或任意 Python 代码粘贴到这里，例如：\n"
                "import datetime\n"
                "def print_current_time(date_format=\"%Y-%m-%d\", "
                "time_format=\"%H:%M:%S\"):\n"
                "    now = datetime.datetime.now()\n"
                "    date_part = now.strftime(date_format)\n"
                "    time_part = now.strftime(time_format)\n"
                "    return f\"{date_part} {time_part}\"")
            self.code_edit.setMinimumHeight(210)
            form.addRow("Python 代码", self.code_edit)

            self.func_edit = QLineEdit()
            self.func_edit.setPlaceholderText("如 print_current_time —— 代码中定义的函数名（必填）")
            form.addRow("调用函数名", self.func_edit)

            # 可引用变量：动态增删的流程变量列表（与形参同名则自动传入，其余注入环境）
            self._py_var_combos: list[QComboBox] = []
            self._py_var_rows: list[QWidget] = []
            var_widget = QWidget()
            var_outer = QVBoxLayout(var_widget)
            var_outer.setContentsMargins(0, 0, 0, 0)
            var_outer.setSpacing(4)
            self._py_var_box = QVBoxLayout()
            self._py_var_box.setContentsMargins(0, 0, 0, 0)
            self._py_var_box.setSpacing(4)
            var_outer.addLayout(self._py_var_box)
            self._py_add_btn = QPushButton("➕ 添加变量")
            self._py_add_btn.setCursor(Qt.PointingHandCursor)
            self._py_add_btn.setToolTip(
                "把流程中声明的变量加进来：与函数形参同名的会自动作为参数传入函数，"
                "其余注入代码环境供内部读取")
            self._py_add_btn.clicked.connect(lambda: self._add_py_var_row())
            var_outer.addWidget(self._py_add_btn, 0, Qt.AlignLeft)
            self._py_no_var_hint = QLabel("流程中暂无变量：先添加「变量」步骤声明，"
                                          "即可在这里勾选供代码使用。")
            self._py_no_var_hint.setStyleSheet("color: #8a939c; font-size: 9pt;")
            self._py_no_var_hint.setWordWrap(True)
            var_outer.addWidget(self._py_no_var_hint)
            form.addRow("参数传入变量", var_widget)

            # 结果变量：函数返回值保存到这里
            self.py_result_var = self._var_combo("（选择变量）")
            self.py_result_var.setToolTip(
                "函数返回值将写入该流程变量，供后续步骤使用")
            form.addRow("结果保存到", self.py_result_var)
            self._var_combo_hint(form)

            hint = QLabel("运行逻辑：\n"
                          "· 先执行上方代码，再调用「调用函数名」指定的函数，把返回值作为结果；\n"
                          "· 「参数传入变量」勾选的变量：与函数形参**同名**的会自动作为参数传入"
                          "（如变量 date_format、time_format 勾选后 → "
                          "print_current_time(date_format=值, time_format=值)），"
                          "不同名的仍注入代码环境，函数体内可直接读取；\n"
                          "· 函数必填形参若无同名变量可传且无默认值，运行时会明确报错；\n"
                          "· 结果会保存到上方选择的流程变量，供后续步骤使用。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self._refresh_py_var_state()

        root.addLayout(form)

        if t in ("find", "web"):
            # 网页步骤同样适用：网址打不开不该把整个流程一棍子打死
            text = ("找不到目标时跳过本步，继续执行后续步骤（默认终止流程）" if t == "find"
                    else "本步失败时跳过，继续执行后续步骤（默认终止流程）")
            self.continue_box = QCheckBox(text)
            root.addWidget(self.continue_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _spin(lo, hi, suffix="") -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        if suffix:
            s.setSuffix(suffix)
        return s

    @staticmethod
    def _dspin(lo, hi, suffix="") -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(1)
        if suffix:
            s.setSuffix(suffix)
        return s

    # ---------- 后台操作 / 窗口绑定 ----------
    def _add_window_picker(self, form: QFormLayout, label: str = "目标窗口") -> QWidget:
        """添加窗口选取行（标题显示框 + 选取按钮），返回包装 QWidget。

        窗口按**标题**匹配，运行时动态查找句柄，应用重启后仍有效。
        """
        widget = QWidget()
        win_row = QHBoxLayout(widget)
        win_row.setContentsMargins(0, 0, 0, 0)
        self.window_edit = QLineEdit()
        self.window_edit.setReadOnly(True)
        self.window_edit.setPlaceholderText("未绑定窗口")
        self.window_edit.setStyleSheet("color: #6a737d; background: #f7f9fb;")
        pick_win = QPushButton("🎯 选取窗口")
        pick_win.setToolTip("隐藏本程序后，移动鼠标到目标窗口上（会实时高亮），\n"
                            "单击确认，右键或 Esc 取消。窗口按标题匹配，重启后仍有效。")
        pick_win.clicked.connect(self._request_window)
        self._pick_win_btn = pick_win
        win_row.addWidget(self.window_edit, 1)
        win_row.addWidget(pick_win)
        form.addRow(label, widget)
        return widget

    def _build_background_row(self, form: QFormLayout, label: str = "置顶应用") -> None:
        """置顶应用行（click/press）：复选框 + 窗口选取，勾选才显示窗口行。"""
        self.background_box = QCheckBox(label)
        self.background_box.setToolTip(
            "勾选后把键鼠定向到下方绑定的窗口执行：运行期间会短暂把该窗口\n"
            "带到前台（结束后自动还原），因此对浏览器、新版记事本等现代应用\n"
            "也有效。窗口按标题匹配，应用重启后仍能找到。")
        form.addRow("", self.background_box)
        win_widget = self._add_window_picker(form)
        win_widget.setVisible(False)
        self.background_box.toggled.connect(win_widget.setVisible)

    def _request_window(self):
        self.hide()
        self.windowCaptureRequested.emit()

    def set_window(self, hwnd: int, title: str) -> None:
        """窗口识别后回填标题（运行时按标题匹配，句柄不持久化）。"""
        self._window_title = title or ""
        self._set_window_text()

    def _set_window_text(self) -> None:
        """按当前绑定的标题刷新显示框。"""
        title = getattr(self, "_window_title", "") or ""
        if title:
            self.window_edit.setText(title)
            self.window_edit.setStyleSheet("color: #1668a8; background: #eaf3fb;")
        else:
            self.window_edit.setText("未绑定窗口")
            self.window_edit.setStyleSheet("color: #6a737d; background: #f7f9fb;")

    def finish_window_capture(self):
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    def _browse_app(self):
        from PySide6.QtWidgets import QFileDialog
        start = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "选择应用", start,
            "应用程序 (*.exe *.bat *.cmd *.lnk);;所有文件 (*)")
        if path:
            self.path_edit.setText(path)

    def _browse_close_app(self):
        """打开进程列表选择：输入框显示完整描述（应用名—进程名「窗口标题」），
        实际按进程名关闭（单独存到 _close_app_name，避免显示文本带进参数）。"""
        dlg = ProcessPickerDialog(self)
        if dlg.exec() == QDialog.Accepted:
            item = dlg.selected_item()
            if item:
                self._close_app_name = item.get("name", "")
                self.target_edit.setText(dlg._display(item))
                self.target_edit.setToolTip(
                    f"实际按进程名 {self._close_app_name} 关闭（含同名子进程）")

    # ---------- python函数步骤：代码可引用变量（动态增删） ----------
    def _add_py_var_row(self, name: str = ""):
        """追加一行「流程变量」下拉；旧配置残留的名字经 _set_combo_value 补项保留。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        combo = self._var_combo("（选择流程变量）")
        combo.setToolTip("与函数形参同名时会自动作为参数传入；不同名的注入代码环境供函数内部读取")
        self._set_combo_value(combo, name)
        lay.addWidget(combo, 1)
        rm = QPushButton("🗑")
        rm.setFixedSize(36, 28)
        rm.setCursor(Qt.PointingHandCursor)
        rm.setToolTip("移除该变量")
        rm.setStyleSheet(
            "QPushButton { border: 1px solid #e1e4e8; border-radius: 4px;"
            " background: #ffffff; color: #c0392b; font-weight: 600; }"
            "QPushButton:hover { background: #fdeeee; border-color: #e0a0a0; }")
        rm.clicked.connect(lambda _, c=combo: self._remove_py_var_row(c))
        lay.addWidget(rm)
        self._py_var_box.addWidget(row)
        self._py_var_rows.append(row)
        self._py_var_combos.append(combo)
        self.adjustSize()

    def _remove_py_var_row(self, combo: QComboBox):
        """移除某变量行。摘除父级的三步顺序见 _clear_py_var_rows 注释。"""
        row = combo.parentWidget()
        if row is None:
            return
        self._py_var_box.removeWidget(row)
        if combo in self._py_var_combos:
            self._py_var_combos.remove(combo)
        if row in self._py_var_rows:
            self._py_var_rows.remove(row)
        row.hide()
        row.setParent(None)
        row.deleteLater()
        self.adjustSize()

    def _clear_py_var_rows(self):
        """清空全部变量行。动态子控件必须 hide → setParent(None) → deleteLater 三步，
        否则可见控件脱离父级的瞬间会闪成顶层窗口（见项目沉淀的 _clear_layout 教训）。"""
        for combo in self._py_var_combos:
            row = combo.parentWidget()
            if row is not None:
                self._py_var_box.removeWidget(row)
                row.hide()
                row.setParent(None)
                row.deleteLater()
        self._py_var_combos.clear()
        self._py_var_rows.clear()

    def _refresh_py_var_state(self):
        """流程有变量才显示「添加变量」按钮；没有则显示引导提示。"""
        has = bool(self._flow_var_names())
        self._py_add_btn.setVisible(has)
        self._py_no_var_hint.setVisible(not has)

    # ---------- 网页步骤：按操作类型显隐参数行 ----------
    def _web_row(self, form: QFormLayout, key: str, label: str, field) -> None:
        """登记一行（key 决定它在哪种操作下显示）。"""
        form.addRow(label, field)
        self._web_rows.append((key, form, form.rowCount() - 1))

    def _sync_web_rows(self) -> None:
        """只显示与当前操作相关的行，避免用户面对一堆无关输入框。"""
        if not getattr(self, "_web_rows", None):
            return
        act = self.web_action.currentData()
        scope = self.tab_scope_combo.currentData()
        want = {
            "url": act == "open",
            "launch": act == "open",
            "tab_target": act == "open",
            "load_timeout": act == "open",
            "wait_after": act == "open",
            "scope": act == "close_tab",
            "match": act == "close_tab" and scope == "match",
            "close_hint": act == "close_browser",
        }
        for key, form, row in self._web_rows:
            form.setRowVisible(row, want.get(key, False))
        self.adjustSize()

    def _check_var_name(self):
        """变量名失焦时检查是否与流程中已有变量重复（排除自身），重复则提示。"""
        name = self.var_name.text().strip()
        if not name:
            return
        self_name = (self._step.params.get("name") or "").strip()   # 编辑已有步骤时排除自己
        for n in self._flow_var_names():
            if n == self_name:
                continue
            if n == name:
                QMessageBox.warning(
                    self, "变量名已存在",
                    f"流程中已有变量「{name}」，本步骤声明会覆盖它的值。\n"
                    "确认无误可继续，或换个变量名。")
                return

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str):
        """回填变量下拉：值在列表里就选中该项；不在列表（旧配置残留）时补项选中，
        保证再次保存不丢失；空值选第一项（占位项）。"""
        value = (value or "").strip()
        if not value:
            combo.setCurrentIndex(0)
            return
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif combo.isEditable():
            combo.setEditText(value)
        else:
            combo.addItem(value, value)
            combo.setCurrentIndex(combo.count() - 1)

    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        """读取可编辑下拉的值：当前显示文本与选中项一致时用 data（如「无」→空），
        否则用用户手动输入的文本（如 'a,b'）。"""
        text = combo.currentText().strip()
        idx = combo.currentIndex()
        if 0 <= idx < combo.count() and combo.itemText(idx) == text:
            data = combo.currentData()
            if data is not None:
                return str(data)
        return text

    # ---------- 数据 ----------
    def _fill(self, step: FlowStep):
        p = step.params
        t = step.type
        if t == "var":
            self.var_name.setText(p.get("name", "") or "")
            self.var_type.setCurrentIndex(max(0, self.var_type.findData(p.get("type", "string"))))
            self.var_default.setText(str(p.get("default_value", "") or ""))
        elif t in ("if", "elseif", "while"):
            self.cond_edit.setText(p.get("condition", "") or "")
        elif t == "foreach":
            self._set_combo_value(self.foreach_items, p.get("items", "") or "")
            self.foreach_item_var.setText(p.get("item_var", "item") or "")
            self.foreach_index_var.setText(p.get("index_var", "index") or "")
        elif t == "exit":
            self._set_combo_value(self.exit_var, p.get("variable", "") or "")
        elif t == "log":
            self._set_combo_value(self.log_vars, p.get("variables", "") or "")
            self.log_text.setText(p.get("text", "") or "")
            self.raw_check.setChecked(bool(p.get("raw")))
        elif t == "clip_set":
            self._set_combo_value(self.clip_name, p.get("name", "") or "")
            self.clip_text.setText(p.get("text", "") or "")
        elif t == "clip_get":
            self._set_combo_value(self.clip_variable, p.get("variable", "") or "")
        elif t == "ocr":
            self._set_combo_value(self.ocr_variable, p.get("variable", "") or "")
            self.ocr_lang.setCurrentIndex(max(0, self.ocr_lang.findData(p.get("lang", "ch"))))
            self.ocr_multi.setChecked(bool(p.get("multi_ocr", True)))
            self._set_region_text(p.get("region", "") or "")
        elif t == "text_find":
            self.tf_text.setText(p.get("text", "") or "")
            self._set_region_text(p.get("region", "") or "")
            self.tf_click.setChecked(bool(p.get("click")))
            self.tf_button.setCurrentIndex(
                max(0, self.tf_button.findData(p.get("click_button", "left"))))
            self.tf_button.setEnabled(self.tf_click.isChecked())
            self._set_combo_value(self.tf_variable, p.get("variable", "") or "")
        elif t == "screenshot":
            self._set_region_text(p.get("region", "") or "")
            if p.get("save_mode") == "choose":
                self.save_choose_radio.setChecked(True)
            else:
                self.save_var_radio.setChecked(True)
            self._set_combo_value(self.shot_variable, p.get("variable", "") or "")
            self._sync_shot_save_rows()
        elif t == "find_image":
            self._image = p.get("image", "") or ""
            self._image_path = p.get("image_path", "") or ""
            self._update_preview()
            self.confidence.setValue(float(p.get("confidence", 0.85)))
            self._set_region_text(p.get("region", "") or "")
            self._set_combo_value(self.find_var, p.get("variable", "") or "")
            self.preview_check.setChecked(bool(p.get("preview")))
            self.preview_spin.setValue(float(p.get("preview_duration", 1.0) or 1.0))
            self.preview_spin.setEnabled(self.preview_check.isChecked())
        elif t == "yolo_detect":
            self.model_path_edit.setText(p.get("model_path", "") or "")
            self._set_region_text(p.get("region", "") or "")
            self.classes_edit.setText(p.get("classes", "") or "")
            self.confidence.setValue(float(p.get("confidence", 0.5)))
            self.device_combo.setCurrentIndex(
                max(0, self.device_combo.findData(p.get("device", "cuda"))))
            self.action_combo.setCurrentIndex(
                max(0, self.action_combo.findData(p.get("action", "none"))))
            self._set_combo_value(self.yolo_var, p.get("variable", "") or "")
            self.preview_check.setChecked(bool(p.get("preview")))
            self.preview_spin.setValue(float(p.get("preview_duration", 1.0) or 1.0))
            self.preview_spin.setEnabled(self.preview_check.isChecked())
        elif t == "click":
            pv = (p.get("pos_var") or "").strip()
            if p.get("fixed_position"):
                if pv:
                    self.var_radio.setChecked(True)
                else:
                    self.fixed_radio.setChecked(True)
            else:
                self.follow_radio.setChecked(True)
            self.pos_x.setValue(int(p.get("pos_x", 0)))
            self.pos_y.setValue(int(p.get("pos_y", 0)))
            self._set_combo_value(self.pos_var, pv)
            self._sync_pos_rows()
            self.count.setValue(int(p.get("count", 1)))
            self.duration.setValue(float(p.get("duration_sec", 0)))
            self._fill_background(p)
        elif t == "press":
            self.keys_edit.set_hotkey(p.get("keys", "space"))
            self.interval.setValue(int(p.get("interval_ms", 100)))
            self.count.setValue(int(p.get("count", 1)))
            self.duration.setValue(float(p.get("duration_sec", 0)))
            self._fill_background(p)
        elif t == "find":
            self._image = p.get("image", "") or ""
            self._image_path = p.get("image_path", "") or ""
            self._update_preview()
            self.confidence.setValue(float(p.get("confidence", 0.85)))
            self.interval.setValue(int(p.get("interval_ms", 500)))
            self.click_combo.setCurrentIndex(max(0, self.click_combo.findData(p.get("click_type"))))
            self.offset_x.setValue(int(p.get("offset_x", 0)))
            self.offset_y.setValue(int(p.get("offset_y", 0)))
            self.timeout.setValue(float(p.get("search_timeout_sec", 10)))
            self._set_region_text(p.get("region", "") or "")
            self.continue_box.setChecked(step.continue_on_fail)
        elif t == "wait":
            self.seconds.setValue(float(p.get("seconds", 1)))
        elif t == "app":
            self.path_edit.setText(p.get("path", "") or "")
            self.wait_sec.setValue(float(p.get("wait_sec", 2)))
        elif t == "close_app":
            target = p.get("target", "") or ""
            self._close_app_name = (p.get("process") or "").strip() or target
            self.target_edit.setText(target or self._close_app_name)
            self.wait_sec.setValue(float(p.get("wait_sec", 0.5)))
        elif t == "web":
            self.web_action.setCurrentIndex(max(0, self.web_action.findData(p.get("action"))))
            self.url_edit.setText(p.get("url", "") or "")
            self.launch_combo.setCurrentIndex(
                max(0, self.launch_combo.findData(p.get("launch_mode"))))
            self.tab_target_combo.setCurrentIndex(
                max(0, self.tab_target_combo.findData(p.get("tab_target"))))
            self.load_timeout.setValue(float(p.get("load_timeout_sec", 20)))
            self.wait_after.setValue(float(p.get("wait_after_sec", 0)))
            self.tab_scope_combo.setCurrentIndex(
                max(0, self.tab_scope_combo.findData(p.get("tab_scope"))))
            self.match_edit.setText(p.get("match_text", "") or "")
            self.continue_box.setChecked(step.continue_on_fail)
            self._sync_web_rows()
        elif t == "py_func":
            self.code_edit.setPlainText(p.get("code", "") or "")
            self.func_edit.setText(p.get("func_name", "") or "")
            self._clear_py_var_rows()
            for n in p.get("variables") or []:
                n = (str(n) or "").strip()
                if n:
                    self._add_py_var_row(n)
            self._set_combo_value(self.py_result_var, p.get("result_var", "") or "")
            self._refresh_py_var_state()

    def _fill_background(self, p: dict) -> None:
        """回填后台操作/窗口绑定字段（click/press）。"""
        self._window_title = p.get("window_title", "") or ""
        self.background_box.setChecked(bool(p.get("background")))
        self._set_window_text()

    def apply_to(self, step: FlowStep) -> None:
        t = step.type
        if t == "var":
            step.params.update({
                "name": self.var_name.text().strip(),
                "type": self.var_type.currentData(),
                "default_value": self.var_default.text(),
            })
        elif t in ("if", "elseif", "while"):
            step.params.update({"condition": self.cond_edit.text().strip()})
        elif t == "foreach":
            step.params.update({
                "items": self._combo_value(self.foreach_items),
                "item_var": self.foreach_item_var.text().strip() or "item",
                "index_var": self.foreach_index_var.text().strip() or "index",
            })
        elif t == "exit":
            step.params.update({"variable": self._combo_value(self.exit_var)})
        elif t == "log":
            step.params.update({
                "variables": self._combo_value(self.log_vars),
                "text": self.log_text.text(),
                "raw": self.raw_check.isChecked(),
            })
        elif t == "clip_set":
            step.params.update({
                "name": self._combo_value(self.clip_name),
                "text": self.clip_text.text(),
            })
        elif t == "clip_get":
            step.params.update({"variable": self._combo_value(self.clip_variable)})
        elif t == "ocr":
            step.params.update({
                "variable": self._combo_value(self.ocr_variable),
                "lang": self.ocr_lang.currentData(),
                "multi_ocr": self.ocr_multi.isChecked(),
                "region": getattr(self, "_region", step.params.get("region", "")) or "",
            })
        elif t == "text_find":
            step.params.update({
                "text": self.tf_text.text().strip(),
                "region": getattr(self, "_region", step.params.get("region", "")) or "",
                "click": self.tf_click.isChecked(),
                "click_button": self.tf_button.currentData(),
                "variable": self._combo_value(self.tf_variable),
            })
        elif t == "screenshot":
            step.params.update({
                "region": getattr(self, "_region", step.params.get("region", "")) or "",
                "save_mode": "variable" if self.save_var_radio.isChecked() else "choose",
                "variable": self._combo_value(self.shot_variable),
            })
        elif t == "find_image":
            new_image = getattr(self, "_image", "") or step.params.get("image", "") or ""
            new_path = getattr(self, "_image_path", "") or step.params.get("image_path", "") or ""
            step.params.update({
                "image": new_image,
                "image_path": new_path,
                "confidence": round(self.confidence.value(), 2),
                "region": getattr(self, "_region", step.params.get("region", "")) or "",
                "variable": self._combo_value(self.find_var),
                "preview": self.preview_check.isChecked(),
                "preview_duration": self.preview_spin.value(),
            })
        elif t == "yolo_detect":
            step.params.update({
                "model_path": self.model_path_edit.text().strip(),
                "region": getattr(self, "_region", step.params.get("region", "")) or "",
                "classes": self.classes_edit.text().strip(),
                "confidence": round(self.confidence.value(), 2),
                "device": self.device_combo.currentData(),
                "action": self.action_combo.currentData(),
                "variable": self._combo_value(self.yolo_var),
                "preview": self.preview_check.isChecked(),
                "preview_duration": self.preview_spin.value(),
            })
        elif t == "click":
            pv = self._combo_value(self.pos_var) if self.var_radio.isChecked() else ""
            step.params.update({
                "mouse_button": self.btn_combo.currentData(),
                "click_type": self.type_combo.currentData(),
                "interval_ms": self.interval.value(),
                # 变量坐标模式也算固定位置（不跟随鼠标），由 pos_var 区分
                "fixed_position": self.fixed_radio.isChecked() or self.var_radio.isChecked(),
                "pos_x": self.pos_x.value(), "pos_y": self.pos_y.value(),
                "pos_var": pv,
                "count": self.count.value(), "duration_sec": self.duration.value(),
            })
            self._apply_background(step)
        elif t == "press":
            step.params.update({
                "keys": self.keys_edit.hotkey(),
                "interval_ms": self.interval.value(),
                "count": self.count.value(), "duration_sec": self.duration.value(),
            })
            self._apply_background(step)
        elif t == "find":
            # 防呆：截图选区结果为空时保留原有模板，避免把已设模板写空
            new_image = getattr(self, "_image", "") or step.params.get("image", "") or ""
            new_path = getattr(self, "_image_path", "") or step.params.get("image_path", "") or ""
            step.params.update({
                "image": new_image,
                "image_path": new_path,
                "confidence": round(self.confidence.value(), 2),
                "interval_ms": self.interval.value(),
                "click_type": self.click_combo.currentData(),
                "offset_x": self.offset_x.value(), "offset_y": self.offset_y.value(),
                "search_timeout_sec": self.timeout.value(),
                "region": getattr(self, "_region", step.params.get("region", "")) or "",
            })
            step.continue_on_fail = self.continue_box.isChecked()
        elif t == "wait":
            step.params.update({"seconds": self.seconds.value()})
        elif t == "app":
            step.params.update({
                "path": self.path_edit.text().strip(),
                "wait_sec": self.wait_sec.value(),
            })
        elif t == "close_app":
            text = self.target_edit.text().strip()
            process = getattr(self, "_close_app_name", "") or text
            # 保存：target=完整描述（显示/回填用），process=进程名（运行时 taskkill 用）
            step.params.update({
                "target": text if text != process else process,
                "process": process,
                "wait_sec": self.wait_sec.value(),
            })
        elif t == "web":
            step.params.update({
                "action": self.web_action.currentData(),
                "url": self.url_edit.text().strip(),
                "launch_mode": self.launch_combo.currentData(),
                "tab_target": self.tab_target_combo.currentData(),
                "load_timeout_sec": self.load_timeout.value(),
                "wait_after_sec": self.wait_after.value(),
                "tab_scope": self.tab_scope_combo.currentData(),
                "match_text": self.match_edit.text().strip(),
            })
            step.continue_on_fail = self.continue_box.isChecked()
        elif t == "py_func":
            used: list[str] = []
            for combo in self._py_var_combos:
                n = self._combo_value(combo)
                if n and n not in used:
                    used.append(n)
            step.params.update({
                "code": self.code_edit.toPlainText(),
                "func_name": self.func_edit.text().strip(),
                "variables": used,
                "result_var": self._combo_value(self.py_result_var),
            })

    def _apply_background(self, step: FlowStep) -> None:
        """把后台操作/窗口绑定字段写回步骤参数（click/press）。"""
        step.params.update({
            "background": self.background_box.isChecked(),
            "window_title": getattr(self, "_window_title", "") or "",
        })

    # ---------- 模板图截图选区 ----------
    def _request_capture(self):
        self.hide()
        self.templateCaptureRequested.emit()

    def set_template_image(self, filename: str, fullpath: str = ""):
        self._image = filename
        if fullpath:
            self._image_path = fullpath
        self._update_preview()

    def _pick_image(self):
        """从本地选择一张图片作为找图模板，复制到 templates/ 供跨目录运行。"""
        from PySide6.QtWidgets import QFileDialog
        import shutil
        import time
        import uuid
        from ..config import TEMPLATE_DIR
        path, _ = QFileDialog.getOpenFileName(
            self, "选择模板图片", os.path.expanduser("~"),
            "图片文件 (*.png *.jpg *.jpeg *.bmp);;所有文件 (*)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower() or ".png"
        name = f"tpl_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}{ext}"
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        dst = os.path.join(TEMPLATE_DIR, name)
        try:
            shutil.copyfile(path, dst)
        except OSError as e:
            QMessageBox.warning(self, "复制失败", f"无法复制图片到模板目录：{e}")
            return
        self.set_template_image(name, dst)

    def finish_template_capture(self):
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    # ---------- 目标检测：模型文件 ----------
    def _pick_model_file(self):
        """浏览选择 YOLOv5 模型文件（选完仍可手动改路径）。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 YOLOv5 模型文件", os.path.expanduser("~"),
            "模型文件 (*.pt *.onnx *.engine);;所有文件 (*)")
        if path:
            self.model_path_edit.setText(path)

    def _check_model_path(self):
        """手动输入模型路径失焦时即时校验：非空但文件不存在则提示（不拦截继续编辑）。"""
        path = self.model_path_edit.text().strip()
        if path and not os.path.isfile(path):
            QMessageBox.warning(self, "模型路径无效",
                                f"模型文件不存在：\n{path}\n\n请检查路径是否正确。")


    def _update_preview(self):
        """显示模板实际图片（等比缩放到预览框）；缺失时显示占位文字。"""
        from ..config import resolve_template_path
        filename = getattr(self, "_image", "")
        path = resolve_template_path(filename, getattr(self, "_image_path", ""))
        if path:
            pm = QPixmap(path)
            if not pm.isNull():
                self.preview.setPixmap(pm.scaled(self.preview.size(), Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
                self.image_edit.setText(f"{os.path.basename(path)}（{pm.width()} x {pm.height()}）")
                return
            self.preview.setText("模板文件缺失\n请重新截图")
        else:
            self.preview.setText("未设置模板\n点击「屏幕截图选区」")
        self.preview.setStyleSheet(
            "border: 1px solid #c9d1d9; border-radius: 6px; background: #f7f9fb;")
        self.image_edit.setText(os.path.basename(filename or ""))

    # ---------- 区域 ----------
    def _set_region_text(self, region):
        from ..config import parse_region_str
        self._region = region or ""
        rt = parse_region_str(self._region)
        self.region_edit.setText(f"{rt[0]}, {rt[1]}, {rt[2]} x {rt[3]}" if rt else "全屏（整个虚拟桌面）")

    def _apply_manual_region(self):
        """解析「左上x,左上y,右下x,右下y」输入，转成 "x,y,w,h" 存入 _region 并刷新显示。"""
        text = (self.manual_edit.text() or "").strip()
        text = text.replace("（", "(").replace("）", ")").replace("，", ",")
        parts = []
        for v in text.strip("() ").split(","):
            v = v.strip()
            if not v:
                continue
            try:
                parts.append(int(v))
            except ValueError:
                QMessageBox.warning(self, "坐标格式错误",
                                    "请按「左上x,左上y,右下x,右下y」输入 4 个整数，\n如 100,200,400,500。")
                return
        if len(parts) != 4:
            QMessageBox.warning(self, "坐标格式错误",
                                "需要 4 个数字：左上x,左上y,右下x,右下y，\n如 100,200,400,500。")
            return
        x1, y1, x2, y2 = parts
        if x2 <= x1 or y2 <= y1:
            QMessageBox.warning(self, "坐标无效",
                                "右下角坐标需大于左上角坐标（x2>x1 且 y2>y1）。")
            return
        self._set_region_text(f"{x1},{y1},{x2 - x1},{y2 - y1}")
        self.manual_edit.clear()

    def _request_region(self):
        self.hide()
        self.regionCaptureRequested.emit()

    def set_region(self, rect):
        self._set_region_text(",".join(str(v) for v in rect))

    def finish_region_capture(self):
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    def _request_point(self) -> None:
        """点"屏幕点选坐标"：隐藏对话框，由外部隐藏主窗口并启动遮罩。"""
        self.hide()
        self.pointCaptureRequested.emit()

    def set_point(self, x: int, y: int) -> None:
        self.pos_x.setValue(int(x))
        self.pos_y.setValue(int(y))

    def finish_point_capture(self) -> None:
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
