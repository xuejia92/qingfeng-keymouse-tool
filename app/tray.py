"""系统托盘：左键切换窗口显隐，右键菜单启停功能 / 全部停止 / 退出。"""
from __future__ import annotations

import os

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .config import APP_NAME, resource_path
from .keymap import hotkey_display


class TrayIcon(QSystemTrayIcon):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        icon_path = resource_path(os.path.join("assets", "icon.png"))
        if os.path.isfile(icon_path):
            self.setIcon(QIcon(icon_path))
        self.setToolTip(APP_NAME)

        menu = QMenu()
        self.act_show = QAction("显示 / 隐藏主窗口", menu)
        self.act_show.triggered.connect(window.toggle_show_hide)
        menu.addAction(self.act_show)
        menu.addSeparator()

        self.act_clicker = QAction("鼠标连点", menu)
        self.act_clicker.setCheckable(True)
        self.act_clicker.triggered.connect(window.toggle_clicker)
        menu.addAction(self.act_clicker)

        self.act_presser = QAction("键盘连按", menu)
        self.act_presser.setCheckable(True)
        self.act_presser.triggered.connect(window.toggle_presser)
        menu.addAction(self.act_presser)

        self.act_finder = QAction("找图任务（全部启用）", menu)
        self.act_finder.setCheckable(True)
        self.act_finder.triggered.connect(window.toggle_all_finder)
        menu.addAction(self.act_finder)

        menu.addSeparator()
        act_stop = QAction("全部停止", menu)
        act_stop.triggered.connect(window.stop_all)
        menu.addAction(act_stop)
        menu.addSeparator()
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

        window.featureStateChanged.connect(self._sync_checks)
        window.hideToTrayNotice.connect(
            lambda: self.showMessage(APP_NAME,
                                     "已最小化到托盘，程序仍在运行。\n"
                                     f"显示/隐藏窗口：{hotkey_display(window.cfg.show_hide_hotkey)}    "
                                     f"紧急停止：{hotkey_display(window.cfg.stop_all_hotkey)}",
                                     self.icon(), 2500))
        self._sync_checks("clicker", False)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # 左键单击
            self.window.toggle_show_hide()

    def _sync_checks(self, feature: str, running: bool) -> None:
        if feature == "clicker":
            self.act_clicker.setChecked(running)
        elif feature == "presser":
            self.act_presser.setChecked(running)
        elif feature == "finder":
            self.act_finder.setChecked(running)

    def _quit(self) -> None:
        self.window.shutdown()
        self.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
