"""全局日志总线：各任务/流程/热键把运行事件写进来，悬浮窗与界面订阅显示。"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal

_bus: "LogBus | None" = None


class LogBus(QObject):
    message = Signal(str)   # 一条带时间前缀的日志文本

    def write(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.message.emit(f"{stamp}  {text}")


def bus() -> LogBus:
    global _bus
    if _bus is None:
        _bus = LogBus()
    return _bus


def log(text: str) -> None:
    """任意线程可调用；信号自动排队到主线程显示。"""
    bus().write(text)
