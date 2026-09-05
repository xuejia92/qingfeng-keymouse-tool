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

FLOW_STEP_TYPES = {"var": "变量", "log": "打印输出", "ocr": "文字识别",
                   "text_find": "文字查找", "screenshot": "截图",
                   "find_image": "找图", "yolo_detect": "目标检测",
                   "click": "鼠标点击", "press": "键盘连按", "find": "找图点击",
                   "wait": "延时等待", "web": "打开关闭网页或浏览器", "http_request": "网络请求",
                   "deepseek": "DeepSeek 对话", "script": "执行脚本",
                   "notify": "消息通知",
                   "speech": "语音播报",
                   "app": "打开应用",
                   "close_app": "关闭应用", "clip_set": "赋值剪贴板",
                   "clip_get": "获取剪贴板内容",
                   "py_func": "python函数",
                   "color_pick": "屏幕取色",
                   # DrissionPage 可视化网页自动化（dp_actors.py）
                   "dp_browser": "打开浏览器", "dp_element": "元素操作",
                   "dp_tab": "切换标签", "dp_listen": "监听网络数据",
                   "dp_page_shot": "网页截图", "dp_ele_shot": "元素截图",
                   "dp_upload": "上传文件",
                   "dp_close_browser": "关闭浏览器",
                   "if": "条件判断", "elseif": "否则如果",
                   "else": "否则", "endif": "条件结束",
                   "foreach": "Foreach 循环", "endForeach": "Foreach 循环结束",
                   "while": "while 循环", "endWhile": "while 循环结束",
                   "break": "break 中断循环", "continue": "continue 继续循环",
                   "exit": "退出流程"}

# 自动成对生成的步骤类型：不显示在模块面板（endif/endForeach/endWhile
# 分别随 if/foreach/while 拖入时自动创建）。
# 模块面板只展示「可拖拽」的类型，其余类型在编辑器中由程序自动补全。
AUTO_STEP_TYPES = {"endif", "endForeach", "endWhile"}

# 结构/控制流标记步骤：name 是派生显示名（用户不可自定义，也没有自定义入口），
# 加载/构造时强制刷新为当前 FLOW_STEP_TYPES 的显示名。这样显示名升级时（例如
# 「循环」→「Foreach 循环」）旧流程文件里残留的旧 name 会被自动纠正，保证
# 列表、删除确认、执行日志等所有用到 name 的位置显示一致。
STRUCTURAL_STEP_TYPES = {
    "if", "elseif", "else", "endif",
    "foreach", "endForeach", "while", "endWhile",
    "break", "continue",
}

# 变量类型：值 -> 显示名
VARIABLE_TYPES = {"string": "字符串", "integer": "整数", "float": "浮点数",
                  "bool": "布尔型", "list": "列表", "dict": "字典"}


# web 步骤（打开关闭网页或浏览器）的子动作：值 -> 显示名
WEB_ACTIONS = {"open": "打开网址", "close_tab": "关闭标签页", "close_browser": "关闭浏览器"}

# 网络请求步骤：默认 User-Agent（Chrome 桌面版）
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/112.0.0.0 Safari/537.36")


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
            "default_value": "",          # 默认值文本，按 type 解析；支持表达式（$引用/运算/拼接/函数）
        }
    if step_type == "log":
        return {
            "variables": "",              # 要打印的变量名，逗号分隔；空=不打印任何变量
            "text": "",                   # 附加文本，支持 $变量名 占位
            "raw": False,                 # 原始输出：不加时间戳、不自动换行，内容原样显示
            "show_type": False,           # 显示变量的 Python 类型（如 count = 5 (int)）
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
            "attach_port": "",            # attach（接管已打开的浏览器）时的调试端口
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
    if step_type in ("if", "elseif", "while"):
        return {
            "condition": "",              # 条件表达式，如 x>=1 && y<=10（支持 &&/||/! 与比较运算）
        }
    if step_type == "foreach":
        return {
            "items": "",                  # 数据源：变量/下标/$引用/函数表达式，结果须可遍历
            "item_var": "item",           # 每轮当前元素写入的变量名
            "index_var": "index",         # 每轮下标写入的变量名（空=不写）
        }
    if step_type == "exit":
        return {
            "variable": "",               # 可选：退出流程前打印该变量的值（空=不打印）
        }
    if step_type in ("else", "endif", "endForeach", "endWhile", "break", "continue"):
        return {}                         # 结构/控制流标记步骤，无参数
    raise ValueError(f"未知步骤类型: {step_type}")

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
        if self.type == "log" and self.name == "日志输出":
            self.name = "打印输出"
        if self.type == "web" and self.name == "网页操作":
            self.name = "打开关闭网页或浏览器"
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
            if self.type == "while":
                cond = (p.get("condition") or "").strip() or "未填条件"
                if len(cond) > 28:
                    cond = cond[:27] + "…"
                return f"while 循环 {cond}"
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
            if self.type == "yolo_detect":
                model = os.path.basename(p.get("model_path") or "") or "未设模型"
                return f"目标检测 {model} → {p.get('variable') or '未指定变量'}"
            if self.type == "screenshot":
                var = p.get("variable") or ""
                if p.get("save_mode") == "choose":
                    return f"截图 → 自选保存 → {var}" if var else "截图 → 自选保存"
                return f"截图 → {var}" if var else "截图 → 默认保存"
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

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.loops = int(clamp(self.loops, 0, 9999))
        self.created_seq = max(0, int(self.created_seq or 0))


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

        if ("mail_auth_code" not in data or "capture_excluded_ids" not in data
                or "clear_log_on_run" not in data or "log_print_only" not in data
                or "flow_group_seqs" not in data or seqs_migrated):
            cfg.save()   # 旧配置自动补写 mail_auth_code / clear_log_on_run / log_print_only 等新增字段
        return cfg
