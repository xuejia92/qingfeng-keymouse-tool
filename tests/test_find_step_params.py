# -*- coding: utf-8 -*-
"""找图步骤（run_find_step）数值解析的健壮性契约（2026-10-02 code review 修复）。

原实现四处会出问题：
1. `float(p.get("search_timeout_sec", 10) or 0)`——输入框被清空时值是 `""`（**不是缺失**），
   `or 0` 把它变成 0，而 `_wait_hit` 里 `timeout<=0` 表示**一直等下去**，
   找图步骤永久挂住；
2. `float(p.get("confidence", 0.85))` 裸调，填非数字直接抛异常打断步骤；
3. `int(p.get("count"...))` / `float(p.get("duration_sec"...))` 同样裸调；
4. offset_x/offset_y 在**循环里**每轮重新 int()，既慢又会抛。

修复：统一走 `_num_or`，空值/非法值按各字段既有默认回落，**只有显式填 0**
才表示「无限」；confidence/偏移量提到循环外只解析一次。

测试不真正找图：mock 掉模板加载与 `_wait_hit`，直接检查传进去的参数。
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from app import tasks
from app.tasks import run_find_step


def _noop(*_a):
    pass


class FindParamsCase(unittest.TestCase):
    """跑一次 run_find_step（_wait_hit 立刻返回 None），返回它收到的参数。"""

    def _probe(self, params: dict) -> tuple[list[tuple[float, float]], str]:
        seen: list[tuple[float, float]] = []

        def fake_wait_hit(_template, confidence, timeout, _region, _stop):
            seen.append((timeout, confidence))
            return None                      # 立刻「找不到」→ 只跑一轮

        p = {"image": "x.png", "count": 1, **params}
        with mock.patch.object(tasks.finder, "load_template", return_value=object()), \
                mock.patch.object(tasks, "_wait_hit", side_effect=fake_wait_hit), \
                mock.patch.object(tasks.input_actors, "click"):
            reason = run_find_step(p, threading.Event(), _noop)
        return seen, reason

    def _first(self, params: dict) -> tuple[float, float]:
        seen, reason = self._probe(params)
        self.assertEqual(reason, "等待目标超时")
        self.assertEqual(len(seen), 1, "只应跑一轮")
        return seen[0]


class TestTimeoutParsing(FindParamsCase):
    def test_blank_timeout_falls_back_to_ten_not_infinite(self):
        """★ 用户把输入框清空 → 回落 10 秒，**不能**变成无限等待。"""
        timeout, _conf = self._first({"search_timeout_sec": ""})
        self.assertEqual(timeout, 10.0)

    def test_none_timeout_falls_back_to_ten(self):
        timeout, _conf = self._first({"search_timeout_sec": None})
        self.assertEqual(timeout, 10.0)

    def test_missing_timeout_uses_ten(self):
        timeout, _conf = self._first({})
        self.assertEqual(timeout, 10.0)

    def test_garbage_timeout_falls_back_to_ten(self):
        """非法值不再抛异常打断步骤。"""
        timeout, _conf = self._first({"search_timeout_sec": "abc"})
        self.assertEqual(timeout, 10.0)

    def test_explicit_zero_still_means_infinite(self):
        """用户主动填 0 = 无限等待，这个语义必须保留。"""
        timeout, _conf = self._first({"search_timeout_sec": 0})
        self.assertEqual(timeout, 0.0)

    def test_explicit_zero_string_also_infinite(self):
        timeout, _conf = self._first({"search_timeout_sec": "0"})
        self.assertEqual(timeout, 0.0)

    def test_valid_timeout_is_kept(self):
        timeout, _conf = self._first({"search_timeout_sec": "3.5"})
        self.assertEqual(timeout, 3.5)

    def test_negative_timeout_clamped_to_zero(self):
        """负数无意义，夹到 0（= 无限）而不是留个负 deadline。"""
        timeout, _conf = self._first({"search_timeout_sec": -5})
        self.assertEqual(timeout, 0.0)


class TestConfidenceParsing(FindParamsCase):
    def test_default_confidence(self):
        _timeout, conf = self._first({})
        self.assertAlmostEqual(conf, 0.85)

    def test_garbage_confidence_does_not_raise(self):
        _timeout, conf = self._first({"confidence": "abc"})
        self.assertAlmostEqual(conf, 0.85)

    def test_blank_confidence_uses_default(self):
        _timeout, conf = self._first({"confidence": ""})
        self.assertAlmostEqual(conf, 0.85)

    def test_confidence_clamped_to_unit_range(self):
        _timeout, high = self._first({"confidence": 5})
        _timeout2, low = self._first({"confidence": -1})
        self.assertAlmostEqual(high, 1.0)
        self.assertAlmostEqual(low, 0.0)

    def test_valid_confidence_kept(self):
        _timeout, conf = self._first({"confidence": 0.6})
        self.assertAlmostEqual(conf, 0.6)


class TestOtherNumericFields(FindParamsCase):
    def test_garbage_count_and_duration_do_not_raise(self):
        """count/duration 填脏值时步骤仍能跑完（原来 int()/float() 直接抛）。"""
        seen, reason = self._probe({"count": "五次", "duration_sec": "很久"})
        self.assertEqual(reason, "等待目标超时")
        self.assertEqual(len(seen), 1)

    def test_garbage_offsets_do_not_raise(self):
        """offset_x/offset_y 在循环里解析，脏值原来每轮都抛。"""
        seen, reason = self._probe({"offset_x": "左", "offset_y": "右"})
        self.assertEqual(reason, "等待目标超时")
        self.assertEqual(len(seen), 1)

    def test_garbage_everything_at_once(self):
        """把所有数值字段同时填成垃圾值也不该崩。"""
        seen, reason = self._probe({
            "search_timeout_sec": "", "confidence": "", "count": None,
            "duration_sec": None, "offset_x": None, "offset_y": None,
        })
        self.assertEqual(reason, "等待目标超时")
        self.assertEqual(len(seen), 1)
        timeout, conf = seen[0]
        self.assertEqual(timeout, 10.0)
        self.assertAlmostEqual(conf, 0.85)


class TestSourceContract(unittest.TestCase):
    def test_no_bare_float_on_config_values(self):
        """源码级兜底：run_find_step 里不得再出现裸 float()/int() 解析配置。"""
        import inspect
        src = inspect.getsource(run_find_step)
        body = src.split("template = finder.load_template")[1]
        for bad in ('float(p.get(', 'int(p.get('):
            self.assertNotIn(bad, body,
                             f"解析配置值必须走 _num_or，别用裸 {bad}...)")


if __name__ == "__main__":
    unittest.main()
