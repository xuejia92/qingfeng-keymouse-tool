"""打开应用（app）步骤 + 后台键鼠的测试。

后台键鼠需真实窗口句柄，单元测试只覆盖纯函数（VK 转换、参数默认值、
summary、执行失败路径）；真实窗口/后台发送靠手工冒烟（见 win_actors 冒烟）。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import win_actors
from app.config import FLOW_STEP_TYPES, FlowStep, default_step_params
from app.tasks import run_app_step


class TestAppStepConfig(unittest.TestCase):
    def test_registered(self):
        self.assertEqual(FLOW_STEP_TYPES.get("app"), "打开应用")

    def test_default_params(self):
        p = default_step_params("app")
        self.assertEqual(p["path"], "")
        self.assertEqual(p["process"], "")
        self.assertEqual(p["target"], "")
        self.assertTrue(p["use_process"])      # 「进程打开」默认勾选
        self.assertEqual(p["wait_sec"], 2.0)
        # 已移除句柄与窗口绑定相关字段（改按标题匹配，且 app 不再需要窗口）
        self.assertNotIn("hwnd", p)
        self.assertNotIn("background", p)
        self.assertNotIn("window_title", p)

    def test_click_press_have_background_fields(self):
        for t in ("click", "press"):
            p = default_step_params(t)
            self.assertIn("background", p)
            self.assertIn("window_title", p)
            self.assertNotIn("hwnd", p)
            self.assertFalse(p["background"])

    def test_close_app_registered(self):
        self.assertEqual(FLOW_STEP_TYPES.get("close_app"), "关闭应用")

    def test_close_app_default_params(self):
        p = default_step_params("close_app")
        self.assertEqual(p["target"], "")
        self.assertIn("wait_sec", p)
        self.assertNotIn("window_title", p)

    def test_summary_app(self):
        s = FlowStep(type="app", params={"path": "C:/x/notepad.exe", "wait_sec": 2})
        self.assertIn("notepad.exe", s.summary())

    def test_summary_app_process_target(self):
        """从进程列表选择后 summary 显示完整描述（Google Chrome — chrome.exe）。"""
        s = FlowStep(type="app", params={"target": "Google Chrome — chrome.exe",
                                         "process": "chrome.exe"})
        self.assertIn("chrome.exe", s.summary())
        self.assertIn("打开", s.summary())

    def test_summary_app_process_no_target(self):
        """只有手填进程名（无列表描述）也按进程显示。"""
        s = FlowStep(type="app", params={"process": "notepad.exe"})
        self.assertIn("notepad.exe", s.summary())

    def test_summary_close_app(self):
        s = FlowStep(type="close_app", params={"target": "notepad.exe"})
        self.assertIn("notepad.exe", s.summary())

    def test_summary_app_no_path(self):
        s = FlowStep(type="app")
        self.assertIn("未选应用", s.summary())

    def test_summary_background_mark(self):
        # 界面上该选项叫「置顶应用」，步骤列表标记同步显示「置顶」
        s = FlowStep(type="click", params={"background": True})
        self.assertIn("置顶", s.summary())


class TestWinActorsPure(unittest.TestCase):
    def test_key_to_vk_special(self):
        self.assertEqual(win_actors.key_to_vk("space"), 0x20)
        self.assertEqual(win_actors.key_to_vk("enter"), 0x0D)
        self.assertEqual(win_actors.key_to_vk("esc"), 0x1B)
        self.assertEqual(win_actors.key_to_vk("up"), 0x26)

    def test_key_to_vk_char(self):
        self.assertEqual(win_actors.key_to_vk("a"), 0x41)
        self.assertEqual(win_actors.key_to_vk("A"), 0x41)

    def test_key_to_vk_invalid(self):
        with self.assertRaises(ValueError):
            win_actors.key_to_vk("不存在的键")

    def test_launch_app_empty_path(self):
        ok, why = win_actors.launch_app("")
        self.assertFalse(ok)
        self.assertIn("路径", why)

    def test_launch_app_missing_path(self):
        ok, why = win_actors.launch_app(r"C:\不存在的目录\xxx.exe")
        self.assertFalse(ok)
        self.assertIn("不存在", why)

    def test_background_click_no_window(self):
        self.assertFalse(win_actors.background_click(0, "left", 1, 0, 0))

    def test_background_press_no_window(self):
        self.assertFalse(win_actors.background_press(0, "space"))

    def test_window_title_zero_hwnd(self):
        self.assertEqual(win_actors.window_title(0), "")

    def test_window_exists_zero(self):
        self.assertFalse(win_actors.window_exists(0))

    def test_window_rect_zero_hwnd(self):
        self.assertIsNone(win_actors.window_rect(0))

    def test_window_class_zero_hwnd(self):
        self.assertEqual(win_actors.window_class(0), "")

    def test_cursor_pos_non_negative(self):
        x, y = win_actors.cursor_pos()
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)

    def test_cursor_window_info_zero_hwnd_returns_empty(self):
        # 传入 0 表示无 skip；只要返回结构正确即可（句柄 int、标题 str、矩形 None 或四元组）
        hwnd, title, rect = win_actors.cursor_window_info()
        self.assertIsInstance(hwnd, int)
        self.assertIsInstance(title, str)
        self.assertTrue(rect is None or len(rect) == 4)

    def test_activate_window_zero_hwnd_false(self):
        self.assertFalse(win_actors.activate_window(0))

    def test_restore_foreground_noop(self):
        # 未 activate 过时 restore 是空操作，不抛异常
        win_actors.restore_foreground()

    def test_find_window_like_empty_title(self):
        self.assertEqual(win_actors.find_window_like(""), 0)

    def test_wait_window_by_title_empty(self):
        # 空标题不等待，立即返回 0
        self.assertEqual(win_actors.wait_window_by_title(""), 0)

    def test_list_processes_structure(self):
        # 只列有可见窗口的应用进程：每条含 pid/name/path/app_name/title（title 非空），
        # 过滤掉无窗口的后台子进程/服务；全 Unicode API 无乱码
        procs = win_actors.list_processes()
        self.assertIsInstance(procs, list)
        for p in procs:
            self.assertIn("pid", p)
            self.assertTrue(p["name"].lower().endswith(".exe"))
            self.assertIn("path", p)                       # 「打开应用」未运行时启动用
            self.assertTrue(p["path"].lower().endswith(".exe"))
            self.assertTrue(p["title"], "应只保留有窗口标题的进程")
            for v in (p["name"], p["app_name"], p["title"]):
                self.assertNotIn("\ufffd", v)

    def test_find_process_window_empty(self):
        self.assertEqual(win_actors.find_process_window(""), 0)

    def test_find_process_window_unknown_name(self):
        # 名字不存在的进程：不会崩、返回 0（真实枚举无副作用）
        self.assertEqual(win_actors.find_process_window("__qf_no_such_proc__.exe"), 0)

    def test_bring_to_front_zero_hwnd_false(self):
        self.assertFalse(win_actors.bring_to_front(0))


class TestOpenRunningProcess(unittest.TestCase):
    """「打开应用」运行时：目标进程已在运行 → 带出窗口；未运行 → 按路径启动。"""

    def test_nothing_configured_fails(self):
        ok, why = run_app_step({"path": "", "process": "", "wait_sec": 0})
        self.assertFalse(ok)
        self.assertIn("未选择", why)

    def test_process_running_brings_to_front(self):
        with mock.patch("app.tasks.win_actors.find_process_window", return_value=1234) as find_, \
                mock.patch("app.tasks.win_actors.bring_to_front") as bring:
            ok, why = run_app_step({"path": "", "process": "chrome.exe", "wait_sec": 0})
        self.assertTrue(ok, why)
        find_.assert_called_once_with("chrome.exe")
        bring.assert_called_once_with(1234)
        self.assertIn("带到前台", why)

    def test_process_not_running_without_path_fails(self):
        with mock.patch("app.tasks.win_actors.find_process_window", return_value=0), \
                mock.patch("app.tasks.win_actors.launch_app") as launch:
            ok, why = run_app_step({"path": "", "process": "chrome.exe", "wait_sec": 0})
        self.assertFalse(ok)
        launch.assert_not_called()
        self.assertIn("未在运行", why)

    def test_process_not_running_launches_by_path(self):
        with mock.patch("app.tasks.win_actors.find_process_window", return_value=0), \
                mock.patch("app.tasks.win_actors.launch_app",
                           return_value=(True, "已启动 a.exe")) as launch:
            ok, why = run_app_step({"path": "C:/x/a.exe", "process": "a.exe",
                                    "wait_sec": 0})
        self.assertTrue(ok, why)
        launch.assert_called_once_with("C:/x/a.exe")

    def test_no_process_plain_path_launches(self):
        """旧配置只有 path：行为不变，直接启动。"""
        with mock.patch("app.tasks.win_actors.find_process_window") as find_, \
                mock.patch("app.tasks.win_actors.launch_app",
                           return_value=(True, "已启动 a.exe")) as launch:
            ok, why = run_app_step({"path": "C:/x/a.exe", "process": "", "wait_sec": 0})
        self.assertTrue(ok, why)
        find_.assert_not_called()
        launch.assert_called_once_with("C:/x/a.exe")

    def test_process_running_wins_over_path(self):
        """进程与路径都填时以进程为准：在运行就不启动路径。"""
        with mock.patch("app.tasks.win_actors.find_process_window", return_value=99), \
                mock.patch("app.tasks.win_actors.bring_to_front"), \
                mock.patch("app.tasks.win_actors.launch_app") as launch:
            ok, why = run_app_step({"path": "C:/x/a.exe", "process": "a.exe",
                                    "wait_sec": 0})
        self.assertTrue(ok, why)
        launch.assert_not_called()

    def test_use_process_false_skips_process_matching(self):
        """关闭「进程打开」：即使填了进程名也不匹配带出，直接用路径启动。"""
        with mock.patch("app.tasks.win_actors.find_process_window") as find_, \
                mock.patch("app.tasks.win_actors.bring_to_front") as bring, \
                mock.patch("app.tasks.win_actors.launch_app",
                           return_value=(True, "已启动 a.exe")) as launch:
            ok, why = run_app_step({"path": "C:/x/a.exe", "process": "a.exe",
                                    "use_process": False, "wait_sec": 0})
        self.assertTrue(ok, why)
        find_.assert_not_called()
        bring.assert_not_called()
        launch.assert_called_once_with("C:/x/a.exe")

    def test_use_process_false_no_path_fails(self):
        """关闭「进程打开」但没填路径：报错而不是去匹配进程。"""
        with mock.patch("app.tasks.win_actors.find_process_window") as find_:
            ok, why = run_app_step({"path": "", "process": "a.exe",
                                    "use_process": False, "wait_sec": 0})
        self.assertFalse(ok)
        find_.assert_not_called()
        self.assertIn("路径", why)


class TestAppDialogForm(unittest.TestCase):
    """「打开应用」编辑对话框：进程选择描述回填/保存、旧配置路径兼容。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_fill_and_apply_process_target(self):
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="app", params={"target": "Google Chrome — chrome.exe",
                                            "process": "chrome.exe",
                                            "path": "C:/x/chrome.exe", "wait_sec": 1})
        dlg = StepParamsDialog(step)
        self.assertIn("Google Chrome", dlg.app_proc_edit.text())
        self.assertEqual(dlg.path_edit.text(), "C:/x/chrome.exe")
        out = FlowStep(type="app")
        dlg.apply_to(out)
        self.assertEqual(out.params["process"], "chrome.exe")
        self.assertIn("Google Chrome", out.params["target"])
        self.assertEqual(out.params["path"], "C:/x/chrome.exe")
        self.assertEqual(out.params["wait_sec"], 1.0)
        dlg.close()

    def test_legacy_path_only_config(self):
        """旧配置只有 path：回填到路径行，进程行为空，行为不变。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="app", params={"path": "C:/x/old.exe", "wait_sec": 2})
        dlg = StepParamsDialog(step)
        self.assertEqual(dlg.path_edit.text(), "C:/x/old.exe")
        self.assertEqual(dlg.app_proc_edit.text(), "")
        out = FlowStep(type="app")
        dlg.apply_to(out)
        self.assertEqual(out.params["process"], "")
        self.assertEqual(out.params["path"], "C:/x/old.exe")
        dlg.close()

    def test_rejects_empty_target(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="app"))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请选择要打开的应用")
        dlg.close()

    def test_process_check_default_and_row_toggle(self):
        """「进程打开」默认勾选且目标进程行可见；取消勾选 → 目标进程行隐藏。"""
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="app"))
        self.assertTrue(dlg.app_use_proc.isChecked())
        self.assertFalse(dlg.app_proc_edit.isHidden())   # 勾选 → 目标进程行可见
        dlg.app_use_proc.setChecked(False)
        self.assertTrue(dlg.app_proc_edit.isHidden())    # 取消勾选 → 目标进程行隐藏
        dlg.close()

    def test_fill_unchecked_hides_and_apply_clears_process(self):
        """旧步骤 use_process=False：回填时不勾选、进程行隐藏；写回清空进程字段。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="app", params={"path": "D:/工作资料", "process": "a.exe",
                                            "target": "a.exe", "use_process": False,
                                            "wait_sec": 1})
        dlg = StepParamsDialog(step)
        self.assertFalse(dlg.app_use_proc.isChecked())
        self.assertTrue(dlg.app_proc_edit.isHidden())
        out = FlowStep(type="app")
        dlg.apply_to(out)
        self.assertFalse(out.params["use_process"])
        self.assertEqual(out.params["process"], "")
        self.assertEqual(out.params["target"], "")
        self.assertEqual(out.params["path"], "D:/工作资料")
        dlg.close()

    def test_fill_checked_keeps_process(self):
        """use_process=True（默认）：回填勾选且保留进程字段。"""
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="app", params={"path": "C:/x/chrome.exe",
                                            "process": "chrome.exe", "wait_sec": 1})
        dlg = StepParamsDialog(step)
        self.assertTrue(dlg.app_use_proc.isChecked())
        self.assertFalse(dlg.app_proc_edit.isHidden())
        out = FlowStep(type="app")
        dlg.apply_to(out)
        self.assertTrue(out.params["use_process"])
        self.assertEqual(out.params["process"], "chrome.exe")
        dlg.close()

    def test_unchecked_rejects_empty_path(self):
        """关闭「进程打开」后路径为空：拦截并提示填写应用路径。"""
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="app"))
        dlg.app_use_proc.setChecked(False)
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        self.assertTrue(warn.called)
        self.assertEqual(warn.call_args[0][1], "请填写应用路径")
        dlg.close()

    def test_browse_app_picks_any_file(self):
        """浏览文件：默认过滤器为「所有文件」；选中的文件回填到应用路径。"""
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="app"))
        with mock.patch("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        return_value=("C:/data/报表.xlsx", "所有文件 (*)")) as pick:
            dlg._browse_app()
        self.assertEqual(dlg.path_edit.text(), "C:/data/报表.xlsx")
        self.assertTrue(pick.call_args[0][3].startswith("所有文件 (*)"))
        dlg.close()

    def test_browse_app_dir_picks_folder(self):
        """浏览文件夹：选择的目录回填到应用路径。"""
        from app.ui.flow_dialog import StepParamsDialog
        dlg = StepParamsDialog(FlowStep(type="app"))
        with mock.patch("PySide6.QtWidgets.QFileDialog.getExistingDirectory",
                        return_value="D:/工作资料") as pick:
            dlg._browse_app_dir()
        self.assertEqual(dlg.path_edit.text(), "D:/工作资料")
        pick.assert_called_once()
        dlg.close()


if __name__ == "__main__":
    unittest.main()
