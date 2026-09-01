"""流程变量的值解析、格式化与引用替换。

变量默认值以文本保存；运行期按变量声明类型解析为真实 Python 值。
支持在日志文本、其它参数字段中使用 $变量名 引用（简单占位替换）。
"""
from __future__ import annotations

import json
import re
from typing import Any


def parse_value(value_type: str, text: str = "") -> Any:
    """把文本按变量类型解析为 Python 值。

    string：原样字符串
    integer：整数；解析失败抛 ValueError
    float：浮点数
    bool：true/false/yes/no/1/0/on/off（大小写不敏感）
    list / dict：JSON 数组 / JSON 对象
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
        data = text.strip()
        if not data:
            return []
        try:
            val = json.loads(data)
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(f"变量默认值不是 JSON 列表: {text!r}") from e
        if not isinstance(val, list):
            raise ValueError(f"变量类型是 list，但默认值不是数组: {text!r}")
        return val
    if t == "dict":
        data = text.strip()
        if not data:
            return {}
        try:
            val = json.loads(data)
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(f"变量默认值不是 JSON 对象: {text!r}") from e
        if not isinstance(val, dict):
            raise ValueError(f"变量类型是 dict，但默认值不是对象: {text!r}")
        return val
    raise ValueError(f"未知变量类型: {value_type}")


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
