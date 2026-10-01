"""全局热键管理。

内部用 pynput 低级钩子引擎（app/physical_hotkeys.py）：只认**物理按键**，
程序自己合成的按键（流程发送的技能键等）不会触发热键——否则游戏自动化
流程一边发技能键、一边被自己的按键停掉（2026-09-22 现场事故）。

对外仍是：register(hotkey) / unregister(hotkey) / unregister_all()，
触发时发出 triggered(hotkey) 信号（自动排队到主线程），由 MainWindow 分发。
单键热键（如 F6）注册时带 suppress 拦截，避免按键漏进其他程序；
含 Ctrl/Alt/Win 的组合键不拦截。
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

from .physical_hotkeys import engine as _engine

log = logging.getLogger(__name__)


class HotkeyManager(QObject):
    triggered = Signal(str)  # 触发的热键（keyboard 库小写格式）

    def __init__(self):
        super().__init__()
        self._handlers: dict[str, object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def normalize(hotkey: str) -> str:
        return (hotkey or "").strip().lower()

    def register(self, hotkey: str) -> bool:
        """注册热键，返回是否成功。重复注册视为已成功。"""
        hotkey = self.normalize(hotkey)
        if not hotkey:
            return False
        with self._lock:
            if hotkey in self._handlers:
                return True
        suppress = not any(m in hotkey for m in ("ctrl+", "alt+", "win+"))
        ok = _engine.register(hotkey, lambda: self._on_trigger(hotkey),
                              suppress=suppress)
        if not ok:
            log.error("热键注册失败（含无法识别的键名）: %s", hotkey)
            return False
        with self._lock:
            self._handlers[hotkey] = True
        log.info("热键注册成功: %s (suppress=%s)", hotkey, suppress)
        return True

    def _on_trigger(self, hotkey: str) -> None:
        log.info("热键触发: %s", hotkey)
        from .logbus import log as overlay_log
        overlay_log(f"热键触发：{hotkey}")
        self.triggered.emit(hotkey)

    def unregister(self, hotkey: str) -> None:
        hotkey = self.normalize(hotkey)
        with self._lock:
            self._handlers.pop(hotkey, None)
        _engine.unregister(hotkey)
        log.info("热键注销: %s", hotkey)

    def unregister_all(self) -> None:
        with self._lock:
            self._handlers.clear()
        _engine.unregister_all()
