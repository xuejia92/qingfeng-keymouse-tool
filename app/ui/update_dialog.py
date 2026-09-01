"""自动更新界面：后台线程检查版本 / 下载新程序，Qt 信号驱动进度条与完成回调。"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout)

from ..updater import download_update, resolve_update_sources, update_download_dest


class VersionFetcher(QObject):
    """后台线程解析更新源，fetched 信号带回 (版本号或 None, 候选地址列表)。"""

    fetched = Signal(object)

    def __init__(self):
        super().__init__()
        self._running = False

    def isRunning(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        def work():
            try:
                self.fetched.emit(resolve_update_sources())
            finally:
                self._running = False

        threading.Thread(target=work, daemon=True, name="检查更新").start()


class AutoDownloader(QObject):
    """后台线程下载新版本（不弹窗，供状态栏手动更新流程使用）。

    completed 带回下载好的本地文件路径；failed 带回失败原因；
    progress 实时回传 (已下载字节, 总字节；0=服务器未给大小)。
    """

    completed = Signal(str)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, download_urls: list[str]):
        super().__init__()
        self._urls = [u for u in (download_urls or []) if u]
        self._dest = update_download_dest()
        self._running = False

    def isRunning(self) -> bool:
        return self._running

    def dest(self) -> str:
        return self._dest

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        urls, dest = self._urls, self._dest

        def work():
            last = "下载失败"
            try:
                for url in urls:   # 依次尝试候选地址
                    ok, why = download_update(
                        url, dest,
                        progress_cb=lambda d, t: self.progress.emit(d, t))
                    if ok:
                        self.completed.emit(dest)
                        return
                    last = why
                self.failed.emit(last)
            finally:
                self._running = False

        threading.Thread(target=work, daemon=True, name="手动下载更新").start()


class DownloadDialog(QDialog):
    """下载进度对话框：进度条 + 已下载/总大小，可取消（自动清理半成品）。"""

    progress = Signal(int, int)   # (已下载字节, 总字节；0=服务器未给大小)
    completed = Signal(str)       # 下载完成的本地文件路径
    failed = Signal(str)          # 失败原因（空串 = 用户取消）

    def __init__(self, version: str, download_urls: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"下载更新 v{version}")
        self.setMinimumWidth(440)
        self._stop = threading.Event()
        self._dest = update_download_dest()
        self._urls = [u for u in (download_urls or []) if u]
        self.file: str | None = None
        self.error: str = ""

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addWidget(QLabel(f"正在下载新版本 v{version} …"))
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        root.addWidget(self.bar)
        row = QHBoxLayout()
        self.size_label = QLabel("准备下载…")
        cancel = QPushButton("取消")
        cancel.clicked.connect(self._cancel)
        row.addWidget(self.size_label, 1)
        row.addWidget(cancel)
        root.addLayout(row)

        self.progress.connect(self._on_progress)
        self.completed.connect(self._on_completed)
        self.failed.connect(self._on_failed)

    def start(self) -> None:
        urls, dest = self._urls, self._dest

        def work():
            last = "下载失败"
            for url in urls:   # 依次尝试候选地址（如 v2.0.0 / 2.0.0 两种 tag）
                ok, why = download_update(
                    url, dest,
                    progress_cb=lambda done, total: self.progress.emit(done, total),
                    stop_event=self._stop)
                if ok:
                    self.completed.emit(dest)
                    return
                if why == "已取消":
                    self.failed.emit("")
                    return
                last = why
            self.failed.emit(last)

        threading.Thread(target=work, daemon=True, name="下载更新").start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(min(100, int(done * 100 / total)))
            self.size_label.setText(f"{done / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.bar.setRange(0, 0)   # 服务器未返回大小：显示滚动的不定进度
            self.size_label.setText(f"已下载 {done / 1048576:.1f} MB")

    def _on_completed(self, path: str) -> None:
        self.file = path
        self.bar.setValue(100)
        self.accept()

    def _on_failed(self, why: str) -> None:
        self.error = why
        self.reject()

    def _cancel(self) -> None:
        self._stop.set()
        self.size_label.setText("正在取消…")

    def reject(self) -> None:
        self._stop.set()   # 任何关闭路径都停掉下载线程
        super().reject()

    @staticmethod
    def run(version: str, download_urls: list[str], parent=None) -> tuple[str | None, str]:
        """模态运行下载；返回 (下载完成的文件路径或 None, 失败原因)。"""
        dlg = DownloadDialog(version, download_urls, parent)
        dlg.start()
        dlg.exec()
        return dlg.file, dlg.error
