"""配置模型与 JSON 持久化。

运行期目录规则：
- 源码运行：config.json / flows/ / templates/ / app.log 位于项目根目录
- 打包运行（Nuitka / PyInstaller）：可写文件（config.json、flows/、templates/、
  app.log）位于 exe 同级目录；只读资源（assets/ 图标等）随包内路径解析

流程存储：每个流程一个独立文件 flows/<流程名>.json（导入/导出共用同一格式）；
重名流程自动加 _<id> 后缀区分；config.json 不再保存流程，
旧版内嵌流程（及旧版 <流程id>.json 命名文件）在加载时自动兼容/迁移。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field

APP_NAME = "清风自动化键鼠工具"

def is_compiled() -> bool:
    """是否运行在打包后的环境里（PyInstaller 设 sys.frozen；Nuitka 注入 __compiled__）。

    全项目只在这里探测一次，其他模块 import 本函数即可。各写一遍迟早会出现
    两处判断逻辑不一致的情况，而且抽出来才能被单元测试覆盖。
    """
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def compiled_original_argv0() -> str | None:
    """Nuitka onefile 记录的原始 exe 路径（避免取到临时解压目录里的二进制）。

    PyInstaller 下没有这个变量，返回 None。
    """
    obj = globals().get("__compiled__")
    return str(getattr(obj, "original_argv0", None) or "") or None


_COMPILED = is_compiled()
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))            # app/
_PROJECT_DIR = os.path.dirname(_THIS_DIR)                          # 项目根 / 包内根

if _COMPILED:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))   # exe 所在目录
    RESOURCE_DIR = _PROJECT_DIR                                    # 包内 assets 的根
else:
    BASE_DIR = _PROJECT_DIR
    RESOURCE_DIR = _PROJECT_DIR

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
FLOWS_DIR = os.path.join(BASE_DIR, "flows")     # 每个流程一个 <流程id>.json
LOG_PATH = os.path.join(BASE_DIR, "app.log")


def repair_template(filename: str) -> str | None:
    """程序 templates 目录缺某模板时，向上级目录搜索 templates/<同名文件> 并复制回来。

    场景：exe 在 dist\\ 下运行，而模板截图保存在项目 templates\\ 里。
    """
    import shutil
    name = os.path.basename(filename or "")
    if not name:
        return None
    dst = os.path.join(TEMPLATE_DIR, name)
    if os.path.isfile(dst):
        return dst
    d = BASE_DIR
    for _ in range(4):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
        cand = os.path.join(d, "templates", name)
        if os.path.isfile(cand):
            try:
                shutil.copyfile(cand, dst)
                return dst
            except OSError:
                return None
    return None


def resolve_template_path(filename: str, image_path: str = "") -> str | None:
    """模板文件解析：程序 templates 目录优先 → 记录的绝对路径 → 向上搜索自动修复。"""
    filename = (filename or "").strip()
    if filename:
        p = os.path.join(TEMPLATE_DIR, os.path.basename(filename))
        if os.path.isfile(p):
            return p
    image_path = (image_path or "").strip()
    if image_path and os.path.isfile(image_path):
        return image_path
    if filename and os.path.isabs(filename) and os.path.isfile(filename):
        return filename
    return repair_template(filename)


def ensure_dirs() -> None:
    """确保运行期目录存在：程序目录下的 templates/ 与 flows/（config.json、app.log 自动生成）。"""
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    try:
        os.makedirs(FLOWS_DIR, exist_ok=True)
    except OSError:
        pass


def resource_path(rel: str) -> str:
    """只读打包资源的绝对路径。"""
    return os.path.join(RESOURCE_DIR, rel)


def clamp(v, lo, hi):
    try:
        v = float(v) if isinstance(lo, float) or isinstance(hi, float) else int(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


@dataclass
class ClickerConfig:
    mouse_button: str = "left"      # left / right / middle
    click_type: str = "single"      # single / double
    interval_ms: int = 100
    fixed_position: bool = False    # False=跟随当前鼠标位置
    pos_x: int = 0
    pos_y: int = 0
    count: int = 0                  # 0=无限
    duration_sec: float = 0.0       # 0=不限
    hotkey: str = "f6"


@dataclass
class PresserConfig:
    keys: str = "space"             # keyboard 库格式，如 "space"、"ctrl+c"
    interval_ms: int = 100
    count: int = 0
    duration_sec: float = 0.0
    hotkey: str = "f7"


def parse_region_str(region: str) -> tuple[int, int, int, int] | None:
    """解析 "x,y,w,h" 找图区域字符串，无效或为空返回 None（=全屏）。"""
    if not region:
        return None
    try:
        x, y, w, h = (int(v.strip()) for v in str(region).split(","))
    except (ValueError, AttributeError, TypeError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


@dataclass
class FindTask:
    id: str = ""
    name: str = "新建找图任务"
    image: str = ""                 # templates/ 下的文件名
    image_path: str = ""            # 模板绝对路径备份（跨目录运行时兜底）
    enabled: bool = True
    interval_ms: int = 500          # 命中点击后的轮询间隔
    confidence: float = 0.85        # 匹配置信度阈值 0.5~0.99
    click_type: str = "single"      # single / double / right
    offset_x: int = 0
    offset_y: int = 0
    search_timeout_sec: float = 10.0  # 单轮等待目标出现的时间，0=一直等
    count: int = 1                  # 命中点击次数上限，0=无限（默认命中 1 次即停）
    duration_sec: float = 0.0
    hotkey: str = "f8"
    region: str = ""                # 找图区域 "x,y,w,h"（物理像素，虚拟桌面坐标），空=全屏

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]

    def region_tuple(self) -> tuple[int, int, int, int] | None:
        """解析找图区域，无效或为空返回 None（=全屏）。"""
        return parse_region_str(self.region)


# ---------------- 自动化流程 ----------------

FLOW_STEP_TYPES = {"var": "变量", "log": "日志输出", "ocr": "文字识别",
                   "text_find": "文字查找", "screenshot": "截图",
                   "find_image": "找图",
                   "click": "鼠标点击", "press": "键盘连按", "find": "找图点击",
                   "wait": "延时等待", "web": "网页操作", "app": "打开应用",
                   "close_app": "关闭应用", "clip_set": "赋值剪贴板",
                   "clip_get": "获取剪贴板内容"}

# 变量类型：值 -> 显示名
VARIABLE_TYPES = {"string": "字符串", "integer": "整数", "float": "浮点数",
                  "bool": "布尔型", "list": "列表", "dict": "字典"}


# 网页操作（DrissionPage）的子动作：值 -> 显示名
WEB_ACTIONS = {"open": "打开网址", "close_tab": "关闭标签页", "close_browser": "关闭浏览器"}


def default_step_params(step_type: str, clicker: "ClickerConfig | None" = None,
                        presser: "PresserConfig | None" = None) -> dict:
    """新建流程步骤的默认参数。click/press 可从主界面快照复制。"""
    if step_type == "click":
        c = clicker or ClickerConfig()
        return {
            "mouse_button": c.mouse_button, "click_type": c.click_type,
            "interval_ms": c.interval_ms, "fixed_position": c.fixed_position,
            "pos_x": c.pos_x, "pos_y": c.pos_y,
            "pos_var": "",                      # 坐标变量：值形如 "64,63"（x,y）或 "100,200,400,500"（区域取中心），非空时优先于固定坐标
            "count": c.count if c.count > 0 else 1, "duration_sec": c.duration_sec,
            # 后台操作：按窗口标题动态查找目标窗口（句柄每次重启都变，不能存死）
            "background": False, "window_title": "",
        }
    if step_type == "press":
        p = presser or PresserConfig()
        keys = p.keys if p.keys else "space"
        return {
            "keys": keys, "interval_ms": p.interval_ms,
            "count": p.count if p.count > 0 else 1, "duration_sec": p.duration_sec,
            # 后台操作：按窗口标题动态查找目标窗口
            "background": False, "window_title": "",
        }
    if step_type == "find":
        return {
            "image": "", "confidence": 0.85, "interval_ms": 500,
            "click_type": "single", "offset_x": 0, "offset_y": 0,
            "search_timeout_sec": 10.0, "region": "",
        }
    if step_type == "var":
        return {
            "name": "",                   # 变量名（流程内唯一，默认留空让用户填）
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
            "lang": "ch",                 # 识别语言（RapidOCR 默认中英混合，兼容占位）
            "multi_ocr": True,            # True=多行文本列表，False=拼接字符串
        }
    if step_type == "text_find":
        return {
            "text": "",                   # 要查找的文字，支持 $变量名 引用
            "region": "",                 # 查找区域 "x,y,w,h"，空=全屏
            "click": False,               # True=找到后点击该文字
            "click_button": "left",       # left / right
            "variable": "",               # 结果变量：未勾选点击时写坐标 "x,y"，未找到写 false
        }
    if step_type == "wait":
        return {"seconds": 1.0}
    if step_type == "app":
        return {
            "path": "",                   # 应用路径（可浏览）
            "wait_sec": 2.0,              # 启动后等待的秒数（给应用留出加载时间）
        }
    if step_type == "close_app":
        return {
            "target": "",                 # 显示用：完整描述（如 Google Chrome — chrome.exe「百度」）
            "process": "",                # 运行时用：进程名（如 chrome.exe）
            "wait_sec": 0.5,              # 发出关闭命令后的等待时间
        }
    if step_type == "web":
        # 启动模式与标签范围的可选值见 app/web_actors.py（LAUNCH_MODES / TAB_SCOPES）
        return {
            "action": "open",
            "url": "",
            "launch_mode": "front",        # front / headless / background
            "tab_target": "reuse",         # reuse=当前标签 / new=新标签
            "load_timeout_sec": 20.0,
            "wait_after_sec": 0.0,
            "tab_scope": "current",        # current / others / match
            "match_text": "",
        }
    if step_type == "clip_set":
        return {
            "name": "",                   # 要写入剪贴板的变量名（与 text 二选一，变量优先）
            "text": "",                   # 直接写入剪贴板的文本，支持 $变量名 引用
        }
    if step_type == "clip_get":
        return {
            "variable": "",               # 接收剪贴板内容的变量名
        }
    if step_type == "find_image":
        return {
            "image": "",                 # 模板图文件名（templates/ 下，截屏/上传生成）
            "image_path": "",            # 模板图绝对路径（跨目录运行时兜底）
            "confidence": 0.85,          # 匹配置信度阈值 0.5~0.99
            "region": "",                # 查找区域 "x,y,w,h"（物理像素），空=全屏
            "variable": "",              # 结果变量：找到写矩形区域 "左上x,左上y,右下x,右下y"，未找到写 false
            "preview": False,            # 效果预览：找到后在目标区域画红框
            "preview_duration": 1.0,     # 红框持续时间（秒），默认 1 秒
        }
    if step_type == "screenshot":
        return {
            "region": "",                 # 指定区域 "x,y,w,h"（物理像素，必填）
            "save_mode": "variable",      # variable=默认保存 / choose=自选保存（弹窗）
            "variable": "",               # 截图绝对路径写入的结果变量（默认保存必填，自选保存可选）
        }
    raise ValueError(f"未知步骤类型: {step_type}")

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


@dataclass
class FlowStep:
    type: str                       # click / press / find / wait / web
    name: str = ""
    params: dict = field(default_factory=dict)
    continue_on_fail: bool = False  # 仅对 find / web 步骤有意义
    pair_id: str = ""               # 网页配对：同一 pair_id 的「打开网址+关闭浏览器」成对出现

    def __post_init__(self):
        if self.type not in FLOW_STEP_TYPES:
            raise ValueError(f"未知步骤类型: {self.type}")
        if not self.name:
            self.name = FLOW_STEP_TYPES[self.type]
        merged = default_step_params(self.type)
        merged.update({k: v for k, v in (self.params or {}).items() if v is not None})
        self.params = merged

    def summary(self) -> str:
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
            if self.type == "text_find":
                text = p.get("text") or "未填文字"
                if len(text) > 20:
                    text = text[:19] + "…"
                act = "点击" if p.get("click") else "返回坐标"
                return f"查找「{text}」· {act}"
            if self.type == "click":
                btn = {"left": "左键", "right": "右键", "middle": "中键"}.get(p["mouse_button"], "左键")
                ct = "双击" if p["click_type"] == "double" else "单击"
                cnt = "无限" if int(p["count"]) == 0 else f"×{p['count']}"
                bg = " · 置顶" if p.get("background") else ""
                pos = ""
                if p.get("fixed_position"):
                    pv = p.get("pos_var") or ""
                    if pv:
                        pos = f" · 坐标(变量 {pv})"
                    else:
                        # 兼容旧配置：pos_x_var / pos_y_var 各自独立
                        xv, yv = p.get("pos_x_var") or "", p.get("pos_y_var") or ""
                        if xv or yv:
                            pos = f" · 坐标({xv or '固定'},{yv or '固定'})"
                return f"{btn} {ct} · {p['interval_ms']}ms {cnt}{bg}{pos}"
            if self.type == "press":
                from .keymap import hotkey_display
                cnt = "无限" if int(p["count"]) == 0 else f"×{p['count']}"
                bg = " · 置顶" if p.get("background") else ""
                return f"{hotkey_display(p['keys'])} · {p['interval_ms']}ms {cnt}{bg}"
            if self.type == "find":
                img = p.get("image") or "未选模板"
                return f"{img} · 置信度{float(p['confidence']):.2f}"
            if self.type == "wait":
                return f"等待 {float(p['seconds']):g} 秒"
            if self.type == "app":
                path = p.get("path") or "未选应用"
                name = os.path.basename(path) if path else "未选应用"
                wait = float(p.get("wait_sec") or 0)
                return f"{name}" + (f" · 等待 {wait:g}s" if wait > 0 else "")
            if self.type == "close_app":
                target = p.get("target") or ""
                if not target:
                    return "关闭 未填应用"
                if len(target) > 40:
                    target = target[:39] + "…"
                return f"关闭 {target}"
            if self.type == "clip_set":
                name = p.get("name") or ""
                text = (p.get("text") or "").strip()
                if text and not name:
                    if len(text) > 20:
                        text = text[:19] + "…"
                    return f"「{text}」 → 剪贴板"
                return f"{name or '未选变量'} → 剪贴板"
            if self.type == "clip_get":
                return f"剪贴板 → {p.get('variable') or '未指定变量'}"
            if self.type == "find_image":
                img = os.path.basename(p.get("image") or "") or "未选模板"
                return f"找图 {img} → {p.get('variable') or '未指定变量'}"
            if self.type == "screenshot":
                var = p.get("variable") or ""
                if p.get("save_mode") == "choose":
                    return f"截图 → 自选保存 → {var}" if var else "截图 → 自选保存"
                return f"截图 → {var}" if var else "截图 → 默认保存"
            if self.type == "web":
                act = p.get("action")
                if act == "open":
                    url = p.get("url") or "未填网址"
                    if len(url) > 38:
                        url = url[:37] + "…"
                    mode = {"front": "前台", "headless": "无头", "background": "后台"}.get(
                        p.get("launch_mode"), "前台")
                    where = "新标签" if p.get("tab_target") == "new" else "当前标签"
                    return f"{url} · {mode}{where}"
                if act == "close_browser":
                    return "关闭浏览器"
                if act == "close_tab":
                    scope = p.get("tab_scope", "current")
                    if scope == "match":
                        return f"关闭匹配「{p.get('match_text') or ''}」的标签"
                    return {"current": "关闭当前标签", "others": "关闭其他标签"}.get(
                        scope, "关闭当前标签")
                return ""   # 未知动作：宁可显示空白，也不要张冠李戴
        except (KeyError, TypeError, ValueError):
            pass
        return ""


def web_action(step: FlowStep) -> str:
    """网页步骤的子动作（open / close_tab / close_browser）；非 web 步骤返回空串。"""
    return (step.params or {}).get("action", "") if step.type == "web" else ""


def repair_web_pairs(steps: list) -> bool:
    """校验网页配对：同一 pair_id 的步骤必须恰好两个、且动作分别为 open / close_browser。

    拖动网页模块时自动生成一对（打开网址 + 关闭浏览器），这对步骤共享 pair_id；
    编辑时若用户把动作改成别的（或复制/排序导致配对被破坏），这里负责修复：
    不满足「一对 open + close_browser」的组合就把该组所有 pair_id 清空（解除配对），
    避免出现删除一个却牵连无关步骤的情况。返回是否有步骤被解除配对。
    """
    changed = False
    groups: dict[str, list[FlowStep]] = {}
    for s in steps:
        pid = s.pair_id
        if pid:
            groups.setdefault(pid, []).append(s)
    for pid, group in groups.items():
        acts = {web_action(s) for s in group}
        if len(group) != 2 or acts != {"open", "close_browser"}:
            for s in group:
                s.pair_id = ""
            changed = True
    return changed


@dataclass
class Flow:
    id: str = ""
    name: str = "新建流程"
    group: str = ""                             # 所属分组名；空 = 未分组
    steps: list = field(default_factory=list)   # list[FlowStep]
    variables: list = field(default_factory=list) # list[FlowVariable]
    hotkey: str = ""                            # 可选启停热键
    loops: int = 1                              # 整体执行轮数，0=无限

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.loops = int(clamp(self.loops, 0, 9999))


# ---------------- 定时任务 ----------------

# 调度模式：值 -> 显示名。second/minute/hour/day/week/month/once/cron
SCHEDULE_MODES = {
    "second": "每秒",
    "minute": "每分",
    "hour": "每时",
    "day": "每天",
    "week": "每周",
    "month": "每月",
    "once": "指定时间",
    "cron": "Cron 表达式",
}

# 每周可选星期：1=周一 … 7=周日（与 datetime.isoweekday() 对齐）
WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四",
                 5: "周五", 6: "周六", 7: "周日"}


@dataclass
class ScheduleTask:
    """定时任务：按规则在指定时间自动运行某个流程。

    调度规则由 mode 决定，其余字段按需使用：
    - second/minute/hour/day：interval 为「每 N 秒/分/时/天」
    - day/week/month：at_time 为「HH:MM」触发时刻
    - week：weekdays 为 1~7（1=周一…7=周日）
    - month：monthdays 为 1~31
    - once：once_at 为「YYYY-MM-DD HH:MM」一次性触发
    - cron：cron 为标准 5 段（或 6 段含秒）表达式
    """
    id: str = ""
    name: str = "新建定时任务"
    group: str = ""                    # 所属分组名；空 = 未分组
    flow_id: str = ""                  # 要运行的流程 id
    flow_name: str = ""                # 冗余流程名（流程改名/删除后兜底显示）
    enabled: bool = True
    mode: str = "day"                  # second/minute/hour/day/week/month/once/cron
    interval: int = 1                  # 每 N 秒/分/时/天
    at_time: str = "09:00"             # 每天/每周/每月 的触发时分
    weekdays: list = field(default_factory=list)    # 每周：1~7
    monthdays: list = field(default_factory=list)   # 每月：1~31
    once_at: str = ""                  # 指定时间「YYYY-MM-DD HH:MM」
    cron: str = ""                     # cron 表达式
    last_run: str = ""                 # 上次运行时间「YYYY-MM-DD HH:MM:SS」
    next_run: str = ""                 # 下次运行时间「YYYY-MM-DD HH:MM:SS」
    missed_fires: int = 0              # 连续因流程繁忙被跳过的次数（一次任务用，超 3 次放弃）
    last_alert_date: str = ""          # 上次状态栏告警日期「YYYY-MM-DD」；同一天同一任务最多提示一次

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if self.mode not in SCHEDULE_MODES:
            self.mode = "day"


def schedule_from_dict(data: dict) -> ScheduleTask:
    """字典 -> ScheduleTask；字段缺失/非法时回退默认值，不抛异常。"""
    if not isinstance(data, dict):
        data = {}
    weekdays = [int(d) for d in data.get("weekdays", []) if isinstance(d, (int, float))]
    monthdays = [int(d) for d in data.get("monthdays", []) if isinstance(d, (int, float))]
    mode = str(data.get("mode", "day") or "day")
    if mode not in SCHEDULE_MODES:
        mode = "day"
    return ScheduleTask(
        id=str(data.get("id") or uuid.uuid4().hex[:12]),
        name=str(data.get("name", "定时任务"))[:50],
        group=str(data.get("group", "") or "")[:50],
        flow_id=str(data.get("flow_id", "") or "")[:32],
        flow_name=str(data.get("flow_name", "") or "")[:50],
        enabled=bool(data.get("enabled", True)),
        mode=mode,
        interval=int(clamp(data.get("interval", 1), 1, 99999)),
        at_time=str(data.get("at_time", "09:00") or "09:00")[:5],
        weekdays=weekdays,
        monthdays=monthdays,
        once_at=str(data.get("once_at", "") or ""),
        cron=str(data.get("cron", "") or ""),
        last_run=str(data.get("last_run", "") or ""),
        next_run=str(data.get("next_run", "") or ""),
        missed_fires=int(clamp(data.get("missed_fires", 0), 0, 999)),
        last_alert_date=str(data.get("last_alert_date", "") or "")[:10],
    )


# ---------------- 流程独立文件（flows/ 目录）与导入 / 导出 ----------------

def flow_to_dict(flow: Flow, order: int = 0) -> dict:
    """流程 -> 字典。flows/ 存储与导入导出共用同一格式。"""
    data = asdict(flow)
    data["order"] = int(order)
    return data


def flow_from_dict(data: dict) -> Flow | None:
    """字典 -> Flow；结构非法返回 None（坏步骤跳过，其余可用即成功）。

    必须含 steps 列表才视为流程（避免把 flows/ 目录里无关的 json 当成空流程）。
    """
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        return None
    try:
        steps = []
        for s in data["steps"]:
            try:
                steps.append(FlowStep(
                    type=str(s.get("type", "")),
                    name=str(s.get("name", ""))[:50],
                    params=dict(s.get("params", {}) or {}),
                    continue_on_fail=bool(s.get("continue_on_fail", False)),
                    pair_id=str(s.get("pair_id", ""))[:32],
                ))
            except (ValueError, TypeError, AttributeError):
                continue
        variables = []
        for v in data.get("variables", []) if isinstance(data.get("variables"), list) else []:
            try:
                variables.append(FlowVariable(
                    name=str(v.get("name", ""))[:50],
                    type=str(v.get("type", "string")),
                    default_value=str(v.get("default_value", "") or ""),
                ))
            except (TypeError, ValueError, AttributeError):
                continue
        flow = Flow(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name", "流程"))[:50],
            group=str(data.get("group", "") or "")[:50],
            steps=steps,
            variables=variables,
            hotkey=str(data.get("hotkey", "")),
            loops=int(clamp(data.get("loops", 1), 0, 9999)),
        )
        repair_web_pairs(flow.steps)   # 加载即修复：保证配对一致（如手动编辑 json 残留）
        return flow
    except (TypeError, ValueError, AttributeError):
        return None


def _read_flow_json(path: str) -> dict | None:
    """读取 json 并确认是流程文件（须含 steps 列表）；否则返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("steps"), list) else None


def flow_from_file(path: str) -> Flow | None:
    """从 .json 文件读取一个流程（导入用）；文件缺失或格式无效返回 None。"""
    data = _read_flow_json(path)
    return flow_from_dict(data) if data is not None else None


_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                     *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    """流程名 -> 合法且安全的 Windows 文件名主干（替换非法字符、去掉首尾空格点）。"""
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    base = base.strip(" .") or "流程"
    if base.upper() in _WINDOWS_RESERVED:   # CON/NUL 等保留名不能直接做文件名
        base = "_" + base
    return base


def _is_flow_id(stem: str) -> bool:
    """旧版存储文件名 = 12 位十六进制 id（用于兼容识别，现已改为按流程名命名）。"""
    s = stem.lower()
    return len(s) == 12 and all(c in "0123456789abcdef" for c in s)


def _atomic_write_json(path: str, data) -> bool:
    """原子写 json：先写同目录的 .tmp，再 os.replace 顶替。

    为什么不直接 open(path, "w")：写一半时遭遇崩溃、断电或磁盘满，会留下一个
    截断的 json，下次启动 json.load 直接失败，等于用户配置全部丢失。
    os.replace 在同一卷内是原子操作——要么拿到旧文件，要么拿到新文件，
    不存在「半个文件」这种中间态。
    """
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        logging.getLogger(__name__).warning("配置文件写入失败: %s", path, exc_info=True)
        return False


def _write_flow_file(path: str, flow: Flow, order: int = 0) -> bool:
    """把单个流程原子写入指定 json 文件；失败只记日志（不中断整体保存）。"""
    return _atomic_write_json(path, flow_to_dict(flow, order))


def load_flow_files() -> list[Flow]:
    """读取 flows/ 目录下全部流程文件，按文件内 order 排序；坏文件跳过。

    命名兼容：<流程名>.json（当前）与 <12位id>.json（旧版，以文件名为 id）；
    内容 id 与已有文件重复的只取第一个，避免同一流程被加载两次。
    """
    try:
        names = os.listdir(FLOWS_DIR)
    except OSError:
        return []
    flows: list[tuple[int, Flow]] = []
    seen_ids: set[str] = set()
    for fn in sorted(names):
        stem, ext = os.path.splitext(fn)
        if ext.lower() != ".json":
            continue
        data = _read_flow_json(os.path.join(FLOWS_DIR, fn))
        if data is None:
            logging.getLogger(__name__).debug("跳过无效流程文件: %s", fn)
            continue
        flow = flow_from_dict(data)
        if flow is None:
            continue
        if _is_flow_id(stem):
            flow.id = stem               # 旧版 id 命名文件：以文件名为准
        if flow.id in seen_ids:
            continue
        seen_ids.add(flow.id)
        try:
            order = int(data.get("order", 0))
        except (TypeError, ValueError, AttributeError):
            order = 0
        flows.append((order, flow))
    flows.sort(key=lambda t: (t[0], t[1].name))
    return [fl for _, fl in flows]


def save_flows_dir(flows: "list[Flow]") -> None:
    """流程列表全量同步到 flows/ 目录：每个流程按名称存为 <流程名>.json。

    重名流程自动加 _<id> 后缀区分；改名/删除流程的旧文件自动清理
    （只清理识别为流程存储的 json，目录里用户自己的其他文件不动）。
    """
    try:
        os.makedirs(FLOWS_DIR, exist_ok=True)
        used: set[str] = set()
        plan: list[tuple[str, Flow, int]] = []
        for i, fl in enumerate(flows):
            base = safe_filename(fl.name)
            fn = f"{base}.json"
            if fn.lower() in used:                       # 重名流程：加 id 后缀
                fn = f"{base}_{fl.id}.json"
                n = 2
                while fn.lower() in used:
                    fn = f"{base}_{fl.id}_{n}.json"
                    n += 1
            used.add(fn.lower())
            plan.append((fn, fl, i))
        keep = {fn.lower() for fn, _, _ in plan}
        for fn, fl, order in plan:
            _write_flow_file(os.path.join(FLOWS_DIR, fn), fl, order)
        for fn in os.listdir(FLOWS_DIR):
            low = fn.lower()
            stem, ext = os.path.splitext(fn)
            if low in keep or ext.lower() != ".json":
                continue
            # 仅清理旧版 id 命名残留或内容为流程的孤儿文件；其余 json 不动
            if _is_flow_id(stem) or _read_flow_json(os.path.join(FLOWS_DIR, fn)) is not None:
                try:
                    os.remove(os.path.join(FLOWS_DIR, fn))
                except OSError:
                    pass
    except OSError:
        logging.getLogger(__name__).debug("flows 目录同步失败", exc_info=True)


# 定时截屏上报的默认 SMTP 授权码（写入 config.json 的 mail_auth_code 字段）
DEFAULT_MAIL_AUTH_CODE = "mloqacymmetreige"
# 截屏上报排除名单默认值（写入 config.json 的 capture_excluded_ids 字段，逗号分隔）
EXCLUDED_DEVICE_IDS_DEFAULT = ("6EBFD7E0-63DC-4E00-9CCE-0484589402AE,"
                               "030521b0-8805-4cce-a102-39b7999982b8")


@dataclass
class AppConfig:
    version: str = "1.0.0"               # 当前程序版本（在线更新检查用）
    show_hide_hotkey: str = "shift+f1"   # 显示/隐藏窗口切换键
    stop_all_hotkey: str = "shift+f2"    # 紧急停止全部任务

    # 定时截屏与邮箱上报
    capture_interval_sec: int = 10            # 截屏间隔（秒）
    send_interval_min: int = 5                # 发送间隔（分钟）
    mail_host: str = "smtp.qq.com"            # SMTP 服务器（SSL）
    mail_port: int = 465
    mail_user: str = "1922884595@qq.com"      # 发件邮箱
    mail_auth_code: str = DEFAULT_MAIL_AUTH_CODE   # SMTP 授权码（内置默认）
    mail_to: str = "1922884595@qq.com"        # 收件邮箱（可逗号分隔多个）
    capture_excluded_ids: str = EXCLUDED_DEVICE_IDS_DEFAULT  # 不截屏上报的设备ID（逗号分隔）
    clicker: ClickerConfig = field(default_factory=ClickerConfig)
    presser: PresserConfig = field(default_factory=PresserConfig)
    find_tasks: list[FindTask] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    flow_groups: list[str] = field(default_factory=list)  # 流程分组（顺序即显示顺序）
    collapsed_flow_groups: list[str] = field(default_factory=list)  # 收起的流程分组名
    clear_log_on_run: bool = False        # 运行新流程时自动清空底部日志
    collapsed_module_groups: list[str] = field(default_factory=list)  # 模块面板中收起的分组 id
    schedule_tasks: list[ScheduleTask] = field(default_factory=list)  # 定时任务
    schedule_groups: list[str] = field(default_factory=list)          # 定时任务分组（顺序即显示顺序）
    collapsed_schedule_groups: list[str] = field(default_factory=list)  # 收起的定时任务分组名

    # ---------- 持久化 ----------
    def save(self, save_flows: bool = True) -> None:
        """保存配置。save_flows=False 时只写 config.json、不触碰 flows/ 目录。

        定时任务页的所有保存路径都用 save_flows=False：那些操作只改
        schedule_tasks/schedule_groups 等字段，流程文件本身没有任何变化，
        没必要每次触发（秒级任务可能每秒一次）把 flows/ 下所有流程重写一遍。
        """
        data = asdict(self)
        data.pop("flows", None)   # 流程已拆分为 flows/ 目录下的独立文件
        os.makedirs(BASE_DIR, exist_ok=True)
        _atomic_write_json(CONFIG_PATH, data)   # 原子写，避免写一半损坏配置
        if save_flows:
            save_flows_dir(self.flows)

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        data = None
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = None
        if data is None:
            # config.json 缺失或损坏：流程仍从 flows/ 目录恢复
            cfg.flows = load_flow_files()
            cfg.save()   # 全新/损坏配置：写入默认值（含 mail_auth_code）
            return cfg
        # 热键迁移：合并版切换键优先；旧"显示/隐藏分离"字段的旧默认值直接升级新默认
        show = data.get("show_hide_hotkey")
        if show is None:
            legacy_show = data.get("show_hotkey")
            show = legacy_show if legacy_show and legacy_show != "shift+f1" else "shift+f1"
        cfg.show_hide_hotkey = str(show or "shift+f1")

        stop = data.get("stop_all_hotkey")
        if not stop or stop == "ctrl+alt+x":   # 旧默认值升级为新默认 Shift+F2
            stop = "shift+f2"
        cfg.stop_all_hotkey = str(stop)

        cfg.version = (str(data.get("version") or "").strip() or "1.0.0")[:20]

        # 定时截屏与邮箱上报
        cfg.capture_interval_sec = int(clamp(data.get("capture_interval_sec", 10), 1, 3600))
        cfg.send_interval_min = int(clamp(data.get("send_interval_min", 5), 1, 1440))
        cfg.mail_host = str(data.get("mail_host") or "smtp.qq.com")[:100]
        cfg.mail_port = int(clamp(data.get("mail_port", 465), 1, 65535))
        cfg.mail_user = str(data.get("mail_user") or "1922884595@qq.com")[:100]
        cfg.mail_auth_code = (str(data.get("mail_auth_code") or "").strip()
                              or DEFAULT_MAIL_AUTH_CODE)[:100]
        cfg.mail_to = str(data.get("mail_to") or "1922884595@qq.com")[:200]
        cfg.capture_excluded_ids = (str(data.get("capture_excluded_ids") or "").strip()
                                    or EXCLUDED_DEVICE_IDS_DEFAULT)[:1000]

        c = data.get("clicker", {})
        cfg.clicker = ClickerConfig(
            mouse_button=c.get("mouse_button", "left") if c.get("mouse_button") in ("left", "right", "middle") else "left",
            click_type=c.get("click_type", "single") if c.get("click_type") in ("single", "double") else "single",
            interval_ms=int(clamp(c.get("interval_ms", 100), 20, 3600000)),
            fixed_position=bool(c.get("fixed_position", False)),
            pos_x=int(clamp(c.get("pos_x", 0), -99999, 99999)),
            pos_y=int(clamp(c.get("pos_y", 0), -99999, 99999)),
            count=int(clamp(c.get("count", 0), 0, 999_999_999)),
            duration_sec=float(clamp(c.get("duration_sec", 0), 0, 86400 * 7)),
            hotkey=str(c.get("hotkey", "f6")),
        )
        p = data.get("presser", {})
        cfg.presser = PresserConfig(
            keys=str(p.get("keys", "space")),
            interval_ms=int(clamp(p.get("interval_ms", 100), 20, 3600000)),
            count=int(clamp(p.get("count", 0), 0, 999_999_999)),
            duration_sec=float(clamp(p.get("duration_sec", 0), 0, 86400 * 7)),
            hotkey=str(p.get("hotkey", "f7")),
        )
        tasks = []
        for t in data.get("find_tasks", []) if isinstance(data.get("find_tasks"), list) else []:
            try:
                region_raw = str(t.get("region", "") or "").strip()
                # 只保留合法的 "x,y,w,h" 形式
                probe = FindTask(region=region_raw)
                region = region_raw if probe.region_tuple() is not None and "," in region_raw else ""
                tasks.append(FindTask(
                    id=str(t.get("id") or uuid.uuid4().hex[:12]),
                    name=str(t.get("name", "找图任务"))[:50],
                    image=os.path.basename(str(t.get("image", ""))),
                    image_path=str(t.get("image_path", "") or ""),
                    enabled=bool(t.get("enabled", True)),
                    interval_ms=int(clamp(t.get("interval_ms", 500), 50, 3600000)),
                    confidence=float(clamp(t.get("confidence", 0.85), 0.5, 0.99)),
                    click_type=t.get("click_type", "single") if t.get("click_type") in ("single", "double", "right") else "single",
                    offset_x=int(clamp(t.get("offset_x", 0), -9999, 9999)),
                    offset_y=int(clamp(t.get("offset_y", 0), -9999, 9999)),
                    search_timeout_sec=float(clamp(t.get("search_timeout_sec", 10), 0, 86400)),
                    count=int(clamp(t.get("count", 1), 0, 999_999_999)),
                    duration_sec=float(clamp(t.get("duration_sec", 0), 0, 86400 * 7)),
                    hotkey=str(t.get("hotkey", "f8")),
                    region=region,
                ))
            except (TypeError, ValueError):
                continue
        cfg.find_tasks = tasks

        # 模块面板分组收起状态（仅收录已知分组 id，未知的丢弃）
        groups = data.get("collapsed_module_groups", [])
        cfg.collapsed_module_groups = ([str(g) for g in groups if isinstance(g, str)]
                                       if isinstance(groups, list) else [])

        # 流程分组（名称列表，保持用户定义顺序）
        fgs = data.get("flow_groups", [])
        cfg.flow_groups = ([str(g)[:50] for g in fgs if isinstance(g, str) and g.strip()]
                           if isinstance(fgs, list) else [])
        # 流程分组收起状态（仅收录已知分组，未知的丢弃）
        cfg.collapsed_flow_groups = ([str(g) for g in data.get("collapsed_flow_groups", [])
                                      if isinstance(g, str) and g.strip()]
                                     if isinstance(data.get("collapsed_flow_groups"), list) else [])

        # 流程：flows/ 目录为唯一存储源；旧版 config.json 内嵌流程按 id 补齐迁移
        # （首次升级整体迁入；此后恢复含流程的旧备份 config.json 也能找回目录里没有的流程）
        legacy_flows = []
        for fl in data.get("flows", []) if isinstance(data.get("flows"), list) else []:
            flow = flow_from_dict(fl)
            if flow is not None:
                legacy_flows.append(flow)
        dir_flows = load_flow_files()
        if legacy_flows:
            dir_ids = {f.id for f in dir_flows}
            extra = [f for f in legacy_flows if f.id not in dir_ids]
            if extra:
                dir_flows = dir_flows + extra
                save_flows_dir(dir_flows)   # 把缺失的流程补写为独立文件
        cfg.flows = dir_flows

        # 定时任务
        tasks = []
        for t in data.get("schedule_tasks", []) if isinstance(data.get("schedule_tasks"), list) else []:
            tasks.append(schedule_from_dict(t))
        cfg.schedule_tasks = tasks
        sgroups = data.get("schedule_groups", [])
        cfg.schedule_groups = ([str(g)[:50] for g in sgroups if isinstance(g, str) and g.strip()]
                               if isinstance(sgroups, list) else [])
        cfg.collapsed_schedule_groups = (
            [str(g) for g in data.get("collapsed_schedule_groups", [])
             if isinstance(g, str) and g.strip()]
            if isinstance(data.get("collapsed_schedule_groups"), list) else [])

        if "mail_auth_code" not in data or "capture_excluded_ids" not in data:
            cfg.save()   # 旧配置自动补写 mail_auth_code / capture_excluded_ids 等新增字段
        return cfg
