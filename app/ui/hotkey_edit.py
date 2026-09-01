"""热键录入控件：点击后按下组合键即完成录制，Esc 清除。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit

from ..keymap import hotkey_display, qt_key_event_to_hotkey


class HotkeyEdit(QLineEdit):
    hotkeyChanged = Signal(str)  # keyboard 库小写格式；清除时发 ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self._hotkey = ""
        self._refresh()
        self.setPlaceholderText("点击后按下快捷键")

    def hotkey(self) -> str:
        return self._hotkey

    def set_hotkey(self, hk: str) -> None:
        self._hotkey = (hk or "").strip().lower()
        self._refresh()

    def _refresh(self) -> None:
        self.setText(hotkey_display(self._hotkey))

    def mousePressEvent(self, ev) -> None:
        super().mousePressEvent(ev)
        self.setText("按下快捷键… (Esc 清除)")
        self.setFocus()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Escape:
            changed = self._hotkey != ""
            self._hotkey = ""
            self._refresh()
            self.clearFocus()
            if changed:
                self.hotkeyChanged.emit("")
            return
        hk = qt_key_event_to_hotkey(ev)
        if hk:
            self._hotkey = hk
            self._refresh()
            self.clearFocus()
            self.hotkeyChanged.emit(hk)
        # 纯修饰键按下时不提交，继续等待完整组合
