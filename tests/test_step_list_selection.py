# -*- coding: utf-8 -*-
"""右侧步骤列表的「选中不该被刷新吞掉」契约（2026-10-02）。

**用户反馈**：「有流程正在运行，操作其他流程，比如选中流程里面某个模块，就会刷新掉，
导致无法选中、进行拖动或注释」。
**根因**：`_update_run_button` 里无条件 `step_list.clear()` + 重新 addItem，
而 `_on_step_started`（流程每推进一步）都会走到它 —— 于是只要**任何**流程在运行，
每步都会把你正在看的列表重建一次，选中项随之消失。

修法两条，本文件逐条钉住：
1. `_refresh_step_list` 按**内容签名**决定要不要重建；签名没变就一个字节都不动；
2. 确实要重建时备份 + 恢复选中（行号与多选），并按流程各自记忆选中位置。

⚠️ offscreen 平台跑，只断言对象身份 / 行号 / 数据角色，不断言字体度量。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.config import (AppConfig, Flow, FlowStep, default_step_params,
                        step_missing_required)
from app.ui import flow_tab as flow_tab_mod
from app.ui.flow_dialog import _STEP_MISSING_ROLE
from app.ui.flow_tab import FlowTab
from tests._env import TempConfigPaths


class StepSelectionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()
        self.cfg = AppConfig()
        self.cfg.collapsed_module_groups = []

        self.flow_a = Flow(name="流程A")
        self.flow_a.steps = [self._step("wait") for _ in range(3)]
        self.flow_b = Flow(name="流程B")
        self.flow_b.steps = [self._step("log")]
        self.cfg.flows = [self.flow_a, self.flow_b]

        self.tab = FlowTab(self.cfg)      # 构造时自动选中第一个流程
        self._settle()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _step(self, t):
        return FlowStep(type=t, params=default_step_params(t))

    def _settle(self, n: int = 3):
        for _ in range(n):
            self._app.processEvents()

    def _items(self):
        return [self.tab.step_list.item(i) for i in range(self.tab.step_list.count())]

    def _selected_rows(self):
        return sorted(i.row() for i in self.tab.step_list.selectedIndexes())

    def _select(self, row: int, extra_rows=()):
        self.tab.step_list.setCurrentRow(row)
        for r in extra_rows:
            self.tab.step_list.item(r).setSelected(True)
        self._settle()


class TestNoSenselessRebuild(StepSelectionCase):
    def test_unchanged_content_is_not_rebuilt(self):
        """内容没变时刷新不该重建列表（否则选中/拖动全被清掉）。"""
        before = self._items()
        self.tab._reload_steps()
        self._settle()
        after = self._items()
        self.assertEqual(len(before), len(after))
        for a, b in zip(before, after):
            self.assertIs(a, b, "内容没变却重建了列表")

    def test_selection_survives_when_other_flow_is_running(self):
        """★ 用户报的场景：别的流程正在跑（每步都会触发刷新），选中不能被清掉。"""
        self._select(1, extra_rows=(2,))
        self.assertEqual(self.tab.step_list.currentRow(), 1)
        self.assertEqual(self._selected_rows(), [1, 2])

        # 另一个流程每推进一步 -> _on_step_started -> _reload_steps
        self.tab._on_step_started(self.flow_b.id, 0, "打印输出")
        self._settle()

        self.assertEqual(self.tab.step_list.currentRow(), 1, "选中行被刷新掉了")
        self.assertEqual(self._selected_rows(), [1, 2], "多选被刷新掉了")

    def test_selection_survives_many_running_steps(self):
        """连续跑很多步也一样（模拟流程跑很久）。"""
        self._select(2)
        for i in range(8):
            self.tab._on_step_started(self.flow_b.id, 0, f"步骤{i}")
        self._settle()
        self.assertEqual(self.tab.step_list.currentRow(), 2)

    def test_left_list_refresh_alone_does_not_touch_step_selection(self):
        """只刷新左栏条目（状态变色）不该动步骤列表。"""
        self._select(1)
        before = self._items()[1]
        self.tab._update_left_item(self.flow_b.id)
        self._settle()
        self.assertIs(self.tab.step_list.item(1), before)
        self.assertEqual(self.tab.step_list.currentRow(), 1)


class TestRebuildRestoresSelection(StepSelectionCase):
    def test_content_change_rebuilds_and_keeps_selection(self):
        """内容真变了（改步骤名）就该重建，但选中要保住。"""
        self._select(1, extra_rows=(2,))
        before0 = self.tab.step_list.item(0)
        self.flow_a.steps[1].name = "改名后的步骤"
        self.tab._reload_steps()
        self._settle()
        self.assertIsNot(self.tab.step_list.item(0), before0, "内容变了却没重建")
        self.assertEqual(self.tab.step_list.currentRow(), 1)
        self.assertEqual(self._selected_rows(), [1, 2])
        self.assertIn("改名后的步骤", self.tab.step_list.item(1).text())

    def test_comment_toggle_rebuilds_and_keeps_selection(self):
        """勾「注释」后列表要更新（加 // 前缀），选中同样不能丢。"""
        self._select(1)
        self.flow_a.steps[1].commented = True
        self.tab._reload_steps()
        self._settle()
        self.assertTrue(self.tab.step_list.item(1).text().startswith("2. // "))
        self.assertEqual(self.tab.step_list.currentRow(), 1)

    def test_missing_param_red_frame_updates_both_ways(self):
        """缺参红框要能跟着参数变化更新（签名里必须带上 step_missing_required）。"""
        need = FlowStep(type="press")
        need.params["keys"] = ""                 # 人为制造「必填没填」
        self.assertEqual(step_missing_required(need), ["按键"])
        self.flow_a.steps[0] = need
        self.tab._reload_steps()
        self._settle()
        self.assertEqual(self.tab.step_list.item(0).data(_STEP_MISSING_ROLE), ["按键"])

        need.params["keys"] = "ctrl+s"           # 填上 -> 红框该消失
        self.tab._reload_steps()
        self._settle()
        self.assertIsNone(self.tab.step_list.item(0).data(_STEP_MISSING_ROLE))

    def test_row_beyond_new_length_is_dropped(self):
        """重建后行号越界（步骤被删短了）时不能报错，也不该留下非法选中。"""
        self._select(2)
        del self.flow_a.steps[1:]                # 只剩 1 行
        self.tab._reload_steps()
        self._settle()
        self.assertEqual(self.tab.step_list.count(), 1)
        self.assertLess(self.tab.step_list.currentRow(), 1)


class TestSelectionMemoryPerFlow(StepSelectionCase):
    def test_switching_flow_restores_that_flows_selection(self):
        """A/B 之间来回切，各自记住自己上次选中的行。"""
        self._select(2)                          # A 里选第 3 行
        self.tab._select_flow_item(self.flow_b.id)
        self._settle()
        self.assertEqual(self.tab._selected_flow().name, "流程B")

        self.tab._select_flow_item(self.flow_a.id)
        self._settle()
        self.assertEqual(self.tab.step_list.currentRow(), 2, "切回 A 时没还原选中")

    def test_each_flow_keeps_its_own_row(self):
        """两个流程各选不同行，互不串味。"""
        self._select(0)
        self.tab._select_flow_item(self.flow_b.id)
        self._settle()
        self.assertEqual(self.tab.step_list.count(), 1)
        self.tab._select_flow_item(self.flow_a.id)
        self._settle()
        self.assertEqual(self.tab.step_list.currentRow(), 0)


class TestRunningFlowStillTracksCurrentStep(StepSelectionCase):
    def test_running_flow_list_keeps_marking_current_step(self):
        """运行中流程自己的列表仍要滚动到「▶ 当前步」——即该重建时照样重建。"""
        runner = mock.Mock()
        runner.is_running = True
        runner.current_step_index = 1
        self.tab._runners[self.flow_a.id] = runner
        self.tab._reload_steps()
        self._settle()
        self.assertIn("▶ ", self.tab.step_list.item(1).text())

        runner.current_step_index = 2            # 推进到下一步 -> 标记要跟着走
        self.tab._reload_steps()
        self._settle()
        self.assertIn("▶ ", self.tab.step_list.item(2).text())
        self.assertNotIn("▶ ", self.tab.step_list.item(1).text())


class TestSingleRowSelectionSemantics(StepSelectionCase):
    """`setCurrentRow` 必须是「单选」语义。

    Qt 在 ExtendedSelection 下的 `setCurrentRow` 只加选不清除（实测：先选 3 再
    setCurrentRow(1) → [1, 3]）。列表刷新又会**恢复**用户的选中，两者一叠加，
    程序化设当前行就会带上旧选中，让随后的删除/复制被误判成「多选」。
    """

    def test_step_list_set_current_row_is_single_select(self):
        sl = self.tab.step_list
        sl.setCurrentRow(2)
        sl.setCurrentRow(0)
        self.assertEqual(sorted(i.row() for i in sl.selectedIndexes()), [0])
        self.assertEqual(sl.currentRow(), 0)

    def test_set_current_row_after_multi_select_collapses_to_one(self):
        self._select(1, extra_rows=(2,))
        self.assertEqual(self._selected_rows(), [1, 2])
        self.tab.step_list.setCurrentRow(0)
        self.assertEqual(self._selected_rows(), [0])

    def test_drop_after_selection_selects_only_the_new_row(self):
        """★ 回归：选中第 3 行后拖入模块，只能选中新行——否则按删除会删掉一串步骤。"""
        self._select(2)
        with mock.patch.object(self.tab, "_edit_step_param"):
            self.tab._on_step_dropped("wait", 0)
            self._settle()
        self.assertEqual(self._selected_rows(), [0],
                         "拖入后留下了旧的选中，删除时会被当成多选")

    def test_delete_uses_single_step_path_after_refresh(self):
        """刷新恢复选中后再删，走的必须是「单步删除」分支（不是多选确认）。"""
        self._select(1)
        self.tab._reload_steps()                 # 触发一次刷新（恢复选中）
        self._settle()
        self.assertEqual(self._selected_rows(), [1])
        with mock.patch.object(self.tab, "_confirm_del_step", return_value=True) as one, \
                mock.patch.object(self.tab, "_confirm_del_steps",
                                  return_value=True) as many:
            self.tab._del_step()
        self.assertEqual(one.call_count, 1, "没走单步删除")
        self.assertEqual(many.call_count, 0, "被误判成多选了")


class TestSourceContract(unittest.TestCase):
    """源码级兜底：把「重建只在 _refresh_step_list 里发生」钉死。"""

    @classmethod
    def setUpClass(cls):
        cls._src = Path(flow_tab_mod.__file__).read_text(encoding="utf-8")

    @staticmethod
    def _body(src: str, name: str) -> str:
        """截出某个方法的函数体文本（到下一个同级 `def` 为止）。"""
        return src.split(f"def {name}")[1].split("\n    def ")[0]

    def test_update_run_button_does_not_clear_the_list(self):
        """`_update_run_button` 每推进一步都会被调用，里面绝不能再直接 clear。"""
        body = self._body(self._src, "_update_run_button")
        self.assertNotIn("step_list.clear()", body,
                         "步骤列表的 clear 又被放回 _update_run_button 了——"
                         "运行时会吞掉用户的选中/拖动")

    def test_refresh_step_list_short_circuits_on_same_signature(self):
        """重建前必须比对内容签名，相同就提前返回。"""
        body = self._body(self._src, "_refresh_step_list")
        self.assertIn("if sig == self._step_list_sig", body)
        self.assertIn("return", body.split("if sig == self._step_list_sig")[1][:60])

    def test_signature_covers_everything_that_changes_a_row(self):
        """签名要覆盖：流程 id / 运行行 / 类型 / 名称 / 摘要 / 失败继续 / 注释 / 缺参。"""
        body = self._body(self._src, "_refresh_step_list")
        # 取「构造签名」到「比对签名」之间的整段（内层也有右括号，不能按 ")" 截）
        sig = body.split("sig = (")[1].split("if sig ==")[0]
        for token in ("flow.id", "running_idx", "single_row", "s.type", "s.name",
                      "s.summary()", "continue_on_fail", "commented",
                      "step_missing_required"):
            self.assertIn(token, sig, f"签名里漏了 {token}——改了它列表不会刷新")


if __name__ == "__main__":
    unittest.main()
