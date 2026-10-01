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


# ---- 左上角「运行中流程」悬浮窗的可配置项（2026-09-27） ----
# 位置九宫格键（设置页下拉的顺序即此元组的顺序）
RUN_OVERLAY_POSITIONS = ("top_left", "top_center", "top_right",
                         "middle_left", "center", "middle_right",
                         "bottom_left", "bottom_center", "bottom_right")
RUN_OVERLAY_POS_LABELS = {
    "top_left": "左上角", "top_center": "顶部居中", "top_right": "右上角",
    "middle_left": "左侧居中", "center": "屏幕正中", "middle_right": "右侧居中",
    "bottom_left": "左下角", "bottom_center": "底部居中", "bottom_right": "右下角",
}
RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX = 8, 72
RUN_OVERLAY_DEFAULT_TEXT_COLOR = "#00e676"   # 默认高亮绿（沿用 2026-09 初版配色）

# 状态日志（「状态日志」步骤输出到浮层的消息流，2026-10-01）：按级别着色。
# ⚠️ 三级**默认颜色不在这里**——它们定义在 app/running_overlay.py，
# 因为浮层是「用户自定义外观」域：默认色要避开所有主题令牌色值，
# 否则切主题时会被主题引擎重映射（契约测试会扫 config.py 的浅色字面量）。
RUN_OVERLAY_LOG_MAX_LINES_MIN, RUN_OVERLAY_LOG_MAX_LINES_MAX = 1, 30
# 状态日志浮层的最大宽/高（px）：宽度决定多长换行，高度是窗口上限
RUN_OVERLAY_LOG_WIDTH_MIN, RUN_OVERLAY_LOG_WIDTH_MAX = 120, 2000
# 状态日志「多久没有新消息就自动收起」的秒数（用户可调）
RUN_OVERLAY_LOG_AUTO_HIDE_MIN, RUN_OVERLAY_LOG_AUTO_HIDE_MAX = 5, 3600
RUN_OVERLAY_LOG_HEIGHT_MIN, RUN_OVERLAY_LOG_HEIGHT_MAX = 60, 2000
STATUS_LOG_LEVELS = ("normal", "warn", "error")  # 级别键（顺序即下拉顺序）
STATUS_LOG_LEVEL_LABELS = {
    "normal": "普通", "warn": "警告", "error": "错误",
}

# ---- 全局字体百分比（2026-10-01，设置页「界面外观 -> 界面文字大小」）----
# 所有页面的文字大小都按这个百分比缩放（100 = 原始大小），页面间距/风格随之保持统一。
# 实现见 app/ui/theme.py：应用字体按比例缩放 + 所有 QSS 里的 font-size 统一换算，
# 所以不用在 140+ 处 UI 代码里各写一遍。
UI_FONT_SCALE_MIN, UI_FONT_SCALE_MAX, UI_FONT_SCALE_STEP = 70, 150, 5
UI_FONT_SCALE_DEFAULT = 100


def normalize_xy_pos(value) -> str:
    """把 "x,y" 形式的手动拖动位置规范化；非法一律返回 ""（回落到九宫格定位）。

    容错手改 config.json：负坐标（多屏在左侧时合法）保留，非数字/格式错当空处理。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    parts = [x.strip() for x in s.replace("，", ",").split(",")]
    if len(parts) != 2:
        return ""
    try:
        x, y = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if abs(x) > 100000 or abs(y) > 100000:
        return ""
    return f"{x},{y}"


def normalize_hex_color(value, fallback: str = "") -> str:
    """把任意输入规范成 #rrggbb / #rrggbbaa 小写形式；非法回退 fallback。

    接受 "#RGB"、"#" 可省略、任意大小写；空串返回 fallback（背景色
    fallback="" 即「透明」）。手改 config.json 填错颜色也不该让加载失败。
    """
    s = str(value or "").strip()
    if not s:
        return fallback
    if not s.startswith("#"):
        s = "#" + s
    body = s[1:]
    if len(body) not in (3, 6, 8) or any(c not in "0123456789abcdefABCDEF" for c in body):
        return fallback
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return "#" + body.lower()


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

FLOW_STEP_TYPES = {"var": "变量", "log": "打印输出", "status_log": "状态日志",
                   "ocr": "文字识别",
                   "shot_translate": "截图谷歌翻译",
                   "text_find": "文字查找", "wait_text": "等待文字出现", "screenshot": "截图",
                   "manual_shot": "手动截图",
                   "find_image": "找图", "wait_image": "等待图片出现", "yolo_detect": "目标检测",
                   "click": "鼠标点击", "press": "键盘连按", "find": "找图点击",
                   "wait": "延时等待", "web": "打开关闭网页或浏览器", "http_request": "网络请求",
                   "deepseek": "DeepSeek 对话", "script": "执行脚本",
                   "notify": "消息通知",
                   "speech": "语音播报",
                   "qq_mail": "邮件发送",
                   "float_image": "图片悬浮",
                   "app": "打开应用",
                   "close_app": "关闭应用", "clip_set": "赋值剪贴板",
                   "clip_get": "获取剪贴板内容",
                   "py_func": "python函数",
                   "color_pick": "屏幕取色",
                   "shutdown": "定时关机",
                   # DrissionPage 可视化网页自动化（dp_actors.py）
                   "dp_browser": "打开浏览器", "dp_element": "元素操作",
                   "dp_tab": "切换标签", "dp_listen": "监听网络数据",
                   "dp_page_shot": "网页截图", "dp_ele_shot": "元素截图",
                   "dp_upload": "上传文件",
                   "dp_close_browser": "关闭浏览器",
                   "if": "条件判断", "elseif": "否则如果",
                   "else": "否则", "endif": "条件结束",
                   "foreach": "Foreach 循环", "endForeach": "Foreach 循环结束",
                   "for": "for 循环", "endFor": "for 循环结束",
                   "while": "while 循环", "endWhile": "while 循环结束",
                   "break": "break 中断循环", "continue": "continue 继续循环",
                   "exit": "退出流程"}

# 步骤类型 -> 该步骤会「产出/写入」的变量参数字段名。供流程各变量下拉收集
# 「流程内部变量」：既含 var 步骤的声明，也含其它步骤把结果写入的变量（OCR/找图/
# 截图/取色/网络请求/DeepSeek/脚本/python函数/DrissionPage 等的输出字段，
# foreach 每轮写入的 item/index）。运行期变量存储是共享 dict——任何步骤执行后，
# 其产出变量即可被后续「打印输出」或表达式引用。无产出字段的步骤类型不在表内。
STEP_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "var": ("name",),
    "foreach": ("item_var", "index_var"),
    "for": ("var",),
    "ocr": ("variable",),
    "shot_translate": ("variable", "source_var"),
    "text_find": ("variable",),
    "wait_text": ("result_var", "pos_var"),
    "find_image": ("variable",),
    "wait_image": ("result_var", "pos_var"),
    "yolo_detect": ("variable",),
    "screenshot": ("variable",),
    "manual_shot": ("variable",),
    "color_pick": ("variable",),
    "clip_get": ("variable",),
    "http_request": ("status_var", "headers_var", "cookie_var", "text_var"),
    "deepseek": ("result_var",),
    "script": ("result_var",),
    "py_func": ("result_var",),
    "dp_browser": ("browser_var",),
    "dp_element": ("result_var",),
    "dp_tab": ("result_var",),
    "dp_listen": ("url_var", "status_var", "body_var"),
    "dp_page_shot": ("result_var",),
    "dp_ele_shot": ("result_var",),
}

# 自动成对生成的步骤类型：不显示在模块面板（endif/endForeach/endWhile/endFor
# 分别随 if/foreach/while/for 拖入时自动创建）。
# 模块面板只展示「可拖拽」的类型，其余类型在编辑器中由程序自动补全。
AUTO_STEP_TYPES = {"endif", "endForeach", "endWhile", "endFor"}

# 结构/控制流标记步骤：name 是派生显示名（用户不可自定义，也没有自定义入口），
# 加载/构造时强制刷新为当前 FLOW_STEP_TYPES 的显示名。这样显示名升级时（例如
# 「循环」→「Foreach 循环」）旧流程文件里残留的旧 name 会被自动纠正，保证
# 列表、删除确认、执行日志等所有用到 name 的位置显示一致。
STRUCTURAL_STEP_TYPES = {
    "if", "elseif", "else", "endif",
    "foreach", "endForeach", "for", "endFor", "while", "endWhile",
    "break", "continue",
}

# 变量类型：值 -> 显示名
VARIABLE_TYPES = {"string": "字符串", "integer": "整数", "float": "浮点数",
                  "bool": "布尔型", "list": "列表", "dict": "字典"}


# web 步骤（打开关闭网页或浏览器）的子动作：值 -> 显示名
WEB_ACTIONS = {"open": "打开网址", "close_tab": "关闭标签页", "close_browser": "关闭浏览器"}

# 「定时关机」步骤的触发方式：值 -> 显示名（配置项 trigger_mode）
#   at_time   —— 到某个具体时刻（HH:MM）触发
#   countdown —— 从步骤开始执行起，经过指定 时/分/秒 后触发
#   condition —— 屏幕上出现指定图片或文字后触发
SHUTDOWN_TRIGGER_MODES = {
    "at_time": "指定时间点",
    "countdown": "倒计时",
    "condition": "条件触发",
}

# 「定时关机」触发后的电源动作：值 -> 显示名（配置项 power_action）
# 关机/重启走 shutdown.exe（/s、/r），睡眠/锁定走 rundll32；
# 前者支持 /f 强制结束阻塞关机的程序，后者与 /f 无关。
POWER_ACTIONS = {
    "shutdown": "关机",
    "restart": "重启",
    "sleep": "睡眠",
    "lock": "锁定屏幕",
}

# 「截图谷歌翻译」步骤：语言表。代码沿用谷歌的写法（zh-CN / zh-TW），
# 不是 ISO 639-1 的两字母形式——直接作为接口的 sl / tl 参数使用。
# 源语言额外支持 auto（自动检测）；目标语言不能是 auto，故分成两张表。
TRANSLATE_LANGUAGES = {
    "auto": "自动检测",
    "zh-CN": "中文（简体）",
    "zh-TW": "中文（繁体）",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "ru": "俄语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "ar": "阿拉伯语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "ms": "马来语",
    "hi": "印地语",
    "tr": "土耳其语",
    "pl": "波兰语",
    "nl": "荷兰语",
    "uk": "乌克兰语",
}

TRANSLATE_TARGET_LANGUAGES = {k: v for k, v in TRANSLATE_LANGUAGES.items() if k != "auto"}

# 网络请求步骤：默认 User-Agent（Chrome 桌面版）
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/112.0.0.0 Safari/537.36")

# while 循环每轮迭代之间的等待（秒）：默认 0.1 秒，避免条件恒真的循环满速空转
# （刷日志、吃 CPU、把界面/变量状态刷得看不清）。步骤参数 interval_sec 可改，
# 填 0 = 不等待；测试把它置 0 即可跳过真实等待（见 tests/test_while_interval.py）。
WHILE_ITER_INTERVAL_DEFAULT_SEC = 0.1
WHILE_ITER_INTERVAL_MAX_SEC = 3600.0

# 「键盘连按」每次按下的**按住时长**（毫秒）：按下后按住这么久再松开。
# 太短（<20ms）有些程序/游戏一帧都扫不到，太长会被判成长按；50ms 是通用折中。
# 步骤参数 hold_ms 可改，0 = 按下即松开（2026-09-22：这个参数以前叫「持续时长」，
# 语义是"整个连按跑多久"，跟用户的预期（每次按住多久）不符，已改名改语义）。
PRESS_HOLD_DEFAULT_MS = 50


def while_iter_interval(params) -> float:
    """读 while 步骤的「每轮迭代间隔」（秒）。

    缺字段 / 非法值 / NaN → 回退 WHILE_ITER_INTERVAL_DEFAULT_SEC（默认 1 秒）；
    负数按 0 处理；上限夹到 WHILE_ITER_INTERVAL_MAX_SEC。0 表示不等待。
    """
    raw = (params or {}).get("interval_sec", WHILE_ITER_INTERVAL_DEFAULT_SEC)
    try:
        sec = float(raw)
    except (TypeError, ValueError):
        return WHILE_ITER_INTERVAL_DEFAULT_SEC
    if sec != sec:                      # NaN
        return WHILE_ITER_INTERVAL_DEFAULT_SEC
    return min(max(sec, 0.0), WHILE_ITER_INTERVAL_MAX_SEC)


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
            "count": p.count if p.count > 0 else 1,
            # 每次都按住多久再松开（毫秒）；不是"整个连按持续多久"
            "hold_ms": PRESS_HOLD_DEFAULT_MS,
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
            "default_value": "",          # 默认值文本，按 type 解析；支持表达式（$引用/运算/拼接/函数）
        }
    if step_type == "log":
        return {
            "variables": "",              # 要打印的变量名，逗号分隔；空=不打印任何变量
            "text": "",                   # 附加文本，支持 $变量名 占位
            "raw": False,                 # 原始输出：不加时间戳、不自动换行，内容原样显示
            "show_type": False,           # 显示变量的 Python 类型（如 count = 5 (int)）
        }
    if step_type == "status_log":
        # 参考「打印输出」但输出目标是**运行状态浮层**（不是日志面板），
        # 且带级别（普通/警告/错误）——浮层里按级别用不同颜色显示。
        return {
            "text": "",                   # 要输出的内容，支持 $变量名 占位
            "level": "normal",            # 级别：normal 普通 / warn 警告 / error 错误
        }
    if step_type == "ocr":
        return {
            "region": "",                 # 识别区域 "x,y,w,h"，空=全屏
            "variable": "",               # 结果保存到的变量名（列表/字典）
            "lang": "ch",                 # 识别语言（RapidOCR 默认中英混合，兼容占位）
            "multi_ocr": True,            # True=多行文本列表，False=拼接字符串
        }
    if step_type == "shot_translate":
        return {
            # 无 region：截图区域在**运行时**由用户框选（见 tasks.run_shot_translate_step）
            "source_lang": "auto",        # 源语言（auto=自动检测）
            "target_lang": "zh-CN",       # 目标语言
            "variable": "",               # 译文保存到的变量名
            "source_var": "",             # 识别出的原文保存到的变量名（留空不写）
            "merge_lines": False,         # True=多行合并为一段再翻译（更通顺）
            "show_notify": False,         # 翻译后弹通知浮窗显示结果
            "notify_seconds": 0.0,        # 浮窗停留秒数，<=0 表示只能手动关闭
            "copy_clipboard": False,      # 译文复制到剪贴板
            "timeout_sec": 10.0,          # 翻译请求超时（秒）
            "proxy": "",                  # 代理，如 http://127.0.0.1:7890
        }
    if step_type == "text_find":
        return {
            "text": "",                   # 要查找的文字，支持 $变量名 引用
            "region": "",                 # 查找区域 "x,y,w,h"，空=全屏
            "click": False,               # True=找到后点击该文字
            "click_button": "left",       # left / right
            "variable": "",               # 结果变量：未勾选点击时写坐标 "x,y"，未找到写 false
        }
    if step_type == "wait_text":
        return {
            "text": "",                   # 目标文字（必填，支持 $变量名 引用）
            "region": "",                 # 识别区域 "x,y,w,h"（物理像素），空=全屏
            "interval_sec": 1.0,          # 每次识别的时间间隔（秒）
            "tolerance": 0.8,             # 近似匹配容错度 0~1（1=完全匹配，越小越宽松）
            "timeout_sec": 0.0,           # 最长等待（秒），0=一直等到出现为止
            "result_var": "",             # 可选：命中后把识别到的整行文字写入该变量
            "pos_var": "",                # 可选：命中后把文字中心坐标 "x,y" 写入该变量
        }
    if step_type == "wait":
        return {"seconds": 1.0}
    if step_type == "app":
        return {
            "path": "",                   # 应用路径（可浏览）；进程未运行时启动用，支持文件/快捷方式/文件夹
            "process": "",                # 目标进程名（如 chrome.exe）：运行时已在运行则带出窗口
            "target": "",                 # 显示用：进程列表选择的完整描述（如 Google Chrome — chrome.exe）
            "use_process": True,          # 进程打开：勾选（默认）时先匹配目标进程置前，未运行再用路径启动；
                                          # 取消勾选则忽略目标进程，直接用路径打开程序/文档/文件夹
            "wait_sec": 2.0,              # 启动/带出后等待的秒数（给应用留出加载时间）
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
            "launch_mode": "front",        # front / headless / background / attach
            "attach_port": "",             # attach（接管手动打开的浏览器）时的调试端口，如 9333
            "tab_target": "reuse",         # reuse=当前标签 / new=新标签
            "load_timeout_sec": 20.0,
            "wait_after_sec": 0.0,
            "tab_scope": "current",        # current / others / match
            "match_text": "",
        }
    if step_type == "http_request":
        return {
            "url": "",                    # 请求网址（必填，不带协议会自动补 https://）
            "method": "get",              # get / post
            "headers": "",                # 请求头：每行一条「Name: Value」，支持 $变量名
            "body": "",                   # 请求体（POST 时发送，支持 $变量名；GET 忽略）
            "cookie": "",                 # Cookie 字符串（可选，支持 $变量名）
            "result_type": "text",        # text=文本 / image=图片（保存到文件）
            "user_agent": DEFAULT_USER_AGENT,  # User-Agent，支持 $变量名
            "timeout": 5.0,               # 超时时间（秒）
            "use_proxy": True,            # 是否使用系统代理
            "proxy": "127.0.0.1:7897",    # 代理地址 host:port
            # 结果变量（都可选，按需勾选）：
            "status_var": "",             # HTTP 状态码（整数）
            "headers_var": "",            # 响应头（dict）
            "cookie_var": "",             # 响应 Cookie（dict）
            "text_var": "",               # 文本内容（text）/ 图片保存路径（image）
        }
    if step_type == "deepseek":
        return {
            "model": "deepseek-v4-flash",      # 默认 flash；可下拉选 pro 或手动输入
            "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),  # 留空运行时也读环境变量
            "system": "You are a helpful assistant",  # 角色设定（system 消息）
            "thinking": False,                # 思考模式：thinking.enabled + reasoning_effort=high
            "stream": False,                  # 流式输出（默认关闭）
            "question": "",                   # 提问内容（必填，支持 $变量名）
            "result_var": "",                 # 结果变量：最终回答
            "timeout": 60.0,                  # 超时（秒），推理模型较慢
            "base_url": "https://api.deepseek.com",
            "use_proxy": True,                # 系统代理
            "proxy": "127.0.0.1:7897",
        }
    if step_type == "script":
        return {
            "script_type": "cmd",         # cmd / bat / powershell / python（cmd 与 bat 都走 cmd.exe）
            "source": "text",             # text=文本内容 / file=脚本文件
            "content": "",                # 脚本内容（source=text 时，支持 $变量名 引用）
            "path": "",                   # 脚本文件完整路径（source=file 时，支持 $变量名 引用）
            "encoding": "utf-8",          # gb2312 / utf-8 / utf-8-sig / ascii（默认 utf-8 无 BOM）
            "window_mode": "hidden",      # hidden=隐藏窗口 / keep=完成后保留命令窗口
            "admin": False,               # 以管理员权限运行（UAC 提权）
            "timeout": 120.0,             # 超时（秒，仅隐藏窗口模式生效）
            "result_var": "",             # 输出结果（stdout+stderr）写入的变量
        }
    if step_type == "notify":
        return {
            "msg_type": "info",           # info=信息 / success=成功 / warning=警告 / error=错误
            "position": "bottom",         # 显示位置（默认屏幕中间底部），见 POSITIONS 注释
            "content": "",                # 消息内容，支持 $变量名 引用
            "duration": 2.0,              # 自动消失延迟（秒），0=不自动消失（仅手动关闭）
            "width": 320,                 # 通知宽度（像素），高度随内容自适应
        }
    if step_type == "speech":
        return {
            "content": "",                # 播报内容，支持 $变量名 引用（手动输入或选变量）
            "wait": True,                 # 是否等待播报完成：勾选=播完再继续；不勾=后台播放不阻塞
        }
    if step_type == "qq_mail":
        return {
            "mail_host": "smtp.qq.com",   # SMTP 服务器（默认 QQ 邮箱）
            "mail_port": 465,             # SMTP 端口（QQ 邮箱 SSL 端口，默认 465）
            "mail_user": "",              # 发送人邮箱（用户自行填写）
            "mail_auth_code": "",         # 发送人邮箱授权码（QQ 邮箱「设置-账户-开启SMTP服务」生成）
            "mail_to": "",                # 收件人邮箱（用户自行填写，多个用逗号/分号分隔，支持 $变量名）
            "subject": "",                # 邮件主题，支持 $变量名 引用
            "content": "",                # 邮件正文，支持 $变量名 引用
            "attachments": [],            # 附件绝对路径列表（可多个，浏览选择）
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
    if step_type == "wait_image":
        return {
            "image": "",                 # 模板图文件名（templates/ 下，截屏/上传生成）
            "image_path": "",            # 模板图绝对路径（跨目录运行时兜底）
            "confidence": 0.85,          # 匹配置信度阈值 0.5~0.99
            "region": "",                # 查找区域 "x,y,w,h"（物理像素），空=全屏
            "interval_sec": 1.0,         # 每次找图的时间间隔（秒）
            "timeout_sec": 0.0,          # 最长等待（秒），0=一直找直到出现为止
            "result_var": "",            # 可选：命中后写矩形区域 "左上x,左上y,右下x,右下y"
            "pos_var": "",               # 可选：命中后写中心坐标 "x,y"
        }
    if step_type == "screenshot":
        return {
            "region": "",                 # 指定区域 "x,y,w,h"（物理像素，必填）
            "save_mode": "variable",      # variable=默认保存 / choose=自选保存（弹窗）
            "variable": "",               # 截图绝对路径写入的结果变量（默认保存必填，自选保存可选）
        }
    if step_type == "manual_shot":
        # 与「截图」的区别：区域和保存位置都不在编辑期预设，全部由用户运行时决定。
        return {
            "default_name": "",           # 保存对话框的默认文件名前缀（空=用「手动截图」）
            "variable": "",               # 可选：截图保存的绝对路径写入该变量
        }
    if step_type == "float_image":
        # 异步步骤：把图片贴在桌面最前端，随即返回、不阻塞后续流程。
        # 图片来源二选一（source_mode）：
        #   template —— 模板图（沿用「找图」那套参数：image=模板目录文件名 / image_path=绝对路径兜底），
        #               编辑对话框直接复用现成的「屏幕截图选区 / 上传图片」控件；
        #   address  —— 图片地址：本地路径或 http(s) 网址，支持 $变量名 引用
        #               （即「用变量传入图片地址」，本地图片与网络图片都走这条）。
        return {
            "source_mode": "template",   # template=模板图 / address=图片地址（本地或网络）
            "image": "",                 # 模板图文件名（templates/ 下，截屏/上传生成）
            "image_path": "",            # 模板图绝对路径（跨目录运行时兜底）
            "address": "",               # 图片地址：本地路径 / file:// / http(s) 网址，支持 $变量名
            "timeout": 10.0,             # 网络图片下载超时（秒）
            "use_proxy": True,           # 网络图片是否走代理（与「网络请求」一致，默认走本机 7897）
            "proxy": "127.0.0.1:7897",   # 代理地址 host:port
            "position": "right_bottom",  # 悬浮位置：right_bottom/right_top/left_top/left_bottom/center/custom
            "x": "",                     # position=custom 时的左上角 x（物理像素）
            "y": "",                     # position=custom 时的左上角 y（物理像素）
            "scale": 100,                # 显示缩放百分比（10~400）
            "click_to_close": False,     # 勾选=单击图片即关闭（默认关，靠 ✕ / Esc 关闭）
        }
    if step_type == "color_pick":
        return {
            "color": "",                  # 取到的颜色（运行时原样写入结果变量），如 #FF0000 或 255,0,0
            "format": "hex",              # 取色保存格式：hex=#RRGGBB / rgb=255,0,0
            "variable": "",               # 颜色字符串写入的结果变量（必填）
        }
    if step_type == "yolo_detect":
        return {
            "model_path": "",             # YOLOv5 模型文件路径（.pt），可浏览选取或手动输入
            "region": "",                 # 检测范围 "x,y,w,h"（物理像素），空=全屏
            "classes": "",                # 检测类别过滤：逗号分隔类别名，支持 $变量名 引用；空=全部类别
            "confidence": 0.5,            # 置信度阈值（自训练模型建议 0.2~0.5，设太高检不出）
            "device": "cuda",             # 推理设备：cuda / cpu
            "action": "none",             # 附加动作：none / left / right / double（对最高置信度目标中心）
            "preview": False,             # 效果预览：红框标注检测目标（左上类别、右上置信度）
            "preview_duration": 1.0,      # 红框持续时间（秒），默认 1 秒
            "variable": "",               # 结果变量：list[dict{class, confidence, region}]，未检测到写空列表
        }
    if step_type == "py_func":
        return {
            "code": "",                   # 用户 Python 代码（def 函数定义）
            "func_name": "",              # 必填：要调用的函数名（固定调用该函数并取返回值）
            "variables": [],              # 勾选的流程变量：与函数形参同名者自动作为关键字实参传入，其余注入环境
            "result_var": "",             # 函数返回值保存到的流程变量名
        }
    # ---- DrissionPage 可视化网页自动化（dp_actors.py）----
    if step_type == "dp_browser":
        return {
            "browser_var": "",            # 浏览器对象保存到的变量名（必填）
            "launch_mode": "front",       # front / headless / background / attach
            # 浏览器调试端口（等价 --remote-debugging-port，默认 9333）：
            # attach 用它定位要接管的浏览器；自启模式则给新浏览器设上这个端口，
            # 之后能被别的步骤/流程接管。留空=不带端口（兼容旧流程）。
            "attach_port": "9333",
            "url": "",                    # 打开后访问的网址（可选，支持 $变量名）
            "new_tab": False,             # True=在新标签访问网址
            "load_timeout_sec": 20.0,     # 网址加载超时
        }
    if step_type == "dp_element":
        return {
            "browser_var": "",            # 浏览器变量（「打开浏览器」步骤产生）
            "locator_type": "id",         # id/class/attr/text/tag/css/xpath
            "attr_name": "",              # locator_type=attr 时的属性名
            "match": "=",                 # = 精确 / : 模糊 / ^ 开头 / $ 结尾
            "locator_value": "",          # 定位值（支持 $变量名）
            "index": 1,                   # 多元素时的位置（1 起，负数从末尾数）
            "action": "click",            # 找到元素后执行的操作（DP_ELE_ACTIONS）
            "input_value": "",            # 输入内容/属性名/拖动偏移等（支持 $变量名）
            "file_paths": "",             # to_upload/to_download 用（支持 $变量名，多个换行或 | 分隔）
            "timeout": 10.0,              # 查找元素超时（秒）
            "result_var": "",             # get_text/get_attr/for_new_tab 的结果变量
        }
    if step_type == "dp_tab":
        return {
            "browser_var": "",
            "switch_mode": "index",       # index/title/url/new
            "value": "",                  # 序号/标题/网址（支持 $变量名）
            "url": "",                    # 新建标签时访问的网址（可选）
            "result_var": "",             # 切换后标签信息 {tab_id,title,url}
        }
    if step_type == "dp_listen":
        return {
            "browser_var": "",
            "action": "start",            # start/wait/stop
            "targets": "",                # 监听目标：URL 包含的文字，多个换行分隔；空=全部
            "timeout": 10.0,              # wait 等待超时（秒）
            "url_var": "",                # wait：数据包网址写入的变量
            "status_var": "",             # wait：响应状态码写入的变量
            "body_var": "",               # wait：响应体（json 自动解析）写入的变量
        }
    if step_type == "dp_page_shot":
        return {
            "browser_var": "",
            "path": "",                   # 保存目录（空=程序模板目录 jietu/，支持 $变量名）
            "name": "",                   # 文件名（空=自动时间戳，支持 $变量名）
            "full_page": False,           # True=整页截图
            "result_var": "",             # 截图保存路径写入的变量（必填）
        }
    if step_type == "dp_ele_shot":
        return {
            "browser_var": "",
            "locator_type": "id",
            "attr_name": "",
            "match": "=",
            "locator_value": "",
            "index": 1,
            "timeout": 10.0,
            "path": "",                   # 保存目录（空=程序模板目录 jietu/）
            "name": "",                   # 文件名（空=自动时间戳）
            "result_var": "",             # 截图保存路径写入的变量（必填）
        }
    if step_type == "dp_upload":
        return {
            "browser_var": "",
            "locator_type": "id",
            "attr_name": "",
            "match": "=",
            "locator_value": "",
            "index": 1,
            "timeout": 10.0,
            "file_paths": "",             # 要上传的文件，多个换行或 | 分隔（支持 $变量名）
        }
    if step_type == "dp_close_browser":
        return {
            "browser_var": "",            # 浏览器变量（「打开浏览器」步骤产生）
        }
    if step_type in ("if", "elseif"):
        return {
            "condition": "",              # 条件表达式，如 x>=1 && y<=10（支持 &&/||/! 与比较运算）
        }
    if step_type == "while":
        return {
            "condition": "",              # 同上；直接填 true 表示恒真（需靠 break 退出）
            # 每轮迭代之间的等待（秒）：默认 1 秒，0=不等待（见 while_iter_interval）
            "interval_sec": WHILE_ITER_INTERVAL_DEFAULT_SEC,
        }
    if step_type == "foreach":
        return {
            "items": "",                  # 数据源：变量/下标/$引用/函数表达式，结果须可遍历
            "item_var": "item",           # 每轮当前元素写入的变量名
            "index_var": "index",         # 每轮下标写入的变量名（空=不写）
        }
    if step_type == "for":
        # 「for 循环」= 计数循环（**含结束值**，2026-09-27 用户定的语义）：从 start
        # 数到 stop、两头都算——默认 1..10 正好 10 个数。三个数值都支持表达式
        # （$变量、len($arr) 等），与 foreach 的 items 一样在运行时求值。
        return {
            "var": "i",                    # 循环变量名：每轮把当前数值写入该变量
            "start": "1",                  # 起始值（含）
            "stop": "10",                  # 结束值（含）；默认 1..10 = 10 轮
            "step": "1",                   # 步长（可为负表示倒数；0 非法）
        }
    if step_type == "exit":
        return {
            "variable": "",               # 可选：退出流程前打印该变量的值（空=不打印）
        }
    if step_type == "shutdown":
        # 「定时关机」：先等触发条件成立，再执行电源动作。三种触发方式共用
        # 同一套「触发后执行」参数（见 run_shutdown_step）。
        return {
            # ---- 触发方式 ----
            "trigger_mode": "countdown",  # at_time / countdown / condition
            # ---- ① 指定时间点（trigger_mode=at_time）----
            "at_time": "",                # "HH:MM" 24 小时制，必填
            "at_time_if_passed": "next_day",  # 今天该时刻已过：next_day=顺延到明天 / fail=判失败
            # ---- ② 倒计时（trigger_mode=countdown）----
            "count_hours": 0,             # 时（0~999）
            "count_minutes": 30,          # 分（0~59）
            "count_seconds": 0,           # 秒（0~59），三者之和须 > 0
            # ---- ③ 条件触发（trigger_mode=condition）----
            "cond_mode": "image",         # image=出现指定图片 / text=出现指定文字
            "image": "",                  # 模板图文件名（templates/ 下），cond_mode=image 必填
            "image_path": "",             # 模板图绝对路径（跨目录运行时兜底）
            "confidence": 0.85,           # 找图匹配置信度 0.5~0.99
            "text": "",                   # 目标文字，cond_mode=text 必填，支持 $变量名
            "tolerance": 0.8,             # 文字近似匹配容错度 0~1（1=完全匹配）
            "region": "",                 # 检测区域 "x,y,w,h"，空=全屏（找图与找字共用）
            "interval_sec": 1.0,          # 检测间隔（秒）
            "cond_timeout_sec": 0.0,      # 最长等待（秒），0=一直检测到出现为止
            "hold_sec": 0.0,              # 条件须连续成立的秒数（0=一出现就触发，防瞬时误判）
            # ---- 触发后执行 ----
            "power_action": "shutdown",   # shutdown=关机 / restart=重启 / sleep=睡眠 / lock=锁定
            "warn_sec": 30,               # 触发后关机前的提醒倒计时（秒），0=立即执行；期间停止流程可取消
            "warn_overlay": True,         # 提醒倒计时期间在屏幕下方显示大号红色倒计时浮层
            "warn_overlay_font_size": 48,  # 浮层主文字字号（pt，12~200）
            "force_close_apps": True,     # 强制关闭未保存的程序（/f），否则有未保存窗口时关不掉
            "dry_run": False,             # 演练模式：只写日志不真正执行，用于验证配置
        }
    if step_type in ("else", "endif", "endForeach", "endFor", "endWhile",
                     "break", "continue"):
        return {}                         # 结构/控制流标记步骤，无参数
    raise ValueError(f"未知步骤类型: {step_type}")


# ---- 步骤「必填参数」规则（供流程列表标红未配置的模块，2026-09-26） ----
# 类型 -> ((字段名, 缺失时的提示), ...)
# ⚠️ 唯一依据是各 run_xxx_step / dp_actors 里的实际判定（"未填写…就返回失败"）——
# **不要凭感觉加字段**。典型的坑：result_var / pos_var / variable 这类"结果变量"
# 大多是**可选输出**（不填只是不写变量，步骤照样成功），早期凭猜测当必填导致误报红框。
# 表里只放"无条件必填"；按取值判断的（模式/开关相关）放 _conditional_missing。
STEP_REQUIRED_PARAMS: dict[str, tuple[tuple[str, str], ...]] = {
    "var": (("name", "变量名"),),
    "ocr": (("variable", "结果变量"),),
    "shot_translate": (("variable", "译文变量"),),
    "text_find": (("text", "目标文字"),),
    "wait_text": (("text", "目标文字"),),
    "screenshot": (("variable", "结果变量"),),
    "find_image": (("variable", "结果变量"),),      # 模板图在 _conditional_missing 里判
    "yolo_detect": (("model_path", "模型文件"), ("variable", "结果变量")),
    "press": (("keys", "按键"),),
    "http_request": (("url", "请求地址"),),
    "deepseek": (("question", "提问内容"),),
    "notify": (("content", "通知内容"),),
    "speech": (("content", "播报内容"),),
    "qq_mail": (("mail_user", "发送人邮箱"), ("mail_auth_code", "邮箱授权码"),
                ("mail_to", "收件人邮箱")),
    "py_func": (("code", "python 代码"), ("func_name", "函数名"),
                ("result_var", "结果变量")),        # 这三项 run_py_func_step 里必查
    "clip_get": (("variable", "结果变量"),),
    "color_pick": (("variable", "结果变量"),),
    "dp_browser": (("browser_var", "浏览器变量名"),),
    "dp_element": (("browser_var", "浏览器变量名"), ("locator_value", "定位值")),
    "dp_tab": (("browser_var", "浏览器变量名"),),
    "dp_listen": (("browser_var", "浏览器变量名"),),
    "dp_page_shot": (("browser_var", "浏览器变量名"), ("result_var", "结果变量")),
    "dp_ele_shot": (("browser_var", "浏览器变量名"), ("locator_value", "定位值"),
                    ("result_var", "结果变量")),
    "dp_upload": (("browser_var", "浏览器变量名"), ("locator_value", "定位值"),
                  ("file_paths", "上传文件")),
    "dp_close_browser": (("browser_var", "浏览器变量名"),),
    "if": (("condition", "条件表达式"),),
    "elseif": (("condition", "条件表达式"),),
    "foreach": (("items", "数据源"),),
    "for": (("stop", "结束值"),),
    "while": (("condition", "条件表达式"),),
}


def _blank(p: dict, key: str) -> bool:
    """参数是否为空（None / 空串 / 全空白 都算空）。"""
    return not str(p.get(key) or "").strip()


def _as_number(value) -> float:
    """把参数转成数字，转不了按 0 处理（用于倒计时时长这类求和判断）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _conditional_missing(t: str, p: dict) -> list[str]:
    """按参数取值判断的必填项（同一类型在不同模式下必填的东西不一样）。

    依据同样是各 run_xxx_step 里的实际判定，例如 find_image 的模板图会是
    "模板图加载失败"、web 的网址会是 "未填写网址"、press/click 的后台模式会是
    "未绑定目标窗口"。**不要凭感觉加**（result_var / pos_var 这类结果变量都是可选的）。
    """
    out: list[str] = []
    if t in ("find_image", "wait_image", "find"):
        # 三个找图类步骤：模板图文件名与绝对路径二选一（load_template 两者都看）
        if _blank(p, "image") and _blank(p, "image_path"):
            out.append("模板图")
    elif t in ("click", "press"):
        # 后台模式必须绑定目标窗口（run_click_step / run_press_step 会因缺标题失败）
        if bool(p.get("background")) and _blank(p, "window_title"):
            out.append("目标窗口标题")
        if t == "click" and bool(p.get("fixed_position")):
            # 固定坐标模式：可以用坐标变量，也可以直接填 x/y
            if _blank(p, "pos_var") and (_blank(p, "pos_x") or _blank(p, "pos_y")):
                out.append("固定坐标 x/y（或坐标变量）")
    elif t == "web":
        if (p.get("action") or "open") == "open" and _blank(p, "url"):
            out.append("网址")
    elif t == "script":
        if (p.get("source") or "text") == "file":
            if _blank(p, "path"):
                out.append("脚本文件路径")
        elif _blank(p, "content"):
            out.append("脚本内容")
    elif t == "float_image":
        if (p.get("source_mode") or "image") == "web":
            if _blank(p, "address"):
                out.append("图片地址")
        elif _blank(p, "image") and _blank(p, "image_path"):
            out.append("图片文件")
    elif t == "app":
        # 「打开应用」：填路径，或从进程列表选一个（都没选会判失败）
        if _blank(p, "path") and _blank(p, "process"):
            out.append("程序路径（或从进程列表选择）")
    elif t == "close_app":
        if _blank(p, "target") and _blank(p, "process"):
            out.append("目标进程")
    elif t == "clip_set":
        # 变量与自定义文本二选一
        if _blank(p, "name") and _blank(p, "text"):
            out.append("变量名或文本")
    elif t == "dp_tab":
        if _blank(p, "value"):
            out.append("切换条件（序号/标题/网址）")
    elif t == "dp_element":
        # 有返回值的动作必须给结果变量（dp_actors: "该操作有返回值，请先设置结果变量"）
        if (p.get("action") or "click") in ("get_text", "get_attr", "get_html",
                                            "get_value", "screenshot"):
            if _blank(p, "result_var"):
                out.append("结果变量")
    elif t == "shutdown":
        mode = (p.get("trigger_mode") or "countdown").strip()
        if mode == "at_time":
            if _blank(p, "at_time"):
                out.append("触发时间")
        elif mode == "condition":
            if (p.get("cond_mode") or "image") == "text":
                if _blank(p, "text"):
                    out.append("触发文字")
            elif _blank(p, "image") and _blank(p, "image_path"):
                out.append("触发图片")
        else:
            total = (_as_number(p.get("count_hours")) * 3600
                     + _as_number(p.get("count_minutes")) * 60
                     + _as_number(p.get("count_seconds")))
            if total <= 0:
                out.append("倒计时时长")
    return out


def step_missing_required(step) -> list[str]:
    """返回该步骤「还没设置的必填参数」提示列表（空列表 = 没发现问题）。

    流程列表据此给未配置好的模块画红框（见 ui/flow_dialog.StepRunDelegate）。
    刻意保守：宁可漏报（保存时对话框还会拦）也不误报，免得正常步骤被刷一堆红框。
    """
    t = getattr(step, "type", "") or ""
    if t not in FLOW_STEP_TYPES:
        return []
    p = getattr(step, "params", None) or {}
    out = [label for key, label in STEP_REQUIRED_PARAMS.get(t, ())
           if _blank(p, key)]
    out.extend(_conditional_missing(t, p))
    return out

def parse_at_time(text: str) -> tuple[int, int] | None:
    """解析「定时关机」的时间点文本，支持 "HH:MM" / "H:MM" / "HHMM"。

    返回 (时, 分)，不合法（空、格式错、越界）返回 None。编辑期与运行期共用同一
    个解析器，避免「对话框放行、运行期判失败」的两套标准。
    """
    raw = str(text or "").strip().replace("：", ":")   # 容错全角冒号
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return None
    elif raw.isdigit() and len(raw) == 4:
        parts = [raw[:2], raw[2:]]
    else:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def shutdown_countdown_seconds(p: dict) -> float:
    """「定时关机」倒计时参数（时/分/秒）换算成总秒数；非法值按 0 计，负数归零。

    返回 float 而不是 int：保留亚秒精度，便于把倒计时配成 0.5 秒来快速验证配置。
    """
    total = 0.0
    for key, factor in (("count_hours", 3600), ("count_minutes", 60), ("count_seconds", 1)):
        try:
            total += max(0.0, float(p.get(key) or 0)) * factor
        except (TypeError, ValueError):
            continue
    return round(total, 3)


def format_duration(seconds: float) -> str:
    """秒数 -> 中文可读时长（如「1 小时 30 分 10 秒」）；不足 1 秒显示「0 秒」。"""
    try:
        secs = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        secs = 0
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} 小时")
    if m:
        parts.append(f"{m} 分")
    if s or not parts:
        parts.append(f"{s} 秒")
    return " ".join(parts)


def _dp_locator_text(p: dict) -> str:
    """DrissionPage 步骤参数里的定位信息 -> 摘要短文本（如「#kw」）。"""
    from .dp_actors import build_locator
    t = (p.get("locator_type") or "id").strip()
    if t in ("css", "xpath", "tag"):
        value = (p.get("locator_value") or "").strip() or "?"
        return f"{t}:{value}"
    loc = build_locator(t, p.get("match"), (p.get("locator_value") or "").strip() or "?",
                        p.get("attr_name") or "")
    return loc if len(loc) <= 30 else loc[:29] + "…"


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
    # 失败策略：True=失败跳过继续、流程不终止（不弹提示）；None=未显式设置，
    # 构造时按类型取默认（close_app 默认 True，其余 False；见 __post_init__）。
    continue_on_fail: bool | None = None
    pair_id: str = ""               # 网页配对（兼容遗留）：早期版本拖「网页操作」自动生成
                                    # 的「打开+关闭」一对共享该 id；2026-09-04 解耦后不再生成，
                                    # 仅作旧数据残留字段由 repair_web_pairs 兜底清理，UI 不再联动
    commented: bool = False         # 注释标记：被注释的步骤运行期跳过不执行，列表灰显

    def __post_init__(self):
        if self.type not in FLOW_STEP_TYPES:
            raise ValueError(f"未知步骤类型: {self.type}")
        if not self.name or self.type in STRUCTURAL_STEP_TYPES:
            # 结构标记步骤的 name 恒等于当前显示名（见 STRUCTURAL_STEP_TYPES 说明）
            self.name = FLOW_STEP_TYPES[self.type]
        # continue_on_fail 类型级默认：close_app 默认「失败后继续」（勾选框默认勾选），
        # 其余步骤默认终止流程；显式传入的 bool 一律保留
        if self.continue_on_fail is None:
            self.continue_on_fail = (self.type == "close_app")
        # 显示名升级迁移：只纠正残留的旧版默认名，用户自定义的名称不受影响。
        # 「日志输出」→「打印输出」；「网页操作」→「打开关闭网页或浏览器」
        # （2026-09-04 网页步骤改名：模块面板展示名与实际承载能力更贴合）。
        # 「QQ邮件发送」→「邮件发送」（2026-09-05）；
        # 「等待图片」→「等待图片出现」（2026-09-05，与「等待文字出现」命名对齐）。
        if self.type == "log" and self.name == "日志输出":
            self.name = "打印输出"
        if self.type == "web" and self.name == "网页操作":
            self.name = "打开关闭网页或浏览器"
        if self.type == "qq_mail" and self.name == "QQ邮件发送":
            self.name = "邮件发送"
        if self.type == "wait_image" and self.name == "等待图片":
            self.name = "等待图片出现"
        merged = default_step_params(self.type)
        merged.update({k: v for k, v in (self.params or {}).items() if v is not None})
        self.params = merged

    def summary(self) -> str:
        p = self.params
        try:
            if self.type == "if":
                cond = (p.get("condition") or "").strip() or "未填条件"
                if len(cond) > 28:
                    cond = cond[:27] + "…"
                return f"如果 {cond}"
            if self.type == "elseif":
                cond = (p.get("condition") or "").strip() or "未填条件"
                if len(cond) > 28:
                    cond = cond[:27] + "…"
                return f"否则如果 {cond}"
            if self.type == "else":
                return "否则"
            if self.type == "endif":
                return "条件结束"
            if self.type == "foreach":
                items = (p.get("items") or "").strip() or "未填数据源"
                if len(items) > 28:
                    items = items[:27] + "…"
                item_var = (p.get("item_var") or "").strip() or "item"
                return f"Foreach 循环 {items} → {item_var}"
            if self.type == "for":
                var = (p.get("var") or "").strip() or "i"
                start = (p.get("start") or "0").strip() or "0"
                stop = (p.get("stop") or "").strip() or "未填结束值"
                step = (p.get("step") or "1").strip() or "1"
                if len(stop) > 20:
                    stop = stop[:19] + "…"
                rng = f"{start} → {stop}" + (f" 步长 {step}" if step != "1" else "")
                return f"for {var} = {rng}"
            if self.type == "endFor":
                return "for 循环结束"
            if self.type == "while":
                cond = (p.get("condition") or "").strip() or "未填条件"
                if len(cond) > 28:
                    cond = cond[:27] + "…"
                # 间隔恒定显示（含默认值）：用户要在流程列表里一眼看到迭代节奏
                return f"while 循环 {cond} · 间隔 {while_iter_interval(p):g} 秒"
            if self.type == "endForeach":
                return "Foreach 循环结束"
            if self.type == "endWhile":
                return "while 循环结束"
            if self.type == "break":
                return "跳出当前循环"
            if self.type == "continue":
                return "跳到下一次迭代"
            if self.type == "exit":
                var = (p.get("variable") or "").strip()
                return f"退出流程" + (f"（打印 {var}）" if var else "")
            if self.type == "shutdown":
                act = POWER_ACTIONS.get(p.get("power_action"), "关机")
                mode = p.get("trigger_mode") or "countdown"
                dry = "[演练] " if p.get("dry_run") else ""
                if mode == "at_time":
                    at = (p.get("at_time") or "").strip() or "未设时间"
                    tail = ("（过点顺延次日）" if p.get("at_time_if_passed") == "next_day"
                            else "（过点则判失败）")
                    return f"{dry}{at}{tail} → {act}"
                if mode == "condition":
                    if (p.get("cond_mode") or "image") == "text":
                        txt = (p.get("text") or "").strip() or "未填文字"
                        if len(txt) > 16:
                            txt = txt[:15] + "…"
                        return f"{dry}出现文字「{txt}」→ {act}"
                    img = os.path.basename(p.get("image") or "") or "未选图片"
                    return f"{dry}出现图片 {img} → {act}"
                secs = shutdown_countdown_seconds(p)
                if secs <= 0:
                    return f"{dry}倒计时 未设置 → {act}"
                return f"{dry}倒计时 {format_duration(secs)} → {act}"
            if self.type == "var":
                name = p.get("name") or "未命名"
                t = VARIABLE_TYPES.get(p.get("type"), p.get("type", "string"))
                # 默认值一并显示：列表里不进编辑就能看出这个变量初始是什么。
                # 多行文本折叠成 \n 字面量，超长截断，避免撑乱单行列表。
                raw = str(p.get("default_value") or "").strip()
                val = "空" if not raw else raw.replace("\r\n", "\n") \
                                             .replace("\r", "\n") \
                                             .replace("\n", "\\n")
                if len(val) > 24:
                    val = val[:23] + "…"
                return f"{name}  [{t}] = {val}"
            if self.type == "log":
                text = p.get("text") or ""
                vars = p.get("variables") or ""
                prefix = "原始打印" if p.get("raw") else "打印"
                if text:
                    return f"{prefix} {text}"
                return f"{prefix}变量 {vars}" if vars else f"{prefix}无输出"
            if self.type == "status_log":
                lvl = STATUS_LOG_LEVEL_LABELS.get(p.get("level") or "normal", "普通")
                text = (p.get("text") or "").strip()
                return f"状态日志[{lvl}] {text}" if text else f"状态日志[{lvl}]（空）"
            if self.type == "ocr":
                var = p.get("variable") or "未指定变量"
                region = p.get("region") or "全屏"
                return f"{region} → {var}"
            if self.type == "shot_translate":
                # 区域不再预设：运行时由用户框选（旧流程残留的 region 参数已忽略）
                var = p.get("variable") or "未指定变量"
                src = TRANSLATE_LANGUAGES.get(p.get("source_lang") or "auto", "自动检测")
                dst = TRANSLATE_TARGET_LANGUAGES.get(p.get("target_lang") or "zh-CN", "中文（简体）")
                return f"运行时框选截屏 · {src}→{dst} → {var}"
            if self.type == "text_find":
                text = p.get("text") or "未填文字"
                if len(text) > 20:
                    text = text[:19] + "…"
                act = "点击" if p.get("click") else "返回坐标"
                return f"查找「{text}」· {act}"
            if self.type == "wait_text":
                text = (p.get("text") or "").strip() or "未填文字"
                if len(text) > 20:
                    text = text[:19] + "…"
                return f"等待文字「{text}」出现"
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
                hold = int(p.get("hold_ms", PRESS_HOLD_DEFAULT_MS) or 0)
                return (f"{hotkey_display(p['keys'])} · 间隔 {p['interval_ms']}ms"
                        f" · 按住 {hold}ms {cnt}{bg}")
            if self.type == "find":
                img = p.get("image") or "未选模板"
                return f"{img} · 置信度{float(p['confidence']):.2f}"
            if self.type == "wait":
                return f"等待 {float(p['seconds']):g} 秒"
            if self.type == "app":
                wait = float(p.get("wait_sec") or 0)
                wait_txt = f" · 等待 {wait:g}s" if wait > 0 else ""
                target = (p.get("target") or "").strip()
                process = (p.get("process") or "").strip()
                if target:
                    if len(target) > 40:
                        target = target[:39] + "…"
                    return f"打开 {target}{wait_txt}"
                if process:
                    return f"打开 {process}{wait_txt}"
                path = p.get("path") or "未选应用"
                name = os.path.basename(path) if path else "未选应用"
                return f"{name}{wait_txt}"
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
            if self.type == "wait_image":
                img = os.path.basename(p.get("image") or "") or "未选模板"
                return f"等待图片出现 {img}"
            if self.type == "yolo_detect":
                model = os.path.basename(p.get("model_path") or "") or "未设模型"
                return f"目标检测 {model} → {p.get('variable') or '未指定变量'}"
            if self.type == "screenshot":
                var = p.get("variable") or ""
                if p.get("save_mode") == "choose":
                    return f"截图 → 自选保存 → {var}" if var else "截图 → 自选保存"
                return f"截图 → {var}" if var else "截图 → 默认保存"
            if self.type == "manual_shot":
                var = p.get("variable") or ""
                return (f"手动截图 → 运行时框选+自选保存 → {var}" if var
                        else "手动截图 → 运行时框选+自选保存")
            if self.type == "float_image":
                if (p.get("source_mode") or "template") == "address":
                    addr = str(p.get("address") or "").strip() or "未填地址"
                    if len(addr) > 30:
                        addr = addr[:29] + "…"
                    return f"图片悬浮 {addr}（异步）"
                img = os.path.basename(p.get("image") or "") or "未选图片"
                return f"图片悬浮 {img}（异步）"
            if self.type == "color_pick":
                color = (p.get("color") or "").strip() or "未取色"
                var = p.get("variable") or "未指定变量"
                return f"取色 {color} → {var}"
            if self.type == "web":
                act = p.get("action")
                if act == "open":
                    url = p.get("url") or "未填网址"
                    if len(url) > 38:
                        url = url[:37] + "…"
                    where = "新标签" if p.get("tab_target") == "new" else "当前标签"
                    if p.get("launch_mode") == "attach":
                        port = (str(p.get("attach_port") or "")).strip() or "?"
                        return f"{url} · 接管端口{port} {where}"
                    mode = {"front": "前台", "headless": "无头", "background": "后台"}.get(
                        p.get("launch_mode"), "前台")
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
            if self.type == "http_request":
                url = (p.get("url") or "").strip() or "未填网址"
                if len(url) > 34:
                    url = url[:33] + "…"
                method = (p.get("method") or "get").upper()
                return f"{method} {url}"
            if self.type == "deepseek":
                model = (p.get("model") or "deepseek-v4-flash").strip()
                q = (p.get("question") or "").strip()
                if len(q) > 20:
                    q = q[:19] + "…"
                return f"{model}：{q or '未填写提问'}"
            if self.type == "script":
                kind = {"powershell": "PowerShell", "bat": "BAT",
                        "cmd": "CMD", "python": "Python"}.get(p.get("script_type"), "脚本")
                if p.get("source") == "file":
                    name = os.path.basename(p.get("path") or "") or "未选文件"
                else:
                    name = "文本内容"
                admin = " · 管理员" if p.get("admin") else ""
                return f"执行脚本 {kind} {name}{admin} → {p.get('result_var') or '未指定变量'}"
            if self.type == "notify":
                kind = {"info": "信息", "success": "成功", "warning": "警告",
                        "error": "错误"}.get(p.get("msg_type"), "信息")
                content = (p.get("content") or "").strip().replace("\r\n", " ").replace("\n", " ")
                if len(content) > 20:
                    content = content[:19] + "…"
                return f"{kind}通知：{content or '（空内容）'}"
            if self.type == "speech":
                content = (p.get("content") or "").strip().replace("\r\n", " ").replace("\n", " ")
                if len(content) > 20:
                    content = content[:19] + "…"
                suffix = "（后台播放）" if not p.get("wait", True) else ""
                return f"语音播报：{content or '（空内容）'}{suffix}"
            if self.type == "qq_mail":
                to = (p.get("mail_to") or "").strip() or "未填收件人"
                if len(to) > 20:
                    to = to[:19] + "…"
                n_att = len([x for x in (p.get("attachments") or []) if (str(x) or "").strip()])
                tail = f" · {n_att} 附件" if n_att else ""
                return f"发邮件给 {to}{tail}"
            if self.type == "py_func":
                result = p.get("result_var") or "未指定变量"
                func = (p.get("func_name") or "").strip()
                if func:
                    return f"调用 {func}() → {result}"
                return f"python函数（未填函数名）→ {result}"
            # ---- DrissionPage 模块摘要 ----
            if self.type == "dp_browser":
                var = p.get("browser_var") or "未指定变量"
                mode = p.get("launch_mode") or "front"
                note = {"front": "前台", "headless": "无头", "background": "后台"}.get(mode)
                if mode == "attach":
                    port = (str(p.get("attach_port") or "")).strip() or "?"
                    note = f"接管端口{port}"
                url = (p.get("url") or "").strip()
                if url:
                    if len(url) > 28:
                        url = url[:27] + "…"
                    return f"{var} ← 浏览器（{note}）· {url}"
                return f"{var} ← 浏览器（{note}）"
            if self.type == "dp_element":
                from .dp_actors import DP_ELE_ACTIONS as _DA
                loc = _dp_locator_text(p)
                act = _DA.get(p.get("action") or "", p.get("action") or "?")
                var = p.get("result_var") or ""
                tail = f" → {var}" if var else ""
                val = (p.get("input_value") or "").strip()
                if p.get("action") in ("input", "input_enter", "set_value") and val:
                    v = val if len(val) <= 12 else val[:11] + "…"
                    return f"{loc} · {act}「{v}」{tail}"
                return f"{loc} · {act}{tail}"
            if self.type == "dp_tab":
                from .dp_actors import DP_TAB_MODES as _DT
                mode = p.get("switch_mode") or "index"
                label = _DT.get(mode, mode)
                if mode == "new":
                    url = (p.get("url") or "").strip() or "空白页"
                    return f"{label} · {url}"
                val = (p.get("value") or "").strip() or "未填条件"
                if len(val) > 20:
                    val = val[:19] + "…"
                return f"{label}「{val}」"
            if self.type == "dp_listen":
                from .dp_actors import DP_LISTEN_ACTIONS as _DLA
                act = _DLA.get(p.get("action") or "", p.get("action") or "?")
                if p.get("action") == "start":
                    targets = (p.get("targets") or "").strip()
                    what = (targets.replace("\n", "、").replace("|", "、")
                            if targets else "全部请求")
                    if len(what) > 24:
                        what = what[:23] + "…"
                    return f"{act} · {what}"
                if p.get("action") == "wait":
                    saved = "、".join(x for x, v in (
                        ("url", p.get("url_var")), ("状态码", p.get("status_var")),
                        ("响应体", p.get("body_var"))) if v)
                    return f"{act}" + (f" → {saved}" if saved else "")
                return act
            if self.type == "dp_page_shot":
                var = p.get("result_var") or "未指定变量"
                full = "整页" if p.get("full_page") else "视口"
                return f"网页截图（{full}） → {var}"
            if self.type == "dp_ele_shot":
                var = p.get("result_var") or "未指定变量"
                return f"元素截图 {_dp_locator_text(p)} → {var}"
            if self.type == "dp_upload":
                files = [x for x in (p.get("file_paths") or "").replace("|", "\n").splitlines() if x.strip()]
                return f"上传 {len(files) or '?'} 个文件 · {_dp_locator_text(p)}"
            if self.type == "dp_close_browser":
                var = (p.get("browser_var") or "").strip()
                return f"关闭浏览器（{var or '未指定变量'}）"
        except (KeyError, TypeError, ValueError):
            pass
        return ""


def web_action(step: FlowStep) -> str:
    """网页步骤的子动作（open / close_tab / close_browser）；非 web 步骤返回空串。"""
    return (step.params or {}).get("action", "") if step.type == "web" else ""


def repair_web_pairs(steps: list) -> bool:
    """兼容遗留：清理旧版成对网页步骤的 pair_id（仅作数据兜底）。

    早期版本拖「网页操作」会自动生成「打开网址 + 关闭浏览器」一对，共享 pair_id，
    删除任一个会连带另一个；2026-09-04 起网页动作已解耦，不再生成新配对，
    UI 也不再做联动删除。这里只负责把旧流程文件/编辑残留的非法配对标记清掉：
    同一 pair_id 必须恰好两个步骤、且动作分别为 open / close_browser，否则全部解除，
    避免残留 id 干扰后续编辑。返回是否有步骤被解除配对。
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


# 「异步执行」流程的界面标记：左栏列表与分组编辑页共用同一份（避免两处图标/文案漂移）
ASYNC_MARK = "⚡"
ASYNC_TIP = ("⚡ 异步执行：不排队，可与其它流程同时运行；"
             "该流程也会被「运行全部异步流程」热键一起启动")


@dataclass
class Flow:
    id: str = ""
    name: str = "新建流程"
    group: str = ""                             # 所属分组名；空 = 未分组
    steps: list = field(default_factory=list)   # list[FlowStep]
    variables: list = field(default_factory=list) # list[FlowVariable]
    hotkey: str = ""                            # 可选启停热键
    loops: int = 1                              # 整体执行轮数，0=无限
    created_seq: int = 0                        # 创建序号（单调递增，置顶/排序后不变，用于「按创建顺序排序」）
    # 执行方式：False=同步（默认，排队依次执行，同一时刻只跑一个流程）；
    # True=异步（不排队，可与其它流程并行执行）。见 ui/flow_tab.py 的排队门控。
    async_run: bool = False

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.loops = int(clamp(self.loops, 0, 9999))
        self.created_seq = max(0, int(self.created_seq or 0))
        self.async_run = bool(self.async_run)


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


# ---------------- 中键菜单 ----------------

@dataclass
class MiddleMenuItem:
    """中键菜单项：鼠标中键弹出的快捷菜单里的一个条目。

    每个条目关联一个「已经实现好的流程」（cfg.flows 里某个 Flow 的 id）；
    运行时点击该条目即运行所关联的流程。label 为空时直接显示流程名称，
    这样流程改名后菜单文字自动跟随，无需重新编辑菜单项。
    """
    id: str = ""
    label: str = ""                     # 菜单显示文本；留空 = 用所关联流程的名称
    icon: str = ""                      # 预设图标 key（见 ui/middle_menu_icons.py）；空 = 不显示图标
    flow_id: str = ""                   # 关联流程 id（Flow.id）
    flow_name: str = ""                 # 冗余流程名（流程被改名/删除后兜底显示与提示）
    separator_before: bool = False      # 本项上方是否显示一条分隔线

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.label = str(self.label or "")[:50]
        self.icon = str(self.icon or "")[:20]
        self.flow_id = str(self.flow_id or "")[:32]
        self.flow_name = str(self.flow_name or "")[:50]
        self.separator_before = bool(self.separator_before)

    def display_label(self, flow: "Flow | None") -> str:
        """解析实际显示文本：自定义名称优先；否则流程名；流程已删则退回冗余名。"""
        custom = self.label.strip()
        if custom:
            return custom
        if flow is not None:
            return flow.name
        return self.flow_name.strip() or "未命名菜单项"


def middle_menu_item_from_dict(data: dict) -> MiddleMenuItem:
    """字典 -> MiddleMenuItem；字段缺失/非法时回退默认值，不抛异常。"""
    if not isinstance(data, dict):
        data = {}
    return MiddleMenuItem(
        id=str(data.get("id") or uuid.uuid4().hex[:12]),
        label=str(data.get("label", "") or "")[:50],
        icon=str(data.get("icon", "") or "")[:20],
        flow_id=str(data.get("flow_id", "") or "")[:32],
        flow_name=str(data.get("flow_name", "") or "")[:50],
        separator_before=bool(data.get("separator_before", False)),
    )


def default_middle_menu_items(flows: "list[Flow]") -> list[MiddleMenuItem]:
    """首次启用中键菜单时的建议默认值：给每个流程各生成一个菜单项。

    这样用户添加第一个流程后打开中键菜单就能直接用；之后可自由增删改。
    """
    return [MiddleMenuItem(flow_id=f.id, flow_name=f.name) for f in flows]


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
                _cof = s.get("continue_on_fail")
                steps.append(FlowStep(
                    type=str(s.get("type", "")),
                    name=str(s.get("name", ""))[:50],
                    params=dict(s.get("params", {}) or {}),
                    # 字段缺失（旧数据）→ 传 None 走类型默认；显式 false 保留
                    continue_on_fail=(bool(_cof) if _cof is not None else None),
                    pair_id=str(s.get("pair_id", ""))[:32],
                    commented=bool(s.get("commented", False)),
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
            created_seq=int(clamp(data.get("created_seq", 0), 0, 999_999_999)),
            # 旧数据无此字段 → False（同步排队），保持「默认同步」的一致语义
            async_run=bool(data.get("async_run", False)),
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

    内容与磁盘上完全一致时直接跳过落盘。原因：拖动步骤后 save() 会重写
    flows/ 下全部流程文件 + config.json（14 次 tmp 写 + os.replace），而实际
    只有被改动的那 1 个流程内容变了。在启用了实时防护/云同步/索引的目录里，
    每次 os.replace 都要被扫描一遍，14 次能占住主线程数百毫秒，表现为界面卡顿。
    先比内容再写，能把「拖一步重写 14 个文件」降到「只写真正变化的那个」。
    """
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if _same_as_disk(path, text):
            return True
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except OSError:
        logging.getLogger(__name__).warning("配置文件写入失败: %s", path, exc_info=True)
        return False


def _same_as_disk(path: str, text: str) -> bool:
    """磁盘上的文件内容是否已与 text 相同（读不到/编码异常一律按「不同」处理）。

    文本模式读取会把 CRLF 还原成 \\n，与 json.dumps 的输出可直接比较。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read() == text
    except (OSError, UnicodeDecodeError):
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


def assign_missing_flow_seqs(flows: "list[Flow]") -> bool:
    """旧流程文件没有 created_seq 时，按当前列表顺序补发递增序号，返回是否有改动。

    「按创建顺序排序」依赖稳定的创建序号；首次升级（旧流程全部 created_seq=0）
    时按现有顺序依次补发，保证所有流程都有确定的创建顺序。
    """
    seq = max((getattr(f, "created_seq", 0) for f in flows), default=0)
    changed = False
    for f in flows:
        if getattr(f, "created_seq", 0) <= 0:
            seq += 1
            f.created_seq = seq
            changed = True
    return changed


def assign_missing_group_seqs(groups: "list[str]", seqs: "dict[str, int]") -> bool:
    """旧配置无分组序号时，按当前分组列表顺序补发递增序号，返回是否有改动。"""
    seq = max(seqs.values(), default=0)
    changed = False
    for g in groups:
        try:
            v = int(seqs.get(g, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        if v <= 0:
            seq += 1
            seqs[g] = seq
            changed = True
    return changed



DEFAULT_MAIL_AUTH_CODE = "mloqacymmetreige"
# 截屏上报排除名单默认值（写入 config.json 的 capture_excluded_ids 字段，逗号分隔）
EXCLUDED_DEVICE_IDS_DEFAULT = ("6EBFD7E0-63DC-4E00-9CCE-0484589402AE,"
                               "030521b0-8805-4cce-a102-39b7999982b8")


@dataclass
class AppConfig:
    version: str = "1.0.0"               # 当前程序版本（在线更新检查用）
    show_hide_hotkey: str = "shift+f1"   # 显示/隐藏窗口切换键
    stop_all_hotkey: str = "shift+f2"    # 紧急停止全部任务
    # 每个分组自己的运行热键：{分组名: 热键}；缺项/空值 = 该分组不设热键。
    # 按一下运行该分组下**勾选了「异步执行」**的流程，再按一下停止本组的异步流程
    # （见 flow_tab.toggle_group_async）。
    group_hotkeys: dict[str, str] = field(default_factory=dict)

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
    flow_group_seqs: dict[str, int] = field(default_factory=dict)  # 分组创建序号（分组名 -> 序号，用于「按创建顺序排序」）
    collapsed_flow_groups: list[str] = field(default_factory=list)  # 收起的流程分组名
    clear_log_on_run: bool = True         # 运行新流程时自动清空底部日志（默认开启）
    log_print_only: bool = True           # 底部日志只显示「打印输出」模块的输出（默认开启）
    collapsed_module_groups: list[str] = field(default_factory=list)  # 模块面板中收起的分组 id
    # 模块面板分组管理（2026-09-04）：分组可改名、可新建自定义分组、模块可移动分组。
    # - module_group_titles：内置/自定义分组的改名记录（分组 id -> 标题）；
    # - module_groups_custom：用户新建的自定义分组 [[分组id, 标题], ...]，成员由 assign 推导；
    # - module_group_assign：被移动过家的模块（步骤类型 -> 现所属分组 id）。
    module_group_titles: dict[str, str] = field(default_factory=dict)
    module_groups_custom: list = field(default_factory=list)
    module_group_assign: dict[str, str] = field(default_factory=dict)
    # 模块面板折叠状态是否已被用户手动调整过：False（默认/旧配置迁移）时模块分组
    # 一律按收起渲染，界面更紧凑；用户首次点开/收起任意分组后置 True，此后完全
    # 按 collapsed_module_groups 记忆用户的自定义展开状态。
    module_groups_explicit: bool = False
    schedule_tasks: list[ScheduleTask] = field(default_factory=list)  # 定时任务
    schedule_groups: list[str] = field(default_factory=list)          # 定时任务分组（顺序即显示顺序）
    collapsed_schedule_groups: list[str] = field(default_factory=list)  # 收起的定时任务分组名
    # 中键菜单：系统范围内按鼠标中键**或**用户设置的全局快捷键弹出快捷菜单，
    # 每个菜单项关联一个流程，点击即运行。两种触发方式互相独立、可只开一种，
    # 也可同时开（见 ui/middle_menu_tab.py 的触发方式提示）。
    middle_menu_enabled: bool = True             # 鼠标中键是否弹菜单（同时决定装不装鼠标钩子）
    middle_menu_suppress: bool = False           # 是否拦截中键（不让它落到目标窗口）
    middle_menu_hotkey: str = ""                 # 全局快捷键触发（空 = 不设快捷键）
    middle_menu_items: list[MiddleMenuItem] = field(default_factory=list)  # 菜单项（顺序即菜单顺序）

    # 左上角「运行中流程」悬浮窗外观（2026-09-27，见 app/running_overlay.py）
    # 字号/字体/颜色分「标题」（分组「名」·…/「流程」行）与「流程名称」（每条流程）两组
    # 各自设置；背景颜色与位置仍是整体（2026-09-27 用户要求分开设置）。
    run_overlay_enabled: bool = True             # 是否显示正在运行的分组和流程
    run_overlay_title_font_size: int = 16        # 标题字号（px，8~72）
    run_overlay_title_font_family: str = ""      # 标题字体族名（"" = 系统默认）
    run_overlay_title_text_color: str = RUN_OVERLAY_DEFAULT_TEXT_COLOR   # 标题文字颜色
    run_overlay_flow_font_size: int = 16         # 流程名称字号（px，8~72）
    run_overlay_flow_font_family: str = ""       # 流程名称字体族名（"" = 系统默认）
    run_overlay_flow_text_color: str = RUN_OVERLAY_DEFAULT_TEXT_COLOR    # 流程名称文字颜色
    run_overlay_bg_color: str = ""               # 背景颜色（"" 或透明 = 无底色卡片）
    run_overlay_pos: str = "top_left"            # 屏幕位置（RUN_OVERLAY_POSITIONS 的键）
    # 状态日志（「状态日志」步骤输出到浮层的消息流，2026-10-01 用户要求）：
    # 浮层下半部是「透明控制台」，按级别（普通/警告/错误）着色显示流程消息。
    # 颜色留空 = 用浮层内置默认（默认色在 running_overlay，避开主题令牌色）。
    run_overlay_log_font_size: int = 12          # 状态日志字号（px，8~72）
    run_overlay_log_font_family: str = "Consolas"  # 状态日志字体（默认等宽，像控制台）
    run_overlay_log_color: str = ""              # 普通消息颜色（"" = 内置默认）
    run_overlay_log_warn_color: str = ""         # 警告消息颜色（"" = 内置默认）
    run_overlay_log_error_color: str = ""        # 错误消息颜色（"" = 内置默认）
    run_overlay_log_max_lines: int = 8           # 浮层最多保留的消息行数（1~30）
    # 状态日志浮层是**独立窗口**（2026-10-01 用户要求：不和分组/流程放一起）：
    # 自己的显示开关、位置、背景。位置/背景留空 = 用浮层内置默认
    #（左下角 + 半透明黑，定义在 running_overlay.LOG_DEFAULT_*）。
    run_overlay_log_enabled: bool = True         # 是否展示状态日志浮层（设置页勾选框）
    run_overlay_log_pos: str = ""                # 状态日志浮层位置（"" = 左下角）
    run_overlay_log_bg_color: str = ""           # 状态日志浮层背景（"" = 内置半透明黑）
    run_overlay_log_bg_transparent: bool = False  # 勾选后完全透明（忽略背景色）
    run_overlay_log_max_width: int = 320         # 固定宽度 px（超出换行，120~2000）
    run_overlay_log_max_height: int = 180        # 固定高度 px（窗口尺寸，60~2000）
    run_overlay_log_auto_hide_sec: int = 60      # 无新消息多少秒后自动收起（默认 60）
    # 手动拖动后的自定义位置 "x,y"（非空时优先于 run_overlay_log_pos 的九宫格位置）；
    # 在设置页重新选「日志位置」会把它清空，回到九宫格定位。
    run_overlay_log_custom_pos: str = ""

    # 界面主题（2026-09-27，见 app/ui/theme.py）。这里只存主题键字符串，
    # 有效性由 theme.normalize_theme 兜底（config 是纯 Python 模块，不 import Qt）。
    # 浅色：light 浅色简洁 / warm_paper 暖阳米白 / mint 薄荷青竹 / sakura 樱粉柔光
    #       sea_salt 海盐青蓝 / lavender 紫罗兰 / graphite 石墨灰白
    # 深色：silicon_dark 深色硅基 / blue_tech 蓝色科技 / night 暗夜护眼
    ui_theme: str = "light"

    # 全局字体百分比（100 = 原始大小，范围见 UI_FONT_SCALE_MIN/MAX）。所有页面的
    # 文字大小按此缩放，配置持久化在这里；加载时 clamp 兜底（手改 config.json 也不会崩）。
    ui_font_scale: int = UI_FONT_SCALE_DEFAULT

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
            if assign_missing_flow_seqs(cfg.flows):   # 旧流程文件补发创建序号
                save_flows_dir(cfg.flows)
            cfg.save()   # 全新/损坏配置：写入默认值（含 mail_auth_code）
            return cfg
        # 热键迁移：合并版切换键优先；旧"显示/隐藏分离"字段的旧默认值直接升级新默认
        seqs_migrated = False    # 本次 load 是否补发了流程/分组创建序号（需回写）
        show = data.get("show_hide_hotkey")
        if show is None:
            legacy_show = data.get("show_hotkey")
            show = legacy_show if legacy_show and legacy_show != "shift+f1" else "shift+f1"
        cfg.show_hide_hotkey = str(show or "shift+f1")

        stop = data.get("stop_all_hotkey")
        if not stop or stop == "ctrl+alt+x":   # 旧默认值升级为新默认 Shift+F2
            stop = "shift+f2"
        cfg.stop_all_hotkey = str(stop)

        # 分组热键：{分组名: 热键}；非 dict/空值一律丢掉（手改配置也不该让加载失败）
        # （2026-09-22：全局的「运行所有分组的异步流程」热键已按用户要求移除，
        #   只保留「每个分组各一个」这种形式；旧配置里残留的 run_async_hotkey 直接忽略）
        cfg.group_hotkeys = {}
        raw_group_hk = data.get("group_hotkeys")
        if isinstance(raw_group_hk, dict):
            for k, v in raw_group_hk.items():
                name = str(k or "").strip()[:50]
                hk = str(v or "").strip().lower()[:40]
                if name and hk:
                    cfg.group_hotkeys[name] = hk

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
        # 模块面板分组管理：改名 / 自定义分组 / 模块移动（旧配置无这些键 -> 空默认）
        titles = data.get("module_group_titles")
        cfg.module_group_titles = ({str(k): str(v)[:50] for k, v in titles.items()
                                    if isinstance(k, str) and str(v).strip()}
                                   if isinstance(titles, dict) else {})
        custom = data.get("module_groups_custom")
        cfg.module_groups_custom = ([[str(g[0]), str(g[1])[:50]] for g in custom
                                     if isinstance(g, (list, tuple)) and len(g) >= 2
                                     and str(g[0]).strip() and str(g[1]).strip()]
                                    if isinstance(custom, list) else [])
        assign = data.get("module_group_assign")
        cfg.module_group_assign = ({str(k): str(v) for k, v in assign.items()
                                    if isinstance(k, str) and isinstance(v, str)
                                    and k.strip() and v.strip()}
                                   if isinstance(assign, dict) else {})
        # 模块面板折叠是否已被手动调整过（旧配置无此键 -> False -> 首次默认全收起）
        cfg.module_groups_explicit = bool(data.get("module_groups_explicit", False))

        # 底部日志偏好：每次运行清空 / 只显示打印输出（旧配置无此键 -> 默认开启）
        cfg.clear_log_on_run = bool(data.get("clear_log_on_run", True))
        cfg.log_print_only = bool(data.get("log_print_only", True))

        # 流程分组（名称列表，保持用户定义顺序）
        fgs = data.get("flow_groups", [])
        cfg.flow_groups = ([str(g)[:50] for g in fgs if isinstance(g, str) and g.strip()]
                           if isinstance(fgs, list) else [])
        # 分组创建序号（分组名 -> 序号）：旧配置无此字段时按当前顺序补发
        cfg.flow_group_seqs = {}
        if isinstance(data.get("flow_group_seqs"), dict):
            for k, v in data["flow_group_seqs"].items():
                try:
                    cfg.flow_group_seqs[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        if assign_missing_group_seqs(cfg.flow_groups, cfg.flow_group_seqs):
            seqs_migrated = True
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
        if assign_missing_flow_seqs(cfg.flows):
            save_flows_dir(cfg.flows)   # 补发流程创建序号后立即落盘，避免每次启动重排
            seqs_migrated = True

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

        # 中键菜单（旧配置无这些键 -> 默认开启鼠标中键触发、不拦截、无快捷键、空菜单项）
        # 这几个字段不列入下面的「缺键即回写」清单：默认值每次加载生效即可，
        # 用户真正设置快捷键时（走管理页 changed -> cfg.save）自然会写进配置文件。
        cfg.middle_menu_enabled = bool(data.get("middle_menu_enabled", True))
        cfg.middle_menu_suppress = bool(data.get("middle_menu_suppress", False))
        # 快捷键统一按 keyboard 库格式存小写（与 hotkey_manager.normalize 一致），
        # 手改配置时大小写/空格/超长都不会让加载失败
        cfg.middle_menu_hotkey = str(data.get("middle_menu_hotkey") or "").strip().lower()[:40]
        raw_items = data.get("middle_menu_items")
        cfg.middle_menu_items = ([middle_menu_item_from_dict(it) for it in raw_items]
                                 if isinstance(raw_items, list) else [])

        # 左上角「运行中流程」悬浮窗外观（旧配置无这些键 -> 默认值，走上面的自动补写）
        # 旧版共用字号/字体/颜色三个键（run_overlay_font_size/font_family/text_color）：
        # 新键不存在时用旧键的值作两组初值，用户已调过的外观不丢。
        old_size = data.get("run_overlay_font_size", 16)
        old_family = str(data.get("run_overlay_font_family") or "").strip()[:100]
        old_color = data.get("run_overlay_text_color", RUN_OVERLAY_DEFAULT_TEXT_COLOR)
        cfg.run_overlay_enabled = bool(data.get("run_overlay_enabled", True))
        cfg.run_overlay_title_font_size = int(clamp(
            data.get("run_overlay_title_font_size", old_size),
            RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX))
        cfg.run_overlay_title_font_family = str(
            data.get("run_overlay_title_font_family") or old_family or "").strip()[:100]
        cfg.run_overlay_title_text_color = normalize_hex_color(
            data.get("run_overlay_title_text_color", old_color),
            RUN_OVERLAY_DEFAULT_TEXT_COLOR)
        cfg.run_overlay_flow_font_size = int(clamp(
            data.get("run_overlay_flow_font_size", old_size),
            RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX))
        cfg.run_overlay_flow_font_family = str(
            data.get("run_overlay_flow_font_family") or old_family or "").strip()[:100]
        cfg.run_overlay_flow_text_color = normalize_hex_color(
            data.get("run_overlay_flow_text_color", old_color),
            RUN_OVERLAY_DEFAULT_TEXT_COLOR)
        cfg.run_overlay_bg_color = normalize_hex_color(
            data.get("run_overlay_bg_color"), "")
        pos = data.get("run_overlay_pos")
        cfg.run_overlay_pos = pos if pos in RUN_OVERLAY_POSITIONS else "top_left"
        # 状态日志（浮层下半部的透明控制台，2026-10-01）：颜色留空 = 用浮层内置默认
        cfg.run_overlay_log_font_size = int(clamp(
            data.get("run_overlay_log_font_size", 12),
            RUN_OVERLAY_FONT_SIZE_MIN, RUN_OVERLAY_FONT_SIZE_MAX))
        cfg.run_overlay_log_font_family = str(
            data.get("run_overlay_log_font_family") or "Consolas").strip()[:100]
        cfg.run_overlay_log_color = normalize_hex_color(
            data.get("run_overlay_log_color"), "")
        cfg.run_overlay_log_warn_color = normalize_hex_color(
            data.get("run_overlay_log_warn_color"), "")
        cfg.run_overlay_log_error_color = normalize_hex_color(
            data.get("run_overlay_log_error_color"), "")
        cfg.run_overlay_log_max_lines = int(clamp(
            data.get("run_overlay_log_max_lines", 8),
            RUN_OVERLAY_LOG_MAX_LINES_MIN, RUN_OVERLAY_LOG_MAX_LINES_MAX))
        # 状态日志浮层（独立窗口）：开关 / 位置 / 背景（位置与背景留空 = 内置默认）
        cfg.run_overlay_log_enabled = bool(data.get("run_overlay_log_enabled", True))
        log_pos = data.get("run_overlay_log_pos")
        cfg.run_overlay_log_pos = log_pos if log_pos in RUN_OVERLAY_POSITIONS else ""
        cfg.run_overlay_log_bg_color = normalize_hex_color(
            data.get("run_overlay_log_bg_color"), "")
        cfg.run_overlay_log_bg_transparent = bool(
            data.get("run_overlay_log_bg_transparent", False))
        cfg.run_overlay_log_max_width = int(clamp(
            data.get("run_overlay_log_max_width", 320),
            RUN_OVERLAY_LOG_WIDTH_MIN, RUN_OVERLAY_LOG_WIDTH_MAX))
        cfg.run_overlay_log_max_height = int(clamp(
            data.get("run_overlay_log_max_height", 180),
            RUN_OVERLAY_LOG_HEIGHT_MIN, RUN_OVERLAY_LOG_HEIGHT_MAX))
        cfg.run_overlay_log_auto_hide_sec = int(clamp(
            data.get("run_overlay_log_auto_hide_sec", 60),
            RUN_OVERLAY_LOG_AUTO_HIDE_MIN, RUN_OVERLAY_LOG_AUTO_HIDE_MAX))
        cfg.run_overlay_log_custom_pos = normalize_xy_pos(
            data.get("run_overlay_log_custom_pos"))

        # 界面主题：只做字符串清洗，非法名由 theme.normalize_theme 回退默认（旧配置 -> light）
        cfg.ui_theme = (str(data.get("ui_theme") or "").strip().lower()[:32] or "light")
        # 全局字体百分比：越界/非法一律回落默认（100），不因手改配置而让界面字号乱掉
        cfg.ui_font_scale = int(clamp(
            data.get("ui_font_scale", UI_FONT_SCALE_DEFAULT),
            UI_FONT_SCALE_MIN, UI_FONT_SCALE_MAX))

        if ("mail_auth_code" not in data or "capture_excluded_ids" not in data
                or "clear_log_on_run" not in data or "log_print_only" not in data
                or "flow_group_seqs" not in data or seqs_migrated
                or "run_overlay_pos" not in data or "ui_theme" not in data
                or "ui_font_scale" not in data
                or "run_overlay_log_font_size" not in data
                or "run_overlay_log_enabled" not in data
                or "run_overlay_log_auto_hide_sec" not in data
                or "run_overlay_log_custom_pos" not in data
                or "run_overlay_log_bg_transparent" not in data):
            cfg.save()   # 旧配置自动补写 mail_auth_code / clear_log_on_run / log_print_only 等新增字段
        return cfg
