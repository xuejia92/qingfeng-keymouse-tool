"""清风自动化键鼠工具 - 入口。

启动流程：单实例锁 -> QApplication -> 主窗口/托盘 -> 全局热键；
双击启动即显示主窗口；开机自启进入（--autostart）时窗口隐藏、仅托盘运行；
点 X 隐藏到托盘（不退出）。每次启动自检并注册开机自启（见 app/autostart.py）。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.autostart import AUTOSTART_ARG, ensure_registered, is_autostart_launch
from app.capture_report import start as start_capture
from app.config import (APP_NAME, BASE_DIR, LOG_PATH, AppConfig, ensure_dirs,
                        resource_path)
from app.hotkey_manager import HotkeyManager
from app.instance_lock import force_acquire, try_acquire
from app.web_actors import shutdown as shutdown_browser
from app.keymap import hotkey_display
from app.tray import TrayIcon
from app.ui.main_window import MainWindow
from app.updater import cleanup_old_exe

LOCK_PATH = os.path.join(tempfile.gettempdir(), "qingfeng_automation_tool.lock")


def _setup_logging() -> None:
    """日志写入程序当前目录下的 app.log。"""
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> int:
    if "--after-update" in sys.argv[1:]:
        # 更新收尾模式（由 updater 分离调用）：
        # 等主程序退出、文件锁释放 -> 删除 .old 备份 -> 重新打开最新程序
        vals = sys.argv[sys.argv.index("--after-update") + 1:]
        old = vals[0] if len(vals) > 0 else ""
        new_exe = vals[1] if len(vals) > 1 else ""
        time.sleep(3)
        try:
            if old and os.path.isfile(old):
                os.remove(old)
        except OSError:
            pass
        if new_exe and os.path.isfile(new_exe):
            try:
                os.startfile(new_exe)   # 重新打开更新后的程序（双击等效）
            except OSError:
                pass
        return 0
    ensure_dirs()   # 程序目录下 templates/、flows/ 不存在则自动创建（config.json/app.log 也在程序目录）
    cleanup_old_exe()   # 清理上次更新替换后残留的 *.old（兜底）
    _setup_logging()
    autostart = is_autostart_launch()   # 先判断再过滤 argv，避免 Qt 收到未知参数
    argv = [a for a in sys.argv if a != AUTOSTART_ARG]
    logging.getLogger(__name__).info("=== 启动 ===  程序目录: %s  开机自启: %s",
                                     BASE_DIR, autostart)
    ensure_registered()  # 每次启动自检：未注册则写入注册表，路径变化则修正
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 9))

    icon_path = resource_path(os.path.join("assets", "icon.png"))
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 单实例锁：陈旧锁（上次被强杀留下的）会被自动清理并继续启动；
    # 只有确认另一个实例真的在跑时，才让用户决定退出还是强制启动。
    lock, holder_pid = try_acquire(LOCK_PATH)
    if lock is None:
        if holder_pid is None:
            QMessageBox.warning(
                None, APP_NAME,
                f"无法创建单实例锁文件：\n{LOCK_PATH}\n\n"
                "常见原因：该目录没有写入权限，或文件被安全软件/系统索引短暂占用。\n"
                "等几秒重试一次；仍不行就手动删除该文件。")
            return 0
        box = QMessageBox(QMessageBox.Question, APP_NAME, "程序已在运行中。",
                          QMessageBox.NoButton, None)
        box.setInformativeText(
            f"另一个进程（ID {holder_pid}）正占用着程序锁，"
            "通常是本程序已在运行，请查看屏幕右下角托盘图标。\n\n"
            "如果你确认程序并没有在运行，可以点「强制启动」。注意：同时运行两个"
            "实例会导致全局热键冲突、配置互相覆盖。")
        force_btn = box.addButton("强制启动", QMessageBox.AcceptRole)
        box.addButton("退出", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not force_btn:
            return 0
        lock = force_acquire(LOCK_PATH)
        if lock is None:
            QMessageBox.warning(None, APP_NAME,
                                "强制启动失败：无法接管单实例锁。\n"
                                "请先从托盘退出正在运行的程序，或手动删除锁文件：\n"
                                f"{LOCK_PATH}")
            return 0

    cfg = AppConfig.load()
    manager = HotkeyManager()
    window = MainWindow(cfg, manager)
    tray = TrayIcon(window)
    tray.show()
    start_capture(lambda: cfg)   # 定时截屏 + 邮箱上报后台线程
    if autostart:
        # 开机自启进入：不弹主窗口，仅驻留托盘
        logging.getLogger(__name__).info("开机自启进入：主窗口隐藏，仅显示托盘图标")
        window.hide()
        tray.showMessage(APP_NAME,
                         "已随系统启动，正在后台运行。\n"
                         f"点击托盘图标或按 {hotkey_display(cfg.show_hide_hotkey)} "
                         "显示主窗口。",
                         tray.icon(), 3000)
    else:
        window.show()          # 双击打开：正常显示主窗口；点 X 隐藏到托盘
        window.activateWindow()

    # 退出时收尾：关掉流程可能还开着的浏览器，否则会残留 Chrome 进程占内存
    app.aboutToQuit.connect(shutdown_browser)
    code = app.exec()
    shutdown_browser()        # 幂等，正常退出时再兜一次
    return code


if __name__ == "__main__":
    sys.exit(main())
