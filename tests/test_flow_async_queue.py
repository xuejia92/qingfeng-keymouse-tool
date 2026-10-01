"""流程「同步排队 / 异步并行」执行方式的测试（Flow.async_run）。

覆盖：
- 数据模型：默认同步（False）、旧数据缺字段回退 False、字典往返；
- 流程编辑对话框：复选框默认不勾、回填、apply_to 写回、说明随勾选变化；
- 排队门控（FlowTab._queue）：
  同步流程遇忙排队且 FIFO、异步流程直接并行、流程结束按顺序放行、
  异步流程在跑时不放行同步流程、排队期间改成异步则直接放行、
  「运行/停止」第二次点 = 取消排队、停止全部清空队列、删除流程移出队列、
  定时/热键触发同一流程不重复入队、队首流程被清空则跳过；
- 界面交互：左栏 ⏳ 排队位次与颜色、运行按钮变「✖ 取消排队」、右栏标题、排队期间可编辑。

全部用替身 FlowRunner（不真起线程、不真跑步骤），确保用例既快又确定。
"""
from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app.config import (AppConfig, Flow, FlowStep, flow_from_dict, flow_to_dict)
from app.ui import flow_tab as flow_tab_mod
from app.ui.flow_dialog import FlowMetaDialog
from app.ui.flow_tab import QUEUED_TIP, FlowTab
from tests._env import TempConfigPaths


# ---------------- 替身 FlowRunner ----------------

class _Signal:
    """最小的 Qt 信号替身：只支持 connect / emit（同步直调）。"""

    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for cb in list(self._callbacks):
            cb(*args)


class FakeRunner:
    """替身 FlowRunner：不起线程，由用例显式调用 finish() 结束。"""

    made: list = []

    def __init__(self, flow: Flow):
        self.flow = flow
        self.is_running = False
        self.current_step_index = -1
        self.last_step_ok = True
        self.last_step_reason = ""
        self.stop_calls = 0
        self.stateChanged = _Signal()
        self.stepStarted = _Signal()
        FakeRunner.made.append(self)

    def start(self) -> bool:
        if self.is_running or not self.flow.steps:
            return False
        self.is_running = True
        self.stateChanged.emit("running", "", True)
        return True

    def stop(self) -> None:
        self.stop_calls += 1

    def finish(self, reason: str = "已完成 1 轮", ok: bool = True) -> None:
        """模拟流程跑完：is_running 归位后发 stopped 状态。"""
        self.is_running = False
        self.stateChanged.emit("stopped", reason, ok)


def _flow(name: str, async_run: bool = False, steps: int = 1) -> Flow:
    return Flow(name=name, async_run=async_run,
                steps=[FlowStep(type="wait") for _ in range(steps)])


class _FlowTabCase(unittest.TestCase):
    """公共脚手架：临时路径隔离 + 替身 runner + 常用断言。"""

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
        self.cfg = cfg
        tab = FlowTab(cfg)
        tab.list.setCurrentItem(tab.list.topLevelItem(0).child(0))
        return tab

    @staticmethod
    def _runner_of(tab: FlowTab, flow: Flow):
        return tab._runners.get(flow.id)

    @staticmethod
    def _item_text(tab: FlowTab, flow: Flow) -> str:
        """左栏流程条目的文本，**去掉圆点前缀**（FLOW_BULLET，2026-10-01 起用来区分分组）。

        本文件关心的是状态标记（▶ 运行 / ⏳ 排队第 N 位 / 名字），装饰不算。
        """
        item = tab._flow_item(flow.id)
        if item is None:
            return ""
        text = item.text(0)
        bullet = flow_tab_mod.FLOW_BULLET
        return text[len(bullet):] if text.startswith(bullet) else text


# ---------------- 数据模型 ----------------

class TestFlowAsyncField(unittest.TestCase):
    """Flow.async_run：默认同步，缺失字段回退同步，字典往返不丢。"""

    def test_default_is_sync(self):
        self.assertFalse(Flow().async_run)

    def test_normalized_to_bool(self):
        self.assertTrue(Flow(async_run="yes").async_run)
        self.assertTrue(Flow(async_run=1).async_run)
        self.assertFalse(Flow(async_run=0).async_run)
        self.assertFalse(Flow(async_run=None).async_run)

    def test_dict_roundtrip_keeps_flag(self):
        back = flow_from_dict(flow_to_dict(_flow("F", async_run=True)))
        self.assertTrue(back.async_run)
        back = flow_from_dict(flow_to_dict(_flow("F")))
        self.assertFalse(back.async_run)

    def test_legacy_data_without_field_is_sync(self):
        """旧流程 json 没有 async_run 字段 → 按默认同步处理，不报错。"""
        data = flow_to_dict(_flow("老流程"))
        data.pop("async_run")
        self.assertFalse(flow_from_dict(data).async_run)


# ---------------- 流程编辑对话框 ----------------

class TestFlowMetaDialogAsync(unittest.TestCase):
    """「编辑流程」里的执行方式复选框。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dlg(self, flow: Flow) -> FlowMetaDialog:
        dlg = FlowMetaDialog(flow, create=False, groups=["默认"])
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_default_unchecked_for_sync_flow(self):
        dlg = self._dlg(_flow("F"))
        self.assertFalse(dlg.async_check.isChecked())
        self.assertIn("同步执行", dlg.async_hint.text())
        self.assertIn("排队", dlg.async_hint.text())

    def test_checked_when_async(self):
        dlg = self._dlg(_flow("F", async_run=True))
        self.assertTrue(dlg.async_check.isChecked())
        self.assertIn("异步执行", dlg.async_hint.text())
        self.assertIn("并行", dlg.async_hint.text())

    def test_hint_follows_toggle(self):
        dlg = self._dlg(_flow("F"))
        dlg.async_check.setChecked(True)
        self.assertIn("异步执行", dlg.async_hint.text())
        dlg.async_check.setChecked(False)
        self.assertIn("同步执行", dlg.async_hint.text())

    def test_apply_to_writes_flag(self):
        flow = _flow("F")
        dlg = self._dlg(flow)
        dlg.async_check.setChecked(True)
        dlg.apply_to(flow)
        self.assertTrue(flow.async_run)

    def test_apply_to_keeps_sync_when_unchecked(self):
        flow = _flow("F", async_run=True)
        dlg = self._dlg(flow)
        dlg.async_check.setChecked(False)
        dlg.apply_to(flow)
        self.assertFalse(flow.async_run)

    def test_roundtrip_through_dialog(self):
        flow = _flow("F", async_run=True)
        dlg = self._dlg(flow)
        dlg.apply_to(flow)
        self.assertTrue(flow.async_run)
        self.assertTrue(self._dlg(flow).async_check.isChecked())


# ---------------- 排队门控 ----------------

class TestQueuePolicy(_FlowTabCase):
    """同步排队 / 异步并行 的核心规则。"""

    def test_first_flow_starts_immediately(self):
        a = _flow("A")
        tab = self._tab(a)
        tab.toggle_flow(a.id)
        self.assertTrue(self._runner_of(tab, a).is_running)
        self.assertEqual(tab._queue, [])

    def test_second_sync_flow_queues(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        self.assertEqual(tab._queue, [b.id], "同步流程遇忙应排队")
        self.assertIsNone(self._runner_of(tab, b), "排队中的流程不该建 runner")
        self.assertEqual(len(FakeRunner.made), 1, "排队不该启动线程")

    def test_async_flow_never_queues(self):
        a, c = _flow("A"), _flow("C", async_run=True)
        tab = self._tab(a, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(c.id)
        self.assertEqual(tab._queue, [])
        self.assertTrue(self._runner_of(tab, c).is_running, "异步流程应立即并行运行")

    def test_sync_flow_waits_for_async_flow(self):
        """同步流程与「任何正在运行的流程」互斥：异步在跑时同步仍要排队。"""
        c, b = _flow("C", async_run=True), _flow("B")
        tab = self._tab(c, b)
        tab.toggle_flow(c.id)
        tab.toggle_flow(b.id)
        self.assertEqual(tab._queue, [b.id])

    def test_fifo_order(self):
        a, b, c = _flow("A"), _flow("B"), _flow("C")
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(c.id)
        self.assertEqual(tab._queue, [b.id, c.id])

    def test_queue_is_released_in_order_after_finish(self):
        a, b, c = _flow("A"), _flow("B"), _flow("C")
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(c.id)

        self._runner_of(tab, a).finish()
        self.assertEqual(tab._queue, [c.id], "A 结束后应放行 B")
        self.assertTrue(self._runner_of(tab, b).is_running)
        self.assertEqual(sum(1 for r in tab._runners.values() if r.is_running), 1,
                         "同步流程同一时刻只能跑一个")

        self._runner_of(tab, b).finish()
        self.assertEqual(tab._queue, [])
        self.assertTrue(self._runner_of(tab, c).is_running)

    def test_queue_waits_for_running_async_flow(self):
        """异步流程还在跑时，前一个同步流程结束也不能放行下一个同步流程。"""
        a, b, c = _flow("A"), _flow("B"), _flow("C", async_run=True)
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)     # A 同步，跑
        tab.toggle_flow(b.id)     # B 同步，排队
        tab.toggle_flow(c.id)     # C 异步，并行
        self.assertTrue(self._runner_of(tab, c).is_running)

        self._runner_of(tab, a).finish()
        self.assertEqual(tab._queue, [b.id], "C 还在跑，B 必须继续等")
        self.assertIsNone(self._runner_of(tab, b))

        self._runner_of(tab, c).finish()
        self.assertEqual(tab._queue, [])
        self.assertTrue(self._runner_of(tab, b).is_running, "C 结束后 B 才开跑")

    def test_queued_flow_switched_to_async_is_released(self):
        """排队期间用户把流程改成异步：不必再等，直接放行。"""
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        b.async_run = True
        self._runner_of(tab, a).finish()
        self.assertEqual(tab._queue, [])
        self.assertTrue(self._runner_of(tab, b).is_running)

    def test_second_click_cancels_queue(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(b.id)                 # 再点一次 = 取消排队
        self.assertEqual(tab._queue, [])
        self.assertIsNone(self._runner_of(tab, b))
        # A 结束也不会把已取消的 B 拉起来
        self._runner_of(tab, a).finish()
        self.assertIsNone(self._runner_of(tab, b))

    def test_queued_flow_without_steps_is_skipped(self):
        a, b, c = _flow("A"), _flow("B"), _flow("C")
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(c.id)
        b.steps = []                          # 排队期间步骤被删空
        self._runner_of(tab, a).finish()
        self.assertEqual(tab._queue, [])
        self.assertIsNone(self._runner_of(tab, b))
        self.assertTrue(self._runner_of(tab, c).is_running, "空流程应被跳过，继续放行下一个")

    def test_stop_all_clears_queue(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.stop_all()
        self.assertEqual(tab._queue, [])
        self.assertEqual(self._runner_of(tab, a).stop_calls, 1)
        self._runner_of(tab, a).finish("已手动停止")
        self.assertIsNone(self._runner_of(tab, b), "紧急停止后队列里的流程不该被拉起")

    def test_delete_flow_removes_from_queue(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab._select_flow_item(b.id)
        # 删除确认框是模态的，offscreen 下会挂住：换成固定「是」
        with mock.patch.object(flow_tab_mod.QMessageBox, "question",
                               return_value=flow_tab_mod.QMessageBox.Yes):
            tab._del_flow()
        self.assertEqual(tab._queue, [])
        self.assertNotIn(b, tab._flows)

    def test_queued_flag_accessors(self):
        a, b, c = _flow("A"), _flow("B"), _flow("C")
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(c.id)
        self.assertTrue(tab.is_queued(b.id))
        self.assertFalse(tab.is_queued(a.id))
        self.assertEqual(tab.queued_names(), ["B", "C"])
        self.assertEqual(tab.running_names(), ["A"])


class TestStartIfIdleWithQueue(_FlowTabCase):
    """定时任务 / 中键菜单等外部触发路径与排队的关系。"""

    def test_scheduled_trigger_queues_instead_of_skipping(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        self.assertTrue(tab.start_flow_if_idle(b.id, silent=True),
                        "同步流程遇忙应排队，而不是直接跳过")
        self.assertEqual(tab._queue, [b.id])
        self.assertIn(b.id, tab._silent, "静默标记应保留到真正跑完")

    def test_queued_flow_not_queued_twice(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        self.assertTrue(tab.start_flow_if_idle(b.id))
        self.assertFalse(tab.start_flow_if_idle(b.id), "已在排队的流程不应重复入队")
        self.assertEqual(tab._queue, [b.id])

    def test_running_flow_still_skipped(self):
        a = _flow("A")
        tab = self._tab(a)
        tab.toggle_flow(a.id)
        self.assertFalse(tab.start_flow_if_idle(a.id), "已在运行的流程仍应跳过")

    def test_silent_flag_cleared_after_queued_run(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.start_flow_if_idle(b.id, silent=True)
        self._runner_of(tab, a).finish()
        self._runner_of(tab, b).finish()
        self.assertEqual(tab._silent, set())


class TestSingleStepAndQueue(_FlowTabCase):
    """单步执行是编辑期试跑：不排队，但排队中的流程要先取消排队。"""

    def test_single_step_blocked_while_queued(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab._select_flow_item(b.id)
        before = len(FakeRunner.made)
        tab._run_single_step(0)
        self.assertEqual(len(FakeRunner.made), before, "排队中的流程不该被单步启动")
        self.assertIsNone(tab._runners.get(b.id))

    def test_single_step_allowed_when_idle(self):
        b = _flow("B")
        tab = self._tab(b)
        tab._run_single_step(0)
        self.assertIsNotNone(tab._runners.get(b.id))


class TestQueueUi(_FlowTabCase):
    """排队状态在界面上的呈现。"""

    def test_left_item_shows_position(self):
        a, b, c = _flow("A"), _flow("B"), _flow("C")
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(c.id)
        self.assertEqual(self._item_text(tab, b), "⏳ B（排队第 1 位）")
        self.assertEqual(self._item_text(tab, c), "⏳ C（排队第 2 位）")
        self.assertTrue(self._item_text(tab, a).startswith("▶ "))

    def test_left_item_color_and_tooltip(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        item = tab._flow_item(b.id)
        self.assertEqual(item.foreground(0).color(), QColor("#1668a8"))
        self.assertEqual(item.toolTip(0), QUEUED_TIP)

    def test_position_refreshes_after_dequeue(self):
        a, b, c = _flow("A"), _flow("B"), _flow("C")
        tab = self._tab(a, b, c)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.toggle_flow(c.id)
        tab.toggle_flow(b.id)                 # B 取消排队
        self.assertEqual(self._item_text(tab, c), "⏳ C（排队第 1 位）",
                         "有人出队后，其余人的位次要跟着刷新")
        self.assertEqual(self._item_text(tab, b), "B")

    def test_item_text_returns_to_name_after_release(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        self._runner_of(tab, a).finish()
        self.assertTrue(self._item_text(tab, b).startswith("▶ "),
                        "放行后条目应从「排队」变成「运行中」")
        self.assertEqual(tab._flow_item(b.id).toolTip(0), "")

    def test_left_color_after_release(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        self._runner_of(tab, a).finish()
        self.assertEqual(tab._flow_item(b.id).foreground(0).color(),
                         QColor("#27ae60"))

    def test_run_button_toggles_to_cancel(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab._select_flow_item(b.id)
        self.assertEqual(tab.run_btn.text(), "✖ 取消排队")
        self.assertEqual(tab.run_btn.toolTip(), QUEUED_TIP)
        tab.toggle_flow(b.id)
        tab._reload_steps()
        self.assertEqual(tab.run_btn.text(), "▶ 运行/停止")

    def test_right_title_marks_queued(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab._select_flow_item(b.id)
        self.assertIn("排队中（第 1 位）", tab.right_title.text())

    def test_queued_flow_still_editable(self):
        """排队中的流程还没开始跑，右栏不应锁死（放行时才深拷贝执行）。"""
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab._select_flow_item(b.id)
        self.assertTrue(tab.step_list.isEnabled(), "排队中应可继续编辑步骤")
        tab._select_flow_item(a.id)
        self.assertFalse(tab.step_list.isEnabled(), "运行中仍应锁定编辑")

    def test_queue_survives_list_rebuild(self):
        a, b = _flow("A"), _flow("B")
        tab = self._tab(a, b)
        tab.toggle_flow(a.id)
        tab.toggle_flow(b.id)
        tab.refresh_list()
        self.assertEqual(self._item_text(tab, b), "⏳ B（排队第 1 位）")


class TestQueueGateSourceContract(unittest.TestCase):
    """源码级契约：启动流程必须走 _launch_flow（排队门控的唯一必经之路）。

    将来再有人新增「启动流程」的入口，若绕开 _launch_flow 自建 FlowRunner，
    就会同时漏掉排队判断、左栏状态与信号接线——把这条路用源码断言钉死。
    单步执行是唯一有意的例外（编辑期试跑，不排队，见 _run_single_step）。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = inspect.getsource(flow_tab_mod)

    def test_flowrunner_constructed_in_exactly_two_places(self):
        self.assertEqual(self.src.count("FlowRunner("), 2,
                         "启动流程只能经 _launch_flow（另加单步执行一处）")
        self.assertIn("FlowRunner(", inspect.getsource(flow_tab_mod.FlowTab._launch_flow))

    def test_toggle_flow_goes_through_the_gate(self):
        src = inspect.getsource(flow_tab_mod.FlowTab.toggle_flow)
        self.assertNotIn("FlowRunner(", src)
        for call in ("_should_queue(", "_enqueue(", "_dequeue(", "_launch_flow("):
            self.assertIn(call, src, f"toggle_flow 缺少 {call} 分支")

    def test_finish_paths_drain_the_queue(self):
        for name in ("_on_state", "_on_single_state"):
            src = inspect.getsource(getattr(flow_tab_mod.FlowTab, name))
            self.assertIn("_drain_queue()", src, f"{name} 必须推进排队")


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
