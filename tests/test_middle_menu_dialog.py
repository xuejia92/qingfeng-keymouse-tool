"""中键菜单项编辑对话框（MiddleMenuDialog）的测试。

重点：
- 新建菜单项时「在该项上方显示一条分隔线」默认勾选（用户偏好）；
- 编辑已有项时按原值回填，不会被默认值覆盖；
- 图标以**宫格**呈现，含「无图标」且能正确回填/写回 key；
- 关联流程缺失时插入占位项并拦住确定，避免静默改绑到别的流程。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.config import Flow, FlowStep, MiddleMenuItem
from app.ui import middle_menu_dialog as dlg_mod
from app.ui.middle_menu_dialog import MiddleMenuDialog
from app.ui.middle_menu_icons import PRESET_ICONS


def _flow(name: str, fid: str) -> Flow:
    flow = Flow(name=name, steps=[FlowStep(type="log", name="打印")])
    flow.id = fid
    return flow


class TestMiddleMenuDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.flows = [_flow("流程甲", "f1"), _flow("流程乙", "f2")]

    # ---------- 分隔线默认值 ----------
    def test_new_item_separator_checked_by_default(self):
        dlg = MiddleMenuDialog(None, self.flows)
        self.assertTrue(dlg.sep_check.isChecked())
        dlg.result_item()
        self.assertTrue(dlg._item.separator_before)

    def test_edit_item_keeps_existing_separator_false(self):
        item = MiddleMenuItem(flow_id="f1", flow_name="流程甲", separator_before=False)
        dlg = MiddleMenuDialog(item, self.flows)
        self.assertFalse(dlg.sep_check.isChecked())
        dlg.result_item()
        self.assertFalse(item.separator_before)

    def test_edit_item_keeps_existing_separator_true(self):
        item = MiddleMenuItem(flow_id="f1", separator_before=True)
        dlg = MiddleMenuDialog(item, self.flows)
        self.assertTrue(dlg.sep_check.isChecked())

    # ---------- 图标（宫格） ----------
    def test_icon_grid_shows_all_presets_with_no_icon_first(self):
        dlg = MiddleMenuDialog(None, self.flows)
        self.assertEqual(len(dlg.icon_picker.buttons), len(PRESET_ICONS))
        self.assertEqual(dlg.icon_picker.icon_keys()[0], "")
        self.assertEqual(dlg.icon_picker.buttons[0].text(), "无图标")
        self.assertIn("rocket", dlg.icon_picker.icon_keys())

    def test_new_item_defaults_to_no_icon(self):
        dlg = MiddleMenuDialog(None, self.flows)
        self.assertEqual(dlg.icon_picker.selected_key(), "")

    def test_existing_icon_is_selected_in_grid(self):
        item = MiddleMenuItem(flow_id="f1", icon="star")
        dlg = MiddleMenuDialog(item, self.flows)
        self.assertEqual(dlg.icon_picker.selected_key(), "star")

    def test_unknown_icon_key_falls_back_to_no_icon(self):
        """配置里若是个已经不存在的 key，宫格退回「无图标」而不是乱选一个。"""
        item = MiddleMenuItem(flow_id="f1", icon="removed-icon")
        dlg = MiddleMenuDialog(item, self.flows)
        self.assertEqual(dlg.icon_picker.selected_key(), "")

    def test_result_item_writes_selected_icon(self):
        dlg = MiddleMenuDialog(None, self.flows)
        dlg.icon_picker.set_selected_key("rocket")
        item = dlg.result_item()
        self.assertEqual(item.icon, "rocket")

    def test_clearing_icon_writes_empty(self):
        item = MiddleMenuItem(flow_id="f1", icon="rocket")
        dlg = MiddleMenuDialog(item, self.flows)
        dlg.icon_picker.set_selected_key("")
        dlg.result_item()
        self.assertEqual(item.icon, "")

    def test_hint_follows_selection(self):
        """格子小、名字也短，旁边再给一行「当前选择」文字，避免选完心里没底。"""
        dlg = MiddleMenuDialog(None, self.flows)
        self.assertIn("无图标", dlg.icon_hint.text())
        dlg.icon_picker.set_selected_key("rocket")
        self.assertIn("火箭", dlg.icon_hint.text())

    def test_grid_is_height_capped(self):
        """预设会持续增加，宫格必须限高 + 可滚动，不能把对话框越撑越高。"""
        dlg = MiddleMenuDialog(None, self.flows)
        self.assertLessEqual(dlg.icon_picker.area.height(), 400)
        self.assertIsNotNone(dlg.icon_picker.area.verticalScrollBar())

    # ---------- 关联流程 ----------
    def test_result_item_writes_flow_and_name(self):
        dlg = MiddleMenuDialog(None, self.flows)
        dlg.flow_combo.setCurrentIndex(dlg.flow_combo.findData("f2"))
        item = dlg.result_item()
        self.assertEqual(item.flow_id, "f2")
        self.assertEqual(item.flow_name, "流程乙")

    def test_missing_flow_inserts_placeholder_and_blocks_accept(self):
        item = MiddleMenuItem(flow_id="gone", flow_name="已删流程")
        dlg = MiddleMenuDialog(item, self.flows)
        self.assertEqual(dlg.flow_combo.currentData(), "")      # 占位项，data 为空
        with mock.patch.object(dlg_mod.QMessageBox, "information") as info:
            dlg.accept()
        info.assert_called_once()
        # 没有被静默改绑到第一个流程
        self.assertEqual(item.flow_id, "gone")

    def test_no_flows_blocks_accept(self):
        dlg = MiddleMenuDialog(None, [])
        with mock.patch.object(dlg_mod.QMessageBox, "information") as info:
            dlg.accept()
        info.assert_called_once()

    def test_valid_selection_passes_accept(self):
        dlg = MiddleMenuDialog(None, self.flows)
        with mock.patch.object(dlg_mod.QMessageBox, "information") as info:
            dlg.accept()
        info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
