"""restart_watchdog.py 的测试。

这个脚本靠**退出码**跟 restart.bat 通信（0=重启 / 1=结束 / 2=环境不可用 /
3=主程序异常 / 4=已有实例在跑），映射一旦写歪，bat 的行为就整个跑偏，
所以这里把「情形 → 退出码」这条规则钉死。

另外两处坏了也不容易人工发现的地方：
- 退出消息名必须与 app/instance_lock.py 里完全一致（跨进程注册消息靠字符串对齐）；
- 探测依赖前必须排掉 Windows 应用商店的 python 占位符，否则一探测就弹应用商店。

注意：这些用例**不发广播消息**（test_quit_message_id_is_stable_across_processes
只注册、不 PostMessage），否则跑测试会把正在运行的主程序请退。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import restart_watchdog as wd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_LOCK = os.path.join(ROOT, "app", "instance_lock.py")


class TestExitCodeMapping(unittest.TestCase):
    def test_restart_wins_over_everything(self):
        self.assertEqual(wd.exit_code_for(True, None), wd.EXIT_RESTART)
        self.assertEqual(wd.exit_code_for(True, 0), wd.EXIT_RESTART)
        self.assertEqual(wd.exit_code_for(True, 3), wd.EXIT_RESTART)

    def test_clean_exit_stops_the_bat(self):
        self.assertEqual(wd.exit_code_for(False, 0), wd.EXIT_STOP)
        self.assertEqual(wd.exit_code_for(False, None), wd.EXIT_STOP)

    def test_nonzero_exit_is_a_crash(self):
        for rc in (1, 2, 3, -1, -1073741819):
            self.assertEqual(wd.exit_code_for(False, rc), wd.EXIT_CRASH)

    def test_verdict_messages_and_codes(self):
        code, msg = wd.startup_verdict(True, 0, 30.0)
        self.assertEqual(code, wd.EXIT_RESTART)
        self.assertIn("重启", msg)

        code, _ = wd.startup_verdict(False, 3, 30.0)
        self.assertEqual(code, wd.EXIT_CRASH)

        # 启动后秒退且退出码 0 → 多半是单实例保护把新实例挡回来了：
        # 必须给一个"停在屏幕上"的退出码，否则用户只会看到控制台一闪而过。
        code, msg = wd.startup_verdict(False, 0, 0.4)
        self.assertEqual(code, wd.EXIT_ALREADY_RUNNING)
        self.assertIn("实例", msg)

        code, _ = wd.startup_verdict(False, 0, 30.0)
        self.assertEqual(code, wd.EXIT_STOP)

    def test_codes_are_distinct(self):
        codes = (wd.EXIT_RESTART, wd.EXIT_STOP, wd.EXIT_ENV_ERROR,
                 wd.EXIT_CRASH, wd.EXIT_ALREADY_RUNNING)
        self.assertEqual(len(set(codes)), len(codes))
        self.assertEqual(codes[0], 0)          # bat 的 goto :restart 依赖 0


class TestStoreAliasGuard(unittest.TestCase):
    """Windows 应用商店的 python 占位符必须被排掉，否则探测=弹商店。"""

    def test_real_interpreter_is_not_alias(self):
        self.assertFalse(wd.is_store_alias(sys.executable))

    def test_missing_file_is_not_alias(self):
        self.assertFalse(wd.is_store_alias(os.path.join(ROOT, "no_such_python.exe")))

    def test_empty_file_is_alias(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "python.exe")
            with open(p, "wb"):
                pass
            self.assertTrue(wd.is_store_alias(p))

    def test_non_empty_file_is_not_alias(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "python.exe")
            with open(p, "wb") as f:
                f.write(b"MZ")
            self.assertFalse(wd.is_store_alias(p))

    def test_windowsapps_path_is_alias(self):
        fake = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\python.exe"
        self.assertTrue(wd.is_store_alias(fake))

    def test_probe_refuses_alias_without_launching_it(self):
        fake = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\python.exe"
        with mock.patch.object(subprocess, "run") as run:
            missing = wd.probe_modules(fake, ("json",))
        run.assert_not_called()
        self.assertTrue(missing.startswith("<"))


class TestProbeModules(unittest.TestCase):
    """探测走子进程真导入，这里用标准库模块验证语义。"""

    def test_all_present_returns_none(self):
        self.assertIsNone(wd.probe_modules(sys.executable, ("json", "os", "re")))

    def test_missing_module_is_reported(self):
        self.assertEqual(wd.probe_modules(sys.executable, ("json", "definitely_absent_xyz")),
                         "definitely_absent_xyz")

    def test_unusable_interpreter_is_reported(self):
        missing = wd.probe_modules(os.path.join(ROOT, "no_such_python.exe"), ("json",))
        self.assertTrue(missing.startswith("<"), missing)

    def test_required_modules_cover_the_runtime_stack(self):
        """main.py 顶层就会 import 这些东西，漏一个就会出现「探测通过但启动即崩」。"""
        for name in ("PySide6", "keyboard", "mss", "cv2", "numpy", "PIL", "DrissionPage"):
            self.assertIn(name, wd.REQUIRED_MODULES)


class TestHotkeyEnv(unittest.TestCase):
    def test_default_is_console_only(self):
        self.assertEqual(wd.hotkey_scope({}), "console")
        self.assertEqual(wd.hotkey_scope({wd.ENV_GLOBAL_HOTKEY: "   "}), "console")

    def test_global_scope_when_env_set(self):
        self.assertEqual(wd.hotkey_scope({wd.ENV_GLOBAL_HOTKEY: "ctrl+alt+r"}), "global")

    def test_gate_on_by_default_off_when_disabled(self):
        self.assertTrue(wd.gate_enabled({}))
        for v in ("1", "true", "YES", "on"):
            self.assertFalse(wd.gate_enabled({wd.ENV_GLOBAL_NO_GATE: v}))


class TestQuitMessageContract(unittest.TestCase):
    """看门狗广播的退出消息名必须和主程序监听的完全一致。"""

    def _name_from_source(self, path: str, var: str) -> str:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        m = re.search(rf'{var}\s*=\s*"([^"]+)"', src)
        self.assertIsNotNone(m, f"{path} 里找不到 {var}")
        return m.group(1)

    def test_names_match_instance_lock(self):
        self.assertEqual(wd.QUIT_MESSAGE_NAME,
                         self._name_from_source(INSTANCE_LOCK, "_QUIT_MESSAGE_NAME"))

    @unittest.skipUnless(sys.platform == "win32", "Windows 消息机制")
    def test_quit_message_id_is_stable_across_processes(self):
        """同名注册在不同进程必须拿到同一个 id，否则广播没人认。"""
        import ctypes
        mine = int(ctypes.windll.user32.RegisterWindowMessageW(wd.QUIT_MESSAGE_NAME))
        self.assertGreater(mine, 0)
        code = ("import ctypes;"
                "print(int(ctypes.windll.user32.RegisterWindowMessageW(%r)))"
                % wd.QUIT_MESSAGE_NAME)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), mine)


class TestWindowHelpers(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "toolhelp32 快照")
    def test_parent_chain_finds_the_real_parent(self):
        self.assertEqual(wd.parent_chain(os.getpid(), 1), {os.getppid()})

    def test_parent_chain_zero_depth_is_empty(self):
        self.assertEqual(wd.parent_chain(os.getpid(), 0), set())

    def test_allowed_pids_contains_self_and_child(self):
        pids = wd.allowed_pids(4321)
        self.assertIn(os.getpid(), pids)
        self.assertIn(4321, pids)
        self.assertNotIn(0, pids)

    def test_foreground_gate_never_raises(self):
        self.assertIsInstance(wd.foreground_is_ours(4321), bool)

    @unittest.skipUnless(sys.platform == "win32", "Windows 窗口 API")
    def test_foreground_gate_fails_open_on_errors(self):
        """Win32 调用炸了也必须放行——不能把"按了 Ctrl+R 没反应"留给用户。"""

        class _Raiser:
            def __getattr__(self, name):
                raise OSError("boom")

        with mock.patch("ctypes.windll", _Raiser()):
            self.assertTrue(wd.foreground_is_ours(4321))


class TestShutdownChild(unittest.TestCase):
    """shutdown_child 必须在有界时间内把子进程结束掉（bat 依赖它能返回）。"""

    def test_kills_child_that_ignores_the_quit_broadcast(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            with mock.patch.object(wd, "request_graceful_quit", return_value=False), \
                 mock.patch.object(wd, "FORCE_KILL_TIMEOUT", 5.0):
                t0 = time.monotonic()
                wd.shutdown_child(child)
                self.assertLess(time.monotonic() - t0, 15.0)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()

    def test_already_dead_child_is_a_noop(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=60)
        with mock.patch.object(wd, "request_graceful_quit") as quit_req:
            wd.shutdown_child(child)
        quit_req.assert_not_called()

    def test_graceful_path_returns_once_child_exits(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            def fake_quit():
                child.terminate()          # 模拟"主程序收到退出消息后自己退了"
                return True

            with mock.patch.object(wd, "request_graceful_quit", side_effect=fake_quit):
                t0 = time.monotonic()
                wd.shutdown_child(child)
                self.assertLess(time.monotonic() - t0, 5.0)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()


class TestDistSync(unittest.TestCase):
    """启动前同步 dist（与打包脚本同一套逻辑）：没打过包就跳过，失败不拦启动。"""

    _EMPTY = {"added": 0, "updated": 0, "same": 0, "extra": 0,
              "backed_up": 0, "backup_dir": ""}

    def test_skips_silently_when_no_dist(self):
        missing = os.path.join(tempfile.gettempdir(), "qf_no_such_dist_12345")
        buf = io.StringIO()
        with mock.patch("build.DIST_DIR", missing), \
                contextlib.redirect_stdout(buf):
            self.assertIsNone(wd.sync_dist_data())
        self.assertEqual(buf.getvalue(), "")

    def test_returns_summary_from_build(self):
        st = dict(self._EMPTY, added=2, updated=1)
        with mock.patch("build.DIST_DIR", tempfile.gettempdir()), \
                mock.patch("build.sync_data_dirs", return_value=st), \
                mock.patch("build.sync_task_board", return_value="same"):
            self.assertEqual(wd.sync_dist_data(), st)

    def test_prints_one_line_only_when_changed(self):
        st = dict(self._EMPTY, added=2, updated=1)
        buf = io.StringIO()
        with mock.patch("build.DIST_DIR", tempfile.gettempdir()), \
                mock.patch("build.sync_data_dirs", return_value=st), \
                mock.patch("build.sync_task_board", return_value="same"), \
                contextlib.redirect_stdout(buf):
            wd.sync_dist_data()
        out = buf.getvalue()
        self.assertIn("[同步]", out)
        self.assertIn("新增 2", out)

    def test_silent_when_nothing_changed(self):
        buf = io.StringIO()
        with mock.patch("build.DIST_DIR", tempfile.gettempdir()), \
                mock.patch("build.sync_data_dirs", return_value=dict(self._EMPTY)), \
                mock.patch("build.sync_task_board", return_value="same"), \
                contextlib.redirect_stdout(buf):
            wd.sync_dist_data()
        self.assertEqual(buf.getvalue(), "")

    def test_task_board_sync_is_reported_by_direction(self):
        """task_board.json 双向同步：有方向就提示一行，没变化不刷屏。"""
        for status, expect in (("ws-to-dist", "工作区 → dist"),
                               ("dist-to-ws", "dist → 工作区"),
                               ("same", ""),
                               ("no-dist", "")):
            buf = io.StringIO()
            with mock.patch("build.DIST_DIR", tempfile.gettempdir()), \
                    mock.patch("build.sync_data_dirs",
                               return_value=dict(self._EMPTY)), \
                    mock.patch("build.sync_task_board", return_value=status), \
                    contextlib.redirect_stdout(buf):
                wd.sync_dist_data()
            out = buf.getvalue()
            if expect:
                self.assertIn("task_board.json", out, status)
                self.assertIn(expect, out, status)
            else:
                self.assertNotIn("task_board.json", out, status)

    def test_task_board_sync_failure_is_swallowed(self):
        """task_board.json 同步失败只提示，不能拦住启动。"""
        buf = io.StringIO()
        with mock.patch("build.DIST_DIR", tempfile.gettempdir()), \
                mock.patch("build.sync_data_dirs", return_value=dict(self._EMPTY)), \
                mock.patch("build.sync_task_board", side_effect=OSError("busy")), \
                contextlib.redirect_stdout(buf):
            self.assertIsNotNone(wd.sync_dist_data())
        self.assertIn("task_board.json", buf.getvalue())

    def test_failure_is_swallowed(self):
        """同步失败只提示，不能拦住主程序启动。"""
        with mock.patch("build.DIST_DIR", tempfile.gettempdir()), \
                mock.patch("build.sync_data_dirs", side_effect=OSError("busy")):
            self.assertIsNone(wd.sync_dist_data())      # 不抛异常

    def test_called_before_starting_main(self):
        """源码级契约：同步必须在 Popen(main.py) **之前**调用。"""
        with open(os.path.join(ROOT, "restart_watchdog.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def main()")[1]
        start = body.index("sync_dist_data()")
        popen = body.index("subprocess.Popen")
        self.assertLess(start, popen, "同步要在启动主程序之前做")


class TestConfigSync(unittest.TestCase):
    """config.json 也随启动同步到 dist：version 各自保留，其余以工作区为准。"""

    def test_keeps_dist_version_and_syncs_rest(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "dist"))
            src = {"version": "0.0.1", "mail_host": "smtp.new", "ui_theme": "night"}
            dst = {"version": "v3.9.0", "mail_host": "smtp.old"}
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
                json.dump(src, f)
            with open(os.path.join(tmp, "dist", "config.json"), "w",
                      encoding="utf-8") as f:
                json.dump(dst, f)

            with mock.patch.object(wd, "SCRIPT_DIR", tmp):
                self.assertTrue(wd.sync_dist_config(os.path.join(tmp, "dist")))

            with open(os.path.join(tmp, "dist", "config.json"), encoding="utf-8") as f:
                out = json.load(f)
            self.assertEqual(out["version"], "v3.9.0")       # dist version 保留
            self.assertEqual(out["mail_host"], "smtp.new")   # 其余同步
            self.assertEqual(out["ui_theme"], "night")       # 新字段带过去

            # 内容已一致：再同步不落盘、返回 False
            with mock.patch.object(wd, "SCRIPT_DIR", tmp):
                self.assertFalse(wd.sync_dist_config(os.path.join(tmp, "dist")))
        finally:
            shutil.rmtree(tmp)

    def test_skips_when_dist_config_missing(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"version": "0.0.1"}, f)
            with mock.patch.object(wd, "SCRIPT_DIR", tmp):
                self.assertFalse(wd.sync_dist_config(os.path.join(tmp, "dist")))
        finally:
            shutil.rmtree(tmp)

    def test_skips_when_src_invalid(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "dist"))
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
                f.write("{ not json")
            with open(os.path.join(tmp, "dist", "config.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"version": "v3.9.0"}, f)
            with mock.patch.object(wd, "SCRIPT_DIR", tmp):
                self.assertFalse(wd.sync_dist_config(os.path.join(tmp, "dist")))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
