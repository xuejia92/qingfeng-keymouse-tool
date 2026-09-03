"""条件分支核心逻辑：表达式解析/求值、变量提取、结构校验、控制流跳转表。

纯逻辑模块，不依赖 Qt，可独立单测。流程执行引擎（flows.py）与流程编辑器
（flow_dialog.py / flow_tab.py）共用本模块，保证「编辑期校验」与「运行期求值」
使用同一套规则，避免两处实现不一致。

条件表达式语法（与用户输入对齐，见 translate_condition）：
  - 逻辑与：&& 或 and     逻辑或：|| 或 or     逻辑非：! 或 not
  - 比较：==  !=  <  <=  >  >=（单写 = 视为 ==）
  - 支持算术运算、字符串/数字/列表/字典字面量、len() 等少量安全内置函数

求值安全：只暴露白名单内置函数（见 _SAFE_BUILTINS），与项目现有「python函数」
步骤一致——该工具本就信任用户自填代码，条件表达式只做额外的一层防呆限制。
"""
from __future__ import annotations

import ast

# 条件分支相关的步骤类型（顺序即编辑/执行中识别用的集合）
CONDITION_TYPES = ("if", "elseif", "else", "endif")

# ---------------- 统一块模型 ----------------
# 三种块共用「扁平列表 + 类型配对」：起始块 -> 结束标记。分支头只有 if 有。
BLOCK_PAIRS = {"if": "endif", "foreach": "endForeach", "while": "endWhile"}
BRANCH_HEADS = {"if": ("elseif", "else")}

# 派生的类型集合：type 驱动整套块逻辑（配对/缩进/删除/校验），避免逐块重写。
BLOCK_OPEN_TYPES = tuple(BLOCK_PAIRS.keys())          # ("if", "foreach", "while")
BLOCK_CLOSE_TYPES = tuple(BLOCK_PAIRS.values())       # ("endif", "endForeach", "endWhile")
_CLOSE_TO_OPEN = {v: k for k, v in BLOCK_PAIRS.items()}
ALL_BRANCH_HEADS = tuple(h for hs in BRANCH_HEADS.values() for h in hs)  # ("elseif", "else")

# while 死循环保护上限：循环体内不修改变量导致条件恒真时的兜底
MAX_WHILE_ITERATIONS = 10000
# 块嵌套最大深度：更深报错，防止列表缩进失控
MAX_BLOCK_DEPTH = 8

# 块类型显示名（校验错误信息用；与 config.FLOW_STEP_TYPES 对齐）
_BLOCK_LABELS = {
    "if": "条件判断", "elseif": "否则如果", "else": "否则", "endif": "条件结束",
    "foreach": "Foreach 循环", "endForeach": "Foreach 循环结束",
    "while": "while 循环", "endWhile": "while 循环结束",
    "break": "break 中断循环", "continue": "continue 继续循环",
}

# 循环块的起始/结束类型：break/continue 只能位于这些循环块内部
LOOP_OPEN_TYPES = ("foreach", "while")
LOOP_CLOSE_TYPES = ("endForeach", "endWhile")
_LOOP_CLOSE_TO_OPEN = {"endForeach": "foreach", "endWhile": "while"}

# 求值时暴露给表达式的安全内置（白名单）；其余（open/__import__/eval…）一律不可用。
# 集合函数/迭代工具常用于构造数据源（foreach items 的 range/sorted/slice 等）。
_SAFE_BUILTINS = {
    "len": len, "abs": abs, "min": min, "max": max, "round": round, "sum": sum,
    "int": int, "float": float, "str": str, "bool": bool,
    "any": any, "all": all,
    "range": range, "sorted": sorted, "reversed": reversed,
    "enumerate": enumerate, "zip": zip,
    "list": list, "tuple": tuple, "set": set, "slice": slice,
}


def translate_condition(expr: str) -> str:
    """把用户友好的 && / || / ! / = 转成 Python 可求值表达式。

    只在字符串字面量之外做替换，避免误改 "a=b" 这类字符串内容；替换产生的
    关键字（and/or/not）两侧空格与输入已有空格会叠加，最后统一折叠为单空格
    （同样不触碰字符串字面量内部）。
    """
    s = str(expr or "")
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if s[i] == "\\" and i + 1 < n:
                    out.append(s[i:i + 2])
                    i += 2
                    continue
                out.append(s[i])
                if s[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if s.startswith("&&", i):
            out.append(" and ")
            i += 2
            continue
        if s.startswith("||", i):
            out.append(" or ")
            i += 2
            continue
        if s.startswith("!=", i):
            out.append("!=")
            i += 2
            continue
        if c in ("<", ">") and i + 1 < n and s[i + 1] == "=":
            out.append(c + "=")
            i += 2
            continue
        if s.startswith("==", i):
            out.append("==")
            i += 2
            continue
        if c == "!":
            out.append(" not ")
            i += 1
            continue
        if c == "=":
            out.append("==")
            i += 1
            continue
        out.append(c)
        i += 1
    return _collapse_ws("".join(out))


def _collapse_ws(s: str) -> str:
    """把连续空白折叠为单个空格；字符串字面量内部原样保留。"""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if s[i] == "\\" and i + 1 < n:
                    out.append(s[i:i + 2])
                    i += 2
                    continue
                out.append(s[i])
                if s[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c.isspace():
            if not out or not out[-1].isspace():
                out.append(" ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class _NameCollector(ast.NodeVisitor):
    """遍历 AST 收集「作为变量读取」的标识符。

    排除：函数名（len(x) 里的 len）、True/False/None、关键字。
    属性访问 obj.attr / 方法调用 obj.method() 只收承载对象 obj；
    下标 x["k"] 收 x。
    """

    def __init__(self):
        self.names: list[str] = []

    def visit_Name(self, node):
        if (isinstance(node.ctx, ast.Load)
                and node.id not in ("True", "False", "None", "true", "false")):
            self.names.append(node.id)

    def visit_Call(self, node):
        # 函数名（Name）不作为变量；对象方法/下标调用则访问其承载对象
        if isinstance(node.func, ast.Name):
            pass
        else:
            self.visit(node.func)
        for a in node.args:
            self.visit(a)
        for k in node.keywords:
            self.visit(k.value)

    def visit_Attribute(self, node):
        self.visit(node.value)


def extract_variables(expr: str) -> list[str]:
    """提取条件表达式引用的变量名（去重，保持出现顺序）。

    表达式无法解析为合法 Python 时返回空列表（语法错误由 eval_condition 负责提示）。
    """
    code = translate_condition(expr)
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError:
        return []
    collector = _NameCollector()
    collector.visit(tree)
    seen: set[str] = set()
    result: list[str] = []
    for name in collector.names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


class _BoolLiteralFold(ast.NodeTransformer):
    """把小写 true / false 名字折叠为布尔字面量（AST 词法级别，不误伤 truex 等变量名）。"""

    def visit_Name(self, node):
        if node.id == "true":
            return ast.copy_location(ast.Constant(value=True), node)
        if node.id == "false":
            return ast.copy_location(ast.Constant(value=False), node)
        return node


def eval_condition(expr: str, variables: dict) -> tuple[bool, object, str]:
    """求值条件表达式，返回 (是否成功, 结果值, 说明)。

    失败时结果值为 False，说明为面向用户的可读原因（语法错误 / 变量未定义 / 求值异常）。
    variables 为运行期变量名 -> 值 的字典。
    小写 true / false 视为布尔字面量（可直接写 true 构成恒真循环），
    大写 True / False 是 Python 关键字本就可用。
    """
    text = (str(expr) or "").strip()
    if not text:
        return False, False, "条件表达式为空"
    code = translate_condition(text)
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError as e:
        return False, False, f"条件表达式语法错误：{e.msg}（第 {e.offset or '?'} 个字符附近）"
    tree = _BoolLiteralFold().visit(tree)
    try:
        result = eval(compile(tree, "<condition>", "eval"),
                      {"__builtins__": _SAFE_BUILTINS}, variables)
    except NameError as e:
        name = e.args[0] if e.args else "未知"
        return False, False, f"变量未定义：{name}"
    except Exception as e:  # TypeError/ZeroDivisionError 等运行期错误
        return False, False, f"条件求值失败：{type(e).__name__}: {e}"
    return True, result, f"条件{'成立' if result else '不成立'}"


def validate_condition_structure(steps) -> list[str]:
    """校验 if/elseif/else/endif 的成对闭合与顺序，返回错误信息列表（空 = 合法）。

    每条错误信息含 1 起的步骤序号，可直接展示给用户。steps 为 FlowStep 列表
    （也兼容仅含 type 属性的简单对象）。
    """
    errors: list[str] = []
    stack: list[dict] = []  # 每层：{"if_idx": int, "has_else": bool}
    for i, s in enumerate(steps):
        t = getattr(s, "type", "")
        if t not in CONDITION_TYPES:
            continue
        if t == "if":
            stack.append({"if_idx": i, "has_else": False})
        elif t == "elseif":
            if not stack:
                errors.append(f"第 {i + 1} 步「否则如果」缺少配对的「if」")
            elif stack[-1]["has_else"]:
                errors.append(f"第 {i + 1} 步「否则如果」不能出现在「else」之后")
        elif t == "else":
            if not stack:
                errors.append(f"第 {i + 1} 步「否则」缺少配对的「if」")
            elif stack[-1]["has_else"]:
                errors.append(f"第 {i + 1} 步「否则」重复（同一 if 块只能有一个 else）")
            else:
                stack[-1]["has_else"] = True
        elif t == "endif":
            if not stack:
                errors.append(f"第 {i + 1} 步「条件结束」缺少配对的「if」")
            else:
                stack.pop()
    for frame in stack:
        errors.append(f"第 {frame['if_idx'] + 1} 步「if」缺少配对的「endif 条件结束」")
    return errors


def build_control_flow(steps) -> tuple[dict[int, int], dict[int, int]]:
    """构建条件分支控制流信息，返回 (false_jump, block_end)。

    - false_jump：if/elseif 条件为假时跳到的「下一个分支头」索引（elseif/else/endif）；
      最后一个分支头（else 或最后一个 if/elseif）直接跳到 endif+1。
    - block_end：每个分支头（if/elseif/else）所属条件块结束后的索引（endif+1），
      供执行引擎在「某分支命中后」跳过整块剩余部分。

    同一层级分支头序列为 [if, elseif, ..., else]。结构非法的孤儿块由
    validate_condition_structure 负责发现，这里尽量不抛异常、也不为其生成条目。
    """
    false_jump: dict[int, int] = {}
    block_end: dict[int, int] = {}
    stack: list[list[int]] = []  # 每层分支头索引序列
    for i, s in enumerate(steps):
        t = getattr(s, "type", "")
        if t == "if":
            stack.append([i])
        elif t == "elseif":
            if stack:
                stack[-1].append(i)
        elif t == "else":
            if stack:
                stack[-1].append(i)
        elif t == "endif":
            if stack:
                frame = stack.pop()
                for j in range(len(frame) - 1):
                    false_jump[frame[j]] = frame[j + 1]
                false_jump[frame[-1]] = i + 1
                for h in frame:
                    block_end[h] = i + 1
    return false_jump, block_end


def match_endif(steps, if_idx: int) -> int | None:
    """返回 if_idx 对应 if 的 endif 索引；无匹配返回 None。"""
    depth = 0
    for i in range(if_idx, len(steps)):
        t = getattr(steps[i], "type", "")
        if t == "if":
            depth += 1
        elif t == "endif":
            depth -= 1
            if depth == 0:
                return i
    return None


def enclosing_if(steps, idx: int) -> int | None:
    """返回包含 idx 位置的最内层 if 索引；idx 不在任何 if 块内返回 None。

    idx 指向 endif 时，返回与之配对的 if（endif 视为块内边界）。
    """
    if idx < 0 or idx >= len(steps):
        return None
    if getattr(steps[idx], "type", "") == "endif":
        # idx 本身是 endif：找与它配对的 if（先递减、归零即命中）
        depth = 0
        for i in range(idx, -1, -1):
            t = getattr(steps[i], "type", "")
            if t == "endif":
                depth += 1
            elif t == "if":
                depth -= 1
                if depth == 0:
                    return i
        return None
    # idx 是块内/分支头步骤：找包含它的最内层 if
    depth = 0
    for i in range(idx, -1, -1):
        t = getattr(steps[i], "type", "")
        if t == "endif":
            depth += 1
        elif t == "if":
            if depth == 0:
                return i
            depth -= 1
    return None


def condition_indent_levels(steps) -> list[int]:
    """返回每个步骤在条件块中的缩进层级（0 = 不缩进），长度与 steps 一致。

    现已由 block_indent_levels 通用化：if / foreach / while 及其结束标记都不缩进，
    只有块内步骤缩进；本函数保留名称以兼容既有调用，行为与 block_indent_levels 一致
    （对纯条件块流程，输出完全等同）。
    """
    return block_indent_levels(steps)


def condition_block_indices(steps, idx: int) -> list[int]:
    """返回 idx 所在条件块的全部步骤索引（含 if 与 endif），按升序。

    idx 指向 if/elseif/else/endif 时返回整个块；指向其它类型或结构不完整时返回空列表。
    """
    t = getattr(steps[idx], "type", "") if 0 <= idx < len(steps) else ""
    if t == "if":
        if_idx = idx
    elif t in ("elseif", "else", "endif"):
        if_idx = enclosing_if(steps, idx)
        if if_idx is None:
            return []
    else:
        return []
    end_idx = match_endif(steps, if_idx)
    if end_idx is None:
        return []
    return list(range(if_idx, end_idx + 1))


def _step_defined_variables(t: str, p: dict) -> list[str]:
    """返回某步骤执行后会定义/赋值的变量名列表；不产出变量则返回空列表。

    foreach 的 item_var / index_var 在循环执行后被定义（循环结束保留最后值），
    因此也计入「后续步骤可引用」的变量，供 while 条件/其它步骤的变量校验使用。
    """
    p = p or {}
    single = {
        "var": (p.get("name") or "").strip(),
        "ocr": (p.get("variable") or "").strip(),
        "text_find": (p.get("variable") or "").strip(),
        "find_image": (p.get("variable") or "").strip(),
        "screenshot": (p.get("variable") or "").strip(),
        "clip_get": (p.get("variable") or "").strip(),
        "py_func": (p.get("result_var") or "").strip(),
    }.get(t)
    if single:
        return [single]
    if t == "foreach":
        out: list[str] = []
        for key in ("item_var", "index_var"):
            v = (p.get(key) or "").strip()
            if v and v not in out:
                out.append(v)
        return out
    return []


def defined_variables_before(steps, declared_variables, index: int) -> set[str]:
    """返回 index 位置之前已定义的变量名集合。

    已定义 = 流程声明变量（declared_variables，含 .name 属性的对象）+
    index 之前所有会产出变量的步骤（var / ocr / text_find / find_image /
    screenshot / clip_get / py_func / foreach 的 item/index）。
    """
    names: set[str] = set()
    for v in declared_variables or []:
        name = (getattr(v, "name", "") or "").strip()
        if name:
            names.add(name)
    for i in range(index):
        s = steps[i]
        for var in _step_defined_variables(getattr(s, "type", ""),
                                           getattr(s, "params", {}) or {}):
            if var:
                names.add(var)
    return names


def check_condition_variables(steps, declared_variables, index: int,
                              expr: str) -> tuple[bool, list[str]]:
    """检查条件表达式的变量是否已在 index 之前定义。

    返回 (是否全部已定义, 缺失变量名列表)，缺失变量按表达式出现顺序排列。
    """
    variables = extract_variables(expr)
    defined = defined_variables_before(steps, declared_variables, index)
    missing = [v for v in variables if v not in defined]
    return not missing, missing


# ---------------- 通用块逻辑（if / foreach / while 共用，type 驱动） ----------------

def block_indent_levels(steps) -> list[int]:
    """返回每个步骤在列表中的缩进层级（0 = 不缩进），长度与 steps 一致。

    规则（一句话：**块骨架都不缩进，只有块里的步骤缩进**）：
      - 起始块（if/foreach/while）与其结束标记（endif/endForeach/endWhile）互相对齐，
        都不缩进；
      - elseif/else 分支头与配对的 if 对齐，不缩进；
      - 它们之间夹着的普通步骤缩进一级，嵌套块整体落在外层缩进之上再多一级。

    示例（数字 = 层级）：
        foreach=0, while=1, wait=2, endWhile=1, wait=1, endForeach=0
        if=0, wait=1, elseif=0, wait=1, endif=0

    采用「遇结束标记收一层」的线性扫描，不依赖结构合法：孤儿结束标记/分支头
    不会让层级变负（下限 0），列表渲染不会错位。
    """
    levels: list[int] = []
    depth = 0
    for s in steps:
        t = getattr(s, "type", "")
        if t in BLOCK_CLOSE_TYPES:
            # 结束标记与配对的起始块对齐：按「退出一层后」的层级记录，再真正退出
            depth = max(depth - 1, 0)
            levels.append(depth)
        elif t in ALL_BRANCH_HEADS:
            # 分支头与配对的 if 对齐：按「退出一层后」记录，但不改 depth
            levels.append(max(depth - 1, 0))
        elif t in BLOCK_OPEN_TYPES:
            levels.append(depth)
            depth += 1
        else:
            levels.append(depth)
    return levels


def match_block_end(steps, open_idx: int) -> int | None:
    """返回 open_idx 对应起始块（if/foreach/while）的结束标记索引；无匹配返回 None。

    与 match_endif 同构：只对「同类型」的起始/结束配对计数，其它类型的块、
    分支头（elseif/else）与普通步骤都视为透明，因此嵌套 if/foreach/while 交错时
    也能正确找到各自配对的结束标记。
    """
    if open_idx < 0 or open_idx >= len(steps):
        return None
    t = getattr(steps[open_idx], "type", "")
    end_t = BLOCK_PAIRS.get(t)
    if end_t is None:
        return None
    depth = 0
    for i in range(open_idx, len(steps)):
        st = getattr(steps[i], "type", "")
        if st == t:
            depth += 1
        elif st == end_t:
            depth -= 1
            if depth == 0:
                return i
    return None


def enclosing_block(steps, idx: int) -> int | None:
    """返回包含 idx 位置的最内层起始块（if/foreach/while）索引。

    idx 指向结束标记时，返回与之配对的起始块；指向其它位置（含分支头、
    起始块自身）时，返回包含它的最内层起始块。不在任何块内返回 None。
    """
    if idx < 0 or idx >= len(steps):
        return None
    t = getattr(steps[idx], "type", "")
    if t in BLOCK_CLOSE_TYPES:
        # idx 本身是结束标记：找与它配对的起始块（先递减、归零即命中）
        open_t = _CLOSE_TO_OPEN[t]
        depth = 0
        for i in range(idx, -1, -1):
            st = getattr(steps[i], "type", "")
            if st == t:
                depth += 1
            elif st == open_t:
                depth -= 1
                if depth == 0:
                    return i
        return None
    # idx 是块内 / 分支头 / 起始块步骤：找包含它的最内层起始块
    depth = 0
    for i in range(idx, -1, -1):
        st = getattr(steps[i], "type", "")
        if st in BLOCK_CLOSE_TYPES:
            depth += 1
        elif st in BLOCK_OPEN_TYPES:
            if depth == 0:
                return i
            depth -= 1
    return None


def block_indices(steps, idx: int) -> list[int]:
    """返回 idx 所在块的全部步骤索引（含起始块与结束标记），按升序。

    idx 指向起始块 / 结束标记 / 分支头时返回整块；指向普通步骤或结构不完整时
    返回空列表。
    """
    if idx < 0 or idx >= len(steps):
        return []
    t = getattr(steps[idx], "type", "")
    if t in BLOCK_OPEN_TYPES:
        open_idx = idx
    elif t in BLOCK_CLOSE_TYPES or t in ALL_BRANCH_HEADS:
        open_idx = enclosing_block(steps, idx)
        if open_idx is None:
            return []
    else:
        return []
    end_idx = match_block_end(steps, open_idx)
    if end_idx is None:
        return []
    return list(range(open_idx, end_idx + 1))


def build_block_tree(steps, start: int = 0, end: int | None = None) -> list:
    """从扁平列表推导逻辑树（内存推导，不落盘）。

    返回节点列表。每个节点为 dict：
      - 普通步骤：{"type": t, "index": i}
      - 块：{"type": t, "index": open_idx, "end": end_idx, "children": [...]}
    块的 children 里含块内所有步骤（含嵌套块与分支头 elseif/else）。
    结构非法时尽量降级：未闭合的起始块当作普通节点，孤儿结束标记当作普通节点，
    不抛异常。
    """
    if end is None:
        end = len(steps)
    nodes: list = []
    i = start
    while i < end:
        t = getattr(steps[i], "type", "")
        if t in BLOCK_OPEN_TYPES:
            e = match_block_end(steps, i)
            if e is not None and e < end:
                children = build_block_tree(steps, i + 1, e)
                nodes.append({"type": t, "index": i, "end": e, "children": children})
                i = e + 1
                continue
        nodes.append({"type": t, "index": i})
        i += 1
    return nodes


def max_block_depth(steps) -> int:
    """返回 steps 中块嵌套的最大深度（0 = 无块）。结构非法时按已闭合部分计算。"""
    depth = 0
    peak = 0
    for s in steps:
        t = getattr(s, "type", "")
        if t in BLOCK_OPEN_TYPES:
            depth += 1
            peak = max(peak, depth)
        elif t in BLOCK_CLOSE_TYPES:
            depth = max(depth - 1, 0)
    return peak


def build_loop_flow(steps) -> dict[int, int]:
    """构建循环控制流信息，返回 {起始块索引: 结束标记索引}。

    只覆盖 foreach / while 两种循环块（if 分支由 build_control_flow 处理），
    供执行引擎在「循环体结束 / 跳过整块」时快速拿到配对位置。
    """
    loop_ends: dict[int, int] = {}
    for i, s in enumerate(steps):
        t = getattr(s, "type", "")
        if t in ("foreach", "while"):
            e = match_block_end(steps, i)
            if e is not None:
                loop_ends[i] = e
    return loop_ends


def validate_block_structure(steps) -> list[str]:
    """校验 if/foreach/while 三类块的成对闭合、顺序与嵌套深度，返回错误信息列表
    （空 = 合法）。每条错误信息含 1 起的步骤序号，可直接展示给用户。

    相比 validate_condition_structure（仅 if），本函数是通用版本：
      - 起始块必须有配对结束标记（if→endif / foreach→endForeach / while→endWhile）
      - 结束标记必须配对上方的同类型起始块（孤儿报错）
      - 交叉错位（起始块被其它类型的结束标记闭合）报错
      - elseif/else 必须位于 if 块内、顺序正确（else 之前可多个 elseif，else 至多一个）
      - 嵌套深度超过 MAX_BLOCK_DEPTH 报错
    """
    errors: list[str] = []
    stack: list[dict] = []  # 每层：{"type": 起始类型, "idx": 索引, "has_else": bool}
    for i, s in enumerate(steps):
        t = getattr(s, "type", "")
        if t in BLOCK_OPEN_TYPES:
            stack.append({"type": t, "idx": i, "has_else": False})
            if len(stack) > MAX_BLOCK_DEPTH:
                errors.append(f"第 {i + 1} 步「{_BLOCK_LABELS.get(t, t)}」"
                              f"嵌套超过 {MAX_BLOCK_DEPTH} 层")
        elif t in BLOCK_CLOSE_TYPES:
            if not stack:
                errors.append(f"第 {i + 1} 步「{_BLOCK_LABELS.get(t, t)}」"
                              f"缺少配对的起始块")
                continue
            top = stack[-1]
            if BLOCK_PAIRS[top["type"]] != t:
                errors.append(f"第 {i + 1} 步「{_BLOCK_LABELS.get(t, t)}」与第 "
                              f"{top['idx'] + 1} 步"
                              f"「{_BLOCK_LABELS.get(top['type'], top['type'])}」交叉错位")
            stack.pop()
        elif t == "elseif":
            if not stack or stack[-1]["type"] != "if":
                errors.append(f"第 {i + 1} 步「否则如果」缺少配对的「条件判断」")
            elif stack[-1]["has_else"]:
                errors.append(f"第 {i + 1} 步「否则如果」不能出现在「否则」之后")
        elif t == "else":
            if not stack or stack[-1]["type"] != "if":
                errors.append(f"第 {i + 1} 步「否则」缺少配对的「条件判断」")
            elif stack[-1]["has_else"]:
                errors.append(f"第 {i + 1} 步「否则」重复（同一条件块只能有一个）")
            else:
                stack[-1]["has_else"] = True
    for frame in stack:
        end_t = BLOCK_PAIRS[frame["type"]]
        errors.append(f"第 {frame['idx'] + 1} 步"
                      f"「{_BLOCK_LABELS.get(frame['type'], frame['type'])}」"
                      f"缺少配对的「{_BLOCK_LABELS.get(end_t, end_t)}」")
    # break / continue 只能位于 foreach/while 循环体内
    for i, s in enumerate(steps):
        t = getattr(s, "type", "")
        if t in ("break", "continue") and enclosing_loop(steps, i) is None:
            errors.append(f"第 {i + 1} 步「{_BLOCK_LABELS.get(t, t)}」"
                          f"只能放在 Foreach/while 循环体内")
    return errors


def empty_loop_bodies(steps) -> list[tuple[int, str]]:
    """返回「空循环体」的 (起始块索引, 类型) 列表（foreach/while 与结束标记之间无步骤）。

    空循环体运行期会跳过 / 不空转，此处仅用于编辑期提示，不视为结构错误。
    """
    result: list[tuple[int, str]] = []
    for i, s in enumerate(steps):
        t = getattr(s, "type", "")
        if t not in ("foreach", "while"):
            continue
        e = match_block_end(steps, i)
        if e is None or e == i + 1:
            result.append((i, t))
    return result


def enclosing_loop(steps, idx: int) -> int | None:
    """返回包含 idx 位置的最内层循环块（foreach/while）起始索引；不在任何循环内返回 None。

    与 enclosing_block 的区别：本函数只追踪 foreach/while 的开启与结束，完全忽略
    if/endif（条件块不影响 break/continue 的合法性——break 位于 if 内、而 if 又位于
    循环内时，break 仍然中断的是那个循环）。
    """
    if idx < 0 or idx >= len(steps):
        return None
    t = getattr(steps[idx], "type", "")
    if t in LOOP_CLOSE_TYPES:
        # idx 本身是循环结束标记：找与它配对的循环起始块（先递减、归零即命中）
        open_t = _LOOP_CLOSE_TO_OPEN[t]
        depth = 0
        for i in range(idx, -1, -1):
            st = getattr(steps[i], "type", "")
            if st == t:
                depth += 1
            elif st == open_t:
                depth -= 1
                if depth == 0:
                    return i
        return None
    # idx 是循环体内步骤：找包含它的最内层循环起始块（if/endif 视为透明）
    depth = 0
    for i in range(idx, -1, -1):
        st = getattr(steps[i], "type", "")
        if st in LOOP_CLOSE_TYPES:
            depth += 1
        elif st in LOOP_OPEN_TYPES:
            if depth == 0:
                return i
            depth -= 1
    return None
