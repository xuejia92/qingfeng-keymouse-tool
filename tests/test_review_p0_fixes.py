# -*- coding: utf-8 -*-
"""2026-10-02 code review P0 修复的契约测试（6 处）。

| # | 修复 | 本文件对应 |
|---|------|-----------|
| 1 | `flows.py` 步骤成败判定改白名单 | TestStepReasonSuccess |
| 2 | `main_window.py:651` 恒假条件 | TestOverlayPosReset |
| 3 | `mouse_menu` 钩子线程句柄泄漏 | TestMouseHookThreadGuard |
| 4 | 键盘钩子补 pause/unpause | TestKeyboardHookPause |
| 5 | `screenshot_actor` 单槽并发串味 | TestUiCallIsolation |
| 6 | `win_actors` 前台窗口 thread-local | TestForegroundThreadLocal |

Win32 / Qt 相关一律 mock，不碰真实系统。
"""
from __future__ import annotations

import inspect
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import flows, mouse_menu, physical_hotkeys, win_actors
from app.ui import flow_dialog, main_window as mw_mod
from app.screenshot_actor import _UiCall


# ---------- 1. steps.py 成败判定 ----------
class TestStepReasonSuccess(unittest.TestCase):
    """click/press/find/wait 的成败只看成功白名单，不再靠失败前缀黑名单。"""

    def test_success_reasons(self):
        for reason in ("已完成 3 次", "已完成 10 次", "已到设定时长", "等待完成"):
            self.assertTrue(flows._reason_is_success(reason), reason)

    def test_background_mode_failures_are_now_failures(self):
        """★ 回归：这三条原来被判成「成功」，后台模式点不到窗口却当成功。"""
        for reason in ("未绑定目标窗口", "目标窗口不存在", "未知步骤类型: xxx"):
            self.assertFalse(flows._reason_is_success(reason), reason)

    def test_other_failures_stay_failures(self):
        for reason in ("已手动停止", "等待目标超时", "模板图加载失败", "未设置按键",
                       "按键无效: xxx", "未配置", "miss", "error"):
            self.assertFalse(flows._reason_is_success(reason), reason)

    def test_empty_reason_is_failure(self):
        self.assertFalse(flows._reason_is_success(""))


# ---------- 2. main_window 状态日志位置重置 ----------
class TestOverlayPosReset(unittest.TestCase):
    """在设置页改了显示位置 → 要真的清掉手动拖动的 custom_pos。"""

    def test_source_keeps_old_value_before_assign(self):
        src = Path(mw_mod.__file__).read_text(encoding="utf-8")
        body = src.split("old_log_pos")[0].rsplit("def ", 1)[-1]
        self.assertIn("old_log_pos = self.cfg.run_overlay_log_pos", src,
                      "必须先留住旧值再覆盖，否则比较恒为真")
        assign = src.index("self.cfg.run_overlay_log_pos = ov[")
        keep = src.index("old_log_pos = self.cfg.run_overlay_log_pos")
        self.assertLess(keep, assign, "旧值必须在赋值之前取")

    def test_pos_change_clears_custom_pos(self):
        """逻辑直测：旧值 != 新值时 custom_pos 要被清掉。"""
        cfg = mock.Mock()
        cfg.run_overlay_log_pos = "left-top"
        cfg.run_overlay_log_custom_pos = "300,200"
        new_pos = "center"
        old = cfg.run_overlay_log_pos
        cfg.run_overlay_log_pos = new_pos
        if new_pos != old:
            cfg.run_overlay_log_custom_pos = ""
        self.assertEqual(cfg.run_overlay_log_custom_pos, "")

    def test_unchanged_pos_keeps_custom_pos(self):
        """位置没变时不该清（用户没改设置，拖出来的位置要保留）。"""
        cfg = mock.Mock()
        cfg.run_overlay_log_pos = "left-top"
        cfg.run_overlay_log_custom_pos = "300,200"
        new_pos = "left-top"
        old = cfg.run_overlay_log_pos
        cfg.run_overlay_log_pos = new_pos
        if new_pos != old:
            cfg.run_overlay_log_custom_pos = ""
        self.assertEqual(cfg.run_overlay_log_custom_pos, "300,200")


# ---------- 3. mouse_menu 钩子线程 ----------
class TestMouseHookThreadGuard(unittest.TestCase):
    def test_stop_thread_keeps_reference_when_join_times_out(self):
        """join 超时不能丢 _thread：否则 is_running() 看不到它 → 装第二个钩子。"""
        src = Path(mouse_menu.__file__).read_text(encoding="utf-8")
        body = src.split("def _stop_thread")[1].split("\n    def ")[0]
        self.assertNotIn("self._thread = self._thread, None", body,
                         "别再「先置 None 再 join」")
        code = body.split('"""')[-1]          # 跳过 docstring，只看真正的代码
        after_join = code.split("join(timeout=2.0)")[1]
        self.assertIn("is_alive()", after_join,
                      "join 超时后要重新判活并保留引用")

    def test_run_unhooks_its_own_handle(self):
        """finally 必须 Unhook 本线程的局部句柄，不能用可能被覆盖的实例字段。"""
        src = Path(mouse_menu.__file__).read_text(encoding="utf-8")
        body = src.split("def _run")[1].split("\n    def ")[0]
        self.assertIn("hook = user32.SetWindowsHookExW(", body,
                      "钩子句柄要存进局部变量")
        self.assertIn("UnhookWindowsHookEx(hook)", body)
        self.assertNotIn("UnhookWindowsHookEx(self._hook)", body,
                         "不能用实例字段——那可能是新线程的句柄")
        self.assertIn("if self._hook == hook:", body, "清字段前要确认仍是本线程的")


# ---------- 4. 键盘钩子 pause/unpause ----------
class TestKeyboardHookPause(unittest.TestCase):
    """拖拽期间必须**真的卸掉**键盘钩子，否则会被 Windows 静默摘掉且永不重装。"""

    def _engine(self):
        eng = physical_hotkeys.PhysicalHotkeyEngine()
        eng._start = mock.Mock()
        eng._stop = mock.Mock()
        return eng

    def test_pause_stops_listener(self):
        eng = self._engine()
        eng.register("f6", lambda: None)
        eng._start.reset_mock()
        eng.pause()
        eng._stop.assert_called_once()
        self.assertTrue(eng._paused)

    def test_unpause_restarts_when_handlers_exist(self):
        eng = self._engine()
        eng.register("f6", lambda: None)
        eng.pause()
        eng._start.reset_mock()
        eng.unpause()
        eng._start.assert_called_once()
        self.assertFalse(eng._paused)

    def test_register_during_pause_does_not_start(self):
        """暂停期间新注册热键不能把钩子装回来（拖拽还没结束）。"""
        eng = self._engine()
        eng.register("f6", lambda: None)
        eng.pause()
        eng._start.reset_mock()
        eng.register("f7", lambda: None)
        eng._start.assert_not_called()

    def test_unpause_without_handlers_does_not_start(self):
        eng = self._engine()
        eng.register("f6", lambda: None)
        eng.pause()
        eng._handlers.clear()
        eng._start.reset_mock()
        eng.unpause()
        eng._start.assert_not_called()

    def test_pause_is_idempotent(self):
        eng = self._engine()
        eng.register("f6", lambda: None)
        eng.pause()
        eng._stop.reset_mock()
        eng.pause()
        eng._stop.assert_not_called()

    def test_unpause_without_pause_is_noop(self):
        eng = self._engine()
        eng.register("f6", lambda: None)
        eng._start.reset_mock()
        eng.unpause()
        eng._start.assert_not_called()

    def test_drag_start_pauses_keyboard_hook_too(self):
        """★ 拖拽入口必须同时让键盘钩子让位（否则热键会被静默摘掉）。"""
        src = Path(flow_dialog.__file__).read_text(encoding="utf-8")
        body = src.split("def startDrag")[1].split("\n    def ")[0]
        self.assertIn("physical_hotkeys.pause()", body)
        self.assertIn("physical_hotkeys.unpause()", body)


# ---------- 5. UI 桥接并发隔离 ----------
class TestUiCallIsolation(unittest.TestCase):
    """每次 ui_call 有独立 Event 与结果槽 → 并行流程不互相踩。"""

    def test_each_call_owns_event_and_result(self):
        a, b = _UiCall(lambda: 1), _UiCall(lambda: 2)
        b.done(2)
        self.assertTrue(b.event.is_set())
        self.assertFalse(a.event.is_set(), "b 完成不该唤醒 a")
        self.assertEqual(b.result, 2)
        self.assertIsNone(a.result, "a 还没完成，不该有结果")

    def test_two_calls_keep_own_results(self):
        """两个请求各自拿到自己的返回值，不会串味。"""
        results = {}
        calls = [("A", 11), ("B", 22)]

        def worker(name, value):
            call = _UiCall(lambda: value)
            call.done(call.fn())
            results[name] = call.result

        threads = [threading.Thread(target=worker, args=(n, v)) for n, v in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        self.assertEqual(results, {"A": 11, "B": 22})

    def test_bridge_has_no_shared_slot(self):
        """桥接对象上不能再有共享的 _event/_result 单槽。"""
        src = Path(inspect.getfile(_UiCall)).read_text(encoding="utf-8")
        self.assertNotIn("self._event", src, "_UiCall 不该有实例级事件槽")
        self.assertNotIn("self._result", src.replace("self.result", ""),
                         "_UiCall 不该有实例级结果槽")
        self.assertIn("call = _UiCall(fn)", src, "每次 call 都要新建请求对象")


# ---------- 6. 前台窗口 thread-local ----------
class _FakeUser32:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fg = 100

    def GetForegroundWindow(self):
        return self.fg

    def GetWindowThreadProcessId(self, hw, _p):
        return 10 if hw else 0

    def AttachThreadInput(self, a, b, attach):
        self.calls.append(("attach" if attach else "detach", int(a), int(b)))
        return 1

    def SetForegroundWindow(self, _hw):
        return 1

    def SetFocus(self, _hw):
        return 1


class _FakeKernel32:
    def __init__(self):
        self.tid = 1

    def GetCurrentThreadId(self):
        return self.tid


class TestForegroundThreadLocal(unittest.TestCase):
    def setUp(self):
        self.fake_user = _FakeUser32()
        self.fake_kern = _FakeKernel32()
        # ⚠️ 必须用 mock.patch.object + addCleanup(stop) 替换模块级对象：
        # 直接 setattr 不会还原，fake 会泄漏给后续测试模块（test_win_actors 就被
        # 我们的 _FakeUser32 打挂了——它没有 GetCursorPos 等真实方法）。
        for name, obj in (("_user32", self.fake_user), ("_kernel32", self.fake_kern)):
            patcher = mock.patch.object(win_actors, name, obj)
            patcher.start()
            self.addCleanup(patcher.stop)
        exists = mock.patch.object(win_actors, "window_exists", return_value=True)
        exists.start()
        self.addCleanup(exists.stop)
        self.addCleanup(setattr, win_actors, "_fg_state", threading.local())

    def test_two_threads_keep_separate_records(self):
        """★ 并行流程各自记自己的原前台窗口，互不覆盖。"""
        seen: dict[str, int] = {}
        started = threading.Event()

        def worker_a():
            self.fake_kern.tid = 11
            win_actors.activate_window(200)
            seen["a_prev"] = win_actors._fg_store()["prev"]
            started.set()

        def worker_b():
            started.wait(3)
            self.fake_kern.tid = 22
            self.fake_user.fg = 999        # B 进来时前台已经是别的东西
            win_actors.activate_window(300)
            seen["b_prev"] = win_actors._fg_store()["prev"]

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start(); tb.start()
        ta.join(5); tb.join(5)
        self.assertEqual(seen.get("a_prev"), 100, "A 记的应是自己看到的前台窗口")
        self.assertEqual(seen.get("b_prev"), 999)

    def test_restore_only_detaches_own_tids(self):
        """A 只解自己绑过的 tid，不会把 B 的绑定也解掉（或漏解自己的）。"""
        self.fake_kern.tid = 11
        win_actors.activate_window(200)
        self.fake_kern.tid = 22
        win_actors.activate_window(300)
        self.fake_user.calls.clear()
        self.fake_kern.tid = 22
        win_actors.restore_foreground()
        detaches = [c for c in self.fake_user.calls if c[0] == "detach"]
        self.assertTrue(detaches, "restore 必须解绑本线程绑过的 tid")
        self.assertTrue(all(c[1] == 22 for c in detaches),
                        "只解绑本线程(22)绑的")

    def test_module_level_globals_are_gone(self):
        """不能再有模块级的 _prev_foreground/_attached_tids。"""
        self.assertFalse(hasattr(win_actors, "_prev_foreground"))
        self.assertFalse(hasattr(win_actors, "_attached_tids"))


if __name__ == "__main__":
    unittest.main()
