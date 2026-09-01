"""测试公共工具：把 app.config 的运行期目录重定向到临时目录。

为什么需要它：config.py 把 CONFIG_PATH / FLOWS_DIR 等做成了模块级常量，
直接在真实项目目录上跑测试会污染用户的 config.json 和 flows/。
"""
from __future__ import annotations

import os
import shutil
import tempfile

from app import config as config_mod


class TempConfigPaths:
    """把 app.config 的几个运行期路径切到临时目录，退出时还原。

    用法：
        with TempConfigPaths() as tmp:
            cfg = AppConfig.load()      # 读写的是 tmp 里的空目录
    """

    _KEYS = ("BASE_DIR", "CONFIG_PATH", "FLOWS_DIR", "TEMPLATE_DIR", "LOG_PATH")

    def __init__(self):
        self.tmp = ""
        self._saved = {}

    def __enter__(self) -> str:
        self.tmp = tempfile.mkdtemp(prefix="qf_test_")
        for k in self._KEYS:
            self._saved[k] = getattr(config_mod, k)
        config_mod.BASE_DIR = self.tmp
        config_mod.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        config_mod.FLOWS_DIR = os.path.join(self.tmp, "flows")
        config_mod.TEMPLATE_DIR = os.path.join(self.tmp, "templates")
        config_mod.LOG_PATH = os.path.join(self.tmp, "app.log")
        return self.tmp

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(config_mod, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def write_json(path: str, data) -> None:
    import json

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
