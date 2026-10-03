# -*- coding: utf-8 -*-
"""2026-10-02 code review 第二批（P0 updater + P1 十条 + P2）的契约测试。

对应修复清单见 DETAILS.md §38.4 / §39。每条都尽量用**行为**验证，
只有「结构性约束」才退回源码契约。
"""
from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import finder, flows, http_actor, updater, web_actors, win_actors
from app import config as config_mod
from app.ui import flow_dialog, log_panel, schedule_tab
from tests._env import TempConfigPaths


class TestUpdaterIntegrity(unittest.TestCase):
    """P0：截断的下载不能被当成合法新程序把主程序换掉。"""

    @staticmethod
    def _real_exe_bytes() -> bytes:
        import sys
        with open(sys.executable, "rb") as f:
            return f.read()

    def test_real_exe_is_accepted(self):
        """真实 exe 必须能通过（校验别写太严，否则正常更新也会被拦）。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "real.exe")
            with open(p, "wb") as f:
                f.write(self._real_exe_bytes())
            self.assertTrue(updater._is_pe_file(p))

    def test_truncated_real_exe_is_rejected(self):
        """★ 核心回归：把真实 exe 砍掉一半，绝不能通过校验。"""
        data = self._real_exe_bytes()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "half.exe")
            with open(p, "wb") as f:
                f.write(data[:len(data) // 2])
            self.assertFalse(updater._is_pe_file(p),
                             "半截 exe 通过校验会把主程序换坏，再也启动不了")

    def test_error_page_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "page.bin")
            with open(p, "wb") as f:
                f.write(b"<html><body>404 Not Found</body></html>" + b"\x00" * 4000)
            self.assertFalse(updater._is_pe_file(p))

    def test_tiny_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tiny.exe")
            with open(p, "wb") as f:
                f.write(b"MZ")
            self.assertFalse(updater._is_pe_file(p))

    def test_download_checks_content_length(self):
        src = inspect.getsource(updater.download_update)
        self.assertIn("done != total", src,
                      "下载后必须核对实际字节数（http.client 不会抛 IncompleteRead）")


class TestConfigLoadRobustness(unittest.TestCase):
    """P1：手改 config.json 的各种脏数据都不该让程序起不来。"""

    def setUp(self):
        self._tmp = TempConfigPaths()
        self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _write(self, data):
        with open(config_mod.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    # 原先这里还有三条 mail_auth_code 的用例（空串不回填默认 / 键缺失用默认 / null 变空）：
    # 那是「清空授权码 = 用户主动关闭截屏上报」的守卫。2026-10-03 截图上报功能整体删除后，
    # 该字段不复存在，用例一并移除。

    def test_top_level_array_self_heals(self):
        """顶层是数组：不该 AttributeError 崩掉，要走「损坏配置」自愈分支。"""
        self._write([1, 2, 3])
        cfg = config_mod.AppConfig.load()
        self.assertIsInstance(cfg, config_mod.AppConfig)

    def test_top_level_string_self_heals(self):
        self._write("not a config")
        self.assertIsInstance(config_mod.AppConfig.load(), config_mod.AppConfig)

    def test_broken_nested_section_falls_back(self):
        """clicker 写成字符串：用默认值自愈，不要崩。"""
        self._write({"clicker": "坏掉了", "presser": ["x"]})
        cfg = config_mod.AppConfig.load()
        self.assertEqual(cfg.clicker.interval_ms, 100)


class TestSmtpCertificateVerification(unittest.TestCase):
    """P1：SMTP 必须验证服务端证书（否则中间人能看到授权码）。"""

    def test_mail_actor_passes_context(self):
        import smtplib

        from app import mail_actor
        with mock.patch.object(smtplib, "SMTP_SSL") as ssl_mock, \
                mock.patch.object(smtplib, "SMTP") as plain_mock:
            ssl_mock.return_value = mock.Mock()
            plain_mock.return_value = mock.Mock()
            try:
                mail_actor.send_mail("u", "a@b.com", ["中", "文"], "测试正文")
            except Exception:
                pass
        # 两个构造都要拿到带 context 的调用
        for m in (ssl_mock, plain_mock):
            if m.call_args:
                self.assertIn("context", m.call_args.kwargs,
                              "SMTP 必须显式传 context（默认是 CERT_NONE）")

    def test_default_context_verifies(self):
        import ssl
        self.assertTrue(ssl.create_default_context().check_hostname)
        self.assertNotEqual(ssl.create_default_context().verify_mode, ssl.CERT_NONE)


class TestWin32Declarations(unittest.TestCase):
    """P1：句柄类 Win32 返回值必须是 c_void_p（否则 64 位句柄被截断）。"""

    def test_handle_returning_functions_use_void_p(self):
        import ctypes
        for fn in (win_actors._user32.GetForegroundWindow,
                   win_actors._user32.FindWindowW,
                   win_actors._user32.WindowFromPoint):
            self.assertEqual(fn.restype, ctypes.c_void_p, fn)

    def test_thread_and_focus_functions_declared(self):
        import ctypes
        self.assertEqual(win_actors._user32.AttachThreadInput.restype, ctypes.c_bool)
        self.assertEqual(win_actors._user32.GetWindowThreadProcessId.restype,
                         ctypes.c_ulong)
        self.assertEqual(win_actors._kernel32.GetCurrentThreadId.restype,
                         ctypes.c_ulong)

    def test_version_string_uses_bounded_read(self):
        """版本信息必须按长度切片，不能用无界的 wstring_at。"""
        src = Path(inspect.getfile(win_actors)).read_text(encoding="utf-8")
        self.assertNotIn("ctypes.wstring_at(desc_ptr", src)
        self.assertIn("string_at", src)

    def test_enum_pids_retries_when_buffer_too_small(self):
        src = inspect.getsource(win_actors._enum_pids)
        self.assertIn("for _ in range(3)", src,
                      "缓冲区不够时要按 needed 扩容重试，不能静默截断")


class TestResourceAndTimingFixes(unittest.TestCase):
    def test_step_dialogs_delete_on_close(self):
        """编辑弹窗关掉即销毁，别每双击一次多挂一个隐藏对话框。"""
        from app.ui import flow_tab, finder_tab
        for mod in (flow_tab, finder_tab):
            self.assertIn("WA_DeleteOnClose",
                          Path(inspect.getfile(mod)).read_text(encoding="utf-8"))

    def test_region_capture_passes_dialog_explicitly(self):
        """区域框选要显式传 dlg，不能靠共享槽位（并存时张冠李戴）。"""
        src = Path(inspect.getfile(flow_dialog)).read_text(encoding="utf-8")
        self.assertNotIn("singleShot(250, self._start_region_capture)", src)

    def test_log_panel_appends_incrementally(self):
        """日常追加走增量插入，只有首条/截断才全量重排（O(n²) 的根）。"""
        src = inspect.getsource(log_panel.LogPanel.append)
        self.assertIn("_append_one", src)
        self.assertIn("_rerender", src)

    def test_log_panel_has_append_one_helper(self):
        self.assertTrue(hasattr(log_panel.LogPanel, "_append_one"))

    def test_web_actors_sleep_is_outside_lock(self):
        """open_url 的 wait_after 纯 sleep（最长 60s）必须在 with _LOCK 之外。"""
        import ast
        tree = ast.parse(inspect.getsource(web_actors.open_url))
        in_lock = []
        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "sleep"):
                        in_lock.append(sub.lineno)
        self.assertEqual(in_lock, [],
                         f"第 {in_lock} 行的 sleep 在锁内，并行网页步骤会互相干等")

    def test_finder_has_shutdown(self):
        """mss 必须有显式释放入口（mss>=6 无 __del__ 兜底）。"""
        self.assertTrue(callable(getattr(finder, "shutdown", None)))

    def test_flow_runner_releases_mss(self):
        src = Path(inspect.getfile(flows)).read_text(encoding="utf-8")
        self.assertIn("finder.shutdown()", src)

    def test_overlay_close_event_closes_mss(self):
        from app import capture_overlay
        src = Path(inspect.getfile(capture_overlay)).read_text(encoding="utf-8")
        self.assertIn("def closeEvent", src)
        self.assertIn("_mss.close()", src)

    def test_speech_poll_stop(self):
        """播报要能被打断，不能一次 wait 满 600 秒。"""
        from app import speech_actor
        src = inspect.getsource(speech_actor.speak)
        self.assertIn("stop.is_set()", src)
        self.assertIn("wait(0.1)", src)

    def test_overlay_actor_uses_weakref(self):
        """destroyed 的 lambda 不能闭包强引用窗口（引用环）。"""
        from app import overlay_actor
        src = Path(inspect.getfile(overlay_actor)).read_text(encoding="utf-8")
        self.assertIn("weakref.ref(win)", src)


class TestSchedulePersistThrottle(unittest.TestCase):
    """P1：秒级定时任务不能每秒全量重写 config.json。"""

    def _panel(self):
        panel = schedule_tab.ScheduleTab.__new__(schedule_tab.ScheduleTab)
        panel.cfg = mock.Mock()
        panel._last_persist = 0.0
        return panel

    def test_first_call_saves(self):
        panel = self._panel()
        with mock.patch("app.ui.schedule_tab.time.monotonic", return_value=1000.0):
            panel._persist_times_throttled()
        self.assertEqual(panel.cfg.save.call_count, 1)

    def test_within_window_is_skipped(self):
        panel = self._panel()
        with mock.patch("app.ui.schedule_tab.time.monotonic", return_value=1000.0):
            panel._persist_times_throttled()
        with mock.patch("app.ui.schedule_tab.time.monotonic", return_value=1005.0):
            panel._persist_times_throttled()
        self.assertEqual(panel.cfg.save.call_count, 1,
                         "限流窗口内不该重复写盘（会把用户手改的配置盖回去）")

    def test_after_window_saves_again(self):
        panel = self._panel()
        with mock.patch("app.ui.schedule_tab.time.monotonic", return_value=1000.0):
            panel._persist_times_throttled()
        with mock.patch("app.ui.schedule_tab.time.monotonic", return_value=1061.0):
            panel._persist_times_throttled()
        self.assertEqual(panel.cfg.save.call_count, 2)


class TestHttpImageValidation(unittest.TestCase):
    """P2：非图片响应不能存成 .png；同秒请求不能互相覆盖。"""

    def test_recognises_real_images(self):
        self.assertEqual(http_actor._image_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 20), ".png")
        self.assertEqual(http_actor._image_ext(b"\xff\xd8\xff\xe0" + b"0" * 20), ".jpg")
        self.assertEqual(http_actor._image_ext(b"GIF89a" + b"0" * 20), ".gif")
        self.assertEqual(http_actor._image_ext(b"RIFF" + b"0" * 4 + b"WEBP" + b"0" * 10), ".webp")

    def test_rejects_non_images(self):
        for data in (b"<html>404</html>", b'{"error":"nope"}', b"", b"PK\x03\x04"):
            self.assertIsNone(http_actor._image_ext(data), data[:12])

    def test_save_image_returns_none_for_html(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(http_actor, "HTTP_IMAGE_DIR", d):
                self.assertIsNone(http_actor._save_image(b"<html>404</html>",
                                                         "text/html"))
                self.assertEqual(os.listdir(d), [], "非图片不该落盘")

    def test_two_saves_in_same_second_do_not_collide(self):
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(http_actor, "HTTP_IMAGE_DIR", d):
                p1 = http_actor._save_image(b"\x89PNG\r\n\x1a\n" + b"a" * 40, "image/png")
                p2 = http_actor._save_image(b"\x89PNG\r\n\x1a\n" + b"b" * 40, "image/png")
            self.assertNotEqual(p1, p2, "同一秒的两个请求会互相覆盖（原来只到秒）")


class TestTranslatePartialResult(unittest.TestCase):
    """P2：分块翻译中断时，已译的部分要能拿回来。"""

    def test_partial_translation_is_returned(self):
        from app import translate_actor

        calls = {"n": 0}

        def fake_chunk(chunk, src, dst, timeout, proxy):
            calls["n"] += 1
            if calls["n"] <= 2:
                return f"<{chunk}>", "en"
            raise translate_actor.TranslateError("限流")

        with mock.patch.object(translate_actor, "split_chunks",
                               return_value=["a", "b", "c"]), \
                mock.patch.object(translate_actor, "_translate_chunk",
                                  side_effect=fake_chunk):
            ok, result, why = translate_actor.translate("abc", source="auto",
                                                       target="zh-CN")
        self.assertFalse(ok)
        self.assertIsNotNone(result, "失败时不该把已译部分丢掉")
        self.assertTrue(result.get("partial"))
        self.assertIn("<a>", result["text"])
        self.assertIn("<b>", result["text"])


class TestFlowReasonSuccessWhitelist(unittest.TestCase):
    def test_flows_module_uses_whitelist(self):
        self.assertTrue(callable(getattr(flows, "_reason_is_success", None)))
        self.assertFalse(flows._reason_is_success("目标窗口不存在"))


if __name__ == "__main__":
    unittest.main()
