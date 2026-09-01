"""鼠标连点页。"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QRadioButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ..config import ClickerConfig
from .hotkey_edit import HotkeyEdit
from .widgets import StatusLabel, StopConditionGroup, set_variant


class ClickerTab(QWidget):
    changed = Signal()
    toggleRequested = Signal()
    captureAboutToStart = Signal()   # 取坐标前隐藏主窗口
    captureFinished = Signal()       # 取坐标结束恢复主窗口

    def __init__(self, cfg: ClickerConfig, parent=None):
        super().__init__(parent)
        self._loading = True
        self._build_ui()
        self.apply_config(cfg)
        self._loading = False

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        form_box = QGroupBox("点击设置")
        form = QFormLayout(form_box)

        self.button_combo = QComboBox()
        self.button_combo.addItem("鼠标左键", "left")
        self.button_combo.addItem("鼠标右键", "right")
        self.button_combo.addItem("鼠标中键", "middle")
        self.button_combo.setFixedWidth(200)
        form.addRow("鼠标按键", self.button_combo)

        self.type_combo = QComboBox()
        self.type_combo.addItem("单击", "single")
        self.type_combo.addItem("双击", "double")
        self.type_combo.setFixedWidth(200)
        form.addRow("点击方式", self.type_combo)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(20, 3_600_000)
        self.interval_spin.setValue(100)
        self.interval_spin.setSuffix(" 毫秒")
        self.interval_spin.setMinimumWidth(120)
        form.addRow("点击间隔", self.interval_spin)

        # 位置
        pos_row = QWidget()
        pos_lay = QHBoxLayout(pos_row)
        pos_lay.setContentsMargins(0, 0, 0, 0)
        self.follow_radio = QRadioButton("跟随当前鼠标位置")
        self.fixed_radio = QRadioButton("固定坐标  X")
        self.x_spin = QSpinBox()
        self.x_spin.setRange(-99999, 99999)
        self.y_spin = QSpinBox()
        self.y_spin.setRange(-99999, 99999)
        self.pick_btn = QPushButton("📍 屏幕点选坐标")
        self.pick_btn.setToolTip("隐藏本程序后，点击屏幕任意位置取坐标（Esc 取消）")
        set_variant(self.pick_btn, "primary")
        pos_lay.addWidget(self.follow_radio)
        pos_lay.addWidget(self.fixed_radio)
        pos_lay.addWidget(self.x_spin)
        pos_lay.addWidget(QLabel("Y"))
        pos_lay.addWidget(self.y_spin)
        pos_lay.addWidget(self.pick_btn)
        pos_lay.addStretch(1)
        self._pos_group = QButtonGroup(self)
        self._pos_group.addButton(self.follow_radio)
        self._pos_group.addButton(self.fixed_radio)
        form.addRow("点击位置", pos_row)

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

        # 信号
        self.button_combo.currentIndexChanged.connect(self._ui_changed)
        self.type_combo.currentIndexChanged.connect(self._ui_changed)
        self.interval_spin.valueChanged.connect(self._ui_changed)
        self.follow_radio.toggled.connect(self._ui_changed)
        self.x_spin.valueChanged.connect(self._ui_changed)
        self.y_spin.valueChanged.connect(self._ui_changed)
        self.pick_btn.clicked.connect(self._pick_position)
        self.stop_group.changed.connect(self._ui_changed)
        self.hotkey_edit.hotkeyChanged.connect(lambda _: self._ui_changed())
        self.toggle_btn.clicked.connect(self.toggleRequested)
        set_variant(self.toggle_btn, "success")

    def _ui_changed(self, *_) -> None:
        if not self._loading:
            self.changed.emit()

    # ---------- 数据 ----------
    def apply_config(self, cfg: ClickerConfig) -> None:
        self.button_combo.setCurrentIndex(max(0, self.button_combo.findData(cfg.mouse_button)))
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(cfg.click_type)))
        self.interval_spin.setValue(int(cfg.interval_ms))
        (self.fixed_radio if cfg.fixed_position else self.follow_radio).setChecked(True)
        self.x_spin.setValue(int(cfg.pos_x))
        self.y_spin.setValue(int(cfg.pos_y))
        self.stop_group.set_values(cfg.count, cfg.duration_sec)
        self.hotkey_edit.set_hotkey(cfg.hotkey)

    def snapshot(self) -> ClickerConfig:
        return ClickerConfig(
            mouse_button=self.button_combo.currentData(),
            click_type=self.type_combo.currentData(),
            interval_ms=self.interval_spin.value(),
            fixed_position=self.fixed_radio.isChecked(),
            pos_x=self.x_spin.value(),
            pos_y=self.y_spin.value(),
            count=self.stop_group.values()[0],
            duration_sec=self.stop_group.values()[1],
            hotkey=self.hotkey_edit.hotkey(),
        )

    def _pick_position(self) -> None:
        """屏幕点选坐标：主窗口先隐藏 -> 遮罩单击取点 -> 回写 X/Y。"""
        self.captureAboutToStart.emit()
        QTimer.singleShot(250, self._start_point_pick)

    def _start_point_pick(self) -> None:
        from ..capture_overlay import run_screen_capture

        def done(point=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if point:
                self.x_spin.setValue(int(point[0]))
                self.y_spin.setValue(int(point[1]))
            self.captureFinished.emit()

        try:
            run_screen_capture(on_point=lambda pt: done(pt),
                               on_cancelled=lambda: done())
        except Exception:
            done()

    # ---------- 运行状态 ----------
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
            self.status.set_running(f"已点击 {done} 次 · 用时 {elapsed:.1f} 秒")
