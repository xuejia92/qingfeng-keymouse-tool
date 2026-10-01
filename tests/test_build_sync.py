# -*- coding: utf-8 -*-
"""打包脚本的「同步 templates / flows 到 dist」功能测试（build.py）。

隔离方式：把 build.BASE_DIR / build.DIST_DIR 指到临时目录，绝不碰真实 dist。
覆盖：新增 / 更新 / 幂等（已最新不重复复制）/ 递归子目录 /
      dist 里多余文件只报告不删 / 源目录不存在跳过 / 目标比源新时先备份。
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import build


class TestSyncOneDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_build_sync_")
        self.base = os.path.join(self.tmp, "ws")
        self.dist = os.path.join(self.tmp, "dist")
        os.makedirs(self.base)
        os.makedirs(self.dist)
        mock.patch.object(build, "BASE_DIR", self.base).start()
        mock.patch.object(build, "DIST_DIR", self.dist).start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, root, rel, text, mtime=None):
        p = os.path.join(root, rel)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def _src(self, name="templates"):
        return os.path.join(self.base, name)

    def _dst(self, name="templates"):
        return os.path.join(self.dist, name)

    def test_adds_new_file(self):
        self._write(self._src(), "a.png", "aaa")
        st = build._sync_one_dir("templates")
        self.assertEqual((st["added"], st["updated"]), (1, 0))
        self.assertTrue(os.path.isfile(os.path.join(self._dst(), "a.png")))

    def test_updates_when_size_differs(self):
        self._write(self._src(), "a.png", "aaaa")
        self._write(self._dst(), "a.png", "a")
        st = build._sync_one_dir("templates")
        self.assertEqual(st["updated"], 1)
        with open(os.path.join(self._dst(), "a.png"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "aaaa")

    def test_updates_when_source_newer_same_size(self):
        self._write(self._src(), "a.png", "aa", mtime=2_000_000_000)
        self._write(self._dst(), "a.png", "bb", mtime=1_000_000_000)
        st = build._sync_one_dir("templates")
        self.assertEqual(st["updated"], 1)
        with open(os.path.join(self._dst(), "a.png"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "aa")

    def test_idempotent_when_unchanged(self):
        """第二次运行（文件没变）应全部算「已最新」，不重复复制。"""
        self._write(self._src(), "a.png", "aa")
        build._sync_one_dir("templates")
        st = build._sync_one_dir("templates")
        self.assertEqual((st["added"], st["updated"], st["same"]), (0, 0, 1))

    def test_recurses_into_subdirs(self):
        self._write(self._src(), os.path.join("jietu", "b.png"), "bbb")
        st = build._sync_one_dir("templates")
        self.assertEqual(st["added"], 1)
        self.assertTrue(os.path.isfile(os.path.join(self._dst(), "jietu", "b.png")))

    def test_extra_files_reported_but_kept(self):
        self._write(self._src(), "keep.png", "k")
        self._write(self._dst(), "old_only.png", "o")
        st = build._sync_one_dir("templates")
        self.assertEqual(st["extra"], ["old_only.png"])
        self.assertTrue(os.path.isfile(os.path.join(self._dst(), "old_only.png")))

    def test_missing_source_dir_skipped(self):
        st = build._sync_one_dir("not_exist")
        self.assertEqual((st["added"], st["updated"], st["same"]), (0, 0, 0))

    def test_backs_up_newer_target_before_overwrite(self):
        """dist 里那份更新（像直接在程序里改过）→ 先备份再覆盖，不丢改动。"""
        self._write(self._src(), "p.json", "workspace", mtime=1_000_000_000)
        self._write(self._dst(), "p.json", "edited-in-app", mtime=2_000_000_000)
        backup = os.path.join(self.tmp, "backup")
        st = build._sync_one_dir("templates", backup_root=backup)
        self.assertEqual(st["added"], 0)
        self.assertEqual(st["backed_up"], ["p.json"])
        b = os.path.join(backup, "templates", "p.json")
        self.assertTrue(os.path.isfile(b))
        with open(b, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "edited-in-app")     # 备份的是被覆盖的那份
        with open(os.path.join(self._dst(), "p.json"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "workspace")          # 目标已是最新

    def test_no_backup_when_target_older(self):
        self._write(self._src(), "p.json", "new", mtime=2_000_000_000)
        self._write(self._dst(), "p.json", "old", mtime=1_000_000_000)
        backup = os.path.join(self.tmp, "backup")
        st = build._sync_one_dir("templates", backup_root=backup)
        self.assertEqual(st["backed_up"], [])
        self.assertFalse(os.path.exists(backup))


class TestSyncDataDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_build_sync2_")
        self.base = os.path.join(self.tmp, "ws")
        self.dist = os.path.join(self.tmp, "dist")
        os.makedirs(self.base)
        os.makedirs(self.dist)
        mock.patch.object(build, "BASE_DIR", self.base).start()
        mock.patch.object(build, "DIST_DIR", self.dist).start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_syncs_both_default_dirs(self):
        self._write(os.path.join(self.base, "templates"), "t.png", "t")
        self._write(os.path.join(self.base, "flows"), "f.json", "f")
        build.sync_data_dirs()
        self.assertTrue(os.path.isfile(os.path.join(self.dist, "templates", "t.png")))
        self.assertTrue(os.path.isfile(os.path.join(self.dist, "flows", "f.json")))

    def test_default_names_are_templates_and_flows(self):
        self.assertEqual(build.SYNC_DIRS, ("templates", "flows"))


class TestSyncDataDirsQuiet(unittest.TestCase):
    """quiet=True 的静默模式与返回值汇总（restart_watchdog 每次启动都会调它）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_build_sync3_")
        self.base = os.path.join(self.tmp, "ws")
        self.dist = os.path.join(self.tmp, "dist")
        os.makedirs(self.base)
        os.makedirs(self.dist)
        mock.patch.object(build, "BASE_DIR", self.base).start()
        mock.patch.object(build, "DIST_DIR", self.dist).start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _run_quiet(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            st = build.sync_data_dirs(quiet=True)
        return st, buf.getvalue()

    def test_quiet_prints_nothing_but_returns_summary(self):
        self._write(os.path.join(self.base, "templates"), "a.png", "x")
        st, out = self._run_quiet()
        self.assertEqual(out, "")
        self.assertEqual(st["added"], 1)

    def test_summary_aggregates_both_dirs(self):
        self._write(os.path.join(self.base, "templates"), "a.png", "x")
        self._write(os.path.join(self.base, "flows"), "b.json", "y")
        st, _out = self._run_quiet()
        self.assertEqual(st["added"], 2)

    def test_summary_counts_each_category(self):
        # templates：1 个新增 + 1 个更新 + 1 个已最新；flows：源目录里没有的 dist 文件 → extra
        # （源目录不存在时整个目录会被跳过，连 extra 都不统计，所以这里要建出源 flows）
        self._write(os.path.join(self.base, "templates"), "new.png", "n")
        self._write(os.path.join(self.base, "templates"), "chg.png", "changed-longer")
        self._write(os.path.join(self.base, "templates"), "same.png", "s")
        self._write(os.path.join(self.dist, "templates"), "chg.png", "old")
        self._write(os.path.join(self.dist, "templates"), "same.png", "s")
        self._write(os.path.join(self.base, "flows"), "keep.json", "k")
        self._write(os.path.join(self.dist, "flows"), "gone.json", "g")
        st, _out = self._run_quiet()
        self.assertEqual(st["added"], 2)      # new.png + keep.json
        self.assertEqual(st["updated"], 1)
        self.assertEqual(st["same"], 1)
        self.assertEqual(st["extra"], 1)

    def test_non_quiet_still_prints_header(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            build.sync_data_dirs()
        self.assertIn("[同步]", buf.getvalue())


@unittest.skipUnless(os.name == "nt", "tasklist 只在 Windows 上有")
class TestIsRunning(unittest.TestCase):
    """exe 占用检测：中文 Windows 的 tasklist 输出是 GBK，解码失败绝不能把打包打断。"""

    def test_detects_running_exe(self):
        fake = mock.Mock(stdout="映像名称    PID\n清风自动化键鼠工具.exe   1234\n")
        with mock.patch("subprocess.run", return_value=fake) as run:
            self.assertTrue(build._is_running("清风自动化键鼠工具.exe"))
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("encoding"), "mbcs")   # 必须显式 mbcs，别用默认 utf-8
        self.assertEqual(kwargs.get("errors"), "replace")

    def test_not_running(self):
        fake = mock.Mock(stdout="信息: 没有运行的任务匹配指定标准。\n")
        with mock.patch("subprocess.run", return_value=fake):
            self.assertFalse(build._is_running("清风自动化键鼠工具.exe"))

    def test_decode_failure_returns_false_without_crash(self):
        """stdout 为 None（解码失败）时不能抛 AttributeError。"""
        fake = mock.Mock(stdout=None)
        with mock.patch("subprocess.run", return_value=fake):
            self.assertFalse(build._is_running("x.exe"))

    def test_subprocess_exception_returns_false(self):
        with mock.patch("subprocess.run", side_effect=OSError("no tasklist")):
            self.assertFalse(build._is_running("x.exe"))


if __name__ == "__main__":
    unittest.main()
