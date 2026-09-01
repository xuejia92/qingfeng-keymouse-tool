"""键盘连按页。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..config import PresserConfig
from .hotkey_edit import HotkeyEdit
from .widgets import StatusLabel, StopConditionGroup, set_variant


class PresserTab(QWidget):
    changed = Signal()
    toggleRequested = Signal()

    def __init__(self, cfg: PresserConfig, parent=None):
        super().__init__(parent)
        self._loading = True
        self._build_ui()
        self.apply_config(cfg)
        self._loading = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        form_box = QGroupBox("按键设置")
        form = QFormLayout(form_box)

        self.keys_edit = HotkeyEdit()
        self.keys_edit.setMaximumWidth(220)
        form.addRow("连按按键", self.keys_edit)
        hint = QLabel("支持单键（如 Space、F5）或组合键（如 Ctrl+C），点击输入框后按下即可录制")
        hint.setStyleSheet("color: #888;")
        form.addRow("", hint)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(20, 3_600_000)
        self.interval_spin.setValue(100)
        self.interval_spin.setSuffix(" 毫秒")
        self.interval_spin.setMinimumWidth(120)
        form.addRow("按下间隔", self.interval_spin)

        root.addWidget(form_box)

        self.stop_group = StopConditionGroup()
        root.addWidget(self.stop_group)

        hotkey_box = QGroupBox("热键与运行")
        hk_lay = QHBoxLayout(hotkey_box)
        hk_lay.addWidget(QLabel("启停热键"))
        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setMaximumWidth(220)
        hk_lay.addWidget(self.hotkey_edit)
        hk_lay.addSpacing(18)
        self.toggle_btn = QPushButton("启动")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setMinimumWidth(96)
        hk_lay.addWidget(self.toggle_btn)
        self.status = StatusLabel()
        hk_lay.addWidget(self.status, 1)
        root.addWidget(hotkey_box)
        root.addStretch(1)

        self.keys_edit.hotkeyChanged.connect(lambda _: self._ui_changed())
        self.interval_spin.valueChanged.connect(self._ui_changed)
        self.stop_group.changed.connect(self._ui_changed)
        self.hotkey_edit.hotkeyChanged.connect(lambda _: self._ui_changed())
        self.toggle_btn.clicked.connect(self.toggleRequested)
        set_variant(self.toggle_btn, "success")

    def _ui_changed(self, *_) -> None:
        if not self._loading:
            self.changed.emit()

    def apply_config(self, cfg: PresserConfig) -> None:
        self.keys_edit.set_hotkey(cfg.keys)
        self.interval_spin.setValue(int(cfg.interval_ms))
        self.stop_group.set_values(cfg.count, cfg.duration_sec)
        self.hotkey_edit.set_hotkey(cfg.hotkey)

    def snapshot(self) -> PresserConfig:
        return PresserConfig(
            keys=self.keys_edit.hotkey(),
            interval_ms=self.interval_spin.value(),
            count=self.stop_group.values()[0],
            duration_sec=self.stop_group.values()[1],
            hotkey=self.hotkey_edit.hotkey(),
        )

    def set_running(self, running: bool, note: str = "") -> None:
        self.toggle_btn.setChecked(running)
        self.toggle_btn.setText("停止" if running else "启动")
        set_variant(self.toggle_btn, "danger" if running else "success")
        if running:
            self.status.set_running()
        else:
            self.status.set_stopped(note)

    def set_progress(self, done: int, elapsed: float) -> None:
        if self.toggle_btn.isChecked():
            self.status.set_running(f"已按下 {done} 次 · 用时 {elapsed:.1f} 秒")
