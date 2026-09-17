"""设置页：全局热键 + 配置目录。"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout, QWidget)

from .. import capture_report
from ..config import APP_NAME, BASE_DIR, CONFIG_PATH, LOG_PATH, TEMPLATE_DIR
from .. import hotkey_policy
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
        self.hotkey_edit.set_conflict_checker(lambda hk: hotkey_policy.check(hk, "show_hide"))
        form.addRow("显示 / 隐藏主窗口（切换）", self.hotkey_edit)
        self.stop_edit = HotkeyEdit()
        self.stop_edit.setMaximumWidth(220)
        self.stop_edit.set_conflict_checker(lambda hk: hotkey_policy.check(hk, "stop_all"))
        form.addRow("紧急停止全部任务", self.stop_edit)
        warn = QLabel("⚠ 任务可能在后台持续点击/按键，失控时请立刻按紧急停止热键，"
                      "或用鼠标右键托盘图标选择「全部停止」。")
        warn.setStyleSheet("color: #c0392b;")
        warn.setWordWrap(True)
        form.addRow("", warn)
        root.addWidget(hotkey_box)

        # 截屏上报：把「本机设备号」摆出来 + 一键加入/移出排除名单
        # （以前只能靠日志或翻代码猜，抄错一个字符就等于没排除——2026-09-17）
        cap_box = QGroupBox("截屏上报（定时截屏，打包成 zip 发到收件邮箱）")
        cform = QFormLayout(cap_box)
        dev_row = QHBoxLayout()
        self.device_label = QLabel(capture_report.device_id_raw() or "（读不到设备号）")
        self.device_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.device_label.setWordWrap(True)
        copy_dev = QPushButton("复制")
        set_variant(copy_dev, "primary")
        copy_dev.clicked.connect(
            lambda: QApplication.clipboard().setText(capture_report.device_id_raw()))
        dev_row.addWidget(self.device_label, 1)
        dev_row.addWidget(copy_dev)
        cform.addRow("本机设备号", dev_row)
        self.capture_state = QLabel()
        self.capture_state.setWordWrap(True)
        cform.addRow("当前状态", self.capture_state)
        self.capture_btn = QPushButton()
        set_variant(self.capture_btn, "primary")
        self.capture_btn.clicked.connect(self._toggle_capture_exclude)
        cform.addRow("", self.capture_btn)
        cap_hint = QLabel("不参与上报的设备号存在 config.json 的 capture_excluded_ids；"
                          "在这里切换后下个周期即生效，不需要重启程序。")
        cap_hint.setStyleSheet("color: #888;")
        cap_hint.setWordWrap(True)
        cform.addRow("", cap_hint)
        self._refresh_capture_state()
        root.addWidget(cap_box)

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

    # ---------- 截屏上报 ----------
    def _is_capture_excluded(self) -> bool:
        """内存配置（即将落盘的那份）里，本机是否已在排除名单。"""
        return capture_report.is_excluded_device(self.cfg)

    def _refresh_capture_state(self) -> None:
        excluded = self._is_capture_excluded()
        available = capture_report.device_id_available()
        if not available:
            self.capture_state.setText("读不到本机设备号（注册表 MachineGuid 不可用），"
                                       "无法把本机排除")
        elif excluded:
            self.capture_state.setText("本机不参与截屏上报（已在排除名单）")
        else:
            self.capture_state.setText(
                "本机参与截屏上报：每 %s 秒截图、每 %s 分钟发送一封"
                % (getattr(self.cfg, "capture_interval_sec", 10),
                   getattr(self.cfg, "send_interval_min", 5)))
        self.capture_btn.setEnabled(available)
        self.capture_btn.setText("恢复本机参与截屏上报" if excluded
                                 else "本机不参与截屏上报")

    def _toggle_capture_exclude(self) -> None:
        """把本机设备号加入/移出 config.json 的 capture_excluded_ids（下个周期生效）。"""
        raw = getattr(self.cfg, "capture_excluded_ids", "")
        self.cfg.capture_excluded_ids = (
            capture_report.remove_excluded_id(raw) if self._is_capture_excluded()
            else capture_report.add_excluded_id(raw))
        self._refresh_capture_state()
        self._ui_changed()          # 走主窗口的防抖保存

    def showEvent(self, event):     # noqa: N802（Qt 命名，切回本页时刷新状态）
        super().showEvent(event)
        self._refresh_capture_state()

    def values(self) -> tuple[str, str]:
        return self.hotkey_edit.hotkey(), self.stop_edit.hotkey()
