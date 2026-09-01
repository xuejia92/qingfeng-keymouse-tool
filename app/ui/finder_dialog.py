"""找图任务编辑对话框。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QVBoxLayout)

from ..config import FindTask, resolve_template_path
from .hotkey_edit import HotkeyEdit
from .widgets import StopConditionGroup


class FindTaskDialog(QDialog):
    # 点击"框选区域"：对话框已自隐藏，主窗口也应隐藏后再启动遮罩
    regionCaptureRequested = Signal()
    # 点击"重新截图选区"：同上，完成后经 set_template_image 回写新模板
    templateCaptureRequested = Signal()

    def __init__(self, task: FindTask, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑找图任务")
        self.setMinimumWidth(460)
        self._task = task
        self._region_capture_active = False
        self._image = ""
        self._image_path = ""
        self._build_ui()
        self._fill(task)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)

        top = QHBoxLayout()
        left_col = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(160, 120)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid #bbb; background: #f5f5f5;")
        left_col.addWidget(self.preview)
        self.repick_btn = QPushButton("📷 重新截图选区")
        self.repick_btn.setToolTip("重新在屏幕上框选模板图（确定后生效）")
        self.repick_btn.clicked.connect(self._request_capture)
        left_col.addWidget(self.repick_btn)
        top.addLayout(left_col)

        right = QFormLayout()
        self.name_edit = QLineEdit()
        right.addRow("任务名称", self.name_edit)
        self.image_label = QLabel()
        self.image_label.setStyleSheet("color: #888;")
        right.addRow("模板文件", self.image_label)
        self.click_combo = QComboBox()
        self.click_combo.addItem("单击", "single")
        self.click_combo.addItem("双击", "double")
        self.click_combo.addItem("右键", "right")
        right.addRow("命中后动作", self.click_combo)
        off_row = QHBoxLayout()
        self.offset_x = QSpinBox()
        self.offset_x.setRange(-9999, 9999)
        self.offset_y = QSpinBox()
        self.offset_y.setRange(-9999, 9999)
        off_row.addWidget(QLabel("X"))
        off_row.addWidget(self.offset_x)
        off_row.addWidget(QLabel("Y"))
        off_row.addWidget(self.offset_y)
        off_row.addStretch(1)
        right.addRow("点击偏移", off_row)
        top.addLayout(right, 1)
        root.addLayout(top)

        form.addRow(QLabel(""))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(50, 3_600_000)
        self.interval_spin.setSuffix(" 毫秒")
        form.addRow("命中后间隔", self.interval_spin)

        self.confidence_spin = QDoubleSpinBox()
        self.confidence_spin.setRange(0.50, 0.99)
        self.confidence_spin.setDecimals(2)
        self.confidence_spin.setSingleStep(0.01)
        form.addRow("匹配置信度", self.confidence_spin)

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.0, 86400.0)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setSpecialValueText("0 = 一直等到找到")
        form.addRow("搜索超时", self.timeout_spin)

        # 找图区域
        region_row = QHBoxLayout()
        self.region_edit = QLineEdit()
        self.region_edit.setReadOnly(True)
        self.region_edit.setMinimumWidth(170)
        self.region_edit.setAlignment(Qt.AlignCenter)
        self.pick_region_btn = QPushButton("框选区域…")
        self.clear_region_btn = QPushButton("恢复全屏")
        region_row.addWidget(self.region_edit, 1)
        region_row.addWidget(self.pick_region_btn)
        region_row.addWidget(self.clear_region_btn)
        form.addRow("找图区域", region_row)
        region_hint = QLabel("默认搜索整个虚拟桌面；框选后只在该区域内找图，更快且不易误点")
        region_hint.setStyleSheet("color: #888;")
        form.addRow("", region_hint)
        root.addLayout(form)

        self.stop_group = StopConditionGroup("停止条件（命中次数与时间任一满足即自动停止）")
        root.addWidget(self.stop_group)

        hk_row = QHBoxLayout()
        hk_row.addWidget(QLabel("启停热键"))
        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setMaximumWidth(220)
        hk_row.addWidget(self.hotkey_edit)
        hk_row.addStretch(1)
        root.addLayout(hk_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.pick_region_btn.clicked.connect(self.start_region_capture)
        self.clear_region_btn.clicked.connect(self._clear_region)

    def _clear_region(self) -> None:
        self._region = ""
        self._set_region_text(None)

    def _fill(self, t: FindTask) -> None:
        self.name_edit.setText(t.name)
        self._image = t.image or ""
        self._image_path = t.image_path or ""
        path = resolve_template_path(t.image, t.image_path)
        if path:
            pm = QPixmap(path)
            if not pm.isNull():
                self.preview.setPixmap(pm.scaled(self.preview.size(), Qt.KeepAspectRatio,
                                                 Qt.SmoothTransformation))
            self.image_label.setText(t.image)
            self.image_label.setToolTip(path)
        else:
            self.image_label.setText("（模板丢失，可重新截图）")
        self.click_combo.setCurrentIndex(max(0, self.click_combo.findData(t.click_type)))
        self.offset_x.setValue(int(t.offset_x))
        self.offset_y.setValue(int(t.offset_y))
        self.interval_spin.setValue(int(t.interval_ms))
        self.confidence_spin.setValue(float(t.confidence))
        self.timeout_spin.setValue(float(t.search_timeout_sec))
        self.stop_group.set_values(t.count, t.duration_sec)
        self.hotkey_edit.set_hotkey(t.hotkey)
        self._region = t.region or ""
        self._set_region_text(t.region_tuple())

    def _set_region_text(self, region: tuple[int, int, int, int] | None) -> None:
        if region:
            x, y, w, h = region
            self.region_edit.setText(f"{x}, {y}, {w} x {h}")
        else:
            self.region_edit.setText("全屏（整个虚拟桌面）")

    def set_region(self, rect: tuple[int, int, int, int]) -> None:
        """框选回调：写入并即时显示。"""
        x, y, w, h = rect
        self._region = f"{x},{y},{w},{h}"
        self._set_region_text((x, y, w, h))

    def region_value(self) -> str:
        return getattr(self, "_region", self._task.region or "")

    def start_region_capture(self) -> None:
        """点"框选区域"：隐藏对话框，由外部隐藏主窗口并启动遮罩。"""
        self._region_capture_active = True
        self.hide()
        self.regionCaptureRequested.emit()

    def finish_region_capture(self) -> None:
        if self._region_capture_active:
            self._region_capture_active = False
            self.show()
            self.raise_()
            self.activateWindow()

    # ---------- 重新截图选区 ----------
    def _request_capture(self) -> None:
        self.hide()
        self.templateCaptureRequested.emit()

    def set_template_image(self, filename: str, fullpath: str = "") -> None:
        """截图完成回调：预览新模板，点确定后才写入任务。"""
        self._image = filename
        if fullpath:
            self._image_path = fullpath
        path = resolve_template_path(filename, self._image_path)
        pm = QPixmap(path) if path else QPixmap()
        if not pm.isNull():
            self.preview.setPixmap(pm.scaled(self.preview.size(), Qt.KeepAspectRatio,
                                             Qt.SmoothTransformation))
            self.image_label.setText(filename)
            self.image_label.setToolTip(path or "")
        else:
            self.preview.setText("截图失败")

    def finish_template_capture(self) -> None:
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    def apply_to(self, t: FindTask) -> None:
        t.name = self.name_edit.text().strip() or t.name
        # 防呆：新模板为空时保留原模板
        new_image = getattr(self, "_image", "") or t.image
        new_path = getattr(self, "_image_path", "") or t.image_path
        t.image = new_image
        t.image_path = new_path
        t.click_type = self.click_combo.currentData()
        t.offset_x = self.offset_x.value()
        t.offset_y = self.offset_y.value()
        t.interval_ms = self.interval_spin.value()
        t.confidence = round(self.confidence_spin.value(), 2)
        t.search_timeout_sec = self.timeout_spin.value()
        t.count, t.duration_sec = self.stop_group.values()
        t.hotkey = self.hotkey_edit.hotkey()
        t.region = self.region_value()
