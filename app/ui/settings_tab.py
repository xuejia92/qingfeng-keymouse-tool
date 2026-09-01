"""设置页：全局热键 + 配置目录。"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from ..config import APP_NAME, BASE_DIR, CONFIG_PATH, LOG_PATH, TEMPLATE_DIR
from .hotkey_edit import HotkeyEdit
from .widgets import set_variant


class SettingsTab(QWidget):
    changed = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._loading = True
        self._build_ui()
        self.hotkey_edit.set_hotkey(cfg.show_hide_hotkey)
        self.stop_edit.set_hotkey(cfg.stop_all_hotkey)
        self._loading = False
        self.hotkey_edit.hotkeyChanged.connect(self._ui_changed)
        self.stop_edit.hotkeyChanged.connect(self._ui_changed)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        hotkey_box = QGroupBox("全局热键")
        form = QFormLayout(hotkey_box)
        self.hotkey_edit = HotkeyEdit()
        self.hotkey_edit.setMaximumWidth(220)
        form.addRow("显示 / 隐藏主窗口（切换）", self.hotkey_edit)
        self.stop_edit = HotkeyEdit()
        self.stop_edit.setMaximumWidth(220)
        form.addRow("紧急停止全部任务", self.stop_edit)
        warn = QLabel("⚠ 任务可能在后台持续点击/按键，失控时请立刻按紧急停止热键，"
                      "或用鼠标右键托盘图标选择「全部停止」。")
        warn.setStyleSheet("color: #c0392b;")
        warn.setWordWrap(True)
        form.addRow("", warn)
        root.addWidget(hotkey_box)

        path_box = QGroupBox("文件位置（程序当前目录；目录不存在会自动创建）")
        pform = QFormLayout(path_box)
        cfg_row = QHBoxLayout()
        cfg_label = QLabel(CONFIG_PATH)
        cfg_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_cfg = QPushButton("打开配置目录")
        set_variant(open_cfg, "primary")
        open_cfg.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(BASE_DIR)))
        cfg_row.addWidget(cfg_label, 1)
        cfg_row.addWidget(open_cfg)
        pform.addRow("配置文件", cfg_row)
        tpl_row = QHBoxLayout()
        tpl_label = QLabel(TEMPLATE_DIR)
        tpl_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_tpl = QPushButton("打开模板目录")
        set_variant(open_tpl, "primary")
        open_tpl.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(TEMPLATE_DIR)))
        tpl_row.addWidget(tpl_label, 1)
        tpl_row.addWidget(open_tpl)
        pform.addRow("找图模板", tpl_row)
        log_row = QHBoxLayout()
        log_label = QLabel(LOG_PATH)
        log_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_log = QPushButton("打开日志文件")
        set_variant(open_log, "primary")
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_PATH)))
        log_row.addWidget(log_label, 1)
        log_row.addWidget(open_log)
        pform.addRow("运行日志", log_row)
        root.addWidget(path_box)

        about = QLabel(f"{APP_NAME}  ·  鼠标连点 / 键盘连按 / 屏幕找图点击 / 自动化流程\n"
                       "配置修改后自动保存到 config.json；托盘图标右键可快捷启停与退出。")
        about.setStyleSheet("color: #888;")
        root.addWidget(about)
        root.addStretch(1)

    def _ui_changed(self, *_) -> None:
        if not self._loading:
            self.changed.emit()

    def values(self) -> tuple[str, str]:
        return self.hotkey_edit.hotkey(), self.stop_edit.hotkey()
