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
from ..dp_actors import (DP_ELE_ACTIONS, DP_LISTEN_ACTIONS, DP_LOCATORS,
                         DP_MATCHES, DP_TAB_MODES)
from ..web_actors import LAUNCH_MODES, TAB_SCOPES
from .hotkey_edit import HotkeyEdit

MIME_TYPE = "application/x-qf-flow-type"

_TYPE_ICONS = {"var": "📦", "log": "📄", "ocr": "🔎", "text_find": "🔍",
               "screenshot": "📷", "find_image": "🎯", "yolo_detect": "🧠",
               "color_pick": "🎨",
               "click": "🖱", "press": "⌨", "find": "🖼",
               "wait": "⏱", "web": "🌐", "http_request": "📡", "deepseek": "🤖", "script": "📜", "notify": "🔔", "speech": "🔊", "app": "🚀", "close_app": "⏹",
               "clip_set": "📤", "clip_get": "📥", "py_func": "🐍",
               "if": "🔀", "elseif": "🔁", "else": "↩️", "endif": "🏁",
               "foreach": "🔄", "while": "♻️",
               "endForeach": "🏁", "endWhile": "🏁",
               "break": "🛑", "continue": "⏭️", "exit": "🔚",
               "dp_browser": "🖥", "dp_element": "🧩", "dp_tab": "🗂",
               "dp_listen": "🎧", "dp_page_shot": "📸", "dp_ele_shot": "🎞",
               "dp_upload": "📎",
               "dp_close_browser": "⏹"}


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
    """选择正在运行的进程（供「关闭应用」「打开应用」用）。

    每条显示「应用名 — 进程名 — 窗口标题」，窗口标题用于区分多开实例；
    支持关键字过滤，双击或确定返回选中的进程。purpose:
    - "close"：选要关闭的进程（按钮「关闭所选进程」）；
    - "open"：选要带出/打开的进程（按钮「选中该进程」，title 供回填 exe 路径）。
    """

    def __init__(self, parent=None, purpose: str = "close"):
        super().__init__(parent)
        self._purpose = purpose if purpose in ("close", "open") else "close"
        self.setWindowTitle("选择要关闭的进程" if self._purpose == "close"
                            else "选择要带出的进程")
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
        buttons.button(QDialogButtonBox.Ok).setText(
            "关闭所选进程" if self._purpose == "close" else "选中该进程")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        self.search.setFocus()

    def _update_hint(self):
        if self._purpose == "open":
            self.hint.setText(
                f"共 {len(self._processes)} 个正在运行的应用。\n"
                "选中后：运行「打开应用」时若该进程仍在运行 → 直接把窗口带到前台；\n"
                "若已不在运行 → 按它原来的 exe 完整路径重新启动。")
            return
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
    colorPickRequested = Signal()        # color_pick 步骤点"屏幕取色…"

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
        # 截图步骤：区域可留空（=全屏，默认），不强制框选；「默认保存」必须选择
        # 结果变量（自选保存可选）
        if self._step.type == "screenshot" and getattr(self, "save_var_radio", None) is not None:
            if self.save_var_radio.isChecked() and not self._combo_value(self.shot_variable):
                QMessageBox.warning(self, "请设置结果变量",
                                    "已选择「默认保存」，请选择接收截图路径的结果变量。")
                return
        # 屏幕取色：必须先取色，且必须选择结果变量
        if self._step.type == "color_pick" and getattr(self, "cp_variable", None) is not None:
            if not getattr(self, "_pick_rgb", None):
                QMessageBox.warning(self, "请先取色",
                                    "请点击「屏幕取色…」，在屏幕上单击拾取目标颜色。")
                return
            if not self._combo_value(self.cp_variable):
                QMessageBox.warning(self, "请设置结果变量",
                                    "请选择接收取色结果（HEX 如 #FF0000 / RGB 如 255,0,0）的结果变量。")
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
        # 网络请求：网址必填
        if self._step.type == "http_request" and getattr(self, "http_url", None) is not None:
            if not self.http_url.text().strip():
                QMessageBox.warning(self, "请填写网址",
                                    "网络请求步骤需要填写请求网址。\n"
                                    "不带 http/https 前缀时会自动补 https://。")
                return
        # DeepSeek 对话：提问必填，且必须选择结果变量
        if self._step.type == "deepseek" and getattr(self, "ds_question", None) is not None:
            if not self.ds_question.toPlainText().strip():
                QMessageBox.warning(self, "请填写提问内容",
                                    "DeepSeek 对话步骤需要填写提问内容。")
                return
            if not self._combo_value(self.ds_result_var):
                QMessageBox.warning(self, "请设置结果变量",
                                    "请选择接收 DeepSeek 回答的结果变量。")
                return
        # 执行脚本：文本来源必填内容；文件来源路径必填且文件须存在；结果变量必选
        if self._step.type == "script" and getattr(self, "sc_content", None) is not None:
            if self.sc_src_text_radio.isChecked():
                if not self.sc_content.toPlainText().strip():
                    QMessageBox.warning(self, "请填写脚本内容",
                                        "请粘贴要执行的 CMD / BAT / PowerShell 命令。")
                    return
            else:
                sc_path = self.sc_path.text().strip()
                if not sc_path:
                    QMessageBox.warning(self, "请指定脚本文件",
                                        "请填写脚本文件的完整路径，或点击「浏览…」选择。")
                    return
                if not os.path.isfile(sc_path):
                    QMessageBox.warning(self, "脚本文件不存在",
                                        f"文件不存在：\n{sc_path}\n\n请检查路径是否正确。")
                    return
            if not self._combo_value(self.sc_result_var):
                QMessageBox.warning(self, "请设置结果变量",
                                    "请选择接收脚本输出（stdout+stderr）的结果变量。")
                return
        # 消息通知：内容必填
        if self._step.type == "notify" and getattr(self, "nt_content", None) is not None:
            if not self.nt_content.toPlainText().strip():
                QMessageBox.warning(self, "请填写消息内容",
                                    "消息通知步骤需要填写要显示的消息内容。")
                return
        # 语音播报：内容必填
        if self._step.type == "speech" and getattr(self, "sp_content", None) is not None:
            if not self.sp_content.toPlainText().strip():
                QMessageBox.warning(self, "请填写播报内容",
                                    "语音播报步骤需要填写要朗读的内容（可直接输入文字，或用 $变量名 引用）。")
                return
        # 打开应用：勾选「进程打开」时目标进程与应用路径至少填一个；否则仅需应用路径
        if self._step.type == "app" and getattr(self, "app_proc_edit", None) is not None:
            if self.app_use_proc.isChecked():
                proc_txt = (self.app_proc_edit.text().strip()
                            or getattr(self, "_app_process_name", "") or "")
                if not self.path_edit.text().strip() and not proc_txt:
                    QMessageBox.warning(self, "请选择要打开的应用",
                                        "请填写「应用路径」，或点「从进程列表选择…」选一个运行中的进程。")
                    return
            elif not self.path_edit.text().strip():
                QMessageBox.warning(self, "请填写应用路径",
                                    "已关闭「进程打开」，请填写要打开的程序 / 文档 / 文件夹路径。")
                return
        # 网页「接管浏览器」：必须填写合法端口（1~65535），且与 --remote-debugging-port 一致
        if self._step.type == "web" and getattr(self, "attach_port_edit", None) is not None \
                and self.web_action.currentData() == "open" \
                and self.launch_combo.currentData() == "attach":
            raw = self.attach_port_edit.text().strip()
            try:
                port = int(raw)
            except ValueError:
                port = -1
            if not raw or not (0 < port <= 65535):
                QMessageBox.warning(
                    self, "请填写接管端口",
                    "「接管已打开的浏览器」需要填写调试端口（1~65535）。\n\n"
                    "设置方法：浏览器图标右键 → 属性 → 「目标」末尾加空格后填\n"
                    "--remote-debugging-port=端口号 → 确定，再用该端口启动浏览器；\n"
                    "这里的端口必须与 --remote-debugging-port 后面的数字一致（如 9333）。")
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
        # DrissionPage「打开浏览器」：浏览器变量必填
        if self._step.type == "dp_browser" and getattr(self, "dpb_var", None) is not None:
            if not self.dpb_var.text().strip():
                QMessageBox.warning(self, "请填写浏览器变量",
                                    "「打开浏览器」需要指定浏览器对象保存到的变量名（必填项）。\n"
                                    "后续「元素操作 / 切换标签 / 监听 / 截图 / 上传」都引用该变量。")
                return
        # DrissionPage 其余步骤：浏览器变量必填（取自「打开浏览器」步骤）
        _DP_BROWSER_ATTR = {"dp_element": "dpe_browser", "dp_tab": "dpt_browser",
                            "dp_listen": "dpl_browser", "dp_page_shot": "dps_browser",
                            "dp_ele_shot": "dpes_browser", "dp_upload": "dpu_browser",
                            "dp_close_browser": "dpc_browser"}
        if self._step.type in _DP_BROWSER_ATTR:
            combo = getattr(self, _DP_BROWSER_ATTR[self._step.type], None)
            if combo is not None and not self._combo_value(combo):
                QMessageBox.warning(self, "请选择浏览器变量",
                                    "请先执行「打开浏览器」步骤并把浏览器对象保存到变量，\n"
                                    "再在这里选择/填写该浏览器变量。")
                return
        # 元素操作：定位值必填；有返回值的操作必须设结果变量
        if self._step.type == "dp_element" and getattr(self, "dpe_value", None) is not None:
            if not self.dpe_value.text().strip():
                QMessageBox.warning(self, "请填写定位值", "「元素操作」需要填写元素定位值。")
                return
            act = self.dpe_action.currentData()
            if act in ("get_text", "get_attr", "for_new_tab") \
                    and not self._combo_value(self.dpe_result):
                QMessageBox.warning(self, "请设置结果变量",
                                    "该操作有返回值，请选择接收结果的结果变量。")
                return
        # 网页截图 / 元素截图：结果变量必填；元素截图还须定位值
        if self._step.type in ("dp_page_shot", "dp_ele_shot"):
            res_combo = getattr(self, "dps_result" if self._step.type == "dp_page_shot"
                                else "dpes_result", None)
            if res_combo is not None and not self._combo_value(res_combo):
                QMessageBox.warning(self, "请设置结果变量",
                                    "截图保存路径需要写入结果变量，请选择。")
                return
            if self._step.type == "dp_ele_shot" and not self.dpes_value.text().strip():
                QMessageBox.warning(self, "请填写定位值", "「元素截图」需要填写元素定位值。")
                return
        # 上传文件：定位值与文件路径必填
        if self._step.type == "dp_upload" and getattr(self, "dpu_value", None) is not None:
            if not self.dpu_value.text().strip():
                QMessageBox.warning(self, "请填写定位值", "「上传文件」需要填写元素定位值。")
                return
            if not self.dpu_files.toPlainText().strip():
                QMessageBox.warning(self, "请填写上传文件",
                                    "「上传文件」需要填写要上传的文件路径。")
                return
        # 切换标签：非「新建标签」时切换条件必填
        if self._step.type == "dp_tab" and getattr(self, "dpt_mode", None) is not None:
            if self.dpt_mode.currentData() != "new" and not self.dpt_value.text().strip():
                QMessageBox.warning(self, "请填写切换条件",
                                    "请填写标签序号 / 标题 / 网址（新建标签时不需要）。")
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
            self.show_type_check = QCheckBox("显示变量的 Python 类型")
            self.show_type_check.setToolTip("打印变量时在每个值后追加 Python 类型名，便于确认变量类型：\n"
                                            "如 count = 5 (int)、name = 张三 (str)、arr = [1, 2] (list)")
            form.addRow("", self.show_type_check)
            hint = QLabel("运行时会打印到底部日志（蓝色字体显示），便于调试流程。\n"
                          "勾选「原始输出」后不加时间戳、不自动换行，内容原样显示；\n"
                          "勾选「显示变量的 Python 类型」后，每个变量的值后显示类型名（如 (str)/(int)/(list)）。")
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
            # 截图区域：默认空 = 全屏（整个虚拟桌面）；可框选自定义局部区域
            self._shot_region_widget = QWidget()
            region_row = QHBoxLayout(self._shot_region_widget)
            region_row.setContentsMargins(0, 0, 0, 0)
            self.region_edit = QLineEdit()
            self.region_edit.setReadOnly(True)
            self.region_edit.setToolTip("空 = 全屏（整个虚拟桌面）；点击「框选区域…」可只截指定局部")
            pick_region = QPushButton("框选区域…")
            pick_region.clicked.connect(self._request_region)
            pick_region.setToolTip("隐藏本窗口后框选截图区域（与找图/文字识别区域一致）")
            clear_region = QPushButton("恢复全屏")
            clear_region.clicked.connect(lambda: self._set_region_text(None))
            clear_region.setToolTip("截图区域恢复为全屏")
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

        elif t == "color_pick":
            # 拾取结果：色块预览 + 只读颜色文本 + 「屏幕取色…」按钮
            self._pick_rgb = None        # 拾取到的颜色 (r, g, b)，取色/回填后非空
            color_row = QHBoxLayout()
            self.cp_swatch = QLabel()
            self.cp_swatch.setFixedSize(46, 30)
            self.cp_swatch.setAlignment(Qt.AlignCenter)
            self.cp_swatch.setStyleSheet(
                "border: 1px solid #c9d1d9; border-radius: 6px; background: #f7f9fb;")
            self.cp_value = QLineEdit()
            self.cp_value.setReadOnly(True)
            self.cp_value.setPlaceholderText("尚未取色")
            self.cp_value.setToolTip("屏幕上拾取的颜色值，文本格式跟随右侧「颜色格式」选择")
            pick_btn = QPushButton("🎨 屏幕取色…")
            pick_btn.setToolTip("隐藏窗口后取色：移动鼠标对准目标像素（中心十字即取色点，"
                                "周边像素放大预览），单击确认、右键或 Esc 取消")
            pick_btn.clicked.connect(self._request_color_pick)
            color_row.addWidget(self.cp_swatch)
            color_row.addWidget(self.cp_value, 1)
            color_row.addWidget(pick_btn)
            form.addRow("取色颜色", color_row)

            # 颜色格式：HEX (#RRGGBB) / RGB (255,0,0)，切换即时重排文本
            fmt_row = QHBoxLayout()
            self.cp_fmt_hex = QRadioButton("HEX")
            self.cp_fmt_hex.setToolTip("16 进制 #RRGGBB，如 #FF0000")
            self.cp_fmt_rgb = QRadioButton("RGB")
            self.cp_fmt_rgb.setToolTip("十进制约 255,0,0（红,绿,蓝 0~255）")
            self.cp_fmt_hex.setChecked(True)
            self.cp_fmt_hex.toggled.connect(lambda _: self._refresh_color_ui())
            self.cp_fmt_rgb.toggled.connect(lambda _: self._refresh_color_ui())
            fmt_row.addWidget(QLabel("颜色格式"))
            fmt_row.addWidget(self.cp_fmt_hex)
            fmt_row.addWidget(self.cp_fmt_rgb)
            fmt_row.addStretch(1)
            form.addRow("", fmt_row)

            self.cp_variable = self._var_combo("（选择变量）")
            self.cp_variable.setToolTip(
                "取色结果按所选格式写入该变量（HEX 如 #FF0000 / RGB 如 255,0,0），供后续步骤引用")
            form.addRow("结果变量", self.cp_variable)
            self._var_combo_hint(form)

            hint = QLabel("在屏幕任意位置取色：点击「屏幕取色…」后本窗口隐藏，鼠标对准目标像素\n"
                          "（中心十字即取色点，周边像素实时放大），单击确认取色并回到本窗口。\n"
                          "结果按「颜色格式」保存并写入结果变量：HEX=#RRGGBB / RGB=255,0,0。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

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
            self.app_use_proc = QCheckBox("进程打开（勾选时：目标进程已在运行则把窗口放到桌面最前）")
            self.app_use_proc.setToolTip(
                "勾选（默认）：目标进程已在运行 → 直接把窗口带到最前（不重复启动），\n"
                "未运行 → 用下方「应用路径」打开；\n"
                "取消勾选：忽略目标进程，直接用「应用路径」打开程序 / 文档 / 文件夹。")
            self.app_use_proc.setChecked(True)
            form.addRow("", self.app_use_proc)

            proc_row = QHBoxLayout()
            self.app_proc_edit = QLineEdit()
            self.app_proc_edit.setPlaceholderText(
                "从进程列表选择或手填进程名（如 chrome.exe）")
            pick_proc = QPushButton("从进程列表选择…")
            pick_proc.setToolTip("打开正在运行的进程列表，选择要带出/打开的进程")
            pick_proc.clicked.connect(self._browse_app_process)
            proc_row.addWidget(self.app_proc_edit, 1)
            proc_row.addWidget(pick_proc)
            form.addRow("目标进程", proc_row)
            self._app_proc_row = (form, form.rowCount() - 1)

            path_row = QHBoxLayout()
            self.path_edit = QLineEdit()
            self.path_edit.setPlaceholderText(
                "程序 / 快捷方式 / 文档 / 文件夹（支持所有文件类型）")
            browse = QPushButton("浏览文件…")
            browse.setToolTip("选择要打开的程序 / 文档（所有文件类型）")
            browse.clicked.connect(self._browse_app)
            browse_dir = QPushButton("浏览文件夹…")
            browse_dir.setToolTip("选择要用资源管理器打开的文件夹")
            browse_dir.clicked.connect(self._browse_app_dir)
            path_row.addWidget(self.path_edit, 1)
            path_row.addWidget(browse)
            path_row.addWidget(browse_dir)
            form.addRow("应用路径", path_row)
            self.wait_sec = self._dspin(0, 300, " 秒")
            self.wait_sec.setSpecialValueText("0 = 打开后立刻下一步")
            form.addRow("打开后等待", self.wait_sec)
            self._app_hint = QLabel("")
            self._app_hint.setWordWrap(True)
            self._app_hint.setStyleSheet("color: #8a939c; font-size: 9pt;")
            form.addRow("", self._app_hint)
            self.app_use_proc.toggled.connect(self._sync_app_rows)
            self._sync_app_rows()

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
                "浏览器已经开着时，会沿用现有实例（避免丢登录态），不会为了换模式把它杀掉。\n"
                "「接管已打开的浏览器」：连接手动用 --remote-debugging-port=N 打开的浏览器，"
                "直接开新标签，不新起浏览器进程。")
            self._web_row(form, "launch", "打开方式", self.launch_combo)

            self.attach_port_edit = QLineEdit()
            self.attach_port_edit.setPlaceholderText("如 9333")
            self.attach_port_edit.setToolTip(
                "接管端口：与浏览器快捷方式里 --remote-debugging-port 后面的数字一致。\n"
                "设置方法：浏览器图标右键 → 属性 → 「目标」末尾加空格后填 "
                "--remote-debugging-port=9333 → 确定，再用此端口启动浏览器。")
            self._web_row(form, "attach_port", "接管端口", self.attach_port_edit)

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
                "结束本步骤管理的浏览器会话：\n"
                "· 程序自己启动的浏览器 —— 直接退出，下一个「打开网址」会重开（登录态不保留）；\n"
                "· 接管（attach）的浏览器 —— 只断开连接、窗口保留，可继续手动使用。")
            self.web_close_hint.setWordWrap(True)
            self.web_close_hint.setStyleSheet("color: #6a737d;")
            self._web_row(form, "close_hint", "说明", self.web_close_hint)

            self.web_action.currentIndexChanged.connect(self._sync_web_rows)
            self.launch_combo.currentIndexChanged.connect(self._sync_web_rows)
            self.tab_scope_combo.currentIndexChanged.connect(self._sync_web_rows)

        elif t == "http_request":
            from ..config import DEFAULT_USER_AGENT
            self.http_url = QLineEdit()
            self.http_url.setPlaceholderText("如 https://api.example.com/data（必填，不带协议自动补 https://）")
            self.http_url.setToolTip("请求网址，支持 $变量名 引用；不带 http/https 前缀会自动补 https://")
            form.addRow("网址", self.http_url)

            self.http_method = QComboBox()
            self.http_method.addItem("GET", "get")
            self.http_method.addItem("POST", "post")
            self.http_method.setToolTip("请求方法：GET 或 POST")
            form.addRow("请求方法", self.http_method)

            # 请求体（仅 POST 显示）
            self._http_body_widget = QWidget()
            body_lay = QVBoxLayout(self._http_body_widget)
            body_lay.setContentsMargins(0, 0, 0, 0)
            self.http_body = QPlainTextEdit()
            self.http_body.setPlaceholderText("POST 请求体（可选，支持 $变量名 引用）")
            self.http_body.setToolTip("POST 时随请求发送的内容；GET 忽略。支持 $变量名 引用")
            self.http_body.setMaximumHeight(80)
            body_lay.addWidget(self.http_body)
            form.addRow("请求体", self._http_body_widget)

            self.http_headers = QPlainTextEdit()
            self.http_headers.setPlaceholderText("每行一条，如：\nAuthorization: Bearer xxx\nContent-Type: application/json")
            self.http_headers.setToolTip("自定义请求头，每行一条「Name: Value」；支持 $变量名 引用。\n"
                                         "User-Agent 与 Cookie 有单独字段，若在下方已填则优先使用下方值")
            self.http_headers.setMaximumHeight(80)
            form.addRow("请求头", self.http_headers)

            self.http_cookie = QLineEdit()
            self.http_cookie.setPlaceholderText("如 sessionid=abc123; token=xyz（可选）")
            self.http_cookie.setToolTip("Cookie 字符串，直接作为 Cookie 请求头发送；支持 $变量名 引用")
            form.addRow("Cookie", self.http_cookie)

            self.http_result_type = QComboBox()
            self.http_result_type.addItem("文本（Text）", "text")
            self.http_result_type.addItem("图片（Image）", "image")
            self.http_result_type.setToolTip("结果类型：\n"
                                             "· 文本：响应体按字符集解码为字符串，写入「文本内容」变量；\n"
                                             "· 图片：响应体保存为图片文件，把文件路径写入「文本内容」变量")
            form.addRow("结果类型", self.http_result_type)

            self.http_ua = QLineEdit()
            self.http_ua.setPlaceholderText(DEFAULT_USER_AGENT)
            self.http_ua.setToolTip("User-Agent，默认 Chrome 桌面版；支持 $变量名 引用")
            form.addRow("用户代理", self.http_ua)

            self.http_timeout = self._dspin(1, 300, " 秒")
            self.http_timeout.setToolTip("请求超时时间，默认 5 秒；超时视为步骤失败")
            form.addRow("超时时间", self.http_timeout)

            # 系统代理：复选框 + 代理地址（勾选才显示地址行）
            self.http_proxy_check = QCheckBox("使用系统代理")
            self.http_proxy_check.setToolTip("勾选后经下方代理地址发起请求（默认本机 Clash 127.0.0.1:7897）")
            form.addRow("", self.http_proxy_check)
            self._http_proxy_widget = QWidget()
            proxy_row = QHBoxLayout(self._http_proxy_widget)
            proxy_row.setContentsMargins(0, 0, 0, 0)
            self.http_proxy = QLineEdit()
            self.http_proxy.setPlaceholderText("127.0.0.1:7897")
            self.http_proxy.setToolTip("代理地址 host:port，http 与 https 均走该代理")
            proxy_row.addWidget(self.http_proxy)
            form.addRow("代理地址", self._http_proxy_widget)

            # 4 个结果变量（都可选，按需勾选）
            self.http_status_var = self._var_combo("（不保存状态码）")
            self.http_status_var.setToolTip("HTTP 状态码（整数，含 4xx/5xx）写入该变量")
            form.addRow("状态码变量", self.http_status_var)
            self.http_headers_var = self._var_combo("（不保存响应头）")
            self.http_headers_var.setToolTip("响应头（dict）写入该变量")
            form.addRow("响应头变量", self.http_headers_var)
            self.http_cookie_var = self._var_combo("（不保存响应 Cookie）")
            self.http_cookie_var.setToolTip("响应 Cookie（dict，从 Set-Cookie 解析）写入该变量")
            form.addRow("响应 Cookie 变量", self.http_cookie_var)
            self.http_text_var = self._var_combo("（不保存文本内容）")
            self.http_text_var.setToolTip("文本内容（文本类型）或图片保存路径（图片类型）写入该变量")
            form.addRow("文本内容变量", self.http_text_var)
            self._var_combo_hint(form)

            hint = QLabel("发起一次 HTTP 请求并把结果写入变量：状态码（整数）、响应头（dict）、"
                          "响应 Cookie（dict）、\n文本内容/图片路径。"
                          "4xx/5xx 状态码不算失败（可按状态码变量分支）；仅网络错误/超时算失败。\n"
                          "网址、请求头、请求体、Cookie、用户代理均支持 $变量名 引用。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

            self.http_method.currentIndexChanged.connect(self._sync_http_rows)
            self.http_proxy_check.toggled.connect(self._http_proxy_widget.setVisible)

        elif t == "deepseek":
            self.ds_model = QComboBox()
            self.ds_model.setEditable(True)
            self.ds_model.addItem("deepseek-v4-flash", "deepseek-v4-flash")
            self.ds_model.addItem("deepseek-v4-pro", "deepseek-v4-pro")
            self.ds_model.setToolTip("模型名：可下拉选择 deepseek-v4-flash / deepseek-v4-pro，"
                                     "也可直接输入其它模型名")
            form.addRow("模型", self.ds_model)

            self.ds_api_key = QLineEdit()
            self.ds_api_key.setEchoMode(QLineEdit.Password)
            self.ds_api_key.setPlaceholderText("留空则读取环境变量 DEEPSEEK_API_KEY")
            self.ds_api_key.setToolTip("DeepSeek API Key；留空时运行时自动读取环境变量 "
                                       "DEEPSEEK_API_KEY")
            form.addRow("API Key", self.ds_api_key)

            self.ds_system = QPlainTextEdit()
            self.ds_system.setPlaceholderText("You are a helpful assistant")
            self.ds_system.setToolTip("角色设定（system 消息），定义助手身份/语气/任务；"
                                      "支持 $变量名 引用")
            self.ds_system.setMaximumHeight(60)
            form.addRow("角色设定", self.ds_system)

            self.ds_question = QPlainTextEdit()
            self.ds_question.setPlaceholderText("要问 DeepSeek 的内容（必填，支持 $变量名 引用）")
            self.ds_question.setToolTip("提问内容（user 消息）；支持 $变量名 引用")
            self.ds_question.setMaximumHeight(90)
            form.addRow("提问内容", self.ds_question)

            self.ds_thinking = QCheckBox("思考模式（thinking + reasoning_effort=high）")
            self.ds_thinking.setToolTip("勾选后开启思考模式：请求下发 thinking.enabled 与 "
                                        "reasoning_effort=high；\n思考过程会打印到日志，"
                                        "最终回答仍写入结果变量")
            form.addRow("", self.ds_thinking)

            self.ds_stream = QCheckBox("流式输出（默认关闭）")
            self.ds_stream.setToolTip("勾选后以 SSE 流式接收（内容拼接后仍写入结果变量）")
            form.addRow("", self.ds_stream)

            self.ds_timeout = self._dspin(1, 600, " 秒")
            self.ds_timeout.setToolTip("请求超时时间，默认 60 秒（推理/思考模型较慢）")
            form.addRow("超时时间", self.ds_timeout)

            self.ds_proxy_check = QCheckBox("使用系统代理")
            self.ds_proxy_check.setToolTip("勾选后经下方代理地址发起请求（默认本机 Clash 127.0.0.1:7897）")
            form.addRow("", self.ds_proxy_check)
            self._ds_proxy_widget = QWidget()
            proxy_row = QHBoxLayout(self._ds_proxy_widget)
            proxy_row.setContentsMargins(0, 0, 0, 0)
            self.ds_proxy = QLineEdit()
            self.ds_proxy.setPlaceholderText("127.0.0.1:7897")
            self.ds_proxy.setToolTip("代理地址 host:port")
            proxy_row.addWidget(self.ds_proxy)
            form.addRow("代理地址", self._ds_proxy_widget)

            self.ds_result_var = self._var_combo("（选择结果变量）")
            self.ds_result_var.setToolTip("DeepSeek 的最终回答写入该变量")
            form.addRow("结果变量", self.ds_result_var)
            self._var_combo_hint(form)

            hint = QLabel("调用 DeepSeek API（OpenAI 兼容）做一次对话，把最终回答写入结果变量。\n"
                          "模型可下拉选 deepseek-v4-flash / deepseek-v4-pro，也可直接输入其它模型名。\n"
                          "提问内容与角色设定支持 $变量名 引用；开启思考模式后思考过程会打印到日志。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

            self.ds_proxy_check.toggled.connect(self._ds_proxy_widget.setVisible)

        elif t == "script":
            self.sc_type = QComboBox()
            self.sc_type.addItem("CMD 命令", "cmd")
            self.sc_type.addItem("BAT 批处理", "bat")
            self.sc_type.addItem("PowerShell 脚本", "powershell")
            self.sc_type.addItem("Python 脚本", "python")
            self.sc_type.setToolTip("脚本解释器：CMD / BAT 走 cmd.exe，PowerShell 走 powershell.exe，"
                                    "Python 走 PATH 中的 python/py")
            form.addRow("脚本类型", self.sc_type)

            # 脚本来源：文本内容 / 脚本文件
            self.sc_src_text_radio = QRadioButton("文本内容")
            self.sc_src_file_radio = QRadioButton("脚本文件")
            self.sc_src_text_radio.setChecked(True)
            src_row = QHBoxLayout()
            src_row.addWidget(self.sc_src_text_radio)
            src_row.addWidget(self.sc_src_file_radio)
            src_row.addStretch(1)
            form.addRow("脚本来源", src_row)

            # 文本内容（source=text）
            self._sc_content_widget = QWidget()
            content_lay = QVBoxLayout(self._sc_content_widget)
            content_lay.setContentsMargins(0, 0, 0, 0)
            self.sc_content = QPlainTextEdit()
            code_font = QFont("Consolas")
            code_font.setPointSize(10)
            self.sc_content.setFont(code_font)
            self.sc_content.setPlaceholderText(
                "把 CMD / BAT / PowerShell / Python 命令粘贴到这里，例如：\n"
                "echo hello world\n"
                "dir\n"
                "（PowerShell / Python 请先在上方「脚本类型」里选择对应类型）")
            self.sc_content.setToolTip("脚本内容，支持 $变量名 引用")
            self.sc_content.setMinimumHeight(170)
            content_lay.addWidget(self.sc_content)
            form.addRow("脚本内容", self._sc_content_widget)

            # 脚本文件（source=file）
            self._sc_path_widget = QWidget()
            path_lay = QHBoxLayout(self._sc_path_widget)
            path_lay.setContentsMargins(0, 0, 0, 0)
            self.sc_path = QLineEdit()
            self.sc_path.setPlaceholderText("本地脚本文件的完整路径（如 D:\\scripts\\run.bat）")
            self.sc_path.setToolTip("脚本文件按原样执行，保留 %~dp0 与相对路径；支持 $变量名 引用")
            path_lay.addWidget(self.sc_path)
            self.sc_browse = QPushButton("浏览…")
            self.sc_browse.setCursor(Qt.PointingHandCursor)
            self.sc_browse.clicked.connect(self._pick_script_file)
            path_lay.addWidget(self.sc_browse)
            form.addRow("脚本文件", self._sc_path_widget)

            # 文件编码
            self.sc_encoding = QComboBox()
            for key, label in (("utf-8", "UTF-8 无 BOM（默认）"), ("utf-8-sig", "UTF-8 有 BOM"),
                               ("gb2312", "GB2312"), ("ascii", "ASCII")):
                self.sc_encoding.addItem(label, key)
            self.sc_encoding.setToolTip(
                "文本来源：脚本文件的写出编码；文件来源：脚本输出的解读编码。\n"
                "CMD/BAT 的 UTF-8 脚本执行前会自动 chcp 65001 避免中文乱码")
            form.addRow("文件编码", self.sc_encoding)

            # 运行方式
            self.sc_window = QComboBox()
            self.sc_window.addItem("隐藏窗口", "hidden")
            self.sc_window.addItem("完成后保留命令窗口", "keep")
            self.sc_window.setToolTip(
                "隐藏窗口：后台运行不弹窗口，输出写入结果变量；\n"
                "保留命令窗口：新开可见控制台，脚本跑完后打印输出并等待关闭")
            form.addRow("运行方式", self.sc_window)

            # 管理员权限
            self.sc_admin = QCheckBox("以管理员权限运行")
            self.sc_admin.setToolTip("勾选后触发 UAC 提权（需在弹出的用户账户控制窗口点「是」）")
            form.addRow("", self.sc_admin)

            # 超时
            self.sc_timeout = self._dspin(1, 3600, " 秒")
            self.sc_timeout.setToolTip(
                "脚本执行超时时间（隐藏窗口模式生效；保留命令窗口模式等待用户关闭窗口，不设超时）")
            form.addRow("超时时间", self.sc_timeout)

            # 结果变量
            self.sc_result_var = self._var_combo("（选择结果变量）")
            self.sc_result_var.setToolTip("脚本输出（stdout + stderr）写入该变量，供后续步骤使用")
            form.addRow("结果变量", self.sc_result_var)
            self._var_combo_hint(form)

            hint = QLabel("运行 CMD / BAT / PowerShell / Python 脚本，把输出写入结果变量。\n"
                          "· 脚本来源可二选一：直接粘贴脚本内容，或指定本地脚本文件完整路径（原样执行）；\n"
                          "· 文件编码默认 UTF-8 无 BOM，可切 UTF-8 有 BOM / GB2312 / ASCII；\n"
                          "· 运行方式可选隐藏窗口（后台运行）或完成后保留命令窗口（便于查看结果）；\n"
                          "· 勾选「以管理员权限运行」会触发 UAC 提权；脚本内容/路径支持 $变量名 引用。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

            self.sc_src_text_radio.toggled.connect(self._sync_script_rows)

        elif t == "notify":
            from ..notify_actor import NOTIFY_POSITIONS, NOTIFY_TYPES

            self.nt_type = QComboBox()
            for key, meta in NOTIFY_TYPES.items():
                self.nt_type.addItem(f"{meta['icon']} {meta['label']}", key)
            self.nt_type.setToolTip("消息类型：不同类型使用不同的主题颜色区分")
            form.addRow("消息类型", self.nt_type)

            self.nt_position = QComboBox()
            for key, label in NOTIFY_POSITIONS.items():
                self.nt_position.addItem(label, key)
            self.nt_position.setCurrentIndex(max(0, self.nt_position.findData("bottom")))
            self.nt_position.setToolTip("通知显示在屏幕的位置（默认屏幕中间底部）")
            form.addRow("显示位置", self.nt_position)

            self.nt_content = QPlainTextEdit()
            self.nt_content.setPlaceholderText("要显示的消息内容（支持 $变量名 引用）")
            self.nt_content.setToolTip("消息内容，支持 $变量名 动态输入；多行自动换行，高度随内容自适应")
            self.nt_content.setMaximumHeight(120)
            form.addRow("消息内容", self.nt_content)

            # 插入变量：选中流程变量即以 $变量名 插入消息内容光标处（变量只读下拉，不可手输）
            self.nt_var = QComboBox()
            var_names = self._flow_var_names()
            if var_names:
                self.nt_var.addItem("＋ 插入变量…", "")
                for name in var_names:
                    self.nt_var.addItem(name, name)
                self.nt_var.setToolTip("选中流程中声明的变量，自动以 $变量名 形式插入到消息内容的光标位置")
            else:
                self.nt_var.addItem("流程中暂无变量：先添加「变量」步骤声明", "")
                self.nt_var.setEnabled(False)
                self.nt_var.setToolTip("流程中还没有「变量」步骤；先在步骤列表添加「变量」步骤声明变量，"
                                       "即可在这里选中插入")
            self.nt_var.currentIndexChanged.connect(self._on_nt_insert_var)
            form.addRow("插入变量", self.nt_var)

            self.nt_duration = self._dspin(0, 3600, " 秒")
            self.nt_duration.setDecimals(1)
            self.nt_duration.setSpecialValueText("0 = 不自动消失")
            self.nt_duration.setToolTip("自动消失延迟，默认 2 秒；设为 0 表示不自动消失（仅手动关闭）")
            form.addRow("延迟时间", self.nt_duration)

            self.nt_width = self._spin(120, 1200, " px")
            self.nt_width.setToolTip("通知宽度（像素，默认 320），高度随内容自动调整")
            form.addRow("通知宽度", self.nt_width)

            hint = QLabel("在屏幕指定位置弹出一条消息通知，自动消失或手动关闭。\n"
                          "· 消息类型（信息/成功/警告/错误）用不同主题颜色区分；\n"
                          "· 显示位置默认屏幕中间底部，也可选屏幕中间、上部、四角与左右居中；\n"
                          "· 消息内容支持 $变量名 引用，可在下方「插入变量」下拉里直接选变量自动插入；\n"
                          "· 宽度可调，高度随内容自适应；每条通知都可点右上角 ✕ 手动关闭。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "speech":
            # 播报内容：可手动输入，也支持 $变量名 引用（下方「插入变量」可直接选变量）
            self.sp_content = QPlainTextEdit()
            self.sp_content.setPlaceholderText("要朗读的内容（支持 $变量名 引用）")
            self.sp_content.setToolTip("语音播报内容：直接输入文字，或引用流程变量（如 $result）一起朗读")
            self.sp_content.setMaximumHeight(120)
            form.addRow("播报内容", self.sp_content)

            # 插入变量：选中流程变量即以 $变量名 插入播报内容光标处（变量只读下拉，不可手输）
            self.sp_var = QComboBox()
            var_names = self._flow_var_names()
            if var_names:
                self.sp_var.addItem("＋ 插入变量…", "")
                for name in var_names:
                    self.sp_var.addItem(name, name)
                self.sp_var.setToolTip("选中流程中声明的变量，自动以 $变量名 形式插入到播报内容的光标位置")
            else:
                self.sp_var.addItem("流程中暂无变量：先添加「变量」步骤声明", "")
                self.sp_var.setEnabled(False)
                self.sp_var.setToolTip("流程中还没有「变量」步骤；先在步骤列表添加「变量」步骤声明变量，"
                                       "即可在这里选中插入")
            self.sp_var.currentIndexChanged.connect(self._on_sp_insert_var)
            form.addRow("插入变量", self.sp_var)

            self.sp_wait = QCheckBox("等待播报完成后再继续")
            self.sp_wait.setChecked(True)
            self.sp_wait.setToolTip("勾选：朗读完这一段才执行下一步骤（默认）；\n"
                                    "不勾选：后台排队播放，立即继续后续步骤")
            form.addRow("", self.sp_wait)

            hint = QLabel("用系统语音（pyttsx3 / Windows SAPI5）朗读文本。\n"
                          "· 播报内容可直接输入文字，或引用流程变量（如 $name），"
                          "「插入变量」下拉可一键插入；\n"
                          "· 系统自动优先使用中文语音（若无中文语音则用默认语音）；\n"
                          "· 勾选「等待播报完成后再继续」时，读完整段才执行下一步骤；\n"
                          "· 语音引擎不可用（未安装 pyttsx3 / 无语音设备）时本步骤判失败。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

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

        elif t == "dp_browser":
            self.dpb_var = QLineEdit()
            self.dpb_var.setPlaceholderText("如 browser（必填，后续元素/标签/截图等步骤都引用它）")
            form.addRow("浏览器变量", self.dpb_var)

            self.dpb_mode = QComboBox()
            for k, v in LAUNCH_MODES.items():
                self.dpb_mode.addItem(v, k)
            self.dpb_mode.setToolTip("前台显示 / 无头模式 / 后台静默 / 接管已打开的浏览器（需端口）")
            form.addRow("打开方式", self.dpb_mode)

            self.dpb_port = QLineEdit()
            self.dpb_port.setPlaceholderText("如 9333")
            self.dpb_port.setToolTip("接管端口：与浏览器 --remote-debugging-port 后面的数字一致")
            form.addRow("接管端口", self.dpb_port)
            self._dpb_port_row = (form, form.rowCount() - 1)

            self.dpb_url = QLineEdit()
            self.dpb_url.setPlaceholderText("可选：打开后访问的网址（支持 $变量名，不带协议自动补 https://）")
            form.addRow("访问网址", self.dpb_url)

            self.dpb_new_tab = QCheckBox("在新标签中打开网址")
            form.addRow("", self.dpb_new_tab)

            self.dpb_timeout = self._dspin(1, 300, " 秒")
            form.addRow("加载超时", self.dpb_timeout)

            hint = QLabel("「打开浏览器」启动/接管一个浏览器，并把浏览器对象保存到上方变量；\n"
                          "后续「元素操作 / 切换标签 / 监听 / 截图 / 上传」都从这个变量取浏览器，\n"
                          "串成一条可视化自动化链路。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self.dpb_mode.currentIndexChanged.connect(self._sync_dpb_rows)

        elif t == "dp_element":
            self.dpe_browser = self._browser_combo()
            self.dpe_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dpe_browser)

            self._build_locator_rows(form, "dpe")

            self.dpe_action = QComboBox()
            for k, v in DP_ELE_ACTIONS.items():
                self.dpe_action.addItem(v, k)
            self.dpe_action.setToolTip("找到元素后执行的操作（参照 DrissionPage 元素交互文档）")
            form.addRow("操作", self.dpe_action)

            self.dpe_input = QLineEdit()
            self.dpe_input.setPlaceholderText("输入内容 / 属性名 / 拖动偏移 x,y（支持 $变量名）")
            form.addRow("输入内容", self.dpe_input)
            self._dpe_input_row = (form, form.rowCount() - 1)

            self.dpe_files = QPlainTextEdit()
            self.dpe_files.setMaximumHeight(60)
            self.dpe_files.setPlaceholderText("文件路径，多个换行或用 | 分隔（支持 $变量名）")
            form.addRow("文件路径", self.dpe_files)
            self._dpe_files_row = (form, form.rowCount() - 1)

            self.dpe_result = self._var_combo("（选择变量）")
            self.dpe_result.setToolTip("获取文本/属性/新标签等有返回值的操作，结果写入该变量")
            form.addRow("结果变量", self.dpe_result)
            self._dpe_result_row = (form, form.rowCount() - 1)

            hint = QLabel("定位元素后执行操作；多个元素匹配时用「元素索引」指定位置。\n"
                          "有返回值的操作（获取文本/属性/新标签）会把结果写入「结果变量」。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self.dpe_locator.currentIndexChanged.connect(self._sync_dpe_rows)
            self.dpe_action.currentIndexChanged.connect(self._sync_dpe_rows)

        elif t == "dp_tab":
            self.dpt_browser = self._browser_combo()
            self.dpt_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dpt_browser)

            self.dpt_mode = QComboBox()
            for k, v in DP_TAB_MODES.items():
                self.dpt_mode.addItem(v, k)
            form.addRow("切换方式", self.dpt_mode)

            self.dpt_value = QLineEdit()
            self.dpt_value.setPlaceholderText("标签序号 / 标题 / 网址（支持 $变量名）")
            form.addRow("切换条件", self.dpt_value)
            self._dpt_value_row = (form, form.rowCount() - 1)

            self.dpt_url = QLineEdit()
            self.dpt_url.setPlaceholderText("新建标签时访问的网址（可选，支持 $变量名，不带协议自动补 https://）")
            form.addRow("访问网址", self.dpt_url)
            self._dpt_url_row = (form, form.rowCount() - 1)

            self.dpt_result = self._var_combo("（选择变量）")
            self.dpt_result.setToolTip("切换后标签信息 {tab_id, title, url} 写入该变量")
            form.addRow("结果变量", self.dpt_result)
            self._var_combo_hint(form)

            hint = QLabel("按序号 / 标题 / 网址切换当前标签，或新建一个标签并切换过去；\n"
                          "切换后的标签信息（tab_id、标题、网址）可选写入结果变量。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self.dpt_mode.currentIndexChanged.connect(self._sync_dpt_rows)

        elif t == "dp_listen":
            self.dpl_browser = self._browser_combo()
            self.dpl_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dpl_browser)

            self.dpl_action = QComboBox()
            for k, v in DP_LISTEN_ACTIONS.items():
                self.dpl_action.addItem(v, k)
            form.addRow("监听动作", self.dpl_action)

            self._dpl_rows = []
            self.dpl_targets = QPlainTextEdit()
            self.dpl_targets.setMaximumHeight(60)
            self.dpl_targets.setPlaceholderText("监听目标：URL 包含的文字，多个换行分隔；空=监听全部")
            form.addRow("监听目标", self.dpl_targets)
            self._dpl_rows.append(("targets", form, form.rowCount() - 1))

            self.dpl_timeout = self._dspin(0, 300, " 秒")
            self.dpl_timeout.setToolTip("等待数据包的时长")
            form.addRow("等待超时", self.dpl_timeout)
            self._dpl_rows.append(("timeout", form, form.rowCount() - 1))

            self.dpl_url_var = self._var_combo("（不保存）")
            self.dpl_url_var.setToolTip("数据包网址写入该变量")
            form.addRow("网址变量", self.dpl_url_var)
            self._dpl_rows.append(("url_var", form, form.rowCount() - 1))

            self.dpl_status_var = self._var_combo("（不保存）")
            self.dpl_status_var.setToolTip("响应状态码写入该变量")
            form.addRow("状态码变量", self.dpl_status_var)
            self._dpl_rows.append(("status_var", form, form.rowCount() - 1))

            self.dpl_body_var = self._var_combo("（不保存）")
            self.dpl_body_var.setToolTip("响应体（json 自动解析）写入该变量")
            form.addRow("响应体变量", self.dpl_body_var)
            self._dpl_rows.append(("body_var", form, form.rowCount() - 1))

            hint = QLabel("三步链路：启动监听（指定目标）→ 元素操作触发请求 → 等待捕获数据包，\n"
                          "把网址 / 状态码 / 响应体写入结果变量；结束后停止监听。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self.dpl_action.currentIndexChanged.connect(self._sync_dpl_rows)

        elif t == "dp_page_shot":
            self.dps_browser = self._browser_combo()
            self.dps_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dps_browser)

            self.dps_full = QCheckBox("整页截图（默认截当前视口）")
            form.addRow("", self.dps_full)

            self.dps_path = QLineEdit()
            self.dps_path.setPlaceholderText("保存目录（空=程序模板目录 jietu/，支持 $变量名）")
            form.addRow("保存目录", self.dps_path)

            self.dps_name = QLineEdit()
            self.dps_name.setPlaceholderText("文件名（空=自动时间戳，支持 $变量名）")
            form.addRow("文件名", self.dps_name)

            self.dps_result = self._var_combo("（选择变量）")
            self.dps_result.setToolTip("截图保存路径写入该变量（必填）")
            form.addRow("结果变量", self.dps_result)
            self._var_combo_hint(form)

            hint = QLabel("对当前标签页截图（整页或视口），保存路径写入结果变量。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        elif t == "dp_ele_shot":
            self.dpes_browser = self._browser_combo()
            self.dpes_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dpes_browser)

            self._build_locator_rows(form, "dpes")

            self.dpes_path = QLineEdit()
            self.dpes_path.setPlaceholderText("保存目录（空=程序模板目录 jietu/）")
            form.addRow("保存目录", self.dpes_path)

            self.dpes_name = QLineEdit()
            self.dpes_name.setPlaceholderText("文件名（空=自动时间戳）")
            form.addRow("文件名", self.dpes_name)

            self.dpes_result = self._var_combo("（选择变量）")
            self.dpes_result.setToolTip("元素截图保存路径写入该变量（必填）")
            form.addRow("结果变量", self.dpes_result)
            self._var_combo_hint(form)

            hint = QLabel("定位元素后对其截图，保存路径写入结果变量。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self.dpes_locator.currentIndexChanged.connect(
                lambda *_: self._sync_locator_rows("dpes"))

        elif t == "dp_upload":
            self.dpu_browser = self._browser_combo()
            self.dpu_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dpu_browser)

            self._build_locator_rows(form, "dpu")

            self.dpu_files = QPlainTextEdit()
            self.dpu_files.setMaximumHeight(60)
            self.dpu_files.setPlaceholderText("要上传的文件，多个换行或用 | 分隔（支持 $变量名）")
            form.addRow("上传文件", self.dpu_files)

            hint = QLabel("定位到上传按钮后点击触发文件选择框，并填入要上传的文件路径。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)
            self.dpu_locator.currentIndexChanged.connect(
                lambda *_: self._sync_locator_rows("dpu"))

        elif t == "dp_close_browser":
            self.dpc_browser = self._browser_combo()
            self.dpc_browser.setToolTip("由「打开浏览器」步骤产生的浏览器变量")
            form.addRow("浏览器变量", self.dpc_browser)

            hint = QLabel("关闭该浏览器变量对应的浏览器：\n"
                          "· 自启浏览器（前台/无头/后台）：直接退出，窗口一并关闭；\n"
                          "· 接管（attach）的浏览器：只断开连接，窗口保留可继续手动使用。")
            hint.setStyleSheet("color: #8a939c;")
            hint.setWordWrap(True)
            form.addRow("", hint)

        root.addLayout(form)

        if t in ("find", "web", "close_app"):
            # find/web 默认不勾（失败终止）；close_app 默认勾选（关闭失败不弹提示、继续跑）
            if t == "close_app":
                text = "运行失败后继续运行后续流程"
                tip = ("勾选（默认）：本步关闭失败时不弹出任何提示窗口，跳过本步继续执行后续步骤；\n"
                       "取消勾选：本步关闭失败时终止整个流程并弹出提示。")
            elif t == "find":
                text = "找不到目标时跳过本步，继续执行后续步骤（默认终止流程）"
                tip = ""
            else:
                text = "本步失败时跳过，继续执行后续步骤（默认终止流程）"
                tip = ""
            self.continue_box = QCheckBox(text)
            if t == "close_app":
                self.continue_box.setChecked(True)   # 默认勾选「失败后继续」
                self.continue_box.setToolTip(tip)
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
        cur = self.path_edit.text().strip() if getattr(self, "path_edit", None) else ""
        if cur:
            d = cur if os.path.isdir(cur) else os.path.dirname(cur)
            if d and os.path.isdir(d):
                start = d
        path, _ = QFileDialog.getOpenFileName(
            self, "选择应用", start,
            "所有文件 (*);;应用程序 (*.exe *.lnk *.bat *.cmd);;"
            "文档 (*.txt *.pdf *.docx *.xlsx *.csv);;图片 (*.png *.jpg *.bmp)")
        if path:
            self.path_edit.setText(path)

    def _browse_app_dir(self):
        from PySide6.QtWidgets import QFileDialog
        start = os.path.expanduser("~")
        cur = self.path_edit.text().strip() if getattr(self, "path_edit", None) else ""
        if cur:
            d = cur if os.path.isdir(cur) else os.path.dirname(cur)
            if d and os.path.isdir(d):
                start = d
        d = QFileDialog.getExistingDirectory(self, "选择要打开的文件夹", start)
        if d:
            self.path_edit.setText(d)

    def _sync_app_rows(self) -> None:
        """「打开应用」：目标进程行随「进程打开」勾选显隐，提示文案同步两种模式。"""
        if not getattr(self, "_app_proc_row", None):
            return
        use = self.app_use_proc.isChecked()
        form, row = self._app_proc_row
        form.setRowVisible(row, use)
        if use:
            self._app_hint.setText(
                "运行逻辑：目标进程已在运行 → 直接把它的窗口带到桌面最前（不重复启动）；\n"
                "未运行 → 用下方「应用路径」打开。目标进程与应用路径至少填一个。")
        else:
            self._app_hint.setText(
                "已关闭「进程打开」：不匹配目标进程，直接用「应用路径」\n"
                "打开对应的程序 / 文档 / 文件夹。")
        self.adjustSize()

    def _browse_app_process(self):
        """从进程列表选择：输入框显示完整描述（应用名—进程名「窗口标题」），
        实际按进程名匹配带出（单独存到 _app_process_name）；自动把该进程的 exe
        完整路径回填到「应用路径」，进程不在运行时可原样重新启动。"""
        dlg = ProcessPickerDialog(self, purpose="open")
        if dlg.exec() == QDialog.Accepted:
            item = dlg.selected_item()
            if item:
                name = item.get("name", "")
                exe = item.get("path", "") or ""
                self._app_process_name = name
                self.app_proc_edit.setText(dlg._display(item))
                if exe:
                    self.path_edit.setText(exe)
                self.app_proc_edit.setToolTip(
                    f"实际按进程名 {name} 匹配；未运行时启动：\n{exe or '（未取到完整路径）'}")

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
        launch = self.launch_combo.currentData()
        want = {
            "url": act == "open",
            "launch": act == "open",
            "attach_port": act == "open" and launch == "attach",
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

    def _sync_http_rows(self) -> None:
        """网络请求：请求体行只在 POST 时显示；代理地址行随「使用系统代理」显隐。"""
        if not getattr(self, "_http_body_widget", None):
            return
        is_post = self.http_method.currentData() == "post"
        self._http_body_widget.setVisible(is_post)
        self._http_proxy_widget.setVisible(self.http_proxy_check.isChecked())
        self.adjustSize()

    def _sync_script_rows(self) -> None:
        """执行脚本：脚本来源二选一，切换时联动「脚本内容」/「脚本文件」两行显隐。"""
        if not getattr(self, "_sc_content_widget", None):
            return
        is_text = self.sc_src_text_radio.isChecked()
        self._sc_content_widget.setVisible(is_text)
        self._sc_path_widget.setVisible(not is_text)
        self.adjustSize()

    # ---------- DrissionPage 步骤：浏览器变量 / 元素定位 ----------
    def _browser_var_names(self) -> list[str]:
        """当前流程中「打开浏览器」步骤产生的浏览器变量名（供后续 dp 步骤下拉引用）。"""
        try:
            tab = self.parent()
            flow = tab._selected_flow() if tab is not None else None
            if flow is None:
                return []
            names: list[str] = []
            for s in flow.steps:
                if s.type == "dp_browser":
                    n = (s.params.get("browser_var") or "").strip()
                    if n and n not in names:
                        names.append(n)
            return names
        except Exception:
            return []

    def _browser_combo(self) -> QComboBox:
        """可编辑的浏览器变量下拉：列流程中「打开浏览器」步骤产生的变量，也支持手动输入。"""
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItem("（选择浏览器变量）", "")
        for name in self._browser_var_names():
            combo.addItem(name, name)
        combo.lineEdit().setPlaceholderText("浏览器变量名")
        return combo

    def _build_locator_rows(self, form: QFormLayout, prefix: str) -> None:
        """为需要元素定位的 dp 步骤（元素操作/元素截图/上传文件）构建定位字段组。

        属性名、匹配模式两行随定位方式显隐（css/xpath/tag 不适用匹配模式）。
        控件与行号都挂在 self.{prefix}_* 上，显隐刷新用 _sync_locator_rows(prefix)。
        """
        locator = QComboBox()
        for k, v in DP_LOCATORS.items():
            locator.addItem(v, k)
        locator.setToolTip("元素定位方式（对照 DrissionPage 定位语法速查表）")
        form.addRow("定位方式", locator)

        attr = QLineEdit()
        attr.setPlaceholderText("属性名，如 name、href、value")
        form.addRow("属性名", attr)
        attr_row = (form, form.rowCount() - 1)

        match = QComboBox()
        for k, v in DP_MATCHES.items():
            match.addItem(v, k)
        match.setToolTip("= 精确 / : 模糊包含 / ^ 开头 / $ 结尾")
        form.addRow("匹配模式", match)
        match_row = (form, form.rowCount() - 1)

        value = QLineEdit()
        value.setPlaceholderText("定位值（支持 $变量名），如 kw、submit、搜索按钮")
        form.addRow("定位值", value)

        index = QSpinBox()
        index.setRange(-999, 999)
        index.setValue(1)
        index.setToolTip("多个元素匹配时的位置：1 起正数，负数从末尾数（-1=最后一个）")
        form.addRow("元素索引", index)

        timeout = self._dspin(0, 300, " 秒")
        timeout.setToolTip("查找元素超时；0 表示不等待")
        form.addRow("查找超时", timeout)

        setattr(self, f"{prefix}_locator", locator)
        setattr(self, f"{prefix}_attr", attr)
        setattr(self, f"{prefix}_match", match)
        setattr(self, f"{prefix}_value", value)
        setattr(self, f"{prefix}_index", index)
        setattr(self, f"{prefix}_timeout", timeout)
        setattr(self, f"{prefix}_attr_row", attr_row)
        setattr(self, f"{prefix}_match_row", match_row)

    def _sync_locator_rows(self, prefix: str) -> None:
        """按定位方式显隐「属性名 / 匹配模式」两行。"""
        locator = getattr(self, f"{prefix}_locator", None)
        if locator is None:
            return
        form, arow = getattr(self, f"{prefix}_attr_row")
        _, mrow = getattr(self, f"{prefix}_match_row")
        lt = locator.currentData()
        form.setRowVisible(arow, lt == "attr")
        form.setRowVisible(mrow, lt in ("id", "class", "attr", "text"))
        self.adjustSize()

    def _sync_dpb_rows(self) -> None:
        """「打开浏览器」：接管端口行只在「接管已打开的浏览器」时显示。"""
        if not getattr(self, "_dpb_port_row", None):
            return
        form, row = self._dpb_port_row
        form.setRowVisible(row, self.dpb_mode.currentData() == "attach")
        self.adjustSize()

    def _sync_dpe_rows(self) -> None:
        """「元素操作」：定位字段随定位方式显隐；输入/文件/结果行随操作显隐。"""
        self._sync_locator_rows("dpe")
        act = self.dpe_action.currentData()
        input_acts = {"input", "input_enter", "set_value", "select_text",
                      "select_value", "select_index", "get_attr", "drag"}
        file_acts = {"to_upload", "to_download"}
        result_acts = {"get_text", "get_attr", "for_new_tab"}
        for key, row, visible in (
            ("_dpe_input_row", None, act in input_acts),
            ("_dpe_files_row", None, act in file_acts),
            ("_dpe_result_row", None, act in result_acts),
        ):
            entry = getattr(self, key, None)
            if entry:
                entry[0].setRowVisible(entry[1], visible)
        self.adjustSize()

    def _sync_dpt_rows(self) -> None:
        """「切换标签」：按序号/标题/网址需要条件，新建标签需要网址。"""
        if not getattr(self, "_dpt_value_row", None):
            return
        mode = self.dpt_mode.currentData()
        form, vrow = self._dpt_value_row
        form.setRowVisible(vrow, mode != "new")
        form, urow = self._dpt_url_row
        form.setRowVisible(urow, mode == "new")
        self.adjustSize()

    def _sync_dpl_rows(self) -> None:
        """「监听网络数据」：目标行在启动时显示，超时/结果变量在等待时显示。"""
        if not getattr(self, "_dpl_rows", None):
            return
        act = self.dpl_action.currentData()
        want = {"targets": act == "start", "timeout": act == "wait",
                "url_var": act == "wait", "status_var": act == "wait",
                "body_var": act == "wait"}
        for key, form, row in self._dpl_rows:
            form.setRowVisible(row, want.get(key, False))
        self.adjustSize()

    def _on_nt_insert_var(self, index: int) -> None:
        """消息通知「插入变量」：把选中的变量以 $变量名 插入消息内容光标处，然后复位下拉。"""
        if not getattr(self, "nt_var", None) or not getattr(self, "nt_content", None):
            return
        name = self.nt_var.currentData()
        if not name:
            return
        cur = self.nt_content.textCursor()
        cur.insertText(f"${name}")          # 光标自动移到插入文本之后，便于连续输入
        self.nt_content.setTextCursor(cur)
        self.nt_var.blockSignals(True)
        self.nt_var.setCurrentIndex(0)      # 复位到「＋ 插入变量…」占位项
        self.nt_var.blockSignals(False)
        self.nt_content.setFocus()

    def _on_sp_insert_var(self, index: int) -> None:
        """语音播报「插入变量」：把选中的变量以 $变量名 插入播报内容光标处，然后复位下拉。"""
        if not getattr(self, "sp_var", None) or not getattr(self, "sp_content", None):
            return
        name = self.sp_var.currentData()
        if not name:
            return
        cur = self.sp_content.textCursor()
        cur.insertText(f"${name}")          # 光标自动移到插入文本之后，便于连续输入
        self.sp_content.setTextCursor(cur)
        self.sp_var.blockSignals(True)
        self.sp_var.setCurrentIndex(0)      # 复位到「＋ 插入变量…」占位项
        self.sp_var.blockSignals(False)
        self.sp_content.setFocus()

    def _pick_script_file(self):
        """浏览选择本地脚本文件；按扩展名自动同步脚本类型。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择脚本文件", os.path.expanduser("~"),
            "脚本文件 (*.bat *.cmd *.ps1 *.py);;所有文件 (*)")
        if not path:
            return
        self.sc_path.setText(path)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".ps1":
            self.sc_type.setCurrentIndex(max(0, self.sc_type.findData("powershell")))
        elif ext == ".py":
            self.sc_type.setCurrentIndex(max(0, self.sc_type.findData("python")))
        elif ext in (".bat", ".cmd"):
            self.sc_type.setCurrentIndex(
                max(0, self.sc_type.findData("bat" if ext == ".bat" else "cmd")))

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
            self.show_type_check.setChecked(bool(p.get("show_type")))
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
        elif t == "color_pick":
            fmt = (p.get("format") or "").strip()
            if fmt == "rgb":
                self.cp_fmt_rgb.setChecked(True)
            else:
                self.cp_fmt_hex.setChecked(True)
            self._set_pick_color(p.get("color") or "")
            self._refresh_color_ui()
            self._set_combo_value(self.cp_variable, p.get("variable", "") or "")
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
            target = (p.get("target") or "").strip()
            process = (p.get("process") or "").strip()
            self._app_process_name = process or target
            self.app_use_proc.setChecked(bool(p.get("use_process", True)))
            self.app_proc_edit.setText(target or process or "")
            self.path_edit.setText(p.get("path", "") or "")
            self.wait_sec.setValue(float(p.get("wait_sec", 2)))
            self._sync_app_rows()
        elif t == "close_app":
            target = p.get("target", "") or ""
            self._close_app_name = (p.get("process") or "").strip() or target
            self.target_edit.setText(target or self._close_app_name)
            self.wait_sec.setValue(float(p.get("wait_sec", 0.5)))
            self.continue_box.setChecked(step.continue_on_fail)
        elif t == "web":
            self.web_action.setCurrentIndex(max(0, self.web_action.findData(p.get("action"))))
            self.url_edit.setText(p.get("url", "") or "")
            self.launch_combo.setCurrentIndex(
                max(0, self.launch_combo.findData(p.get("launch_mode"))))
            self.attach_port_edit.setText(str(p.get("attach_port", "") or ""))
            self.tab_target_combo.setCurrentIndex(
                max(0, self.tab_target_combo.findData(p.get("tab_target"))))
            self.load_timeout.setValue(float(p.get("load_timeout_sec", 20)))
            self.wait_after.setValue(float(p.get("wait_after_sec", 0)))
            self.tab_scope_combo.setCurrentIndex(
                max(0, self.tab_scope_combo.findData(p.get("tab_scope"))))
            self.match_edit.setText(p.get("match_text", "") or "")
            self.continue_box.setChecked(step.continue_on_fail)
            self._sync_web_rows()
        elif t == "http_request":
            self.http_url.setText(p.get("url", "") or "")
            self.http_method.setCurrentIndex(
                max(0, self.http_method.findData((p.get("method") or "get").lower())))
            self.http_body.setPlainText(p.get("body", "") or "")
            self.http_headers.setPlainText(p.get("headers", "") or "")
            self.http_cookie.setText(p.get("cookie", "") or "")
            self.http_result_type.setCurrentIndex(
                max(0, self.http_result_type.findData((p.get("result_type") or "text").lower())))
            self.http_ua.setText(p.get("user_agent", "") or "")
            self.http_timeout.setValue(float(p.get("timeout", 5) or 5))
            self.http_proxy_check.setChecked(bool(p.get("use_proxy", True)))
            self.http_proxy.setText(p.get("proxy", "127.0.0.1:7897") or "")
            self._set_combo_value(self.http_status_var, p.get("status_var", "") or "")
            self._set_combo_value(self.http_headers_var, p.get("headers_var", "") or "")
            self._set_combo_value(self.http_cookie_var, p.get("cookie_var", "") or "")
            self._set_combo_value(self.http_text_var, p.get("text_var", "") or "")
            self._sync_http_rows()
        elif t == "deepseek":
            self._set_combo_value(self.ds_model, p.get("model", "deepseek-v4-flash") or "")
            self.ds_api_key.setText(p.get("api_key", "") or "")
            self.ds_system.setPlainText(p.get("system", "You are a helpful assistant") or "")
            self.ds_question.setPlainText(p.get("question", "") or "")
            self.ds_thinking.setChecked(bool(p.get("thinking")))
            self.ds_stream.setChecked(bool(p.get("stream")))
            self.ds_timeout.setValue(float(p.get("timeout", 60) or 60))
            self.ds_proxy_check.setChecked(bool(p.get("use_proxy", True)))
            self.ds_proxy.setText(p.get("proxy", "127.0.0.1:7897") or "")
            self._set_combo_value(self.ds_result_var, p.get("result_var", "") or "")
            self._ds_proxy_widget.setVisible(self.ds_proxy_check.isChecked())
        elif t == "script":
            self.sc_type.setCurrentIndex(
                max(0, self.sc_type.findData((p.get("script_type") or "cmd"))))
            if (p.get("source") or "text") == "file":
                self.sc_src_file_radio.setChecked(True)
            else:
                self.sc_src_text_radio.setChecked(True)
            self.sc_content.setPlainText(p.get("content", "") or "")
            self.sc_path.setText(p.get("path", "") or "")
            self.sc_encoding.setCurrentIndex(
                max(0, self.sc_encoding.findData((p.get("encoding") or "utf-8"))))
            self.sc_window.setCurrentIndex(
                max(0, self.sc_window.findData((p.get("window_mode") or "hidden"))))
            self.sc_admin.setChecked(bool(p.get("admin")))
            self.sc_timeout.setValue(float(p.get("timeout", 120) or 120))
            self._set_combo_value(self.sc_result_var, p.get("result_var", "") or "")
            self._sync_script_rows()
        elif t == "notify":
            self.nt_type.setCurrentIndex(
                max(0, self.nt_type.findData((p.get("msg_type") or "info"))))
            self.nt_position.setCurrentIndex(
                max(0, self.nt_position.findData((p.get("position") or "bottom"))))
            self.nt_content.setPlainText(p.get("content", "") or "")
            self.nt_duration.setValue(float(p.get("duration", 2) or 2))
            self.nt_width.setValue(int(p.get("width", 320) or 320))
        elif t == "speech":
            self.sp_content.setPlainText(p.get("content", "") or "")
            self.sp_wait.setChecked(bool(p.get("wait", True)))
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
        elif t == "dp_browser":
            self.dpb_var.setText(p.get("browser_var", "") or "")
            self.dpb_mode.setCurrentIndex(max(0, self.dpb_mode.findData(p.get("launch_mode", "front"))))
            self.dpb_port.setText(str(p.get("attach_port", "") or ""))
            self.dpb_url.setText(p.get("url", "") or "")
            self.dpb_new_tab.setChecked(bool(p.get("new_tab")))
            self.dpb_timeout.setValue(float(p.get("load_timeout_sec", 20) or 20))
            self._sync_dpb_rows()
        elif t == "dp_element":
            self._set_combo_value(self.dpe_browser, p.get("browser_var", "") or "")
            self.dpe_locator.setCurrentIndex(max(0, self.dpe_locator.findData(p.get("locator_type", "id"))))
            self.dpe_attr.setText(p.get("attr_name", "") or "")
            self.dpe_match.setCurrentIndex(max(0, self.dpe_match.findData(p.get("match", "="))))
            self.dpe_value.setText(p.get("locator_value", "") or "")
            self.dpe_index.setValue(int(p.get("index", 1) or 1))
            self.dpe_action.setCurrentIndex(max(0, self.dpe_action.findData(p.get("action", "click"))))
            self.dpe_input.setText(p.get("input_value", "") or "")
            self.dpe_files.setPlainText(p.get("file_paths", "") or "")
            self.dpe_timeout.setValue(float(p.get("timeout", 10) or 10))
            self._set_combo_value(self.dpe_result, p.get("result_var", "") or "")
            self._sync_dpe_rows()
        elif t == "dp_tab":
            self._set_combo_value(self.dpt_browser, p.get("browser_var", "") or "")
            self.dpt_mode.setCurrentIndex(max(0, self.dpt_mode.findData(p.get("switch_mode", "index"))))
            self.dpt_value.setText(p.get("value", "") or "")
            self.dpt_url.setText(p.get("url", "") or "")
            self._set_combo_value(self.dpt_result, p.get("result_var", "") or "")
            self._sync_dpt_rows()
        elif t == "dp_listen":
            self._set_combo_value(self.dpl_browser, p.get("browser_var", "") or "")
            self.dpl_action.setCurrentIndex(max(0, self.dpl_action.findData(p.get("action", "start"))))
            self.dpl_targets.setPlainText(p.get("targets", "") or "")
            self.dpl_timeout.setValue(float(p.get("timeout", 10) or 10))
            self._set_combo_value(self.dpl_url_var, p.get("url_var", "") or "")
            self._set_combo_value(self.dpl_status_var, p.get("status_var", "") or "")
            self._set_combo_value(self.dpl_body_var, p.get("body_var", "") or "")
            self._sync_dpl_rows()
        elif t == "dp_page_shot":
            self._set_combo_value(self.dps_browser, p.get("browser_var", "") or "")
            self.dps_full.setChecked(bool(p.get("full_page")))
            self.dps_path.setText(p.get("path", "") or "")
            self.dps_name.setText(p.get("name", "") or "")
            self._set_combo_value(self.dps_result, p.get("result_var", "") or "")
        elif t == "dp_ele_shot":
            self._set_combo_value(self.dpes_browser, p.get("browser_var", "") or "")
            self.dpes_locator.setCurrentIndex(max(0, self.dpes_locator.findData(p.get("locator_type", "id"))))
            self.dpes_attr.setText(p.get("attr_name", "") or "")
            self.dpes_match.setCurrentIndex(max(0, self.dpes_match.findData(p.get("match", "="))))
            self.dpes_value.setText(p.get("locator_value", "") or "")
            self.dpes_index.setValue(int(p.get("index", 1) or 1))
            self.dpes_timeout.setValue(float(p.get("timeout", 10) or 10))
            self.dpes_path.setText(p.get("path", "") or "")
            self.dpes_name.setText(p.get("name", "") or "")
            self._set_combo_value(self.dpes_result, p.get("result_var", "") or "")
            self._sync_locator_rows("dpes")
        elif t == "dp_upload":
            self._set_combo_value(self.dpu_browser, p.get("browser_var", "") or "")
            self.dpu_locator.setCurrentIndex(max(0, self.dpu_locator.findData(p.get("locator_type", "id"))))
            self.dpu_attr.setText(p.get("attr_name", "") or "")
            self.dpu_match.setCurrentIndex(max(0, self.dpu_match.findData(p.get("match", "="))))
            self.dpu_value.setText(p.get("locator_value", "") or "")
            self.dpu_index.setValue(int(p.get("index", 1) or 1))
            self.dpu_timeout.setValue(float(p.get("timeout", 10) or 10))
            self.dpu_files.setPlainText(p.get("file_paths", "") or "")
            self._sync_locator_rows("dpu")
        elif t == "dp_close_browser":
            self._set_combo_value(self.dpc_browser, p.get("browser_var", "") or "")

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
                "show_type": self.show_type_check.isChecked(),
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
        elif t == "color_pick":
            step.params.update({
                "color": self._color_text(),
                "format": "rgb" if self.cp_fmt_rgb.isChecked() else "hex",
                "variable": self._combo_value(self.cp_variable),
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
            text = self.app_proc_edit.text().strip()
            use = self.app_use_proc.isChecked()
            # 保存：target=显示文本（列表选择回来的完整描述/手填内容），
            # process=进程名（运行时先匹配进程带出，未运行才按 path 启动）；
            # 未勾选「进程打开」时忽略并清空进程字段（旧配置残留兜底由 use_process=False 执行层拦截）
            process = (getattr(self, "_app_process_name", "") or text) if use else ""
            step.params.update({
                "path": self.path_edit.text().strip(),
                "target": (text if text != process else process) if use else "",
                "process": process,
                "use_process": use,
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
            step.continue_on_fail = self.continue_box.isChecked()
        elif t == "web":
            step.params.update({
                "action": self.web_action.currentData(),
                "url": self.url_edit.text().strip(),
                "launch_mode": self.launch_combo.currentData(),
                "attach_port": self.attach_port_edit.text().strip(),
                "tab_target": self.tab_target_combo.currentData(),
                "load_timeout_sec": self.load_timeout.value(),
                "wait_after_sec": self.wait_after.value(),
                "tab_scope": self.tab_scope_combo.currentData(),
                "match_text": self.match_edit.text().strip(),
            })
            step.continue_on_fail = self.continue_box.isChecked()
        elif t == "http_request":
            step.params.update({
                "url": self.http_url.text().strip(),
                "method": self.http_method.currentData(),
                "body": self.http_body.toPlainText(),
                "headers": self.http_headers.toPlainText(),
                "cookie": self.http_cookie.text().strip(),
                "result_type": self.http_result_type.currentData(),
                "user_agent": self.http_ua.text(),
                "timeout": round(self.http_timeout.value(), 1),
                "use_proxy": self.http_proxy_check.isChecked(),
                "proxy": self.http_proxy.text().strip() or "127.0.0.1:7897",
                "status_var": self._combo_value(self.http_status_var),
                "headers_var": self._combo_value(self.http_headers_var),
                "cookie_var": self._combo_value(self.http_cookie_var),
                "text_var": self._combo_value(self.http_text_var),
            })
        elif t == "deepseek":
            step.params.update({
                "model": self._combo_value(self.ds_model) or "deepseek-v4-flash",
                "api_key": self.ds_api_key.text().strip(),
                "system": self.ds_system.toPlainText(),
                "thinking": self.ds_thinking.isChecked(),
                "stream": self.ds_stream.isChecked(),
                "question": self.ds_question.toPlainText().strip(),
                "result_var": self._combo_value(self.ds_result_var),
                "timeout": round(self.ds_timeout.value(), 1),
                "use_proxy": self.ds_proxy_check.isChecked(),
                "proxy": self.ds_proxy.text().strip() or "127.0.0.1:7897",
            })
        elif t == "script":
            step.params.update({
                "script_type": self.sc_type.currentData(),
                "source": "file" if self.sc_src_file_radio.isChecked() else "text",
                "content": self.sc_content.toPlainText(),
                "path": self.sc_path.text().strip(),
                "encoding": self.sc_encoding.currentData(),
                "window_mode": self.sc_window.currentData(),
                "admin": self.sc_admin.isChecked(),
                "timeout": round(self.sc_timeout.value(), 1),
                "result_var": self._combo_value(self.sc_result_var),
            })
        elif t == "notify":
            step.params.update({
                "msg_type": self.nt_type.currentData(),
                "position": self.nt_position.currentData(),
                "content": self.nt_content.toPlainText().strip(),
                "duration": round(self.nt_duration.value(), 1),
                "width": self.nt_width.value(),
            })
        elif t == "speech":
            step.params.update({
                "content": self.sp_content.toPlainText().strip(),
                "wait": self.sp_wait.isChecked(),
            })
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
        elif t == "dp_browser":
            step.params.update({
                "browser_var": self.dpb_var.text().strip(),
                "launch_mode": self.dpb_mode.currentData(),
                "attach_port": self.dpb_port.text().strip(),
                "url": self.dpb_url.text().strip(),
                "new_tab": self.dpb_new_tab.isChecked(),
                "load_timeout_sec": round(self.dpb_timeout.value(), 1),
            })
        elif t == "dp_element":
            step.params.update({
                "browser_var": self._combo_value(self.dpe_browser),
                "locator_type": self.dpe_locator.currentData(),
                "attr_name": self.dpe_attr.text().strip(),
                "match": self.dpe_match.currentData(),
                "locator_value": self.dpe_value.text().strip(),
                "index": self.dpe_index.value(),
                "action": self.dpe_action.currentData(),
                "input_value": self.dpe_input.text(),
                "file_paths": self.dpe_files.toPlainText().strip(),
                "timeout": round(self.dpe_timeout.value(), 1),
                "result_var": self._combo_value(self.dpe_result),
            })
        elif t == "dp_tab":
            step.params.update({
                "browser_var": self._combo_value(self.dpt_browser),
                "switch_mode": self.dpt_mode.currentData(),
                "value": self.dpt_value.text().strip(),
                "url": self.dpt_url.text().strip(),
                "result_var": self._combo_value(self.dpt_result),
            })
        elif t == "dp_listen":
            step.params.update({
                "browser_var": self._combo_value(self.dpl_browser),
                "action": self.dpl_action.currentData(),
                "targets": self.dpl_targets.toPlainText().strip(),
                "timeout": round(self.dpl_timeout.value(), 1),
                "url_var": self._combo_value(self.dpl_url_var),
                "status_var": self._combo_value(self.dpl_status_var),
                "body_var": self._combo_value(self.dpl_body_var),
            })
        elif t == "dp_page_shot":
            step.params.update({
                "browser_var": self._combo_value(self.dps_browser),
                "full_page": self.dps_full.isChecked(),
                "path": self.dps_path.text().strip(),
                "name": self.dps_name.text().strip(),
                "result_var": self._combo_value(self.dps_result),
            })
        elif t == "dp_ele_shot":
            step.params.update({
                "browser_var": self._combo_value(self.dpes_browser),
                "locator_type": self.dpes_locator.currentData(),
                "attr_name": self.dpes_attr.text().strip(),
                "match": self.dpes_match.currentData(),
                "locator_value": self.dpes_value.text().strip(),
                "index": self.dpes_index.value(),
                "timeout": round(self.dpes_timeout.value(), 1),
                "path": self.dpes_path.text().strip(),
                "name": self.dpes_name.text().strip(),
                "result_var": self._combo_value(self.dpes_result),
            })
        elif t == "dp_upload":
            step.params.update({
                "browser_var": self._combo_value(self.dpu_browser),
                "locator_type": self.dpu_locator.currentData(),
                "attr_name": self.dpu_attr.text().strip(),
                "match": self.dpu_match.currentData(),
                "locator_value": self.dpu_value.text().strip(),
                "index": self.dpu_index.value(),
                "timeout": round(self.dpu_timeout.value(), 1),
                "file_paths": self.dpu_files.toPlainText().strip(),
            })
        elif t == "dp_close_browser":
            step.params.update({
                "browser_var": self._combo_value(self.dpc_browser),
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

    # ---------- 屏幕取色（color_pick） ----------
    def _request_color_pick(self) -> None:
        """点「屏幕取色…」：隐藏对话框，由外部隐藏主窗口并启动取色遮罩。"""
        self.hide()
        self.colorPickRequested.emit()

    def set_color(self, r: int, g: int, b: int) -> None:
        """取色遮罩回调：写入拾取颜色并按当前「颜色格式」刷新显示。"""
        self._pick_rgb = (int(r), int(g), int(b))
        self._refresh_color_ui()

    def finish_color_pick(self) -> None:
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    def _set_pick_color(self, text: str) -> None:
        """把配置里保存的颜色文本（#RRGGBB 或 255,0,0）解析回 RGB；解析失败视为未取色。"""
        text = (text or "").strip()
        rgb = None
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 6:
            try:
                rgb = tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                rgb = None
        else:
            parts = [s.strip() for s in text.split(",")]
            if len(parts) == 3:
                try:
                    rgb = tuple(int(x) for x in parts)
                except ValueError:
                    rgb = None
        self._pick_rgb = rgb if rgb is not None and all(0 <= c <= 255 for c in rgb) else None

    def _refresh_color_ui(self) -> None:
        """按当前格式刷新色块背景与颜色文本；未取色时恢复占位样式。"""
        if getattr(self, "_pick_rgb", None):
            r, g, b = self._pick_rgb
            self.cp_swatch.setStyleSheet(
                f"border: 1px solid #c9d1d9; border-radius: 6px; background: rgb({r},{g},{b});")
            self.cp_value.setText(self._color_text())
        else:
            self.cp_swatch.setStyleSheet(
                "border: 1px solid #c9d1d9; border-radius: 6px; background: #f7f9fb;")
            self.cp_value.clear()

    def _color_text(self) -> str:
        """取色结果按当前「颜色格式」转文本：#RRGGBB / 255,0,0；未取色返回空串。"""
        rgb = getattr(self, "_pick_rgb", None)
        if not rgb:
            return ""
        r, g, b = rgb
        if getattr(self, "cp_fmt_rgb", None) is not None and self.cp_fmt_rgb.isChecked():
            return f"{r},{g},{b}"
        return f"#{r:02X}{g:02X}{b:02X}"
