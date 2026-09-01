"""主窗口：装配各标签页、后台任务与全局热键调度。

窗口正常显示在任务栏；点 X 隐藏到托盘，退出走托盘菜单。
"""
from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from ..config import APP_NAME, AppConfig, ClickerConfig, PresserConfig
from ..capture_report import stop as stop_capture
from ..hotkey_manager import HotkeyManager
from ..keymap import hotkey_display
from ..tasks import ClickTask, PressTask
from ..updater import compare_versions, install_update
from .clicker_tab import ClickerTab
from .finder_tab import FinderTab
from .flow_tab import FlowTab
from .presser_tab import PresserTab
from .settings_tab import SettingsTab
from .update_dialog import AutoDownloader, VersionFetcher

# 基准设计分辨率与对应窗口尺寸：2560x1440 屏 → 1300x900
_BASE_SCREEN = (2560, 1440)
_BASE_WINDOW = (1300, 900)
# 窗口尺寸下限（防止屏幕太小时缩到没法用）
_MIN_WINDOW = (980, 660)


def auto_window_size(screen_w: int, screen_h: int) -> tuple[int, int]:
    """按显示器分辨率动态计算主窗口尺寸。

    以 2560x1440 屏对应 1300x900 为基准，按宽高各自比例取较小的缩放系数
    （保证窗口完整落在屏幕内）：
    - 分辨率 >= 基准（如 4K/2K）：保持 1300x900，不放大
    - 分辨率 < 基准：等比缩小，但宽高都不小于最小窗口尺寸
    """
    if screen_w <= 0 or screen_h <= 0:
        return _BASE_WINDOW
    scale = min(screen_w / _BASE_SCREEN[0], screen_h / _BASE_SCREEN[1])
    w = max(int(_BASE_WINDOW[0] * scale), _MIN_WINDOW[0])
    h = max(int(_BASE_WINDOW[1] * scale), _MIN_WINDOW[1])
    # 大屏不放大：封顶到设计尺寸（1080p 以上保持 1300x900）
    w = min(w, _BASE_WINDOW[0])
    h = min(h, _BASE_WINDOW[1])
    return w, h


class MainWindow(QMainWindow):
    featureStateChanged = Signal(str, bool)  # ("clicker"/"presser"/"finder", running)
    hideToTrayNotice = Signal()

    def __init__(self, cfg: AppConfig, manager: HotkeyManager):
        super().__init__()
        self.cfg = cfg
        self.manager = manager
        # 正常窗口样式：任务栏显示入口；点 X 隐藏到托盘（见 closeEvent）
        self.setWindowTitle(APP_NAME)
        self.resize(*self._window_size())

        self.click_task = ClickTask()
        self.press_task = PressTask()
        self.click_task.get_config = lambda: self._click_snapshot
        self.press_task.get_config = lambda: self._press_snapshot
        self._click_snapshot: ClickerConfig = cfg.clicker
        self._press_snapshot: PresserConfig = cfg.presser
        self._dispatch: dict[str, object] = {}

        tabs = QTabWidget()
        self.tabs = tabs
        self.clicker_tab = ClickerTab(cfg.clicker)
        self.presser_tab = PresserTab(cfg.presser)
        self.finder_tab = FinderTab(cfg.find_tasks)
        self.flow_tab = FlowTab(cfg)
        self.settings_tab = SettingsTab(cfg)
        # 自动化流程是主功能，放第一个
        tabs.addTab(self.flow_tab, "🚀 自动化流程")
        tabs.addTab(self.clicker_tab, "🖱 鼠标连点")
        tabs.addTab(self.presser_tab, "⌨ 键盘连按")
        tabs.addTab(self.finder_tab, "🖼 找图点击")
        tabs.addTab(self.settings_tab, "⚙ 设置")

        # centralWidget = 标签页 + 底部可折叠日志面板
        from .log_panel import LogPanel
        self.log_panel = LogPanel()
        central = QWidget()
        clay = QVBoxLayout(central)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)
        clay.addWidget(tabs, 1)
        clay.addWidget(self.log_panel)
        self.setCentralWidget(central)
        self.log_panel.expandedChanged.connect(self._on_log_expanded)
        self._apply_button_theme()
        self._apply_tab_theme()          # 必须在按钮样式之后：它是往已有 QSS 上追加
        # 接受外部文件拖放（拖入 .json 直接导入为自动化流程）
        self.setAcceptDrops(True)

        bottom = QWidget()
        blay = QHBoxLayout(bottom)
        blay.setContentsMargins(8, 2, 8, 4)
        self.status_hint = QLabel()
        self.status_hint.setStyleSheet("color: #888;")
        stop_btn = QPushButton("全部停止")
        stop_btn.clicked.connect(self.stop_all)
        from .widgets import set_variant
        set_variant(stop_btn, "danger")
        blay.addWidget(self.status_hint, 1)
        blay.addWidget(stop_btn)
        self.statusBar().addPermanentWidget(bottom)
        self.statusBar().setStyleSheet("QStatusBar{border-top: 1px solid #ddd;}")
        # 状态栏左下角：红点 + 提示文字 + 「重启升级」按钮
        # 检测到新版本自动后台下载：红点 +「已检测到新版本」→ 下载完成按钮可用
        self.update_dot = QLabel()
        self.update_dot.setFixedSize(10, 10)
        self.update_dot.setStyleSheet("background: #e53935; border-radius: 5px;")
        self.update_dot.hide()
        self.statusBar().addWidget(self.update_dot)
        self.update_hint = QLabel()
        self.update_hint.setStyleSheet("color: #1668a8; font-weight: 600; padding: 2px 6px;")
        self.update_hint.hide()
        self.statusBar().addWidget(self.update_hint)
        self.update_btn = QPushButton("重启升级")
        self.update_btn.setStyleSheet(
            "QPushButton{background:#1668a8; color:white; border:none;"
            " border-radius:4px; padding:3px 14px; font-weight:600;}"
            "QPushButton:disabled{background:#9bb8d4;}")
        self.update_btn.clicked.connect(self._restart_upgrade)
        self.update_btn.hide()
        self.statusBar().addWidget(self.update_btn)
        # 下载进度条：下载中点击「下载中…」按钮可展开/收起
        self.update_progress = QProgressBar()
        self.update_progress.setFixedWidth(200)
        self.update_progress.setFixedHeight(16)
        self.update_progress.setTextVisible(True)
        self.update_progress.setStyleSheet(
            "QProgressBar{background:#eee; border:none; border-radius:3px;"
            " text-align:center; font-size:10px; color:#555;}"
            "QProgressBar::chunk{background:#1668a8; border-radius:3px;}")
        self.update_progress.hide()
        self.statusBar().addWidget(self.update_progress)
        self._progress_visible = True    # 下载中默认展开进度条，点击可收起
        self._refresh_status_hint()
        self._pending_update: tuple[str, list[str]] | None = None
        self._downloaded_file: str | None = None
        self._update_state = "idle"        # idle / downloading / ready / failed
        self._update_fail_reason = ""

        # ---- 信号接线 ----
        self.clicker_tab.changed.connect(self._on_clicker_changed)
        self.clicker_tab.toggleRequested.connect(self.toggle_clicker)
        self.clicker_tab.captureAboutToStart.connect(self._hide_for_capture)
        self.clicker_tab.captureFinished.connect(self._restore_after_capture)
        self.click_task.stateChanged.connect(self._on_click_state)
        self.click_task.progress.connect(self.clicker_tab.set_progress)

        self.presser_tab.changed.connect(self._on_presser_changed)
        self.presser_tab.toggleRequested.connect(self.toggle_presser)
        self.press_task.stateChanged.connect(self._on_press_state)
        self.press_task.progress.connect(self.presser_tab.set_progress)

        self.finder_tab.changed.connect(self._on_finder_changed)
        self.finder_tab.runningStateChanged.connect(
            lambda: self.featureStateChanged.emit("finder", self.finder_tab.any_running()))
        self.finder_tab.captureAboutToStart.connect(self._hide_for_capture)
        self.finder_tab.captureFinished.connect(self._restore_after_capture)

        self.flow_tab.changed.connect(self._on_flow_changed)
        self.flow_tab.runningStateChanged.connect(self._on_flow_running_changed)
        self.flow_tab.flowStarted.connect(self._on_flow_started)
        # 「每次运行清空日志」勾选状态持久化到配置
        self.log_panel.clearOnRunChanged.connect(self._on_clear_log_setting)

        self.settings_tab.changed.connect(self._on_settings_changed)
        manager.triggered.connect(self._dispatch_hotkey)

        # 初始化期间信号被各标签页守卫屏蔽，这里统一同步一次快照
        self._click_snapshot = self.clicker_tab.snapshot()
        self._press_snapshot = self.presser_tab.snapshot()
        self.cfg.clicker = self._click_snapshot
        self.cfg.presser = self._press_snapshot

        # 配置变化防抖保存
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.cfg.save)
        self.log_panel.clear_on_run = cfg.clear_log_on_run

        self._register_hotkeys()

        # ---- 运行日志面板（底部可折叠，替代原左下角悬浮窗） ----
        from ..logbus import bus, log
        log("程序启动，所有模块就绪")
        bus().message.connect(self.log_panel.append)
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setInterval(500)
        self._overlay_timer.timeout.connect(self._update_overlay)
        self._overlay_timer.start()

        # 启动后延迟检查一次，之后每 1 小时循环检查（后台线程，不阻塞界面）
        QTimer.singleShot(2500, self._check_update)
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(3600 * 1000)   # 1 小时
        self._update_timer.timeout.connect(self._check_update)
        self._update_timer.start()

    def _window_size(self) -> tuple[int, int]:
        """根据当前显示器可用区域（排除任务栏）动态计算窗口尺寸。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return _BASE_WINDOW
        geo = screen.availableGeometry()   # 排除任务栏的实际可用区域
        return auto_window_size(geo.width(), geo.height())

    # ---------- 文件拖放导入 ----------
    @staticmethod
    def _dropped_json_paths(mime) -> list[str]:
        """从拖放数据里取本地 .json 文件路径（含网络地址/非 json 的丢弃）。"""
        if not mime.hasUrls():
            return []
        out = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = url.toLocalFile()
            if p.lower().endswith(".json"):
                out.append(p)
        return out

    def dragEnterEvent(self, ev) -> None:
        if self._dropped_json_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev) -> None:
        if self._dropped_json_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev) -> None:
        paths = self._dropped_json_paths(ev.mimeData())
        if not paths:
            super().dropEvent(ev)
            return
        ev.acceptProposedAction()
        # 切到自动化流程页再导入；逐个尝试，全部失败则汇总提示
        self.tabs.setCurrentWidget(self.flow_tab)
        failed = []
        for p in paths:
            if not self.flow_tab.import_flow_file(p):
                failed.append(os.path.basename(p))
        if failed:
            self.statusBar().showMessage(
                f"导入失败：{'、'.join(failed)} 不是有效的流程文件", 6000)

    # ---------- 日志面板 ----------
    def _on_log_expanded(self, expanded: bool) -> None:
        """日志展开时窗口整体增高、收缩时降低。

        展开优先向下增高；若底部会超出屏幕可用区域，则向上平移窗口
        （保持底部贴屏幕边缘），避免日志面板跑到屏幕外。
        """
        from .log_panel import _TEXT_HEIGHT
        screen = self.screen() or QApplication.primaryScreen()
        delta = _TEXT_HEIGHT if expanded else -_TEXT_HEIGHT
        new_h = max(self.height() + delta, _MIN_WINDOW[1])
        if screen is not None:
            avail = screen.availableGeometry()
            bottom = self.y() + new_h
            if bottom > avail.bottom():
                # 超屏：向上挪，底部对齐屏幕可用区下沿
                self.move(self.x(), max(avail.top(), avail.bottom() - new_h))
        self.resize(self.width(), new_h)

    def _running_summary(self) -> str:
        parts = []
        if self.click_task.is_running:
            parts.append("鼠标连点")
        if self.press_task.is_running:
            parts.append("键盘连按")
        # 走各 tab 的公开方法，不直接翻它们的 _runners（那是内部实现）
        parts.extend(f"找图:{name}" for name in self.finder_tab.running_names())
        parts.extend(f"流程:{name}" for name in self.flow_tab.running_names())
        return " · ".join(parts)

    def _update_overlay(self) -> None:
        self.log_panel.set_summary(self._running_summary())

    def _apply_tab_theme(self) -> None:
        """顶部导航栏配色：选中项蓝色文字 + 底部指示条，hover 浅蓝反馈。

        单独成一个方法，避免和按钮样式混在一起（setStyleSheet 是整体覆盖，
        分两次调用后面的会覆盖前面的，所以这里用 append 累加）。
        """
        tabs = """\
            QTabWidget::pane {
                border: none; border-top: 1px solid #d8dee4;
                background: #f7f9fb; top: -1px;
            }
            QTabBar { background: #ffffff; border: none; }
            QTabBar::tab {
                background: transparent; color: #57606a;
                border: none; padding: 8px 20px; margin-right: 2px;
                font-size: 11pt; font-weight: 500;
                border-bottom: 3px solid transparent;
            }
            QTabBar::tab:hover {
                color: #1668a8; background: #f3f8fd;
                border-bottom: 3px solid #a8cfeb;
            }
            QTabBar::tab:selected {
                color: #1668a8; font-weight: 600; background: #eaf3fb;
                border-bottom: 3px solid #1668a8;
            }
        """
        self.setStyleSheet(self.styleSheet() + tabs)

    def _apply_button_theme(self) -> None:
        """全局按钮配色：默认灰白、蓝=编辑/打开、绿=启动/运行、红=停止/删除。"""
        self.setStyleSheet("""
            QPushButton {
                background: white; color: #24292f;
                border: 1px solid #c9d1d9; border-radius: 4px;
                padding: 4px 12px; font-size: 10pt;
            }
            QPushButton:hover { border-color: #1668a8; color: #1668a8; background: #f3f8fd; }
            QPushButton:pressed { background: #e3edf7; }
            QPushButton:disabled { color: #aab2bb; background: #f2f4f6; border-color: #e1e4e8; }
            QPushButton#btnPrimary {
                background: #1668a8; color: white; border: 1px solid #125a93;
            }
            QPushButton#btnPrimary:hover { background: #1d78c0; color: white; }
            QPushButton#btnPrimary:pressed { background: #125a93; }
            QPushButton#btnSuccess {
                background: #2f9e5b; color: white; border: 1px solid #278a4f;
            }
            QPushButton#btnSuccess:hover { background: #35b168; color: white; }
            QPushButton#btnSuccess:pressed { background: #278a4f; }
            QPushButton#btnDanger {
                background: #d64541; color: white; border: 1px solid #c0392b;
            }
            QPushButton#btnDanger:hover { background: #e2544f; color: white; }
            QPushButton#btnDanger:pressed { background: #c0392b; }
            QPushButton#btnDanger:disabled, QPushButton#btnSuccess:disabled,
            QPushButton#btnPrimary:disabled {
                color: #f0f3f6; background: #b9c2cb; border-color: #b9c2cb;
            }
        """)

    # ---------- 快照与保存 ----------
    def _on_clicker_changed(self) -> None:
        self._click_snapshot = self.clicker_tab.snapshot()
        self.cfg.clicker = self._click_snapshot
        self._register_hotkeys()
        self._save_timer.start()

    def _on_presser_changed(self) -> None:
        self._press_snapshot = self.presser_tab.snapshot()
        self.cfg.presser = self._press_snapshot
        self._register_hotkeys()
        self._save_timer.start()

    def _on_finder_changed(self) -> None:
        self._register_hotkeys()
        self._save_timer.start()

    def _on_flow_changed(self) -> None:
        self._register_hotkeys()
        self._save_timer.start()

    def _on_flow_running_changed(self) -> None:
        self.featureStateChanged.emit("flow", self.flow_tab.any_running())

    def _on_flow_started(self) -> None:
        """有流程开始运行：勾选了「每次运行清空日志」就清空底部日志。"""
        if self.log_panel.clear_on_run:
            self.log_panel.clear()

    def _on_clear_log_setting(self, checked: bool) -> None:
        """「每次运行清空日志」勾选状态变化：持久化到配置。"""
        self.cfg.clear_log_on_run = bool(checked)
        self._save_timer.start()

    def _refresh_status_hint(self) -> None:
        """底部状态栏热键提示，随设置实时刷新。"""
        toggle_hk = hotkey_display(self.cfg.show_hide_hotkey) or "未设置"
        stop_hk = hotkey_display(self.cfg.stop_all_hotkey) or "未设置"
        self.status_hint.setText(f"显示/隐藏窗口：{toggle_hk}    紧急停止：{stop_hk}")

    def _on_settings_changed(self) -> None:
        toggle_hk, stop_hk = self.settings_tab.values()
        self.cfg.show_hide_hotkey = toggle_hk
        self.cfg.stop_all_hotkey = stop_hk
        self._refresh_status_hint()
        self._register_hotkeys()
        self._save_timer.start()

    # ---------- 热键注册与调度 ----------
    def _register_hotkeys(self) -> None:
        self._dispatch = {}
        conflicts: list[str] = []

        def bind(hotkey: str, fn) -> None:
            hk = HotkeyManager.normalize(hotkey)
            if not hk:
                return
            if hk in self._dispatch:
                conflicts.append(hotkey_display(hk))
                return
            self._dispatch[hk] = fn

        bind(self.cfg.show_hide_hotkey, self.toggle_show_hide)
        bind(self.cfg.stop_all_hotkey, self.stop_all)
        bind(self.cfg.clicker.hotkey, self.toggle_clicker)
        bind(self.cfg.presser.hotkey, self.toggle_presser)
        for t in self.cfg.find_tasks:
            hk = HotkeyManager.normalize(t.hotkey)
            if hk:
                bind(t.hotkey, (lambda tid: lambda: self.finder_tab.toggle_task(tid))(t.id))
        for f in self.cfg.flows:
            hk = HotkeyManager.normalize(f.hotkey)
            if hk:
                bind(f.hotkey, (lambda fid: lambda: self.flow_tab.toggle_flow(fid))(f.id))

        self.manager.unregister_all()
        failed = []
        for hk in self._dispatch:
            if not self.manager.register(hk):
                failed.append(hotkey_display(hk))
        if conflicts:
            self.statusBar().showMessage(f"热键冲突，已忽略：{'、'.join(conflicts)}", 5000)
        if failed:
            self.statusBar().showMessage(f"以下热键注册失败：{'、'.join(failed)}", 5000)

    def _dispatch_hotkey(self, hk: str) -> None:
        fn = self._dispatch.get(HotkeyManager.normalize(hk))
        if fn:
            fn()

    # ---------- 功能启停 ----------
    def toggle_clicker(self) -> None:
        if self.click_task.is_running:
            self.click_task.stop()
        else:
            self.clicker_tab.status.set_running()
            self.click_task.start()

    def toggle_presser(self) -> None:
        if self.press_task.is_running:
            self.press_task.stop()
        else:
            self.presser_tab.status.set_running()
            self.press_task.start()

    def stop_all(self) -> None:
        self.click_task.stop()
        self.press_task.stop()
        self.finder_tab.stop_all()
        self.flow_tab.stop_all()

    def toggle_all_finder(self) -> None:
        if self.finder_tab.any_running():
            self.finder_tab.stop_all()
        else:
            self.finder_tab.start_enabled_all()

    def _on_click_state(self, state: str, reason: str) -> None:
        self.clicker_tab.set_running(state == "running", reason)
        self.featureStateChanged.emit("clicker", state == "running")

    def _on_press_state(self, state: str, reason: str) -> None:
        self.presser_tab.set_running(state == "running", reason)
        self.featureStateChanged.emit("presser", state == "running")

    # ---------- 在线更新 ----------
    def _check_update(self) -> None:
        """后台线程拉取远端版本号（无网络/仓库不可达时静默跳过）。

        由定时器驱动：启动后首次 + 每 1 小时一次。已有一次检查在途时不重复起。
        """
        if getattr(self, "_version_fetcher", None) is not None \
                and self._version_fetcher.isRunning():
            return
        self._version_fetcher = VersionFetcher()
        self._version_fetcher.fetched.connect(self._on_version_fetched)
        self._version_fetcher.start()

    def _on_version_fetched(self, source) -> None:
        remote, download_urls = source if isinstance(source, tuple) else (None, [])
        if not remote or not download_urls:
            logging.getLogger(__name__).info("检查更新：未获取到远端版本，跳过")
            return
        local = self.cfg.version or "1.0.0"
        if compare_versions(local, remote) >= 0:
            # local / remote 可能是 "v3.0.2" 这种带 v 前缀的 tag 原文，不要再拼 v
            logging.getLogger(__name__).info("检查更新：当前 %s 已是最新（远端 %s）",
                                             local, remote)
            return
        # 已有更新流程（下载中 / 已就绪 / 失败待重试）时不重复触发
        if self._update_state != "idle":
            return
        logging.getLogger(__name__).info("发现新版本：%s -> %s，开始自动下载：%s",
                                         local, remote, download_urls[0])
        # 不弹窗：红点 + 提示文字，后台线程自动下载，完成后「重启升级」按钮可用
        self._pending_update = (remote, download_urls)
        self._set_update_state("downloading")
        self._start_auto_download(download_urls)

    def _start_auto_download(self, download_urls: list[str]) -> None:
        """后台线程自动下载新版本到本地（不打断用户操作）。"""
        self._downloader = AutoDownloader(download_urls)
        self._downloader.progress.connect(self._on_download_progress)
        self._downloader.completed.connect(self._on_download_completed)
        self._downloader.failed.connect(self._on_download_failed)
        self._downloader.start()

    def _on_download_progress(self, done: int, total: int) -> None:
        """下载中实时刷新状态栏进度条（仅在展开时可见）。"""
        if total > 0:
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(min(100, int(done * 100 / total)))
            self.update_progress.setFormat(
                f"{done / 1048576:.0f} / {total / 1048576:.0f} MB")
        else:
            self.update_progress.setRange(0, 0)   # 服务器未返回大小：滚动不定进度
            self.update_progress.setFormat(f"{done / 1048576:.0f} MB")

    def _on_download_completed(self, file: str) -> None:
        self._downloaded_file = file
        self._update_state = "ready"
        self._set_update_state("ready")
        logging.getLogger(__name__).info("新版本已下载到本地：%s", file)

    def _on_download_failed(self, err: str) -> None:
        self._update_fail_reason = err
        self._update_state = "failed"
        self._set_update_state("failed")
        logging.getLogger(__name__).warning("自动下载新版本失败：%s", err)

    def _set_update_state(self, state: str) -> None:
        """按状态刷新状态栏左下角的红点 / 提示文字 / 按钮 / 进度条。"""
        self._update_state = state
        if state == "idle":
            self.update_dot.hide()
            self.update_hint.hide()
            self.update_btn.hide()
            self.update_progress.hide()
            self._progress_visible = True   # 重置为默认展开
            return
        remote = (self._pending_update or ("", []))[0]
        self.update_dot.show()
        self.update_hint.show()
        self.update_btn.show()
        if state == "downloading":
            self.update_hint.setText(f"已检测到新版本 {remote}，正在自动下载…")
            self.update_btn.setText("下载中…")
            self.update_btn.setEnabled(True)   # 可点击：收起/展开实时进度条
            self._progress_visible = True       # 默认展开进度条，点击可收起
            self.update_progress.setVisible(True)
        elif state == "ready":
            self.update_hint.setText(f"新版本 {remote} 已下载")
            self.update_btn.setText("重启升级")
            self.update_btn.setEnabled(True)
            self.update_progress.hide()
            self._progress_visible = True   # 下次下载默认展开
        elif state == "failed":
            self.update_hint.setText(f"新版本 {remote} 下载失败")
            self.update_btn.setText("重新下载")
            self.update_btn.setEnabled(True)
            self.update_progress.hide()
            self._progress_visible = True

    def _restart_upgrade(self) -> None:
        """左下角按钮：下载中点击=展开/收起进度条；已就绪=替换 exe 并重启；
        失败=重新自动下载。"""
        if self._update_state == "downloading":
            self._progress_visible = not self._progress_visible
            self.update_progress.setVisible(self._progress_visible)
            return
        if self._update_state == "ready" and self._downloaded_file:
            ok, why = install_update(self._downloaded_file)
            if not ok:
                QMessageBox.warning(self, "更新失败", why)
                return
            remote = (self._pending_update or ("", []))[0]
            if remote:
                self.cfg.version = remote
                self.cfg.save()
            logging.getLogger(__name__).info("重启升级：已替换为 %s，程序退出", remote)
            self.shutdown()
            QApplication.quit()
        elif self._update_state == "failed":
            urls = (self._pending_update or ("", []))[1]
            self._set_update_state("downloading")   # 内部默认展开进度条
            self._start_auto_download(urls)

    # ---------- 窗口显隐 ----------
    def _hide_for_capture(self) -> None:
        """截屏取模前隐藏自己，避免主窗口被截进模板。"""
        self._was_visible_before_capture = self.isVisible()
        if self._was_visible_before_capture:
            self.hide()

    def _restore_after_capture(self) -> None:
        """取模结束（保存或取消）后恢复窗口。"""
        if getattr(self, "_was_visible_before_capture", False):
            self._was_visible_before_capture = False
            self.show()
            self.raise_()
            self.activateWindow()

    def show_window(self) -> None:
        """显示并置顶到前台。"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self._force_foreground()

    def hide_window(self) -> None:
        self.hide()

    def toggle_show_hide(self) -> None:
        """显示/隐藏切换键：隐藏时显示；已显示但不在前台时置顶；已在前台时隐藏。"""
        if not self.isVisible() or self.isMinimized():
            self.show_window()
        elif self.isActiveWindow():
            self.hide_window()
        else:
            self.show_window()

    def _force_foreground(self) -> None:
        """把窗口顶到屏幕最前面。

        Qt.Tool 窗口从后台 activateWindow 常被系统拒绝（只闪烁不置前），
        这里用 Win32：临时 TOPMOST + AttachThreadInput 借用前台线程权限。
        """
        self.raise_()
        self.activateWindow()
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(self.winId())
            swp = 0x1 | 0x2  # SWP_NOSIZE | SWP_NOMOVE
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, swp)
            if user32.GetForegroundWindow() != hwnd:
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None)
                this_tid = kernel32.GetCurrentThreadId()
                if fg_tid and fg_tid != this_tid:
                    user32.AttachThreadInput(this_tid, fg_tid, True)
                    user32.SetForegroundWindow(hwnd)
                    user32.AttachThreadInput(this_tid, fg_tid, False)
                else:
                    user32.SetForegroundWindow(hwnd)
            QTimer.singleShot(300, lambda: user32.SetWindowPos(
                hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, swp))
        except Exception:
            logging.getLogger(__name__).debug("强制置前失败", exc_info=True)

    def closeEvent(self, ev) -> None:
        # 点 X 隐藏到托盘，不退出；退出走托盘菜单
        ev.ignore()
        self.hide()
        self.hideToTrayNotice.emit()

    def shutdown(self) -> None:
        stop_capture()          # 停止定时截屏上报线程
        self.stop_all()
        self.manager.unregister_all()
