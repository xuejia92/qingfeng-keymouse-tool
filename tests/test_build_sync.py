# -*- coding: utf-8 -*-
"""打包脚本的「同步数据到 dist」功能测试（build.py）。

隔离方式：把 build.BASE_DIR / build.DIST_DIR 指到临时目录，绝不碰真实 dist。
覆盖：新增 / 更新 / 幂等（已最新不重复复制）/ 递归子目录 /
      dist 里多余文件只报告不删 / 源目录不存在跳过 / 目标比源新时先备份；
      以及 task_board.json 的**双向**同步（谁新以谁为准 + 两处安全兜底）。
"""
from __future__ import annotations

import contextlib
import glob
import io
import json
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


class TestSyncTaskBoard(unittest.TestCase):
    """task_board.json 在「工作区」与「dist」之间的**双向**同步。

    需求背景：源码模式（restart.bat）写工作区那份、打包后的 exe 写 dist 那份，
    两个入口都会改同一个看板，所以必须双向 —— 谁最后改的就以谁为准。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_board_sync_")
        self.base = os.path.join(self.tmp, "ws")
        self.dist = os.path.join(self.tmp, "dist")
        os.makedirs(self.base)
        os.makedirs(self.dist)
        mock.patch.object(build, "BASE_DIR", self.base).start()
        mock.patch.object(build, "DIST_DIR", self.dist).start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 构造 / 读取 ----
    @staticmethod
    def _board(titles):
        return {"version": 1, "columns": [
            {"id": "todo", "name": "待办", "done": False,
             "tasks": [{"id": f"t{i}", "title": t, "note": "", "priority": "normal",
                        "due": "", "created": "2026-10-06 00:00:00"}
                       for i, t in enumerate(titles)]}]}

    def _path(self, root):
        return os.path.join(root, build.TASK_BOARD_FILE)

    def _write(self, root, titles, mtime=None):
        with open(self._path(root), "w", encoding="utf-8") as fh:
            json.dump(self._board(titles), fh, ensure_ascii=False)
        if mtime is not None:
            os.utime(self._path(root), (mtime, mtime))
        return self._path(root)

    def _titles(self, root):
        with open(self._path(root), encoding="utf-8") as fh:
            data = json.load(fh)
        return [t["title"] for t in data["columns"][0]["tasks"]]

    def _backups(self):
        return glob.glob(os.path.join(self.dist, "_sync_backup", "*",
                                      build.TASK_BOARD_FILE))

    # ---- 基本分支 ----
    def test_skips_when_no_dist_dir(self):
        """没打过包（没有 dist）→ 跳过，且不会凭空建出 dist 目录。"""
        shutil.rmtree(self.dist)
        self._write(self.base, ("甲",))
        self.assertEqual(build.sync_task_board(), "no-dist")
        self.assertFalse(os.path.exists(self.dist))

    def test_nothing_on_either_side(self):
        self.assertEqual(build.sync_task_board(), "none")

    def test_workspace_only_copies_to_dist(self):
        self._write(self.base, ("甲", "乙"))
        self.assertEqual(build.sync_task_board(), "ws-to-dist")
        self.assertEqual(self._titles(self.dist), ["甲", "乙"])

    def test_dist_only_copies_back_to_workspace(self):
        """★ 双向：只有 dist 有（在打包版里加过任务）→ 回填工作区。"""
        self._write(self.dist, ("在exe里加的",))
        self.assertEqual(build.sync_task_board(), "dist-to-ws")
        self.assertEqual(self._titles(self.base), ["在exe里加的"])

    def test_identical_is_a_noop(self):
        self._write(self.base, ("甲",))
        shutil.copy2(self._path(self.base), self._path(self.dist))
        self.assertEqual(build.sync_task_board(), "same")

    def test_newer_workspace_wins(self):
        self._write(self.base, ("新的",), mtime=2_000_000_000)
        self._write(self.dist, ("旧的",), mtime=1_000_000_000)
        self.assertEqual(build.sync_task_board(), "ws-to-dist")
        self.assertEqual(self._titles(self.dist), ["新的"])

    def test_newer_dist_wins(self):
        self._write(self.base, ("旧的",), mtime=1_000_000_000)
        self._write(self.dist, ("新的",), mtime=2_000_000_000)
        self.assertEqual(build.sync_task_board(), "dist-to-ws")
        self.assertEqual(self._titles(self.base), ["新的"])

    def test_second_run_is_idempotent(self):
        """同步一次后两边一致，再跑就 no-op —— 不会来回 ping-pong。"""
        self._write(self.base, ("甲",), mtime=2_000_000_000)
        self._write(self.dist, ("乙",), mtime=1_000_000_000)
        self.assertEqual(build.sync_task_board(), "ws-to-dist")
        self.assertEqual(build.sync_task_board(), "same")

    def test_no_tmp_file_left_behind(self):
        self._write(self.base, ("甲",))
        build.sync_task_board()
        self.assertFalse(os.path.exists(self._path(self.dist) + ".tmp"))

    # ---- 安全兜底 ----
    def test_invalid_source_never_overwrites_a_good_file(self):
        """★ 源不是合法看板（手改坏 / 写了一半）→ 跳过，不拿它覆盖对面那份。"""
        self._write(self.dist, ("好任务",), mtime=1_000_000_000)
        with open(self._path(self.base), "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "columns": [')          # 半截
        os.utime(self._path(self.base), (2_000_000_000, 2_000_000_000))
        self.assertEqual(build.sync_task_board(), "invalid-src")
        self.assertEqual(self._titles(self.dist), ["好任务"], "好的那份不能被覆盖")

    def test_backs_up_when_the_overwritten_copy_has_more_tasks(self):
        """★ 被覆盖的那份任务更多（时间戳不可信 / 用户回滚过）→ 先备份再覆盖。"""
        self._write(self.base, ("甲",), mtime=2_000_000_000)              # 新但少
        self._write(self.dist, ("甲", "乙", "丙"), mtime=1_000_000_000)   # 旧但多
        self.assertEqual(build.sync_task_board(), "ws-to-dist")
        self.assertEqual(self._titles(self.dist), ["甲"])
        backups = self._backups()
        self.assertEqual(len(backups), 1, "被覆盖的那份必须先备份")
        with open(backups[0], encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual([t["title"] for t in saved["columns"][0]["tasks"]],
                         ["甲", "乙", "丙"])

    def test_no_backup_when_winner_has_at_least_as_many_tasks(self):
        self._write(self.base, ("甲", "乙", "丙"), mtime=2_000_000_000)
        self._write(self.dist, ("甲",), mtime=1_000_000_000)
        build.sync_task_board()
        self.assertEqual(self._backups(), [])

    # ---- 输出与接线 ----
    def test_quiet_prints_nothing(self):
        self._write(self.base, ("甲",))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            build.sync_task_board(quiet=True)
        self.assertEqual(buf.getvalue(), "")

    def test_prints_the_direction(self):
        self._write(self.base, ("甲",))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            build.sync_task_board()
        out = buf.getvalue()
        self.assertIn("[同步]", out)
        self.assertIn("工作区 → dist", out)

    def test_count_helper(self):
        self.assertEqual(build.task_board_count(self._board(("甲", "乙"))), 2)
        self.assertEqual(build.task_board_count(self._board(())), 0)
        self.assertIsNone(build.task_board_count({"version": 1}))
        self.assertIsNone(build.task_board_count(None))

    def test_wired_into_build_entry_points(self):
        """源码级契约：--sync-only 与打包成功后各调一次（否则改了容易漏接线）。"""
        with open(os.path.join(os.path.dirname(build.__file__), "build.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertGreaterEqual(src.count("sync_task_board()"), 2)


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
