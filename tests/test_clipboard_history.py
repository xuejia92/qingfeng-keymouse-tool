# -*- coding: utf-8 -*-
"""「剪贴板」小程序（app/mini_apps/clipboard_history.py）的测试。

覆盖：
- 数据模型：类型判定优先级（文件 > 图片 > 文本）、内容指纹、摘要/体积/时间格式；
- 仓库：倒序入库、同内容去重并提前、上限淘汰但保留收藏、增删改查；
- **真实剪贴板往返**（离屏平台的剪贴板是进程内的，不会动用户的系统剪贴板）：
  文本/图片/文件三类都能读进来，也能写回去；
- 界面：列表顺序、只看收藏、选中态按钮启停、收藏置顶、删除确认、清空保留收藏、
  复制回剪贴板、修改对话框；
- 功能边界（源码级契约）：**不落盘**、兜底轮询遵守 ctypes 铁律。
"""
from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from app import mini_apps  # noqa: E402
from app.mini_apps import clipboard_history as ch  # noqa: E402


def _text_entry(text="内容", **kw) -> ch.ClipEntry:
    return ch.ClipEntry(kind=ch.KIND_TEXT, text=text, **kw)


def _image_entry(color=0x336699, w=6, h=4) -> ch.ClipEntry:
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(color)
    return ch.ClipEntry(kind=ch.KIND_IMAGE, image=image)


def _files_entry(paths=None) -> ch.ClipEntry:
    return ch.ClipEntry(kind=ch.KIND_FILES,
                        files=list(paths or [r"C:\Windows\notepad.exe"]))


class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 离屏剪贴板是进程内共享的，用例之间互相擦干净
        QGuiApplication.clipboard().clear()


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

    def test_toggle_favorite(self):
        entry = self.store.add(_text_entry("甲"))
        self.assertTrue(self.store.toggle_favorite(entry.uid))
        self.assertTrue(entry.favorite)
        self.assertFalse(self.store.toggle_favorite(entry.uid))
        self.assertIsNone(self.store.toggle_favorite("不存在"))

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

    def test_visible_entries_pins_favorites(self):
        self.store.add(_text_entry("甲"))
        middle = self.store.add(_text_entry("乙"))
        self.store.add(_text_entry("丙"))
        self.store.toggle_favorite(middle.uid)
        self.assertEqual([e.text for e in self.store.visible_entries()],
                         ["乙", "丙", "甲"], "收藏置顶，其余保持时间倒序")
        self.assertEqual([e.text for e in
                          self.store.visible_entries(favorites_only=True)], ["乙"])

    def test_default_store_is_shared(self):
        self.assertIs(ch.default_store(), ch.default_store())


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

    def test_favorites_only_filter(self):
        self._seed()
        self.store.toggle_favorite(self.store.entries[0].uid)
        self.widget.fav_only.setChecked(True)
        self.assertEqual(self.widget.list.count(), 1)

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

    def test_detail_preview_for_image_shows_pixmap(self):
        self.store.add(_image_entry())
        self.widget.refresh()
        self.widget.list.setCurrentRow(0)
        self.assertFalse(self.widget.preview.pixmap().isNull())
        self.assertFalse(self.widget.list.item(0).icon().isNull(), "行上要有缩略图")

    def test_toggle_favorite_from_button(self):
        self._seed()
        self.widget.refresh()
        self.widget.list.setCurrentRow(2)          # 最旧的文本
        self.widget._on_toggle_favorite()
        self.assertTrue(self.store.entries[-1].favorite)
        self.assertTrue(self.widget.list.item(0).text().startswith("★"))

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
# 功能边界（源码级契约）
# ---------------------------------------------------------------------------
class TestBoundaries(unittest.TestCase):

    def test_history_never_touches_disk(self):
        """「不落盘」是本工具明说的边界：剪贴板里常有密码、验证码。

        写死一条源码级契约：这个模块不许出现任何写文件的调用。
        """
        source = inspect.getsource(ch)
        for forbidden in ("open(", "json.dump", "json.load", "pickle",
                          "shutil.copy", "Path("):
            self.assertNotIn(forbidden, source, f"剪贴板历史不该落盘：{forbidden}")

    def test_ctypes_call_sets_argtypes(self):
        """工程铁律：ctypes 调 Win32 必须显式设 argtypes/restype。"""
        source = inspect.getsource(ch.clipboard_sequence)
        self.assertIn("argtypes", source)
        self.assertIn("restype", source)

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
