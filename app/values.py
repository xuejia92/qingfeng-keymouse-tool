"""流程变量的值解析、格式化与引用替换。

变量默认值以文本保存；运行期按变量声明类型解析为真实 Python 值。
支持在日志文本、其它参数字段中使用 $变量名 引用（简单占位替换）。
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any


def parse_value(value_type: str, text: str = "") -> Any:
    """把文本按变量类型解析为 Python 值。

    string：原样字符串
    integer：整数；解析失败抛 ValueError
    float：浮点数
    bool：true/false/yes/no/1/0/on/off（大小写不敏感）
    list / dict：JSON 数组 / JSON 对象（双引号）；同时兼容 Python 字面量写法
    （单引号），如 ['a','b'] 与 {"a":1}
    """
    t = (value_type or "string").strip().lower()
    text = str(text or "")
    if t == "string":
        return text
    if t == "integer":
        try:
            return int(text.strip())
        except ValueError:
            raise ValueError(f"变量默认值不是整数: {text!r}")
    if t == "float":
        try:
            return float(text.strip())
        except ValueError:
            raise ValueError(f"变量默认值不是浮点数: {text!r}")
    if t == "bool":
        low = text.strip().lower()
        if low in ("true", "yes", "1", "on"):
            return True
        if low in ("false", "no", "0", "off", ""):
            return False
        raise ValueError(f"变量默认值不是布尔值: {text!r}")
    if t == "list":
        return _parse_container(text, "list")
    if t == "dict":
        return _parse_container(text, "dict")
    raise ValueError(f"未知变量类型: {value_type}")


def _parse_container(text: str, kind: str) -> Any:
    """解析 list / dict 默认值。

    先按 JSON（双引号）解析，失败再用 ast.literal_eval 兜底，以支持单引号包裹的
    Python 字面量（如 ['a','b']、{'a':1}）。两种写法都解析不出的给出明确报错。
    """
    data = text.strip()
    if not data:
        return [] if kind == "list" else {}
    val = None
    try:
        val = json.loads(data)
    except ValueError:
        val = None
    if val is None:
        # json.loads 返回 None 也可能是输入就是 "null"，此时仍需走 literal_eval 兜底
        try:
            val = ast.literal_eval(data)
        except (ValueError, SyntaxError):
            what = "数组" if kind == "list" else "对象"
            raise ValueError(f"变量默认值不是有效的 {what} 字面量: {text!r}")
    if kind == "list" and not isinstance(val, list):
        raise ValueError(f"变量类型是 list，但默认值不是数组: {text!r}")
    if kind == "dict" and not isinstance(val, dict):
        raise ValueError(f"变量类型是 dict，但默认值不是对象: {text!r}")
    return val


_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def resolve_references(text: str, store: dict) -> str:
    """把文本里的 $变量名 替换为变量的短字符串形式；未知变量原样保留。"""
    if not text:
        return ""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in store:
            return format_value(store[name], brief=True)
        return m.group(0)
    return _REF_RE.sub(repl, text)


def resolve_variable(expr: str, store: dict) -> tuple[bool, Any, str]:
    """解析「变量引用表达式」，返回 (是否成功, 值, 说明)。

    在变量名之外支持 Python 下标/属性语法，可直接取容器内的元素，例如：
      - aaa            -> store["aaa"]
      - aaa['a']       -> store["aaa"]["a"]（字典按键取值）
      - arr[0] / arr[-1] -> store["arr"][0]（列表按索引取值）
      - aaa['a']['b']  -> 多级嵌套下标
    失败时返回 (False, None, 可读原因)，不抛异常。禁止函数调用（len/方法等），
    只放行 名称 / 下标 / 属性 / 字面量 组成的表达式。
    """
    text = (expr or "").strip()
    if not text:
        return False, None, "变量表达式为空"
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        return False, None, f"变量表达式语法错误：{e.msg}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Lambda)):
            return False, None, "变量表达式不支持函数调用"
    try:
        value = eval(compile(tree, "<variable>", "eval"), {"__builtins__": {}}, store)
    except NameError as e:
        name = e.args[0] if e.args else "未知"
        return False, None, f"变量未定义：{name}"
    except KeyError as e:
        return False, None, f"键不存在：{e.args[0]!r}"
    except IndexError:
        return False, None, "列表下标越界"
    except Exception as e:
        return False, None, f"变量解析失败：{type(e).__name__}: {e}"
    return True, value, ""


def value_text(value: Any, brief: bool = True) -> str:
    """变量的短文本形式：str 原样、bool 用 true/false、其他用 JSON。"""
    return format_value(value, brief=brief)


def format_value(value: Any, brief: bool = True) -> str:
    """把变量值转成可显示/可替换的文本。"""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and brief:
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# ---- 变量步骤默认值表达式 ----
# 默认值大部分时候是「按类型填的字面量/文本」；当它含 $变量引用、或整体是
# 运算符/函数/比较/下标表达式时，交给与条件分支同一套安全求值引擎计算。
_EXPR_OP_NODES = (ast.BinOp, ast.UnaryOp, ast.Call, ast.Subscript,
                  ast.Compare, ast.BoolOp, ast.IfExp)


def _has_ref_dollar(text: str) -> bool:
    """文本的字符串字面量之外是否存在 $（引号内的 $ 不算引用标记）。"""
    s = str(text or "")
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"'):
            quote = c
            i += 1
            while i < n:
                if s[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if s[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "$":
            return True
        i += 1
    return False


def _dollar_to_bare(text: str) -> str:
    """把引号外的 $变量名 替换成裸名（$a -> a），供表达式求值引用变量。"""
    s = str(text or "")
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
        if c == "$" and i + 1 < n and (s[i + 1].isalpha() or s[i + 1] == "_"):
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            out.append(s[i + 1:j])
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _eval_then_parse(value_type: str, code: str, store: dict, raw: str):
    """走条件引擎求值表达式，再把结果文本化后按声明类型解析。"""
    from .conditions import eval_condition
    ok, val, why = eval_condition(code, store)
    if not ok:
        raise ValueError(why)
    return parse_value(value_type, format_value(val, brief=True))


def eval_var_default(value_type: str, text: str, store: dict) -> Any:
    """把「变量」步骤的默认值解析为按类型约束的值。

    语法分三种（自动识别）：
      - 字面量/普通文本：hello、2、true、[1,2]、{"a":1}、"带引号字符串"、含空格的
        普通句子等，维持 parse_value 行为（string 类型任意文本原样保留）。
      - 表达式：含引号外的 $变量引用且去掉 $ 后是合法表达式（$a、$count + 1、
        $a + "!"、len($s)、$n > 2、arr[0]），或整体是带运算符/函数/下标/比较的
        Python 表达式（2+3）。与条件分支同一套白名单内置函数
        （len/abs/int/str/…），变量按当时值注入；结果文本化后按 value_type 解析。
      - $ 占位文本：$ 出现在普通句子里、去掉 $ 也拼不成表达式时（如 "你好，$a！"），
        按简单占位替换（引用未知变量保持 $name 原样）。
    表达式求值失败（语法错误 / 变量未定义 / 除零等）抛 ValueError；
    想保留字面 $ 文本时把它写在普通句子里（如 "见 $zz 价"），单独一个 $引用
    且变量未定义会报「变量未定义」。
    """
    raw = str(text or "")
    if _has_ref_dollar(raw):
        code = _dollar_to_bare(raw)
        try:
            ast.parse(code, mode="eval")
        except SyntaxError:
            # $ 是写在普通句子里的占位（中文混排等）→ 简单占位替换
            return parse_value(value_type, resolve_references(raw, store))
        return _eval_then_parse(value_type, code, store, raw)
    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError:
        # 不是合法表达式（含空格等普通文本）→ 按字面量文本解析
        return parse_value(value_type, raw)
    body = tree.body
    if not isinstance(body, _EXPR_OP_NODES):
        # 纯字面量 / 裸词 / 列表字典字面量 → 原 parse_value 路径
        return parse_value(value_type, raw)
    return _eval_then_parse(value_type, raw, store, raw)


def eval_expression_value(text: str, store: dict) -> tuple[bool, Any, str]:
    """把用户输入（变量名/下标/$引用/函数/运算符表达式）求值为原始值。

    供 foreach 数据源等「要值本身、不做类型投影」的场景使用：
      - 裸变量名 arr / 下标 arr[0] / aaa['a']；
      - $变量引用：$arr、$i + 1、$arr[slice(0, $k)]、$start + $step；
      - 字面量与函数：range(0, 3)、sorted($arr)、slice(0, $k)、len($s)
        （可用函数白名单见 conditions._SAFE_BUILTINS）。
    返回 (成功?, 值, 原因)；语法错误/变量未定义/求值异常均不抛异常。
    """
    from .conditions import eval_condition
    raw = str(text or "").strip()
    if not raw:
        return False, None, "表达式为空"
    code = _dollar_to_bare(raw)
    try:
        ast.parse(code, mode="eval")
    except SyntaxError as e:
        return False, None, f"表达式语法错误：{e.msg}"
    ok, val, why = eval_condition(code, store)
    if not ok and why.startswith("条件求值失败"):
        why = "表达式求值失败" + why[len("条件求值失败"):]
    return ok, val, why
