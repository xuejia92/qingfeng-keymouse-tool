"""app/keymap.py 的热键解析测试。

parse_combo 的返回值直接决定热键能不能注册上：拆错了就是用户按了没反应，
而且没有任何报错，属于最难自查的一类问题。
"""
from __future__ import annotations

import unittest

from pynput.keyboard import Key

from app.keymap import hotkey_display, is_modifier_name, parse_combo, to_pynput_key


class TestIsModifier(unittest.TestCase):
    def test_modifiers(self):
        for n in ("ctrl", "alt", "shift", "win", "lctrl", "rshift", "lwin"):
            self.assertTrue(is_modifier_name(n), n)

    def test_not_modifiers(self):
        for n in ("a", "f1", "space", "ctrlx", "", "CTRL "):
            self.assertFalse(is_modifier_name(n), n)

    def test_case_sensitive(self):
        """调用方负责先 lower，这里保持严格匹配。"""
        self.assertFalse(is_modifier_name("CTRL"))


class TestParseCombo(unittest.TestCase):
    def test_single_key(self):
        self.assertEqual(parse_combo("c"), ([], "c"))
        self.assertEqual(parse_combo("f6"), ([], "f6"))

    def test_with_modifiers(self):
        self.assertEqual(parse_combo("ctrl+c"), (["ctrl"], "c"))
        self.assertEqual(parse_combo("ctrl+alt+shift+win+q"),
                         (["ctrl", "alt", "shift", "win"], "q"))

    def test_normalizes_case_and_space(self):
        self.assertEqual(parse_combo("  CTRL + C "), (["ctrl"], "c"))

    def test_order_preserved(self):
        self.assertEqual(parse_combo("shift+ctrl+a"), (["shift", "ctrl"], "a"))

    def test_rejects_no_main_key(self):
        with self.assertRaises(ValueError):
            parse_combo("ctrl+alt")

    def test_rejects_two_main_keys(self):
        with self.assertRaises(ValueError):
            parse_combo("a+b")

    def test_rejects_empty(self):
        for bad in ("", "   ", None, "+++"):
            with self.assertRaises(ValueError):
                parse_combo(bad)


class TestToPynputKey(unittest.TestCase):
    def test_special_names(self):
        self.assertIs(to_pynput_key("space"), Key.space)
        self.assertIs(to_pynput_key("esc"), Key.esc)
        self.assertIs(to_pynput_key("win"), Key.cmd)
        self.assertIs(to_pynput_key("pageup"), Key.page_up)

    def test_function_keys(self):
        for i in (1, 9, 12, 24):
            self.assertIs(to_pynput_key(f"f{i}"), getattr(Key, f"f{i}"))

    def test_out_of_range_function_key_rejected(self):
        with self.assertRaises(ValueError):
            to_pynput_key("f25")

    def test_single_char_is_char(self):
        self.assertEqual(to_pynput_key("A"), "a")

    def test_unknown_rejected(self):
        with self.assertRaises(ValueError):
            to_pynput_key("不存在的键")


class TestHotkeyDisplay(unittest.TestCase):
    def test_display(self):
        self.assertEqual(hotkey_display("ctrl+alt+q"), "Ctrl+Alt+Q")
        self.assertEqual(hotkey_display("esc"), "Esc")
        self.assertEqual(hotkey_display("pageup"), "PageUp")
        self.assertEqual(hotkey_display("up"), "↑")

    def test_empty(self):
        self.assertEqual(hotkey_display(""), "")
        self.assertEqual(hotkey_display(None), "")


class TestRoundTrip(unittest.TestCase):
    """解析出来的键名必须能被 to_pynput_key 接受，否则热键注册会在更后面炸掉。"""

    def test_every_parsed_key_is_playable(self):
        for combo in ("ctrl+c", "alt+f4", "shift+f1", "ctrl+shift+esc", "a", "f12"):
            mods, main = parse_combo(combo)
            for m in mods:
                to_pynput_key(m)
            to_pynput_key(main)


if __name__ == "__main__":
    unittest.main()
