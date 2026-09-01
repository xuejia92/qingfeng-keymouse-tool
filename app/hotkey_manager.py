"""keyboard 库全局热键管理。

所有热键统一注册，触发时发出 triggered(hotkey) 信号（自动排队到主线程），
由 MainWindow 的调度表分发。单键热键（如 F6）采用 suppress 拦截，
避免按键漏进其他程序；含 Ctrl/Alt/Win 的组合键不拦截。
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal



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
            try:
                import keyboard

                suppress = not any(m in hotkey for m in ("ctrl+", "alt+", "win+"))
                handler = keyboard.add_hotkey(hotkey, lambda: self._on_trigger(hotkey),
                                              suppress=suppress)
                self._handlers[hotkey] = handler
                log.info("热键注册成功: %s (suppress=%s)", hotkey, suppress)
                return True
            except Exception:
                log.exception("热键注册失败: %s", hotkey)
                return False

    def _on_trigger(self, hotkey: str) -> None:
        log.info("热键触发: %s", hotkey)
        from .logbus import log as overlay_log
        overlay_log(f"热键触发：{hotkey}")
        self.triggered.emit(hotkey)

    def unregister(self, hotkey: str) -> None:
        hotkey = self.normalize(hotkey)
        with self._lock:
            handler = self._handlers.pop(hotkey, None)
        if handler is None:
            return
        try:
            import keyboard
            keyboard.remove_hotkey(handler)
            log.info("热键注销: %s", hotkey)
        except Exception:
            log.exception("热键注销失败: %s", hotkey)

    def unregister_all(self) -> None:
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
        try:
            import keyboard
            for h in handlers:
                try:
                    keyboard.remove_hotkey(h)
                except Exception:
                    pass
            keyboard.unhook_all()
        except Exception:
            log.exception("keyboard unhook_all 失败")
