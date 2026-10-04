"""中键菜单在主窗口里的触发与执行接线测试。

不启动真实窗口（避免拉起托盘/调度线程/全局钩子），用 MainWindow.__new__ 造一个
壳对象，只装上 show_middle_menu / _on_middle_click / _on_middle_menu_changed
需要的那几个属性，验证「什么情况下弹菜单、选中条目后运行哪个流程、开关与快捷键
如何作用于监听器和热键注册」。
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Signal

from app.config import AppConfig, Flow, FlowStep, MiddleMenuItem
from app.ui import main_window as mw_mod
from app.ui.main_window import MainWindow

_ESC_PATCHER = None


class _FakeEscListener:
    """替身全局 Esc 监听（真货是 `keyboard` 库的进程级钩子）。"""

    def __init__(self, on_esc):
        self.on_esc = on_esc
        self.started = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False

    def press(self):
        self.on_esc()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def setUpModule():      # noqa: N802（unittest 约定）
    """整个模块把全局 Esc 监听换成替身。

    中键菜单弹出期间会挂 `cancel_key.EscListener` —— 真货是 `keyboard` 库的
    **进程级全局键盘钩子**。测试里反复装/卸真钩子既没必要（会捕获测试机上的真实
    Esc 按键），又可能让解释器退出时不干净：本工程记录过"退出码 127、无 traceback"
    的静默崩，全量套件里也确实偶发复现过。真钩子的行为由
    `tests/test_cancel_key.py` 专门覆盖。
    """
    global _ESC_PATCHER
    _ESC_PATCHER = mock.patch.object(mw_mod.cancel_key, "EscListener",
                                     _FakeEscListener)
    _ESC_PATCHER.start()


def tearDownModule():   # noqa: N802（unittest 约定）
    if _ESC_PATCHER is not None:
        _ESC_PATCHER.stop()


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
    def __init__(self, action=None, on_exec=None, rect=None):
        self._action = action
        self.exec_pos = None
        self.execs = []
        self.closed = False
        self.deleted = False
        # on_exec：exec 期间执行一次的回调，用来模拟「菜单开着时用户又按了中键」
        self._on_exec = on_exec
        # 菜单矩形（判断"点在菜单外面"）：默认 (0,0)-(200,300)
        self._rect = rect if rect is not None else QRect(0, 0, 200, 300)

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

    def geometry(self):
        """菜单在屏幕上的矩形（判断"点外面"用）。默认一块够大的区域。"""
        return self._rect


class _FakeStatusBar:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text, ms=0):
        self.messages.append(text)


class _FakeFlowTab:
    def __init__(self, result: bool = True, queued: bool = False):
        self.result = result
        self.queued = queued
        self.calls: list[tuple[str, bool]] = []

    def start_flow_if_idle(self, flow_id, silent=False):
        self.calls.append((flow_id, silent))
        return self.result

    def is_queued(self, flow_id):
        return bool(self.queued)

    def run_all_async(self):
        """热键「运行全部异步流程」会打这个入口（注册热键的测试会绑定它）。"""
        self.calls.append(("__run_all_async__", False))


class _FakeTimer:
    def __init__(self):
        self.starts = 0

    def start(self, *args):
        self.starts += 1


class _FakeWatcher:
    def __init__(self, running: bool = False):
        self._running = running
        self.suppress_value = None
        self.menu_open_value = None
        self.starts = 0
        self.stops = 0
        self.start_ok = True

    def is_running(self):
        return self._running

    def set_suppress(self, value):
        self.suppress_value = bool(value)

    def set_menu_open(self, value):
        """菜单弹出/收起：开着期间监听器会吞掉中键（见 mouse_menu.set_menu_open）。"""
        self.menu_open_value = bool(value)

    def start(self):
        self.starts += 1
        self._running = self.start_ok
        return self.start_ok

    def stop(self):
        self.stops += 1
        self._running = False


class _FakeLabel:
    """替身 QLabel：只关心 setText 的内容。"""

    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


class _Shell:
    """构造一个不跑 __init__ 的 MainWindow 壳，装上被测方法所需属性。"""

    def __init__(self, flows, items, watcher=None, flow_result=True,
                 enabled=True, hotkey="", flow_queued=False):
        self.win = MainWindow.__new__(MainWindow)
        self.win.cfg = AppConfig()
        self.win.cfg.flows = list(flows)
        self.win.cfg.middle_menu_items = list(items)
        self.win.cfg.middle_menu_enabled = enabled
        self.win.cfg.middle_menu_suppress = False
        self.win.cfg.middle_menu_hotkey = hotkey
        self.win._middle_menu_open = False
        self.win._middle_menu = None
        self.win._pending_middle_pos = None
        self.win._pending_tool = ""          # 九宫格工具：菜单里点了哪个工具
        # 中键"按下"已用它关过菜单时，抬起不再重开（见 MENU_SWALLOW_TRIGGER_SEC）
        self.win._menu_swallow_trigger_until = 0.0
        # 触发方式快照：与 cfg 对齐，这样「增删菜单项」不该触发重注册
        self.win._menu_trigger = (bool(enabled), hotkey)
        self.win.flow_tab = _FakeFlowTab(flow_result, queued=flow_queued)
        self.win.mouse_watcher = watcher or _FakeWatcher()
        self.win._save_timer = _FakeTimer()
        self.status = _FakeStatusBar()
        self.win.statusBar = lambda: self.status
        # _refresh_status_hint 要写 status_hint；_register_hotkeys 会碰 manager，
        # 测试里换成计数替身，只验证「该不该重注册」这件事本身
        self.status_hint = _FakeLabel()
        self.win.status_hint = self.status_hint
        self.reregistered: list[int] = []
        self.win._register_hotkeys = lambda: self.reregistered.append(1)


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

    def test_repeat_middle_click_closes_the_menu(self):
        """★ 菜单还开着时**再按中键 = 关掉**（2026-10-04 修「浏览器上无法退出菜单」）。

        为什么鼠标这条必须是"关"而不是"换个位置重开"：菜单弹在浏览器上时，浏览器
        会因中键进入「自动滚动」并抢走鼠标捕获，左键点空白处根本送不到菜单上；
        此时**只有全局钩子看得见的中键**还能用来关闭。若中键是"又关又开"，就等于
        永远关不掉（这也是用户报的现象）。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        first, second = _FakeMenu(None), _FakeMenu(None)
        built = [first, second]
        first._on_exec = lambda: self._press(sh, 400, 300)   # exec 期间又按了一次中键

        with mock.patch.object(mw_mod, "build_menu",
                               side_effect=lambda *a, **k: built.pop(0)) as build, \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)

        self.assertEqual(first.execs, [QPoint(100, 100)])
        self.assertTrue(first.closed)                        # 第二次按中键把它关掉了
        self.assertTrue(first.deleted)
        self.assertEqual(build.call_count, 1, "不该再建第二个菜单（那就是关不掉了）")
        self.assertEqual(second.execs, [])                   # 没有重开
        self.assertEqual(sh.win.flow_tab.calls, [])
        self.assertIsNone(sh.win._middle_menu)                # 收尾复位
        self.assertIsNone(sh.win._pending_middle_pos)
        self.assertFalse(sh.win._middle_menu_open)
        self.assertIs(sh.win.mouse_watcher.menu_open_value, False,
                      "菜单收起后要恢复中键的正常行为")

    def test_hotkey_repeat_also_closes(self):
        """快捷键与中键行为一致：再触发一次都是"关"（见 show_middle_menu 说明）。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        first, second = _FakeMenu(None), _FakeMenu(None)
        built = [first, second]

        def press_hotkey():
            """exec 期间又按了一次快捷键（走的是 show_middle_menu 本身）。"""
            with self._cursor_at(400, 300):
                sh.win.show_middle_menu()

        first._on_exec = press_hotkey

        with mock.patch.object(mw_mod, "build_menu",
                               side_effect=lambda *a, **k: built.pop(0)) as build, \
                self._cursor_at(100, 100):
            sh.win.show_middle_menu()

        self.assertTrue(first.closed)
        self.assertTrue(first.deleted)
        self.assertEqual(build.call_count, 1, "再触发一次是关，不重开")
        self.assertEqual(second.execs, [])

    def test_menu_open_flag_is_set_while_showing(self):
        """菜单开着期间要告诉监听器「吞中键」，收起后立刻恢复。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None)
        seen = {}
        menu._on_exec = lambda: seen.update(
            flag=sh.win.mouse_watcher.menu_open_value)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)
        self.assertIs(seen.get("flag"), True, "菜单开着时中键必须被吞掉")
        self.assertIs(sh.win.mouse_watcher.menu_open_value, False, "收起后要复位")

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

    # ---------- 全局钩子兜底的两种关闭方式（2026-10-04）----------

    def test_click_outside_menu_closes_it(self):
        """★ 点在菜单外面：由全局钩子上报后关掉菜单。

        菜单弹在别的程序上面时 Qt 弹窗拿不到鼠标捕获，"点外面就关"不会触发
        （用户反馈「鼠标点击其他位置，菜单也没有消失」），所以钩子兜一层。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        sh.win._pending_middle_pos = QPoint(100, 100)
        with self._cursor_at(900, 900):
            sh.win._on_menu_button_pressed(900, 900)      # 菜单外
        self.assertTrue(menu.closed)
        self.assertIsNone(sh.win._pending_middle_pos, "关掉后不该再重开")

    def test_click_inside_menu_keeps_it_open(self):
        """点在菜单里面不归我们管（交给 Qt 去触发条目）。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        with self._cursor_at(150, 150):                   # 菜单内
            sh.win._on_menu_button_pressed(150, 150)
        self.assertFalse(menu.closed)

    def test_click_outside_judged_in_logical_coordinates(self):
        """★ 判内外要用 `QCursor.pos()`（逻辑坐标），不能拿钩子的**物理**坐标去比。

        本机显示缩放 125%（dpr=1.24）：逻辑 (1096,827) 对应物理 (1359,1025)。
        用物理坐标去比 Qt 的逻辑矩形，菜单命中区会被放大近 1/4 —— 紧挨菜单外面的
        点击会被当成"点在里面"，菜单关不掉（2026-10-04 用户反馈「点外面还是不行」）。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        # 菜单右下角在 (300, 400)；物理坐标 (330, 430) 折算成逻辑约 (266, 347)：
        # 若错用物理坐标做比较会落在菜单内 → 不关；用逻辑坐标则正确判定为"外面"。
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        sh.win._pending_middle_pos = QPoint(100, 100)
        with self._cursor_at(330, 430):                   # 逻辑坐标：在菜单外
            sh.win._on_menu_button_pressed(330, 430)      # 钩子上报的是物理坐标
        self.assertTrue(menu.closed, "逻辑坐标在菜单外 → 应当关掉")
        self.assertIsNone(sh.win._pending_middle_pos, "关掉后不该再重开")

    def test_click_far_outside_still_closes(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        sh.win._pending_middle_pos = QPoint(100, 100)
        with self._cursor_at(1600, 900):
            sh.win._on_menu_button_pressed(900, 900)
        self.assertTrue(menu.closed)

    def test_button_press_without_menu_is_ignored(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        sh.win._middle_menu = None
        sh.win._on_menu_button_pressed(900, 900, "left")   # 不该抛
        self.assertIsNone(sh.win._middle_menu)

    def test_middle_press_outside_closes_and_does_not_reopen(self):
        """★ 中键点在菜单外面：按下就关，**抬起绝不能再开一个**。

        一次中键 = 按下 + 抬起两个事件。按下那一下已经把它当"开关"用掉了，
        若抬起还去跑 show_middle_menu，就是"关掉又立刻重开" —— 用户看到的就是
        「点菜单外面有时候关不掉」（2026-10-04 反馈里的"有时候"）。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        sh.win._pending_middle_pos = QPoint(100, 100)
        with self._cursor_at(900, 900):
            sh.win._on_menu_button_pressed(900, 900, "middle")   # 按下：菜单外
        self.assertTrue(menu.closed, "按下就该关掉")

        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win._on_middle_click(900, 900)                    # 抬起：不能重开
        self.assertFalse(build.called, "抬起不能再开一个菜单（那就是关不掉）")

    def test_middle_press_inside_also_consumes_the_trigger(self):
        """中键点在菜单**里面**同样只关不重开（Qt 不会用中键去触发条目）。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        with self._cursor_at(150, 150):
            sh.win._on_menu_button_pressed(150, 150, "middle")
        self.assertTrue(menu.closed)
        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win._on_middle_click(150, 150)
        self.assertFalse(build.called)

    def test_swallow_window_expires(self):
        """吞掉的时间窗过了之后，中键仍要能正常唤出菜单（别把中键弄瘫）。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        sh.win._menu_swallow_trigger_until = time.monotonic() - 1.0   # 早已过期
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu) as build, \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)
        self.assertTrue(build.called, "过期的吞并标记不该继续吃掉中键")

    def test_left_click_does_not_set_the_swallow_mark(self):
        """左键点外面：只关菜单，不能顺手把中键的触发也吞掉。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None, rect=QRect(100, 100, 200, 300))
        sh.win._middle_menu = menu
        with self._cursor_at(900, 900):
            sh.win._on_menu_button_pressed(900, 900, "left")
        self.assertTrue(menu.closed)
        self.assertEqual(sh.win._menu_swallow_trigger_until, 0.0)

    def test_esc_listener_wraps_the_menu(self):
        """★ 菜单弹出期间必须挂着**全局** Esc 监听。

        弹窗拿不到键盘焦点时（菜单弹在别的程序上面），Qt 自己的 Esc 处理收不到键，
        全局监听是唯一还能关掉它的路径。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None)
        events = []

        class _FakeEsc:
            def __init__(self, callback):
                self._callback = callback
                events.append("created")

            def __enter__(self):
                events.append("enter")
                return True

            def __exit__(self, *exc):
                events.append("exit")
                return False

        with mock.patch.object(mw_mod.cancel_key, "EscListener", _FakeEsc), \
                mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)
        self.assertEqual(events, ["created", "enter", "exit"],
                         "菜单期间要挂着 Esc 监听，收起后要摘掉")

    def test_hotkey_only_menu_temporarily_starts_the_mouse_hook(self):
        """★ 只开热键（没勾「鼠标中键触发」）时，菜单开着期间要**临时装上**鼠标钩子。

        「点外面关菜单」靠的是鼠标钩子；没勾中键触发时钩子本来就没装 ——
        热键打开的菜单就**永远关不掉**（2026-10-04 用户实测：勾上就好、不勾就不行）。
        菜单收起后要还原成"只开热键"的原始状态。
        """
        watcher = _FakeWatcher(running=False)
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    hotkey="alt+q", watcher=watcher)
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                self._cursor_at(100, 100):
            # 热键最终就是走到 show_middle_menu（见 _register_hotkeys 的 bind）
            sh.win.show_middle_menu(source="热键")
        self.assertEqual(watcher.starts, 1, "菜单开着期间需要鼠标钩子兜「点外面」")
        self.assertEqual(watcher.stops, 1, "菜单收起后要还原")

    def test_middle_trigger_does_not_touch_a_running_hook(self):
        """勾了中键触发（钩子本来在跑）：不要多余地 start/stop。"""
        watcher = _FakeWatcher(running=True)
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    watcher=watcher)
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                self._cursor_at(100, 100):
            sh.win._on_middle_click(100, 100)
        self.assertEqual(watcher.starts, 0)
        self.assertEqual(watcher.stops, 0)

    def test_dismiss_closes_menu_and_does_not_reopen(self):
        """Esc / 点外面 统一走这里：关掉菜单且**不重开**（位置置空，外层循环随即退出）。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")])
        menu = _FakeMenu(None)
        sh.win._middle_menu = menu
        sh.win._pending_middle_pos = QPoint(5, 5)
        sh.win._dismiss_middle_menu()
        self.assertTrue(menu.closed)
        self.assertIsNone(sh.win._pending_middle_pos,
                          "位置置空，外层 while 循环才不会重开")

    def test_menu_dismiss_request_is_wired_to_the_slot(self):
        """钩子线程的 Esc 请求 → `_menuDismissRequested` → `_dismiss_middle_menu`。

        这条接线是 Esc 兜底的命脉，用源码级契约钉住（同项目里其它"接错线就没反应"
        的地方）。`_Shell` 是 __new__ 出来的、不能真连信号，所以在这层验。
        """
        import inspect
        src = inspect.getsource(MainWindow.__init__)
        self.assertIn(
            "self._menuDismissRequested.connect(self._dismiss_middle_menu)", src)
        self.assertIn("self.mouse_watcher.menuButtonPressed.connect(", src)

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
        self.assertIn("已在运行或排队中", " ".join(sh.status.messages))

    def test_started_but_queued_is_reported_as_queued(self):
        """同步流程遇忙会排队：已启动但排在队尾时，提示要说清「在排队」而不是「已启动」。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    flow_result=True, flow_queued=True)
        menu = _FakeMenu(_FakeAction("f1"))
        with mock.patch.object(mw_mod, "build_menu", return_value=menu):
            sh.win._on_middle_click(10, 20)
        self.assertIn("已加入排队", " ".join(sh.status.messages))


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


class _FakeManager:
    """替身热键管理器：只记录注册了哪些键。"""

    def __init__(self):
        self.registered: list[str] = []
        self.unregister_all_calls = 0

    def register(self, hotkey):
        self.registered.append(hotkey)
        return True

    def unregister_all(self):
        self.unregister_all_calls += 1
        self.registered.clear()


class TestHotkeyTrigger(unittest.TestCase):
    """快捷键这条触发路径（与鼠标中键完全独立）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    @staticmethod
    def _cursor_at(x, y):
        return mock.patch.object(mw_mod.QCursor, "pos", return_value=QPoint(x, y))

    def test_hotkey_pops_menu_at_cursor(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    hotkey="ctrl+alt+m")
        menu = _FakeMenu(None)
        with mock.patch.object(mw_mod, "build_menu", return_value=menu), \
                self._cursor_at(222, 111):
            sh.win.show_middle_menu()
        self.assertEqual(menu.exec_pos, QPoint(222, 111))

    def test_hotkey_works_after_middle_click_disabled(self):
        """只用快捷键的人：关掉鼠标中键触发后，快捷键照样能弹菜单。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    enabled=False, hotkey="f10")
        menu = _FakeMenu(_FakeAction("f1"))
        with mock.patch.object(mw_mod, "build_menu", return_value=menu):
            sh.win.show_middle_menu()
        self.assertIsNotNone(menu.exec_pos)
        self.assertEqual(sh.win.flow_tab.calls, [("f1", True)])

    def test_middle_click_still_ignored_when_its_switch_is_off(self):
        """中键开关关着时，即便设了快捷键，鼠标中键本身也不该弹菜单。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    enabled=False, hotkey="f10")
        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win._on_middle_click(10, 20)
        build.assert_not_called()

    def test_hotkey_repeat_closes_the_menu(self):
        """★ 菜单开着时再按快捷键 = **关掉**（与中键同款行为）。

        以前是"关掉旧菜单、在新光标处重开"，但那让**触发手势自己关不掉菜单**
        （用户 2026-10-04 反馈「用热键唤出的菜单也关不掉」）；改成"再按一次是关"。
        """
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    hotkey="ctrl+alt+m")
        first, second = _FakeMenu(None), _FakeMenu(None)
        built = [first, second]
        # exec 期间再按一次快捷键
        first._on_exec = lambda: sh.win.show_middle_menu()

        with mock.patch.object(mw_mod, "build_menu",
                               side_effect=lambda *a, **k: built.pop(0)) as build, \
                mock.patch.object(mw_mod.QCursor, "pos",
                                  side_effect=[QPoint(100, 100), QPoint(400, 300)]):
            sh.win.show_middle_menu()

        self.assertEqual(first.execs, [QPoint(100, 100)])
        self.assertTrue(first.closed)                        # 第二次按键把它关了
        self.assertTrue(first.deleted)                       # 且已安排销毁（不积菜单）
        self.assertEqual(build.call_count, 1, "不该重开第二个菜单")
        self.assertEqual(second.execs, [])
        self.assertEqual(sh.win.flow_tab.calls, [])          # 两次都没选中 → 不跑流程
        self.assertIsNone(sh.win._middle_menu)
        self.assertFalse(sh.win._middle_menu_open)

    def test_no_items_means_no_menu_even_by_hotkey(self):
        sh = _Shell([_flow("甲", "f1")], [], hotkey="f10")
        with mock.patch.object(mw_mod, "build_menu") as build:
            sh.win.show_middle_menu()
        build.assert_not_called()

    def test_modal_dialog_blocks_hotkey(self):
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    hotkey="f10")
        with mock.patch.object(mw_mod, "build_menu") as build, \
                mock.patch.object(mw_mod.QApplication, "activeModalWidget",
                                  return_value=object()):
            sh.win.show_middle_menu()
        build.assert_not_called()


class TestTriggerReconfiguration(unittest.TestCase):
    """开关 / 快捷键变化时如何作用于监听器与热键注册。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_hotkey_change_reregisters_hotkeys(self):
        """新设快捷键必须立刻注册，否则用户按了没反应。"""
        sh = _Shell([_flow("甲", "f1")], [])
        sh.win.cfg.middle_menu_hotkey = "ctrl+alt+m"
        sh.win._on_middle_menu_changed()
        self.assertEqual(len(sh.reregistered), 1)

    def test_menu_item_edit_does_not_reregister(self):
        """增删改菜单项不影响触发方式：不该白挨一次全量重注册。"""
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    hotkey="f10")
        sh.win._on_middle_menu_changed()
        self.assertEqual(sh.reregistered, [])
        self.assertEqual(sh.win._save_timer.starts, 1)    # 但配置要落盘

    def test_middle_switch_change_reregisters(self):
        sh = _Shell([_flow("甲", "f1")], [])
        sh.win.cfg.middle_menu_enabled = False
        sh.win._on_middle_menu_changed()
        self.assertEqual(len(sh.reregistered), 1)

    def test_status_hint_shows_menu_hotkey(self):
        sh = _Shell([_flow("甲", "f1")], [], hotkey="ctrl+alt+m")
        sh.win._refresh_status_hint()
        self.assertIn("快捷菜单", sh.status_hint.text)
        self.assertIn("Ctrl+Alt+M", sh.status_hint.text)

    def test_status_hint_hides_menu_hotkey_when_unset(self):
        sh = _Shell([_flow("甲", "f1")], [])
        sh.win._refresh_status_hint()
        self.assertNotIn("快捷菜单", sh.status_hint.text)


class TestHotkeyRegistration(unittest.TestCase):
    """_register_hotkeys 真的把菜单快捷键接上了 show_middle_menu。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _win(self, hotkey: str):
        win = MainWindow.__new__(MainWindow)
        win.cfg = AppConfig()
        win.cfg.middle_menu_hotkey = hotkey
        win.manager = _FakeManager()
        win.status = _FakeStatusBar()
        win.statusBar = lambda: win.status
        win.toggle_show_hide = lambda: None
        win.stop_all = lambda: None
        win.toggle_clicker = lambda: None
        win.toggle_presser = lambda: None
        win.flow_tab = _FakeFlowTab()
        win.finder_tab = _FakeFlowTab()
        return win

    def test_registered_and_dispatches_to_menu(self):
        win = self._win("ctrl+alt+m")
        win._register_hotkeys()
        self.assertIn("ctrl+alt+m", win.manager.registered)
        # 热键回调是个薄包装（带来源标签，供诊断日志区分中键/热键），
        # 所以不能直接比函数对象；调一下看它是否真的弹菜单。
        with mock.patch.object(win, "show_middle_menu") as show:
            win._dispatch["ctrl+alt+m"]()
        self.assertEqual(show.call_count, 1)
        self.assertEqual(show.call_args.kwargs.get("source"), "热键")

    def test_empty_hotkey_registers_nothing(self):
        win = self._win("")
        win._register_hotkeys()
        self.assertNotIn("", win._dispatch)


class TestInitOrdering(unittest.TestCase):
    """启动顺序契约：菜单状态必须先于热键注册建好。

    快捷键一注册，keyboard 的监听线程立刻生效；若用户此时正好按下它，
    回调会直接进 show_middle_menu()——那时 _middle_menu_open / _middle_menu /
    _pending_middle_pos 还没建，就是 AttributeError。这类竞态跑测试很难复现，
    所以直接用源码级契约钉死（同 test_imgio 的源码扫描做法）。
    """

    def test_state_initialized_before_hotkey_registration(self):
        import inspect
        src = inspect.getsource(MainWindow.__init__)
        self.assertIn("self._middle_menu_open = False", src)
        self.assertIn("self._register_hotkeys()", src)
        self.assertLess(src.index("self._middle_menu_open = False"),
                        src.index("self._register_hotkeys()"),
                        "中键菜单状态必须在登记全局热键之前初始化")

    def test_trigger_snapshot_initialized_before_use(self):
        """_menu_trigger 快照也要先建好，否则首次改配置就 AttributeError。"""
        import inspect
        src = inspect.getsource(MainWindow.__init__)
        self.assertLess(src.index("self._menu_trigger ="),
                        src.index("self._register_hotkeys()"))


class TestHotkeyManagerChain(unittest.TestCase):
    """端到端：真 HotkeyManager + 替身 keyboard 库，验证按键真的弹菜单。

    只把 keyboard 库换成替身（避免测试机上真装一个全局钩子），其余全是真的：
    注册表在 HotkeyManager 手里、dispatch 在 MainWindow 手里。这条链一旦断了，
    用户的表现就是「设了快捷键但按了没反应」，所以值得端到端钉一遍。
    """

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_pressing_the_recorded_hotkey_pops_the_menu(self):
        from PySide6.QtCore import QObject
        from app import hotkey_manager as hm_mod
        from app.hotkey_manager import HotkeyManager

        captured: dict[str, object] = {}

        class _FakeEngine(QObject):
            fired = Signal(str)

            def register(self, hotkey, callback, suppress=False):
                captured[hotkey] = (callback, suppress)
                return True

            def unregister(self, hotkey):
                captured.pop(hotkey, None)

            def unregister_all(self):
                captured.clear()

        manager = HotkeyManager()
        sh = _Shell([_flow("甲", "f1")], [MiddleMenuItem(flow_id="f1")],
                    hotkey="ctrl+alt+m")
        sh.win.manager = manager
        manager.triggered.connect(sh.win._dispatch_hotkey)

        with mock.patch.object(hm_mod, "_engine", _FakeEngine()):
            # _Shell 把 _register_hotkeys 换成了计数替身，这里要真的调类方法
            MainWindow._register_hotkeys(sh.win)
            self.assertIn("ctrl+alt+m", captured)
            callback, suppress = captured["ctrl+alt+m"]
            # 含 Ctrl 的组合键不拦截（同其它热键的既有约定）
            self.assertFalse(suppress)
            menu = _FakeMenu(None)
            with mock.patch.object(mw_mod, "build_menu", return_value=menu):
                callback()          # 模拟用户按下快捷键
        self.assertIsNotNone(menu.exec_pos)      # 菜单确实弹了

    def test_single_key_menu_hotkey_is_suppressed(self):
        """单键快捷键要拦截，否则会漏进当前程序（同其它单键热键约定）。"""
        from PySide6.QtCore import QObject
        from app import hotkey_manager as hm_mod
        from app.hotkey_manager import HotkeyManager

        captured: dict[str, object] = {}

        class _FakeEngine(QObject):
            fired = Signal(str)

            def register(self, hotkey, callback, suppress=False):
                captured[hotkey] = (callback, suppress)
                return True

            def unregister(self, hotkey):
                captured.pop(hotkey, None)

            def unregister_all(self):
                captured.clear()

        manager = HotkeyManager()
        sh = _Shell([_flow("甲", "f1")], [], hotkey="f10")
        sh.win.manager = manager
        with mock.patch.object(hm_mod, "_engine", _FakeEngine()):
            MainWindow._register_hotkeys(sh.win)
        _cb, suppress = captured["f10"]
        self.assertTrue(suppress)


if __name__ == "__main__":
    unittest.main()
