# -*- coding: utf-8 -*-
"""「剪贴板」小程序（app/mini_apps/clipboard_history.py）的测试。

覆盖：
- 数据模型：类型判定优先级（文件 > 图片 > 文本）、内容指纹、摘要/体积/时间格式；
- 仓库：倒序入库、同内容去重并提前、上限淘汰但保留收藏、增删改查、批量删除；
- **当前剪贴板**：横幅显示 + 列表里的「● 当前」特殊标记；
- **永久本地存储**：文本/图片/文件都能落盘并读回、原子写、坏数据兜底、孤儿图片清理；
- 界面：历史与收藏**两个独立面板**、多选批量删除、选中态按钮启停、复制/修改/清空；
- 功能边界（源码级契约）：ctypes 铁律、上限合理、注册到小工具页。

隔离方式：剪贴板历史现在会落盘，所以每个用例都把 `config.BASE_DIR` 切到临时目录
（`TempConfigPaths`），绝不碰真实的 `clipboard_data`。离屏平台的剪贴板是进程内的，
也不会动用户的系统剪贴板。
"""
from __future__ import annotations

import inspect
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QSize, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402
from PySide6.QtWidgets import (QAbstractItemView, QApplication,  # noqa: E402
                               QDialog, QSizePolicy)

from app import mini_apps  # noqa: E402
from app.mini_apps import clipboard_history as ch  # noqa: E402
from app.ui import theme  # noqa: E402
from tests._env import TempConfigPaths  # noqa: E402


def _text_entry(text="内容", **kw) -> ch.ClipEntry:
    return ch.ClipEntry(kind=ch.KIND_TEXT, text=text, **kw)


def _image_entry(color=0x336699, w=6, h=4) -> ch.ClipEntry:
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(color)
    return ch.ClipEntry(kind=ch.KIND_IMAGE, image=image)


def _files_entry(paths=None) -> ch.ClipEntry:
    return ch.ClipEntry(kind=ch.KIND_FILES,
                        files=list(paths or [r"C:\Windows\notepad.exe"]))


def _rows(listw):
    return [listw.item(i) for i in range(listw.count())]


class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 剪贴板历史会落盘：BASE_DIR 切到临时目录，绝不写真实 clipboard_data
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()
        # 离屏剪贴板是进程内共享的，用例之间互相擦干净
        QGuiApplication.clipboard().clear()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class TestEntryModel(_QtCase):

    def test_mime_precedence_files_over_image_over_text(self):
        mime = QMimeData()
        mime.setText("也有文字")
        self.assertEqual(ch.entry_from_mime(mime).kind, ch.KIND_TEXT)

        mime.setImageData(_image_entry().image)
        self.assertEqual(ch.entry_from_mime(mime).kind, ch.KIND_IMAGE)

        mime.setUrls([QUrl.fromLocalFile(r"C:\Windows\notepad.exe")])
        self.assertEqual(ch.entry_from_mime(mime).kind, ch.KIND_FILES,
                         "同时有文件和其它格式时，按复制文件的本意取文件")

    def test_mime_without_usable_content(self):
        self.assertIsNone(ch.entry_from_mime(QMimeData()))
        self.assertIsNone(ch.entry_from_mime(None))

    def test_remote_urls_are_text_not_files(self):
        """网络地址不算「复制的文件」，但会当成文本记下来（复制链接的常见情形）。"""
        mime = QMimeData()
        mime.setUrls([QUrl("http://example.com/a.txt")])
        entry = ch.entry_from_mime(mime)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.kind, ch.KIND_TEXT)
        self.assertIn("example.com", entry.text)

    def test_long_text_is_truncated(self):
        mime = QMimeData()
        mime.setText("啊" * (ch.MAX_TEXT_CHARS + 50))
        entry = ch.entry_from_mime(mime)
        self.assertEqual(len(entry.text), ch.MAX_TEXT_CHARS)

    def test_digest_is_content_based(self):
        self.assertEqual(_text_entry("甲").digest(), _text_entry("甲").digest())
        self.assertNotEqual(_text_entry("甲").digest(), _text_entry("乙").digest())
        self.assertEqual(_image_entry(0x111111).digest(),
                         _image_entry(0x111111).digest())
        self.assertNotEqual(_image_entry(0x111111).digest(),
                            _image_entry(0x222222).digest(),
                            "图片指纹必须看内容，不能只比尺寸")

    def test_image_digest_of_empty(self):
        self.assertEqual(ch.image_digest(None), "empty")
        self.assertEqual(ch.image_digest(QImage()), "empty")

    def test_summary_forms(self):
        self.assertEqual(_text_entry("你好\n世界").summary(), "你好 世界")
        self.assertIn("6×4", _image_entry().summary())
        self.assertIn("1 个文件", _files_entry().summary())
        self.assertIn("notepad.exe", _files_entry().summary())

    def test_one_line_truncates(self):
        entry = _text_entry("字" * 200)
        self.assertLessEqual(len(entry.one_line(10)), 11)
        self.assertTrue(entry.one_line(10).endswith("…"))

    def test_byte_size(self):
        self.assertEqual(_text_entry("abc").byte_size(), 3)
        self.assertEqual(_text_entry("中").byte_size(), 3)
        self.assertGreater(_image_entry().byte_size(), 0)
        self.assertGreater(_files_entry().byte_size(), 0)

    def test_clock_and_stamp(self):
        entry = _text_entry()
        self.assertRegex(entry.clock(), r"^\d\d:\d\d:\d\d$")
        self.assertRegex(entry.stamp(), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_uid_is_unique(self):
        self.assertNotEqual(_text_entry().uid, _text_entry().uid)


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------
class TestStore(_QtCase):

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.addCleanup(self.store.stop_watching)

    def test_newest_first(self):
        first = self.store.add(_text_entry("甲"))
        second = self.store.add(_text_entry("乙"))
        self.assertEqual([e.text for e in self.store.entries], ["乙", "甲"])
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_duplicate_moves_to_front_without_adding(self):
        self.store.add(_text_entry("甲"))
        self.store.add(_text_entry("乙"))
        result = self.store.add(_text_entry("甲"))
        self.assertIsNone(result, "同内容不该新增")
        self.assertEqual([e.text for e in self.store.entries], ["甲", "乙"])
        self.assertEqual(len(self.store.entries), 2)

    def test_changed_signal(self):
        seen = []
        self.store.changed.connect(lambda: seen.append(1))
        self.store.add(_text_entry("甲"))
        self.store.add(_text_entry("乙"))
        self.store.remove(self.store.entries[0].uid)
        self.assertEqual(len(seen), 3)

    def test_trim_drops_oldest_but_keeps_favorites(self):
        with mock.patch.object(ch, "MAX_ENTRIES", 3):
            favorite = _text_entry("收藏的", favorite=True)
            self.store.add(favorite)
            for i in range(5):
                self.store.add(_text_entry(f"第{i}条"))
        self.assertLessEqual(len(self.store.entries), 4)   # 3 条上限 + 收藏不算数
        self.assertIn("收藏的", [e.text for e in self.store.entries])

    def test_remove_and_get(self):
        entry = self.store.add(_text_entry("甲"))
        self.assertIs(self.store.get(entry.uid), entry)
        self.assertTrue(self.store.remove(entry.uid))
        self.assertFalse(self.store.remove(entry.uid))
        self.assertIsNone(self.store.get(entry.uid))

    def test_remove_many(self):
        for i in range(4):
            self.store.add(_text_entry(f"第{i}条"))
        uids = [e.uid for e in self.store.entries[:2]]
        self.assertEqual(self.store.remove_many(uids), 2)
        self.assertEqual(len(self.store.entries), 2)
        self.assertEqual(self.store.remove_many(uids), 0, "已经删过的不再计数")
        self.assertEqual(self.store.remove_many([]), 0)

    def test_toggle_favorite(self):
        entry = self.store.add(_text_entry("甲"))
        self.assertTrue(self.store.toggle_favorite(entry.uid))
        self.assertTrue(entry.favorite)
        self.assertFalse(self.store.toggle_favorite(entry.uid))
        self.assertIsNone(self.store.toggle_favorite("不存在"))

    def test_set_favorite_many(self):
        for i in range(3):
            self.store.add(_text_entry(f"第{i}条"))
        uids = [e.uid for e in self.store.entries[:2]]
        self.assertEqual(self.store.set_favorite_many(uids, True), 2)
        self.assertEqual(len(self.store.favorite_entries()), 2)
        self.assertEqual(self.store.set_favorite_many(uids, True), 0, "没变就不算改动")
        self.assertEqual(self.store.set_favorite_many(uids, False), 2)
        self.assertEqual(self.store.favorite_entries(), [])

    def test_clear_keeps_favorites_by_default(self):
        self.store.add(_text_entry("甲"))
        entry = self.store.add(_text_entry("乙"))
        self.store.toggle_favorite(entry.uid)
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual([e.text for e in self.store.entries], ["乙"])

    def test_clear_all(self):
        self.store.add(_text_entry("甲"))
        self.assertEqual(self.store.clear(keep_favorites=False), 1)
        self.assertEqual(self.store.entries, [])

    def test_update_text(self):
        entry = self.store.add(_text_entry("旧"))
        self.assertTrue(self.store.update_text(entry.uid, "新"))
        self.assertEqual(entry.text, "新")
        self.assertTrue(entry.edited)
        self.assertFalse(self.store.update_text(entry.uid, ""))
        self.assertFalse(self.store.update_text("不存在", "x"))

    def test_update_text_rejects_non_text(self):
        entry = self.store.add(_image_entry())
        self.assertFalse(self.store.update_text(entry.uid, "想改成文字"))
        self.assertEqual(entry.kind, ch.KIND_IMAGE)

    def test_update_text_refreshes_digest(self):
        """改完内容后再复制同样的旧内容，应当被当成新的一条。"""
        entry = self.store.add(_text_entry("原文"))
        self.store.update_text(entry.uid, "改过")
        self.assertIsNotNone(self.store.add(_text_entry("原文")))
        self.assertEqual(len(self.store.entries), 2)

    # ---------- 两个面板的数据来源（收藏单独一侧）----------
    def test_history_entries_exclude_favorites(self):
        self.store.add(_text_entry("甲"))
        middle = self.store.add(_text_entry("乙"))
        self.store.add(_text_entry("丙"))
        self.store.toggle_favorite(middle.uid)
        self.assertEqual([e.text for e in self.store.history_entries()],
                         ["丙", "甲"], "历史面板不该再出现收藏项")
        self.assertEqual([e.text for e in self.store.favorite_entries()], ["乙"])

    def test_panels_are_newest_first(self):
        for i in range(3):
            self.store.add(_text_entry(f"第{i}条"))
        self.assertEqual([e.text for e in self.store.history_entries()],
                         ["第2条", "第1条", "第0条"])

    def test_default_store_is_shared(self):
        with mock.patch.object(ch, "_DEFAULT_STORE", None):
            self.assertIs(ch.default_store(), ch.default_store())
            ch._DEFAULT_STORE = None        # 别把临时目录里的实例留给后面的用例


# ---------------------------------------------------------------------------
# 真实剪贴板往返（离屏 = 进程内剪贴板，不碰用户的系统剪贴板）
# ---------------------------------------------------------------------------
class TestClipboardRoundTrip(_QtCase):

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.addCleanup(self.store.stop_watching)
        self.clip = QGuiApplication.clipboard()

    def test_capture_text(self):
        self.clip.setText("你好 剪贴板")
        entry = self.store.capture_now()
        self.assertEqual(entry.kind, ch.KIND_TEXT)
        self.assertEqual(entry.text, "你好 剪贴板")

    def test_capture_image(self):
        self.clip.setImage(_image_entry(0xABCDEF).image)
        entry = self.store.capture_now()
        self.assertEqual(entry.kind, ch.KIND_IMAGE)
        self.assertEqual((entry.image.width(), entry.image.height()), (6, 4))

    def test_capture_files(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(r"C:\Windows\notepad.exe")])
        self.clip.setMimeData(mime)
        entry = self.store.capture_now()
        self.assertEqual(entry.kind, ch.KIND_FILES)
        self.assertEqual(len(entry.files), 1)

    def test_recopy_same_text_does_not_duplicate(self):
        self.clip.setText("甲")
        self.store.capture_now()
        self.clip.setText("乙")
        self.store.capture_now()
        self.clip.setText("甲")
        self.store.capture_now()
        self.assertEqual([e.text for e in self.store.entries], ["甲", "乙"])

    def test_apply_text_back_to_clipboard(self):
        entry = _text_entry("写回去")
        self.assertTrue(ch.apply_entry_to_clipboard(entry, self.clip))
        self.assertEqual(self.clip.text(), "写回去")

    def test_apply_image_back_to_clipboard(self):
        entry = _image_entry(0x123456)
        self.assertTrue(ch.apply_entry_to_clipboard(entry, self.clip))
        self.assertTrue(self.clip.mimeData().hasImage())
        self.assertEqual(self.clip.image().size(), entry.image.size())

    def test_apply_files_back_to_clipboard(self):
        entry = _files_entry()
        self.assertTrue(ch.apply_entry_to_clipboard(entry, self.clip))
        urls = [u.toLocalFile() for u in self.clip.mimeData().urls()]
        self.assertEqual([os.path.normpath(u) for u in urls],
                         [os.path.normpath(entry.files[0])])

    def test_apply_empty_entry_returns_false(self):
        empty_image = ch.ClipEntry(kind=ch.KIND_IMAGE, image=None)
        empty_files = ch.ClipEntry(kind=ch.KIND_FILES, files=[])
        self.assertFalse(ch.apply_entry_to_clipboard(_text_entry(""), self.clip))
        self.assertFalse(ch.apply_entry_to_clipboard(empty_image, self.clip))
        self.assertFalse(ch.apply_entry_to_clipboard(empty_files, self.clip))

    def test_watching_records_clipboard_changes(self):
        self.store.start_watching()
        self.assertTrue(self.store.watching)
        before = len(self.store.entries)
        self.clip.setText("监听中复制的内容")
        QApplication.processEvents()
        self.assertGreater(len(self.store.entries), before)
        self.assertEqual(self.store.entries[0].text, "监听中复制的内容")

    def test_stop_watching_is_idempotent(self):
        self.store.start_watching()
        self.store.stop_watching()
        self.store.stop_watching()
        self.assertFalse(self.store.watching)

    def test_poll_tick_only_reads_on_sequence_change(self):
        with mock.patch.object(ch, "clipboard_sequence", return_value=7):
            self.store._last_seq = 7
            self.clip.setText("轮询内容")
            self.store._poll_tick()             # 序号没变 -> 不读
            self.assertEqual(self.store.entries, [])
            self.store._last_seq = 6
            self.store._poll_tick()             # 序号变了 -> 读一次
            self.assertEqual(len(self.store.entries), 1)

    def test_poll_tick_ignores_unavailable_sequence(self):
        with mock.patch.object(ch, "clipboard_sequence", return_value=-1):
            self.clip.setText("拿不到序号")
            self.store._poll_tick()
        self.assertEqual(self.store.entries, [])


# ---------------------------------------------------------------------------
# 当前剪贴板（横幅 + 特殊标记）
# ---------------------------------------------------------------------------
class TestCurrentClipboard(_QtCase):
    """★ 需求：显示当前可粘贴的剪贴板内容，并特殊标记出来。"""

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.addCleanup(self.store.stop_watching)
        self.clip = QGuiApplication.clipboard()

    def test_capture_sets_current(self):
        self.clip.setText("此刻能粘贴的")
        self.store.capture_now()
        self.assertIsNotNone(self.store.current)
        self.assertEqual(self.store.current.kind, ch.KIND_TEXT)
        self.assertEqual(self.store.current.summary(), "此刻能粘贴的")

    def test_current_uid_points_at_the_history_entry(self):
        self.clip.setText("甲")
        self.store.capture_now()
        self.assertEqual(self.store.current_uid(), self.store.entries[0].uid)
        self.assertTrue(self.store.is_current(self.store.entries[0]))

    def test_current_uid_is_empty_after_history_cleared(self):
        self.clip.setText("甲")
        self.store.capture_now()
        self.store.clear()
        self.assertEqual(self.store.current_uid(), "", "历史里没有就不该标记")
        self.assertIsNotNone(self.store.current, "但横幅仍要显示剪贴板内容")

    def test_duplicate_capture_reuses_the_history_entry(self):
        self.clip.setText("重复内容")
        self.store.capture_now()
        entry = self.store.entries[0]
        self.clip.setText("别的")
        self.store.capture_now()
        self.clip.setText("重复内容")
        self.store.capture_now()
        self.assertIs(self.store.current, entry, "重复时 current 指回历史里那条")

    def test_unknown_mime_leaves_current_none(self):
        self.clip.clear()
        self.assertIsNone(self.store.capture_now())
        self.assertIsNone(self.store.current)


# ---------------------------------------------------------------------------
# 图片预览（单张 / 多张）
# ---------------------------------------------------------------------------
class TestImagePreviewHelpers(_QtCase):
    """`is_image_path` / `previewable_count` / `preview_images`。"""

    def setUp(self):
        super().setUp()
        self.tmpdir = self._tmp.tmp

    def _png(self, name: str, color=0x336699, size=(8, 6)) -> str:
        path = os.path.join(self.tmpdir, name)
        image = QImage(size[0], size[1], QImage.Format_RGB32)
        image.fill(color)
        self.assertTrue(image.save(path, "PNG"))
        return path

    def test_is_image_path(self):
        for name in ("a.png", "b.JPG", "c.jpeg", "d.bmp", "e.webp"):
            self.assertTrue(ch.is_image_path(name), name)
        for name in ("a.txt", "b.exe", "c", "d.pdf", ""):
            self.assertFalse(ch.is_image_path(name), name)

    def test_images_from_image_entries(self):
        images = ch.preview_images([_image_entry(), _text_entry("文本")])
        self.assertEqual(len(images), 1, "文本记录不参与图片预览")
        self.assertEqual(images[0][1].size(), QSize(6, 4))

    def test_images_from_files_entries(self):
        """★ 一次复制多个图片文件时，要能一起预览（多图）。"""
        paths = [self._png("a.png"), self._png("b.png", 0x00FF00),
                 self._png("c.png", 0xFF0000)]
        entry = ch.ClipEntry(kind=ch.KIND_FILES,
                             files=paths + [os.path.join(self.tmpdir, "note.txt")])
        images = ch.preview_images([entry])
        self.assertEqual(len(images), 3, "非图片文件要跳过")
        self.assertEqual([t for t, _ in images],
                         ["a.png", "b.png", "c.png"])

    def test_missing_image_file_is_skipped(self):
        entry = ch.ClipEntry(kind=ch.KIND_FILES,
                             files=[os.path.join(self.tmpdir, "没有这张.png")])
        self.assertEqual(ch.preview_images([entry]), [])

    def test_preview_images_is_capped(self):
        entry = ch.ClipEntry(kind=ch.KIND_FILES,
                             files=[self._png(f"{i}.png", i * 1111)
                                    for i in range(ch.MAX_PREVIEW_IMAGES + 4)])
        self.assertEqual(len(ch.preview_images([entry])), ch.MAX_PREVIEW_IMAGES)
        self.assertEqual(ch.previewable_count([entry]),
                         ch.MAX_PREVIEW_IMAGES + 4, "总数还是要如实报出来")

    def test_previewable_count(self):
        entry = ch.ClipEntry(kind=ch.KIND_FILES,
                             files=[self._png("x.png"),
                                    os.path.join(self.tmpdir, "y.txt")])
        self.assertEqual(ch.previewable_count([_image_entry(), entry]), 2)
        self.assertEqual(ch.previewable_count([_text_entry("纯文本")]), 0)


class TestImagePreviewInWidget(_QtCase):
    """★ 需求：粘贴的是图片时下面能预览；支持多图预览。"""

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.widget = ch.ClipboardHistoryWidget(store=self.store)
        self.addCleanup(self.widget.shutdown)
        self.addCleanup(self.widget.deleteLater)
        self.clip = QGuiApplication.clipboard()

    def _png(self, name: str, color=0x336699) -> str:
        path = os.path.join(self._tmp.tmp, name)
        image = QImage(8, 6, QImage.Format_RGB32)
        image.fill(color)
        image.save(path, "PNG")
        return path

    def _tile_count(self) -> int:
        return len(self.widget.gallery.tiles())

    def test_current_clipboard_image_is_previewed_without_selection(self):
        """★ 刚复制一张图（列表里什么都没选中）→ 下面直接看到这张图。"""
        self.clip.setImage(_image_entry(0x336699).image)
        self.store.capture_now()
        self.widget.refresh()
        self.assertEqual(self.widget.selected_entries(), [], "确实没选中任何记录")
        self.assertIs(self.widget.stack.currentWidget(), self.widget.gallery)
        self.assertEqual(self._tile_count(), 1)
        self.assertIn("当前剪贴板", self.widget.detail_head.text())
        self.assertIn("1 张", self.widget.detail_head.text())

    def test_selected_image_entry_is_previewed(self):
        entry = self.store.add(_image_entry())
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        self.assertIs(self.widget.stack.currentWidget(), self.widget.gallery)
        self.assertEqual(self._tile_count(), 1)
        titles = [title for title, _image in self.widget.gallery.images()]
        self.assertIn(entry.clock(), titles[0], "缩略图标题要标出这是哪条记录")

    def test_multiple_image_entries_preview_together(self):
        """★ 多选多条图片记录 -> 网格里一起铺出来（多图预览）。"""
        self.store.add(_image_entry(0x111111))
        self.store.add(_image_entry(0x222222))
        self.store.add(_image_entry(0x333333))
        self.widget.refresh()
        self.widget.list.selectAll()
        self.assertIs(self.widget.stack.currentWidget(), self.widget.gallery)
        self.assertEqual(self._tile_count(), 3)
        self.assertIn("已选中 3 条", self.widget.detail_head.text())

    def test_multiple_image_files_preview_together(self):
        """★ 从资源管理器一次复制多个图片文件 -> 一起预览。"""
        paths = [self._png("a.png", 0x111111), self._png("b.png", 0x222222),
                 self._png("c.png", 0x333333)]
        self.store.add(ch.ClipEntry(kind=ch.KIND_FILES, files=paths))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        self.assertIs(self.widget.stack.currentWidget(), self.widget.gallery)
        self.assertEqual(self._tile_count(), 3)

    def test_text_selection_uses_the_text_page(self):
        self.store.add(_text_entry("只是文本"))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        self.assertIs(self.widget.stack.currentWidget(), self.widget.preview)
        self.assertIn("只是文本", self.widget.preview.text())

    def test_mixed_selection_shows_only_the_images(self):
        self.store.add(_text_entry("文字"))
        self.store.add(_image_entry())
        self.widget.refresh()
        self.widget.list.selectAll()
        self.assertIs(self.widget.stack.currentWidget(), self.widget.gallery)
        self.assertEqual(self._tile_count(), 1, "文本条目不出现在图片网格里")

    def test_clicking_a_tile_opens_the_viewer(self):
        self.store.add(_image_entry(0x445566))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        tile = self.widget.gallery.tiles()[0]
        with mock.patch.object(ch.ImagePreviewDialog, "exec") as run:
            tile.clicked.emit(tile._image)
        self.assertTrue(run.called)

    def test_viewer_shows_the_clicked_image(self):
        image = _image_entry(0x778899, 20, 12).image
        dialog = ch.ImagePreviewDialog(image)
        self.assertEqual(dialog.image().size(), image.size())
        self.assertIn("20×12", dialog.windowTitle())
        dialog.deleteLater()

    def test_gallery_lays_out_in_columns(self):
        self.store.add(ch.ClipEntry(kind=ch.KIND_FILES,
                                    files=[self._png(f"{i}.png", i * 1111)
                                           for i in range(4)]))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        gallery = self.widget.gallery
        gallery.resize(3 * (ch.THUMB_W + ch.THUMB_GAP), 300)
        QApplication.processEvents()
        self.assertGreaterEqual(gallery._column_count(), 2)
        self.assertEqual(len(gallery.tiles()), 4)

    def test_preview_cache_avoids_reloading(self):
        """refresh 很频繁：同一批目标不该每次都去读磁盘上的图。"""
        self.store.add(_image_entry())
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        with mock.patch.object(ch, "preview_images",
                               wraps=ch.preview_images) as loader:
            self.widget._sync_detail()
            self.widget._sync_detail()
        self.assertEqual(loader.call_count, 0, "目标没变就不该重新收集")


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
class TestWidget(_QtCase):

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.widget = ch.ClipboardHistoryWidget(store=self.store)
        self.addCleanup(self.widget.shutdown)
        self.addCleanup(self.widget.deleteLater)

    def _seed(self):
        self.store.add(_text_entry("第一条"))
        self.store.add(_image_entry())
        self.store.add(_files_entry())
        return self.store.entries

    def test_rows_follow_visible_order(self):
        self._seed()
        self.widget.refresh()
        self.assertEqual(self.widget.list.count(), 3)
        self.assertIn("文件", self.widget.list.item(0).text())
        self.assertIn("图片", self.widget.list.item(1).text())
        self.assertIn("第一条", self.widget.list.item(2).text())

    def test_count_label(self):
        self._seed()
        self.widget.refresh()
        self.assertIn("共 3 条", self.widget.count_label.text())

    def test_selection_enables_buttons(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)          # 文件那条
        self.assertTrue(self.widget.copy_btn.isEnabled())
        self.assertTrue(self.widget.fav_btn.isEnabled())
        self.assertFalse(self.widget.edit_btn.isEnabled(), "文件没有可改的内容")

        self.widget.list.setCurrentRow(2)          # 文本那条
        self.assertTrue(self.widget.edit_btn.isEnabled())

    def test_no_selection_disables_buttons(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.setCurrentRow(-1)
        self.widget.list.clearSelection()
        self.widget._sync_detail()
        for button in (self.widget.copy_btn, self.widget.edit_btn,
                       self.widget.fav_btn, self.widget.del_btn):
            self.assertFalse(button.isEnabled(), button.text())

    def test_detail_preview_for_text(self):
        self.store.add(_text_entry("预览内容"))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        self.assertIn("预览内容", self.widget.preview.text())
        self.assertIn("文本", self.widget.detail_head.text())

    def test_detail_preview_for_image_shows_gallery(self):
        self.store.add(_image_entry())
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        self.assertIs(self.widget.stack.currentWidget(), self.widget.gallery,
                      "图片走图片预览页，不再是文本框")
        self.assertEqual(len(self.widget.gallery.tiles()), 1)
        self.assertFalse(self.widget.list.item(0).icon().isNull(), "行上要有缩略图")

    def test_delete_asks_first(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.No) as ask:
            self.widget._on_delete()
        self.assertTrue(ask.called)
        self.assertEqual(len(self.store.entries), 3)

        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.Yes):
            self.widget._on_delete()
        self.assertEqual(len(self.store.entries), 2)

    def test_clear_keeps_favorites(self):
        entries = self._seed()
        self.store.toggle_favorite(entries[0].uid)
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.Yes):
            self.widget._on_clear()
        self.assertEqual(len(self.store.entries), 1)
        self.assertTrue(self.store.entries[0].favorite)

    def test_copy_puts_content_back(self):
        self.store.add(_text_entry("复制我"))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        QGuiApplication.clipboard().clear()
        self.widget._on_copy()
        self.assertEqual(QGuiApplication.clipboard().text(), "复制我")

    def test_double_click_copies(self):
        self.store.add(_text_entry("双击复制"))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        QGuiApplication.clipboard().clear()
        self.widget.list.itemDoubleClicked.emit(self.widget.list.item(0))
        self.assertEqual(QGuiApplication.clipboard().text(), "双击复制")

    def test_edit_applies_new_text(self):
        self.store.add(_text_entry("旧内容"))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)

        class _FakeDialog:
            def __init__(self, text, parent=None):
                self._text = "新内容"

            def exec(self):
                return QDialog.DialogCode.Accepted

            def text(self):
                return self._text

        with mock.patch.object(ch, "EditTextDialog", _FakeDialog):
            self.widget._on_edit()
        self.assertEqual(self.store.entries[0].text, "新内容")
        self.assertIn("已修改", self.widget.list.item(0).text())

    def test_edit_cancelled_changes_nothing(self):
        self.store.add(_text_entry("旧内容"))
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)

        class _FakeDialog:
            def __init__(self, text, parent=None):
                pass

            def exec(self):
                return QDialog.DialogCode.Rejected

            def text(self):
                return "不该生效"

        with mock.patch.object(ch, "EditTextDialog", _FakeDialog):
            self.widget._on_edit()
        self.assertEqual(self.store.entries[0].text, "旧内容")

    def test_shutdown_stops_watching(self):
        self.assertTrue(self.store.watching)
        self.widget.shutdown()
        self.assertFalse(self.store.watching)


class TestTwoPanels(_QtCase):
    """★ 需求：收藏的单独放一侧（左侧历史 / 右侧收藏两个独立面板）。"""

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.widget = ch.ClipboardHistoryWidget(store=self.store)
        self.addCleanup(self.widget.shutdown)
        self.addCleanup(self.widget.deleteLater)

    def _seed(self):
        self.store.add(_text_entry("甲"))
        self.store.add(_text_entry("乙"))
        self.store.add(_text_entry("丙"))

    def test_favorites_live_in_their_own_panel(self):
        self._seed()
        favorite = self.store.entries[1]           # 「乙」（entries 是时间倒序）
        self.store.toggle_favorite(favorite.uid)
        self.widget.refresh()
        self.assertEqual([i.data(Qt.UserRole) for i in _rows(self.widget.list)],
                         [self.store.entries[0].uid, self.store.entries[2].uid],
                         "历史面板只剩非收藏项，且仍是时间倒序")
        self.assertEqual([i.data(Qt.UserRole) for i in _rows(self.widget.fav_list)],
                         [favorite.uid], "收藏项在右侧面板")

    def test_panel_titles_show_counts(self):
        self._seed()
        self.store.toggle_favorite(self.store.entries[0].uid)
        self.widget.refresh()
        self.assertIn("2", self.widget.hist_title.text())
        self.assertIn("1", self.widget.fav_title.text())

    def test_favoriting_from_history_moves_it_right(self):
        self._seed()
        self.widget.list.setCurrentRow(0)
        self.widget._on_toggle_favorite()
        self.assertEqual(self.widget.list.count(), 2)
        self.assertEqual(self.widget.fav_list.count(), 1)

    def test_unfavoriting_from_favorites_moves_it_back(self):
        self._seed()
        self.store.toggle_favorite(self.store.entries[0].uid)
        self.widget.refresh()
        self.widget.fav_list.setCurrentRow(0)
        self.widget._on_toggle_favorite()
        self.assertEqual(self.widget.fav_list.count(), 0)
        self.assertEqual(self.widget.list.count(), 3)


class TestCurrentBannerAndMark(_QtCase):
    """★ 需求：横幅显示当前可粘贴内容，列表里那条特殊标记出来。"""

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.widget = ch.ClipboardHistoryWidget(store=self.store)
        self.addCleanup(self.widget.shutdown)
        self.addCleanup(self.widget.deleteLater)
        self.clip = QGuiApplication.clipboard()

    def test_banner_shows_the_current_content(self):
        self.clip.setText("横幅内容")
        self.store.capture_now()
        self.widget.refresh()
        text = self.widget.current_banner.text()
        self.assertIn("当前可粘贴", text)
        self.assertIn("横幅内容", text)

    def test_banner_empty_state(self):
        self.clip.clear()
        self.store.current = None
        self.widget.refresh()
        self.assertIn("剪贴板为空", self.widget.current_banner.text())

    def test_current_row_is_specially_marked(self):
        self.clip.setText("标记我")
        self.store.capture_now()
        self.widget.refresh()
        item = self.widget.list.item(0)
        self.assertTrue(item.text().startswith("●"), item.text())
        self.assertTrue(item.font().bold(), "当前那条要加粗")
        self.assertEqual(item.foreground().color().name(),
                         QColor(theme.token("primary")).name(), "当前那条用主色")

    def test_other_rows_have_no_marker(self):
        self.store.add(_text_entry("普通"))
        self.widget.refresh()
        self.assertFalse(self.widget.list.item(0).text().startswith("●"))
        self.assertFalse(self.widget.list.item(0).font().bold())

    def test_marker_follows_the_clipboard(self):
        self.clip.setText("第一条")
        self.store.capture_now()
        self.widget.refresh()
        self.assertTrue(self.widget.list.item(0).text().startswith("●"))
        self.clip.setText("第二条")
        self.store.capture_now()
        self.widget.refresh()
        self.assertTrue(self.widget.list.item(0).text().startswith("●"))
        self.assertIn("第二条", self.widget.list.item(0).text())
        self.assertFalse(self.widget.list.item(1).text().startswith("●"),
                         "旧的那条标记要摘掉")

    def test_banner_click_selects_the_current_row(self):
        self.clip.setText("点我选中")
        self.store.capture_now()
        self.widget.refresh()
        self.widget._on_banner_clicked()
        self.assertEqual(self.widget.selected_entry().text, "点我选中")

    def test_long_clipboard_text_does_not_blow_up_the_window(self):
        """一条超长文本不能把窗口最小宽度顶到屏幕外。

        横幅是 QLabel，sizeHint 就是整段文字宽度 —— 横向策略必须是 Ignored，
        摘要还要截断，否则复制一段长日志就能把窗口撑爆。
        """
        self.clip.setText("长" * 5000)
        self.store.capture_now()
        self.widget.refresh()
        self.assertLessEqual(len(self.widget.current_banner.text()), 80)
        self.assertEqual(self.widget.current_banner.sizePolicy().horizontalPolicy(),
                         QSizePolicy.Ignored)
        self.assertLess(self.widget.minimumSizeHint().width(), 1200)


class TestBatchDelete(_QtCase):
    """★ 需求：可批量选择删除。"""

    def setUp(self):
        super().setUp()
        self.store = ch.ClipboardStore()
        self.widget = ch.ClipboardHistoryWidget(store=self.store)
        self.addCleanup(self.widget.shutdown)
        self.addCleanup(self.widget.deleteLater)

    def _seed(self, count=4):
        for i in range(count):
            self.store.add(_text_entry(f"第{i}条"))

    def test_both_lists_allow_multi_selection(self):
        for listw in (self.widget.list, self.widget.fav_list):
            self.assertEqual(listw.selectionMode(),
                             QAbstractItemView.ExtendedSelection)

    def test_select_all_then_delete(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.selectAll()
        self.assertEqual(len(self.widget.selected_entries()), 4)
        self.assertIn("删除选中（4）", self.widget.del_btn.text())
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.Yes) as ask:
            self.widget._on_delete()
        self.assertIn("4 条", ask.call_args.args[2], "确认框要说明删几条")
        self.assertEqual(self.store.entries, [])

    def test_delete_only_the_selected_ones(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.item(0).setSelected(True)
        self.widget.list.item(2).setSelected(True)
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.Yes):
            self.widget._on_delete()
        self.assertEqual(len(self.store.entries), 2)

    def test_batch_delete_is_persisted(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.selectAll()
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.Yes):
            self.widget._on_delete()
        self.assertEqual(ch.ClipboardStore().entries, [], "批量删除也要落盘")

    def test_cancel_keeps_everything(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.selectAll()
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.No):
            self.widget._on_delete()
        self.assertEqual(len(self.store.entries), 4)

    def test_multi_selection_disables_single_item_actions(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.selectAll()
        self.assertFalse(self.widget.copy_btn.isEnabled())
        self.assertFalse(self.widget.edit_btn.isEnabled())
        self.assertTrue(self.widget.del_btn.isEnabled())
        self.assertIn("已选中 4 条", self.widget.detail_head.text())

    def test_batch_favorite_moves_them_all_right(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.selectAll()
        self.widget._on_toggle_favorite()
        self.assertEqual(self.widget.fav_list.count(), 4)
        self.assertEqual(self.widget.list.count(), 0)

    def test_delete_key_on_list_requests_delete(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.item(0).setSelected(True)
        with mock.patch.object(ch.QMessageBox, "question",
                               return_value=ch.QMessageBox.Yes):
            self.widget.list.deleteRequested.emit()
        self.assertEqual(len(self.store.entries), 3)


class TestEditDialog(_QtCase):

    def test_rejects_blank_text(self):
        dialog = ch.EditTextDialog("内容")
        dialog.editor.setPlainText("   ")
        with mock.patch.object(ch.QMessageBox, "warning") as warn, \
                mock.patch.object(ch.QDialog, "accept") as accept:
            dialog._on_accept()
        self.assertTrue(warn.called)
        self.assertFalse(accept.called)
        self.assertTrue(dialog.warning)
        dialog.deleteLater()

    def test_accepts_text(self):
        dialog = ch.EditTextDialog("内容")
        dialog.editor.setPlainText("改好了")
        with mock.patch.object(ch.QDialog, "accept") as accept:
            dialog._on_accept()
        self.assertTrue(accept.called)
        self.assertEqual(dialog.text(), "改好了")
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# 永久本地存储
# ---------------------------------------------------------------------------
class TestPersistence(_QtCase):
    """★ 需求：所有内容永久保存在本地（<BASE_DIR>/clipboard_data/）。"""

    def _store(self) -> ch.ClipboardStore:
        store = ch.ClipboardStore()
        self.addCleanup(store.stop_watching)
        return store

    def test_data_dir_lives_under_base_dir(self):
        self.assertTrue(ch.clipboard_data_dir().startswith(self._tmp.tmp))
        self.assertEqual(os.path.basename(ch.clipboard_data_dir()),
                         ch.DATA_DIR_NAME)

    def test_text_and_files_survive_a_restart(self):
        store = self._store()
        store.add(_text_entry("永久保存", favorite=True))
        store.add(_files_entry())
        again = self._store()
        self.assertEqual(len(again.entries), 2)
        text = next(e for e in again.entries if e.kind == ch.KIND_TEXT)
        self.assertEqual(text.text, "永久保存")
        self.assertTrue(text.favorite, "收藏标记也要一起存")
        files = next(e for e in again.entries if e.kind == ch.KIND_FILES)
        self.assertEqual(files.files, [r"C:\Windows\notepad.exe"])

    def test_image_survives_a_restart(self):
        store = self._store()
        store.add(_image_entry(0x336699, 6, 4))
        uid = store.entries[0].uid
        png = os.path.join(ch.clipboard_data_dir(), ch.IMAGES_SUBDIR,
                           f"{uid}.png")
        self.assertTrue(os.path.isfile(png), "图片要另存成 png")
        again = self._store()
        self.assertEqual(again.entries[0].kind, ch.KIND_IMAGE)
        self.assertEqual((again.entries[0].image.width(),
                          again.entries[0].image.height()), (6, 4))
        self.assertEqual(again.entries[0].digest(), store.entries[0].digest(),
                         "读回来的像素要和原来一模一样")

    def test_edited_flag_survives(self):
        store = self._store()
        entry = store.add(_text_entry("旧"))
        store.update_text(entry.uid, "新")
        again = self._store()
        self.assertEqual(again.entries[0].text, "新")
        self.assertTrue(again.entries[0].edited)

    def test_delete_and_clear_are_persisted(self):
        store = self._store()
        keep = store.add(_text_entry("留下"))
        drop = store.add(_text_entry("删掉"))
        store.remove(drop.uid)
        self.assertEqual([e.text for e in self._store().entries], ["留下"])
        store.clear()
        self.assertEqual(self._store().entries, [])
        self.assertIsNotNone(keep)

    def test_orphan_image_is_pruned_when_entry_deleted(self):
        store = self._store()
        store.add(_image_entry())
        uid = store.entries[0].uid
        png = os.path.join(ch.clipboard_data_dir(), ch.IMAGES_SUBDIR,
                           f"{uid}.png")
        self.assertTrue(os.path.isfile(png))
        store.remove(uid)
        self.assertFalse(os.path.exists(png), "删记录时孤儿图片要一起清掉")

    def test_write_is_atomic(self):
        store = self._store()
        store.add(_text_entry("甲"))
        self.assertFalse(os.path.exists(store.history_path + ".tmp"))

    def test_history_file_shape(self):
        store = self._store()
        store.add(_text_entry("甲", favorite=True))
        with open(store.history_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], ch.HISTORY_VERSION)
        self.assertEqual(data["entries"][0]["text"], "甲")
        self.assertTrue(data["entries"][0]["favorite"])

    def test_corrupt_history_is_treated_as_empty(self):
        store = self._store()
        store.save()                     # 先落一份，保证目录与文件都在
        with open(store.history_path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        with mock.patch.object(ch.log, "warning"):   # 兜底路径本来就要记一条警告
            self.assertEqual(self._store().entries, [])

    def test_bad_rows_are_skipped(self):
        store = self._store()
        store.save()
        payload = {"version": 1, "entries": [
            {"kind": "text", "text": "好的"},
            {"kind": "text", "text": ""},                    # 空文本 -> 丢
            "不是字典",                                        # 形状不对 -> 丢
            {"kind": "image", "image": "早就没了.png"},        # 图片文件不在 -> 丢
            {"kind": "没见过的类型"},
        ]}
        with open(store.history_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        again = self._store()
        self.assertEqual([e.text for e in again.entries], ["好的"])

    def test_persist_false_never_touches_disk(self):
        store = ch.ClipboardStore(persist=False)
        self.addCleanup(store.stop_watching)
        store.add(_text_entry("只在内存"))
        self.assertEqual(store.entries[0].text, "只在内存")
        self.assertFalse(os.path.exists(ch.clipboard_data_dir()))


# ---------------------------------------------------------------------------
# 功能边界（源码级契约）
# ---------------------------------------------------------------------------
class TestBoundaries(_QtCase):

    def test_history_is_persisted_now(self):
        """边界变更（2026-10-06）：从「刻意不落盘」改成**永久本地存储**。

        钉住这条：模块里必须真的有落盘实现（早期版本明说不许出现 open/json.dump，
        现在反过来——需求要求所有内容永久保存）。
        """
        source = inspect.getsource(ch)
        for needed in ("json.dump", "os.replace", "HISTORY_FILE"):
            self.assertIn(needed, source)

    def test_security_note_is_documented(self):
        """落盘之后必须在文档里写明风险（剪贴板里常有密码、验证码）。"""
        doc = ch.__doc__ or ""
        self.assertIn("安全提示", doc)
        self.assertIn("密码", doc)

    def test_ctypes_call_sets_argtypes(self):
        """工程铁律：ctypes 调 Win32 必须显式设 argtypes/restype。"""
        source = inspect.getsource(ch.clipboard_sequence)
        self.assertIn("argtypes", source)
        self.assertIn("restype", source)

    def test_data_dir_follows_base_dir(self):
        """必须**每次调用时**读 BASE_DIR，否则测试/打包后路径就错了。"""
        source = inspect.getsource(ch.clipboard_data_dir)
        self.assertIn("config_mod.BASE_DIR", source)

    def test_poll_interval_is_cheap(self):
        self.assertGreaterEqual(ch.POLL_MS, 200)

    def test_registered_as_mini_app(self):
        app = mini_apps.find_app("clipboard_history")
        self.assertIsNotNone(app)
        self.assertEqual(app.name, "剪贴板")
        self.assertTrue(app.icon and app.desc)

    def test_caps_are_sane(self):
        self.assertGreater(ch.MAX_ENTRIES, 0)
        self.assertGreater(ch.MAX_TEXT_CHARS, ch.LIST_SUMMARY_CHARS)


if __name__ == "__main__":
    unittest.main()
