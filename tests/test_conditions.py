"""条件分支核心逻辑（app/conditions.py）的单元测试。

覆盖：表达式翻译、变量提取、求值、结构校验、控制流跳转表、成对匹配、
条件块删除范围、缩进层级、变量前序定义追踪，以及一套复刻执行引擎语义的
「模拟执行」用例（证明 if/elseif/else/endif 的分支定向正确）。
"""
from __future__ import annotations

import unittest

from app.conditions import (ALL_BRANCH_HEADS, BLOCK_CLOSE_TYPES, BLOCK_OPEN_TYPES,
                            BLOCK_PAIRS, MAX_BLOCK_DEPTH, block_indent_levels,
                            block_indices, build_block_tree, build_control_flow,
                            build_loop_flow, check_condition_variables,
                            condition_block_indices, condition_indent_levels,
                            defined_variables_before, empty_loop_bodies,
                            enclosing_block, enclosing_if, enclosing_loop,
                            eval_condition, extract_variables, match_block_end,
                            match_endif, max_block_depth, translate_condition,
                            validate_block_structure,
                            validate_condition_structure)
from app.config import FlowStep, FlowVariable


def _s(step_type, **params):
    return FlowStep(type=step_type, params=params)


def _simulate(steps, variables):
    """复刻执行引擎（flows.FlowRunner._run_once）的分支语义，返回实际执行到的
    普通步骤索引列表（if/elseif/else/endif 与普通步骤均用索引标识）。"""
    false_jump, block_end = build_control_flow(steps)
    executed: list[int] = []
    idx = 0
    n = len(steps)
    pending_ends: list[int] = []
    arrived_by_jump = False
    while idx < n:
        s = steps[idx]
        t = s.type
        if not arrived_by_jump and t in ("elseif", "else", "endif") and pending_ends:
            idx = pending_ends.pop()
            continue
        if t in ("if", "elseif"):
            ok, result, _ = eval_condition(s.params.get("condition", ""), variables)
            truthy = bool(result) if ok else False
            if truthy:
                pending_ends.append(block_end.get(idx, n))
                idx += 1
                arrived_by_jump = False
            else:
                target = false_jump.get(idx, n)
                arrived_by_jump = (target != block_end.get(idx, n))
                idx = target
            continue
        if t == "else":
            pending_ends.append(block_end.get(idx, n))
            idx += 1
            arrived_by_jump = False
            continue
        if t == "endif":
            idx += 1
            arrived_by_jump = False
            continue
        executed.append(idx)
        idx += 1
    return executed


class TestTranslateCondition(unittest.TestCase):
    def test_logical_operators(self):
        self.assertEqual(translate_condition("x>=1 && y<=10"), "x>=1 and y<=10")
        self.assertEqual(translate_condition("a || b"), "a or b")
        self.assertEqual(translate_condition("!x"), " not x")
        self.assertEqual(translate_condition("x != 1"), "x != 1")

    def test_single_equal_becomes_equal(self):
        self.assertEqual(translate_condition("x = 1"), "x == 1")
        # 已有的比较运算符不被破坏
        self.assertEqual(translate_condition("x >= 1"), "x >= 1")
        self.assertEqual(translate_condition("x <= 1"), "x <= 1")
        self.assertEqual(translate_condition("x == 1"), "x == 1")

    def test_string_literal_untouched(self):
        self.assertEqual(translate_condition('x == "a=b"'), 'x == "a=b"')
        self.assertEqual(translate_condition('x == "a&&b"'), 'x == "a&&b"')
        self.assertEqual(translate_condition("x == 'a||b'"), "x == 'a||b'")


class TestExtractVariables(unittest.TestCase):
    def test_comparison(self):
        self.assertEqual(extract_variables("x>=1 && y<=10"), ["x", "y"])

    def test_function_call_excludes_name(self):
        self.assertEqual(extract_variables("len(items) > 0"), ["items"])

    def test_subscript_and_attribute(self):
        self.assertEqual(extract_variables('x["a"] > 0'), ["x"])
        self.assertEqual(extract_variables("obj.attr > 0"), ["obj"])

    def test_literal_only(self):
        self.assertEqual(extract_variables("1 + 2 > 3"), [])

    def test_dedup_keep_order(self):
        self.assertEqual(extract_variables("x > 1 && x < 5 || y == 2"), ["x", "y"])

    def test_bad_syntax_returns_empty(self):
        self.assertEqual(extract_variables("x >"), [])

    def test_lowercase_true_false_not_variables(self):
        """小写 true/false 是字面量，不要求变量定义。"""
        self.assertEqual(extract_variables("true"), [])
        self.assertEqual(extract_variables("x == false"), ["x"])


class TestEvalCondition(unittest.TestCase):
    def test_true_and_false(self):
        ok, result, _ = eval_condition("x>=1 && y<=10", {"x": 5, "y": 3})
        self.assertTrue(ok)
        self.assertIs(result, True)
        ok, result, _ = eval_condition("x>=1 && y<=10", {"x": 0, "y": 3})
        self.assertTrue(ok)
        self.assertIs(result, False)

    def test_empty(self):
        ok, result, why = eval_condition("", {})
        self.assertFalse(ok)
        self.assertIn("为空", why)

    def test_syntax_error(self):
        ok, _, why = eval_condition("x >", {})
        self.assertFalse(ok)
        self.assertIn("语法", why)

    def test_undefined_variable(self):
        ok, _, why = eval_condition("x > 1", {})
        self.assertFalse(ok)
        self.assertIn("未定义", why)

    def test_len_builtin_allowed(self):
        ok, result, _ = eval_condition("len(items) > 2", {"items": [1, 2, 3]})
        self.assertTrue(ok)
        self.assertIs(result, True)

    def test_literal_true_constant(self):
        """直接写小写 true = 恒真（while 无限循环入口）；false 恒假。"""
        ok, result, _ = eval_condition("true", {})
        self.assertTrue(ok)
        self.assertIs(result, True)
        ok, result, _ = eval_condition("false", {})
        self.assertTrue(ok)
        self.assertIs(result, False)

    def test_lowercase_true_does_not_break_variable_with_prefix(self):
        """折叠是词法级的：truex / 变量 true（若真的声明了）仍可正常引用。"""
        ok, result, _ = eval_condition("truex > 2", {"truex": 5})
        self.assertTrue(ok)
        self.assertIs(result, True)
        ok, result, _ = eval_condition("x == true", {"x": True})
        self.assertTrue(ok)
        self.assertIs(result, True)

    def test_unsafe_builtin_blocked(self):
        ok, _, _ = eval_condition("__import__('os')", {})
        self.assertFalse(ok)


class TestValidateStructure(unittest.TestCase):
    def test_valid(self):
        steps = [_s("if", condition="a"), _s("wait"), _s("endif")]
        self.assertEqual(validate_condition_structure(steps), [])

    def test_valid_elseif_else(self):
        steps = [_s("if", condition="a"), _s("wait"), _s("elseif", condition="b"),
                 _s("wait"), _s("else"), _s("wait"), _s("endif")]
        self.assertEqual(validate_condition_structure(steps), [])

    def test_orphan_endif(self):
        errors = validate_condition_structure([_s("endif")])
        self.assertEqual(len(errors), 1)
        self.assertIn("条件结束", errors[0])

    def test_orphan_elseif(self):
        errors = validate_condition_structure([_s("elseif", condition="x")])
        self.assertEqual(len(errors), 1)
        self.assertIn("否则如果", errors[0])

    def test_unclosed_if(self):
        errors = validate_condition_structure([_s("if", condition="x")])
        self.assertEqual(len(errors), 1)
        self.assertIn("缺少配对", errors[0])

    def test_duplicate_else(self):
        steps = [_s("if", condition="x"), _s("else"), _s("else"), _s("endif")]
        errors = validate_condition_structure(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("重复", errors[0])

    def test_elseif_after_else(self):
        steps = [_s("if", condition="x"), _s("else"), _s("elseif", condition="y"),
                 _s("endif")]
        errors = validate_condition_structure(steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("else", errors[0])


class TestControlFlow(unittest.TestCase):
    def test_if_endif(self):
        steps = [_s("if", condition="a"), _s("wait"), _s("endif")]
        false_jump, block_end = build_control_flow(steps)
        self.assertEqual(false_jump, {0: 3})
        self.assertEqual(block_end, {0: 3})

    def test_if_elseif_else(self):
        steps = [_s("if", condition="a"), _s("wait"), _s("elseif", condition="b"),
                 _s("wait"), _s("else"), _s("wait"), _s("endif")]
        false_jump, block_end = build_control_flow(steps)
        self.assertEqual(false_jump, {0: 2, 2: 4, 4: 7})
        self.assertEqual(block_end, {0: 7, 2: 7, 4: 7})


class TestMatching(unittest.TestCase):
    def test_match_endif_basic(self):
        steps = [_s("if"), _s("wait"), _s("endif")]
        self.assertEqual(match_endif(steps, 0), 2)

    def test_match_endif_nested(self):
        steps = [_s("if"), _s("if"), _s("wait"), _s("endif"), _s("endif")]
        self.assertEqual(match_endif(steps, 0), 4)
        self.assertEqual(match_endif(steps, 1), 3)

    def test_enclosing_if(self):
        steps = [_s("if"), _s("wait"), _s("elseif", condition="x"), _s("wait"),
                 _s("endif")]
        self.assertEqual(enclosing_if(steps, 2), 0)
        self.assertIsNone(enclosing_if(steps, 5))

    def test_condition_block_indices(self):
        steps = [_s("if", condition="a"), _s("wait"), _s("elseif", condition="b"),
                 _s("wait"), _s("else"), _s("wait"), _s("endif"), _s("wait")]
        self.assertEqual(condition_block_indices(steps, 0), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(condition_block_indices(steps, 4), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(condition_block_indices(steps, 6), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(condition_block_indices(steps, 7), [])


class TestVariableTracking(unittest.TestCase):
    def _steps(self):
        return [_s("var", name="x"), _s("if", condition="x > 1 && y < 5"),
                _s("wait"), _s("var", name="y"), _s("endif")]

    def test_defined_before(self):
        steps = self._steps()
        names = defined_variables_before(steps, [], 1)
        self.assertIn("x", names)
        self.assertNotIn("y", names)

    def test_declared_variable_always_defined(self):
        steps = [_s("if", condition="k > 0"), _s("endif")]
        names = defined_variables_before(steps, [FlowVariable(name="k", type="integer")], 0)
        self.assertIn("k", names)

    def test_check_missing(self):
        steps = self._steps()
        ok, missing = check_condition_variables(steps, [], 1, "x > 1 && y < 5")
        self.assertFalse(ok)
        self.assertEqual(missing, ["y"])   # x 已定义，y 未定义

    def test_check_all_defined(self):
        steps = self._steps()
        ok, missing = check_condition_variables(steps, [], 1, "x > 1")
        self.assertTrue(ok)
        self.assertEqual(missing, [])


class TestIndentLevels(unittest.TestCase):
    """条件块缩进层级（condition_indent_levels）：步骤列表按此渲染视觉层级。"""

    def test_flat_block(self):
        """分支头（if/elseif/else/endif）一律不缩进、互相对齐，只有分支内步骤缩进。"""
        steps = [_s("wait"), _s("if", condition="a"), _s("press"),
                 _s("elseif", condition="b"), _s("click"),
                 _s("else"), _s("wait"), _s("endif"), _s("wait")]
        self.assertEqual(condition_indent_levels(steps),
                         [0, 0, 1, 0, 1, 0, 1, 0, 0])

    def test_nested_block(self):
        """嵌套块逐层叠加：内层 if/endif 落在外层缩进之上，内层内容再多一级。"""
        steps = [_s("if", condition="a"), _s("wait"), _s("if", condition="b"),
                 _s("wait"), _s("endif"), _s("wait"), _s("endif")]
        self.assertEqual(condition_indent_levels(steps), [0, 1, 1, 2, 1, 1, 0])

    def test_branch_heads_inside_nested_block(self):
        """内层块里的 elseif 与该内层 if 对齐，不跟着块内容多缩进一级。"""
        steps = [_s("if", condition="a"), _s("wait"), _s("if", condition="b"),
                 _s("wait"), _s("elseif", condition="c"), _s("wait"),
                 _s("endif"), _s("endif")]
        self.assertEqual(condition_indent_levels(steps), [0, 1, 1, 2, 1, 2, 1, 0])

    def test_orphan_marks_never_go_negative(self):
        """孤儿 endif / 未闭合 if 都不让层级变负，渲染不会错位。"""
        self.assertEqual(condition_indent_levels([_s("endif"), _s("wait")]), [0, 0])
        self.assertEqual(
            condition_indent_levels([_s("wait"), _s("if", condition="a")]), [0, 0])

    def test_empty_and_no_condition(self):
        self.assertEqual(condition_indent_levels([]), [])
        self.assertEqual(condition_indent_levels([_s("wait"), _s("click")]), [0, 0])


class TestSimulateBranch(unittest.TestCase):
    def test_if_true(self):
        steps = [_s("if", condition="x>3"), _s("wait"), _s("endif"), _s("wait")]
        self.assertEqual(_simulate(steps, {"x": 5}), [1, 3])

    def test_if_false(self):
        steps = [_s("if", condition="x>3"), _s("wait"), _s("endif"), _s("wait")]
        self.assertEqual(_simulate(steps, {"x": 1}), [3])

    def test_elseif_chain(self):
        steps = [_s("if", condition="x>10"), _s("wait"),
                 _s("elseif", condition="x>3"), _s("wait"),
                 _s("else"), _s("wait"), _s("endif"), _s("wait")]
        self.assertEqual(_simulate(steps, {"x": 20}), [1, 7])
        self.assertEqual(_simulate(steps, {"x": 5}), [3, 7])
        self.assertEqual(_simulate(steps, {"x": 1}), [5, 7])

    def test_nested(self):
        steps = [_s("if", condition="a"), _s("if", condition="b"), _s("wait"),
                 _s("endif"), _s("wait"), _s("endif"), _s("wait")]
        self.assertEqual(_simulate(steps, {"a": True, "b": True}), [2, 4, 6])
        self.assertEqual(_simulate(steps, {"a": True, "b": False}), [4, 6])
        self.assertEqual(_simulate(steps, {"a": False, "b": True}), [6])

    def test_nested_with_outer_elseif(self):
        steps = [_s("if", condition="a"), _s("if", condition="b"), _s("wait"),
                 _s("endif"), _s("elseif", condition="c"), _s("wait"),
                 _s("endif"), _s("wait")]
        # 外层 a 成立时，内层 b 无论真假，外层 elseif 都应被跳过
        self.assertEqual(_simulate(steps, {"a": True, "b": True, "c": True}), [2, 7])
        self.assertEqual(_simulate(steps, {"a": True, "b": False, "c": True}), [7])
        # 外层 a 不成立时，走 elseif
        self.assertEqual(_simulate(steps, {"a": False, "b": True, "c": True}), [5, 7])
        self.assertEqual(_simulate(steps, {"a": False, "b": False, "c": False}), [7])


class TestBlockModel(unittest.TestCase):
    """通用块模型（if / foreach / while 共用）的常量、配对、缩进、树与校验。"""

    def test_block_pairs_definition(self):
        self.assertEqual(BLOCK_PAIRS, {"if": "endif", "foreach": "endForeach",
                                       "while": "endWhile"})
        self.assertEqual(BLOCK_OPEN_TYPES, ("if", "foreach", "while"))
        self.assertEqual(BLOCK_CLOSE_TYPES, ("endif", "endForeach", "endWhile"))
        self.assertEqual(ALL_BRANCH_HEADS, ("elseif", "else"))

    def test_match_block_end_foreach(self):
        steps = [_s("foreach"), _s("wait"), _s("endForeach")]
        self.assertEqual(match_block_end(steps, 0), 2)
        self.assertIsNone(match_block_end(steps, 1))

    def test_match_block_end_cross_nested(self):
        """if 内嵌 foreach、foreach 内嵌 while，交错闭合仍各自正确配对。"""
        steps = [_s("if"), _s("foreach"), _s("while"), _s("wait"),
                 _s("endWhile"), _s("endForeach"), _s("endif")]
        self.assertEqual(match_block_end(steps, 0), 6)
        self.assertEqual(match_block_end(steps, 1), 5)
        self.assertEqual(match_block_end(steps, 2), 4)

    def test_block_indices_foreach(self):
        steps = [_s("wait"), _s("foreach"), _s("wait"), _s("endForeach"), _s("wait")]
        self.assertEqual(block_indices(steps, 1), [1, 2, 3])   # 起始块
        self.assertEqual(block_indices(steps, 3), [1, 2, 3])   # 结束标记
        self.assertEqual(block_indices(steps, 0), [])          # 块外普通步骤
        self.assertEqual(block_indices(steps, 4), [])

    def test_block_indices_branch_head(self):
        """elseif/else 分支头归属整个 if 块。"""
        steps = [_s("if"), _s("wait"), _s("else"), _s("wait"), _s("endif")]
        self.assertEqual(block_indices(steps, 2), [0, 1, 2, 3, 4])

    def test_enclosing_block(self):
        steps = [_s("wait"), _s("foreach"), _s("while"), _s("wait"),
                 _s("endWhile"), _s("endForeach")]
        self.assertIsNone(enclosing_block(steps, 0))   # 块外普通步骤
        self.assertEqual(enclosing_block(steps, 3), 2)  # 内层 while 体内
        self.assertEqual(enclosing_block(steps, 4), 2)  # endWhile 配对 while
        self.assertEqual(enclosing_block(steps, 5), 1)  # endForeach 配对 foreach

    def test_block_indent_levels_nested(self):
        """文档示例：foreach=0, while=1, wait=2, endWhile=1, wait=1, endForeach=0。"""
        steps = [_s("foreach"), _s("while"), _s("wait"), _s("endWhile"),
                 _s("wait"), _s("endForeach")]
        self.assertEqual(block_indent_levels(steps), [0, 1, 2, 1, 1, 0])

    def test_block_indent_levels_if_matches_condition(self):
        """纯 if 块下通用缩进与旧 condition_indent_levels 输出一致。"""
        steps = [_s("wait"), _s("if", condition="a"), _s("press"),
                 _s("elseif", condition="b"), _s("click"), _s("endif"), _s("wait")]
        self.assertEqual(block_indent_levels(steps),
                         condition_indent_levels(steps))

    def test_block_indent_orphan_never_negative(self):
        self.assertEqual(block_indent_levels([_s("endForeach"), _s("wait")]), [0, 0])
        self.assertEqual(block_indent_levels([_s("endWhile")]), [0])

    def test_build_block_tree(self):
        steps = [_s("wait"), _s("foreach"), _s("wait"), _s("endForeach"), _s("wait")]
        tree = build_block_tree(steps)
        self.assertEqual(len(tree), 3)
        self.assertEqual(tree[0], {"type": "wait", "index": 0})
        self.assertEqual(tree[1]["type"], "foreach")
        self.assertEqual(tree[1]["index"], 1)
        self.assertEqual(tree[1]["end"], 3)
        self.assertEqual(tree[1]["children"], [{"type": "wait", "index": 2}])
        self.assertEqual(tree[2], {"type": "wait", "index": 4})

    def test_max_block_depth(self):
        steps = [_s("foreach"), _s("while"), _s("wait"), _s("endWhile"),
                 _s("endForeach")]
        self.assertEqual(max_block_depth(steps), 2)
        self.assertEqual(max_block_depth([]), 0)
        self.assertEqual(max_block_depth([_s("wait")]), 0)

    def test_build_loop_flow(self):
        steps = [_s("foreach"), _s("wait"), _s("endForeach"),
                 _s("while", condition="i<3"), _s("wait"), _s("endWhile")]
        self.assertEqual(build_loop_flow(steps), {0: 2, 3: 5})

    def test_empty_loop_bodies(self):
        steps = [_s("foreach"), _s("endForeach"),
                 _s("while", condition="i<3"), _s("wait"), _s("endWhile")]
        self.assertEqual(empty_loop_bodies(steps), [(0, "foreach")])


class TestValidateBlockStructure(unittest.TestCase):
    def test_valid_foreach_while(self):
        steps = [_s("foreach"), _s("wait"), _s("endForeach"),
                 _s("while", condition="i<3"), _s("wait"), _s("endWhile")]
        self.assertEqual(validate_block_structure(steps), [])

    def test_valid_mixed_nested(self):
        steps = [_s("if", condition="a"), _s("foreach"), _s("wait"),
                 _s("endForeach"), _s("endif")]
        self.assertEqual(validate_block_structure(steps), [])

    def test_orphan_endForeach(self):
        errors = validate_block_structure([_s("endForeach")])
        self.assertEqual(len(errors), 1)
        self.assertIn("Foreach 循环结束", errors[0])

    def test_orphan_endWhile(self):
        errors = validate_block_structure([_s("endWhile")])
        self.assertEqual(len(errors), 1)
        self.assertIn("while 循环结束", errors[0])

    def test_unclosed_foreach(self):
        errors = validate_block_structure([_s("foreach")])
        self.assertEqual(len(errors), 1)
        self.assertIn("缺少配对", errors[0])
        self.assertIn("Foreach 循环结束", errors[0])

    def test_unclosed_while(self):
        errors = validate_block_structure([_s("while", condition="i<3")])
        self.assertEqual(len(errors), 1)
        self.assertIn("while 循环结束", errors[0])

    def test_cross_mismatch(self):
        """foreach 被 endWhile 闭合 → 交叉错位。"""
        errors = validate_block_structure([_s("foreach"), _s("endWhile")])
        self.assertEqual(len(errors), 1)
        self.assertIn("交叉错位", errors[0])

    def test_depth_limit(self):
        steps = [_s("foreach") for _ in range(MAX_BLOCK_DEPTH + 1)]
        steps += [_s("endForeach") for _ in range(MAX_BLOCK_DEPTH + 1)]
        errors = validate_block_structure(steps)
        self.assertTrue(any("嵌套超过" in e for e in errors))

    def test_break_continue_inside_loop_valid(self):
        steps = [_s("foreach"), _s("break"), _s("endForeach"),
                 _s("while", condition="i<3"), _s("continue"), _s("endWhile")]
        self.assertEqual(validate_block_structure(steps), [])

    def test_break_outside_loop_invalid(self):
        errors = validate_block_structure([_s("break")])
        self.assertEqual(len(errors), 1)
        self.assertIn("只能放在 Foreach/while 循环体内", errors[0])

    def test_continue_outside_loop_invalid(self):
        errors = validate_block_structure([_s("wait"), _s("continue")])
        self.assertEqual(len(errors), 1)
        self.assertIn("continue 继续循环", errors[0])

    def test_continue_inside_if_outside_loop_invalid(self):
        """continue 位于 if 块内、但 if 不在循环内 → 仍非法（if 不算循环）。"""
        steps = [_s("if", condition="a"), _s("continue"), _s("endif")]
        errors = validate_block_structure(steps)
        self.assertTrue(any("只能放在" in e for e in errors))

    def test_break_inside_if_inside_loop_valid(self):
        """break 位于 if 内、if 又位于 foreach 内 → 合法（break 中断的是 foreach）。"""
        steps = [_s("foreach"), _s("if", condition="a"), _s("break"),
                 _s("endif"), _s("endForeach")]
        self.assertEqual(validate_block_structure(steps), [])


class TestEnclosingLoop(unittest.TestCase):
    def test_inside_foreach(self):
        steps = [_s("foreach"), _s("wait"), _s("endForeach")]
        self.assertEqual(enclosing_loop(steps, 1), 0)

    def test_inside_while(self):
        steps = [_s("while", condition="i<3"), _s("wait"), _s("endWhile")]
        self.assertEqual(enclosing_loop(steps, 1), 0)

    def test_on_close_marker_returns_its_loop(self):
        steps = [_s("foreach"), _s("wait"), _s("endForeach")]
        self.assertEqual(enclosing_loop(steps, 2), 0)

    def test_inside_if_inside_loop(self):
        """if 视为透明：break 位于 if 内、if 位于循环内，仍返回循环块。"""
        steps = [_s("foreach"), _s("if", condition="a"), _s("wait"),
                 _s("endif"), _s("endForeach")]
        self.assertEqual(enclosing_loop(steps, 2), 0)

    def test_outside_any_loop(self):
        steps = [_s("wait"), _s("foreach"), _s("endForeach")]
        self.assertIsNone(enclosing_loop(steps, 0))

    def test_inside_if_only_not_loop(self):
        steps = [_s("if", condition="a"), _s("wait"), _s("endif")]
        self.assertIsNone(enclosing_loop(steps, 1))

    def test_nested_returns_inner_loop(self):
        steps = [_s("foreach"), _s("while", condition="i<3"), _s("wait"),
                 _s("endWhile"), _s("endForeach")]
        self.assertEqual(enclosing_loop(steps, 2), 1)

    def test_bad_index(self):
        self.assertIsNone(enclosing_loop([], 0))
        self.assertIsNone(enclosing_loop([_s("wait")], 5))


if __name__ == "__main__":
    unittest.main()
