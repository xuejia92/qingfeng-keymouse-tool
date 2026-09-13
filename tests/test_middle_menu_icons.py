"""中键菜单预设图标注册表（app/ui/middle_menu_icons.py）的测试。

图标在配置里只以 key 的形式存储，显示时才映射成 emoji 画成 QIcon。这里钉住：
- 注册表本身自洽（key 唯一、非空 key 都有 emoji 与中文名、第一项是「无图标」）；
- 未知/空 key 一律当无图标处理，不能抛异常；
- 占位图标必须全透明（「不设置图标就留空」这条需求的实现基础）。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui.middle_menu_icons import (ICON_SIZE, PRESET_ICONS, blank_icon,
                                      emoji_for, icon_for, label_for)


class TestPresetRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_first_entry_is_no_icon(self):
        """下拉第一项必须是「无图标」，否则用户没法把已有图标去掉。"""
        key, emoji, name = PRESET_ICONS[0]
        self.assertEqual(key, "")
        self.assertEqual(emoji, "")
        self.assertTrue(name)

    def test_keys_unique_and_nonempty_entries_complete(self):
        keys = [k for k, _, _ in PRESET_ICONS]
        self.assertEqual(len(keys), len(set(keys)), "图标 key 不能重复")
        for key, emoji, name in PRESET_ICONS:
            if not key:
                continue
            self.assertTrue(emoji, f"{key} 缺少 emoji")
            self.assertTrue(name, f"{key} 缺少中文名")

    def test_registry_is_non_trivial(self):
        self.assertGreaterEqual(len(PRESET_ICONS) - 1, 12)

    def test_legacy_keys_are_never_removed(self):
        """已发布过的 key 不能删改——用户的 config.json 里存的就是它们。

        删掉一个 key 会让老配置里的图标静默失效（退回「无图标」），
        所以只允许新增。
        """
        legacy = {
            "play", "rocket", "star", "gear", "tools", "folder", "doc", "globe",
            "mail", "chat", "image", "camera", "keyboard", "mouse", "clock",
            "bell", "lock", "search", "refresh", "check", "warning", "fire",
            "bolt", "clip", "link",
        }
        keys = {k for k, _, _ in PRESET_ICONS}
        self.assertTrue(legacy <= keys, f"丢失了已发布的 key: {sorted(legacy - keys)}")

    def test_emoji_are_not_reused(self):
        """同一个 emoji 被两个 key 复用会让用户分不清，也不该出现。"""
        emojis = [e for k, e, _ in PRESET_ICONS if k]
        dup = {e for e in emojis if emojis.count(e) > 1}
        self.assertEqual(dup, set(), f"emoji 重复: {sorted(dup)}")


class TestLookups(unittest.TestCase):
    def test_emoji_and_label_for_known_key(self):
        self.assertTrue(emoji_for("rocket"))
        self.assertEqual(label_for("rocket"), "火箭")

    def test_unknown_and_empty_key_return_blank(self):
        for bad in ("", "   ", "no-such-icon", None):
            self.assertEqual(emoji_for(bad), "")
            self.assertEqual(label_for(bad), "")
            self.assertTrue(icon_for(bad).isNull(), f"{bad!r} 应视为无图标")


class TestIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_every_preset_renders_a_usable_icon(self):
        """每个非空 key 都要能画出图标（防止 emoji 写错/漏映射）。"""
        for key, emoji, name in PRESET_ICONS:
            if not key:
                continue
            icon = icon_for(key)
            self.assertFalse(icon.isNull(), f"{key}({emoji}) 未渲染出图标")
            pixmap = icon.pixmap(ICON_SIZE, ICON_SIZE)
            self.assertFalse(pixmap.isNull(), f"{key} pixmap 为空")
            self.assertEqual(pixmap.size().width(), ICON_SIZE)

    def test_icon_is_cached(self):
        self.assertEqual(icon_for("rocket").cacheKey(), icon_for("rocket").cacheKey())

    def test_blank_icon_is_fully_transparent(self):
        """「不设置图标就留空」靠这张全透明占位图实现，不能有半点可见像素。

        注意不能改用「画一个空格字符」的写法：空格在某些字体回退下会画出豆腐块。
        """
        image = blank_icon().pixmap(ICON_SIZE, ICON_SIZE).toImage()
        self.assertEqual(image.width(), ICON_SIZE)
        for x in range(image.width()):
            for y in range(image.height()):
                self.assertEqual(image.pixelColor(x, y).alpha(), 0,
                                 f"占位图标在 ({x},{y}) 有可见像素")

    def test_blank_icon_is_reused(self):
        self.assertEqual(blank_icon().cacheKey(), blank_icon().cacheKey())


if __name__ == "__main__":
    unittest.main()
