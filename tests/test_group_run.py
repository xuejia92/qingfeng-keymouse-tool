# -*- coding: utf-8 -*-
"""流程分组编辑页 + 批量执行（2026-09-22）。

需求原话：「自动化流程，流程分组可编辑，添加一个编辑页面，可以执行当前分组下所有流程，
只有勾选了异步执行的流程，可以同时运行，勾选了异步执行的流程前面加个异步执行的图标
标记出来，可以自定义热键执行所有分组下的异步流程，也可以手动点击运行执行」。

覆盖：
- `flows_in_group` 过滤 + `run_group_async`（只跑本组异步流程）；
- `run_flows` / `run_group` 的排队语义：异步立即并行、同步排队、
  已在跑/已在排队/空步骤跳过、不得自建 FlowRunner；
- 分组热键：按一下跑本组异步流程、再按一下停本组异步（同步流程不受影响）；
- 左栏异步标记 ⚡ 与运行 ▶ / 排队 ⏳ 共存 + tooltip；
- 热键槽位（hotkey_policy）+ 主窗口接线（源码级契约）；
- `GroupRunDialog`：列表内容、按钮动作、热键写回、关闭后定时器停。

全部用替身 FlowRunner（不真起线程），沿用 test_flow_async_queue 的脚手架思路。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import hotkey_policy
from app.config import ASYNC_MARK, ASYNC_TIP, AppConfig, Flow, FlowStep
from app.ui import flow_tab as flow_tab_mod
from app.ui.flow_dialog import GroupRunDialog
from app.ui.flow_tab import QUEUED_TIP, FlowTab
from tests._env import TempConfigPaths


class _Signal:
    """最小 Qt 信号替身（connect / emit）。"""

    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for cb in list(self._callbacks):
            cb(*args)


class FakeRunner:
    """替身 FlowRunner：不起线程，由用例显式 finish()。"""

    made: list = []

    def __init__(self, flow: Flow):
        self.flow = flow
        self.is_running = False
        self.current_step_index = -1
        self.last_step_ok = True
        self.last_step_reason = ""
        self.stateChanged = _Signal()
        self.stepStarted = _Signal()
        self.stop_calls = 0
        self._stop_requested = False
        FakeRunner.made.append(self)

    def start(self) -> bool:
        if self.is_running or not self.flow.steps:
            return False
        self.is_running = True
        self._stop_requested = False
        self.stateChanged.emit("running", "", True)
        return True

    def stop(self) -> None:
        """只计数：真实 FlowRunner.stop 是置停止位，is_running 由线程收尾时归位。"""
        self.stop_calls += 1
        self._stop_requested = True     # 真实 runner 里对应 _stop Event

    @property
    def stopping(self) -> bool:
        """已收到停止请求、线程还在收尾（与真实 FlowRunner.stopping 语义一致）。"""
        return self.is_running and self._stop_requested

    def finish(self, reason: str = "已完成 1 轮", ok: bool = True) -> None:
        self.is_running = False
        self._stop_requested = False
        self.stateChanged.emit("stopped", reason, ok)


def _flow(name: str, group: str = "", async_run: bool = False, steps: int = 1) -> Flow:
    return Flow(name=name, group=group, async_run=async_run,
                steps=[FlowStep(type="wait") for _ in range(steps)])


class _TabCase(unittest.TestCase):
    """公共脚手架：临时路径隔离 + 替身 runner。"""

    def setUp(self):
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self._patch = mock.patch.object(flow_tab_mod, "FlowRunner", FakeRunner)
        self._patch.start()
        FakeRunner.made = []
        self._msgbox = mock.patch.object(flow_tab_mod, "QMessageBox").start()
        self._save = mock.patch.object(AppConfig, "save").start()

    def tearDown(self):
        mock.patch.stopall()
        self._tmp.__exit__(None, None, None)

    def _tab(self, *flows: Flow) -> FlowTab:
        cfg = AppConfig()
        cfg.flows = list(flows)
        cfg.flow_groups = ["组A", "组B"]
        self.cfg = cfg
        return FlowTab(cfg)

    @staticmethod
    def _running(tab: FlowTab, flow: Flow) -> bool:
        runner = tab._runners.get(flow.id)
        return bool(runner and runner.is_running)

    @staticmethod
    def _runner_of(tab: FlowTab, flow: Flow):
        return tab._runners.get(flow.id)


class TestGroupFiltering(_TabCase):
    def test_flows_in_group(self):
        a1, a2 = _flow("a1", "组A"), _flow("a2", "组A")
        b1, none = _flow("b1", "组B"), _flow("n1", "")
        tab = self._tab(a1, a2, b1, none)
        self.assertEqual([f.name for f in tab.flows_in_group("组A")], ["a1", "a2"])
        self.assertEqual([f.name for f in tab.flows_in_group("组B")], ["b1"])
        self.assertEqual([f.name for f in tab.flows_in_group("")], ["n1"])

    def test_run_group_async_only_starts_async_of_that_group(self):
        a1 = _flow("a1", "组A", async_run=True)
        a2 = _flow("a2", "组A")            # 同步：不该被启动
        b1 = _flow("b1", "组B", async_run=True)
        tab = self._tab(a1, a2, b1)
        self.assertEqual(tab.run_group_async("组A"), 1)
        self.assertTrue(self._running(tab, a1))
        self.assertFalse(self._running(tab, a2))
        self.assertIsNone(self._runner_of(tab, b1))     # 别组不动


class TestRunFlowsSemantics(_TabCase):
    def test_all_async_start_immediately(self):
        f1 = _flow("异步1", "组A", async_run=True)
        f2 = _flow("异步2", "组A", async_run=True)
        f3 = _flow("异步3", "组B", async_run=True)
        tab = self._tab(f1, f2, f3)
        self.assertEqual(tab.run_flows([f1, f2, f3]), 3)
        for f in (f1, f2, f3):
            self.assertTrue(self._running(tab, f), f.name)
        self.assertEqual(tab._queue, [])          # 异步从不排队

    def test_sync_flows_run_one_by_one(self):
        f1, f2, f3 = (_flow("同步1", "组A"), _flow("同步2", "组A"), _flow("同步3", "组A"))
        tab = self._tab(f1, f2, f3)
        self.assertEqual(tab.run_flows([f1, f2, f3]), 3)   # 1 个在跑 + 2 个排队
        self.assertTrue(self._running(tab, f1))
        self.assertFalse(self._running(tab, f2))
        self.assertEqual(tab._queue, [f2.id, f3.id])

    def test_mixed_group_async_runs_alongside_sync(self):
        s1 = _flow("同步", "组A")
        a1 = _flow("异步1", "组A", async_run=True)
        a2 = _flow("异步2", "组A", async_run=True)
        tab = self._tab(s1, a1, a2)
        self.assertEqual(tab.run_group("组A"), 3)
        self.assertTrue(self._running(tab, s1))
        self.assertTrue(self._running(tab, a1))     # 异步不受同步影响
        self.assertTrue(self._running(tab, a2))
        self.assertEqual(tab._queue, [])

    def test_second_sync_of_group_queues_behind_first(self):
        s1 = _flow("同步1", "组A")
        a1 = _flow("异步", "组A", async_run=True)
        s2 = _flow("同步2", "组A")
        tab = self._tab(s1, a1, s2)
        tab.run_group("组A")
        self.assertTrue(self._running(tab, s1))
        self.assertTrue(self._running(tab, a1))
        self.assertEqual(tab._queue, [s2.id])       # 同步2 排队等同步1

    def test_running_and_queued_flows_are_skipped(self):
        s1 = _flow("同步1", "组A")
        s2 = _flow("同步2", "组A")
        tab = self._tab(s1, s2)
        tab.run_group("组A")                         # s1 启动、s2 排队
        self.assertEqual(tab.run_group("组A"), 0)    # 再点一次：都已在跑/排队
        self.assertEqual(tab._queue, [s2.id])

    def test_flows_without_steps_are_skipped(self):
        empty = _flow("空流程", "组A", steps=0)
        ok = _flow("有步骤", "组A")
        tab = self._tab(empty, ok)
        self.assertEqual(tab.run_group("组A"), 1)
        self.assertFalse(self._running(tab, empty))
        self.assertTrue(self._running(tab, ok))

    def test_run_group_missing_group_returns_zero(self):
        tab = self._tab(_flow("a", "组A"))
        self.assertEqual(tab.run_group("不存在的组"), 0)

    def test_run_group_async_without_async_flows(self):
        tab = self._tab(_flow("同步", "组A"))
        self.assertEqual(tab.run_group_async("组A"), 0)

    def test_run_group_async_skips_already_running(self):
        a1 = _flow("异步1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.run_group_async("组A")
        self.assertEqual(tab.run_group_async("组A"), 0)
        self.assertEqual(len(FakeRunner.made), 1)      # 没有重复起跑

    def test_run_group_async_missing_group_returns_zero(self):
        tab = self._tab(_flow("a", "组A", async_run=True))
        self.assertEqual(tab.run_group_async("不存在的组"), 0)

    def test_batch_launch_reuses_single_entry(self):
        """源码级契约：批量执行必须复用 _launch_flow / _enqueue，不许自建 FlowRunner。"""
        import inspect
        src = inspect.getsource(FlowTab.run_flows)
        self.assertNotIn("FlowRunner(", src)
        self.assertIn("_launch_flow", src)
        self.assertIn("_enqueue", src)
        self.assertIn("_should_queue", src)


class TestAsyncMarkAndTooltip(_TabCase):
    def test_async_mark_on_idle_flow(self):
        f = _flow("异步流程", "组A", async_run=True)
        tab = self._tab(f)
        self.assertEqual(tab._flow_item_text(f), f"{ASYNC_MARK} 异步流程")

    def test_sync_flow_has_no_mark(self):
        f = _flow("同步流程", "组A")
        tab = self._tab(f)
        self.assertEqual(tab._flow_item_text(f), "同步流程")

    def test_mark_coexists_with_running_and_queued(self):
        a1 = _flow("异步", "组A", async_run=True)
        a2 = _flow("异步排队", "组A", async_run=True)
        s = _flow("同步", "组A")            # 让 a2 变成排队（异步不排队，这里用手工入队模拟）
        tab = self._tab(a1, a2, s)
        tab._launch_flow(a1)
        self.assertEqual(tab._flow_item_text(a1), f"{ASYNC_MARK} ▶ 异步")
        tab._enqueue(a2)                    # 手工入队：验证标记与 ⏳ 的组合
        self.assertEqual(tab._flow_item_text(a2), f"{ASYNC_MARK} ⏳ 异步排队（排队第 1 位）")

    def test_left_item_tooltip_mentions_async(self):
        f = _flow("异步流程", "组A", async_run=True)
        tab = self._tab(f)
        item = tab._flow_item(f.id)
        self.assertIn("异步执行", item.toolTip(0))

    def test_queued_tooltip_wins_over_async(self):
        f = _flow("异步流程", "组A", async_run=True)
        tab = self._tab(f)
        tab._enqueue(f)
        item = tab._flow_item(f.id)
        self.assertEqual(item.toolTip(0), QUEUED_TIP)


class TestHotkeySlot(unittest.TestCase):
    def test_group_hotkeys_default_empty(self):
        self.assertEqual(AppConfig().group_hotkeys, {})

    def test_global_run_async_hotkey_is_removed(self):
        """用户明确不要「运行所有分组的异步流程」这个全局热键：字段与槽位都删掉。"""
        self.assertFalse(hasattr(AppConfig(), "run_async_hotkey"))
        self.assertNotIn("run_async",
                         [s for s, _l, _h in hotkey_policy.collect_hotkeys(AppConfig())])

    def test_main_window_registers_group_hotkeys(self):
        path = os.path.join(os.path.dirname(flow_tab_mod.__file__), "main_window.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def _register_hotkeys")[1].split("def _dispatch_hotkey")[0]
        self.assertIn("group_hotkeys", body)
        self.assertIn("toggle_group_async", body)   # 开关语义：只跑本组异步流程
        self.assertNotIn("run_async_hotkey", body)  # 全局那个已按用户要求移除

    def test_group_slot_and_conflict(self):
        cfg = AppConfig()
        cfg.group_hotkeys = {"组A": "ctrl+alt+j"}
        slots = {s: (label, hk) for s, label, hk in hotkey_policy.collect_hotkeys(cfg)}
        self.assertIn("group_run:组A", slots)
        self.assertEqual(slots["group_run:组A"][0], "分组「组A」")
        f = _flow("某流程", "组A")
        f.hotkey = "ctrl+alt+j"
        cfg.flows = [f]
        self.assertEqual(
            hotkey_policy.find_hotkey_conflict("ctrl+alt+j", cfg, "group_run:组A"),
            "流程「某流程」")

    def test_group_hotkeys_load_and_skip_bad_entries(self):
        import json
        with TempConfigPaths() as tmp:
            path = os.path.join(tmp, "config.json")
            AppConfig().save()
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["group_hotkeys"] = {"组A": "Ctrl+Alt+J", "组B": "", "": "ctrl+alt+k"}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            cfg = AppConfig.load()
            self.assertEqual(cfg.group_hotkeys, {"组A": "ctrl+alt+j"})   # 空名/空热键丢掉

    def test_group_hotkeys_survive_broken_value(self):
        import json
        with TempConfigPaths() as tmp:
            path = os.path.join(tmp, "config.json")
            AppConfig().save()
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["group_hotkeys"] = "not-a-dict"
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            self.assertEqual(AppConfig.load().group_hotkeys, {})


class _FakeHost:
    """GroupRunDialog 的 host 替身（真 FlowTab 需要整窗口，这里只验对话框行为）。"""

    def __init__(self, flows):
        self._flows = list(flows)
        self.cfg = AppConfig()
        self.changed = _Signal()
        self.group_runs: list[str] = []
        self.async_runs: list[str] = []
        self.rename_calls: list[str] = []
        self.rename_result = False
        self.group_hk_calls: list[tuple[str, str]] = []

    def group_hotkey(self, group: str) -> str:
        return str((self.cfg.group_hotkeys or {}).get(group) or "")

    def set_group_hotkey(self, group: str, hotkey: str) -> None:
        self.group_hk_calls.append((group, hotkey))
        hk = str(hotkey or "").strip().lower()
        if hk:
            self.cfg.group_hotkeys[group] = hk
        else:
            self.cfg.group_hotkeys.pop(group, None)

    def flows_in_group(self, group: str):
        return [f for f in self._flows if (f.group or "") == (group or "")]

    def flow_status_text(self, flow: Flow) -> str:
        return flow._status if hasattr(flow, "_status") else ""

    def run_group(self, group: str):
        self.group_runs.append(group)

    def run_group_async(self, group: str):
        self.async_runs.append(group)

    def rename_group_interactive(self, group: str) -> bool:
        self.rename_calls.append(group)
        return self.rename_result


class TestGroupRunDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dlg(self, flows, group="组A"):
        host = _FakeHost(flows)
        dlg = GroupRunDialog(host, group)
        self.addCleanup(dlg.reject)     # 收尾停定时器
        return dlg, host

    def test_lists_group_flows_with_async_mark(self):
        a1 = _flow("异步流程", "组A", async_run=True)
        a2 = _flow("同步流程", "组A")
        dlg, _ = self._dlg([a1, a2, _flow("别人家", "组B")])
        self.assertEqual(dlg.list.count(), 2)
        self.assertTrue(dlg.list.item(0).text().startswith(ASYNC_MARK))
        self.assertFalse(dlg.list.item(1).text().startswith(ASYNC_MARK))
        self.assertIn("1 个异步执行", dlg.title.text())

    def test_status_is_shown_and_colored(self):
        a1 = _flow("异步流程", "组A", async_run=True)
        a1._status = "运行中"
        dlg, _ = self._dlg([a1])
        self.assertIn("运行中", dlg.list.item(0).text())

    def test_run_group_button_calls_host(self):
        dlg, host = self._dlg([_flow("a", "组A")])
        dlg.run_group_btn.click()
        self.assertEqual(host.group_runs, ["组A"])

    def test_run_async_button_calls_host(self):
        """「⚡ 运行本组异步流程」按钮走 run_group_async（与分组热键同一入口）。"""
        dlg, host = self._dlg([_flow("a", "组A", async_run=True)])
        dlg.run_async_btn.click()
        self.assertEqual(host.async_runs, ["组A"])
        self.assertEqual(host.group_runs, [])

    def test_buttons_disabled_appropriately(self):
        """没流程时两个按钮都禁用；只有同步流程时「运行本组异步流程」禁用。"""
        empty, _ = self._dlg([_flow("别人家", "组B")], group="组A")
        self.assertFalse(empty.run_group_btn.isEnabled())
        self.assertFalse(empty.run_async_btn.isEnabled())
        only_sync, _ = self._dlg([_flow("同步", "组A")])
        self.assertTrue(only_sync.run_group_btn.isEnabled())
        self.assertFalse(only_sync.run_async_btn.isEnabled())

    def test_dialog_has_no_global_hotkey_row(self):
        """全局热键已移到「⚙ 设置」页，分组编辑页只留本分组热键。

        2026-09-22 用户反馈「编辑其他分组也显示了一样的热键」——实测不是数据串值
        （本分组热键按分组名各存一份），而是那行**全局热键**在每个分组页面都显示
        同一个值，看着像串值。归置到设置页后这种误会就不存在了。
        """
        dlg, _ = self._dlg([_flow("a", "组A")])
        self.assertFalse(hasattr(dlg, "hotkey_edit"))
        self.assertTrue(hasattr(dlg, "group_hotkey_edit"))

    def test_group_hotkey_label_mentions_async(self):
        """行标签要写明「运行本组异步流程」——热键的作用就是这个。"""
        from PySide6.QtWidgets import QLabel
        dlg, _ = self._dlg([_flow("a", "组A")])
        texts = [lb.text() for lb in dlg.findChildren(QLabel)]
        self.assertTrue(any("运行本组异步流程" in t for t in texts), texts)

    def test_group_conflict_checker_attached(self):
        dlg, _ = self._dlg([_flow("a", "组A")])
        self.assertIsNotNone(dlg.group_hotkey_edit._conflict_checker)

    def test_group_hotkey_writes_via_host(self):
        dlg, host = self._dlg([_flow("a", "组A")])
        dlg.group_hotkey_edit.hotkeyChanged.emit("ctrl+alt+j")   # 等价用户录入
        self.assertEqual(host.group_hk_calls, [("组A", "ctrl+alt+j")])
        self.assertEqual(host.cfg.group_hotkeys, {"组A": "ctrl+alt+j"})

    def test_group_hotkey_clear_button(self):
        from PySide6.QtWidgets import QPushButton
        dlg, host = self._dlg([_flow("a", "组A")])
        dlg.group_hotkey_edit.hotkeyChanged.emit("ctrl+alt+j")
        clears = [b for b in dlg.findChildren(QPushButton) if b.text() == "清除"]
        self.assertEqual(len(clears), 1)      # 只有本分组那一行（全局热键已挪到设置页）
        clears[0].click()
        self.assertEqual(host.cfg.group_hotkeys, {})
        self.assertEqual(host.group_hk_calls[-1], ("组A", ""))

    def test_group_hotkey_prefilled_from_host(self):
        flows = [_flow("a", "组A")]
        host = _FakeHost(flows)
        host.cfg.group_hotkeys = {"组A": "ctrl+alt+j"}
        dlg = GroupRunDialog(host, "组A")
        self.addCleanup(dlg.reject)
        self.assertEqual(dlg.group_hotkey_edit.hotkey(), "ctrl+alt+j")

    def test_timer_stops_on_close(self):
        dlg, _ = self._dlg([_flow("a", "组A")])
        self.assertTrue(dlg._timer.isActive())
        dlg.accept()
        self.assertFalse(dlg._timer.isActive())

    def test_rename_keeps_dialog_open_when_cancelled(self):
        dlg, host = self._dlg([_flow("a", "组A")])
        host.rename_result = False
        dlg.rename_btn.click()
        self.assertEqual(host.rename_calls, ["组A"])
        self.assertTrue(dlg.isVisible() or not dlg.result())   # 没被关掉
        self.assertTrue(dlg._timer.isActive())

    def test_rename_closes_dialog_on_success(self):
        dlg, host = self._dlg([_flow("a", "组A")])
        host.rename_result = True
        dlg.rename_btn.click()
        self.assertFalse(dlg._timer.isActive())


class TestGroupHotkey(_TabCase):
    """每个分组的运行热键：设置 → 分组名**下面一行小字**显示 → 清除 / 改名迁移 / 删组清理。"""

    def test_title_first_line_and_hotkey_second_line(self):
        f = _flow("a", "组A")
        tab = self._tab(f)
        self.assertEqual(tab.group_hotkey("组A"), "")
        self.assertEqual(tab._group_title_text("组A", True), f"{flow_tab_mod.DISCLOSURE_EXPANDED} 组A")   # 首行只有名字
        self.assertEqual(tab._group_hotkey_text("组A"), "")            # 未设热键 → 第二行空
        tab.set_group_hotkey("组A", "Ctrl+Alt+J")
        self.assertEqual(tab.group_hotkey("组A"), "ctrl+alt+j")        # 统一存小写
        self.assertEqual(tab._group_title_text("组A", True), f"{flow_tab_mod.DISCLOSURE_EXPANDED} 组A")  # 首行不掺热键
        self.assertEqual(tab._group_title_text("组A", False), f"{flow_tab_mod.DISCLOSURE_COLLAPSED} 组A")
        self.assertIn("Ctrl", tab._group_hotkey_text("组A"))           # 热键在第二行

    def test_header_shows_hotkey_on_its_own_small_label(self):
        """热键渲染在分组名**下面**的独立 QLabel 里，而不是名字后面的括号。"""
        from PySide6.QtWidgets import QLabel
        tab = self._tab(_flow("a", "组A"))
        tab.set_group_hotkey("组A", "ctrl+alt+j")
        header = tab.list.itemWidget(tab._group_item("组A"), 0)
        btn = header.findChild(flow_tab_mod.QPushButton, "groupTitle")
        hk = header.findChild(QLabel, "groupHotkey")
        self.assertIsNotNone(hk)
        self.assertEqual(btn.text(), f"{flow_tab_mod.DISCLOSURE_EXPANDED} 组A")
        self.assertIn("Ctrl", hk.text())

    def test_header_has_no_hotkey_label_when_unset(self):
        from PySide6.QtWidgets import QLabel
        tab = self._tab(_flow("a", "组A"))
        header = tab.list.itemWidget(tab._group_item("组A"), 0)
        self.assertIsNone(header.findChild(QLabel, "groupHotkey"))

    def test_hotkey_label_uses_smaller_font(self):
        """小字：QSS 里 #groupHotkey 的字号必须**小于**分组名（用户要求「字小一点」）。"""
        import re
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()

        def _pt(anchor: str) -> float:
            seg = src.split(anchor, 1)[1]
            return float(re.search(r"font-size:\s*([\d.]+)pt", seg).group(1))

        self.assertIn("QLabel#groupHotkey", src)
        self.assertLess(_pt("QLabel#groupHotkey"), _pt('QPushButton[groupHeader="true"]'))

    def test_clear_removes_hotkey_and_second_line(self):
        tab = self._tab(_flow("a", "组A"))
        tab.set_group_hotkey("组A", "ctrl+alt+j")
        tab.set_group_hotkey("组A", "")
        self.assertEqual(tab.group_hotkey("组A"), "")
        self.assertEqual(tab.cfg.group_hotkeys, {})
        self.assertEqual(tab._group_hotkey_text("组A"), "")

    def test_ungrouped_never_gets_hotkey(self):
        tab = self._tab(_flow("a", ""))
        tab.set_group_hotkey("", "ctrl+alt+j")
        self.assertEqual(tab.cfg.group_hotkeys, {})

    def test_set_hotkey_notifies_main_window(self):
        """写热键要 refresh_list + changed（主窗口据此重注册热键、防抖保存）。"""
        tab = self._tab(_flow("a", "组A"))
        seen = []
        tab.changed.connect(lambda: seen.append(1))
        tab.set_group_hotkey("组A", "ctrl+alt+j")
        self.assertEqual(len(seen), 1)

    def test_rename_group_migrates_hotkey(self):
        tab = self._tab(_flow("a", "组A"))
        tab.set_group_hotkey("组A", "ctrl+alt+j")
        with mock.patch.object(flow_tab_mod.QInputDialog, "getText",
                               return_value=("组C", True)):
            tab._rename_group("组A")
        self.assertEqual(tab.cfg.group_hotkeys, {"组C": "ctrl+alt+j"})
        self.assertIn("Ctrl", tab._group_hotkey_text("组C"))

    def test_delete_group_clears_hotkey(self):
        tab = self._tab(_flow("a", "组A"))
        tab.set_group_hotkey("组A", "ctrl+alt+j")
        self._msgbox.question.return_value = self._msgbox.Yes
        tab._del_group("组A")
        self.assertEqual(tab.cfg.group_hotkeys, {})


class TestStartupSelection(_TabCase):
    """打开软件时，默认选中最上面分组下的第一个流程。"""

    def test_selects_first_visible_flow(self):
        # _flows 内部顺序与左栏显示顺序不一致（组B 的流程排在前面，但组A 显示在上）
        b1 = _flow("B流程", "组B")
        a1 = _flow("A流程", "组A")
        tab = self._tab(b1, a1)
        self.assertEqual(tab._selected_flow().name, "A流程")

    def test_skips_groups_without_flows(self):
        b1 = _flow("B流程", "组B")
        tab = self._tab(b1)                 # 组A 空、组B 有流程
        self.assertEqual(tab._selected_flow().name, "B流程")

    def test_falls_back_to_ungrouped(self):
        n1 = _flow("零散流程", "")
        tab = self._tab(n1)
        self.assertEqual(tab._selected_flow().name, "零散流程")

    def test_running_flow_still_wins(self):
        b1 = _flow("B流程", "组B")
        a1 = _flow("A流程", "组A")
        tab = self._tab(b1, a1)
        tab._launch_flow(b1)
        tab.refresh_list()
        self.assertEqual(tab._selected_flow().name, "B流程")

    def test_row_heights_split_group_and_flow(self):
        """分组行比流程行高（用户要求「分组高度高一点」+ 2026-10-01「分组和流程要区分」）。

        原来两者都是 47px 的等高行，样式上分不开；现在分组头是通栏色带 + 左侧主色竖条，
        行高由 `GROUP_ROW_H` / `FLOW_ROW_H` **逐条 setSizeHint** 控制
        （QSS 的 `::item{height}` 对两种行一视同仁，分不开）。
        2026-10-01 第二轮按用户「节约空间」把两档都压矮（50/36 → 36/28）。
        """
        with open(flow_tab_mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(f"GROUP_ROW_H = {flow_tab_mod.GROUP_ROW_H}", src)
        self.assertIn(f"FLOW_ROW_H = {flow_tab_mod.FLOW_ROW_H}", src)
        self.assertLessEqual(flow_tab_mod.GROUP_ROW_H, 40)       # 紧凑：原来是 50
        self.assertLessEqual(flow_tab_mod.FLOW_ROW_H, 30)        # 紧凑：原来是 36
        self.assertIn("QWidget#groupHeaderBox", src)             # 分组头整块底色
        self.assertIn("border-left: 3px solid #1668a8", src)     # 段标题的左侧主色竖条
        self.assertNotIn("height: 36px", src)                    # 行高不再由 QSS 统一指定

        # 真建一棵树量一下：分组行必须明显高于流程行
        tab = self._tab(_flow("A流程", "组A"), _flow("B流程", "组A"))
        item = tab._group_item("组A")
        r_group = tab.list.visualItemRect(item).height()
        self.assertGreater(item.childCount(), 0, "组A 下应有流程")
        for i in range(item.childCount()):
            r_flow = tab.list.visualItemRect(item.child(i)).height()
            self.assertLess(r_flow, r_group,
                            f"流程行({r_flow})必须比分组行({r_group})矮才能一眼区分")


class TestGroupHotkeyToggle(_TabCase):
    """分组热键的开关语义（2026-09-22）：跑的是**本组异步流程**，再按一次停止本组异步。

    用户原话：「再次按下热键，可以停止所有当前分组下的异步流程任务」+
    「只需要单独运行当前每个分组下的所有异步流程」。
    规则：本组的异步流程在跑/排队 → 停止（运行中的停掉、排队中的取消排队）；
    本组没有异步活动 → 运行本组的异步流程。同步流程既不会被它启动、也不会被它停掉；
    别的分组不受影响。
    """

    def _two_groups(self):
        a1 = _flow("A1", "组A", async_run=True)
        a2 = _flow("A2", "组A", async_run=True)
        b1 = _flow("B1", "组B", async_run=True)
        tab = self._tab(a1, a2, b1)
        return tab, (a1, a2, b1)

    def test_toggle_starts_async_of_group_when_idle(self):
        tab, (a1, a2, b1) = self._two_groups()
        self.assertEqual(tab.toggle_group_async("组A"), 2)
        self.assertTrue(self._running(tab, a1))
        self.assertTrue(self._running(tab, a2))
        self.assertIsNone(self._runner_of(tab, b1))     # 别组完全没被牵动

    def test_toggle_skips_sync_flows(self):
        async_f = _flow("异步", "组A", async_run=True)
        sync_f = _flow("同步", "组A")
        tab = self._tab(async_f, sync_f)
        self.assertEqual(tab.toggle_group_async("组A"), 1)
        self.assertTrue(self._running(tab, async_f))
        self.assertIsNone(self._runner_of(tab, sync_f))    # 同步流程根本没被启动

    def test_toggle_stops_when_running(self):
        tab, (a1, a2, b1) = self._two_groups()
        tab.toggle_group_async("组A")
        self.assertEqual(tab.toggle_group_async("组A"), 2)     # 再按 = 停止
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)
        self.assertEqual(self._runner_of(tab, a2).stop_calls, 1)

    def test_stop_only_affects_own_group(self):
        tab, (a1, a2, b1) = self._two_groups()
        tab.toggle_group_async("组A")
        tab._launch_flow(b1)                            # 组B 也在跑
        tab.stop_group("组A", only_async=True)
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)
        self.assertEqual(self._runner_of(tab, b1).stop_calls, 0)
        self.assertTrue(self._running(tab, b1))

    def test_stop_does_not_touch_sync_flows(self):
        """only_async=True 的契约：本组同步流程（哪怕在跑）不被动。"""
        async_f = _flow("异步", "组A", async_run=True)
        sync_f = _flow("同步", "组A")
        tab = self._tab(async_f, sync_f)
        tab.toggle_group_async("组A")                   # 只起了异步
        tab._launch_flow(sync_f)                        # 手工把同步也跑起来
        tab.stop_group("组A", only_async=True)
        self.assertEqual(self._runner_of(tab, async_f).stop_calls, 1)
        self.assertEqual(self._runner_of(tab, sync_f).stop_calls, 0)
        self.assertTrue(self._running(tab, sync_f))

    def test_stop_group_still_cancels_queued(self):
        """整组停止（only_async=False）仍会取消排队中的流程。"""
        s1 = _flow("S1", "组A")            # 同步：第一个跑、第二个排队
        s2 = _flow("S2", "组A")
        tab = self._tab(s1, s2, _flow("O", "组B"))
        tab.run_group("组A")
        self.assertEqual(tab._queue, [s2.id])
        self.assertEqual(tab.stop_group("组A"), 2)      # 1 个在跑 + 1 个排队
        self.assertEqual(tab._queue, [])
        self.assertEqual(self._runner_of(tab, s1).stop_calls, 1)

    def test_toggle_ignores_queued_sync_only(self):
        """本组只有「排队中的同步流程」时不算它的活动（热键只管异步）。"""
        s1 = _flow("S1", "组A")            # 同步
        b1 = _flow("B1", "组B")
        tab = self._tab(s1, b1)
        tab._launch_flow(b1)                # 组B 在跑
        tab.run_group("组A")                # 组A 的同步流程只能排队
        self.assertEqual(tab._queue, [s1.id])
        self.assertEqual(tab.toggle_group_async("组A"), 0)   # 本组没有异步可跑
        self.assertEqual(tab._queue, [s1.id])                # 排队不受影响

    def test_hotkey_stop_cancels_queued_sync(self):
        """真机反馈的核心场景：整组启动后按热键停止，排队中的同步流程不能接着开跑。

        原来只停异步、不取消排队 → 异步一停队列立刻被放行，同步流程马上开跑，
        用户看到"还有流程在跑"，就以为停止没生效。
        """
        a1 = _flow("异步", "组A", async_run=True)
        s1 = _flow("同步1", "组A")
        s2 = _flow("同步2", "组A")
        other = _flow("别组", "组B")
        tab = self._tab(a1, s1, s2, other)
        tab.run_group("组A")                     # 整组启动：异步跑起来、同步排队
        self.assertTrue(self._running(tab, a1))
        self.assertEqual(tab._queue, [s1.id, s2.id])
        tab.toggle_group_async("组A")            # 按热键停止
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)
        self.assertEqual(tab._queue, [])         # 排队的同步被取消
        self.assertFalse(self._running(tab, s1))        # 没有"停完又开跑"
        self.assertIsNone(self._runner_of(tab, other))  # 别组不受影响

    def test_stop_group_cancels_queued_async_too(self):
        """排队中的异步同样被取消（它还没开始跑，取消不影响任何正在做的事）。"""
        a1 = _flow("异步1", "组A", async_run=True)
        a2 = _flow("异步2", "组A", async_run=True)
        tab = self._tab(a1, a2)
        tab._enqueue(a2)
        self.assertEqual(tab.stop_group("组A", only_async=True), 1)
        self.assertEqual(tab._queue, [])

    def test_stop_when_idle_returns_zero(self):
        tab, _ = self._two_groups()
        self.assertEqual(tab.stop_group("组A", only_async=True), 0)

    def test_after_stop_toggle_starts_again(self):
        """停止后流程收尾（is_running 归位），再按热键应该重新启动。"""
        tab, (a1, a2, _b1) = self._two_groups()
        tab.toggle_group_async("组A")
        tab.toggle_group_async("组A")
        for f in (a1, a2):
            self._runner_of(tab, f).finish()             # 模拟线程收尾
        self.assertEqual(tab.toggle_group_async("组A"), 2)    # 又能启动

    def test_hotkey_binds_toggle_async(self):
        """源码级契约：分组热键必须绑 toggle_group_async（绑 run_group 就没法再按停）。"""
        path = os.path.join(os.path.dirname(flow_tab_mod.__file__), "main_window.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def _register_hotkeys")[1].split("def _dispatch_hotkey")[0]
        self.assertIn("toggle_group_async", body)
        self.assertNotIn("flow_tab.run_group(", body)


class TestRunningOverview(_TabCase):
    """running_overview()：为左上角悬浮窗提供「来源/分组/热键」结构。"""

    def test_empty_when_nothing_running(self):
        tab = self._tab(_flow("a", "组A", async_run=True))
        ov = tab.running_overview()
        self.assertEqual(ov["source"], "")
        self.assertEqual(ov["flows"], [])

    def test_group_async_source(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1, _flow("b", "组B", async_run=True))
        tab.cfg.group_hotkeys = {"组A": "ctrl+alt+1"}
        tab.run_group_async("组A")
        ov = tab.running_overview()
        self.assertEqual(ov["source"], "group_async")
        self.assertEqual(ov["group"], "组A")
        self.assertEqual([f[0] for f in ov["flows"]], ["a1"])
        self.assertEqual(ov["flows"][0][1], "ctrl+alt+1")   # 分组热键

    def test_group_all_source(self):
        a1 = _flow("a1", "组A", async_run=True)
        a2 = _flow("a2", "组A")               # 同步：会排队，不算 running
        tab = self._tab(a1, a2)
        tab.cfg.group_hotkeys = {"组A": "ctrl+alt+1"}
        tab.run_group("组A")
        ov = tab.running_overview()
        self.assertEqual(ov["source"], "group_all")
        self.assertEqual(ov["group"], "组A")
        self.assertEqual([f[0] for f in ov["flows"]], ["a1"])   # 只列正在运行的异步

    def test_single_source_uses_flow_hotkey(self):
        a1 = _flow("a1", "组A", async_run=True)
        a1.hotkey = "f6"
        tab = self._tab(a1)
        tab.toggle_flow(a1.id)
        ov = tab.running_overview()
        self.assertEqual(ov["source"], "single")
        self.assertEqual(ov["group"], "")
        self.assertEqual(ov["flows"][0][1], "f6")    # 单流程用自身热键

    def test_single_no_hotkey_gives_empty(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.toggle_flow(a1.id)
        ov = tab.running_overview()
        self.assertEqual(ov["flows"][0][1], "")

    def test_source_resets_when_all_finish(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.run_group_async("组A")
        self.assertEqual(tab.running_overview()["source"], "group_async")
        self._runner_of(tab, a1).finish()
        ov = tab.running_overview()
        self.assertEqual(ov["source"], "")
        self.assertEqual(ov["flows"], [])

    def test_group_source_reports_async_flag(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.run_group_async("组A")
        self.assertEqual(tab.running_overview()["flows"][0][2], True)


class TestRestartWhileStopping(_TabCase):
    """频繁启停：线程收尾期间再点「运行」不能丢（登记待重启，收尾后自动起）。

    真机现象：按下停止后线程还要几十毫秒才结束（is_running 仍 True），这期间
    再按一次本意是「重新启动」，若被当成「又停一次」就会被吞掉 —— 用户看到的是
    「按了运行没反应」。
    """

    def test_toggle_while_stopping_marks_pending(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.toggle_flow(a1.id)                 # 启动
        tab.toggle_flow(a1.id)                 # 停止（线程开始收尾）
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)
        self.assertTrue(self._runner_of(tab, a1).stopping)
        tab.toggle_flow(a1.id)                 # 收尾期间再点 = 想再跑
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)   # 没有又停一次
        self.assertIn(a1.id, tab._restart_pending)

    def test_pending_auto_restarts_after_finish(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.toggle_flow(a1.id)
        tab.toggle_flow(a1.id)
        tab.toggle_flow(a1.id)                 # 待重启
        made = len(FakeRunner.made)
        self._runner_of(tab, a1).finish("已手动停止", ok=False)
        self.assertEqual(len(FakeRunner.made), made + 1)      # 起了新 runner
        self.assertTrue(self._running(tab, a1))
        self.assertNotIn(a1.id, tab._restart_pending)

    def test_finish_without_pending_does_not_restart(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.toggle_flow(a1.id)
        made = len(FakeRunner.made)
        self._runner_of(tab, a1).finish()      # 正常跑完，没有待重启意图
        self.assertEqual(len(FakeRunner.made), made)
        self.assertFalse(self._running(tab, a1))

    def test_stop_all_clears_pending(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.toggle_flow(a1.id)
        tab.toggle_flow(a1.id)
        tab.toggle_flow(a1.id)                 # 待重启
        tab.stop_all()
        self.assertEqual(tab._restart_pending, set())
        self._runner_of(tab, a1).finish("已手动停止", ok=False)
        self.assertFalse(self._running(tab, a1))    # 不会偷偷起来

    def test_group_hotkey_restarts_while_stopping(self):
        """分组热键：本组异步都在收尾时再按 = 想再跑一次（不丢启动）。"""
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.run_group_async("组A")             # 启动
        tab.toggle_group_async("组A")          # 停止
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)
        tab.toggle_group_async("组A")          # 收尾期间再按
        self.assertIn(a1.id, tab._restart_pending)
        made = len(FakeRunner.made)
        self._runner_of(tab, a1).finish("已手动停止", ok=False)
        self.assertEqual(len(FakeRunner.made), made + 1)
        self.assertTrue(self._running(tab, a1))

    def test_group_active_keeps_definition_but_stopping_is_flagged(self):
        a1 = _flow("a1", "组A", async_run=True)
        tab = self._tab(a1)
        tab.run_group_async("组A")
        tab.stop_group("组A", only_async=True)
        self.assertEqual(self._runner_of(tab, a1).stop_calls, 1)
        self.assertTrue(tab._is_stopping(a1.id))
        tab.toggle_group_async("组A")          # 只剩收尾中的 → 走「再启动」
        self.assertIn(a1.id, tab._restart_pending)


if __name__ == "__main__":
    unittest.main()
