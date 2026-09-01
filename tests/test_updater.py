"""app/updater.py 的版本比对与下载地址解析测试。

只测不联网的纯函数：版本比对接错了会直接导致「反复提示更新」或「有更新不提示」，
这两种都是用户一眼能看见的 bug，而它又最容易在一次小重构里被写坏。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.updater import (
    _asset_download_url,
    _clean_version,
    _release_version,
    _tag_download_urls,
    _write_upgrade_bat,
    compare_versions,
    manifest_version,
    resolve_download_urls,
    resolve_update_sources,
)


class TestCleanVersion(unittest.TestCase):
    def test_strips_prefix_and_space(self):
        self.assertEqual(_clean_version("v3.0.1"), "3.0.1")
        self.assertEqual(_clean_version("V3.0.1"), "3.0.1")
        self.assertEqual(_clean_version("  3.0.1  "), "3.0.1")

    def test_only_leading_prefix(self):
        """中间的 v 是版本的一部分（如 1.0.0v2），不能被削掉。"""
        self.assertEqual(_clean_version("1.0.0v2"), "1.0.0v2")

    def test_empty(self):
        self.assertEqual(_clean_version(""), "")
        self.assertEqual(_clean_version(None), "")


class TestCompareVersions(unittest.TestCase):
    def test_less_equal_greater(self):
        self.assertEqual(compare_versions("1.0.0", "1.0.1"), -1)
        self.assertEqual(compare_versions("1.0.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("1.0.1", "1.0.0"), 1)

    def test_numeric_not_lexicographic(self):
        """3.0.10 > 3.0.9：按字符串比会得出 3.0.10 < 3.0.9 的错误结论。"""
        self.assertEqual(compare_versions("3.0.9", "3.0.10"), -1)
        self.assertEqual(compare_versions("3.0.10", "3.0.9"), 1)

    def test_v_prefix_is_ignored(self):
        self.assertEqual(compare_versions("v3.0.1", "3.0.1"), 0)
        self.assertEqual(compare_versions("v3.0.1", "v3.0.2"), -1)

    def test_different_lengths_padded(self):
        self.assertEqual(compare_versions("1.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("1", "1.0.1"), -1)

    def test_non_numeric_segments_become_zero(self):
        self.assertEqual(compare_versions("abc", "0"), 0)
        self.assertEqual(compare_versions("", "1.0.0"), -1)

    def test_symmetry(self):
        for a, b in [("1.2.3", "1.2.4"), ("2.0", "10.0"), ("v1", "1.0.0")]:
            self.assertEqual(compare_versions(a, b), -compare_versions(b, a))


class TestManifestVersion(unittest.TestCase):
    def test_reads_version(self):
        self.assertEqual(manifest_version({"version": "3.0.2"}), "3.0.2")

    def test_missing_or_blank_is_none(self):
        self.assertIsNone(manifest_version(None))
        self.assertIsNone(manifest_version({}))
        self.assertIsNone(manifest_version({"version": "   "}))
        self.assertIsNone(manifest_version({"version": None}))


class TestDownloadUrls(unittest.TestCase):
    def test_tag_urls_try_both_forms(self):
        urls = _tag_download_urls("v3.0.1")
        self.assertEqual(len(urls), 2)
        self.assertIn("releases/download/v3.0.1/", urls[0])
        self.assertIn("releases/download/3.0.1/", urls[1])
        # exe 文件名是中文，必须被 URL 编码
        self.assertNotIn("清风", urls[0])

    def test_custom_url_wins(self):
        self.assertEqual(resolve_download_urls({"download_url": "https://x/a.exe"}, "1.0"),
                         ["https://x/a.exe"])

    def test_falls_back_to_tag_urls(self):
        urls = resolve_download_urls({}, "1.0")
        self.assertEqual(len(urls), 2)

    def test_blank_custom_url_ignored(self):
        self.assertEqual(len(resolve_download_urls({"download_url": "  "}, "1.0")), 2)


class TestAssetUrl(unittest.TestCase):
    """GitHub/Gitee 附件直链挑选：精确名优先，任意 .exe 兜底。"""

    def test_exact_filename_wins(self):
        assets = [
            {"name": "default.exe", "browser_download_url": "https://a/default.exe"},
            {"name": "清风自动化键鼠工具.exe", "browser_download_url": "https://a/real.exe"},
        ]
        self.assertEqual(_asset_download_url(assets), "https://a/real.exe")

    def test_any_exe_fallback(self):
        """GitHub 资产名是 ASCII（QingFeng_KeyMouse_Tool.exe），不能精确匹配也要能挑中。"""
        assets = [{"name": "QingFeng_KeyMouse_Tool.exe",
                   "browser_download_url": "https://a/x.exe"}]
        self.assertEqual(_asset_download_url(assets), "https://a/x.exe")

    def test_skips_non_exe_and_partials(self):
        assets = [
            {"name": "readme.txt", "browser_download_url": "https://a/r.txt"},
            {"name": "old.exe.download", "browser_download_url": "https://a/o.exe.download"},
            {"name": "bad", "browser_download_url": "https://a/bad"},
        ]
        self.assertIsNone(_asset_download_url(assets))

    def test_empty(self):
        self.assertIsNone(_asset_download_url(None))
        self.assertIsNone(_asset_download_url([]))


class TestReleaseVersion(unittest.TestCase):
    """版本号取 release 显示名 name 优先（GitHub tag 是中文），tag_name 兜底。"""

    def test_name_preferred(self):
        self.assertEqual(_release_version(
            {"name": "v3.0.5", "tag_name": "键鼠自动化"}), "v3.0.5")

    def test_tag_fallback(self):
        self.assertEqual(_release_version({"tag_name": "v3.0.1"}), "v3.0.1")

    def test_empty(self):
        self.assertEqual(_release_version({}), "")
        self.assertEqual(_release_version(None), "")


class TestResolveSources(unittest.TestCase):
    """更新源优先级：GitHub -> Gitee -> manifest；均用 mock 不联网。"""

    def test_github_first_and_no_gitee_call(self):
        rel = {"name": "v3.1.0", "tag_name": "键鼠自动化",
               "assets": [{"name": "QingFeng_KeyMouse_Tool.exe",
                           "browser_download_url": "https://gh/x/1.exe"}]}
        with patch("app.updater.fetch_latest_release",
                   return_value=rel) as m, \
             patch("app.updater.fetch_manifest") as fm:
            ver, urls = resolve_update_sources()
            self.assertEqual(ver, "v3.1.0")
            self.assertEqual(urls, ["https://gh/x/1.exe"])
            m.assert_called_once()
            fm.assert_not_called()

    def test_fallback_to_gitee(self):
        """GitHub 失败（返回 None）时走 Gitee，并补拼 Gitee 附件候选。"""
        def fake_fetch(api="", timeout=15.0):
            if "github" in api:
                return None
            return {"tag_name": "v2.0.0",
                    "assets": [{"name": "清风自动化键鼠工具.exe",
                                "browser_download_url": "https://gitee/2.exe"}]}
        with patch("app.updater.fetch_latest_release", side_effect=fake_fetch) as m, \
             patch("app.updater.fetch_manifest") as fm:
            ver, urls = resolve_update_sources()
            self.assertEqual(ver, "v2.0.0")
            self.assertEqual(urls[0], "https://gitee/2.exe")
            self.assertGreaterEqual(len(urls), 3)   # 直链 + 2 个拼地址候选
            self.assertEqual(m.call_count, 2)       # 先 GitHub 后 Gitee
            fm.assert_not_called()

    def test_fallback_to_manifest(self):
        with patch("app.updater.fetch_latest_release", return_value=None) as m, \
             patch("app.updater.fetch_manifest",
                   return_value={"version": "9.9.9",
                                 "download_url": "https://m/a.exe"}) as fm:
            ver, urls = resolve_update_sources()
            self.assertEqual(ver, "9.9.9")
            self.assertEqual(urls, ["https://m/a.exe"])
            self.assertEqual(m.call_count, 2)
            fm.assert_called_once()


class TestUpgradeBat(unittest.TestCase):
    """升级收尾 bat 的生成（PyInstaller onefile 升级的关键环节）。

    内容错了会导致升级后旧 exe 删不掉 / 新 exe 没启动 / 中文路径乱码，
    这类问题只在用户点「重启升级」时暴露，必须钉测试。
    """

    def test_bat_is_pure_ascii_and_derives_paths_at_runtime(self):
        exe = os.path.join(tempfile.gettempdir(), "清风自动化键鼠工具.exe")
        new = exe + ".new"
        bat = _write_upgrade_bat(exe, new)
        try:
            self.assertTrue(os.path.isfile(bat), "应生成 bat 文件")
            with open(bat, "rb") as f:
                raw = f.read()
            # 纯 ASCII 是硬约束：cmd 按 ANSI 代码页解析 bat，GBK/UTF-8 不一致
            # 时中文路径全乱码（曾经的事故）；含任何非 ASCII 字节则解码直接失败
            content = raw.decode("ascii")
            self.assertNotIn("清风", content)      # 中文路径不写进文本
            self.assertNotIn(exe, content)
            # 运行时用 %~f0（bat 名 = <exe>.upgrade.bat）反推 exe 路径
            self.assertIn('set "EXE=%SELF:.upgrade.bat=%"', content)
            # 等待循环：del 探测（删得动=进程退出且锁释放）+ ping 延时
            # （timeout 在无控制台的分离进程里会报 Input redirection 错误失效）
            self.assertIn(":wait", content)
            self.assertIn("goto wait", content)
            self.assertIn("del /f /q", content)
            self.assertIn("ping -n 2 127.0.0.1", content)
            self.assertIn("geq 120", content)      # 超时兜底，不死等
            # 顶替、启动、自删
            self.assertIn("move /y", content)
            self.assertIn('start "" "%EXE%"', content)
            self.assertIn('del /f /q "%~f0"', content)
        finally:
            if os.path.isfile(bat):
                os.remove(bat)

    def test_bat_failure_returns_empty(self):
        bad = os.path.join("Z:\\nonexistent_dir_xyz", "a.exe")  # 不存在的盘/目录
        self.assertEqual(_write_upgrade_bat(bad, bad + ".new"), "")


if __name__ == "__main__":
    unittest.main()
