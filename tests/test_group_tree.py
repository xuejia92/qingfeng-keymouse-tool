# -*- coding: utf-8 -*-
"""「按分组拖放」通用件（app/ui/group_tree.py）的测试。

自动化流程页与定时任务页共用它，所以这里的用例覆盖**共享语义**：
顺序推算（组内插到落点前 / 排到组末尾 / 白拖一下不重建）与拖放翻译
（分组头 / 条目行 / 非法落点）。页面各自的接线由 test_flow_tab / test_scheduler 覆盖。
"""
from __future__ import annotations

import os
import unittest
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui.group_tree import GroupDropTree, reorder_for_group, same_object_order


@dataclass
class _Item:
    """替身条目：只用到 id / group 两个字段（与 Flow/ScheduleTask 的用法一致）。"""

    id: str
    group: str = ""


def _items(*pairs) -> list[_Item]:
    return [_Item(i, g) for i, g in pairs]


class TestReorderForGroup(unittest.TestCase):
    def test_appends_to_target_group_end(self):
        items = _items(("a", "办公"), ("b", "办公"), ("c", ""))
        new = reorder_for_group(items, items[2], "办公")
        self.assertEqual([x.id for x in new], ["a", "b", "c"])
        self.assertIs(new[2], items[2])

    def test_inserts_before_the_drop_target(self):
        items = _items(("a", "办公"), ("b", "办公"), ("c", ""))
        new = reorder_for_group(items, items[2], "办公", before=items[0])
        self.assertEqual([x.id for x in new], ["c", "a", "b"])

    def test_unknown_before_falls_back_to_group_end(self):
        items = _items(("a", "办公"), ("b", ""))
        new = reorder_for_group(items, items[1], "办公", before=_Item("不存在"))
        self.assertEqual([x.id for x in new], ["a", "b"])

    def test_reorders_inside_the_same_group(self):
        items = _items(("a", "办公"), ("b", "办公"), ("c", "办公"))
        new = reorder_for_group(items, items[0], "办公", before=items[2])
        self.assertEqual([x.id for x in new], ["b", "a", "c"])

    def test_returns_none_when_nothing_would_change(self):
        """已经在目标分组、落点也算出原位 → 返回 None（调用方不该重建列表）。"""
        items = _items(("a", "办公"), ("b", "办公"))
        self.assertIsNone(reorder_for_group(items, items[1], "办公"))
        self.assertIsNone(reorder_for_group(items, items[0], "办公", before=items[0]))

    def test_applies_even_when_order_is_unchanged_but_group_differs(self):
        """★ 顺序没变但**归属变了**时必须照做。

        例：`[A(办公), B(游戏)]` 把 B 拖进「办公」→ 算出来还是 [A, B]，
        但 B 的 group 要从「游戏」变成「办公」；只看顺序就跳过会漏掉这次改动。
        """
        items = _items(("a", "办公"), ("b", "游戏"))
        new = reorder_for_group(items, items[1], "办公")
        self.assertIsNotNone(new, "归属变了就得返回新顺序")
        self.assertEqual([x.id for x in new], ["a", "b"])


class TestSameObjectOrder(unittest.TestCase):
    def test_identity_not_equality(self):
        """⚠️ 必须比对象身份：dataclass 的 `==` 按字段值比，内容相同会被判"没变"。"""
        a1 = _Item("same", "办公")
        a2 = _Item("same", "办公")
        self.assertEqual(a1, a2, "前提：两者按值相等")
        self.assertFalse(same_object_order([a1], [a2]))
        self.assertTrue(same_object_order([a1], [a1]))


class TestGroupDropTree(unittest.TestCase):
    """拖放翻译（需要 Qt；真实几何在离屏也能算，但要先 resize+show）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTreeWidgetItem
        self.Qt = Qt
        self.tree = GroupDropTree("task")
        self.tree.resize(320, 400)
        self.tree.show()
        self._app.processEvents()
        self.ids = {}
        for g in ("办公", "游戏", ""):
            gitem = QTreeWidgetItem()
            gitem.setData(0, Qt.UserRole, ("group", g))
            gitem.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(gitem)
            self.ids[g] = gitem
            for name in (("甲", "乙") if g == "办公" else (("丙",) if g == "游戏" else ("丁",))):
                citem = QTreeWidgetItem([name])
                citem.setData(0, Qt.UserRole, ("task", name))
                gitem.addChild(citem)
            gitem.setExpanded(True)
        self._app.processEvents()

    def tearDown(self):
        self.tree.hide()

    def _child(self, group: str, index: int = 0):
        return self.ids[group].child(index)

    def test_drop_on_group_header(self):
        self.tree._dragging_id = "丁"
        target = self.tree.drop_target(
            self.tree.visualItemRect(self.ids["办公"]).center())
        self.assertEqual(target, ("丁", "办公", ""))

    def test_drop_on_entry_row_inserts_before_it(self):
        self.tree._dragging_id = "丁"
        target = self.tree.drop_target(
            self.tree.visualItemRect(self._child("办公", 1)).center())
        self.assertEqual(target, ("丁", "办公", "乙"))

    def test_no_target_when_not_dragging(self):
        self.tree._dragging_id = None
        self.assertIsNone(self.tree.drop_target(
            self.tree.visualItemRect(self.ids["办公"]).center()))

    def test_group_headers_are_not_draggable(self):
        from unittest import mock
        from PySide6.QtWidgets import QTreeWidget
        self.tree.setCurrentItem(self.ids["办公"])
        with mock.patch.object(QTreeWidget, "startDrag") as super_drag:
            self.tree.startDrag(self.Qt.MoveAction)
        self.assertFalse(super_drag.called, "分组头不该发起拖动")

    def test_entry_drag_is_delegated_to_qt(self):
        """条目拖动要真的交给 Qt（否则没有拖影、也收不到放下事件）。"""
        from unittest import mock
        from PySide6.QtWidgets import QTreeWidget
        self.tree.setCurrentItem(self._child("办公", 0))
        with mock.patch.object(QTreeWidget, "startDrag") as super_drag:
            self.tree.startDrag(self.Qt.MoveAction)
        self.assertTrue(super_drag.called)

    def test_drop_with_no_target_is_ignored(self):
        """落在空白处：事件被忽略，不发信号。"""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QDropEvent
        from PySide6.QtCore import QMimeData
        seen = []
        self.tree.entry_dropped.connect(lambda *a: seen.append(a))
        data = QMimeData()
        event = QDropEvent(QPointF(5, 9000), self.Qt.MoveAction, data,
                           self.Qt.LeftButton, self.Qt.NoModifier)
        try:
            self.tree.dropEvent(event)
            self.assertEqual(seen, [])
        finally:
            del event
            _keep = data


if __name__ == "__main__":
    unittest.main()
