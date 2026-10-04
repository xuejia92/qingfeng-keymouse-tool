# -*- coding: utf-8 -*-
"""全局约定：测试里**不许建裸的 `QCoreApplication`**。

为什么值得单开一个文件钉住
--------------------------
`QApplication.instance()` 返回的是"当前那个 QCoreApplication"——如果某个用例先建了
**非 GUI** 的 `QCoreApplication`，后面同进程里的 `QApplication.instance()` 就会把它
当成"应用已存在"而**不再创建 GUI 应用**。此后任何 GUI 调用
（`QApplication.activeModalWidget` / `QCursor.pos` / `QPixmap` / `QMenu`…）
都会直接**访问违规、崩掉整个进程**：没有 traceback、退出码 139，
看起来就像"测试跑一半自己没了"，极难定位。

实测（2026-10-04）：
- 先 `QCoreApplication([])`、再 `QApplication.activeModalWidget()` → 必崩；
- `python -m unittest tests.test_mouse_menu tests.test_middle_menu_trigger`
  这个顺序必崩（先跑的那个文件建的是 QCoreApplication）。
  全量 `discover` 只是**靠文件名的字母顺序侥幸躲过**（GUI 测试恰好排在前面），
  换个名字、或只跑子集，雷就会炸。

正确写法（两种都行）：
    from PySide6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
"""
from __future__ import annotations

import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FORBIDDEN = "QCoreApplication("


class TestNoBareCoreApplication(unittest.TestCase):

    def _test_sources(self):
        for name in sorted(os.listdir(TESTS_DIR)):
            if not name.endswith(".py") or name == os.path.basename(__file__):
                continue
            path = os.path.join(TESTS_DIR, name)
            with open(path, encoding="utf-8") as handle:
                yield name, handle.read()

    def test_no_bare_qcoreapplication_construction(self):
        offenders = [name for name, src in self._test_sources()
                     if FORBIDDEN in src]
        self.assertEqual(
            offenders, [],
            "这些测试文件在建裸的 QCoreApplication —— 会让同进程后续所有 GUI 用例"
            f"崩进程（无 traceback）：{offenders}。请改用 "
            "QApplication.instance() or QApplication([])")

    def test_guard_still_matches_the_dangerous_pattern(self):
        """守卫自身要有效：换行/空格写法也得认得出来，别把 `QCoreApplication(` 写散。

        这里不做复杂解析，只确保约定所依赖的字符串确实出现在会崩的那种写法里。
        """
        sample = "from PySide6.QtCore import QCoreApplication\nQCoreApplication([])\n"
        self.assertIn(FORBIDDEN, sample)


if __name__ == "__main__":
    unittest.main()
