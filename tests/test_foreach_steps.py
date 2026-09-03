# -*- coding: utf-8 -*-
"""foreach 循环步骤的端到端执行测试（复用 FlowRunner._run_once）。

覆盖：列表/字典/字符串遍历、item/index 变量写入、空数据跳过、循环结束保留
最后值、嵌套循环、体内套 if、非列表数据源报错、数据源未定义报错。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import Flow, FlowStep, FlowVariable
from app.flows import FlowRunner, FlowVariableStore


def _run(flow):
    """在独立 runner 上跑一轮 _run_once，返回 (runner, 结束原因)。"""
    runner = FlowRunner(flow)
    runner.vars = FlowVariableStore(flow)
    reason = runner._run_once()
    return runner, reason


def _foreach(items="items", item_var="x", index_var="i"):
    return FlowStep(type="foreach", params={"items": items,
                                            "item_var": item_var,
                                            "index_var": index_var})


def _end():
    return FlowStep(type="endForeach")


def _bump():
    """循环体内把计数器 n 加一，证明循环体真的执行了。"""
    return FlowStep(type="py_func", params={
        "code": "def bump(n):\n    return n + 1",
        "func_name": "bump", "variables": ["n"], "result_var": "n"})


class TestForeachExecution(unittest.TestCase):
    def _base(self, items_default, extra_steps=None):
        variables = [FlowVariable(name="items", type="list",
                                  default_value=items_default),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [_foreach(), _bump(), _end()]
        if extra_steps:
            steps += extra_steps
        return Flow(name="f", variables=variables, steps=steps)

    def test_list_traversal(self):
        flow = self._base("[1, 2, 3]")
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)   # 循环体执行了 3 次
        self.assertEqual(runner.vars.values["x"], 3)   # item 保留最后值
        self.assertEqual(runner.vars.values["i"], 2)   # index 保留最后值

    def test_empty_list_skips(self):
        flow = self._base("[]")
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 0)   # 循环体未执行
        self.assertNotIn("x", runner.vars.values)      # 未注入 item/index

    def test_dict_traversal_value_to_item_key_to_index(self):
        """字典遍历：元素变量=值、下标变量=键（顺序 a→b→c）。"""
        variables = [FlowVariable(name="items", type="dict",
                                  default_value='{"a": "苹果", "b": "橘子", "c": "桃子"}'),
                     FlowVariable(name="seen", type="list", default_value="[]")]
        steps = [
            _foreach(items="items", item_var="item", index_var="key"),
            FlowStep(type="py_func", params={
                "code": "def collect(seen, item, key):\n    seen.append((key, item))\n    return seen",
                "func_name": "collect", "variables": ["seen", "item", "key"],
                "result_var": "seen"}),
            _end(),
        ]
        flow = Flow(name="f", variables=variables, steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["seen"],
                         [("a", "苹果"), ("b", "橘子"), ("c", "桃子")])
        # 循环结束保留最后一轮：item=值「桃子」、index=键「c」
        self.assertEqual(runner.vars.values["item"], "桃子")
        self.assertEqual(runner.vars.values["key"], "c")

    def test_dict_traversal_last_item_is_value(self):
        """字典遍历结束后 item 保留最后一个值（不是键）。"""
        variables = [FlowVariable(name="items", type="dict",
                                  default_value='{"a": 1, "b": 2, "c": 3}')]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["x"], 3)     # item=最后一个值
        self.assertEqual(runner.vars.values["i"], "c")   # index=最后一个键

    def test_string_traversal_by_char(self):
        variables = [FlowVariable(name="items", type="string",
                                  default_value="abc")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["x"], "c")

    def test_non_list_data_source_fails(self):
        variables = [FlowVariable(name="items", type="integer",
                                  default_value="42")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(), _end()])
        runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("不可遍历", reason)

    def test_undefined_data_source_fails(self):
        flow = Flow(name="f", steps=[_foreach(items="nope"), _end()])
        runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("未定义", reason)

    def test_items_subscript_expression(self):
        """数据源支持 Python 下标语法：data['nums'] 取子列表遍历。"""
        variables = [FlowVariable(name="data", type="dict",
                                  default_value='{"nums": [1, 2, 3]}'),
                     FlowVariable(name="n", type="integer", default_value="0")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(items="data['nums']"), _bump(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)

    def test_items_range_with_dollar_ref(self):
        """数据源支持函数 + $引用：range(0, $k) 遍历 0..k-1。"""
        variables = [FlowVariable(name="k", type="integer", default_value="3"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(items="range(0, $k)"), _bump(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)
        self.assertEqual(runner.vars.values["x"], 2)   # 最后元素 2
        self.assertEqual(runner.vars.values["i"], 2)   # 数字下标

    def test_items_sorted_function(self):
        """数据源支持函数：sorted($arr) 遍历排序结果。"""
        variables = [FlowVariable(name="arr", type="list",
                                  default_value="[3, 1, 2]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(items="sorted($arr)"), _bump(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)
        self.assertEqual(runner.vars.values["x"], 3)   # 排序后最大在末尾

    def test_items_slice_indexing_on_list(self):
        """slice 用在列表下标上：$arr[slice(0, $k)] 遍历前 k 个元素。"""
        variables = [FlowVariable(name="arr", type="list",
                                  default_value="[1, 2, 3, 4, 5]"),
                     FlowVariable(name="k", type="integer", default_value="3"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(items="$arr[slice(0, $k)]"), _bump(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 3)
        self.assertEqual(runner.vars.values["x"], 3)

    def test_items_literal_list(self):
        """数据源支持字面量列表：[1, 2] 直接遍历。"""
        variables = [FlowVariable(name="n", type="integer", default_value="0")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(items="[1, 2]"), _bump(), _end()])
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 2)

    def test_items_slice_object_alone_fails_helpfully(self):
        """slice(...) 单独作数据源不可遍历，报错要提示下标切片 / range 用法。"""
        variables = [FlowVariable(name="k", type="integer", default_value="3")]
        flow = Flow(name="f", variables=variables,
                    steps=[_foreach(items="slice(0, $k)"), _end()])
        runner, reason = _run(flow)
        self.assertIsNotNone(reason)
        self.assertIn("切片对象", reason)
        self.assertIn("range", reason)

    def test_nested_foreach(self):
        """外层遍历行，内层遍历每行元素：2x2 共执行 4 次循环体。"""
        variables = [FlowVariable(name="rows", type="list",
                                  default_value="[[1, 2], [3, 4]]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [
            _foreach(items="rows", item_var="row", index_var="ri"),
            _foreach(items="row", item_var="v", index_var="ci"),
            _bump(),
            _end(),
            _end(),
        ]
        flow = Flow(name="f", variables=variables, steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 4)

    def test_foreach_body_with_if(self):
        """循环体内套 if：只对偶数累加。"""
        variables = [FlowVariable(name="items", type="list",
                                  default_value="[1, 2, 3, 4]"),
                     FlowVariable(name="n", type="integer", default_value="0")]
        steps = [
            _foreach(),
            FlowStep(type="if", params={"condition": "x % 2 == 0"}),
            _bump(),
            FlowStep(type="endif"),
            _end(),
        ]
        flow = Flow(name="f", variables=variables, steps=steps)
        runner, reason = _run(flow)
        self.assertIsNone(reason)
        self.assertEqual(runner.vars.values["n"], 2)   # 只有 2、4 两次


if __name__ == "__main__":
    unittest.main()
