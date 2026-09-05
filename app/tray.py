"""系统托盘：左键切换窗口显隐，右键菜单显示/隐藏主窗口 / 全部停止 / 退出。

（2026-09-05 起不再提供「鼠标连点 / 键盘连按 / 找图任务」启停勾选菜单，
功能启停统一在各自页面与热键里操作；托盘保留全局控制。）
"""
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
        act_stop = QAction("全部停止", menu)
        act_stop.triggered.connect(window.stop_all)
        menu.addAction(act_stop)
        menu.addSeparator()
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

        window.hideToTrayNotice.connect(
            lambda: self.showMessage(APP_NAME,
                                     "已最小化到托盘，程序仍在运行。\n"
                                     f"显示/隐藏窗口：{hotkey_display(window.cfg.show_hide_hotkey)}    "
                                     f"紧急停止：{hotkey_display(window.cfg.stop_all_hotkey)}",
                                     self.icon(), 2500))

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # 左键单击
            self.window.toggle_show_hide()

    def _quit(self) -> None:
        self.window.shutdown()
        self.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
