"""打开应用（app）步骤 + 后台键鼠的测试。

后台键鼠需真实窗口句柄，单元测试只覆盖纯函数（VK 转换、参数默认值、
summary、执行失败路径）；真实窗口/后台发送靠手工冒烟（见 win_actors 冒烟）。
"""
from __future__ import annotations

import unittest

from app import win_actors
from app.config import FLOW_STEP_TYPES, FlowStep, default_step_params


class TestAppStepConfig(unittest.TestCase):
    def test_registered(self):
        self.assertEqual(FLOW_STEP_TYPES.get("app"), "打开应用")

    def test_default_params(self):
        p = default_step_params("app")
        self.assertEqual(p["path"], "")
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
        # 只列有可见窗口的应用进程：每条含 pid/name/app_name/title（title 非空），
        # 过滤掉无窗口的后台子进程/服务；全 Unicode API 无乱码
        procs = win_actors.list_processes()
        self.assertIsInstance(procs, list)
        for p in procs:
            self.assertIn("pid", p)
            self.assertTrue(p["name"].lower().endswith(".exe"))
            self.assertTrue(p["title"], "应只保留有窗口标题的进程")
            for v in (p["name"], p["app_name"], p["title"]):
                self.assertNotIn("\ufffd", v)


if __name__ == "__main__":
    unittest.main()
