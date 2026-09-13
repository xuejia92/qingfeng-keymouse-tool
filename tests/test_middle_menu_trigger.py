"""中键菜单在主窗口里的触发与执行接线测试。

不启动真实窗口（避免拉起托盘/调度线程/全局钩子），用 MainWindow.__new__ 造一个
壳对象，只装上 _on_middle_click / _on_middle_menu_changed 需要的那几个属性，
验证「什么情况下弹菜单、选中条目后运行哪个流程、开关变化如何作用于监听器」。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint

from app.config import AppConfig, Flow, FlowStep, MiddleMenuItem
from app.ui import main_window as mw_mod
from app.ui.main_window import MainWindow


def _flow(name: str, fid: str, with_steps: bool = True) -> Flow:
    steps = [FlowStep(type="log", name="打印")] if with_steps else []
    flow = Flow(name=name, steps=steps)
    flow.id = fid
    return flow


class _FakeAction:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _FakeMenu:
    def __init__(self, action=None, on_exec=None):
        self._action = action
        self.exec_pos = None
        self.execs = []
        self.closed = False
        self.deleted = False
        # on_exec：exec 期间执行一次的回调，用来模拟「菜单开着时用户又按了中键」
        self._on_exec = on_exec

    def exec(self, pos):
        self.exec_pos = pos
        self.execs.append(pos)
        if self._on_exec is not None:
            callback, self._on_exec = self._on_exec, None
            callback()
        if self.closed:
            return None      # 真 QMenu 被 close() 关掉时 exec 就是返回空
        return self._action

    def close(self):
        self.closed = True

    def deleteLater(self):
        self.deleted = True


class _FakeStatusBar:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text, ms=0):
        self.messages.append(text)


class _FakeFlowTab:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    def start_flow_if_idle(self, flow_id, silent=False):
        self.calls.append((flow_id, silent))
        return self.result


class _FakeTimer:
    def __init__(self):
        self.starts = 0

    def start(self, *args):
        self.starts += 1


class _FakeWatcher:
    def __init__(self, running: bool = False):
        self._running = running
        self.suppress_value = None
        self.starts = 0
        self.stops = 0
        self.start_ok = True

    def is_running(self):
        return self._running

    def set_suppress(self, value):
        self.suppress_value = bool(value)

    def start(self):
        self.starts += 1
        self._running = self.start_ok
        return self.start_ok

    def stop(self):
        self.stops += 1
        self._running = False


class _Shell:
    """构造一个不跑 __init__ 的 MainWindow 壳，装上被测方法所需属性。"""

    def __init__(self, flows, items, watcher=None, flow_result=True):
        self.win = MainWindow.__new__(MainWindow)
        self.win.cfg = AppConfig()
        self.win.cfg.flows = list(flows)
        self.win.cfg.middle_menu_items = list(items)
        self.win.cfg.middle_menu_enabled = True
        self.win.cfg.middle_menu_suppress = False
        self.win._middle_menu_open = False
        self.win._middle_menu = None
        self.win._pending_middle_pos = None
        self.win.flow_tab = _FakeFlowTab(flow_result)
        self.win.mouse_watcher = watcher or _FakeWatcher()
        self.win._save_timer = _FakeTimer()
        self.status = _FakeStatusBar()
        self.win.statusBar = lambda: self.status


class TestMiddleClickTrigger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_disabled_does_not_build_menu(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        sh.win.cfg.middle_menu_enabled = False
        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win._on_middle_click(10, 20)
        build.assert_not_called()
        self.assertEqual(sh.win.flow_tab.calls, [])

    # ---------- 菜单位置 / 重复按中键 ----------

    @staticmethod
    def _cursor_at(x, y):
        return mock.patch.object(mw_mod.QCursor, "pos",
                                 return_value=QPoint(x, y))

    def _press(self, sh, x, y):
        """模拟一次「中键抬起」信号送达（光标在 x, y）。"""
        with self._cursor_at(x, y):
            sh.win._on_middle_click(x, y)

    def test_menu_pops_at_cursor_position(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                self._cursor_at(321, 222):
            sh.win._on_middle_click(0, 0)          # 信号带的物理坐标不参与定位
        self.assertEqual(menu.exec_pos, QPoint(321, 222))

    def test_repeat_middle_click_reopens_at_new_position(self):
        """菜单还开着时再按中键：关掉旧菜单，并在**新光标处**重开。

        这正是「菜单位置卡住」的修复点——以前这里直接 return，用户得先手动关掉
        菜单再按一次才能换位置。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        first, second = _FakeMenu(None), _FakeMenu(None)
        built = [first, second]
        first._on_exec = lambda: self._press(sh, 400, 300)   # exec 期间又按了一次中键

        with mock.patch.object(mw_mod, "build_menu",
                               side_effect=lambda *a, **k: built.pop(0)), \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)

        self.assertEqual(first.execs, [QPoint(100, 100)])
        self.assertTrue(first.closed)                        # 旧菜单被主动关掉
        self.assertTrue(first.deleted)                       # 且已安排销毁（不积菜单）
        self.assertEqual(second.execs, [QPoint(400, 300)])   # 在新位置重开
        self.assertEqual(sh.win.flow_tab.calls, [])           # 两次都没选中 → 不跑流程
        self.assertIsNone(sh.win._middle_menu)                # 收尾复位
        self.assertIsNone(sh.win._pending_middle_pos)
        self.assertFalse(sh.win._middle_menu_open)

    def test_repeat_click_without_new_position_stops_reopening(self):
        """正常关闭（用户点了别处）不应进入重开循环。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu) as build, \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)
        self.assertEqual(build.call_count, 1)
        self.assertFalse(menu.closed)
        self.assertTrue(menu.deleted)

    def test_nested_press_on_a_closing_menu_does_not_start_a_second_loop(self):
        """重开空档里（_middle_menu 已置空、循环未退出）再按中键不应另起一个循环。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        sh.win._middle_menu_open = True
        with mock.patch.object(mw_mod, "build_menu") as build:
            self._press(sh, 10, 10)
        build.assert_not_called()

    def test_no_items_does_not_build_menu(self):
        sh = _Shell([_flow("甲", "f1")], [])
        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win._on_middle_click(10, 20)
        build.assert_not_called()

    def test_modal_dialog_open_skips_menu(self):
        """有模态对话框时不抢焦点弹菜单。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        with mock.patch.object(mw_mod, "build_menu") as build, \
                mock.patch.object(mw_mod.QApplication, "activeModalWidget",
                                  return_value=object()):
            sh.win._on_middle_click(10, 20)
        build.assert_not_called()

    def test_reentrant_trigger_ignored(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        sh.win._middle_menu_open = True
        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win._on_middle_click(10, 20)
        build.assert_not_called()

    def test_dismissing_menu_runs_nothing(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu):
            sh.win._on_middle_click(10, 20)
        self.assertIsNotNone(menu.exec_pos)              # 菜单确实弹过
        self.assertEqual(sh.win.flow_tab.calls, [])
        self.assertFalse(sh.win._middle_menu_open)       # 收尾复位

    def test_chosen_item_runs_matching_flow_silently(self):
        flows = [_flow("甲", "f1"), _flow("乙", "f2")]
        sh = _Shell(flows, [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(_FakeAction("f2"))
        with mock.patch.object(mw_mod, "build_menu", return_value=menu):
            sh.win._on_middle_click(10, 20)
        self.assertEqual(sh.win.flow_tab.calls, [("f2", True)])   # silent=True
        self.assertIn("已启动「乙」", " ".join(sh.status.messages))

    def test_all_broken_items_shows_hint_and_runs_nothing(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="gone")])
        with mock.patch.object(mw_mod, "build_menu", return_value=None):
            sh.win._on_middle_click(10, 20)
        self.assertEqual(sh.win.flow_tab.calls, [])
        self.assertIn("没有可运行的菜单项", " ".join(sh.status.messages))

    def test_flow_without_steps_warns(self):
        sh = _Shell([_flow("空流程", "f1", with_steps=False)],
                    [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(_FakeAction("f1"))
        with mock.patch.object(mw_mod, "build_menu", return_value=menu):
            sh.win._on_middle_click(10, 20)
        self.assertEqual(sh.win.flow_tab.calls, [])
        self.assertIn("还没有步骤", " ".join(sh.status.messages))

    def test_already_running_flow_is_skipped(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    flow_result=False)
        menu = _FakeMenu(_FakeAction("f1"))
        with mock.patch.object(mw_mod, "build_menu", return_value=menu):
            sh.win._on_middle_click(10, 20)
        self.assertEqual(sh.win.flow_tab.calls, [("f1", True)])
        self.assertIn("已在运行中", " ".join(sh.status.messages))


class TestSwitchWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_enabling_starts_watcher_and_saves(self):
        watcher = _FakeWatcher(running=False)
        sh = _Shell([_flow("甲", "f1")], [], watcher=watcher)
        sh.win.cfg.middle_menu_enabled = True
        sh.win.cfg.middle_menu_suppress = True
        sh.win._on_middle_menu_changed()
        self.assertEqual(watcher.starts, 1)
        self.assertTrue(watcher.suppress_value)
        self.assertEqual(sh.win._save_timer.starts, 1)

    def test_disabling_stops_watcher(self):
        watcher = _FakeWatcher(running=True)
        sh = _Shell([_flow("甲", "f1")], [], watcher=watcher)
        sh.win.cfg.middle_menu_enabled = False
        sh.win._on_middle_menu_changed()
        self.assertEqual(watcher.stops, 1)

    def test_enabled_and_running_does_not_restart(self):
        watcher = _FakeWatcher(running=True)
        sh = _Shell([_flow("甲", "f1")], [], watcher=watcher)
        sh.win.cfg.middle_menu_enabled = True
        sh.win._on_middle_menu_changed()
        self.assertEqual(watcher.starts, 0)

    def test_start_failure_reports_status(self):
        watcher = _FakeWatcher(running=False)
        watcher.start_ok = False
        sh = _Shell([_flow("甲", "f1")], [], watcher=watcher)
        sh.win.cfg.middle_menu_enabled = True
        sh.win._on_middle_menu_changed()
        self.assertIn("中键菜单启动失败", " ".join(sh.status.messages))


if __name__ == "__main__":
    unittest.main()
