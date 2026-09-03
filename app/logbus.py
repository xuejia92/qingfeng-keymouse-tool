"""全局日志总线：各任务/流程/热键把运行事件写进来，悬浮窗与界面订阅显示。"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal

_bus: "LogBus | None" = None


class LogBus(QObject):
    # 一条日志：带时间前缀的文本 + 种类。kind="print" 表示打印输出模块的输出
    # （日志面板蓝色显示），"log" 为普通日志（默认色）。
    message = Signal(str, str)

    def write(self, text: str) -> None:
        self._emit(text, "log")

    def write_print(self, text: str) -> None:
        self._emit(text, "print")

    def write_print_raw(self, text: str) -> None:
        self._emit(text, "print_raw")

    def _emit(self, text: str, kind: str) -> None:
        # 原始输出（print_raw）：不加时间戳，由日志面板原样渲染（也不自动换行）
        if kind == "print_raw":
            self.message.emit(text, kind)
            return
        stamp = time.strftime("%H:%M:%S")
        self.message.emit(f"{stamp}  {text}", kind)


def bus() -> LogBus:
    global _bus
    if _bus is None:
        _bus = LogBus()
    return _bus


def log(text: str) -> None:
    """任意线程可调用；信号自动排队到主线程显示。"""
    bus().write(text)


def log_print(text: str) -> None:
    """打印输出模块的输出：日志面板以蓝色字体区分显示。"""
    bus().write_print(text)


def log_print_raw(text: str) -> None:
    """打印输出模块的「原始输出」：不加时间戳、不自动换行，内容原样显示。"""
    bus().write_print_raw(text)
