"""app/instance_lock.py 的锁信息解析测试。

只测不需要 Qt 事件循环的纯函数。_extract_pid 的顺序陷阱是这里最值得钉死的一处：
PySide6 实测返回 (pid, hostname, appname)，Qt 文档写的是 (hostname, pid, appname)，
一旦有人「照文档改成按下标取」，陈旧锁就会重新变成死锁。
"""
from __future__ import annotations

import os
import sys
import unittest

from app.instance_lock import (_extract_pid, _pid_exists, _process_is_self,
                               _remove_lock_file, try_acquire)


class TestExtractPid(unittest.TestCase):
    def test_pynput_order_pid_first(self):
        self.assertEqual(_extract_pid((1234, "DESKTOP-ABC", "清风自动化键鼠工具")), 1234)

    def test_qt_doc_order_pid_second(self):
        """同一个值按文档顺序传进来，也必须能取出 pid。"""
        self.assertEqual(_extract_pid(("DESKTOP-ABC", 1234, "清风自动化键鼠工具")), 1234)

    def test_pid_last(self):
        self.assertEqual(_extract_pid(("DESKTOP-ABC", "app", 4321)), 4321)

    def test_list_input(self):
        self.assertEqual(_extract_pid([5678, "host", "app"]), 5678)

    def test_no_integer_returns_none(self):
        self.assertIsNone(_extract_pid(("host", "app")))
        self.assertIsNone(_extract_pid(()))
        self.assertIsNone(_extract_pid(None))

    def test_ignores_zero_and_negative(self):
        self.assertIsNone(_extract_pid((0, "host", "app")))
        self.assertIsNone(_extract_pid((-1, "host", "app")))

    def test_bool_is_not_pid(self):
        """bool 是 int 的子类，True 会被当成 pid=1——必须排除。"""
        self.assertIsNone(_extract_pid((True, "host", "app")))


class TestPidExists(unittest.TestCase):
    def test_current_process_exists(self):
        self.assertTrue(_pid_exists(os.getpid()))

    def test_invalid_pids(self):
        for bad in (0, -1, None):
            self.assertFalse(_pid_exists(bad))

    @unittest.skipUnless(sys.platform == "win32", "Windows 专用探测路径")
    def test_implausible_pid_absent(self):
        """取一个极大 PID（几乎不可能被分配），应当判定为不存在。"""
        self.assertFalse(_pid_exists(0x7FFF_FFFE))


class TestProcessIsSelf(unittest.TestCase):
    def test_current_process_counts_as_self(self):
        self.assertTrue(_process_is_self(os.getpid()))

    def test_dead_pid_is_not_self(self):
        self.assertFalse(_process_is_self(0x7FFF_FFFE))

    @unittest.skipUnless(sys.platform == "win32", "依赖 exe 名比对")
    def test_other_program_is_not_self(self):
        """找一个不是本程序解释器的活进程，必须判定为「不是自己」。

        这条的意义：源码运行时 _SELF_EXE 是 python.exe，如果锁是打包版 exe
        （或别的启动方式）留下的，光看"进程活着"会误判成自己人。
        """
        from app.instance_lock import _SELF_EXE, _pid_exe_name
        import ctypes

        buf = (ctypes.c_uint * 8192)()
        need = ctypes.c_uint()
        ctypes.windll.psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf),
                                          ctypes.byref(need))
        other = None
        for pid in buf[:need.value // ctypes.sizeof(ctypes.c_uint)]:
            name = (_pid_exe_name(pid) or "").lower()
            if name and _SELF_EXE not in name and "system idle" not in name:
                other = pid
                break
        if other is None:
            self.skipTest("没找到可对照的其他进程")
        self.assertFalse(_process_is_self(other))


class TestTryAcquire(unittest.TestCase):
    """try_acquire 需要 QCoreApplication 才能用 QLockFile。"""

    @classmethod
    def setUpClass(cls):
        global _APP
        from PySide6.QtCore import QCoreApplication

        _APP = QCoreApplication.instance() or QCoreApplication([])

    def test_fresh_path_acquires(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            lock, holder = try_acquire(os.path.join(d, "t.lock"))
            self.assertIsNotNone(lock)
            self.assertIsNone(holder)
            if lock is not None:
                lock.unlock()

    def test_self_held_lock_reports_own_pid(self):
        """同一进程已经占着锁时，必须返回自己的 PID，让调用方弹「强制启动」。"""
        import tempfile

        from PySide6.QtCore import QLockFile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.lock")
            first = QLockFile(path)
            self.assertTrue(first.tryLock(0))
            try:
                lock, holder = try_acquire(path)
                self.assertIsNone(lock)
                self.assertEqual(holder, os.getpid())
            finally:
                first.unlock()

    def test_hand_written_lock_with_dead_pid_is_taken_over(self):
        """锁里记着一个不存在的 PID：应当能接管，而不是报错退出。"""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.lock")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{0x7FFF_FFFE}\n某程序\nDESKTOP-TEST\n\n")
            lock, holder = try_acquire(path)
            self.assertIsNotNone(lock)
            self.assertIsNone(holder)
            if lock is not None:
                lock.unlock()

    def test_repeated_acquire_does_not_hang(self):
        """拿不到锁时必须及时返回（内部最多重试约 1 秒），不能把启动卡住。"""
        import tempfile
        import time

        from PySide6.QtCore import QLockFile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.lock")
            first = QLockFile(path)
            self.assertTrue(first.tryLock(0))
            try:
                t0 = time.monotonic()
                try_acquire(path)
                self.assertLess(time.monotonic() - t0, 5.0)
            finally:
                first.unlock()


class TestRemoveLockFile(unittest.TestCase):
    def test_removes_and_tolerates_missing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "test.lock")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            self.assertTrue(_remove_lock_file(p))
            self.assertFalse(os.path.exists(p))
            self.assertTrue(_remove_lock_file(p))   # 文件已不在，也应视为成功


class TestShowRequest(unittest.TestCase):
    """二次启动置前：广播消息与过滤器（Windows 消息机制，需 QCoreApplication）。"""

    @classmethod
    def setUpClass(cls):
        global _APP
        from PySide6.QtCore import QCoreApplication

        _APP = QCoreApplication.instance() or QCoreApplication([])

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_message_id_stable_and_positive(self):
        from app.instance_lock import show_request_message_id
        a = show_request_message_id()
        self.assertGreater(a, 0)
        self.assertEqual(show_request_message_id(), a)   # 同名注册返回同一 id

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_broadcast_returns_bool(self):
        from app.instance_lock import broadcast_show_request
        self.assertIsInstance(broadcast_show_request(), bool)

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_filter_matches_registered_message(self):
        import ctypes
        from ctypes import wintypes
        from app.instance_lock import ShowRequestFilter, show_request_message_id
        called = []
        f = ShowRequestFilter(lambda: called.append(1))
        msg = wintypes.MSG()
        msg.message = show_request_message_id()
        addr = ctypes.addressof(msg)
        self.assertEqual(f.nativeEventFilter(b"windows_generic_MSG", addr), (True, 0))
        self.assertEqual(called, [1])

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_filter_ignores_unrelated_message(self):
        import ctypes
        from ctypes import wintypes
        from app.instance_lock import ShowRequestFilter
        called = []
        f = ShowRequestFilter(lambda: called.append(1))
        msg = wintypes.MSG()
        msg.message = 999
        addr = ctypes.addressof(msg)
        self.assertEqual(f.nativeEventFilter(b"windows_generic_MSG", addr), (False, 0))
        self.assertEqual(called, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_filter_ignores_non_windows_event(self):
        import ctypes
        from ctypes import wintypes
        from app.instance_lock import ShowRequestFilter, show_request_message_id
        called = []
        f = ShowRequestFilter(lambda: called.append(1))
        msg = wintypes.MSG()
        msg.message = show_request_message_id()
        addr = ctypes.addressof(msg)
        self.assertEqual(f.nativeEventFilter(b"xcb_generic_event_t", addr), (False, 0))
        self.assertEqual(called, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_msg_id_offset_matches_ctypes_layout(self):
        """读消息 id 的字节偏移必须取自 ctypes 的类型描述，不能写死魔数。"""
        from ctypes import wintypes
        from app.instance_lock import ShowRequestFilter
        f = ShowRequestFilter(lambda: None)
        self.assertEqual(f._id_off, wintypes.MSG.message.offset)

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_filter_hot_path_does_not_build_msg_struct(self):
        """热路径不得再新建 MSG 结构体对象。

        这个函数每条 Windows 消息都被调用一次（拖动时是海量消息），旧实现走
        `ctypes.cast(ptr, POINTER(MSG)).contents` 每次新建一个 MSG 对象，实测
        3.66µs/条；改成按偏移直接读后 0.26µs/条。这里钉死「不许再用 cast」。
        """
        import ctypes
        from ctypes import wintypes
        from unittest import mock

        from app.instance_lock import ShowRequestFilter
        f = ShowRequestFilter(lambda: None)
        msg = wintypes.MSG()
        msg.message = 999
        addr = ctypes.addressof(msg)
        with mock.patch.object(ctypes, "cast",
                               side_effect=AssertionError("热路径不应调用 ctypes.cast")):
            self.assertEqual(f.nativeEventFilter(b"windows_generic_MSG", addr), (False, 0))


if __name__ == "__main__":
    unittest.main()
