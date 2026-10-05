# -*- coding: utf-8 -*-
"""「任务看板」小程序（app/mini_apps/task_board.py）的测试。

覆盖：
- **数据层**（TaskBoardStore，纯 Python）：默认看板、增删改任务、跨列/列内移动、
  锚点语义、删列保护、列左右移动、清理已完成、原子落盘与往返读回、坏数据兜底；
- **界面层**（TaskBoardWidget / TaskListWidget）：按 store 建列、筛选、空列占位、
  落点翻译（锚点 + 前后）、真实 drop 事件走通、拖动期间延后重建；
- **接线**：注册到小工具页、九宫格出现卡片、独立窗口承载控件、关闭时落盘。

数据文件走 `tests/_env.TempConfigPaths` 换成临时目录，不碰真实 task_board.json。
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import (QDragEnterEvent, QDragMoveEvent, QDropEvent,
                           QFontMetrics)
from PySide6.QtWidgets import QApplication

from app import mini_apps
from app.mini_apps import task_board as tb
from app.ui.tools_tab import ToolsTab
from tests._env import TempConfigPaths


class _QtCase(unittest.TestCase):
    """需要 QApplication 的用例基类（离屏）。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _board(self, **tasks) -> tb.TaskBoardWidget:
        """造一个看板：`_board(待办=["甲", "乙"], 进行中=["丙"])`。"""
        store = tb.TaskBoardStore()
        for name, titles in tasks.items():
            column = next(c for c in store.columns if c.name == name)
            for title in titles:
                store.add_task(column.id, title)
        store.save()
        return tb.TaskBoardWidget(store)


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
class TestStoreBasics(_QtCase):

    def test_first_run_creates_default_board_on_disk(self):
        """首次运行：默认三列，且立刻落一份文件（用户能看到数据存在哪）。"""
        store = tb.TaskBoardStore()
        self.assertEqual([c.name for c in store.columns], ["待办", "进行中", "已完成"])
        self.assertTrue(store.columns[-1].done, "「已完成」应当是完成列")
        self.assertTrue(os.path.exists(store.path))
        with open(store.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], tb.BOARD_VERSION)
        self.assertEqual(len(data["columns"]), 3)

    def test_add_update_delete_task(self):
        store = tb.TaskBoardStore()
        cid = store.columns[0].id
        task = store.add_task(cid, "写测试", note="覆盖数据层", priority="high",
                              due="2026-12-31")
        self.assertIsNotNone(task)
        self.assertEqual(store.task_count(), 1)

        self.assertTrue(store.update_task(task.id, title="写更多测试",
                                          priority="low"))
        self.assertEqual(store.find_task(task.id)[2].title, "写更多测试")
        self.assertEqual(store.find_task(task.id)[2].priority, "low")
        self.assertFalse(store.update_task(task.id, title="写更多测试"),
                         "值没变就不该算改动")

        self.assertTrue(store.delete_task(task.id))
        self.assertEqual(store.task_count(), 0)
        self.assertIsNone(store.find_task(task.id))

    def test_add_task_needs_title_and_column(self):
        store = tb.TaskBoardStore()
        self.assertIsNone(store.add_task(store.columns[0].id, "   "))
        self.assertIsNone(store.add_task("不存在的列", "甲"))

    def test_priority_falls_back_to_normal(self):
        store = tb.TaskBoardStore()
        task = store.add_task(store.columns[0].id, "甲", priority="乱写的")
        self.assertEqual(task.priority, tb.DEFAULT_PRIORITY)

    def test_due_is_normalized_and_validated(self):
        store = tb.TaskBoardStore()
        cid = store.columns[0].id
        self.assertEqual(store.add_task(cid, "甲", due="2026-3-5").due, "2026-03-05")
        self.assertEqual(store.add_task(cid, "乙", due="不是日期").due, "")
        self.assertEqual(store.add_task(cid, "丙", due="").due, "")

    def test_overdue_flag(self):
        store = tb.TaskBoardStore()
        cid = store.columns[0].id
        self.assertTrue(store.add_task(cid, "旧", due="2000-01-01").is_overdue())
        self.assertFalse(store.add_task(cid, "新", due="2999-01-01").is_overdue())
        self.assertFalse(store.add_task(cid, "无").is_overdue())


class TestStoreMove(_QtCase):

    def _three(self):
        store = tb.TaskBoardStore()
        todo, doing, done = store.columns
        a = store.add_task(todo.id, "甲")
        b = store.add_task(todo.id, "乙")
        c = store.add_task(todo.id, "丙")
        return store, todo, doing, done, a, b, c

    def test_move_to_other_column_appends(self):
        store, todo, doing, _done, a, _b, _c = self._three()
        self.assertTrue(store.move_task(a.id, doing.id))
        self.assertEqual([t.title for t in doing.tasks], ["甲"])
        self.assertEqual([t.title for t in todo.tasks], ["乙", "丙"])

    def test_move_with_anchor_before_and_after(self):
        store, todo, doing, _done, a, b, _c = self._three()
        store.move_task(b.id, doing.id)
        store.move_task(a.id, doing.id, anchor_tid=b.id, after=False)
        self.assertEqual([t.title for t in doing.tasks], ["甲", "乙"])
        store.move_task(a.id, doing.id, anchor_tid=b.id, after=True)
        self.assertEqual([t.title for t in doing.tasks], ["乙", "甲"])

    def test_reorder_within_same_column(self):
        store, todo, _doing, _done, a, _b, c = self._three()
        # 把「甲」拖到「丙」后面
        self.assertTrue(store.move_task(a.id, todo.id, anchor_tid=c.id, after=True))
        self.assertEqual([t.title for t in todo.tasks], ["乙", "丙", "甲"])

    def test_move_back_to_same_place_is_noop(self):
        store, todo, _doing, _done, a, b, _c = self._three()
        # 拖到「乙」前面 = 原位，不该算改动（否则会白落一次盘）
        self.assertFalse(store.move_task(a.id, todo.id, anchor_tid=b.id, after=False))

    def test_move_onto_itself_is_ignored(self):
        store, _todo, _doing, _done, a, _b, _c = self._three()
        self.assertFalse(store.move_task(a.id, store.columns[0].id, anchor_tid=a.id))

    def test_move_unknown_ids_returns_false(self):
        store, todo, _doing, _done, a, _b, _c = self._three()
        self.assertFalse(store.move_task("无此任务", todo.id))
        self.assertFalse(store.move_task(a.id, "无此列"))

    def test_move_by_delta(self):
        store, todo, _doing, _done, a, _b, _c = self._three()
        self.assertTrue(store.move_task_by(a.id, 1))
        self.assertEqual([t.title for t in todo.tasks], ["乙", "甲", "丙"])
        self.assertTrue(store.move_task_by(a.id, -5), "越界会被夹到最前")
        self.assertEqual([t.title for t in todo.tasks], ["甲", "乙", "丙"])
        self.assertFalse(store.move_task_by(a.id, -1), "已在最前，再上移不该算改动")


class TestStoreColumns(_QtCase):

    def test_add_rename_move_delete_column(self):
        store = tb.TaskBoardStore()
        col = store.add_column("稍后")
        self.assertEqual([c.name for c in store.columns], ["待办", "进行中", "已完成", "稍后"])
        self.assertTrue(store.rename_column(col.id, "以后再说"))
        self.assertEqual(store.find_column(col.id).name, "以后再说")
        self.assertTrue(store.move_column(col.id, -1))
        self.assertEqual([c.name for c in store.columns][-2], "以后再说")
        self.assertTrue(store.delete_column(col.id))
        self.assertEqual(len(store.columns), 3)

    def test_last_column_cannot_be_deleted(self):
        store = tb.TaskBoardStore()
        while len(store.columns) > 1:
            store.delete_column(store.columns[-1].id)
        self.assertEqual(len(store.columns), 1)
        self.assertFalse(store.delete_column(store.columns[0].id))
        self.assertEqual(len(store.columns), 1)

    def test_move_column_out_of_range_is_noop(self):
        store = tb.TaskBoardStore()
        first = store.columns[0].id
        self.assertFalse(store.move_column(first, -1))
        self.assertFalse(store.move_column(store.columns[-1].id, 1))

    def test_clear_column_and_clear_done(self):
        store = tb.TaskBoardStore()
        todo, _doing, done = store.columns
        store.add_task(todo.id, "甲")
        store.add_task(todo.id, "乙")
        store.add_task(done.id, "完成一")
        store.add_task(done.id, "完成二")
        self.assertEqual(store.done_count(), 2)
        self.assertEqual(store.clear_column(todo.id), 2)
        self.assertEqual(store.task_count(), 2)
        self.assertEqual(store.clear_done(), 2)
        self.assertEqual(store.task_count(), 0)

    def test_set_column_done(self):
        store = tb.TaskBoardStore()
        todo = store.columns[0]
        self.assertTrue(store.set_column_done(todo.id, True))
        self.assertEqual(store.done_count(), 0)
        store.add_task(todo.id, "甲")
        self.assertEqual(store.done_count(), 1)
        self.assertFalse(store.set_column_done(todo.id, True), "没变就不算改动")


class TestStorePersistence(_QtCase):

    def test_round_trip_keeps_everything(self):
        store = tb.TaskBoardStore()
        todo = store.columns[0]
        task = store.add_task(todo.id, "持久化", note="多行\n备注",
                              priority="high", due="2026-11-11")
        store.add_column("自定义列")
        store.move_column(store.columns[-1].id, -3)
        self.assertTrue(store.save())

        again = tb.TaskBoardStore()
        self.assertEqual([c.name for c in again.columns],
                         [c.name for c in store.columns])
        back = again.find_task(task.id)[2]
        self.assertEqual((back.title, back.note, back.priority, back.due),
                         ("持久化", "多行\n备注", "high", "2026-11-11"))
        self.assertEqual(back.created, task.created)

    def test_corrupt_file_falls_back_to_default(self):
        path = tb.board_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        with mock.patch.object(tb.log, "warning"):      # 兜底路径本来就要记一条警告
            store = tb.TaskBoardStore()
        self.assertEqual(len(store.columns), 3)

    def test_unexpected_shape_falls_back_to_default(self):
        path = tb.board_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"columns": "不是列表"}, f)
        store = tb.TaskBoardStore()
        self.assertEqual([c.name for c in store.columns], ["待办", "进行中", "已完成"])

    def test_bad_rows_are_skipped(self):
        path = tb.board_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"columns": [
                {"id": "a", "name": "列A", "tasks": [
                    {"title": ""},                       # 没标题 -> 丢
                    "不是字典",                            # 形状不对 -> 丢
                    {"title": "好任务", "priority": "乱写"},  # 优先级回落
                ]},
                "不是字典",
            ]}, f)
        store = tb.TaskBoardStore()
        self.assertEqual([c.name for c in store.columns], ["列A"])
        self.assertEqual(len(store.columns[0].tasks), 1)
        self.assertEqual(store.columns[0].tasks[0].priority, tb.DEFAULT_PRIORITY)

    def test_save_is_atomic_no_tmp_left(self):
        store = tb.TaskBoardStore()
        store.add_task(store.columns[0].id, "甲")
        store.save()
        self.assertFalse(os.path.exists(store.path + ".tmp"))

    def test_board_path_follows_base_dir(self):
        self.assertTrue(tb.board_path().startswith(self._tmp.tmp))


# ---------------------------------------------------------------------------
# 界面层
# ---------------------------------------------------------------------------
class TestBoardWidget(_QtCase):

    def test_columns_and_cards_built_from_store(self):
        board = self._board(待办=["甲", "乙"], 进行中=["丙"])
        self.assertEqual(list(board._columns), [c.id for c in board.store.columns])
        self.assertEqual(board._lists[board.store.columns[0].id].count(), 2)
        self.assertEqual(board._columns[board.store.columns[0].id].count_label.text(), "2")

    def test_empty_column_gets_placeholder(self):
        board = self._board()
        lw = board._lists[board.store.columns[0].id]
        self.assertEqual(lw.count(), 1)
        self.assertIsNone(lw.item(0).data(Qt.UserRole))
        self.assertEqual(lw.item(0).flags(), Qt.NoItemFlags)

    def test_filter_hides_non_matching(self):
        board = self._board(待办=["写周报", "买牛奶"], 进行中=["写代码"])
        board._on_filter_changed("写")
        todo = board._lists[board.store.columns[0].id]
        self.assertFalse(todo.item(0).isHidden())
        self.assertTrue(todo.item(1).isHidden())
        self.assertEqual(board.filter_text(), "写")
        self.assertIn("筛选出 2", board.status_label.text())

        board._on_filter_changed("")
        self.assertFalse(todo.item(1).isHidden())

    def test_filter_matches_note_and_priority(self):
        store = tb.TaskBoardStore()
        cid = store.columns[0].id
        store.add_task(cid, "甲", note="关键字在里面")
        store.add_task(cid, "乙", priority="high")
        board = tb.TaskBoardWidget(store)
        self.assertEqual(store.match_count("关键字"), 1)
        board._on_filter_changed("高")          # 命中优先级显示名「高」
        lw = board._lists[cid]
        self.assertTrue(lw.item(0).isHidden())
        self.assertFalse(lw.item(1).isHidden())

    def test_filter_no_match_shows_placeholder(self):
        board = self._board(待办=["甲"])
        board._on_filter_changed("找不到")
        lw = board._lists[board.store.columns[0].id]
        self.assertEqual(lw.count(), 2)
        self.assertIsNone(lw.item(1).data(Qt.UserRole))
        self.assertIn("没有匹配", lw.item(1).text())

    def test_status_shows_totals(self):
        board = self._board(待办=["甲", "乙"], 已完成=["丙"])
        self.assertIn("共 3 个任务", board.status_label.text())
        self.assertIn("已完成 1", board.status_label.text())
        self.assertTrue(board.clean_btn.isEnabled())
        self.assertIn(board.store.path, board.status_label.toolTip())

    def test_clean_button_disabled_without_done_tasks(self):
        board = self._board(待办=["甲"])
        self.assertFalse(board.clean_btn.isEnabled())

    def test_size_hint_is_roomy(self):
        board = self._board()
        self.assertGreaterEqual(board.sizeHint().width(), 3 * tb.MIN_COLUMN_W)

    # ---------- 三列铺满 + 动态伸缩 ----------

    def _shown_board(self, width: int = 1000, height: int = 520):
        board = self._board(待办=["甲"])
        board.resize(width, height)
        board.show()
        QApplication.processEvents()
        board.layout().activate()
        QApplication.processEvents()
        return board

    def test_columns_fill_the_whole_width(self):
        """★ 需求：三列默认铺满可用宽度（没有固定宽度、也没有尾部空白）。"""
        board = self._shown_board()
        columns = [board._columns[c.id] for c in board.store.columns]
        widths = [c.width() for c in columns]
        strip = board.strip.width()
        gap = board.strip_lay.spacing() * (len(columns) - 1)
        self.assertEqual(len(columns), 3)
        self.assertAlmostEqual(sum(widths) + gap, strip, delta=6,
                               msg="列宽 + 间距应当刚好铺满整条看板")
        self.assertLessEqual(max(widths) - min(widths), 2, "三列应当等宽")
        self.assertGreater(min(widths), tb.MIN_COLUMN_W,
                           "宽窗口下应当比最小宽度更宽（说明真的伸展开了）")

    def test_columns_stretch_and_shrink_with_window(self):
        """★ 需求：动态伸缩 —— 窗口变宽列变宽，变窄列变窄。"""
        board = self._shown_board(1000)
        first = board.store.columns[0].id
        wide = board._columns[first].width()

        board.resize(1400, 520)
        QApplication.processEvents()
        board.layout().activate()
        QApplication.processEvents()
        wider = board._columns[first].width()
        self.assertGreater(wider, wide, "窗口变宽，列应当跟着变宽")

        board.resize(760, 520)
        QApplication.processEvents()
        board.layout().activate()
        QApplication.processEvents()
        narrower = board._columns[first].width()
        self.assertLess(narrower, wider, "窗口变窄，列应当跟着变窄")
        self.assertGreaterEqual(narrower, tb.MIN_COLUMN_W, "但不小于最小宽度")

    def test_minimum_window_still_fits_three_columns(self):
        """最小窗口下三列仍并排放下（每列不小于最小宽度），不需要横向滚动。"""
        board = self._shown_board(700)
        widths = [board._columns[c.id].width() for c in board.store.columns]
        self.assertEqual(len(widths), 3)
        for w in widths:
            self.assertGreaterEqual(w, tb.MIN_COLUMN_W)
        self.assertEqual(board.area.horizontalScrollBar().maximum(), 0)

    def test_strip_refuses_to_compress_below_min_width(self):
        """放不下时靠横向滚动兜底，而不是把列继续压扁。

        看板条的最小宽度 = 三列最小宽 + 列间距；QScrollArea 的 widgetResizable
        不会把内容压到它自己的最小宽度以下，于是窗口再窄就出现横向滚动条。
        """
        board = self._board()
        expected = tb.MIN_COLUMN_W * 3 + board.strip_lay.spacing() * 2
        self.assertGreaterEqual(board.strip.minimumSizeHint().width(), expected)


class TestDrop(_QtCase):

    def _shown(self, **tasks):
        board = self._board(**tasks)
        board.resize(900, 500)
        board.show()
        QApplication.processEvents()
        return board

    def test_drop_anchor_before_after_and_empty(self):
        board = self._shown(待办=["甲", "乙"])
        lw = board._lists[board.store.columns[0].id]
        first, second = lw.item(0), lw.item(1)
        r0, r1 = lw.visualItemRect(first), lw.visualItemRect(second)

        anchor, after = lw.drop_anchor(QPoint(10, r1.top() + 1))
        self.assertEqual(anchor, second.data(Qt.UserRole).id)
        self.assertFalse(after, "落在上半 -> 插到它前面")

        anchor, after = lw.drop_anchor(QPoint(10, r1.bottom() - 1))
        self.assertEqual(anchor, second.data(Qt.UserRole).id)
        self.assertTrue(after, "落在下半 -> 插到它后面")

        anchor, after = lw.drop_anchor(QPoint(10, r0.bottom() + 400))
        self.assertEqual((anchor, after), ("", False), "落在空白 -> 排到末尾")

    def test_drop_task_moves_and_saves(self):
        board = self._board(待办=["甲"], 进行中=[])
        store = board.store
        todo, doing = store.columns[0], store.columns[1]
        task_id = todo.tasks[0].id

        board.drop_task(task_id, doing.id, "", False)
        self.assertEqual([t.title for t in doing.tasks], ["甲"])
        self.assertEqual(todo.tasks, [])
        with open(store.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["columns"][1]["tasks"][0]["title"], "甲")

    def test_real_drop_event_reaches_handler(self):
        """真的往 viewport 投 DragEnter/DragMove/Drop，验证事件通路（不是只调方法）。"""
        board = self._shown(待办=["甲", "乙"])
        lw = board._lists[board.store.columns[0].id]
        got = []
        lw.taskDropped.connect(lambda *a: got.append(a))

        mime = QMimeData()
        mime.setData(tb.TASK_MIME, b"some-task-id")
        y = int(lw.visualItemRect(lw.item(1)).bottom() + 200)
        self._send_drag(lw, mime, y)
        event = self._send_drop(lw, mime, y)

        self.assertTrue(event.isAccepted())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "some-task-id")
        self.assertEqual((got[0][1], got[0][2]), ("", False))

    @staticmethod
    def _send_drag(listw, mime, y: int):
        """投 DragEnter + DragMove（真实拖动一定会先经过这两步，Qt 靠它记住落点目标）。"""
        viewport = listw.viewport()
        QApplication.sendEvent(viewport, QDragEnterEvent(
            QPoint(12, y), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier))
        QApplication.sendEvent(viewport, QDragMoveEvent(
            QPoint(12, y), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier))

    @staticmethod
    def _send_drop(listw, mime, y: int) -> QDropEvent:
        event = QDropEvent(QPointF(12, y), Qt.MoveAction, mime,
                           Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(listw.viewport(), event)
        return event

    def test_drop_on_item_inserts_before_it(self):
        """落在某张卡上半 -> 插到它前面（端到端走一遍事件）。"""
        board = self._shown(待办=["甲", "乙", "丙"])
        todo = board.store.columns[0]
        lw = board._lists[todo.id]
        mime = QMimeData()
        mime.setData(tb.TASK_MIME, todo.tasks[0].id.encode("utf-8"))
        rect = lw.visualItemRect(lw.item(2))
        self._send_drag(lw, mime, rect.top() + 1)
        self._send_drop(lw, mime, rect.top() + 1)
        # 甲 先被移出（下标 0），再插到 丙 前面 -> 落在 乙 与 丙 之间
        self.assertEqual([t.title for t in todo.tasks], ["乙", "甲", "丙"])

    def test_foreign_mime_is_ignored(self):
        board = self._shown(待办=["甲"])
        lw = board._lists[board.store.columns[0].id]
        mime = QMimeData()
        mime.setText("别的程序拖来的文本")
        self._send_drag(lw, mime, 40)
        event = self._send_drop(lw, mime, 40)
        self.assertFalse(event.isAccepted())

    def test_reload_is_deferred_while_dragging(self):
        """★ 拖动期间不重建视图（否则会把正在拖的列表控件删掉）。

        drop 只置 pending 标记；等拖动方 end_drag() 之后才异步重建。
        """
        board = self._board(待办=["甲"], 进行中=[])
        todo, doing = board.store.columns[0], board.store.columns[1]
        task_id = todo.tasks[0].id
        old_list = board._lists[todo.id]

        board.begin_drag()
        board.drop_task(task_id, doing.id, "", False)
        self.assertTrue(board._reload_pending, "拖动期间应当只挂起重建")
        self.assertIs(board._lists[todo.id], old_list, "拖动期间视图不该被重建")
        self.assertEqual([t.title for t in doing.tasks], ["甲"], "模型要立刻更新")

        board.end_drag()
        QApplication.processEvents()
        self.assertFalse(board._reload_pending)
        self.assertIsNot(board._lists[todo.id], old_list, "拖动结束后才重建")

    def test_request_reload_immediate_when_idle(self):
        board = self._board(待办=["甲"])
        todo = board.store.columns[0]
        old = board._lists[todo.id]
        board.request_reload()
        self.assertIsNot(board._lists[todo.id], old)


class TestBoardActions(_QtCase):

    def test_delete_task_asks_first(self):
        board = self._board(待办=["甲"])
        task_id = board.store.columns[0].tasks[0].id
        with mock.patch.object(tb.QMessageBox, "question",
                               return_value=tb.QMessageBox.Yes):
            board.delete_task(task_id)
        self.assertEqual(board.store.task_count(), 0)

    def test_delete_task_cancelled_keeps_it(self):
        board = self._board(待办=["甲"])
        task_id = board.store.columns[0].tasks[0].id
        with mock.patch.object(tb.QMessageBox, "question",
                               return_value=tb.QMessageBox.No):
            board.delete_task(task_id)
        self.assertEqual(board.store.task_count(), 1)

    def test_clear_done_asks_and_clears(self):
        board = self._board(待办=["甲"], 已完成=["旧一", "旧二"])
        with mock.patch.object(tb.QMessageBox, "question",
                               return_value=tb.QMessageBox.Yes):
            board.clear_done()
        self.assertEqual(board.store.task_count(), 1)
        self.assertEqual(board.store.done_count(), 0)

    def test_delete_last_column_is_refused_by_store(self):
        """数据层仍守住「至少一列」（界面已不暴露删列，见 TestBoardActions）。"""
        store = tb.TaskBoardStore()
        while len(store.columns) > 1:
            store.delete_column(store.columns[-1].id)
        self.assertEqual(len(store.columns), 1)
        self.assertFalse(store.delete_column(store.columns[0].id))

    def test_no_new_column_entry(self):
        """★ 需求：取消「新建列」——看板默认就是待办/进行中/已完成。"""
        board = self._board()
        self.assertEqual([c.name for c in board.store.columns],
                         ["待办", "进行中", "已完成"])
        self.assertFalse(hasattr(board, "new_column"), "不该还有新建列的界面入口")
        from PySide6.QtWidgets import QPushButton
        texts = [b.text() for b in board.findChildren(QPushButton)]
        self.assertNotIn("＋ 新建列", texts)

    def test_column_menu_keeps_delete_but_has_no_add(self):
        """列菜单：保留「删除本列…」（误建的列要能自己删掉），但不提供新建列。"""
        board = self._board()
        cid = board.store.columns[0].id
        labels = [a.text() for a in board.build_column_menu(cid).actions()]
        self.assertIn("重命名…", labels)
        self.assertIn("清空本列…", labels)
        self.assertIn("删除本列…", labels)
        self.assertNotIn("新建列", "".join(labels))

    def test_delete_column_asks_then_persists(self):
        board = self._board(待办=["甲"])
        cid = board.store.columns[1].id            # 删「进行中」（空列）
        with mock.patch.object(tb.QMessageBox, "question",
                               return_value=tb.QMessageBox.Yes):
            board.delete_column(cid)
        self.assertEqual([c.name for c in board.store.columns], ["待办", "已完成"])
        self.assertEqual([c.name for c in tb.TaskBoardStore().columns],
                         ["待办", "已完成"], "要落盘，不能只改内存")

    def test_delete_column_cancelled_keeps_it(self):
        board = self._board()
        cid = board.store.columns[1].id
        with mock.patch.object(tb.QMessageBox, "question",
                               return_value=tb.QMessageBox.No):
            board.delete_column(cid)
        self.assertEqual(len(board.store.columns), 3)

    def test_delete_last_column_is_refused_in_ui(self):
        store = tb.TaskBoardStore()
        while len(store.columns) > 1:
            store.delete_column(store.columns[-1].id)
        board = tb.TaskBoardWidget(store)
        with mock.patch.object(tb.QMessageBox, "information") as info:
            board.delete_column(store.columns[0].id)
        self.assertTrue(info.called)
        self.assertEqual(len(store.columns), 1)

    def test_task_menu_items(self):
        board = self._board(待办=["甲"], 进行中=["乙"])
        tid = board.store.columns[0].tasks[0].id
        labels = [a.text() for a in board.build_task_menu(tid).actions()]
        for expected in ("编辑…", "移到", "上移", "下移", "删除"):
            self.assertIn(expected, labels)

    def test_rename_column_persists(self):
        board = self._board()
        cid = board.store.columns[0].id
        with mock.patch.object(tb.QInputDialog, "getText",
                               return_value=("今天要做", True)):
            board.rename_column(cid)
        self.assertEqual(board.store.find_column(cid).name, "今天要做")

    def test_new_task_via_dialog(self):
        board = self._board()
        cid = board.store.columns[0].id
        fake = mock.Mock()
        fake.exec.return_value = tb.QDialog.Accepted
        fake.values.return_value = {"title": "新任务", "note": "", "priority": "high",
                                    "due": "2026-12-01"}
        with mock.patch.object(tb, "TaskDialog", return_value=fake):
            board.new_task(cid)
        self.assertEqual(board.store.columns[0].tasks[0].title, "新任务")
        self.assertEqual(board.store.columns[0].tasks[0].priority, "high")

    def test_edit_task_via_dialog(self):
        board = self._board(待办=["甲"])
        tid = board.store.columns[0].tasks[0].id
        fake = mock.Mock()
        fake.exec.return_value = tb.QDialog.Accepted
        fake.values.return_value = {"title": "甲改", "note": "备注", "priority": "low",
                                    "due": ""}
        with mock.patch.object(tb, "TaskDialog", return_value=fake):
            board.edit_task(tid)
        task = board.store.find_task(tid)[2]
        self.assertEqual((task.title, task.note, task.priority), ("甲改", "备注", "low"))

    def test_dialog_cancelled_changes_nothing(self):
        board = self._board(待办=["甲"])
        tid = board.store.columns[0].tasks[0].id
        fake = mock.Mock()
        fake.exec.return_value = tb.QDialog.Rejected
        with mock.patch.object(tb, "TaskDialog", return_value=fake):
            board.edit_task(tid)
        self.assertEqual(board.store.find_task(tid)[2].title, "甲")

    def test_shutdown_does_not_rewrite_the_file(self):
        """★ 关窗不该无条件重写数据文件。

        每次改动本来就即时落盘；关窗前再整份写一遍，只会在用户手改 json 之后
        把内存里的旧状态盖回去 —— 那正是「手动删掉的列又自己回来」的成因。
        """
        board = self._board(待办=["甲"])
        with mock.patch.object(board.store, "save") as save:
            board.shutdown()
        self.assertFalse(save.called)

    def test_shutdown_retries_after_failed_save(self):
        """上次落盘失败（磁盘满 / 权限）时，关窗前补写一次，别真丢改动。"""
        board = self._board(待办=["甲"])
        with mock.patch.object(board.store, "save", return_value=False):
            board._commit()
        self.assertTrue(board._dirty)
        with mock.patch.object(board.store, "save", return_value=True) as save:
            board.shutdown()
        self.assertTrue(save.called)
        self.assertFalse(board._dirty)


class TestTaskDialog(_QtCase):

    def test_new_dialog_defaults(self):
        dlg = tb.TaskDialog()
        self.assertEqual(dlg.windowTitle(), "新建任务")
        self.assertEqual(dlg.values()["priority"], tb.DEFAULT_PRIORITY)
        self.assertEqual(dlg.values()["due"], "")
        self.assertEqual(dlg.values()["title"], "")

    def test_empty_title_is_rejected(self):
        dlg = tb.TaskDialog()
        with mock.patch.object(tb.QMessageBox, "information") as info:
            dlg._on_accept()
        self.assertTrue(info.called)
        self.assertEqual(dlg.result(), 0)

    def test_edit_dialog_prefills(self):
        task = tb.Task(title="甲", note="说明", priority="high", due="2026-10-10")
        dlg = tb.TaskDialog(task=task, column_name="待办")
        self.assertEqual(dlg.windowTitle(), "编辑任务")
        values = dlg.values()
        self.assertEqual(values["title"], "甲")
        self.assertEqual(values["note"], "说明")
        self.assertEqual(values["priority"], "high")
        self.assertEqual(values["due"], "2026-10-10")

    def test_due_toggle_controls_date(self):
        dlg = tb.TaskDialog()
        self.assertFalse(dlg.due_edit.isEnabled())
        dlg.due_box.setChecked(True)
        self.assertTrue(dlg.due_edit.isEnabled())
        self.assertTrue(dlg.values()["due"])


class TestDelegate(_QtCase):

    def _delegate_case(self, note: str = ""):
        """造一个只有一张卡片的列表，返回 (delegate, option, index)。"""
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QStyleOptionViewItem

        store = tb.TaskBoardStore()
        cid = store.columns[0].id
        store.add_task(cid, "无备注")
        store.add_task(cid, "有备注", note=note)
        board = tb.TaskBoardWidget(store)
        lw = board._lists[cid]
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, tb.MIN_COLUMN_W, 100)
        return lw, option

    def test_note_takes_exactly_one_line(self):
        """★ 需求：备注在列表里默认只显示一行 —— 再长也只占一行高度。"""
        self.assertEqual(tb.NOTE_MAX_LINES, 1)
        lw, option = self._delegate_case("短备注")
        delegate = lw.itemDelegate()
        plain = delegate.sizeHint(option, lw.indexFromItem(lw.item(0)))
        noted = delegate.sizeHint(option, lw.indexFromItem(lw.item(1)))
        self.assertEqual(plain.height(), tb.CARD_H, "没备注就只有标题+元信息两行")
        self.assertEqual(noted.height(), tb.CARD_H + tb.NOTE_LINE_H)

    def test_long_note_is_capped_at_one_line(self):
        """很长的备注也只占一行（超出用「…」收尾，完整内容看悬停提示）。"""
        lw, option = self._delegate_case("很长很长的备注" * 40)
        delegate = lw.itemDelegate()
        noted = delegate.sizeHint(option, lw.indexFromItem(lw.item(1)))
        self.assertEqual(noted.height(), tb.CARD_H + tb.NOTE_LINE_H)
        task = lw.item(1).data(Qt.UserRole)
        lines = delegate.note_lines(task, QFontMetrics(option.font),
                                    option.rect.width())
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("…"), "放不下的部分要省略号收尾")

    def test_placeholder_has_its_own_height(self):
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QStyleOptionViewItem

        board = self._board()
        lw = board._lists[board.store.columns[0].id]
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, tb.MIN_COLUMN_W, 100)
        hint = lw.itemDelegate().sizeHint(option, lw.indexFromItem(lw.item(0)))
        self.assertEqual(hint.height(), tb.PLACEHOLDER_H)

    def test_paint_smoke_all_priorities_and_states(self):
        """三种优先级 + 选中态各画一遍，确保自绘路径不抛异常（漏导入之类）。"""
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

        board = self._board(待办=["高", "中", "低", "有备注"])
        cid = board.store.columns[0].id
        board.store.columns[0].tasks[0].priority = "high"
        board.store.columns[0].tasks[1].priority = "normal"
        board.store.columns[0].tasks[2].priority = "low"
        board.store.columns[0].tasks[3].note = "一行备注"
        board.reload()
        lw = board._lists[cid]
        delegate = lw.itemDelegate()
        pixmap = QPixmap(tb.MIN_COLUMN_W, 120)
        pixmap.fill(Qt.white)
        painter = QPainter(pixmap)
        try:
            for row in range(lw.count()):
                for state in (QStyle.State_None, QStyle.State_Selected):
                    option = QStyleOptionViewItem()
                    option.rect = QRect(0, 0, tb.MIN_COLUMN_W, 90)
                    option.state = state
                    delegate.paint(painter, option, lw.indexFromItem(lw.item(row)))
        finally:
            painter.end()


class TestWrapLines(_QtCase):

    def _fm(self):
        from PySide6.QtGui import QFont, QFontMetrics
        font = QFont()
        font.setPointSizeF(10.0)
        return QFontMetrics(font)

    def test_short_text_is_one_line(self):
        fm = self._fm()
        self.assertEqual(tb.wrap_lines("短", fm, 200, 2), ["短"])

    def test_long_text_wraps_into_two_lines(self):
        fm = self._fm()
        text = "这是一段很长的备注" * 8
        lines = tb.wrap_lines(text, fm, 180, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"), "放不下的部分要用省略号收尾")
        for line in lines:
            self.assertLessEqual(fm.horizontalAdvance(line), 180)

    def test_max_lines_is_respected(self):
        fm = self._fm()
        text = "abc def ghi jkl mno pqr stu vwx yz " * 10
        self.assertEqual(len(tb.wrap_lines(text, fm, 120, 1)), 1)
        self.assertEqual(len(tb.wrap_lines(text, fm, 120, 3)), 3)

    def test_degenerate_inputs(self):
        fm = self._fm()
        self.assertEqual(tb.wrap_lines("", fm, 100, 2), [])
        self.assertEqual(tb.wrap_lines("甲", fm, 0, 2), [])
        self.assertEqual(tb.wrap_lines("甲", fm, 100, 0), [])

    def test_text_fitting_exactly_is_not_elided(self):
        fm = self._fm()
        width = fm.horizontalAdvance("刚好一行")
        lines = tb.wrap_lines("刚好一行", fm, width, 2)
        self.assertEqual(lines, ["刚好一行"])


class TestPriorityColors(_QtCase):

    def test_priority_tokens_are_red_green_gray(self):
        """★ 需求：优先级颜色分别是红 / 绿 / 灰。"""
        self.assertEqual(tb.PRIORITY_TOKENS["high"], "danger")
        self.assertEqual(tb.PRIORITY_TOKENS["normal"], "success")
        self.assertEqual(tb.PRIORITY_TOKENS["low"], "text_muted")

    def test_resolved_colors_are_distinct_red_green_gray(self):
        from app.ui import theme
        high = theme.token(tb.PRIORITY_TOKENS["high"])
        normal = theme.token(tb.PRIORITY_TOKENS["normal"])
        low = theme.token(tb.PRIORITY_TOKENS["low"])
        self.assertEqual(len({high, normal, low}), 3, "三种优先级必须颜色不同")

        def rgb(value):
            value = value.lstrip("#")
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

        r, g, b = rgb(high)
        self.assertGreater(r, g, "高优先级应当是红色系")
        self.assertGreater(r, b)
        r, g, b = rgb(normal)
        self.assertGreater(g, r, "中优先级应当是绿色系")
        self.assertGreater(g, b)
        r, g, b = rgb(low)
        self.assertLess(max(r, g, b) - min(r, g, b), 30, "低优先级应当是灰色系")


class TestExternalSync(_QtCase):
    """磁盘数据被外部改过时**以磁盘为准**重载，而不是被内存里的旧状态盖回去。

    背景（真实故障）：用户在编辑器里手动删掉了 task_board.json 里的一列，但小程序
    还开着、内存里仍持有那一列，下一次改动/关窗就把它整份写回文件——表现就是
    「手动删掉的列又自己回来了」。
    """

    @staticmethod
    def _edit_on_disk(mutate):
        """模拟「用户手改 json」：另开一个 store 改完落盘。"""
        store = tb.TaskBoardStore()
        mutate(store)
        store.save()

    def test_external_edit_is_picked_up(self):
        board = self._board(待办=["甲"])
        self._edit_on_disk(lambda s: s.delete_column(s.columns[1].id))
        board._sync_from_disk()
        self.assertEqual([c.name for c in board.store.columns], ["待办", "已完成"])
        self.assertEqual(len(board._columns), 2, "视图也要跟着重建")

    def test_external_edit_keeps_memory_out_of_the_file(self):
        """★ 回归：外部改动之后，内存里的旧状态不许再写回文件。"""
        board = self._board(待办=["甲"])
        self._edit_on_disk(lambda s: s.delete_column(s.columns[1].id))
        board._sync_from_disk()
        with mock.patch.object(board.store, "save") as save:
            board.shutdown()                      # 关窗
        self.assertFalse(save.called)
        self.assertEqual([c.name for c in tb.TaskBoardStore().columns],
                         ["待办", "已完成"], "文件里不该再冒出被删掉的列")

    def test_no_change_means_no_reload(self):
        board = self._board(待办=["甲"])
        before = board._lists[board.store.columns[0].id]
        board._sync_from_disk()
        self.assertIs(board._lists[board.store.columns[0].id], before,
                      "磁盘没变就不该重建视图（滚动位置/选中态要留住）")

    def test_own_save_does_not_trigger_reload(self):
        board = self._board(待办=["甲"])
        board.drop_task(board.store.columns[0].tasks[0].id,
                        board.store.columns[1].id, "", False)
        before = board._lists[board.store.columns[0].id]
        board._sync_from_disk()
        self.assertIs(board._lists[board.store.columns[0].id], before,
                      "自己刚写过的内容不该被当成外部改动")

    def test_half_written_json_is_ignored(self):
        """手改到一半（非法 json）时先不动，等写完再同步 —— 免得反手盖成默认看板。"""
        board = self._board(待办=["甲"])
        with open(board.store.path, "w", encoding="utf-8") as f:
            f.write('{"version": 1, "columns": [')          # 半截
        board._sync_from_disk()
        self.assertEqual([c.name for c in board.store.columns],
                         ["待办", "进行中", "已完成"])
        self.assertEqual(board.store.task_count(), 1, "内存里的任务不能被清掉")

    def test_json_without_columns_is_ignored(self):
        board = self._board(待办=["甲"])
        with open(board.store.path, "w", encoding="utf-8") as f:
            f.write('{"version": 1}')
        board._sync_from_disk()
        self.assertEqual(board.store.task_count(), 1)

    def test_missing_file_is_ignored(self):
        board = self._board(待办=["甲"])
        os.remove(board.store.path)
        board._sync_from_disk()
        self.assertEqual(board.store.task_count(), 1)

    def test_sync_skipped_while_dragging(self):
        board = self._board(待办=["甲"])
        self._edit_on_disk(lambda s: s.delete_column(s.columns[1].id))
        board.begin_drag()
        board._sync_from_disk()
        self.assertEqual(len(board.store.columns), 3, "拖动中不碰数据")
        board.end_drag()
        board._sync_from_disk()
        self.assertEqual(len(board.store.columns), 2, "拖动结束后补上")

    def test_show_and_hide_manage_the_sync_timer(self):
        board = self._board()
        self.assertFalse(board._sync_timer.isActive())
        board.show()
        QApplication.processEvents()
        self.assertTrue(board._sync_timer.isActive())
        board.hide()
        self.assertFalse(board._sync_timer.isActive())


class TestLooksLikeBoardGate(_QtCase):

    def test_gate_accepts_only_board_shaped_json(self):
        self.assertTrue(tb._looks_like_board('{"columns": []}'))
        self.assertTrue(tb._looks_like_board('{"version": 1, "columns": [{}]}'))
        self.assertFalse(tb._looks_like_board('{"version": 1}'))
        self.assertFalse(tb._looks_like_board('[]'))
        self.assertFalse(tb._looks_like_board(''))
        self.assertFalse(tb._looks_like_board('{"columns": [}'))     # 半截


# ---------------------------------------------------------------------------
# 接线：注册到小工具页
# ---------------------------------------------------------------------------
class TestRegistration(_QtCase):

    def test_registered_as_builtin_tool(self):
        app = mini_apps.find_app("task_board")
        self.assertIsNotNone(app, "任务看板应当已登记到小程序注册表")
        self.assertEqual(app.name, "任务看板")
        self.assertTrue(app.icon)
        self.assertTrue(app.desc)

    def test_factory_makes_new_widgets(self):
        app = mini_apps.find_app("task_board")
        first, second = app.create(), app.create()
        self.assertIsInstance(first, tb.TaskBoardWidget)
        self.assertIsNot(first, second)

    def test_tools_tab_shows_the_card(self):
        tab = ToolsTab()
        self.assertIn("任务看板", [c.text() for c in tab.cards()])

    def test_mini_window_hosts_the_board(self):
        from app.ui import mini_window
        win = mini_window.open_mini_app(mini_apps.find_app("task_board"))
        try:
            self.assertIsInstance(win.content, tb.TaskBoardWidget)
            self.assertEqual(win.windowTitle(), "任务看板")
        finally:
            mini_window.close_all()

    def test_window_close_does_not_rewrite_the_file(self):
        """关窗只走 shutdown()，不再无条件重写文件（免得盖掉用户手改的内容）。"""
        from app.ui import mini_window
        win = mini_window.open_mini_app(mini_apps.find_app("task_board"))
        board = win.content
        with mock.patch.object(board.store, "save") as save:
            win.close()
        self.assertFalse(save.called)


if __name__ == "__main__":
    unittest.main()
