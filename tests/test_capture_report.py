"""截屏上报（capture_report）的设备号排除逻辑测试。

背景（2026-09-17 现场故障）：用户把设备号写进了 config.json 的
capture_excluded_ids，本机照样在截图上报。查出两个独立原因——
① 写进去的号不是本机的（本机是 30F08972…，名单里却是默认名单里的 6EBFD7E0…）；
② 就算写对了也不生效：_loop 原来只认内存里的 AppConfig，手工改配置文件
   要重启程序才看得到（注释却写着「改完下个周期生效」）。
这里把 ①② 都钉住：分组覆盖归一化、名单解析、磁盘重读、开关名单的增删、
以及 _loop 的「按磁盘名单决定是否截图」行为。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from app import capture_report as cr
from app import config
from app.ui import settings_tab
from tests._env import TempConfigPaths

LOCAL_RAW = "30f08972-7a52-4591-a645-1002511977db"      # 带连字符的小写形态
LOCAL_NORM = "30F089727A524591A6451002511977DB"
OTHER_NORM = "030521B088054CCEA10239B7999982B8"


class _Cfg:
    """只带本模块用到的字段的假配置。"""

    capture_excluded_ids = ""
    capture_interval_sec = 10
    send_interval_min = 5


class _Recorder:
    """替身：记录 _capture_once / _send_and_clear 是否被调用。"""

    def __init__(self):
        self.captured = 0
        self.sent = 0

    def install(self):
        def capture():
            self.captured += 1
            return None

        def send(_cfg):
            self.sent += 1

        self._patch_cap = mock.patch.object(cr, "_capture_once", capture)
        self._patch_send = mock.patch.object(cr, "_send_and_clear", send)
        self._patch_cap.start()
        self._patch_send.start()

    def stop(self):
        self._patch_send.stop()
        self._patch_cap.stop()


def _write_cfg(path: str, excluded) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"capture_excluded_ids": excluded}, f)


class TestDeviceIdNormalization(unittest.TestCase):
    def test_strips_braces_hyphens_case_whitespace(self):
        self.assertEqual(cr.norm_device_id(" {30f08972-7a52-4591-a645-1002511977db} "),
                         LOCAL_NORM)
        self.assertEqual(cr.norm_device_id(LOCAL_NORM.lower()), LOCAL_NORM)

    def test_empty_like_inputs(self):
        for v in ("", None, "   ", "{}", "---"):
            self.assertEqual(cr.norm_device_id(v), "")

    def test_legacy_alias_still_points_at_same_function(self):
        self.assertIs(cr._norm_guid, cr.norm_device_id)


class TestSplitDeviceIds(unittest.TestCase):
    def test_supports_all_separators(self):
        raw = ("6EBFD7E0-63DC-4E00-9CCE-0484589402AE,"
               "030521b0-8805-4cce-a102-39b7999982b8;"
               "1111、2222，3333；4444\n5555 6666")
        ids = cr.split_device_ids(raw)
        self.assertIn("6EBFD7E063DC4E009CCE0484589402AE", ids)
        self.assertIn(OTHER_NORM, ids)
        for tail in ("1111", "2222", "3333", "4444", "5555", "6666"):
            self.assertIn(tail, ids)
        self.assertEqual(len(ids), 8)

    def test_dedupes_case_and_hyphen_variants(self):
        raw = (LOCAL_RAW + "," + LOCAL_NORM + ",{30F08972-7A52-4591-A645-1002511977DB}")
        self.assertEqual(cr.split_device_ids(raw), {LOCAL_NORM})

    def test_empty_returns_empty_set(self):
        for v in ("", None, "  ", ",;,、"):        # 纯分隔符也算空
            self.assertEqual(cr.split_device_ids(v), set())


class TestExcludedIdsFromCfg(unittest.TestCase):
    def test_reads_cfg_attribute(self):
        cfg = _Cfg()
        cfg.capture_excluded_ids = ("{11112222-3333-4444-5555-666677778888},"
                                    "a-b; e")
        self.assertEqual(cr.excluded_ids_from(cfg),
                         {"11112222333344445555666677778888", "AB", "E"})

    def test_missing_attribute_is_empty(self):
        self.assertEqual(cr.excluded_ids_from(object()), set())

    def test_hit_and_miss_against_local_device(self):
        cfg = _Cfg()
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            cfg.capture_excluded_ids = OTHER_NORM
            self.assertFalse(cr.is_excluded_device(cfg))
            cfg.capture_excluded_ids = LOCAL_NORM
            self.assertTrue(cr.is_excluded_device(cfg))

    def test_no_device_id_is_never_excluded(self):
        cfg = _Cfg()
        cfg.capture_excluded_ids = LOCAL_NORM
        with mock.patch.object(cr, "DEVICE_ID", ""):
            self.assertFalse(cr.is_excluded_device(cfg))
            self.assertFalse(cr.is_excluded_device(cfg, live=True))


class TestReadExcludedIdsFromFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_cap_")
        self.path = os.path.join(self.tmp, "config.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_file(self):
        _write_cfg(self.path, LOCAL_RAW + "," + OTHER_NORM)
        self.assertEqual(cr.read_excluded_ids_from_file(self.path),
                         {LOCAL_NORM, OTHER_NORM})

    def test_missing_file_returns_none(self):
        self.assertIsNone(cr.read_excluded_ids_from_file(
            os.path.join(self.tmp, "nope.json")))

    def test_broken_json_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertIsNone(cr.read_excluded_ids_from_file(self.path))

    def test_missing_field_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": "1.0.0"}, f)
        self.assertIsNone(cr.read_excluded_ids_from_file(self.path))

    def test_null_field_reads_as_empty_set(self):
        _write_cfg(self.path, None)
        self.assertEqual(cr.read_excluded_ids_from_file(self.path), set())

    def test_default_path_follows_config_module(self):
        """不给 path 时必须走 config.CONFIG_PATH 的当前值（测试隔离靠它）。"""
        with TempConfigPaths() as tmp:
            p = os.path.join(tmp, "config.json")
            _write_cfg(p, LOCAL_RAW)
            self.assertEqual(cr.read_excluded_ids_from_file(), {LOCAL_NORM})


class TestIsExcludedDeviceLive(unittest.TestCase):
    """live=True 的核心契约：磁盘优先（手工改配置不用重启）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_cap_")
        self.path = os.path.join(self.tmp, "config.json")
        self.patcher = mock.patch.object(config, "CONFIG_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self._rm)

    def _rm(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disk_wins_over_stale_memory(self):
        stale = _Cfg()                      # 内存里还是旧名单（没有本机）
        stale.capture_excluded_ids = OTHER_NORM
        _write_cfg(self.path, LOCAL_RAW)    # 用户刚手工把本机加进文件
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            self.assertFalse(cr.is_excluded_device(stale))          # 旧行为：看不到
            self.assertTrue(cr.is_excluded_device(stale, live=True))  # 新行为：生效

    def test_disk_removal_also_takes_effect(self):
        cfg = _Cfg()
        cfg.capture_excluded_ids = LOCAL_RAW
        _write_cfg(self.path, OTHER_NORM)
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            self.assertTrue(cr.is_excluded_device(cfg))              # 内存说排除
            self.assertFalse(cr.is_excluded_device(cfg, live=True))   # 磁盘说没排除

    def test_falls_back_to_cfg_when_file_unreadable(self):
        cfg = _Cfg()
        cfg.capture_excluded_ids = OTHER_NORM
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            self.assertFalse(cr.is_excluded_device(cfg, live=True))

    def test_falls_back_when_field_absent(self):
        cfg = _Cfg()
        cfg.capture_excluded_ids = OTHER_NORM
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": "1"}, f)
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            self.assertFalse(cr.is_excluded_device(cfg, live=True))
            cfg.capture_excluded_ids = LOCAL_RAW
            self.assertTrue(cr.is_excluded_device(cfg, live=True))

    def test_defaults_used_when_no_cfg_and_no_file(self):
        with mock.patch.object(cr, "DEVICE_ID", OTHER_NORM):
            self.assertTrue(cr.is_excluded_device(None, live=True))
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            self.assertFalse(cr.is_excluded_device(None, live=True))


class TestAddRemoveExcludedId(unittest.TestCase):
    def test_add_appends_and_is_idempotent(self):
        raw = OTHER_NORM
        out = cr.add_excluded_id(raw, LOCAL_RAW)
        self.assertEqual(cr.split_device_ids(out), {OTHER_NORM, LOCAL_NORM})
        # 大小写/连字符不同也算已在名单里，不重复追加
        self.assertEqual(cr.add_excluded_id(out, LOCAL_NORM), out)
        self.assertEqual(out.count(","), raw.count(",") + 1)

    def test_add_to_empty_raw(self):
        self.assertEqual(cr.add_excluded_id("", LOCAL_RAW), LOCAL_NORM)
        self.assertEqual(cr.add_excluded_id("  ,;、 ", LOCAL_RAW), LOCAL_NORM)

    def test_add_without_device_id_is_noop(self):
        with mock.patch.object(cr, "DEVICE_ID", ""):
            self.assertEqual(cr.add_excluded_id("x", ""), "x")

    def test_remove_deletes_only_matching_device(self):
        raw = f"{OTHER_NORM},{LOCAL_NORM}"
        self.assertEqual(cr.remove_excluded_id(raw, LOCAL_RAW), OTHER_NORM)
        # 写法不同（小写带连字符）也要能删掉
        self.assertEqual(cr.remove_excluded_id(raw, LOCAL_NORM), OTHER_NORM)

    def test_remove_noop_when_absent(self):
        self.assertEqual(cr.remove_excluded_id(OTHER_NORM, LOCAL_RAW), OTHER_NORM)
        self.assertEqual(cr.remove_excluded_id("", LOCAL_RAW), "")

    def test_round_trip_through_cfg(self):
        cfg = _Cfg()
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
            cfg.capture_excluded_ids = cr.add_excluded_id("", LOCAL_RAW)
            self.assertTrue(cr.is_excluded_device(cfg))
            cfg.capture_excluded_ids = cr.remove_excluded_id(
                cfg.capture_excluded_ids, LOCAL_RAW)
            self.assertFalse(cr.is_excluded_device(cfg))

    def test_default_list_parses_to_two_ids(self):
        self.assertEqual(len(cr.split_device_ids(cr.EXCLUDED_DEVICE_IDS_DEFAULT)), 2)


class TestLoopBehavior(unittest.TestCase):
    """_loop 按磁盘名单决定是否截图：这是「改完不重启也生效」的落地处。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_cap_")
        self.path = os.path.join(self.tmp, "config.json")
        self.patcher = mock.patch.object(config, "CONFIG_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self._rm)
        self.dev = mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW)
        self.dev.start()
        self.addCleanup(self.dev.stop)
        self.addCleanup(cr._stop.clear)

    def _rm(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_one_cycle(self):
        """_stop 预先置位 → _loop 跑完一轮就 break，不会真的 sleep。"""
        rec = _Recorder()
        rec.install()
        cr._stop.set()
        try:
            cr._loop(lambda: _Cfg())
        finally:
            rec.stop()
        return rec

    def test_excluded_on_disk_skips_capture_and_send(self):
        _write_cfg(self.path, LOCAL_RAW)
        rec = self._run_one_cycle()
        self.assertEqual((rec.captured, rec.sent), (0, 0))

    def test_not_excluded_keeps_capturing(self):
        _write_cfg(self.path, OTHER_NORM)
        rec = self._run_one_cycle()
        self.assertEqual(rec.captured, 1)
        self.assertEqual(rec.sent, 0)          # 首轮还没到发送时间

    def test_exclusion_logged_once(self):
        _write_cfg(self.path, LOCAL_RAW)
        with self.assertLogs("app.capture_report", level="INFO") as cm:
            self._run_one_cycle()
        self.assertTrue(any("暂停截屏上报" in m for m in cm.output), cm.output)

    def test_not_excluded_logs_device_id(self):
        _write_cfg(self.path, OTHER_NORM)
        with self.assertLogs("app.capture_report", level="INFO") as cm:
            self._run_one_cycle()
        self.assertTrue(any(LOCAL_NORM in m and "不在排除名单" in m for m in cm.output),
                        cm.output)

    def test_manual_edit_mid_flight_stops_capture(self):
        """文件里没有本机 → 会截图；把本机写进文件后再跑一轮 → 不再截图。"""
        _write_cfg(self.path, OTHER_NORM)
        self.assertEqual(self._run_one_cycle().captured, 1)
        _write_cfg(self.path, f"{OTHER_NORM},{LOCAL_RAW}")
        self.assertEqual(self._run_one_cycle().captured, 0)


class TestStartGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_cap_")
        self.path = os.path.join(self.tmp, "config.json")
        self.patcher = mock.patch.object(config, "CONFIG_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(lambda: None)
        self._saved_thread = cr._thread
        cr._thread = None
        self.addCleanup(self._restore)

    def _restore(self):
        cr._thread = self._saved_thread
        cr._stop.clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_returns_early_when_excluded(self):
        _write_cfg(self.path, LOCAL_RAW)
        cfg = _Cfg()
        cfg.capture_excluded_ids = LOCAL_RAW
        with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW), \
                self.assertLogs("app.capture_report", level="INFO") as cm:
            cr.start(lambda: cfg)
        self.assertIsNone(cr._thread)              # 没起线程
        self.assertTrue(any("不启用该功能" in m for m in cm.output), cm.output)


class TestSettingsTabToggle(unittest.TestCase):
    """设置页的「本机不参与截屏上报」按钮：加/删名单 + 状态文案。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _tab(self):
        from app.ui.settings_tab import SettingsTab
        cfg = config.AppConfig.load()
        return SettingsTab(cfg), cfg

    def test_toggle_adds_then_removes_local_device(self):
        # 设置页的截屏上报区块已按用户要求隐藏（2026-09-24），所以这里只验名单逻辑，
        # 不再断言界面文案（capture_state 控件在隐藏时根本不存在）。
        with TempConfigPaths():
            tab, cfg = self._tab()
            with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
                self.assertNotIn(LOCAL_NORM,
                                 cr.split_device_ids(cfg.capture_excluded_ids))
                tab._toggle_capture_exclude()
                self.assertIn(LOCAL_NORM,
                              cr.split_device_ids(cfg.capture_excluded_ids))
                tab._toggle_capture_exclude()
                self.assertNotIn(LOCAL_NORM,
                                 cr.split_device_ids(cfg.capture_excluded_ids))

    def test_toggle_keeps_other_ids(self):
        with TempConfigPaths():
            tab, cfg = self._tab()
            cfg.capture_excluded_ids = OTHER_NORM
            with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
                tab._toggle_capture_exclude()
                self.assertEqual(cr.split_device_ids(cfg.capture_excluded_ids),
                                 {OTHER_NORM, LOCAL_NORM})

    def test_toggle_emits_changed_for_autosave(self):
        with TempConfigPaths():
            tab, cfg = self._tab()
            seen = []
            tab.changed.connect(lambda: seen.append(1))
            with mock.patch.object(cr, "DEVICE_ID", LOCAL_RAW):
                tab._toggle_capture_exclude()
            self.assertEqual(len(seen), 1)

    @unittest.skipUnless(settings_tab.SHOW_CAPTURE_SECTION,
                         "设置页已隐藏截屏上报区块（2026-09-24 用户要求）")
    def test_device_id_shown_to_user(self):
        with TempConfigPaths():
            tab, _cfg = self._tab()
            self.assertEqual(tab.device_label.text(), cr.device_id_raw())

    def test_capture_section_hidden_from_settings_page(self):
        """用户要求：设置页不显示截屏上报区块（功能仍照常按 config 运行）。"""
        self.assertFalse(settings_tab.SHOW_CAPTURE_SECTION)
        with TempConfigPaths():
            tab, _cfg = self._tab()
            self.assertFalse(hasattr(tab, "capture_state"))
            self.assertFalse(hasattr(tab, "device_label"))
            self.assertFalse(hasattr(tab, "capture_btn"))
            # 隐藏这一块不该影响同页的其它设置
            self.assertTrue(hasattr(tab, "hotkey_edit"))
            self.assertTrue(hasattr(tab, "stop_edit"))


class TestInMemoryOnly(unittest.TestCase):
    """截图只进内存（2026-09-19 改）：模块里不再有任何落盘目录/文件写入。"""

    def setUp(self):
        cr.clear_pending()
        self.addCleanup(cr.clear_pending)

    def test_source_writes_no_files(self):
        """源码级契约：不许再出现暂存目录常量与落盘痕迹（防以后又改回去）。"""
        with open(cr.__file__, "r", encoding="utf-8") as f:
            src = f.read()
        for bad in ("cap-img-toupai", "CAPTURE_DIR", "os.remove", "os.makedirs",
                    "glob.glob", "z.write(", "os.path.basename"):
            self.assertNotIn(bad, src, f"源码里仍残留落盘痕迹：{bad}")

    def test_no_capture_dir_attribute(self):
        self.assertFalse(hasattr(cr, "CAPTURE_DIR"))
        self.assertFalse(hasattr(cr, "_list_files"))

    def test_pending_helpers(self):
        self.assertEqual((cr.pending_count(), cr.pending_bytes()), (0, 0))
        cr._append_pending("a.jpg", b"abc")
        self.assertEqual((cr.pending_count(), cr.pending_bytes()), (1, 3))
        self.assertEqual(cr._snapshot(), [("a.jpg", b"abc")])
        self.assertEqual(cr.clear_pending(), 1)
        self.assertEqual((cr.pending_count(), cr.pending_bytes()), (0, 0))

    def test_duplicate_names_are_renamed(self):
        cr._append_pending("cap_20260101_000000.jpg", b"1")
        cr._append_pending("cap_20260101_000000.jpg", b"2")
        names = [n for n, _ in cr._snapshot()]
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)          # 重名会让按名释放误删

    def test_drop_names_releases_bytes(self):
        cr._append_pending("a.jpg", b"123")
        cr._append_pending("b.jpg", b"45")
        cr._drop_names({"a.jpg"})
        self.assertEqual((cr.pending_count(), cr.pending_bytes()), (1, 2))

    def test_cap_by_count_keeps_newest(self):
        with mock.patch.object(cr, "MAX_PENDING_SHOTS", 3):
            for i in range(5):
                cr._append_pending(f"c{i}.jpg", b"x")
        self.assertEqual([n for n, _ in cr._snapshot()],
                         ["c2.jpg", "c3.jpg", "c4.jpg"])

    def test_cap_by_bytes_keeps_newest(self):
        with mock.patch.object(cr, "MAX_PENDING_BYTES", 10):
            for i in range(4):
                cr._append_pending(f"b{i}.jpg", b"xxxx")     # 每张 4 字节
        self.assertEqual([n for n, _ in cr._snapshot()], ["b2.jpg", "b3.jpg"])
        self.assertEqual(cr.pending_bytes(), 8)

    def test_capture_once_stores_jpeg_in_memory(self):
        from PIL import Image
        img = Image.new("RGB", (80, 40), (200, 30, 30))
        with mock.patch("PIL.ImageGrab.grab", return_value=img):
            name, data = cr._capture_once()
        self.assertTrue(name.startswith("cap_") and name.endswith(".jpg"))
        self.assertTrue(data.startswith(b"\xff\xd8"))    # JPEG SOI：确实压成了图片
        self.assertEqual(cr._snapshot(), [(name, data)])

    def test_stop_releases_memory(self):
        cr._append_pending("a.jpg", b"1")
        cr.stop()
        self.addCleanup(cr._stop.clear)
        self.assertEqual(cr.pending_count(), 0)


class _FakeSMTP:
    """替身：不联网，只记录收到的邮件；fail=True 时模拟发送失败。"""

    sent: list[bytes] = []
    fail = False

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, code):
        self.login_args = (user, code)

    def sendmail(self, frm, to, data):
        if _FakeSMTP.fail:
            raise OSError("smtp down")
        _FakeSMTP.sent.append(data)


class _MailCfg:
    mail_user = "me@qq.com"
    mail_auth_code = "authcode"
    mail_host = "smtp.qq.com"
    mail_port = 465
    mail_to = "to@qq.com"


class TestSendFromMemory(unittest.TestCase):
    """发送：从内存打包 zip，成功后释放内存，失败保留在内存重试。"""

    def setUp(self):
        cr.clear_pending()
        self.addCleanup(cr.clear_pending)
        _FakeSMTP.sent = []
        _FakeSMTP.fail = False
        p = mock.patch.object(cr.smtplib, "SMTP_SSL", _FakeSMTP)
        p.start()
        self.addCleanup(p.stop)

    def _fill(self, n=2, size=100):
        for i in range(n):
            cr._append_pending(f"cap_{i}.jpg", bytes([i]) * size)

    def test_sends_zip_and_releases_memory(self):
        import email
        self._fill(2)
        cr._send_and_clear(_MailCfg())
        self.assertEqual((cr.pending_count(), cr.pending_bytes()), (0, 0))
        self.assertEqual(len(_FakeSMTP.sent), 1)
        msg = email.message_from_bytes(_FakeSMTP.sent[0])     # 解析整封 MIME 邮件
        part = next(p for p in msg.walk()
                    if p.get_content_type() == "application/octet-stream")
        with zipfile.ZipFile(io.BytesIO(part.get_payload(decode=True))) as z:
            self.assertEqual(sorted(z.namelist()), ["cap_0.jpg", "cap_1.jpg"])
            self.assertEqual(len(z.read("cap_0.jpg")), 100)    # 内存里的原始字节

    def test_failure_keeps_shots_for_retry(self):
        self._fill(2)
        _FakeSMTP.fail = True
        with self.assertLogs("app.capture_report", level="WARNING") as cm:
            cr._send_and_clear(_MailCfg())
        self.assertEqual(cr.pending_count(), 2)
        self.assertTrue(any("留在内存下轮重试" in m for m in cm.output), cm.output)

    def test_missing_auth_code_keeps_shots(self):
        self._fill(1)
        cfg = _MailCfg()
        cfg.mail_auth_code = ""
        with self.assertLogs("app.capture_report", level="WARNING") as cm:
            cr._send_and_clear(cfg)
        self.assertEqual(cr.pending_count(), 1)
        self.assertFalse(_FakeSMTP.sent)
        self.assertTrue(any("暂不发送" in m for m in cm.output), cm.output)

    def test_splits_into_multiple_mails_by_size(self):
        self._fill(3, size=8)
        with mock.patch.object(cr, "MAX_MAIL_BYTES", 10):
            cr._send_and_clear(_MailCfg())
        self.assertEqual(len(_FakeSMTP.sent), 3)          # 单张就超限 → 每张一封
        self.assertEqual(cr.pending_count(), 0)

    def test_empty_memory_sends_nothing(self):
        cr._send_and_clear(_MailCfg())
        self.assertFalse(_FakeSMTP.sent)

    def test_partial_failure_only_keeps_failed_batch(self):
        self._fill(2, size=1)
        orig = _FakeSMTP.sendmail
        calls = {"n": 0}

        def flaky(self_, frm, to, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("first fails")
            return orig(self_, frm, to, data)

        with mock.patch.object(cr, "MAX_MAIL_BYTES", 1):  # 每张一封
            with mock.patch.object(_FakeSMTP, "sendmail", flaky):
                cr._send_and_clear(_MailCfg())
        self.assertEqual([n for n, _ in cr._snapshot()], ["cap_0.jpg"])
        self.assertEqual(len(_FakeSMTP.sent), 1)          # 第二封成功了


if __name__ == "__main__":
    unittest.main()
