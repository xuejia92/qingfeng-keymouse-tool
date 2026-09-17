"""this.cmd 的静态契约测试。

为什么需要它：this.cmd 是**双击运行**的发布脚本，它的三个坑都不会在开发时
暴露出来，而一旦被"顺手清理"就会重现「双击一下窗口一闪、什么都没发生」：

1. 本目录属主不是当前 Windows 用户，git 2.35+ 直接拒绝一切操作
   （fatal: detected dubious ownership in repository）。必须带 safe.directory 放行。
2. 本机没有配置任何 git 提交身份（没有 ~/.gitconfig），commit 会报
   "Author identity unknown"。脚本必须自己兜住。
3. 分支名不能写死：仓库只有 main，历史脚本里写的是 origin master，
   每次 pull 都 fatal。另外还要防 Git 自带的 usr\\bin 里的 Unix find/findstr
   遮蔽 System32 同名程序（参数含义完全不同，会去遍历整个磁盘）。

这里只做**纯文本检查**，不执行脚本 —— 执行会真的提交并推送。
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = os.path.join(ROOT, "this.cmd")


class TestThisCmdSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CMD, "rb") as f:
            cls.raw = f.read()
        cls.text = cls.raw.decode("gbk")          # 必须能用 GBK 解码（脚本 chcp 936）
        cls.lines = cls.text.splitlines()

    # ---- 文件本身 ----

    def test_file_exists(self):
        self.assertTrue(os.path.isfile(CMD))

    def test_decodes_as_gbk(self):
        """脚本第一行 chcp 936，文件必须是 GBK —— 否则中文提示全成乱码。"""
        self.assertIn("chcp 936", self.text)
        self.raw.decode("gbk")                    # 解不开会抛异常

    def test_no_bom_and_uniform_crlf(self):
        """BOM 会让 @echo off 失效并多打印一行；混用 LF 在个别 cmd 版本上会吞命令。"""
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "bat 不该有 BOM")
        self.assertEqual(self.raw.replace(b"\r\n", b"").count(b"\n"), 0,
                         "存在非 CRLF 的换行")
        self.assertGreater(self.raw.count(b"\r\n"), 20)

    # ---- 坑 1：仓库属主 / 权限 ----

    def test_passes_safe_directory(self):
        self.assertIn("safe.directory", self.text)

    def test_safe_directory_is_per_command_not_global(self):
        """用 -c 传参，不改用户的全局 git 配置。

        提示文字里可以建议用户手动 `git config --global --add safe.directory`，
        但脚本**自己**绝不能去改全局配置 —— 所以那种行必须都是 echo。
        """
        self.assertIn('-c "safe.directory=%REPO%"', self.text)
        for line in self.lines:
            if "config --global --add safe.directory" in line:
                self.assertTrue(line.strip().startswith("echo"),
                                f"这行会真的改全局配置：{line.strip()!r}")

    def test_has_access_failure_branch(self):
        self.assertIn(":no_access", self.text)

    # ---- 坑 2：提交身份 ----

    def test_ensures_commit_identity(self):
        self.assertIn("config user.email", self.text)
        self.assertIn("config --local user.name", self.text)
        self.assertIn("config --local user.email", self.text)

    def test_identity_written_only_after_checkonly_branch(self):
        """--check 必须只读：身份写入要在 CHECKONLY 跳转之后。"""
        i_check = next(i for i, l in enumerate(self.lines)
                       if "if defined CHECKONLY goto :do_check" in l)
        i_write = next(i for i, l in enumerate(self.lines)
                       if "config --local user.name" in l)
        self.assertLess(i_check, i_write, "--check 分支前不该改任何配置")

    # ---- 坑 3：分支名不写死 + 工具遮蔽 ----

    def test_no_hardcoded_branch(self):
        """历史脚本写的是 origin master，而仓库只有 main。"""
        self.assertNotRegex(self.text, r"origin\s+master")
        self.assertNotRegex(self.text, r"origin\s+main")
        self.assertIn("BRANCH", self.text)

    def test_branch_detected_via_symbolic_ref(self):
        """还没提交过的分支只有 symbolic-ref 拿得到名字（rev-parse 会返回 HEAD）。"""
        self.assertIn("symbolic-ref --short HEAD", self.text)

    def test_uses_system32_find(self):
        """Git 的 usr\\bin 里也有 find.exe，参数含义完全不同。"""
        self.assertIn("%SystemRoot%\\System32\\find.exe", self.text)
        self.assertIn("%SystemRoot%\\System32\\findstr.exe", self.text)
        self.assertNotRegex(self.text, r"\|\s*find(\.exe)?\s+/[cv]", "裸 find 会被 MSYS 抢走")

    # ---- 错误可见性 ----

    def test_pauses_so_errors_stay_on_screen(self):
        """原来的脚本双击后窗口一闪就关，用户看不到任何错误。"""
        self.assertIn("pause", self.text)
        i_halt = next(i for i, l in enumerate(self.lines) if l.strip() == ":halt")
        self.assertIn("pause", "\n".join(self.lines[i_halt:i_halt + 5]))

    def test_has_check_and_help_entry(self):
        self.assertIn('"--check"', self.text)
        self.assertIn('"--help"', self.text)

    # ---- 流程步骤齐全 ----

    def test_core_git_steps_present(self):
        for needle in ("rev-parse --is-inside-work-tree", "fetch origin",
                       "add -A", 'commit -m "%MSG%"', "merge --ff-only",
                       "push origin"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)

    def test_never_force_pushes(self):
        """发布脚本里出现强推迟早会吃掉别人的提交。"""
        self.assertNotIn("--force", self.text)
        self.assertNotIn("-f origin", self.text)


if __name__ == "__main__":
    unittest.main()
