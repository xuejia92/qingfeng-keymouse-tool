"""热键录入控件：点击后按下组合键即完成录制，Esc 清除。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit, QMessageBox

from ..keymap import hotkey_display, qt_key_event_to_hotkey


class HotkeyEdit(QLineEdit):
    hotkeyChanged = Signal(str)  # keyboard 库小写格式；清除时发 ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignCenter)
        self._hotkey = ""
        self._conflict_checker = None   # callable(candidate) -> 冲突归属名或 None
        self._refresh()
        self.setPlaceholderText("点击后按下快捷键")

    def hotkey(self) -> str:
        return self._hotkey

    def set_conflict_checker(self, checker) -> None:
        """设置冲突校验器：checker(candidate) 返回冲突归属名（None=无冲突）。

        有冲突时拒绝本次录入并弹窗提示，热键保持原值。
        """
        self._conflict_checker = checker

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
            if self._conflict_checker is not None:
                owner = self._conflict_checker(hk)
                if owner:
                    QMessageBox.warning(
                        self, "热键冲突",
                        f"该热键已被「{owner}」占用，请换一个组合。")
                    self._refresh()          # 恢复原值显示
                    self.clearFocus()
                    return
            self._hotkey = hk
            self._refresh()
            self.clearFocus()
            self.hotkeyChanged.emit(hk)
        # 纯修饰键按下时不提交，继续等待完整组合
