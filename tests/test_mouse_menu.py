"""app/mouse_menu.py 全局中键监听器的测试。

重点钉死三件事：
1) 触发条件是「真实的、非注入的中键抬起」——按下不触发、合成点击不触发；
2) 拦截（suppress）只作用于中键，其它鼠标事件一律放行；
3) 钩子回调绝不抛异常（ctypes 回调抛异常会返回 0，静默吞掉鼠标事件）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.mouse_menu import (HC_ACTION, LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED,
                            MSLLHOOKSTRUCT, WH_MOUSE_LL, WM_MBUTTONDOWN,
                            WM_MBUTTONUP, WM_MOUSEMOVE, MouseMenuWatcher,
                            is_injected, should_suppress, should_trigger)


def _call_hook(watcher: MouseMenuWatcher, msg: int, flags: int,
               x: int = 10, y: int = 20) -> int:
    """构造一个 MSLLHOOKSTRUCT 直接喂给钩子回调，返回回调的返回值。"""
    data = MSLLHOOKSTRUCT()
    data.pt.x, data.pt.y = x, y
    data.flags = flags
    return watcher._hook_proc(HC_ACTION, msg, ctypes.addressof(data))


class TestConstants(unittest.TestCase):
    def test_win32_constant_values(self):
        """常量必须与 Win32 头文件一致，写错会静默失效。"""
        self.assertEqual(WH_MOUSE_LL, 14)
        self.assertEqual(HC_ACTION, 0)
        self.assertEqual(WM_MBUTTONDOWN, 0x0207)
        self.assertEqual(WM_MBUTTONUP, 0x0208)
        self.assertEqual(LLMHF_INJECTED, 0x00000001)
        self.assertEqual(LLMHF_LOWER_IL_INJECTED, 0x00000002)

    def test_struct_layout_is_pointer_width(self):
        """dwExtraInfo 是 ULONG_PTR，必须按指针宽度对齐（否则解出脏标志）。"""
        ptr = ctypes.sizeof(ctypes.c_void_p)
        expected = 32 if ptr == 8 else 24      # 8/4+4+4+pad/4 + ULONG_PTR
        self.assertEqual(ctypes.sizeof(MSLLHOOKSTRUCT), expected)
        # dwExtraInfo 必须是最后一个字段且按其自身宽度对齐
        self.assertEqual(MSLLHOOKSTRUCT.dwExtraInfo.offset % ptr, 0)
        self.assertEqual(ctypes.sizeof(MSLLHOOKSTRUCT) - MSLLHOOKSTRUCT.dwExtraInfo.offset,
                         ptr)


class TestPurePredicates(unittest.TestCase):
    def test_is_injected(self):
        self.assertFalse(is_injected(0))
        self.assertTrue(is_injected(LLMHF_INJECTED))
        self.assertTrue(is_injected(LLMHF_LOWER_IL_INJECTED))
        self.assertTrue(is_injected(LLMHF_INJECTED | 0x10))   # 与其它标志共存

    def test_trigger_only_on_real_middle_up(self):
        self.assertTrue(should_trigger(WM_MBUTTONUP, 0))
        self.assertFalse(should_trigger(WM_MBUTTONDOWN, 0))          # 按下不触发
        self.assertFalse(should_trigger(WM_MBUTTONUP, LLMHF_INJECTED))
        self.assertFalse(should_trigger(WM_MOUSEMOVE, 0))

    def test_suppress_only_real_middle_events(self):
        self.assertTrue(should_suppress(WM_MBUTTONDOWN, 0))
        self.assertTrue(should_suppress(WM_MBUTTONUP, 0))
        self.assertFalse(should_suppress(WM_MOUSEMOVE, 0))           # 不动其它事件
        self.assertFalse(should_suppress(WM_MBUTTONDOWN, LLMHF_INJECTED))


class TestHookProc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.watcher = MouseMenuWatcher()
        self.seen: list[tuple[int, int]] = []
        self.watcher.middleClicked.connect(lambda x, y: self.seen.append((x, y)))

    def test_real_middle_up_emits_and_passes_through(self):
        ret = _call_hook(self.watcher, WM_MBUTTONUP, 0, 111, 222)
        self.assertEqual(self.seen, [(111, 222)])
        self.assertEqual(ret, 0)          # 未开拦截：放行

    def test_injected_click_ignored(self):
        """本工具合成的中键点击必须被忽略，否则流程点中键会形成反馈环。"""
        _call_hook(self.watcher, WM_MBUTTONUP, LLMHF_INJECTED)
        _call_hook(self.watcher, WM_MBUTTONDOWN, LLMHF_LOWER_IL_INJECTED)
        self.assertEqual(self.seen, [])

    def test_middle_down_does_not_emit(self):
        _call_hook(self.watcher, WM_MBUTTONDOWN, 0)
        self.assertEqual(self.seen, [])

    def test_non_middle_event_passes_through(self):
        ret = _call_hook(self.watcher, WM_MOUSEMOVE, 0)
        self.assertEqual(self.seen, [])
        self.assertEqual(ret, 0)

    def test_suppress_swallows_only_middle(self):
        self.watcher.set_suppress(True)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 1)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONUP, 0), 1)
        self.assertEqual(self.seen, [(10, 20)])        # 抬起仍然触发菜单
        self.assertEqual(_call_hook(self.watcher, WM_MOUSEMOVE, 0), 0)   # 其它鼠标事件放行

    def test_suppress_off_passes_middle_through(self):
        self.watcher.set_suppress(False)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 0)

    def test_suppress_can_toggle_at_runtime(self):
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 0)
        self.watcher.set_suppress(True)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 1)
        self.watcher.set_suppress(False)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 0)

    def test_callback_never_raises_on_bad_payload(self):
        """lParam 无效也不能抛异常（ctypes 回调抛异常 = 静默吞掉鼠标事件）。"""
        try:
            self.watcher._hook_proc(HC_ACTION, WM_MBUTTONUP, 0)
        except Exception as exc:      # pragma: no cover - 失败时给出可读信息
            self.fail(f"钩子回调不应抛异常: {exc!r}")


@unittest.skipUnless(sys.platform == "win32", "低层鼠标钩子仅 Windows")
class TestLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QCoreApplication
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_start_stop_restart(self):
        watcher = MouseMenuWatcher()
        self.assertFalse(watcher.is_running())
        self.assertTrue(watcher.start(), "低层鼠标钩子装载失败")
        self.assertTrue(watcher.is_running())
        watcher.stop()
        self.assertFalse(watcher.is_running())
        self.assertTrue(watcher.start())      # 可重复启动
        watcher.stop()
        self.assertFalse(watcher.is_running())

    def test_stop_without_start_is_noop(self):
        watcher = MouseMenuWatcher()
        watcher.stop()                        # 不应抛异常
        self.assertFalse(watcher.is_running())


class TestGilSwitchInterval(unittest.TestCase):
    """装钩子必须先把 GIL 切换间隔降下来。

    低层鼠标钩子回调是 Python，由系统在装载线程里同步调用——Windows 会等它
    返回才继续处理鼠标输入。GIL 默认 5ms 才强制切换一次且交接不公平，主线程
    一忙钩子线程就要等满 5ms，每条鼠标事件被拖慢 → 输入积压 → 拖动卡顿。
    实测降到 1ms：投递延迟 16ms → 0ms，采样数 44 → 872（= 空载基线）。
    """

    def setUp(self):
        self._orig = sys.getswitchinterval()

    def tearDown(self):
        sys.setswitchinterval(self._orig)

    def test_lowers_interval_to_1ms(self):
        from app.mouse_menu import lower_gil_switch_interval
        sys.setswitchinterval(0.005)
        lower_gil_switch_interval()
        self.assertAlmostEqual(sys.getswitchinterval(), 0.001, places=6)

    def test_never_raises_the_interval(self):
        """已经是更小的值时不许调高（幂等、只下调）。"""
        from app.mouse_menu import lower_gil_switch_interval
        sys.setswitchinterval(0.0002)
        lower_gil_switch_interval()
        self.assertAlmostEqual(sys.getswitchinterval(), 0.0002, places=6)

    def test_idempotent(self):
        from app.mouse_menu import lower_gil_switch_interval
        lower_gil_switch_interval()
        first = sys.getswitchinterval()
        lower_gil_switch_interval()
        self.assertEqual(sys.getswitchinterval(), first)

    def test_start_applies_it(self):
        """start() 装钩子前必须调用它（钩子没装成功也要先降下来）。"""
        from app.mouse_menu import MouseMenuWatcher, lower_gil_switch_interval
        sys.setswitchinterval(0.005)
        watcher = MouseMenuWatcher()
        watcher._ready = mock.Mock()          # 别真等 3 秒装载超时
        with mock.patch.object(MouseMenuWatcher, "_run", lambda self: None), \
                mock.patch.object(MouseMenuWatcher, "is_running", lambda self: False), \
                mock.patch("app.mouse_menu.lower_gil_switch_interval",
                           wraps=lower_gil_switch_interval) as spy:
            watcher.start()
            spy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
