"""app/updater.py 的版本比对与下载地址解析测试。

只测不联网的纯函数：版本比对接错了会直接导致「反复提示更新」或「有更新不提示」，
这两种都是用户一眼能看见的 bug，而它又最容易在一次小重构里被写坏。
"""
from __future__ import annotations

import unittest

from app.updater import (
    _clean_version,
    _tag_download_urls,
    compare_versions,
    manifest_version,
    resolve_download_urls,
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


if __name__ == "__main__":
    unittest.main()
