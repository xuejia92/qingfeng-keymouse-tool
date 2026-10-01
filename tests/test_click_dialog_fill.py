# -*- coding: utf-8 -*-
"""点击步骤参数对话框的回填回归测试（2026-09-22）。

现场故障：用户在「点击间隔」里改成 350ms，确定之后再次编辑，间隔又显示默认 20ms。

根因：`StepParamsDialog._build` 的 click 分支用 `_spin(20, ...)` 建控件，并在分支末尾
（构造期）按 `step.params` 设过一次初值；而 `_fill()`（`__init__` 里 `_build` 之后调用，
是编辑已存在步骤时的唯一回填入口）的 click 分支**没有回填** mouse_button / click_type /
interval_ms —— 于是每次打开都退回默认值。press / find / wait 等分支本来就回填了
interval_ms，只有 click 漏了。

顺带把这类 bug 的通用形态钉成源码级契约：`apply_to` 写进 params 的每个键，
`_fill` 里都必须有对应的 `p.get(...)` 回填。
"""
from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import FlowStep, default_step_params


_KEEP: list = []      # 对话框必须留着引用：临时对象被 GC 回收后 C++ 控件即失效
                      # （直接用 _dlg(step).interval 会报 Internal C++ object already deleted）


def _dlg(step):
    from app.ui.flow_dialog import StepParamsDialog
    dlg = StepParamsDialog(step)
    _KEEP.append(dlg)
    return dlg


class TestClickDialogFill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_interval_filled_from_params(self):
        step = FlowStep(type="click",
                        params=dict(default_step_params("click"), interval_ms=350))
        self.assertEqual(_dlg(step).interval.value(), 350)

    def test_button_and_click_type_filled(self):
        step = FlowStep(type="click",
                        params=dict(default_step_params("click"),
                                    mouse_button="right", click_type="double"))
        dlg = _dlg(step)
        self.assertEqual(dlg.btn_combo.currentData(), "right")
        self.assertEqual(dlg.type_combo.currentData(), "double")

    def test_edit_then_reopen_keeps_interval(self):
        """正是用户的操作路径：改间隔 → 确定 → 再打开 → 值还在。"""
        step = FlowStep(type="click", params=default_step_params("click"))
        dlg1 = _dlg(step)
        dlg1.interval.setValue(350)
        dlg1.apply_to(step)
        self.assertEqual(step.params["interval_ms"], 350)
        dlg2 = _dlg(step)             # 再编辑同一个步骤
        self.assertEqual(dlg2.interval.value(), 350)

    def test_other_click_fields_survive_reopen(self):
        """同一个坑的其它字段一起验：坐标模式与计数也得回来。"""
        step = FlowStep(type="click", params=default_step_params("click"))
        dlg1 = _dlg(step)
        dlg1.interval.setValue(120)
        dlg1.interval.setValue(120)
        dlg1.fixed_radio.setChecked(True)
        dlg1.pos_x.setValue(321)
        dlg1.pos_y.setValue(654)
        dlg1.count.setValue(7)
        dlg1.apply_to(step)
        dlg2 = _dlg(step)
        self.assertEqual(dlg2.interval.value(), 120)
        self.assertEqual(dlg2.pos_x.value(), 321)
        self.assertEqual(dlg2.pos_y.value(), 654)
        self.assertEqual(dlg2.count.value(), 7)
        self.assertTrue(dlg2.fixed_radio.isChecked())

    def test_interval_below_widget_min_is_clamped(self):
        """控件下限 20ms：更小的值会被夹到下限（既有设计，不是本次 bug）。"""
        step = FlowStep(type="click",
                        params=dict(default_step_params("click"), interval_ms=5))
        self.assertEqual(_dlg(step).interval.value(), 20)


def _method_src(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _keys_by_type(seg: str, kind: str) -> dict:
    """把方法体按 `if/elif t == "xxx"` 切块，返回 {类型表达式: 参数键集合}。"""
    blocks: dict[str, list[str]] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in seg.splitlines():
        m = re.match(r'^(?:el)?if t (?:==|in) (.+):$', line.strip())
        if m:
            if cur:
                blocks.setdefault(cur, []).append("\n".join(buf))
            cur, buf = m.group(1), []
        elif cur:
            buf.append(line)
    if cur:
        blocks.setdefault(cur, []).append("\n".join(buf))
    rx = (re.compile(r'p\.get\(\s*"([A-Za-z_0-9]+)"') if kind == "fill"
          else re.compile(r'"([A-Za-z_0-9]+)":'))
    return {t: set().union(*[set(rx.findall(c)) for c in chunks])
            for t, chunks in blocks.items()}


class TestFillCoversApplyToKeys(unittest.TestCase):
    """源码级契约：`apply_to` 写进 params 的键，`_fill` 必须回填。

    漏一个键 → 用户改完保存、再打开又显示默认值（本次 click 的 interval_ms 就是）。
    """

    def _pairs(self, src: str):
        fill = _keys_by_type(_method_src(src, "def _fill(self, step: FlowStep)",
                                        "def _fill_background"), "fill")
        app = _keys_by_type(_method_src(src, "def apply_to(self, step: FlowStep)",
                                        "def _apply_background"), "apply")
        return fill, app

    @staticmethod
    def _real_source() -> str:
        from app.ui import flow_dialog
        with open(flow_dialog.__file__, encoding="utf-8") as f:
            return f.read()

    def test_parser_detects_missing_fill(self):
        """先证明检查器本身有效，免得正则失效后永远通过。"""
        fake = (
            '    def _fill(self, step: FlowStep):\n'
            '        if t == "demo":\n'
            '            self.a.setValue(int(p.get("alpha", 1)))\n'
            '    def _fill_background(self, p: dict) -> None:\n'
            '        pass\n'
            '    def apply_to(self, step: FlowStep) -> None:\n'
            '        if t == "demo":\n'
            '            step.params.update({"alpha": 1, "beta": 2})\n'
            '    def _apply_background(self, step: FlowStep) -> None:\n'
            '        pass\n')
        fill, app = self._pairs(fake)
        self.assertEqual(fill['"demo"'], {"alpha"})
        self.assertEqual(app['"demo"'] - fill['"demo"'], {"beta"})

    def test_every_applied_key_is_filled_back(self):
        fill, app = self._pairs(self._real_source())
        missing = {t: sorted(keys - fill.get(t, set()))
                   for t, keys in app.items() if keys - fill.get(t, set())}
        self.assertEqual(missing, {},
                         f"这些参数 apply_to 写了、_fill 却没回填：{missing}")


if __name__ == "__main__":
    unittest.main()
