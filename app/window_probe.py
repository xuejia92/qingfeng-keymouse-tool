"""临时窗口探针 v5：Qt 全局事件过滤器 + Show 瞬间抓 Python 调用栈。

v4 已定位：一闪而逝的窗口 = QComboBox 私有弹出容器(QComboBoxPrivateContainer)
+ QMenu。v5 不再走 Win32 层，直接在 QApplication 装全局事件过滤器：
任何顶层窗口（Popup/Dialog/Window）收到 Show/Create/激活 事件的瞬间，
过滤器在主线程同步执行，traceback 直接给出「谁把它 show 出来的」。

定位完即删本文件及 schedule_tab 里的 _start_probe 调用。
"""
from __future__ import annotations

import logging
import traceback

from PySide6.QtCore import QObject

_LOG = logging.getLogger("window_probe")

_EVENT_NAMES = {}


class _Sniffer(QObject):  # installEventFilter 要求必须是 QObject 子类
    def __init__(self):
        super().__init__()
        self._count = 0

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 约定
        try:
            from PySide6.QtCore import QEvent
            et = event.type()
            if et not in (QEvent.Type.Show, QEvent.Type.Create,
                          QEvent.Type.WindowActivate, QEvent.Type.Hide,
                          QEvent.Type.Close):
                return False
            try:
                is_win = obj.isWindowType()
            except Exception:
                is_win = False
            if not is_win:
                return False
            mo = obj.metaObject() if hasattr(obj, "metaObject") else None
            cls = mo.className() if mo else type(obj).__name__
            try:
                title = obj.windowTitle()
            except Exception:
                title = ""
            try:
                name = obj.objectName()
            except Exception:
                name = ""
            names = _EVENT_NAMES or {
                QEvent.Type.Show: "Show",
                QEvent.Type.Create: "Create",
                QEvent.Type.WindowActivate: "激活",
                QEvent.Type.Hide: "Hide",
                QEvent.Type.Close: "Close",
            }
            self._count += 1
            stack = "".join(traceback.format_stack(limit=18))
            _LOG.info(
                "顶层窗口事件#%d: %s class=%s objName=%r title=%r\nPython栈:\n%s",
                self._count, names.get(et, str(int(et))), cls, name, title, stack)
        except Exception:
            pass
        return False


class WindowEventProbe:
    """v5：Qt 全局事件过滤器。用法（Qt 主线程）：
        probe = WindowEventProbe(duration=10.0); probe.start()
    duration 后自动卸载过滤器（经 QTimer.singleShot）。
    """

    def __init__(self, duration: float = 10.0):
        self._duration = duration
        self._sniffer = None
        self._installed = False

    def start(self) -> None:
        if self._installed:
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        self._sniffer = _Sniffer()
        app.installEventFilter(self._sniffer)
        self._installed = True
        _LOG.info("窗口探针v5: Qt事件过滤器已安装，记录 %.0f 秒", self._duration)
        QTimer.singleShot(int(self._duration * 1000), self.stop)

    def stop(self) -> None:
        if not self._installed:
            return
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and self._sniffer is not None:
            app.removeEventFilter(self._sniffer)
        _LOG.info("窗口探针v5: 结束，共捕获顶层窗口事件 %d 条",
                  self._sniffer._count if self._sniffer else 0)
        self._sniffer = None
        self._installed = False
