# -*- coding: utf-8 -*-
"""「启动项」小程序（app/mini_apps/startup_items.py）的测试。

⚠️ **绝不碰真实注册表**：所有注册表读写都收口在模块的 `_reg_*` 六个函数里，
本文件把这一层整体替换掉；文件的增删用临时目录里**自己创建**的文件。
"""
from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import mini_apps  # noqa: E402
from app.mini_apps import startup_items as si  # noqa: E402

BLOB_ENABLED = bytes([0x02] + [0] * 11)
BLOB_DISABLED = bytes([0x03] + [0] * 11)


def _item(name="Demo", command='"C:\\Tools\\demo.exe"', **kw) -> si.StartupItem:
    return si.StartupItem(name=name, command=command, **kw)


# ---------------------------------------------------------------------------
# 命令行解析 / 校验（纯函数）
# ---------------------------------------------------------------------------
class TestCommandParsing(unittest.TestCase):

    def test_quoted_command(self):
        self.assertEqual(
            si._exe_from_command('"C:\\Program Files\\A\\a.exe" --min'),
            "C:\\Program Files\\A\\a.exe")

    def test_unquoted_with_spaces_and_args(self):
        """★ 不带引号但路径含空格：不能按第一个空格切（实测踩过的坑）。"""
        self.assertEqual(
            si._exe_from_command(r"D:\Program Files\HongAnapp\HongAnapp.exe"),
            r"D:\Program Files\HongAnapp\HongAnapp.exe")
        self.assertEqual(
            si._exe_from_command(r"C:\a b\Thunder.exe -silent -flag"),
            r"C:\a b\Thunder.exe")

    def test_env_var_path_kept(self):
        self.assertEqual(si._exe_from_command(r"%windir%\system32\x.exe"),
                         r"%windir%\system32\x.exe")

    def test_plain_command(self):
        self.assertEqual(si._exe_from_command("cmd.exe /c echo hi"), "cmd.exe")

    def test_non_program_command_returns_empty(self):
        for bad in ("", "   ", "not-a-path", "#$%^"):
            self.assertEqual(si._exe_from_command(bad), "", bad)

    def test_build_command_quotes_and_appends_args(self):
        self.assertEqual(si.build_command(r"C:\a b\t.exe", "--x"),
                         '"C:\\a b\\t.exe" --x')
        self.assertEqual(si.build_command(r"C:\a b\t.exe"), '"C:\\a b\\t.exe"')
        self.assertEqual(si.build_command('"C:\\t.exe"', "  "), '"C:\\t.exe"')
        self.assertEqual(si.build_command("", "--x"), "")


class TestValidation(unittest.TestCase):

    def test_validate_add(self):
        self.assertTrue(si.validate_add("", "x"))
        self.assertIn("反斜杠", si.validate_add("a\\b", "x"))
        self.assertTrue(si.validate_add("a", ""))
        self.assertEqual(si.validate_add("a", "x"), "")
        self.assertEqual(si.validate_add("  a  ", "  x  "), "")

    def test_name_exists(self):
        with mock.patch.object(si, "_reg_read_values",
                               return_value={"A": "cmd"}) as reader:
            self.assertTrue(si.name_exists(si.SCOPE_USER, "A"))
            self.assertFalse(si.name_exists(si.SCOPE_USER, "B"))
            # 未知范围回落到 HKCU（读取照样发生，不会抛异常）
            self.assertTrue(si.name_exists("不存在的范围", "A"))
        self.assertTrue(reader.called)


class TestEnabledFlag(unittest.TestCase):

    def test_missing_marker_means_enabled(self):
        self.assertTrue(si._is_enabled(None))
        self.assertTrue(si._is_enabled(b""))

    def test_odd_first_byte_means_disabled(self):
        self.assertTrue(si._is_enabled(BLOB_ENABLED))
        self.assertTrue(si._is_enabled(bytes([0x06] + [0] * 11)))
        self.assertFalse(si._is_enabled(BLOB_DISABLED))
        self.assertFalse(si._is_enabled(bytes([0x07] + [0] * 11)))


class TestLnkPeek(unittest.TestCase):
    """从 .lnk 字节里尽力猜目标路径（只读探测）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qf_startup_test_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)   # 自己建的临时目录

    def test_finds_utf16_target(self):
        path = os.path.join(self.dir, "My.lnk")
        payload = ("\x00\x01" + r"C:\Tools\myapp.exe" + "\x00\x00").encode("utf-16-le")
        with open(path, "wb") as handle:
            handle.write(payload)
        self.assertEqual(si._peek_lnk_target(path), r"C:\Tools\myapp.exe")

    def test_missing_file_returns_empty(self):
        self.assertEqual(si._peek_lnk_target(os.path.join(self.dir, "无.lnk")), "")

    def test_garbage_returns_empty(self):
        path = os.path.join(self.dir, "junk.lnk")
        with open(path, "wb") as handle:
            handle.write(b"\x01\x02\x03\x04not a shortcut")
        self.assertEqual(si._peek_lnk_target(path), "")


# ---------------------------------------------------------------------------
# 列举
# ---------------------------------------------------------------------------
class TestListItems(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qf_startup_folder_")
        # 启动文件夹里放：一个快捷方式、一个 bat、一个 desktop.ini、一个子目录
        self.link = os.path.join(self.dir, "Alpha.lnk")
        with open(self.link, "wb") as handle:
            handle.write(("\x00" + r"D:\Apps\alpha.exe").encode("utf-16-le"))
        with open(os.path.join(self.dir, "beta.bat"), "w",
                  encoding="utf-8") as handle:
            handle.write("@echo off\n")
        with open(os.path.join(self.dir, "desktop.ini"), "w",
                  encoding="utf-8") as handle:
            handle.write("[.ShellClassInfo]\n")
        os.makedirs(os.path.join(self.dir, "subfolder"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, *, approved_run=None, approved_folder=None):
        values = {
            "HKCU": {"Zed": "z.exe", "Alpha": '"C:\\a.exe" -x'},
            "HKLM": {"System": "sys.exe"},
        }
        folders = {si.SCOPE_USER: self.dir, si.SCOPE_MACHINE: ""}
        with mock.patch.object(si, "_reg_read_values",
                               side_effect=lambda root, path: values.get(root, {})), \
             mock.patch.object(si, "_reg_read_binary",
                               side_effect=lambda root, path:
                               (approved_run or {}) if path == si.APPROVED_RUN_KEY
                               else (approved_folder or {})), \
             mock.patch.object(si, "_startup_folder",
                               side_effect=lambda scope: folders[scope]):
            return si.list_items()

    def test_lists_registry_and_folder_items(self):
        keys = [(i.source, i.name) for i in self._run()]
        for expected in ((si.SOURCE_REG, "Alpha"), (si.SOURCE_REG, "Zed"),
                         (si.SOURCE_REG, "System"),
                         (si.SOURCE_FOLDER, "Alpha"), (si.SOURCE_FOLDER, "beta")):
            self.assertIn(expected, keys)

    def test_registry_items_are_sorted_and_scoped(self):
        items = [i for i in self._run() if i.source == si.SOURCE_REG]
        self.assertEqual([i.name for i in items if i.scope == si.SCOPE_USER],
                         ["Alpha", "Zed"])
        machine = [i for i in items if i.scope == si.SCOPE_MACHINE]
        self.assertEqual([i.name for i in machine], ["System"])

    def test_desktop_ini_and_directories_ignored(self):
        names = [i.name for i in self._run() if i.source == si.SOURCE_FOLDER]
        self.assertNotIn("desktop", names)
        self.assertNotIn("subfolder", names)

    def test_folder_item_gets_target_and_path(self):
        item = next(i for i in self._run()
                    if i.source == si.SOURCE_FOLDER and i.name == "Alpha")
        self.assertEqual(item.path, self.link)
        self.assertEqual(item.target_path(), r"D:\Apps\alpha.exe")

    def test_enabled_flags_from_startupapproved(self):
        items = self._run(approved_run={"Alpha": BLOB_DISABLED},
                          approved_folder={"beta.bat": BLOB_DISABLED})
        by_key = {(i.source, i.scope, i.name): i.enabled for i in items}
        self.assertFalse(by_key[(si.SOURCE_REG, si.SCOPE_USER, "Alpha")])
        self.assertTrue(by_key[(si.SOURCE_REG, si.SCOPE_USER, "Zed")])
        self.assertFalse(by_key[(si.SOURCE_FOLDER, si.SCOPE_USER, "beta")])

    def test_missing_folder_is_tolerated(self):
        with mock.patch.object(si, "_reg_read_values", return_value={}), \
             mock.patch.object(si, "_reg_read_binary", return_value={}), \
             mock.patch.object(si, "_startup_folder",
                               return_value=os.path.join(self.dir, "不存在")):
            self.assertEqual(si.list_items(), [])


# ---------------------------------------------------------------------------
# 修改：启用 / 禁用 / 删除 / 添加
# ---------------------------------------------------------------------------
class TestMutations(unittest.TestCase):

    def test_set_enabled_registry(self):
        item = _item("Demo", scope=si.SCOPE_USER)
        with mock.patch.object(si, "_reg_write_binary") as writer:
            si.set_enabled(item, False)
        writer.assert_called_once_with("HKCU", si.APPROVED_RUN_KEY, "Demo",
                                       BLOB_DISABLED)
        with mock.patch.object(si, "_reg_write_binary") as writer:
            si.set_enabled(item, True)
        writer.assert_called_once_with("HKCU", si.APPROVED_RUN_KEY, "Demo",
                                       BLOB_ENABLED)

    def test_set_enabled_folder_uses_filename(self):
        item = _item("Alpha", source=si.SOURCE_FOLDER,
                     path=r"C:\Startup\Alpha.lnk")
        with mock.patch.object(si, "_reg_write_binary") as writer:
            si.set_enabled(item, False)
        writer.assert_called_once_with("HKCU", si.APPROVED_FOLDER_KEY,
                                       "Alpha.lnk", BLOB_DISABLED)

    def test_set_enabled_machine_uses_hklm(self):
        item = _item("Sys", scope=si.SCOPE_MACHINE)
        with mock.patch.object(si, "_reg_write_binary") as writer:
            si.set_enabled(item, True)
        writer.assert_called_once_with("HKLM", si.APPROVED_RUN_KEY, "Sys",
                                       BLOB_ENABLED)

    def test_delete_registry_item_also_clears_marker(self):
        with mock.patch.object(si, "_reg_delete_value") as del_value, \
             mock.patch.object(si, "_reg_delete_binary") as del_binary:
            si.delete_item(_item("Demo"))
        del_value.assert_called_once_with("HKCU", si.RUN_KEY, "Demo")
        del_binary.assert_called_once_with("HKCU", si.APPROVED_RUN_KEY, "Demo")

    def test_delete_folder_item_goes_to_recycle_bin(self):
        item = _item("Alpha", source=si.SOURCE_FOLDER,
                     path=r"C:\Startup\Alpha.lnk")
        with mock.patch.object(si, "_recycle") as recycle, \
             mock.patch.object(si, "_reg_delete_binary") as del_binary:
            si.delete_item(item)
        recycle.assert_called_once_with(r"C:\Startup\Alpha.lnk")
        del_binary.assert_called_once_with("HKCU", si.APPROVED_FOLDER_KEY,
                                           "Alpha.lnk")

    def test_add_writes_value_and_clears_disable_marker(self):
        with mock.patch.object(si, "_reg_write_value") as write_value, \
             mock.patch.object(si, "_reg_write_binary") as write_binary:
            si.add_registry_item("Demo", '"C:\\d.exe"', si.SCOPE_MACHINE)
        write_value.assert_called_once_with("HKLM", si.RUN_KEY, "Demo",
                                            '"C:\\d.exe"')
        write_binary.assert_called_once_with("HKLM", si.APPROVED_RUN_KEY,
                                             "Demo", BLOB_ENABLED)

    def test_writable_depends_on_admin_for_machine_scope(self):
        with mock.patch.object(si, "is_admin", return_value=False):
            self.assertTrue(_item(scope=si.SCOPE_USER).writable)
            self.assertFalse(_item(scope=si.SCOPE_MACHINE).writable)
        with mock.patch.object(si, "is_admin", return_value=True):
            self.assertTrue(_item(scope=si.SCOPE_MACHINE).writable)

    def test_location_text(self):
        self.assertEqual(_item(scope=si.SCOPE_USER).location, "注册表 · 当前用户")
        self.assertEqual(_item(source=si.SOURCE_FOLDER,
                               scope=si.SCOPE_MACHINE).location,
                         "启动文件夹 · 所有用户")


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])


DEMO_ITEMS = [
    si.StartupItem(name="Alpha", command='"C:\\a.exe" -x',
                   source=si.SOURCE_REG, scope=si.SCOPE_USER, enabled=True),
    si.StartupItem(name="Beta", command="C:\\b.exe", source=si.SOURCE_REG,
                   scope=si.SCOPE_USER, enabled=False),
]


class TestStartupManagerWidget(_QtCase):

    def _make(self, items=None):
        items = DEMO_ITEMS if items is None else items
        with mock.patch.object(si, "list_items", return_value=list(items)):
            return si.StartupManagerWidget()

    def _clear_selection(self, widget):
        widget.table.clearSelection()
        widget.table.setCurrentCell(-1, -1)
        widget._sync_buttons()

    def test_rows_and_columns(self):
        widget = self._make()
        self.assertEqual(widget.table.rowCount(), 2)
        self.assertEqual(widget.table.columnCount(), 4)
        self.assertEqual(widget.table.item(0, 1).text(), "Alpha")
        self.assertEqual(widget.table.item(0, 0).text(), "已启用")
        self.assertEqual(widget.table.item(1, 0).text(), "已禁用")
        self.assertEqual(widget.table.item(0, 2).text(), "注册表 · 当前用户")
        self.assertEqual(widget.table.item(0, 3).text(), '"C:\\a.exe" -x')

    def test_summary_counts(self):
        widget = self._make()
        self.assertIn("共 2 项", widget.summary.text())
        self.assertIn("已启用 1", widget.summary.text())
        self.assertIn("已禁用 1", widget.summary.text())

    def test_buttons_disabled_without_selection(self):
        widget = self._make()
        self._clear_selection(widget)
        for btn in (widget.enable_btn, widget.disable_btn, widget.delete_btn,
                    widget.open_btn):
            self.assertFalse(btn.isEnabled(), btn.text())

    def test_enable_only_for_disabled_and_vice_versa(self):
        widget = self._make()
        widget.table.selectRow(0)        # Alpha：已启用
        self.assertFalse(widget.enable_btn.isEnabled())
        self.assertTrue(widget.disable_btn.isEnabled())
        widget.table.selectRow(1)        # Beta：已禁用
        self.assertTrue(widget.enable_btn.isEnabled())
        self.assertFalse(widget.disable_btn.isEnabled())

    def test_machine_scope_without_admin_disables_mutations(self):
        items = [si.StartupItem(name="Sys", command="sys.exe",
                                scope=si.SCOPE_MACHINE, enabled=True)]
        widget = self._make(items)
        with mock.patch.object(si, "is_admin", return_value=False):
            widget.table.selectRow(0)
            for btn in (widget.enable_btn, widget.disable_btn,
                        widget.delete_btn):
                self.assertFalse(btn.isEnabled(), btn.text())
                self.assertIn("管理员", btn.toolTip())
            self.assertTrue(widget.open_btn.isEnabled(), "打开位置是只读操作")

    def test_toggling_calls_module_and_refreshes(self):
        widget = self._make()
        widget.table.selectRow(1)        # Beta（已禁用）-> 启用
        with mock.patch.object(si, "set_enabled") as setter, \
             mock.patch.object(si, "list_items", return_value=list(DEMO_ITEMS)):
            widget._set_enabled(True)
        setter.assert_called_once()
        self.assertTrue(setter.call_args.args[1], "应当以「启用」调用")
        self.assertEqual(setter.call_args.args[0].name, "Beta")

    def test_toggle_failure_shows_message(self):
        widget = self._make()
        widget.table.selectRow(1)
        with mock.patch.object(si, "set_enabled",
                               side_effect=PermissionError("拒绝访问")), \
             mock.patch.object(si.QMessageBox, "warning") as warn:
            widget._set_enabled(True)
        self.assertTrue(warn.called)

    def test_delete_asks_before_removing(self):
        widget = self._make()
        widget.table.selectRow(1)
        with mock.patch.object(si.QMessageBox, "question",
                               return_value=si.QMessageBox.No) as ask, \
             mock.patch.object(si, "delete_item") as deleter:
            widget._on_delete()
        self.assertTrue(ask.called)
        self.assertFalse(deleter.called, "选「否」时不该删")

    def test_delete_confirmed_removes_item(self):
        widget = self._make()
        widget.table.selectRow(1)
        with mock.patch.object(si.QMessageBox, "question",
                               return_value=si.QMessageBox.Yes), \
             mock.patch.object(si, "delete_item") as deleter, \
             mock.patch.object(si, "list_items", return_value=[DEMO_ITEMS[0]]):
            widget._on_delete()
        deleter.assert_called_once()

    def test_refresh_keeps_selected_row(self):
        widget = self._make()
        widget.table.selectRow(1)
        with mock.patch.object(si, "list_items", return_value=list(DEMO_ITEMS)):
            widget.refresh()
        self.assertEqual(widget.table.currentRow(), 1)

    def test_open_without_selection_is_noop(self):
        widget = self._make()
        self._clear_selection(widget)
        with mock.patch.object(si.QMessageBox, "information") as info:
            widget._on_open()
        self.assertFalse(info.called)


class TestAddStartupDialog(_QtCase):

    def _dialog(self):
        dialog = si.AddStartupDialog()
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_name_defaults_to_program_basename(self):
        dialog = self._dialog()
        dialog.program_edit.setText(r"C:\Tools\MyApp.exe")
        self.assertEqual(dialog.name(), "MyApp")
        dialog.name_edit.setText("自定义名")
        self.assertEqual(dialog.name(), "自定义名")

    def test_command_quotes_and_appends_args(self):
        dialog = self._dialog()
        dialog.program_edit.setText(r"C:\a b\app.exe")
        dialog.args_edit.setText("--silent")
        self.assertEqual(dialog.command(), '"C:\\a b\\app.exe" --silent')

    def test_scope_defaults_to_current_user_and_tip_follows(self):
        dialog = self._dialog()
        self.assertEqual(dialog.scope(), si.SCOPE_USER)
        self.assertIn("HKEY_CURRENT_USER", dialog.tip.text())
        dialog.scope_box.setCurrentIndex(1)
        self.assertEqual(dialog.scope(), si.SCOPE_MACHINE)
        self.assertIn("HKEY_LOCAL_MACHINE", dialog.tip.text())

    def test_invalid_input_blocks_accept(self):
        dialog = self._dialog()
        with mock.patch.object(si.QMessageBox, "warning") as warn, \
             mock.patch.object(si.QDialog, "accept") as accept:
            dialog._on_accept()               # 什么都没填
        self.assertTrue(warn.called)
        self.assertFalse(accept.called)
        self.assertTrue(dialog.warning)

    def test_valid_input_accepts(self):
        dialog = self._dialog()
        dialog.program_edit.setText(r"C:\t.exe")
        with mock.patch.object(si.QDialog, "accept") as accept:
            dialog._on_accept()
        self.assertTrue(accept.called)
        self.assertEqual(dialog.warning, "")


# ---------------------------------------------------------------------------
# 安全约定（源码级）
# ---------------------------------------------------------------------------
class TestSafetyContracts(unittest.TestCase):

    def test_winreg_is_imported_lazily(self):
        """winreg 只能在函数里延迟导入：顶层导入会让非 Windows 环境直接崩，
        也让单测没法在不碰真实注册表的前提下 import 本模块。"""
        src = inspect.getsource(si)
        top_level = [line for line in src.splitlines()
                     if line.startswith(("import winreg", "from winreg"))]
        self.assertEqual(top_level, [], f"winreg 不能顶层导入：{top_level}")

    def test_registered_as_mini_app(self):
        app = mini_apps.find_app("startup_items")
        self.assertIsNotNone(app)
        self.assertEqual(app.name, "启动项")
        self.assertTrue(app.icon and app.desc)

    def test_folder_delete_uses_recycle_bin(self):
        """启动文件夹里的快捷方式是用户的文件：删除必须走回收站，不能直接删。"""
        self.assertIn("_recycle", inspect.getsource(si.delete_item))
        self.assertIn("FOF_ALLOWUNDO", inspect.getsource(si._recycle))

    def test_win32_calls_set_argtypes(self):
        """项目铁律：ctypes 调 Win32 必须显式设 argtypes/restype。"""
        src = inspect.getsource(si._recycle)
        self.assertIn("argtypes", src)
        self.assertIn("restype", src)

    def test_every_backend_is_mockable(self):
        """接缝要齐：六个 _reg_* + _startup_folder + _recycle。"""
        for name in ("_reg_read_values", "_reg_read_binary", "_reg_write_value",
                     "_reg_write_binary", "_reg_delete_value",
                     "_reg_delete_binary", "_startup_folder", "_recycle"):
            self.assertTrue(callable(getattr(si, name)), name)


if __name__ == "__main__":
    unittest.main()
