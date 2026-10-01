# -*- coding: utf-8 -*-
"""全局「按 Esc 取消」监听（app/cancel_key.py）的测试。

替身 keyboard 库（`mock.patch.dict("sys.modules", {"keyboard": _FakeKeyboard})`）——
测试机上绝不能真挂一个全局键盘钩子，否则跑测试时用户按 Esc 会莫名其妙被吃掉或触发。

这个模块存在的意义就是几条「写错了会出大事」的约束，用例也围绕它们：
· **不能 suppress**：一 suppress，全系统的 Esc 都被吞掉（用户的弹窗、游戏全废）。
· **不能 unhook_all**：那是把程序里所有全局热键（显示/隐藏、紧急停止、连点…）一起干掉。
· **同一个键只能向库挂一次**：挂两次会让库在注销时 KeyError，并把全局钩子漏在系统里
  （真机验证过），所以本模块自己记引用计数、只挂一个钩子。
· **回调异常必须就地吞掉**：钩子线程崩了是系统级事故，不能把异常抛回去。
· **任何失败都只降级成 False**：少一个取消方式可以，把关机流程判失败不行。
"""
from __future__ import annotations

import unittest
from unittest import mock

import app.cancel_key as cancel_key_mod
import app.hotkey_policy  # noqa: F401  提前导入：延迟首次导入时 Shiboken 会打一行提示
from app.cancel_key import ESC_KEY, EscListener


class _FakeKeyboard:
    """替身 keyboard 库：只实现我们真正用到的那两个接口。"""

    def __init__(self, fail_add: bool = False, fail_remove: bool = False):
        self.calls: list[tuple] = []
        self.registrations: list[tuple] = []   # [(key, callback, handler)]
        self.fail_add = fail_add
        self.fail_remove = fail_remove

    def on_press_key(self, key, callback, suppress=False):
        self.calls.append(("on_press_key", key, suppress))
        if self.fail_add:
            raise RuntimeError("注册失败（模拟无权限）")
        handler = object()
        self.registrations.append((key, callback, handler))
        return handler

    def unhook(self, handler):
        self.calls.append(("unhook", handler))
        if self.fail_remove:
            raise RuntimeError("注销失败")
        self.registrations = [r for r in self.registrations if r[2] is not handler]

    # ---------- 不该被调到的接口：调到就该让用例炸 ----------

    def add_hotkey(self, *a, **kw):        # pragma: no cover
        raise AssertionError(
            "该用 on_press_key：add_hotkey 对同一个键会覆盖 handler，注销时 KeyError")

    def unhook_all(self):                  # pragma: no cover
        raise AssertionError("绝不能调用 unhook_all：会干掉程序里其它全局热键")

    def block_key(self, *a, **kw):         # pragma: no cover
        raise AssertionError("绝不能把 Esc 拉黑：那是系统级污染")


class TestEscListener(unittest.TestCase):
    def setUp(self):
        self.fired = 0

        def on_esc():
            self.fired += 1

        self.on_esc = on_esc
        self.fake = _FakeKeyboard()
        # 模块级状态是全局的：先清空，跑完再还回去，免得用例之间串味
        self._saved = (cancel_key_mod._listeners[:], cancel_key_mod._unregister)
        cancel_key_mod._listeners.clear()
        cancel_key_mod._unregister = None
        self.addCleanup(self._restore_state)

        patcher = mock.patch.dict("sys.modules", {"keyboard": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_state(self):
        listeners, unregister = self._saved
        cancel_key_mod._listeners[:] = listeners
        cancel_key_mod._unregister = unregister

    def _press(self) -> None:
        """模拟用户按下 Esc：调替身库里登记的回调（真库也是这么调的）。"""
        for key, callback, _ in list(self.fake.registrations):
            if key == ESC_KEY:
                callback()

    # ---------- 注册 ----------

    def test_registers_esc_without_suppressing(self):
        """只监听不拦截：suppress 必须显式为 False（项目里单键热键默认 True，不能照搬）。"""
        listener = EscListener(self.on_esc)
        self.assertTrue(listener.start())
        self.assertEqual(self.fake.calls, [("on_press_key", ESC_KEY, False)])
        self.assertEqual(ESC_KEY, "esc")     # 与 keymap / hotkey_manager 的键名一致
        self.assertTrue(listener.active())

    def test_press_fires_callback(self):
        listener = EscListener(self.on_esc)
        listener.start()
        self._press()
        self.assertEqual(self.fired, 1)
        self._press()
        self.assertEqual(self.fired, 2)
        listener.stop()

    def test_callback_exception_is_swallowed(self):
        """回调跑在键盘钩子线程里：它抛异常等于把钩子线程掀翻，必须就地吞掉。"""
        def boom():
            raise RuntimeError("炸了")

        listener = EscListener(boom)
        listener.start()
        with self.assertLogs("app.cancel_key", level="ERROR") as cm:
            self._press()                   # 不抛出去即为通过
        self.assertIn("Esc 取消回调出错", "\n".join(cm.output))
        listener.stop()

    def test_missing_library_degrades_to_false(self):
        """没装 keyboard 库：返回 False，且不抛异常（少个取消方式而已）。"""
        with mock.patch.dict("sys.modules", {"keyboard": None}):
            listener = EscListener(self.on_esc)
            with self.assertLogs("app.cancel_key", level="WARNING") as cm:
                self.assertFalse(listener.start())
            self.assertIn("未安装 keyboard 库", "\n".join(cm.output))
            self.assertFalse(listener.active())
            listener.stop()                 # 没挂上也得能安全收尾
        self.assertEqual(self.fake.calls, [])

    def test_registration_failure_degrades_to_false(self):
        self.fake.fail_add = True
        listener = EscListener(self.on_esc)
        with self.assertLogs("app.cancel_key", level="ERROR") as cm:
            self.assertFalse(listener.start())
        self.assertIn("注册 Esc 取消监听失败", "\n".join(cm.output))
        self.assertFalse(listener.active())
        self.assertEqual(self.fake.registrations, [])

    # ---------- 注销 ----------

    def test_stop_unregisters_the_hook(self):
        """注销只摘自己那一个：绝不能 unhook_all（会连累程序里其它全局热键）。"""
        listener = EscListener(self.on_esc)
        with self.assertLogs("app.cancel_key", level="INFO") as cm:
            listener.start()
            self.assertIn("已开启 Esc 取消监听", "\n".join(cm.output))
        handler = self.fake.registrations[0][2]
        with self.assertLogs("app.cancel_key", level="INFO") as cm:
            listener.stop()
            self.assertIn("已关闭 Esc 取消监听", "\n".join(cm.output))
        self.assertEqual(self.fake.calls[-1], ("unhook", handler))
        self.assertEqual(self.fake.registrations, [])
        self.assertFalse(listener.active())
        self._press()                       # 摘掉后再按，不该再触发
        self.assertEqual(self.fired, 0)

    def test_stop_is_idempotent_and_safe_without_start(self):
        listener = EscListener(self.on_esc)
        listener.stop()                     # 没 start 过
        self.assertEqual(self.fake.calls, [])
        listener.start()
        listener.stop()
        listener.stop()                     # 再摘一次
        unregisters = [c for c in self.fake.calls if c[0] == "unhook"]
        self.assertEqual(len(unregisters), 1)

    def test_removal_failure_is_swallowed(self):
        listener = EscListener(self.on_esc)
        listener.start()
        self.fake.fail_remove = True
        with self.assertLogs("app.cancel_key", level="ERROR") as cm:
            listener.stop()                 # 不抛出去即为通过
        self.assertIn("注销 Esc 取消监听失败", "\n".join(cm.output))
        self.assertFalse(listener.active())

    # ---------- 多个监听者 ----------

    def test_two_listeners_share_a_single_hook(self):
        """**同一个键只向库挂一次**：挂两次会让库在注销时 KeyError 并把钩子漏在系统里。"""
        other_fired = []
        first = EscListener(self.on_esc)
        second = EscListener(lambda: other_fired.append(1))
        self.assertTrue(first.start())
        self.assertTrue(second.start())
        self.assertEqual(len(self.fake.registrations), 1)
        self._press()
        self.assertEqual((self.fired, len(other_fired)), (1, 1))

    def test_unregistering_one_keeps_the_others_listening(self):
        other_fired = []
        first = EscListener(self.on_esc)
        second = EscListener(lambda: other_fired.append(1))
        first.start()
        second.start()
        self._press()
        self.assertEqual((self.fired, len(other_fired)), (1, 1))
        first.stop()
        self.assertEqual(len(self.fake.registrations), 1)   # 钩子还在：还有人要听
        self._press()
        self.assertEqual((self.fired, len(other_fired)), (1, 2))
        second.stop()
        self.assertEqual(self.fake.registrations, [])       # 最后一个走了才摘钩子
        self._press()
        self.assertEqual((self.fired, len(other_fired)), (1, 2))

    # ---------- with 语义 ----------

    def test_context_manager_yields_flag_and_unregisters(self):
        with EscListener(self.on_esc) as ok:
            self.assertTrue(ok)
            self._press()
        self.assertEqual(self.fired, 1)
        self.assertEqual(self.fake.registrations, [])

    def test_context_manager_unregisters_on_exception(self):
        """with 里出事也必须摘干净：漏一个全局 Esc 钩子在系统里，后患无穷。"""
        with self.assertRaises(ValueError):
            with EscListener(self.on_esc):
                raise ValueError("流程里炸了")
        self.assertEqual(self.fake.registrations, [])

    def test_enter_is_bulletproof(self):
        """即使 start 意外抛异常，也只能退化成 False，不能把调用方的流程掀翻。"""
        with mock.patch.object(EscListener, "start",
                               side_effect=RuntimeError("意外")):
            with self.assertLogs("app.cancel_key", level="ERROR") as cm:
                with EscListener(self.on_esc) as ok:
                    self.assertFalse(ok)
            self.assertIn("开启 Esc 取消监听异常", "\n".join(cm.output))
        self.assertEqual(self.fake.calls, [])


class TestHotkeyConflictWarning(unittest.TestCase):
    """Esc 已被别的热键占用时给出提示（否则用户会奇怪「按 Esc 怎么还干了别的」）。"""

    def setUp(self):
        self.fake = _FakeKeyboard()
        self._saved = (cancel_key_mod._listeners[:], cancel_key_mod._unregister)
        cancel_key_mod._listeners.clear()
        cancel_key_mod._unregister = None
        self.addCleanup(lambda: (cancel_key_mod._listeners.clear(),
                                 setattr(cancel_key_mod, "_unregister",
                                         self._saved[1])))
        patcher = mock.patch.dict("sys.modules", {"keyboard": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_warns_when_esc_is_taken(self):
        with mock.patch("app.hotkey_policy.check", return_value="紧急停止"):
            listener = EscListener(lambda: None)
            with self.assertLogs("app.cancel_key", level="WARNING") as cm:
                listener.start()
            self.assertIn("紧急停止", "\n".join(cm.output))
            listener.stop()

    def test_quiet_when_esc_is_free(self):
        with mock.patch("app.hotkey_policy.check", return_value=None):
            listener = EscListener(lambda: None)
            listener.start()
            listener.stop()

    def test_conflict_check_failure_is_ignored(self):
        with mock.patch("app.hotkey_policy.check", side_effect=RuntimeError("没配置")):
            listener = EscListener(lambda: None)
            self.assertTrue(listener.start())    # 查不了也照常监听
            listener.stop()


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
