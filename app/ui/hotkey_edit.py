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
        # Tab / Backspace 必须走默认处理，不能录成热键：
        # 它们在 keymap 里有映射（"tab"/"backspace"），录进去后**没有修饰符**
        # → hotkey_manager 判定 suppress=True → 低级钩子把全系统的这个键吞掉；
        # 而 Tab 被吞后焦点再也移不出这个控件（设置页直接卡死），Backspace 被吞
        # 则全局删字失效（2026-10-02 review）。本控件是 readOnly，这两个键在此
        # 也没有正常用途，放行最安全。
        if ev.key() in (Qt.Key_Tab, Qt.Key_Backspace):
            super().keyPressEvent(ev)
            return
        hk = qt_key_event_to_hotkey(ev)
        if hk:
            if self._conflict_checker is not None:
                reason = self._conflict_checker(hk)
                if reason:
                    # reason 可能是「该热键已被 X 占用」，也可能是
                    # 「Alt+字母在 Windows 上收不到」这类硬规则提示，直接展示
                    QMessageBox.warning(self, "热键不可用", str(reason))
                    self._refresh()          # 恢复原值显示
                    self.clearFocus()
                    return
            self._hotkey = hk
            self._refresh()
            self.clearFocus()
            self.hotkeyChanged.emit(hk)
        # 纯修饰键按下时不提交，继续等待完整组合
