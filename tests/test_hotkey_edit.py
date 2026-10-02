# -*- coding: utf-8 -*-
"""热键录入框的按键处理契约（2026-10-02 code review 修复）。

`Tab` / `Backspace` 在 `keymap` 里有映射（"tab" / "backspace"），录进去之后
**不带修饰符** → `hotkey_manager` 判定 `suppress=True` → 低级钩子把全系统
这个键吞掉：

- Tab 被吞 → 焦点再也移不出录入框，设置页直接卡死；
- Backspace 被吞 → 全局删字失效。

本控件是 `readOnly`，这两个键在这里没有任何正常用途，所以直接放行给默认处理
（Tab 移焦点、Backspace 无操作），绝不允许录成热键。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from app.ui.hotkey_edit import HotkeyEdit


def _key(k, text: str = "", modifiers=Qt.NoModifier) -> QKeyEvent:
    return QKeyEvent(QEvent.KeyPress, k, modifiers, text)


class HotkeyKeyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.edit = HotkeyEdit()
        self.fired: list[str] = []
        self.edit.hotkeyChanged.connect(self.fired.append)

    def _press(self, ev: QKeyEvent):
        self.edit.keyPressEvent(ev)


class TestNavigationKeysAreNotRecordable(HotkeyKeyCase):
    def test_tab_is_not_recorded(self):
        """★ Tab 必须走默认处理（移焦点），不能录成热键。"""
        self._press(_key(Qt.Key_Tab))
        self.assertEqual(self.edit.hotkey(), "")
        self.assertEqual(self.fired, [], "Tab 不该触发 hotkeyChanged")

    def test_backspace_is_not_recorded(self):
        """Backspace 同理：录了会吞掉全局删格。"""
        self._press(_key(Qt.Key_Backspace))
        self.assertEqual(self.edit.hotkey(), "")
        self.assertEqual(self.fired, [])

    def test_tab_with_modifier_also_not_recorded(self):
        """Ctrl+Tab / Shift+Tab 同样放行（焦点在控件间移动）。"""
        self._press(_key(Qt.Key_Tab, modifiers=Qt.ControlModifier))
        self.assertEqual(self.edit.hotkey(), "")
        self._press(_key(Qt.Key_Tab, modifiers=Qt.ShiftModifier))
        self.assertEqual(self.edit.hotkey(), "")

    def test_existing_hotkey_survives_tab(self):
        """已有热键时按 Tab 不能把它改掉（否则用户只想移焦点却丢了配置）。"""
        self.edit.set_hotkey("f6")
        self._press(_key(Qt.Key_Tab))
        self.assertEqual(self.edit.hotkey(), "f6")


class TestNormalRecordingStillWorks(HotkeyKeyCase):
    def test_single_function_key_is_recorded(self):
        """单键热键（如 F6）仍然允许——这是项目的既有能力。"""
        self._press(_key(Qt.Key_F6))
        self.assertEqual(self.edit.hotkey(), "f6")
        self.assertEqual(self.fired, ["f6"])

    def test_combo_is_recorded(self):
        self._press(_key(Qt.Key_F1, modifiers=Qt.ControlModifier | Qt.ShiftModifier))
        self.assertEqual(self.edit.hotkey(), "ctrl+shift+f1")

    def test_escape_clears(self):
        self.edit.set_hotkey("f6")
        self._press(_key(Qt.Key_Escape))
        self.assertEqual(self.edit.hotkey(), "")
        self.assertEqual(self.fired, [""])


class TestSourceContract(unittest.TestCase):
    def test_navigation_keys_are_released_early(self):
        """源码级兜底：Tab/Backspace 的放行必须在 qt_key_event_to_hotkey 之前。"""
        import inspect
        src = inspect.getsource(HotkeyEdit.keyPressEvent)
        guard = src.find("Qt.Key_Tab")
        record = src.find("qt_key_event_to_hotkey")
        self.assertNotEqual(guard, -1, "keyPressEvent 里没有放行 Tab")
        self.assertLess(guard, record,
                        "放行判断必须写在录制逻辑之前，否则 Tab 仍会被录进去")


if __name__ == "__main__":
    unittest.main()
