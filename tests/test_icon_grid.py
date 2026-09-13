"""宫格图标选择器（app/ui/icon_grid.py）的测试。

覆盖：格子与预设一一对应且顺序一致、「无图标」在首格并用透明占位图标、
互斥选中、取值 / 回填（含未知 key 退回「无图标」）、信号通知，以及限高可滚动。
"""
from __future__ import annotations

import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.icon_grid import (ICON_GRID_COLUMNS, MAX_GRID_HEIGHT, TILE_GAP,
                              TILE_H, IconPicker)
from app.ui.middle_menu_icons import PRESET_ICONS, blank_icon


class TestIconPicker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.picker = IconPicker()

    # ---------- 结构 ----------
    def test_one_tile_per_preset_in_order(self):
        self.assertEqual(self.picker.icon_keys(), [k for k, _, _ in PRESET_ICONS])

    def test_tile_text_is_the_chinese_name(self):
        for btn, (_key, _emoji, name) in zip(self.picker.buttons, PRESET_ICONS):
            self.assertEqual(btn.text(), name)

    def test_first_tile_is_no_icon_with_blank_icon(self):
        """「无图标」必须在第一格，且用全透明占位图标（不是缺省图标）。"""
        first = self.picker.buttons[0]
        self.assertEqual(first.text(), "无图标")
        self.assertTrue(first.isCheckable())
        self.assertEqual(first.icon().cacheKey(), blank_icon().cacheKey())

    def test_every_tile_has_an_icon(self):
        for btn in self.picker.buttons:
            self.assertFalse(btn.icon().isNull(), f"{btn.text()} 没有图标")

    def test_tiles_are_fixed_size_so_grid_stays_even(self):
        for btn in self.picker.buttons:
            self.assertEqual((btn.width(), btn.height()), (self.picker.buttons[0].width(),
                                                           self.picker.buttons[0].height()))

    def test_columns_respected(self):
        picker = IconPicker(columns=4)
        grid = picker.buttons[0].parentWidget().layout()
        pos = lambda b: grid.getItemPosition(grid.indexOf(b))[:2]   # noqa: E731
        self.assertEqual(pos(picker.buttons[0]), (0, 0))
        self.assertEqual(pos(picker.buttons[3]), (0, 3))
        self.assertEqual(pos(picker.buttons[4]), (1, 0))    # 第 5 个换到第二行

    # ---------- 选中 ----------
    def test_default_has_no_selection(self):
        self.assertEqual(self.picker.selected_key(), "")
        self.assertIsNone(self.picker._group.checkedButton())

    def test_set_selected_key_checks_that_tile(self):
        self.picker.set_selected_key("rocket")
        self.assertEqual(self.picker.selected_key(), "rocket")
        self.assertTrue(self.picker._btn_of_key["rocket"].isChecked())

    def test_selected_name_matches_tile_text(self):
        self.picker.set_selected_key("rocket")
        self.assertEqual(self.picker.selected_name(), "火箭")

    def test_unknown_key_falls_back_to_no_icon(self):
        self.picker.set_selected_key("no-such-key")
        self.assertEqual(self.picker.selected_key(), "")

    def test_empty_key_selects_no_icon_tile(self):
        self.picker.set_selected_key("rocket")
        self.picker.set_selected_key("")
        self.assertEqual(self.picker.selected_key(), "")
        self.assertTrue(self.picker._btn_of_key[""].isChecked())

    def test_whitespace_key_treated_as_empty(self):
        self.picker.set_selected_key("   ")
        self.assertEqual(self.picker.selected_key(), "")

    def test_selection_is_exclusive(self):
        self.picker.set_selected_key("rocket")
        self.picker.set_selected_key("star")
        checked = [b for b in self.picker.buttons if b.isChecked()]
        self.assertEqual(len(checked), 1)
        self.assertEqual(self.picker.selected_key(), "star")

    # ---------- 信号 ----------
    def test_selecting_emits_signal(self):
        seen: list[str] = []
        self.picker.selectionChanged.connect(seen.append)
        self.picker.set_selected_key("rocket")
        self.assertEqual(seen, ["rocket"])

    def test_reselecting_same_key_does_not_emit_again(self):
        self.picker.set_selected_key("rocket")
        seen: list[str] = []
        self.picker.selectionChanged.connect(seen.append)
        self.picker.set_selected_key("rocket")
        self.assertEqual(seen, [])

    # ---------- 尺寸 ----------
    def test_height_is_capped_so_dialog_stays_compact(self):
        rows = math.ceil(len(PRESET_ICONS) / ICON_GRID_COLUMNS)
        uncapped = rows * TILE_H + (rows - 1) * TILE_GAP
        self.assertLessEqual(self.picker.area.height(), MAX_GRID_HEIGHT)
        self.assertLess(self.picker.area.height(), uncapped,
                        "预设很多时必须限高，否则对话框会被撑高")

    def test_short_grid_does_not_scroll(self):
        """格子少时按需收缩高度，不留一大片空白。"""
        few = IconPicker(columns=100)          # 全部塞进一行
        self.assertLessEqual(few.area.height(), TILE_H)


if __name__ == "__main__":
    unittest.main()
