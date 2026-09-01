# -*- coding: utf-8 -*-
"""发布工具 publish_tool.py 的版本清单同步测试。

dist/config.json 是完整程序配置（含邮箱、找图任务等），发布成功后只允许
改 version 字段、其余必须原样保留——改坏了就是丢用户配置，必须钉测试。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import publish_tool


class TestSyncManifestVersion(unittest.TestCase):
    """发布成功后 dist/config.json 的 version 同步行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qf_pub_test_")
        self._patcher = mock.patch.object(publish_tool, "BASE_DIR", self.tmp)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.dist = os.path.join(self.tmp, "dist")
        os.makedirs(self.dist, exist_ok=True)
        self.logs: list[str] = []
        self.manifest = os.path.join(self.dist, "config.json")

    def _write(self, data):
        with open(self.manifest, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _read(self):
        with open(self.manifest, encoding="utf-8") as f:
            return json.load(f)

    def test_syncs_version_and_keeps_other_fields(self):
        """只改 version（自动补 v 前缀），其余字段原样保留。"""
        self._write({"version": "v3.4.0", "mail_user": "a@qq.com",
                     "备注": "中文值不被转义",
                     "clicker": {"interval_ms": 100}})
        publish_tool.sync_manifest_version("3.5.0", self.logs.append)
        data = self._read()
        self.assertEqual(data["version"], "v3.5.0")
        self.assertEqual(data["mail_user"], "a@qq.com")
        self.assertEqual(data["备注"], "中文值不被转义")
        self.assertEqual(data["clicker"], {"interval_ms": 100})

    def test_v_prefix_input_unchanged(self):
        self._write({"version": "v3.4.0"})
        publish_tool.sync_manifest_version("v3.5.0", self.logs.append)
        self.assertEqual(self._read()["version"], "v3.5.0")

    def test_missing_manifest_warns_only(self):
        """清单不存在：不抛错、不新建文件，只告警（发布已成功不能被阻断）。"""
        publish_tool.sync_manifest_version("3.5.0", self.logs.append)
        self.assertFalse(os.path.isfile(self.manifest))
        self.assertTrue(any("⚠️" in line for line in self.logs))

    def test_broken_json_warns_and_keeps_file(self):
        """清单损坏：不抛错、原文件不被破坏，只告警。"""
        with open(self.manifest, "w", encoding="utf-8") as f:
            f.write("{not json")
        publish_tool.sync_manifest_version("3.5.0", self.logs.append)
        with open(self.manifest, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")
        self.assertTrue(any("失败" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
