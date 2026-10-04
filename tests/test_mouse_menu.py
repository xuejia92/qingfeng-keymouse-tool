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
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import mouse_menu
from app.mouse_menu import (HC_ACTION, LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED,
                            MSLLHOOKSTRUCT, WH_MOUSE_LL, WM_LBUTTONDOWN,
                            WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEMOVE,
                            WM_RBUTTONDOWN, MouseMenuWatcher, is_injected,
                            should_suppress, should_trigger)


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
        # ⚠️ 必须是 **QApplication**（GUI），不能图省事建 QCoreApplication：
        # QApplication.instance() 会把它当成"应用已存在"，之后任何 GUI 调用
        # （QApplication.activeModalWidget / QCursor.pos / QPixmap…）都直接
        # **访问违规崩进程**（无 traceback、退出码 139）。实测踩过：先跑本文件、
        # 再跑 test_middle_menu_trigger 必崩（2026-10-04）。
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

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

    # ---------- 菜单开着期间吞掉中键（2026-10-04，修「浏览器上无法退出菜单」）----------

    def test_menu_open_swallows_middle_but_still_triggers(self):
        """菜单开着时：中键**被吞掉**（浏览器别执行默认行为），但触发信号照发。

        为什么两条都要：吞掉是为了不让浏览器"顺手开新标签页 / 关标签页 / 进自动滚动"；
        信号照发是因为**这时候的中键正是用户关闭菜单的手势**——菜单弹在浏览器上时，
        点击会被浏览器的自动滚动抢走鼠标捕获，只有全局钩子看得见的中键还能用。
        """
        self.watcher.set_menu_open(True)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 1)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONUP, 0), 1)
        self.assertEqual(self.seen, [(10, 20)], "抬起仍要触发（用来关菜单）")
        self.assertEqual(_call_hook(self.watcher, WM_MOUSEMOVE, 0), 0,
                         "只吞中键，其它鼠标事件照常放行")

    def test_menu_open_is_independent_from_user_suppress_setting(self):
        """菜单的临时开关不能覆盖用户「拦截中键」的选择（两个独立状态）。"""
        self.watcher.set_suppress(False)
        self.watcher.set_menu_open(True)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 1)
        self.assertFalse(self.watcher.suppress(), "用户设置不该被菜单开关改掉")
        self.watcher.set_menu_open(False)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 0,
                         "菜单收起后中键恢复放行")

    def test_menu_open_does_not_affect_injected_clicks(self):
        """合成点击本来就忽略，与菜单开关无关。"""
        self.watcher.set_menu_open(True)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, LLMHF_INJECTED), 0)
        self.assertEqual(self.seen, [])

    def test_button_press_is_always_reported(self):
        """★ 任意鼠标键按下**一律上报**（不再拿 menu_open 当门）。

        以前只在"菜单开着"时才发这个信号，那个门只是个优化，但一旦它判断有误，
        "点外面关菜单"整条链路就直接哑掉、还没有任何痕迹（用户反馈过
        "热键打开的菜单点外面不关"，而两条触发路径是同一个函数 —— 门是首要嫌疑）。
        现在由主窗口自己判断要不要理会。
        """
        seen = []
        self.watcher.menuButtonPressed.connect(lambda x, y, b: seen.append((x, y, b)))
        self.assertEqual(_call_hook(self.watcher, WM_LBUTTONDOWN, 0), 0)
        self.assertEqual(seen, [(10, 20, "left")], "菜单没开时也要上报")
        self.watcher.set_menu_open(True)
        self.assertEqual(_call_hook(self.watcher, WM_RBUTTONDOWN, 0), 0)
        self.assertEqual(_call_hook(self.watcher, WM_MBUTTONDOWN, 0), 1,
                         "中键在菜单期间仍要吞掉")
        self.assertEqual(seen[-1], (10, 20, "middle"))

    def test_menu_open_ignores_injected_button_presses(self):
        """程序合成的按键不该被当成"用户点了外面"。"""
        seen = []
        self.watcher.menuButtonPressed.connect(lambda x, y, b: seen.append((x, y, b)))
        self.watcher.set_menu_open(True)
        _call_hook(self.watcher, WM_LBUTTONDOWN, LLMHF_INJECTED)
        self.assertEqual(seen, [])

    def test_button_press_carries_button_name(self):
        """按下事件要带上按钮名：中键是"开关"语义，主窗口要单独处理。"""
        seen = []
        self.watcher.menuButtonPressed.connect(lambda x, y, b: seen.append(b))
        self.watcher.set_menu_open(True)
        _call_hook(self.watcher, WM_LBUTTONDOWN, 0)
        _call_hook(self.watcher, WM_RBUTTONDOWN, 0)
        _call_hook(self.watcher, WM_MBUTTONDOWN, 0)
        self.assertEqual(seen, ["left", "right", "middle"])

    # ---------- 钩子体检（被系统静默摘掉时自愈）----------

    def _arm_watchdog(self, last_pos=(10, 10), silence=5.0):
        self.watcher._enabled = True
        self.watcher._suspended = False
        self.watcher._started_ok = True
        self.watcher._last_event_pos = last_pos
        self.watcher._last_event_at = time.monotonic() - silence

    def test_watchdog_reinstalls_after_two_suspicious_checks(self):
        """★ 光标动了却收不到事件 → 钩子多半被系统静默摘掉了，重装它。

        低层钩子回调超时会被 Windows 直接卸载且**不通知**，之后中键唤菜单、
        "点外面关菜单"全都失灵，而 is_running() 还是 True —— 用户看到的就是
        "有时候不生效"。连续两次可疑才动手，避免误伤。
        """
        self._arm_watchdog()
        with mock.patch.object(mouse_menu, "cursor_position",
                               return_value=(500, 500)), \
                mock.patch.object(self.watcher, "_reinstall") as reinstall:
            self.watcher._check_hook_alive()
            self.assertFalse(reinstall.called, "第一次可疑先不动手")
            self.watcher._check_hook_alive()
        self.assertTrue(reinstall.called, "连续两次可疑就该重装")

    def test_watchdog_quiet_when_cursor_matches(self):
        """光标位置和最后收到的事件一致 → 钩子活着，什么都不做。"""
        self._arm_watchdog(last_pos=(500, 500))
        with mock.patch.object(mouse_menu, "cursor_position",
                               return_value=(502, 501)), \
                mock.patch.object(self.watcher, "_reinstall") as reinstall:
            self.watcher._check_hook_alive()
            self.watcher._check_hook_alive()
        self.assertFalse(reinstall.called)

    def test_watchdog_quiet_when_events_are_recent(self):
        """光标对不上、但**刚收到过事件** → 说明不了什么，不能重装。"""
        self._arm_watchdog(last_pos=(10, 10), silence=0.0)
        with mock.patch.object(mouse_menu, "cursor_position",
                               return_value=(500, 500)), \
                mock.patch.object(self.watcher, "_reinstall") as reinstall:
            self.watcher._check_hook_alive()
            self.watcher._check_hook_alive()
        self.assertFalse(reinstall.called)

    def test_watchdog_skips_when_not_running_or_suspended(self):
        self._arm_watchdog()
        self.watcher._started_ok = False
        with mock.patch.object(mouse_menu, "cursor_position",
                               return_value=(500, 500)), \
                mock.patch.object(self.watcher, "_reinstall") as reinstall:
            self.watcher._check_hook_alive()
        self.assertFalse(reinstall.called)

    def test_mouse_move_updates_watchdog_state(self):
        """移动事件最热，也必须记录位置（否则体检会把活着的钩子当死掉）。"""
        self.watcher._last_event_pos = None
        _call_hook(self.watcher, WM_MOUSEMOVE, 0, 321, 222)
        self.assertEqual(self.watcher._last_event_pos, (321, 222))
        self.assertGreater(self.watcher._last_event_at, 0.0)


@unittest.skipUnless(sys.platform == "win32", "低层鼠标钩子仅 Windows")
class TestLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ⚠️ 必须是 **QApplication**（GUI），不能图省事建 QCoreApplication：
        # QApplication.instance() 会把它当成"应用已存在"，之后任何 GUI 调用
        # （QApplication.activeModalWidget / QCursor.pos / QPixmap…）都直接
        # **访问违规崩进程**（无 traceback、退出码 139）。实测踩过：先跑本文件、
        # 再跑 test_middle_menu_trigger 必崩（2026-10-04）。
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

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
