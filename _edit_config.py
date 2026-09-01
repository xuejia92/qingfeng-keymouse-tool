# -*- coding: utf-8 -*-
from pathlib import Path
p = Path('app/config.py')
s = p.read_text(encoding='utf-8')

# 1. Replace FLOW_STEP_TYPES block
start = s.index('FLOW_STEP_TYPES =')
end = s.index('WEB_ACTIONS', start)
# Find start of comment line before WEB_ACTIONS
comment = s.rfind('\n# 网页操作', start, end)
if comment > 0:
    end = comment
old_block = s[start:end].rstrip('\n')
new_block = '''FLOW_STEP_TYPES = {"var": "变量", "log": "日志输出", "ocr": "文字识别",
                   "click": "鼠标点击", "press": "键盘连按", "find": "找图点击",
                   "wait": "延时等待", "web": "网页操作", "app": "打开应用",
                   "close_app": "关闭应用"}

# 变量类型：值 -> 显示名
VARIABLE_TYPES = {"string": "字符串", "integer": "整数", "float": "浮点数",
                  "bool": "布尔型", "list": "列表", "dict": "字典"}'''
s = s[:start] + new_block + '\n\n' + s[end:]

# 2. Add var/log/ocr defaults after find block
marker = '''    if step_type == "find":
        return {
            "image": "", "confidence": 0.85, "interval_ms": 500,
            "click_type": "single", "offset_x": 0, "offset_y": 0,
            "search_timeout_sec": 10.0, "region": "",
        }
'''
assert marker in s
new_defaults = marker + '''    if step_type == "var":
        return {
            "name": "myvar",              # 变量名（流程内唯一）
            "type": "string",             # string / integer / float / bool / list / dict
            "default_value": "",          # 默认值文本，按 type 解析
        }
    if step_type == "log":
        return {
            "variables": "",              # 要输出的变量名，逗号分隔；空=输出全部变量
            "text": "",                   # 附加文本，支持 $变量名 占位
        }
    if step_type == "ocr":
        return {
            "region": "",                 # 识别区域 "x,y,w,h"，空=全屏
            "variable": "",               # 结果保存到的变量名（列表/字典）
            "lang": "ch",                 # PaddleOCR 语言，ch / en
            "multi_ocr": True,            # True=多行文本列表，False=拼接字符串
        }
'''
s = s.replace(marker, new_defaults, 1)

# 3. Add FlowVariable before FlowStep
marker = '\n\n@dataclass\nclass FlowStep:'
assert marker in s
flow_var = '''
@dataclass
class FlowVariable:
    """流程变量定义。

    默认值以字符串形式保存；初始化时按 type 解析为真实 Python 类型。
    """
    name: str = ""
    type: str = "string"          # string / integer / float / bool / list / dict
    default_value: str = ""

    def __post_init__(self):
        self.name = (self.name or "").strip()
        if self.type not in VARIABLE_TYPES:
            self.type = "string"

    def parse_value(self, value: str = None) -> object:
        """按类型解析默认值字符串。"""
        from .values import parse_value as _parse
        return _parse(self.type, self.default_value if value is None else value)

    def summary(self) -> str:
        t = VARIABLE_TYPES.get(self.type, self.type)
        return f"{self.name}  [{t}]"
'''
s = s.replace(marker, flow_var + marker, 1)

# 4. Add summary cases
marker = '''    def summary(self) -> str:
        p = self.params
        try:
            if self.type == "click":'''
assert marker in s
summary_new = '''    def summary(self) -> str:
        p = self.params
        try:
            if self.type == "var":
                name = p.get("name") or "未命名"
                t = VARIABLE_TYPES.get(p.get("type"), p.get("type", "string"))
                return f"{name}  [{t}]"
            if self.type == "log":
                text = p.get("text") or ""
                vars = p.get("variables") or ""
                if text:
                    return f"输出 {text}"
                return f"输出变量 {vars}" if vars else "输出全部变量"
            if self.type == "ocr":
                var = p.get("variable") or "未指定变量"
                region = p.get("region") or "全屏"
                return f"{region} → {var}"
            if self.type == "click":'''
s = s.replace(marker, summary_new, 1)

# 5. Add Flow.variables
marker = '''    steps: list = field(default_factory=list)   # list[FlowStep]
    hotkey: str = ""'''
assert marker in s
s = s.replace(marker, '''    steps: list = field(default_factory=list)   # list[FlowStep]
    variables: list = field(default_factory=list) # list[FlowVariable]
    hotkey: str = ""''', 1)

p.write_text(s, encoding='utf-8')
print('config.py edited')
