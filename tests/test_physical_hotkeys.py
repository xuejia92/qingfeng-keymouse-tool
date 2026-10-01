# -*- coding: utf-8 -*-
"""物理热键引擎（app/physical_hotkeys.py）的测试。

核心语义：
- 只认**物理按键**：injected（程序合成）的按键不更新按下状态、不触发热键；
- 组合匹配按录入时的键序（ctrl,alt,shift,win,主键）；
- suppress=True 的热键触发后要把这次按键吞掉（win32_event_filter 返回 False）；
- 修饰键自身不触发；同一引擎支持同键多回调；register/unregister 生命周期。

测试直接调引擎的 `_win32_event_filter`（用假 data 对象），不起真钩子；
生命周期类用例会真启停 pynput Listener（Windows 桌面会话下可用）。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

from app.physical_hotkeys import PhysicalHotkeyEngine, _VK_TO_NAME

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
VK_Q, VK_A, VK_CTRL, VK_F6, VK_SHIFT, VK_F1 = 0x51, 0x41, 0xA2, 0x75, 0xA0, 0x70


class _Data:
    """KBDLLHOOKSTRUCT 替身。"""

    def __init__(self, vk, injected=False):
        self.vkCode = vk
        self.flags = 0x10 if injected else 0


class _Hooked(QObject):
    """记录引擎触发（Signal 跨线程排队 → 测试里直接同步断言回调即可）。"""

    fired = Signal(str)


class TestVkMap(unittest.TestCase):
    def test_common_keys(self):
        self.assertEqual(_VK_TO_NAME[VK_Q], "q")
        self.assertEqual(_VK_TO_NAME[VK_F6], "f6")
        self.assertEqual(_VK_TO_NAME[VK_CTRL], "ctrl")
        self.assertEqual(_VK_TO_NAME[0x20], "space")
        self.assertEqual(_VK_TO_NAME[0x1B], "esc")
        self.assertEqual(_VK_TO_NAME[0x70], "f1")

    def test_numpad_digits_share_names(self):
        """小键盘数字与主键盘数字映射到同名热键是**有意**的（按哪个都该触发）；
        因此 vk→名 允许多对一，只要求每个 vk 都有名可查。"""
        for vk in (0x30, 0x60):          # 主键盘 0 / 小键盘 0
            self.assertEqual(_VK_TO_NAME[vk], "0")


class TestEngineMatching(unittest.TestCase):
    def setUp(self):
        self.eng = PhysicalHotkeyEngine()
        self.fired: list[str] = []
        self.eng.fired.connect(self.fired.append)

    def tearDown(self):
        self.eng.unregister_all()

    def _down(self, vk, injected=False):
        return self.eng._win32_event_filter(WM_KEYDOWN, _Data(vk, injected))

    def _up(self, vk, injected=False):
        return self.eng._win32_event_filter(WM_KEYUP, _Data(vk, injected))

    def test_simple_hotkey_fires_on_physical_press(self):
        self.eng.register("f6", lambda: None)
        ret = self._down(VK_F6)
        self.assertEqual(self.fired, ["f6"])
        self.assertTrue(ret)                       # 未注册 suppress → 不吞键

    def test_suppressed_hotkey_swallows_key(self):
        self.eng.register("f6", lambda: None, suppress=True)
        ret = self._down(VK_F6)
        self.assertEqual(self.fired, ["f6"])
        self.assertFalse(ret)                      # 吞掉，不漏进游戏

    def test_injected_key_is_ignored(self):
        self.eng.register("f6", lambda: None)
        self.assertTrue(self._down(VK_F6, injected=True))   # 合成键：放行不吞
        self.assertEqual(self.fired, [])                    # 也不触发

    def test_modifier_combo(self):
        seen = []
        self.eng.register("ctrl+q", lambda: seen.append(1))
        self._down(VK_CTRL)
        self._down(VK_Q)
        self.assertEqual(seen, [1])
        self._up(VK_Q)
        self._up(VK_CTRL)
        self._down(VK_Q)                            # ctrl 已松开 → 纯 q 不匹配
        self.assertEqual(seen, [1])
        self._up(VK_Q)

    def test_injected_main_with_physical_modifier_still_ignored(self):
        """用户按着 ctrl 时流程合成 q：不能触发 ctrl+q（真机事故场景）。"""
        seen = []
        self.eng.register("ctrl+q", lambda: seen.append(1))
        self._down(VK_CTRL)                         # 物理按下 ctrl
        self.assertTrue(self._down(VK_Q, injected=True))    # 流程合成的 q
        self.assertEqual(seen, [])
        self._up(VK_Q, injected=True)
        self._up(VK_CTRL)

    def test_alt_letter_combo_supported(self):
        seen = []
        self.eng.register("alt+a", lambda: seen.append(1))
        self._down(0xA4)                            # 物理按下 alt
        self._down(VK_A)
        self.assertEqual(seen, [1])
        self._up(VK_A)
        self._up(0xA4)

    def test_modifier_alone_never_fires(self):
        seen = []
        self.eng.register("ctrl", lambda: seen.append(1))
        self._down(VK_CTRL)
        self._up(VK_CTRL)
        self.assertEqual(seen, [])

    def test_same_hotkey_multiple_callbacks(self):
        hits: list[int] = []
        self.eng.register("f6", lambda: hits.append(1))
        self.eng.register("f6", lambda: hits.append(2))
        self._down(VK_F6)
        self.assertEqual(hits, [1, 2])

    def test_unregister_stops_firing(self):
        seen = []
        self.eng.register("f6", lambda: seen.append(1))
        self.eng.unregister("f6")
        self._down(VK_F6)
        self.assertEqual(seen, [])

    def test_register_rejects_unknown_key_names(self):
        self.assertFalse(self.eng.register("任意键", lambda: None))
        self.assertFalse(self.eng.register("", lambda: None))

    def test_release_removes_pressed_state(self):
        seen = []
        self.eng.register("q", lambda: seen.append(1))
        self._down(VK_Q)
        self._up(VK_Q)
        self._down(VK_Q)                            # 松开后再次按下：再次触发
        self.assertEqual(seen, [1, 1])


class TestEngineLifecycle(unittest.TestCase):
    def test_listener_starts_and_stops(self):
        eng = PhysicalHotkeyEngine()
        self.assertIsNone(eng._listener)
        eng.register("f6", lambda: None)
        self.assertIsNotNone(eng._listener)
        eng.unregister_all()
        self.assertIsNone(eng._listener)

    def test_unregister_all_clears(self):
        eng = PhysicalHotkeyEngine()
        eng.register("f6", lambda: None)
        eng.register("f7", lambda: None)
        eng.unregister_all()
        self.assertEqual(eng._handlers, {})
        self.assertEqual(eng._pressed, set())


class TestHotkeyManagerOnEngine(unittest.TestCase):
    """HotkeyManager 切到物理引擎后，注册/注销/触发链路仍然成立。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.hotkey_manager import HotkeyManager
        self.mgr = HotkeyManager()
        self.fired: list[str] = []
        self.mgr.triggered.connect(self.fired.append)

    def tearDown(self):
        self.mgr.unregister_all()

    def test_register_and_engine_state(self):
        self.assertTrue(self.mgr.register("ctrl+q"))
        self.assertTrue(self.mgr.register("ctrl+q"))       # 重复注册视为成功
        from app.physical_hotkeys import engine
        self.assertIn("ctrl+q", engine._handlers)

    def test_unregister_removes_from_engine(self):
        self.mgr.register("f6")
        self.mgr.unregister("f6")
        from app.physical_hotkeys import engine
        self.assertNotIn("f6", engine._handlers)

    def test_triggered_signal(self):
        self.mgr.register("f6")
        self.mgr._on_trigger("f6")                         # 引擎回调 → 信号
        self.assertEqual(self.fired, ["f6"])


if __name__ == "__main__":
    unittest.main()
