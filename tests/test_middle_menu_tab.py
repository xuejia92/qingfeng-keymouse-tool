"""中键菜单管理页（MiddleMenuTab）的测试。

覆盖：列表渲染（自定义名称 / 流程名回退 / 断链红字）、增删改、上下移排序、
开关持久化，以及流程增删改后的联动刷新。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QMessageBox

from app.config import AppConfig, Flow, FlowStep, MiddleMenuItem
from app.ui import middle_menu_tab as tab_mod
from app.ui.middle_menu_tab import MiddleMenuTab
from tests._env import TempConfigPaths


def _flow(name: str, fid: str) -> Flow:
    flow = Flow(name=name, steps=[FlowStep(type="log", name="打印")])
    flow.id = fid
    return flow


class _FakeDialog:
    """替身对话框：直接 Accepted，并为新建项补一个流程与名称。"""

    def __init__(self, item, flows, parent=None):
        self._item = item or MiddleMenuItem()
        self._flows = list(flows or [])

    def exec(self):
        return QDialog.DialogCode.Accepted

    def result_item(self):
        if not self._item.flow_id and self._flows:
            self._item.flow_id = self._flows[0].id
            self._item.flow_name = self._flows[0].name
        if not self._item.label:
            self._item.label = "新入口"
        return self._item


class TestMiddleMenuTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = TempConfigPaths()
        self.tmp.__enter__()
        self.cfg = AppConfig()
        self.cfg.flows = [_flow("流程甲", "f1"), _flow("流程乙", "f2")]
        self.cfg.middle_menu_items = [
            MiddleMenuItem(label="甲入口", flow_id="f1", flow_name="流程甲"),
            MiddleMenuItem(flow_id="f2", flow_name="流程乙"),
        ]
        self.tab = MiddleMenuTab(self.cfg)
        self.changed = []
        self.tab.changed.connect(lambda: self.changed.append(1))

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    # ---------- 渲染 ----------
    def test_initial_rows(self):
        self.assertEqual(self.tab.list.count(), 2)
        self.assertEqual(self.tab.list.item(0).text(), "甲入口    ·    流程：流程甲")
        # 未填自定义名称：直接显示流程名
        self.assertEqual(self.tab.list.item(1).text(), "流程乙")

    def test_initial_switches_reflect_config(self):
        self.assertTrue(self.tab.enable_check.isChecked())
        self.assertFalse(self.tab.suppress_check.isChecked())

    def test_construction_does_not_emit_changed(self):
        """构造期只是回填初值，不应触发变更回调（否则会误保存）。"""
        self.assertEqual(self.changed, [])

    def test_broken_link_flagged(self):
        self.cfg.middle_menu_items[1].flow_id = "gone"
        self.tab.refresh_list()
        self.assertIn("关联流程已不存在", self.tab.list.item(1).text())

    def test_flow_rename_followed(self):
        """未自定义名称的条目，流程改名后菜单文字随之变化。"""
        self.cfg.flows[1].name = "流程乙改名"
        self.tab.refresh_list()
        self.assertEqual(self.tab.list.item(1).text(), "流程乙改名")

    # ---------- 图标 ----------
    def test_no_icon_column_when_nobody_has_icon(self):
        """一条图标都没设时，列表不能凭空撑出图标列（文字会白白右移一列）。"""
        for i in range(self.tab.list.count()):
            self.assertTrue(self.tab.list.item(i).icon().isNull())

    def test_unknown_icon_key_alone_does_not_open_icon_column(self):
        """配置里塞了不认识的 key 时，等同于没设图标，不该撑出图标列。

        这是回归测试：曾按 it.icon 字符串判断「有没有图标」，未知 key 字符串非空
        却画不出图标，会把整个列表带进图标模式、给所有条目补占位图标。
        """
        for it in self.cfg.middle_menu_items:
            it.icon = "no-such-icon"
        self.tab.refresh_list()
        for i in range(self.tab.list.count()):
            self.assertTrue(self.tab.list.item(i).icon().isNull())

    def test_iconless_row_gets_blank_placeholder_when_icons_mixed(self):
        """有图标也有无图标时，无图标的补透明占位图标，保证文字左缘对齐。"""
        from app.ui.middle_menu_icons import blank_icon
        self.cfg.middle_menu_items[0].icon = "rocket"
        self.tab.refresh_list()
        first, second = self.tab.list.item(0), self.tab.list.item(1)
        self.assertFalse(first.icon().isNull())
        self.assertNotEqual(first.icon().cacheKey(), blank_icon().cacheKey())
        self.assertEqual(second.icon().cacheKey(), blank_icon().cacheKey())

    # ---------- 排序 ----------
    def test_move_down_swaps_and_emits(self):
        self.tab.list.setCurrentRow(0)
        self.tab._move(1)
        self.assertEqual([i.flow_id for i in self.cfg.middle_menu_items], ["f2", "f1"])
        self.assertEqual([i.flow_id for i in self.tab._items], ["f2", "f1"])
        self.assertEqual(self.changed, [1])

    def test_move_up_at_top_is_noop(self):
        self.tab.list.setCurrentRow(0)
        self.tab._move(-1)
        self.assertEqual([i.flow_id for i in self.tab._items], ["f1", "f2"])
        self.assertEqual(self.changed, [])

    def test_edge_buttons_disabled(self):
        self.tab.list.setCurrentRow(0)
        self.assertFalse(self.tab.up_btn.isEnabled())
        self.assertTrue(self.tab.down_btn.isEnabled())
        self.tab.list.setCurrentRow(1)
        self.assertTrue(self.tab.up_btn.isEnabled())
        self.assertFalse(self.tab.down_btn.isEnabled())

    def test_buttons_disabled_without_selection(self):
        self.tab.list.setCurrentRow(-1)
        self.assertFalse(self.tab.edit_btn.isEnabled())
        self.assertFalse(self.tab.del_btn.isEnabled())

    # ---------- 增删 ----------
    def test_add_appends_item(self):
        with mock.patch.object(tab_mod, "MiddleMenuDialog", _FakeDialog):
            self.tab._add_item()
        self.assertEqual(self.tab.list.count(), 3)
        self.assertEqual(self.cfg.middle_menu_items[-1].flow_id, "f1")
        self.assertEqual(self.changed, [1])

    def test_add_without_flows_warns(self):
        self.cfg.flows = []
        with mock.patch.object(tab_mod.QMessageBox, "information") as info:
            self.tab._add_item()
        info.assert_called_once()
        self.assertEqual(len(self.cfg.middle_menu_items), 2)
        self.assertEqual(self.changed, [])

    def test_delete_confirmed_removes(self):
        self.tab.list.setCurrentRow(0)
        with mock.patch.object(tab_mod.QMessageBox, "question",
                               return_value=QMessageBox.Yes):
            self.tab._del_item()
        self.assertEqual([i.flow_id for i in self.tab._items], ["f2"])
        self.assertEqual(self.changed, [1])

    def test_delete_cancelled_keeps_item(self):
        self.tab.list.setCurrentRow(0)
        with mock.patch.object(tab_mod.QMessageBox, "question",
                               return_value=QMessageBox.No):
            self.tab._del_item()
        self.assertEqual(len(self.tab._items), 2)
        self.assertEqual(self.changed, [])

    def test_edit_writes_back(self):
        self.tab.list.setCurrentRow(0)
        with mock.patch.object(tab_mod, "MiddleMenuDialog", _FakeDialog):
            self.tab._edit_item()
        self.assertEqual(len(self.tab._items), 2)
        self.assertEqual(self.changed, [1])

    # ---------- 开关 ----------
    def test_enable_toggle_persists(self):
        self.tab.enable_check.setChecked(False)
        self.assertFalse(self.cfg.middle_menu_enabled)
        self.assertEqual(self.changed, [1])

    def test_suppress_toggle_persists(self):
        self.tab.suppress_check.setChecked(True)
        self.assertTrue(self.cfg.middle_menu_suppress)
        self.assertEqual(self.changed, [1])

    # ---------- 联动 ----------
    def test_on_flows_changed_refreshes_broken_state(self):
        self.cfg.flows = [self.cfg.flows[0]]          # 流程乙被删除
        self.tab.on_flows_changed()
        self.assertIn("关联流程已不存在", self.tab.list.item(1).text())


if __name__ == "__main__":
    unittest.main()
