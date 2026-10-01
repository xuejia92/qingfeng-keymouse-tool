"""自动化流程页：左栏流程列表（新建/编辑只管元信息），右栏实时编排模块。

右栏即编辑器：模块面板拖入步骤列表、列表内拖拽排序、双击改参数、删除步骤，
全部实时写回所选流程并自动保存；每行右侧「▶ 执行」可单步试运行该模块；
所选流程运行中时右栏锁定（防执行中改动）。

执行方式（Flow.async_run，在「编辑流程」里设置）：
- 同步（默认）：按先后顺序排队依次执行——有流程正在运行时，新启动的同步流程
  排到队尾，等前面全部结束后自动开始，同一时刻只会有一个流程在跑；
- 异步：不排队，立即开始，可与其它流程并行执行。
排队门控与放行都在本模块（_queue / _should_queue / _drain_queue），
FlowRunner 本身仍然是一条流程一条线程，不做任何排队判断。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import uuid

from PySide6.QtCore import QItemSelectionModel, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFileDialog, QFrame, QGroupBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..conditions import (BLOCK_CLOSE_TYPES, BLOCK_OPEN_TYPES, BLOCK_PAIRS,
                          block_indent_levels, block_indices, enclosing_if,
                          enclosing_loop, match_endif, validate_block_structure)
from ..config import (ASYNC_MARK, ASYNC_TIP, AppConfig, BASE_DIR, FLOW_STEP_TYPES,
                      FLOWS_DIR, Flow, FlowStep, default_step_params, flow_from_file,
                      flow_to_dict, repair_web_pairs, safe_filename,
                      step_missing_required)
from ..flows import FlowRunner
from . import theme
from ..keymap import hotkey_display
from ..logbus import log
from .flow_dialog import (FlowMetaDialog, GroupRunDialog, ModuleButton, StepList,
                          StepRunDelegate, StepParamsDialog, _STEP_MISSING_ROLE,
                          _TYPE_ICONS)
from .widgets import (DISCLOSURE_COLLAPSED, DISCLOSURE_EXPANDED,
                set_variant)


# 模块面板分组：组 id -> (分组标题, 模块类型列表)。顺序即面板显示顺序，
# 组 id 同时用于持久化收起状态（config.json 的 collapsed_module_groups）。
# 注意：endif 是随 if 自动生成的结构标记（见 config.AUTO_STEP_TYPES），不出现在面板。
MODULE_GROUPS = [
    # 「常用」放模块面板第一个：日常最常用的模块（语音播报/变量/延时/打印/状态日志/
    # 剪贴板/通知/退出）一眼可见，不必滚动。
    ("logic",    "常用",       ["speech", "var", "wait", "log", "status_log",
                                "clip_set", "clip_get", "notify", "exit"]),
    # 「应用与功能」紧跟常用之后（应用打开/关闭、网页、网络请求、DeepSeek、脚本、邮件、
    # 图片悬浮、定时关机）。
    ("app_web",  "应用与功能", ["app", "close_app", "web",
                                "http_request", "deepseek", "script", "qq_mail",
                                "float_image", "shutdown"]),
    ("input",    "键鼠操作",   ["click", "press", "find"]),
    ("perceive", "目标识别",   ["ocr", "shot_translate", "text_find", "wait_text", "screenshot",
                                "manual_shot", "find_image",
                                "wait_image", "yolo_detect", "color_pick"]),
    # DrissionPage 可视化网页自动化（dp_actors.py）：浏览器对象管理 + 元素操作 +
    # 标签切换 / 监听网络 / 页面截图 / 元素截图 / 上传文件 / 按变量关闭浏览器，
    # 串成一条可视化链路。
    ("drission", "DrissionPage", ["dp_browser", "dp_element", "dp_tab", "dp_listen",
                                   "dp_page_shot", "dp_ele_shot", "dp_upload",
                                   "dp_close_browser"]),
    ("condition", "条件分支",  ["if", "elseif", "else", "for", "foreach", "while",
                                "break", "continue"]),
    ("python",   "python",     ["py_func"]),
]

# 步骤列表里条件块内每深一层的缩进前缀（2 个全角空格：宽度稳定、一眼可辨层级）。
INDENT_UNIT = "　　"

# 分支头类型：可单独删除，删除只影响该分支头本身，不牵连同块的 if / endif
# 与其它分支（删除 if / endif 仍按整块删除，见 FlowTab._del_step）。
BRANCH_TYPES = ("elseif", "else")

# 排队中条目的悬停提示（左栏条目 / 运行按钮共用同一口径，避免两处说法不一致）
QUEUED_TIP = "排队中：等前面的流程结束后自动开始；再点一次「运行/停止」可取消排队"
# 异步流程的标记/提示文案在 config（ASYNC_MARK / ASYNC_TIP），左栏与分组编辑页共用

# 左栏两种行的行高与流程条目前缀（2026-10-01）。
# 用户反馈「分组列表和流程列表看起来一样」：原来两者都是 47px 的等高行、底色又都很淡。
# 现在靠**高度 + 形状 + 缩进**三层区分：
#   分组 = 通栏色带 + 左侧主色竖条 + 加粗主色标题（GROUP_ROW_H）
#   流程 = 白底 + 缩进 + 圆点前缀（FLOW_ROW_H）
# 行高必须**逐条 setSizeHint**：QSS 的 `QTreeWidget::item{height}` 对两种行一视同仁，
# 光靠样式分不开（实测 setSizeHint 可以既抬高也压矮 QSS 定下的 46px）。
# 2026-10-01 第二轮：用户要求「节约空间」→ 两档都压矮（50/36 → 36/28），字号整体降 1pt
# （分组名 10→9pt、热键 8→7pt、条目 10→9pt），并去掉分组里的流程计数徽标。
GROUP_ROW_H = 36
FLOW_ROW_H = 28
# 中间「要执行的模块」列表的行高：比左栏流程条目大一档（2026-10-01 用户要求
# 「行高稍微大一点、字体稍微大一点，紧凑一点」）——它是真正要执行的主体，
# 可读性优先；字号同步用 10pt（比左栏的 9pt 大一档）。
STEP_ROW_H = 30
# 分组头右侧两个小按钮（编辑分组 ⚙ / 新建流程 ＋）的固定尺寸：紧凑 + 贴右
GROUP_BTN_W = 22
GROUP_BTN_H = 20
# 流程条目前缀：用**文字**而不是图标，这样它跟着条目字色走——主题一切换自动变色，
# 不必为每套主题重画一遍 pixmap（图标是死色，深色主题下会留一个浅色圆点）。
FLOW_BULLET = "•  "

# 结束标记 -> 起始块反向映射；以及整块删除时按起始块类型展示的块名 / 提示文案。
_CLOSE_TO_OPEN = {v: k for k, v in BLOCK_PAIRS.items()}

# 模块面板的展开/收起指示符（2026-10-01）。
# 原来标题用 ASCII 的 `^`/字母 `v`（用户嫌难看），分组头用 `▾`/`▸`——而这两个字
# **微软雅黑粗体里没有字形**（分组头正是 bold 9pt），实测渲染成方框。字形统一走
# `widgets.DISCLOSURE_*`（雅黑全字重都有）。
# 全面板**只用一条规则**：`▼` = 展开着，`▲` = 收起了 —— 分组头和面板标题一致
# （标题原先是 `^`/`v`，那是"点击会做什么"的反向语义，和分组头正好相反；这里统一成"当前状态"，
#  点击行为不变，靠 tooltip 说明）。

# 撤销栈每个流程最多保留多少步（步骤对象很小，50 步足够日常回退，也不会攒内存）
MAX_UNDO_STEPS = 50
_BLOCK_KIND = {"if": "条件块", "foreach": "Foreach 循环块", "for": "for 循环块",
               "while": "while 循环块"}
_BLOCK_DEL_MSG = {
    "if": "已删除整个 if 条件块（含 endif 与分支）",
    "foreach": "已删除整个 Foreach 循环块（含结束标记与循环体内步骤）",
    "for": "已删除整个 for 循环块（含结束标记与循环体内步骤）",
    "while": "已删除整个 while 循环块（含结束标记与循环体内步骤）",
}


def _clone_flow(flow: Flow) -> Flow:
    """流程深拷贝，交给后台执行线程。

    为什么不能直接 dataclasses.replace(flow)：它只做浅拷贝，flow.steps 这个列表
    对象仍与界面共享。现在是靠「运行中锁定右栏编辑」的约定在保护，属于约定而非
    机制；一旦将来出现任何后台改动流程的路径（在线同步、定时触发、导入覆盖），
    界面动一步，正在跑的流程就跟着变了。这里深拷贝一次，把边界钉死。
    """
    return copy.deepcopy(flow)


class FlowTab(QWidget):
    changed = Signal()               # 流程配置增删改
    stepsChanged = Signal()          # 仅步骤顺序/内容变化：只需落盘，不必刷新其它页
    runningStateChanged = Signal()   # 任一流程运行状态变化
    flowStarted = Signal(bool)       # 有流程开始运行（含单步执行）；bool = 该流程是否含「状态日志」模块
                                 # 主窗口据此决定状态日志浮层显隐（没有就隐藏，避免空框）

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._flows = cfg.flows
        # _runners 只登记「正在运行」的 runner：流程一结束就摘掉。
        # 原来只增不减，删掉流程后条目还留着，反复增删会让它一直变大。
        self._runners: dict[str, FlowRunner] = {}
        # 同步流程的排队队列（flow_id，FIFO）：有流程在跑时，新启动的同步流程排到
        # 队尾，等前面全部结束后由 _drain_queue 自动放行。异步流程从不进这个队列。
        self._queue: list[str] = []
        self._draining = False   # 放行过程中防止重入（_launch_flow 会同步回调 _on_state）
        self._single_rows: dict[str, int] = {}   # flow_id -> 单步执行中的真实步骤行号
        # 静默触发的流程 id（定时任务等无人值守触发）：结束失败时不弹模态框，
        # 只走状态栏提示 + 日志，避免自动任务反复打断用户。
        self._silent: set[str] = set()
        # 最近一次「批量启动」的执行来源，供左上角悬浮窗区分展示：
        #   ("group_async", 分组名) / ("group_all", 分组名) / ("single", flow_id) / None
        # 单个流程用 toggle_flow / 单步执行时会设成 single；分组热键/整组按钮设成 group_*。
        # 仅在「当前仍有流程在运行」时有效，全部结束即归 None（见 _on_state）。
        self._run_source: tuple[str, str] | None = None
        # 「停止中又被点了运行」的流程 id：线程收尾期间用户再按运行/热键，
        # 说明他想让它跑起来，收尾完成后自动启动（见 _on_state 末尾）。
        self._restart_pending: set[str] = set()
        # 步骤复制/粘贴（Ctrl+C / Ctrl+V）用的内部剪贴板：存深拷贝的步骤对象列表。
        # 用内部存储而不是系统剪贴板：步骤是带参数的结构化对象，走文本剪贴板要序列化/反序列化，
        # 还要处理别的程序放进来的文本，得不偿失。
        self._step_clipboard: list[FlowStep] = []
        # 撤销栈（Ctrl+Z，按流程 id 分开存）：每次真正改动步骤的操作前压一份深拷贝快照。
        # 用「操作前后比较」决定是否入栈（见 _undoable），所以"点了删除又取消"这类
        # 没造成变化的操作不会污染历史。
        self._undo: dict[str, list[list[FlowStep]]] = {}
        self._capture_step_dlg = None
        self._ratio_applied = False
        self._searching = False        # 模块面板搜索中：此时收起/展开分组不落盘
        self._build_ui()
        self.refresh_list()

    # ---------- UI ----------
    def _build_ui(self):
        self.setObjectName("flowTab")
        self.setStyleSheet("""
            QWidget#flowTab { background: #f7f9fb; }
            QWidget#flowTab QListWidget {
                font-size: 9pt; outline: none;
                border: 1px solid #d8dee4; border-radius: 6px;
                background: white;
            }
            QWidget#flowTab QListWidget::item { padding: 4px 8px; }
            QWidget#flowTab QListWidget::item:hover { background-color: #e8f1fa; }
            QWidget#flowTab QListWidget::item:selected {
                background-color: #1668a8; color: white;
            }
            QWidget#flowTab QTreeWidget#flowList {
                font-size: 9pt; outline: none;
                border: 1px solid #d8dee4; border-radius: 6px;
                background: white;
            }
            QWidget#flowTab QTreeWidget#flowList::item {
                /* 左右不留 padding：item 的内边距会把 setItemWidget 的分组色带往里挤，
                   色带就贴不到左右边（实测两侧各缩 6px，段标题的通栏感没了）。
                   缩进交给 setIndentation，上下留白交给 setSizeHint 的行高。 */
                padding: 0px;
                border-bottom: 1px solid #f2f5f8;
            }
            QWidget#flowTab QTreeWidget#flowList::item:hover { background-color: #f2f5f8; }
            /* 选中：**最浅的蓝色令牌 + 主色字**（原来整条实心主色 #1668a8 + 白字，太重）。
               用 primary_soft（浅色主题 #e8f1fa）而不是 pressed_bg(#e3edf7)：
               ⚠️ 浅色主题下 sel_bg 与 pressed_bg **是同一个值 #e3edf7**，分组色带正是 sel_bg，
               选 pressed_bg 会和色带**完全同色**；primary_soft 比它再浅一档，至少有个层次。 */
            QWidget#flowTab QTreeWidget#flowList::item:selected {
                background-color: #e8f1fa; color: #1668a8;
            }
            QWidget#flowTab QTreeWidget#flowList::branch { background: transparent; }
            /* ===== 左栏两种行：分组是"段标题"，流程是"条目"（2026-10-01）=====
               用户反馈「分组列表和流程列表看起来一样」——原来两者都是 47px 等高行、
               底色又都很淡。现在：分组 = 通栏色带 + 左侧主色竖条 + 加粗主色；
               流程 = 白底 + 缩进 + 圆点前缀。行高逐条 setSizeHint（见 GROUP_ROW_H）。 */
            /* 分组头：**整条通栏**（含右侧按钮区）都吃这层底色。原来做成居中的小圆角
               药丸，形状和流程条目撞脸；通栏色带 + 左侧竖条才像一段的分隔标题。 */
            QWidget#flowTab QTreeWidget#flowList QWidget#groupHeaderBox {
                background: #e9f0f8;
                border-left: 3px solid #1668a8;    /* 左侧主色竖条：段标题的锚点 */
                border-top: 1px solid #e1e4e8;
                border-bottom: 1px solid #d8dee4;
            }
            QWidget#flowTab QTreeWidget#flowList QWidget#groupHeaderBox:hover {
                background: #dce8f4;
            }
            /* 分组名的可点击按钮：加粗、主色（第二行是更小更淡的热键） */
            QWidget#flowTab QTreeWidget#flowList QPushButton[groupHeader="true"] {
                text-align: left; padding: 0px 6px;
                font-size: 9pt; font-weight: 700; color: #1668a8;
                border: none; background: transparent;
            }
            QWidget#flowTab QTreeWidget#flowList QLabel#groupHotkey {
                color: #57606a; font-size: 7pt;
            }
            /* 分组头右侧的 ⚙ / ＋ 小按钮：紧凑、贴右，别占地方 */
            QWidget#flowTab QTreeWidget#flowList QPushButton {
                text-align: center; padding: 0px;
                font-size: 9pt; color: #1668a8;
                border: none; border-radius: 4px; background: transparent;
            }
            QWidget#flowTab QTreeWidget#flowList QPushButton:hover { background: #e9f0f8; }
            /* 步骤行：行高比左栏条目大一档（= STEP_ROW_H，有测试钉住两处一致），
               字号 10pt（见 step_list 自己的样式表）。
               上下 padding 必须显式归零——QSS 的 `height` 是**内容高**，
               通用规则 `QListWidget::item{padding:4px 8px}` 会再加 8px 上去
               （原来 38+8=44px）。横向留 8px 不贴边。 */
            QWidget#flowTab QListWidget#stepView::item {
                height: 30px; padding: 0px 8px;
            }
            QWidget#flowTab QGroupBox {
                border: 1px solid #d8dee4; border-radius: 6px;
                background: white; margin-top: 0px; font-weight: 600;
            }
            QWidget#flowTab QGroupBox::title { subcontrol-origin: margin; left: 10px; }
            /* ===== 右栏模块面板：行高/字号与左栏**逐项对齐**（2026-10-01）=====
               左栏分组行 = GROUP_ROW_H、流程行 = FLOW_ROW_H、字号 9pt；
               这里的分组头与模块按钮用同一组常量和同一档字号，整页看起来才是一个节奏。
               高度在构造处 setFixedHeight（QSS 只管字号/内边距/配色）。 */
            QWidget#flowTab QGroupBox#modulePanel QPushButton {
                text-align: left; padding: 0px 10px;
                font-size: 9pt; border: 1px solid #d8dee4;
                border-radius: 6px; background: white; color: #24292f;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton:hover {
                border-color: #1668a8; color: #1668a8; background: #f3f8fd;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton:disabled {
                color: #aab2bb; background: #f2f4f6; border-color: #e1e4e8;
            }
            /* 模块分组标题：浅灰蓝底与按钮区区分，左对齐蓝色加粗，点击即收起/展开。
               选择器必须比上方 QGroupBox#modulePanel QPushButton 更具体（含 id+属性），
               否则 modulePanel 的 padding/背景会压过本规则导致样式失效。
               字号/字重与左栏的分组名一致（9pt / 700）。 */
            QWidget#flowTab QGroupBox#modulePanel QPushButton[groupHeader="true"] {
                text-align: left; padding: 0px 10px;
                font-size: 9pt; font-weight: 700; color: #1668a8;
                border: none; border-radius: 6px; background: #e9f0f8;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton[groupHeader="true"]:hover {
                background: #dce8f4;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton[groupHeader="true"]:disabled {
                color: #aab2bb; background: #f2f4f6;
            }
            /* 面板标题（可点击文字按钮）：点击一键收起/展开全部分组 */
            QWidget#flowTab QPushButton#panelTitleBtn {
                text-align: left; padding: 2px 6px;
                font-size: 10pt; font-weight: 600; color: #1668a8;
                border: none; border-radius: 4px; background: transparent;
            }
            QWidget#flowTab QPushButton#panelTitleBtn:hover {
                background: #e9f0f8;
            }
            QWidget#flowTab QPushButton#panelTitleBtn:disabled {
                color: #aab2bb; background: transparent;
            }
            QWidget#flowTab QScrollArea#moduleScroll {
                background: transparent; border: none;
            }
            /* 模块搜索输入框与清空按钮（面板底部）。清空按钮选择器必须带上
               modulePanel 前缀 + id，否则被上方更具体的 QGroupBox#modulePanel QPushButton 压过。 */
            QWidget#flowTab QLineEdit#moduleSearch {
                border: 1px solid #d8dee4; border-radius: 6px;
                padding: 5px 8px; font-size: 10pt; background: white; color: #24292f;
            }
            QWidget#flowTab QLineEdit#moduleSearch:focus { border-color: #1668a8; }
            QWidget#flowTab QGroupBox#modulePanel QPushButton#moduleSearchClear {
                text-align: center; padding: 4px 10px;
                font-size: 10pt; border: 1px solid #d8dee4;
                border-radius: 6px; background: white; color: #1668a8;
            }
            QWidget#flowTab QGroupBox#modulePanel QPushButton#moduleSearchClear:hover {
                border-color: #1668a8; background: #f3f8fd;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        # ===== 左栏：流程列表（按分组组织，右键菜单管理流程） =====
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(6)
        lbar1 = QHBoxLayout()
        lbar1.setSpacing(4)
        self.new_group_btn = QPushButton("➕ 添加分组")
        self.new_group_btn.setToolTip("新建流程分组（可在分组下添加流程）")
        set_variant(self.new_group_btn, "primary")
        lbar1.addWidget(self.new_group_btn, 1)
        llay.addLayout(lbar1)
        self.list = QTreeWidget()
        self.list.setObjectName("flowList")
        self.list.setHeaderHidden(True)
        self.list.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.list.setRootIsDecorated(False)      # 分组头自带按钮，不需要系统展开箭头
        self.list.setIndentation(22)              # 流程条目明显缩进，压在分组色带之下
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._flow_context_menu)
        llay.addWidget(self.list, 1)
        left.setMinimumWidth(200)
        splitter.addWidget(left)

        # ===== 中栏：步骤编排（实时编辑） =====
        center = QWidget()
        clay = QVBoxLayout(center)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(6)

        self.right_title = QLabel("流程模块（选中左侧流程后编排）")
        self.right_title.setStyleSheet(
            "font-size: 12pt; font-weight: 600; color: #24292f; padding: 2px;")
        clay.addWidget(self.right_title)

        self.step_list = StepList()
        self.step_list.setObjectName("stepView")
        # 中间步骤列表（真正要执行的模块）：行高与字号比左栏条目各大一档，
        # 可读性优先；左右只留 8px、上下不留 padding（垂直留白交给行高），保持紧凑。
        # ⚠️ 行高值与 STEP_ROW_H 常量、以及 _build_ui 里那条同名 QSS 三处必须一致
        #（QSS 是普通字符串，写死数值；测试按 STEP_ROW_H 钉两处出现次数）。
        self.step_list.setStyleSheet(
            "QWidget#flowTab QListWidget#stepView { font-size: 10pt; }"
            "QWidget#flowTab QListWidget#stepView::item"
            " { height: 30px; padding: 0px 8px; }")
        self.step_list.setItemDelegate(StepRunDelegate(self.step_list))
        self.step_list.stepDropped.connect(self._on_step_dropped)
        self.step_list.orderChanged.connect(self._on_order_changed)
        self.step_list.stepRunRequested.connect(self._run_single_step)
        # 列表内 Ctrl+C / Ctrl+V：复制、粘贴步骤（支持多选）
        self.step_list.copyRequested.connect(self._copy_steps)
        self.step_list.pasteRequested.connect(self._paste_steps)
        # 列表内 Ctrl+Z：撤销上一步步骤改动（可连续撤销）
        self.step_list.undoRequested.connect(self._undo_steps)
        self.step_list.itemDoubleClicked.connect(lambda *_: self._edit_step_param())
        self.step_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.step_list.customContextMenuRequested.connect(self._step_context_menu)
        clay.addWidget(self.step_list, 1)

        sbtn_row = QHBoxLayout()
        self.run_btn = QPushButton("▶ 运行/停止")
        self.run_btn.setToolTip("运行/停止选中流程")
        self.step_edit_btn = QPushButton("✎ 编辑参数")
        self.step_edit_btn.clicked.connect(self._edit_step_param)
        set_variant(self.step_edit_btn, "primary")
        self.step_del_btn = QPushButton("🗑 删除步骤")
        self.step_del_btn.clicked.connect(self._del_step)
        self.step_del_btn.setToolTip("删除选中的步骤；列表里按住 Ctrl 可多选、Shift 选一段，"
                                     "然后一次删掉多个")
        set_variant(self.step_del_btn, "danger")
        sbtn_row.addWidget(self.run_btn)
        sbtn_row.addWidget(self.step_edit_btn)
        sbtn_row.addWidget(self.step_del_btn)
        sbtn_row.addStretch(1)
        clay.addLayout(sbtn_row)
        splitter.addWidget(center)

        # ===== 右栏：模块面板（按功能分组，点击分组标题收起/展开） =====
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(2)

        # 面板标题：可点击文字（点击一键收起/展开全部分组），文字后带 ^ 符号
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(0)
        self.panel_title_btn = QPushButton(f"模块面板 {DISCLOSURE_COLLAPSED}")
        self.panel_title_btn.setObjectName("panelTitleBtn")
        self.panel_title_btn.setToolTip("点击一键收起/展开所有模块分组")
        self.panel_title_btn.setCursor(Qt.PointingHandCursor)
        self.panel_title_btn.clicked.connect(self._toggle_collapse_all)
        btn_row.addWidget(self.panel_title_btn)
        btn_row.addStretch(1)
        rlay.addLayout(btn_row)

        self.panel_box = QGroupBox()
        self.panel_box.setObjectName("modulePanel")
        panel = QVBoxLayout(self.panel_box)
        panel.setContentsMargins(8, 8, 8, 8)
        panel.setSpacing(2)

        # 分组内容放进滚动区：全部展开时高度不够可滚动，折叠后自动收缩
        scroll = QScrollArea()
        scroll.setObjectName("moduleScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_box = QWidget()
        scroll_lay = QVBoxLayout(scroll_box)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(2)

        self._module_btns = []                       # 全部模块按钮（锁定/解锁统一遍历）
        self._module_btn_by_type: dict[str, ModuleButton] = {}   # 步骤类型 -> 模块按钮
        self._group_headers: dict[str, QPushButton] = {}   # gid -> 分组标题按钮
        self._group_wrappers: dict[str, QWidget] = {}      # gid -> 模块按钮容器
        self._group_titles = {gid: title for gid, title, _ in MODULE_GROUPS}
        collapsed = self._module_collapsed_set()
        for gid, title, types in MODULE_GROUPS:
            header = QPushButton(
                f"{DISCLOSURE_EXPANDED} {title}" if gid not in collapsed
                else f"{DISCLOSURE_COLLAPSED} {title}")
            header.setProperty("groupHeader", True)
            header.setCursor(Qt.PointingHandCursor)
            header.setToolTip("点击收起/展开")
            # 行高与左栏分组行一致（GROUP_ROW_H）：整页的分组/条目节奏要一样
            header.setFixedHeight(GROUP_ROW_H)
            header.setCheckable(True)
            header.setChecked(gid not in collapsed)
            header.toggled.connect(lambda on, g=gid: self._on_group_toggled(g, on))
            self._group_headers[gid] = header
            scroll_lay.addWidget(header)

            wrapper = QWidget()
            # 不让分组容器被布局拉高：展开时组内按钮紧凑排列，不留空白
            wrapper.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            # ⚠️ 模块按钮的字号**必须写在容器自己的样式表里**（2026-10-01 实测）：
            # 面板顶层的 `QWidget#flowTab QGroupBox#modulePanel QPushButton` 规则对
            # ModuleButton **只生效背景/边框/内边距，字号完全不生效**——按钮沿用应用默认
            # 字体（10pt），所以一直比左栏（9pt）显得大。而同样是 QPushButton 的分组头
            # （带 [groupHeader="true"] 那条）却正常，`setFont()` 也无效（用完 font() 仍是 10pt）。
            # 逐条实测过：容器级 QSS 生效 ✓、逐按钮 QSS 生效 ✓、setFont ✗。
            # 选容器级：7 个样式表就够，不用给 60 多个按钮各挂一份。
            wrapper.setStyleSheet("QPushButton { font-size: 9pt; }")
            wrap_lay = QVBoxLayout(wrapper)
            wrap_lay.setContentsMargins(0, 0, 0, 0)
            # 行间距与左栏一致：左栏流程行是紧挨着的（pitch = FLOW_ROW_H），
            # 这里原来留 3px，pitch 变成 31，看起来就比左栏"松"（用户反馈紧凑一点）。
            wrap_lay.setSpacing(1)
            for t in types:
                label = FLOW_STEP_TYPES[t]
                btn = ModuleButton(t, label)
                # 行高与左栏流程行一致（FLOW_ROW_H）：整页的分组/条目节奏要一样
                btn.setFixedHeight(FLOW_ROW_H)
                if t == "web":
                    btn.setToolTip("拖入添加「打开网址」步骤（浏览器保持打开）；"
                                   "需要关闭浏览器时，双击该步骤在「操作」里选择「关闭浏览器」")
                self._module_btns.append(btn)
                self._module_btn_by_type[t] = btn
                wrap_lay.addWidget(btn)
            wrapper.setVisible(gid not in collapsed)
            self._group_wrappers[gid] = wrapper
            scroll_lay.addWidget(wrapper)

        # 无结果提示：搜索不到模块时显示，平时隐藏
        self._no_result_label = QLabel("未找到匹配模块")
        self._no_result_label.setObjectName("moduleNoResult")
        self._no_result_label.setAlignment(Qt.AlignCenter)
        self._no_result_label.setStyleSheet("color: #8899aa; padding: 14px 4px;")
        self._no_result_label.setVisible(False)
        scroll_lay.addWidget(self._no_result_label)

        scroll_lay.addStretch(1)   # 多余空间收到底部：全收起时标题紧凑，全展开时按钮顶对齐
        self._update_panel_title()   # 初始全收起时标题符号为 ▼（可点击展开全部）
        scroll.setWidget(scroll_box)
        panel.addWidget(scroll, 1)

        # 底部搜索框 + 清空按钮：实时按关键词过滤模块并展开命中分组
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 6, 0, 0)
        search_row.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("moduleSearch")
        self.search_edit.setPlaceholderText("搜索模块…")
        self.search_edit.setClearButtonEnabled(False)
        self.search_edit.textChanged.connect(self._on_module_search_changed)
        search_row.addWidget(self.search_edit, 1)
        self.search_clear_btn = QPushButton("清空")
        self.search_clear_btn.setObjectName("moduleSearchClear")
        self.search_clear_btn.setCursor(Qt.PointingHandCursor)
        self.search_clear_btn.setToolTip("清空搜索并恢复显示所有模块")
        self.search_clear_btn.clicked.connect(self._clear_module_search)
        search_row.addWidget(self.search_clear_btn)
        panel.addLayout(search_row)

        rlay.addWidget(self.panel_box, 1)
        right.setMinimumWidth(180)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([210, 600, 180])
        self._splitter = splitter
        root.addWidget(splitter, 1)

        # ---- 信号 ----
        self.new_group_btn.clicked.connect(self._add_group)
        self.run_btn.clicked.connect(self._toggle_selected)
        self.list.itemDoubleClicked.connect(lambda *_: self._on_flow_double_clicked())
        self.list.itemSelectionChanged.connect(self.refresh_steps_view)

    def showEvent(self, ev):
        """首次显示时按比例应用三栏宽度（构造期 setSizes 会被布局覆盖）。"""
        super().showEvent(ev)
        if not self._ratio_applied and self.isVisible():
            self._ratio_applied = True
            total = sum(self._splitter.sizes()) or 1
            self._splitter.setSizes([int(total * 0.22), int(total * 0.62),
                                     int(total * 0.16)])

    # ---------- 左栏列表（分组树） ----------
    def refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        collapsed = set(self.cfg.collapsed_flow_groups)
        # 分组顺序：flow_groups 定义的分组在前，「未分组」兜底放最后
        groups = [g for g in self.cfg.flow_groups if g.strip()]
        by_group: dict[str, list[Flow]] = {g: [] for g in groups}
        for f in self._flows:
            g = f.group if f.group in by_group else ""
            by_group.setdefault(g, []).append(f)
        for g in groups + [""]:
            gitem = QTreeWidgetItem()
            gitem.setData(0, Qt.UserRole, ("group", g))
            gitem.setFlags(Qt.ItemIsEnabled)          # 分组头不可选中
            # 行高逐条设：分组明显比流程高，两者一眼分得开（QSS 分不开，见 GROUP_ROW_H）
            gitem.setSizeHint(0, QSize(0, GROUP_ROW_H))
            self.list.addTopLevelItem(gitem)
            expanded = g not in collapsed
            self.list.setItemWidget(gitem, 0, self._group_header_widget(g, expanded))
            for f in by_group.get(g, []):
                citem = QTreeWidgetItem([self._flow_row_text(f)])
                citem.setData(0, Qt.UserRole, ("flow", f.id))
                citem.setSizeHint(0, QSize(0, FLOW_ROW_H))
                runner = self._runners.get(f.id)
                if runner and runner.is_running:
                    citem.setForeground(0, QColor(theme.token("run_marker")))
                elif f.id in self._queue:
                    citem.setForeground(0, QColor(theme.token("primary")))
                    citem.setToolTip(0, QUEUED_TIP)
                elif f.async_run:
                    citem.setToolTip(0, ASYNC_TIP)
                gitem.addChild(citem)
            gitem.setExpanded(expanded)
        self.list.blockSignals(False)
        self._restore_selection()
        self.refresh_steps_view()

    def _restore_selection(self):
        """重建后恢复选中：优先运行中的流程；否则选**最上面分组下的第一个流程**。

        按左栏显示顺序（分组顺序 + 组内顺序）取，而不是 _flows 列表顺序——
        分组置顶/排序后两者会不一致，而用户期望打开软件后列表第一条就是选中的。
        """
        for f in self._flows:
            runner = self._runners.get(f.id)
            if runner and runner.is_running:
                self._select_flow_item(f.id)
                return
        first = self._first_visible_flow()
        if first is not None:
            self._select_flow_item(first.id)

    def _select_flow_item(self, flow_id: str):
        item = self._flow_item(flow_id)
        if item is not None:
            self.list.setCurrentItem(item)

    def _group_item(self, g: str) -> QTreeWidgetItem | None:
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            if it.data(0, Qt.UserRole) == ("group", g):
                return it
        return None

    def _flow_item(self, flow_id: str) -> QTreeWidgetItem | None:
        for i in range(self.list.topLevelItemCount()):
            g = self.list.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                if c.data(0, Qt.UserRole) == ("flow", flow_id):
                    return c
        return None

    def _first_visible_flow(self) -> Flow | None:
        """左栏从上往下第一个流程（空分组自动跳过）。"""
        for i in range(self.list.topLevelItemCount()):
            gitem = self.list.topLevelItem(i)
            for j in range(gitem.childCount()):
                data = gitem.child(j).data(0, Qt.UserRole)
                if data and data[0] == "flow":
                    return self._flow_by_id(data[1])
        return None

    # ---------- 分组热键（运行本组全部流程） ----------
    def group_hotkey(self, g: str) -> str:
        """该分组的运行热键；未设置或「未分组」返回空串。"""
        if not g:
            return ""
        return str((self.cfg.group_hotkeys or {}).get(g) or "")

    def set_group_hotkey(self, g: str, hotkey: str) -> None:
        """写入/清除某分组的运行热键（分组编辑页调用；空值 = 清除该分组的热键）。

        立即刷新左栏（标题括号里要显示新热键）并通知主窗口重注册热键 + 防抖保存。
        """
        if not g:
            return
        if self.cfg.group_hotkeys is None:
            self.cfg.group_hotkeys = {}
        hk = str(hotkey or "").strip().lower()
        if hk:
            self.cfg.group_hotkeys[g] = hk
        else:
            self.cfg.group_hotkeys.pop(g, None)
        self.refresh_list()
        self.changed.emit()

    def _group_title_text(self, g: str, expanded: bool) -> str:
        """分组标题**首行**：展开符 + 分组名（热键另起一行小字，见 _group_hotkey_text）。"""
        arrow = DISCLOSURE_EXPANDED if expanded else DISCLOSURE_COLLAPSED
        return f"{arrow} {g if g else '未分组'}"

    def _group_hotkey_text(self, g: str) -> str:
        """分组标题**第二行**的小字热键；未设置时返回空串（那一行就不显示）。"""
        hk = self.group_hotkey(g)
        return hotkey_display(hk) if hk else ""

    def _group_header_widget(self, g: str, expanded: bool) -> QWidget:
        """分组头：通栏色带 + 标题（+ 第二行小字热键）+ 右侧贴边的 ⚙ / ＋。

        底色挂在**最外层**（objectName=groupHeaderBox），所以整行从左到右（含右侧按钮区）
        都是同一条色带——这是"分组"与"流程条目"最主要的形状差别（2026-10-01）。
        热键放在分组名**下面一行、字号更小**：跟在名字后缀里会让长分组名把它挤没，
        也不符合"分组名下面显示快捷键"的读法（2026-09-22 用户要求）。

        右侧按钮固定尺寸并靠右：`text_col` 占满剩余宽度，两个按钮自然被推到最右，
        省下来的横向空间留给分组名（2026-10-01 第二轮：用户要求紧凑、按钮靠右）。
        """
        name = g if g else "未分组"
        w = QWidget()
        w.setObjectName("groupHeaderBox")     # 通栏底色 + 左边竖条都在这一层
        w.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 1, 4, 1)
        h.setSpacing(2)

        text_col = QVBoxLayout()              # 左列：标题 + 热键，上下两行
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)

        title = QPushButton(self._group_title_text(g, expanded))
        title.setObjectName("groupTitle")
        title.setProperty("groupHeader", True)
        title.setCursor(Qt.PointingHandCursor)
        title.setToolTip("点击展开/收起分组")
        title.setContextMenuPolicy(Qt.CustomContextMenu)
        title.customContextMenuRequested.connect(
            lambda pos, g=g: self._group_context_menu(g, title.mapToGlobal(pos)))
        title.clicked.connect(lambda _, g=g: self._toggle_group(g))
        text_col.addWidget(title)

        hk_text = self._group_hotkey_text(g)
        if hk_text:                          # 没设热键就不占那一行
            hk_label = QLabel(hk_text)
            hk_label.setObjectName("groupHotkey")
            hk_label.setContentsMargins(14, 0, 6, 0)   # 与首行文字左对齐（让开展开符）
            hk_label.setToolTip("分组热键：按一下运行本组异步流程，再按一下停止本组异步流程")
            text_col.addWidget(hk_label)
        h.addLayout(text_col, 1)             # 占满剩余宽度 → 把按钮顶到最右

        if g:      # 「未分组」不是可编辑分组：不给编辑入口
            edit = QPushButton("⚙")
            edit.setCursor(Qt.PointingHandCursor)
            edit.setToolTip(f"编辑「{name}」分组：查看本组流程、整组运行、设置本组热键")
            edit.clicked.connect(lambda _, g=g: self.open_group_dialog(g))
            edit.setFixedSize(GROUP_BTN_W, GROUP_BTN_H)
            h.addWidget(edit)
        plus = QPushButton("＋")
        plus.setCursor(Qt.PointingHandCursor)
        plus.setToolTip(f"在「{name}」分组下新建流程")
        plus.clicked.connect(lambda _, g=g: self._new_flow(g))
        plus.setFixedSize(GROUP_BTN_W, GROUP_BTN_H)
        h.addWidget(plus)
        return w

    def _toggle_group(self, g: str):
        """点击分组头：切换展开/收起，并把状态持久化到 config。"""
        item = self._group_item(g)
        if item is None:
            return
        expanded = not item.isExpanded()
        item.setExpanded(expanded)
        header = self.list.itemWidget(item, 0)
        if header is not None:
            btn = header.findChild(QPushButton, "groupTitle")
            if btn is not None:
                btn.setText(self._group_title_text(g, expanded))
        collapsed = set(self.cfg.collapsed_flow_groups)
        if expanded:
            collapsed.discard(g)
        else:
            collapsed.add(g)
        self.cfg.collapsed_flow_groups = sorted(collapsed)
        self.cfg.save()

    def _flow_item_text(self, f: Flow, failed: bool = False) -> str:
        """左栏条目文本：异步流程加 ⚡、运行中加 ▶、排队中加 ⏳ 与位次。

        异步标记恒在名字最前，状态标记紧随其后；失败标注红字由调用方处理。
        """
        mark = f"{ASYNC_MARK} " if f.async_run else ""
        runner = self._runners.get(f.id)
        if runner and runner.is_running:
            return f"{mark}▶ {f.name}"
        if f.id in self._queue:
            return f"{mark}⏳ {f.name}（排队第 {self._queue.index(f.id) + 1} 位）"
        if failed:
            return f"{mark}{f.name}（上次失败）"
        return f"{mark}{f.name}"

    def _flow_row_text(self, f: Flow, failed: bool = False) -> str:
        """左栏**流程条目**的显示文本 = 圆点前缀 + `_flow_item_text`。

        圆点（FLOW_BULLET）是流程行与分组色带的第二个视觉差别（第一个是行高）；
        用文字而不是图标，颜色自动跟随条目字色，不用为每套主题重画 pixmap。
        """
        return FLOW_BULLET + self._flow_item_text(f, failed=failed)

    def _update_left_item(self, flow_id: str, failed: bool = False):
        """按流程当前状态刷新左栏对应条目。"""
        flow = next((f for f in self._flows if f.id == flow_id), None)
        item = self._flow_item(flow_id)
        if flow is None or item is None:
            return
        item.setText(0, self._flow_row_text(flow, failed=failed))
        runner = self._runners.get(flow_id)
        queued = flow_id in self._queue
        if runner and runner.is_running:
            item.setForeground(0, QColor(theme.token("run_marker")))
        elif queued:
            item.setForeground(0, QColor(theme.token("primary")))
        elif failed:
            item.setForeground(0, QColor(theme.token("danger_pressed")))
        else:
            # 回到"没有显式字色"，交回 QSS 管：否则 setForeground 会**压过**
            # `::item:selected { color: 主色 }`，选中时字色不跟随（2026-10-01）。
            item.setData(0, Qt.ForegroundRole, None)
        if queued:
            item.setToolTip(0, QUEUED_TIP)
        elif flow.async_run:
            item.setToolTip(0, ASYNC_TIP)
        else:
            item.setToolTip(0, "")

    @staticmethod
    def _loops_text(f: Flow) -> str:
        return "无限循环" if f.loops == 0 else f"{f.loops} 轮"

    def _selected_flow(self) -> Flow | None:
        item = self.list.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != "flow":
            return None
        return next((f for f in self._flows if f.id == data[1]), None)

    def _selected_running(self) -> bool:
        flow = self._selected_flow()
        if flow is None:
            return False
        runner = self._runners.get(flow.id)
        return bool(runner and runner.is_running)

    # ---------- 右栏：步骤编排 ----------
    def refresh_steps_view(self):
        self._reload_steps()

    def _toggle_collapse_all(self):
        """点击面板标题：一键收起/展开全部模块分组（状态经 toggled 信号持久化）。"""
        if self._searching:   # 搜索中不响应一键收起/展开，避免污染过滤态
            return
        all_collapsed = all(not h.isChecked() for h in self._group_headers.values())
        for header in self._group_headers.values():
            header.setChecked(all_collapsed)   # 全收起 -> 展开全部；否则收起全部
        self._update_panel_title()

    def _update_panel_title(self):
        """标题符号随状态刷新：还有分组展开着显示 ▼，全收起了显示 ▲。

        与分组头**同一条规则**（▼ = 展开着，▲ = 收起了）；点击行为不变，
        由 tooltip 说明"点一下会收起/展开全部"。
        符号一律走 `DISCLOSURE_*`，不要在别处写死字形（测试按常量比对）。
        """
        all_collapsed = all(not h.isChecked() for h in self._group_headers.values())
        arrow = (DISCLOSURE_COLLAPSED if all_collapsed
                 else DISCLOSURE_EXPANDED)
        self.panel_title_btn.setText(f"模块面板 {arrow}")

    def _on_group_toggled(self, gid: str, expanded: bool):
        """分组标题点击：切换模块按钮区显示/隐藏，并把状态持久化到 config。"""
        if self._searching:   # 搜索中分组头由过滤逻辑接管，不持久化
            return
        header = self._group_headers.get(gid)
        wrapper = self._group_wrappers.get(gid)
        if header is None or wrapper is None:
            return
        header.setText((f"{DISCLOSURE_EXPANDED} " if expanded else f"{DISCLOSURE_COLLAPSED} ")
                       + self._group_titles[gid])
        wrapper.setVisible(expanded)
        if not self.cfg.module_groups_explicit:
            # 首次手动调整：先把当前渲染态固化为记忆起点（未展开的组视为收起），
            # 之后再往下走 collapsed_module_groups 的常规增删，保证状态完整。
            self.cfg.module_groups_explicit = True
            self.cfg.collapsed_module_groups = sorted(
                g for g, h in self._group_headers.items() if not h.isChecked())
        collapsed = set(self.cfg.collapsed_module_groups)
        if expanded:
            collapsed.discard(gid)
        else:
            collapsed.add(gid)
        self.cfg.collapsed_module_groups = sorted(collapsed)
        self.cfg.save()
        self._update_panel_title()   # 单独展开/收起分组后同步刷新标题符号

    # ---------- 模块面板搜索 ----------
    def _module_collapsed_set(self) -> set[str]:
        """按配置计算当前应处于收起状态的分组集合（未手动调整过则默认全收起）。"""
        collapsed = set(self.cfg.collapsed_module_groups)
        if not self.cfg.module_groups_explicit:
            collapsed = {gid for gid, _, _ in MODULE_GROUPS}
        return collapsed

    @staticmethod
    def _module_matches(step_type: str, kw: str) -> bool:
        """模块是否命中关键词：匹配模块类型名（英文）或显示名（中文）。"""
        label = FLOW_STEP_TYPES.get(step_type, "")
        return kw in step_type.lower() or kw in label.lower()

    def _apply_module_search(self, kw: str):
        """按关键词过滤模块：仅显示命中模块，自动展开含命中模块的分组，隐藏空分组。"""
        matched_any = False
        for gid, title, types in MODULE_GROUPS:
            matched = [t for t in types if self._module_matches(t, kw)]
            header = self._group_headers[gid]
            wrapper = self._group_wrappers[gid]
            for t in types:
                self._module_btn_by_type[t].setVisible(t in matched)
            has_match = bool(matched)
            header.blockSignals(True)
            header.setText((f"{DISCLOSURE_EXPANDED} " if has_match else f"{DISCLOSURE_COLLAPSED} ")
                           + title)
            header.setChecked(has_match)
            header.setVisible(has_match)          # 空分组整体隐藏
            header.blockSignals(False)
            wrapper.setVisible(has_match)
            matched_any = matched_any or has_match
        self._no_result_label.setVisible(not matched_any)

    def _restore_module_panel(self):
        """退出搜索：恢复所有分组/模块的原始展开收起状态，隐藏无结果提示。"""
        collapsed = self._module_collapsed_set()
        for gid, title, types in MODULE_GROUPS:
            header = self._group_headers[gid]
            wrapper = self._group_wrappers[gid]
            expanded = gid not in collapsed
            header.blockSignals(True)
            header.setText((f"{DISCLOSURE_EXPANDED} " if expanded else f"{DISCLOSURE_COLLAPSED} ")
                           + title)
            header.setChecked(expanded)
            header.setVisible(True)
            header.blockSignals(False)
            wrapper.setVisible(expanded)
            for t in types:
                self._module_btn_by_type[t].setVisible(True)
        self._no_result_label.setVisible(False)
        self._update_panel_title()

    def _on_module_search_changed(self, text: str):
        """搜索框内容变化：实时过滤；空则恢复全部显示。"""
        self._searching = True
        try:
            kw = (text or "").strip().lower()
            if not kw:
                self._restore_module_panel()
            else:
                self._apply_module_search(kw)
        finally:
            self._searching = False

    def _clear_module_search(self):
        """清空搜索框并恢复所有模块与分组。"""
        if self.search_edit.text():
            self.search_edit.clear()   # 触发 textChanged("") -> _restore_module_panel
        else:
            self._restore_module_panel()

    def _reload_steps(self):
        flow = self._selected_flow()
        running = self._selected_running()
        editable = flow is not None and not running

        # 锁定/解锁编辑控件
        for b in self._module_btns:
            b.setEnabled(editable)
        for h in self._group_headers.values():
            h.setEnabled(editable)
        self.step_list.setEnabled(editable)
        self.step_edit_btn.setEnabled(editable)
        self.step_del_btn.setEnabled(editable)
        self.panel_title_btn.setEnabled(editable)
        self.panel_title_btn.setToolTip("点击一键收起/展开所有模块分组" if editable
                                        else "流程运行中，已锁定编辑")
        self._update_run_button()

    def _update_run_button(self):
        """运行按钮随选中流程状态切换文案：运行中=停止、排队中=取消排队。"""
        flow = self._selected_flow()
        runner = self._runners.get(flow.id) if flow else None
        running = bool(runner and runner.is_running)
        queued = bool(flow is not None and flow.id in self._queue)
        if running:
            self.run_btn.setText("■ 停止")
        elif queued:
            self.run_btn.setText("✖ 取消排队")
        else:
            self.run_btn.setText("▶ 运行/停止")
        set_variant(self.run_btn, "success" if not (running or queued) else "danger")
        self.run_btn.setToolTip(QUEUED_TIP if queued
                                else "运行/停止选中流程（同步流程会排队，异步流程立即并行运行）")

        self.step_list.blockSignals(True)
        self.step_list.clear()
        if flow is None:
            self.right_title.setText("流程模块（选中左侧流程后编排）")
            self.step_list.blockSignals(False)
            return
        runner = self._runners.get(flow.id)
        is_running = bool(runner and runner.is_running)
        single_row = self._single_rows.get(flow.id) if is_running else None
        if single_row is not None:
            running_idx = single_row  # 单步执行：高亮被单独运行的那一步
        else:
            running_idx = runner.current_step_index if is_running else None
        hk = f"【{hotkey_display(flow.hotkey)}】" if flow.hotkey else ""
        queued = flow.id in self._queue
        title = None
        if single_row is not None:
            step_name = (flow.steps[single_row].name
                         if 0 <= single_row < len(flow.steps) else "")
            title = f"「{flow.name}」单步执行中：{step_name}{hk}"
        elif running_idx is not None and running_idx >= 0:
            step_name = (flow.steps[running_idx].name
                         if 0 <= running_idx < len(flow.steps) else "")
            title = (f"「{flow.name}」运行中 · 步骤 "
                     f"{running_idx + 1}/{len(flow.steps)}：{step_name}{hk}")
        elif queued:
            title = (f"「{flow.name}」排队中（第 {self._queue.index(flow.id) + 1} 位）"
                     f"（{len(flow.steps)} 步 · {self._loops_text(flow)}）"
                     f"{hk} · 等前面的流程结束后自动开始")
        if title is not None:
            self.right_title.setText(title)
        else:
            self.right_title.setText(f"「{flow.name}」要执行的模块"
                                     f"（{len(flow.steps)} 步 · {self._loops_text(flow)}）{hk}")
        # 块（if/foreach/while）内的步骤按层级缩进（块骨架与结束标记对齐不缩进，
        # 体内步骤缩进一级，嵌套逐层叠加）
        levels = block_indent_levels(flow.steps)
        for i, s in enumerate(flow.steps):
            mark = "（失败继续）" if s.continue_on_fail else ""
            running = running_idx is not None and i == running_idx
            # ▶ 标记放在缩进之后、序号之前：缩进量不受运行状态影响，层级始终对齐
            head = "▶ " if running else ""
            commented = bool(getattr(s, "commented", False))
            comment_tag = "// " if commented else ""
            text = (f"{INDENT_UNIT * levels[i]}{head}{i + 1}. "
                    f"{comment_tag}{_TYPE_ICONS.get(s.type, '')} {s.name} · "
                    f"{s.summary()}{mark}")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, i)
            # 必填参数没填的步骤：列表里描红框（委托画）+ tooltip 写清缺什么。
            # 已注释的步骤不标红——它运行期会被跳过，标了只是噪音。
            missing = [] if commented else step_missing_required(s)
            if missing:
                item.setData(_STEP_MISSING_ROLE, missing)
                item.setToolTip("还没设置：" + "、".join(missing)
                                + "\n双击这个步骤打开设置")
            if running:
                item.setBackground(QColor(theme.token("sel_bg")))
                item.setForeground(QColor(theme.token("success_pressed")))
            elif commented:
                item.setForeground(QColor(theme.token("text_muted")))
            self.step_list.addItem(item)
        if not flow.steps:
            empty = QListWidgetItem("（流程为空：把上方模块拖进来）")
            empty.setFlags(Qt.ItemIsEnabled)
            self.step_list.addItem(empty)
        self.step_list.blockSignals(False)

    def _current_step(self) -> FlowStep | None:
        flow = self._selected_flow()
        row = self.step_list.currentRow()
        if flow is not None and 0 <= row < len(flow.steps):
            return flow.steps[row]
        return None

    def _on_step_dropped(self, step_type: str, row: int):
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            self._reload_steps()
            return
        row = max(0, min(row, len(flow.steps)))
        before = self._snapshot(flow)   # 撤销点（被拒绝的拖入不会入栈，见 _record_undo）
        new_row: int | None = None      # 新步骤所在行（拖入后要选中它）
        open_editor = True              # 是否顺手打开它的编辑窗
        if step_type == "web":
            # 网页动作（2026-09-04 起解耦）：拖「网页操作」只生成单个「打开网址」步骤，
            # 浏览器保持打开不自动关闭；「关闭浏览器」不再作为面板独立模块，
            # 需要时双击本步骤在「操作」下拉里选择。
            step = FlowStep(type="web", params=dict(
                default_step_params("web", self.cfg.clicker, self.cfg.presser)))
            flow.steps.insert(row, step)
            new_row = row
            self._status_msg("已添加「打开网址」步骤（浏览器保持打开；关闭请编辑步骤的「操作」）",
                             5000)
        elif step_type in BLOCK_PAIRS:
            # if / foreach / while：起始块 + 结束标记成对出现（结束标记为自动结构标记，
            # 不在面板展示），删除起始块或结束标记时按整块同步删除。
            new_row = self._insert_block_pair(flow, step_type, row)
        elif step_type in ("elseif", "else"):
            new_row = self._insert_condition_branch(flow, step_type, row)
        elif step_type in ("break", "continue"):
            # 这两个没有可填参数，弹窗是空的，不打开
            new_row = self._insert_break_continue(flow, step_type, row)
            open_editor = False
        else:
            step = FlowStep(type=step_type,
                            params=default_step_params(step_type, self.cfg.clicker,
                                                       self.cfg.presser))
            flow.steps.insert(row, step)
            new_row = row
        self._record_undo(flow, before)
        self._reload_steps()
        # 拖进来就立刻打开它的编辑窗（2026-09-26 用户要求）：省掉"拖进来再双击"这一步。
        # 用 singleShot(0) 等这次 dropEvent 处理完再弹——此刻列表刚重建、drop 事件还没
        # 返回，直接开非模态窗会打断 Qt 的拖放收尾。
        # 被拒绝的拖入（分支位置非法 / break 不在循环里）new_row 为 None，自然不弹。
        if open_editor and new_row is not None and 0 <= new_row < len(flow.steps):
            self.step_list.setCurrentRow(new_row)
            QTimer.singleShot(0, self._edit_step_param)
        # 拖入模块只动了 flow.steps：发 stepsChanged 即可（只落盘）。
        # 走 changed 会连带重注册全部全局热键、并重建定时任务页/中键菜单页的列表，
        # 那些页面只显示流程名，与步骤无关 —— 每次拖入白花十几毫秒（2026-09-15）。
        self.stepsChanged.emit()

    def _insert_block_pair(self, flow: Flow, open_type: str, row: int) -> int:
        """把起始块（if/foreach/while）与其结束标记成对插入到 row 位置。

        结束标记（endif/endForeach/endWhile）是自动结构标记，不在模块面板展示，
        由这里随起始块一起创建；删除起始块或结束标记时按整块同步删除。
        返回**起始块**的行号（拖入后要选中它并打开编辑窗）。
        """
        end_type = BLOCK_PAIRS[open_type]
        open_step = FlowStep(type=open_type, params=dict(
            default_step_params(open_type, self.cfg.clicker, self.cfg.presser)))
        end_step = FlowStep(type=end_type, params=dict(
            default_step_params(end_type, self.cfg.clicker, self.cfg.presser)))
        flow.steps.insert(row, open_step)
        flow.steps.insert(row + 1, end_step)
        self._status_msg(f"已生成「{FLOW_STEP_TYPES[open_type]} + "
                         f"{FLOW_STEP_TYPES[end_type]}」一对（删除时同步删除）", 4000)
        return row

    def _insert_condition_branch(self, flow: Flow, step_type: str, row: int) -> int | None:
        """把 elseif/else 插入到正确位置的条件块内。

        elseif 插到 else 之前（无 else 则 endif 之前），多个 elseif 依次堆叠；
        else 插到 endif 之前，且同一 if 块至多一个。若 drop 位置不在任何 if 块内
        则拒绝并提示（状态栏 + 消息框），不产生脏数据。
        """
        steps = flow.steps
        probe = min(row, len(steps) - 1) if steps else -1
        if_idx = enclosing_if(steps, probe) if probe >= 0 else None
        if if_idx is None:
            self._status_msg("「否则如果 / 否则」必须拖到 if 条件块内部", 5000)
            QMessageBox.information(
                self, "无法添加分支",
                "「否则如果 / 否则」必须位于某个 if 条件块内部。\n"
                "请先把它们拖到 if 与 endif 之间的步骤行上。")
            return
        end_idx = match_endif(steps, if_idx)
        if end_idx is None:
            self._status_msg("if 缺少配对的 endif，无法添加分支", 5000)
            return
        if step_type == "else":
            if any(s.type == "else" for s in steps[if_idx:end_idx]):
                self._status_msg("该 if 条件块已存在「否则」，不能再添加", 5000)
                QMessageBox.information(self, "无法添加「否则」",
                                        "同一 if 条件块只能有一个「否则」。")
                return
            insert_at = end_idx                     # else 恒插到 endif 之前
        else:                                       # elseif
            insert_at = end_idx                     # 默认插到 endif 之前
            for i in range(end_idx - 1, if_idx, -1):
                if steps[i].type == "else":
                    insert_at = i                   # 有 else 则插到 else 之前
                    break
        new_step = FlowStep(type=step_type, params=dict(
            default_step_params(step_type, self.cfg.clicker, self.cfg.presser)))
        steps.insert(insert_at, new_step)
        self._status_msg(f"已添加「{FLOW_STEP_TYPES[step_type]}」到 if 条件块", 4000)
        return insert_at

    def _insert_break_continue(self, flow: Flow, step_type: str, row: int) -> int | None:
        """把 break/continue 插入到 row 位置，但仅当该位置落在 foreach/while 循环体内。

        先插入再用 enclosing_loop 校验插入后的位置是否在循环内；不在则回滚并提示，
        保证不产生「落在非循环流程里的 break/continue」脏数据。成功返回其行号。
        """
        step = FlowStep(type=step_type, params=dict(
            default_step_params(step_type, self.cfg.clicker, self.cfg.presser)))
        flow.steps.insert(row, step)
        if enclosing_loop(flow.steps, row) is None:
            del flow.steps[row]
            label = FLOW_STEP_TYPES[step_type]
            self._status_msg(f"「{label}」只能放在 Foreach/while 循环体内", 6000)
            QMessageBox.information(
                self, "无法添加",
                f"「{label}」只能放在 Foreach 循环或 while 循环的循环体内部。\n"
                "请先拖入循环模块，再把该步骤拖到循环体（起始块与结束标记之间）内。")
            return None
        self._status_msg(f"已添加「{FLOW_STEP_TYPES[step_type]}」", 4000)
        return row

    def _on_order_changed(self):
        flow = self._selected_flow()
        if flow is None:
            return
        # 延迟重建，避免在 dropEvent 过程中改动列表结构
        def apply():
            order = []
            for i in range(self.step_list.count()):
                idx = self.step_list.item(i).data(Qt.UserRole)
                if idx is not None and 0 <= idx < len(flow.steps):
                    order.append(flow.steps[idx])
            if len(order) != len(flow.steps):
                self._reload_steps()          # 顺序未完整读出，重建回到当前 steps
                return
            original = list(flow.steps)       # 快照：结构非法时回滚到拖拽前
            flow.steps[:] = order
            errors = validate_block_structure(flow.steps)
            if errors:
                # 拖拽破坏了 if/foreach/while 的闭合边界（如把结束标记拖出块）：
                # 回滚并提示，保证块结构始终完整，不落盘脏数据。
                flow.steps[:] = original
                self._status_msg("不能把步骤拖出循环/条件块边界：" + errors[0], 6000)
            else:
                # 拖动只改了步骤顺序：流程名/分组/热键/轮数都没变，左栏条目也不显示
                # 步骤，所以只需触发落盘。走 changed 会连带重注册全部全局热键并重建
                # 定时任务页、中键菜单页的列表——那部分对拖动排序毫无意义。
                self._record_undo(flow, original)   # 拖动排序也能撤销
                self.stepsChanged.emit()
            self._reload_steps()
        QTimer.singleShot(0, apply)

    def _edit_step_param(self):
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        step = self._current_step()
        if step is None:
            return
        dlg = StepParamsDialog(step, self)
        dlg.regionCaptureRequested.connect(lambda: self._capture_region_for_step(dlg))
        dlg.templateCaptureRequested.connect(lambda: self._capture_template_for_step(dlg))
        dlg.pointCaptureRequested.connect(lambda: self._capture_point_for_step(dlg))
        dlg.windowCaptureRequested.connect(lambda: self._capture_window_for_step(dlg))
        dlg.colorPickRequested.connect(lambda: self._capture_color_for_step(dlg))

        # 非模态 + finished 信号保存：截图选区期间对话框 hide() 不会像 exec() 那样
        # 立刻以 Rejected 结束编辑会话（那是 image 保存丢失的根因）
        def _finished(result):
            if result == StepParamsDialog.Accepted:
                before = self._snapshot(flow)      # 撤销点：保存参数同样可撤销
                dlg.apply_to(step)
                # 兼容遗留：旧版成对网页步骤被改成其它动作后，解除残留配对标记
                if repair_web_pairs(flow.steps):
                    self._status_msg("已解除旧的网页配对标记（网页步骤现已独立）", 4000)
                self._record_undo(flow, before)
                self._reload_steps()
                # 步骤参数只写进 flow.steps：发 stepsChanged（只落盘 + 重绘右栏）。
                # 步骤名不参与任何其它页面的显示，无需重注册热键或刷新那两个页面。
                self.stepsChanged.emit()

        dlg.finished.connect(_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _del_step(self):
        """删除步骤：支持 Ctrl 多选一次删多个（单步时保留原有的针对性提示）。"""
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        rows = self._selected_step_rows()
        if not rows:
            return
        if len(rows) == 1:
            self._del_single_step(flow, rows[0])
            return
        if not self._confirm_del_steps(flow, rows):
            return
        self._delete_step_rows(flow, rows)

    def _selected_step_rows(self) -> list[int]:
        """步骤列表里当前选中的行号（升序、去重）。

        Ctrl 点选多个 / Shift 范围选择都会体现在 selectedItems() 里；
        「（流程为空…）」这类占位项没有行号数据，自动忽略。
        """
        rows = set()
        for item in self.step_list.selectedItems():
            row = item.data(Qt.UserRole)
            if isinstance(row, int):
                rows.add(row)
        return sorted(rows)

    def _block_expanded_rows(self, flow, rows) -> list[int]:
        """把选中行展开成「实际要操作的完整行列表」（升序、去重）。

        规则与单步删除一致：if/foreach/while 的起始块**或**结束标记 → 连带整块
        （含结束标记、分支与块内步骤）；「否则 / 否则如果」→ 只算分支头自己；
        其余 → 只算自己。
        **一次性算完整列表再操作**，不逐行处理——否则动了前面的步骤后，后面
        选中行的行号就漂了。删除与复制共用这一份展开规则。
        """
        steps = flow.steps
        out: set[int] = set()
        for row in rows:
            if not (0 <= row < len(steps)):
                continue
            step = steps[row]
            if step.type in BRANCH_TYPES:
                out.add(row)
            elif step.type in BLOCK_OPEN_TYPES or step.type in BLOCK_CLOSE_TYPES:
                block = block_indices(steps, row)
                out.update(block if block else [row])
            else:
                out.add(row)
        return sorted(out)

    def _rows_to_remove(self, flow, rows) -> set[int]:
        """要移除的步骤索引集合（见 _block_expanded_rows 的展开规则）。"""
        return set(self._block_expanded_rows(flow, rows))

    # ---------- 撤销（Ctrl+Z） ----------

    @staticmethod
    def _same_steps(a, b) -> bool:
        """两份步骤列表内容是否一致（FlowStep 是 dataclass，逐字段比较）。"""
        return list(a) == list(b)

    @staticmethod
    def _snapshot(flow) -> list:
        """给当前步骤列表拍一份可回退的深拷贝快照。"""
        return copy.deepcopy(flow.steps)

    def _record_undo(self, flow, before) -> None:
        """操作结束后登记撤销点：**只有跟快照不一样才入栈**。

        这样"点了删除又取消""往非法位置拖被拒绝"这类没造成改动的操作不会污染历史
        （否则用户按 Ctrl+Z 会看到"撤销了但什么都没变"）。
        """
        if self._same_steps(flow.steps, before):
            return
        stack = self._undo.setdefault(flow.id, [])
        stack.append(before)
        del stack[:-MAX_UNDO_STEPS]

    def _undo_steps(self):
        """撤销当前流程的上一步步骤改动（Ctrl+Z），可连续撤销直到最早一步。"""
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        stack = self._undo.get(flow.id) or []
        if not stack:
            self._status_msg("没有可撤销的操作了", 4000)
            return
        snapshot = stack.pop()
        flow.steps[:] = snapshot
        repair_web_pairs(flow.steps)   # 兼容遗留：恢复后清一遍残留的网页配对标记
        self._reload_steps()
        tail = f"（还能再撤销 {len(stack)} 步）" if stack else "（已回到最早的版本）"
        self._status_msg(f"已撤销上一步操作{tail}", 4000)
        self.stepsChanged.emit()

    # ---------- 复制 / 粘贴（Ctrl+C / Ctrl+V） ----------

    def _copy_steps(self):
        """复制选中的步骤到内部剪贴板（多选生效；块会连带整块一起复制）。"""
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        rows = self._selected_step_rows()
        if not rows:
            self._status_msg("请先在列表里选中要复制的步骤（Ctrl 可多选）", 4000)
            return
        picked = self._block_expanded_rows(flow, rows)
        # 深拷贝：粘贴出的副本必须与原步骤完全独立（改副本不能动到原件）
        self._step_clipboard = [copy.deepcopy(flow.steps[i]) for i in picked]
        extra = f"（含块内步骤，共 {len(picked)} 个）" if len(picked) > len(rows) else ""
        self._status_msg(f"已复制 {len(rows)} 个步骤{extra}"
                         f"，按 Ctrl+V 粘贴", 5000)

    def _paste_steps(self):
        """把剪贴板里的步骤粘贴到当前行之后（没选中则追加到末尾），并选中粘贴出的步骤。"""
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        if not self._step_clipboard:
            self._status_msg("剪贴板里还没有复制过步骤（先选中步骤按 Ctrl+C）", 4000)
            return
        row = self.step_list.currentRow()
        insert_at = row + 1 if 0 <= row < len(flow.steps) else len(flow.steps)
        copies = [copy.deepcopy(s) for s in self._step_clipboard]
        before = self._snapshot(flow)          # 撤销点
        original = list(flow.steps)            # 结构校验失败时整体回滚用
        flow.steps[insert_at:insert_at] = copies
        errors = validate_block_structure(flow.steps)
        if errors:
            # 例如把 break/continue 粘到了循环外：宁可整体回滚也不落脏数据
            flow.steps[:] = original
            self._status_msg("粘贴会破坏流程结构，已取消：" + errors[0], 6000)
            self._reload_steps()
            return
        repair_web_pairs(flow.steps)
        self._record_undo(flow, before)
        self._reload_steps()
        self._select_rows(range(insert_at, insert_at + len(copies)))
        self._status_msg(f"已粘贴 {len(copies)} 个步骤", 4000)
        self.stepsChanged.emit()       # 只改了步骤：同上

    def _delete_step_rows(self, flow, rows) -> None:
        """按 _rows_to_remove 的结果批量删除，删完校验块结构，异常则整体回滚。"""
        remove = self._rows_to_remove(flow, rows)
        if not remove:
            return
        before = self._snapshot(flow)          # 撤销点
        original = list(flow.steps)            # 结构校验失败时整体回滚用
        flow.steps[:] = [s for i, s in enumerate(flow.steps) if i not in remove]
        errors = validate_block_structure(flow.steps)
        if errors:
            # 块级联已保证配对，正常不会走到这里；真发生了宁可整体回滚也不落脏数据
            flow.steps[:] = original
            self._status_msg("删除会破坏 if/循环的配对结构，已取消：" + errors[0], 6000)
            self._reload_steps()
            return
        repair_web_pairs(flow.steps)   # 兼容遗留：删除后即时清理残留的网页配对标记
        extra = (f"（含条件/循环块的连带步骤，共移除 {len(remove)} 个）"
                 if len(remove) > len(rows) else "")
        self._status_msg(f"已删除选中的 {len(rows)} 个步骤{extra}", 5000)
        self._record_undo(flow, before)
        self._reload_steps()
        self.stepsChanged.emit()       # 只删了步骤：不必刷新热键与其它页面（同上）

    def _confirm_del_steps(self, flow, rows) -> bool:
        """多选批量删除的确认框：写清选中几个、实际会移除几个（含块级联）。

        默认按钮同样是「取消」，回车/Esc 都不会误删。
        """
        remove = self._rows_to_remove(flow, rows)
        lines = []
        for r in rows[:6]:
            if 0 <= r < len(flow.steps):
                name = FLOW_STEP_TYPES.get(flow.steps[r].type, flow.steps[r].type)
                lines.append(f"{r + 1}. {name}")
        more = f"\n…等共 {len(rows)} 个" if len(rows) > 6 else ""
        text = f"确定删除选中的 {len(rows)} 个步骤吗？\n\n" + "\n".join(lines) + more
        if len(remove) > len(rows):
            text += (f"\n\n其中包含条件/循环块，实际会移除 {len(remove)} 个步骤"
                     f"（含结束标记与块内步骤）。")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("批量删除步骤")
        box.setText(text)
        btn_del = box.addButton("删除", QMessageBox.ButtonRole.YesRole)
        btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(btn_cancel)
        box.exec()
        return box.clickedButton() is btn_del

    def _del_single_step(self, flow, row):
        """删除单个步骤（原 _del_step 主体：提示语按步骤类型区分，保留不动）。"""
        if not (0 <= row < len(flow.steps)):
            return
        if not self._confirm_del_step(flow, row):
            return
        before = self._snapshot(flow)   # 撤销点（确认框之后才拍，取消删除不留历史）
        step = flow.steps[row]
        if step.type in BRANCH_TYPES:
            # 「否则 / 否则如果」只删分支头本身：条件判断、条件结束与其它分支
            # 全部保留（原来删任意条件步骤都会连带整块，等于没法只摘掉一个分支）。
            # 该分支下原有的步骤不删除，会并入上一分支，提示里说清去向。
            inner = self._branch_body_count(flow.steps, row)
            del flow.steps[row]
            label = FLOW_STEP_TYPES[step.type]
            if inner:
                self._status_msg(f"已删除「{label}」；其下 {inner} 个步骤"
                                 f"已并入上一分支", 6000)
            else:
                self._status_msg(f"已删除「{label}」（条件判断与其它分支保留）", 4000)
        elif step.type in BLOCK_OPEN_TYPES or step.type in BLOCK_CLOSE_TYPES:
            # 块结构（if/endif、foreach/endForeach、while/endWhile）：删除起始块
            # 或结束标记都会删除整个块（含结束标记、分支与块内全部步骤）。
            block = block_indices(flow.steps, row)
            if block:
                keep = set(block)
                flow.steps[:] = [s for i, s in enumerate(flow.steps) if i not in keep]
                open_type = (step.type if step.type in BLOCK_PAIRS
                             else _CLOSE_TO_OPEN[step.type])
                self._status_msg(_BLOCK_DEL_MSG.get(
                    open_type, "已删除整个块"), 4000)
            else:
                del flow.steps[row]
        else:
            del flow.steps[row]
        repair_web_pairs(flow.steps)   # 兼容遗留：删除后即时清理残留的网页配对标记
        self._record_undo(flow, before)
        self._reload_steps()
        self.stepsChanged.emit()       # 只删了步骤：不必刷新热键与其它页面（同上）

    def _confirm_del_step(self, flow, row) -> bool:
        """删除步骤前的确认弹窗。

        不同步骤的删除范围不同（条件块整块、只删分支头），弹窗里
        必须写清连带影响再让用户决定。默认按钮是「取消」，按回车/Esc 都不会误删。
        """
        step = flow.steps[row]
        extra = ""
        if step.type in BRANCH_TYPES:
            label = FLOW_STEP_TYPES[step.type]
            inner = self._branch_body_count(flow.steps, row)
            tail = f"\n\n其下 {inner} 个步骤将并入上一分支。" if inner else ""
            extra = (f"\n\n只删除「{label}」这个分支头，条件判断、条件结束"
                     f"与其它分支都保留。{tail}")
        elif step.type in BLOCK_OPEN_TYPES or step.type in BLOCK_CLOSE_TYPES:
            n = len(block_indices(flow.steps, row))
            if n > 1:
                open_type = (step.type if step.type in BLOCK_PAIRS
                             else _CLOSE_TO_OPEN[step.type])
                if open_type == "if":
                    extra = (f"\n\n同时删除整个条件块的 {n} 个步骤"
                             f"（含分支与条件结束）。")
                else:
                    kind = _BLOCK_KIND.get(open_type, "块")
                    extra = (f"\n\n同时删除整个 {kind} 的 {n} 个步骤"
                             f"（含结束标记与循环体内步骤）。")
        name = FLOW_STEP_TYPES.get(step.type, step.type)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("删除步骤")
        box.setText(f"确定删除第 {row + 1} 步「{name}」吗？\n\n{step.summary()}{extra}")
        btn_del = box.addButton("删除", QMessageBox.ButtonRole.YesRole)
        btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(btn_cancel)      # 默认落在「取消」：回车也不会误删
        box.exec()
        return box.clickedButton() is btn_del

    @staticmethod
    def _branch_body_count(steps, idx: int) -> int:
        """分支头 idx 之下、下一个同级分支头（elseif/else/endif）之前的步骤数。

        嵌套 if 整块算作一个步骤计入；用于删除分支头后提示"多少步骤并入上一分支"。
        """
        depth = 0
        count = 0
        for i in range(idx + 1, len(steps)):
            t = getattr(steps[i], "type", "")
            if t == "if":
                depth += 1
            elif t == "endif":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and t in BRANCH_TYPES:
                break
            count += 1
        return count

    def _step_context_menu(self, pos):
        """步骤列表右键菜单：编辑参数 / 删除步骤（复用底部按钮的逻辑）。"""
        item = self.step_list.itemAt(pos)
        if item is None or item.data(Qt.UserRole) is None:   # 空流程占位行不弹菜单
            return
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        # 右键点**已选中**的行时保留多选（Ctrl 多选后要批量操作：注释/删除）；
        # 点未选中的行才按常规改为只选它。默认的 setCurrentItem 会把其它选中项清掉。
        if item.isSelected():
            self.step_list.setCurrentItem(item, QItemSelectionModel.NoUpdate)
        else:
            self.step_list.setCurrentItem(item)
        row = item.data(Qt.UserRole)
        step = flow.steps[row] if isinstance(row, int) and 0 <= row < len(flow.steps) else None
        menu = QMenu(self)
        # 显示效果：字号与中间模块列表一致（10pt）、行高同档、内边距收紧，
        # 悬停/预选蓝色高亮（2026-10-01 按用户要求由 12pt/大内边距改小）。
        menu.setStyleSheet("""
            QMenu {
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 6px;
                padding: 4px;
                font-size: 10pt;
            }
            QMenu::item {
                color: #24292f;
                padding: 6px 26px 6px 10px;
                margin: 1px 2px;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background: #1668a8;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #a7afb8;
                background: transparent;
            }
        """)
        edit_act = menu.addAction("✎ 编辑参数")
        comment_act = None
        if step is not None:
            # 文案随选中数量与当前状态变：多选时写清"选中的 N 个模块"及方向
            rows = self._selected_step_rows()
            picked = [flow.steps[r] for r in rows if 0 <= r < len(flow.steps)]
            if len(picked) > 1:
                all_commented = all(s.commented for s in picked)
                comment_act = menu.addAction(
                    (f"✅ 取消注释选中的 {len(picked)} 个模块" if all_commented
                     else f"🚫 注释选中的 {len(picked)} 个模块"))
            else:
                comment_act = menu.addAction("✅ 取消注释" if step.commented else "🚫 注释")
        undo_act = menu.addAction("↩ 撤销（Ctrl+Z）")
        undo_act.setEnabled(bool(self._undo.get(flow.id)))   # 没历史就置灰
        copy_act = menu.addAction("📋 复制（Ctrl+C）")
        paste_act = menu.addAction("📥 粘贴（Ctrl+V）")
        paste_act.setEnabled(bool(self._step_clipboard))   # 没复制过就置灰
        del_act = menu.addAction("🗑 删除步骤")
        act = menu.exec(self.step_list.viewport().mapToGlobal(pos))
        if act == edit_act:
            self._edit_step_param()
        elif comment_act is not None and act == comment_act:
            self._toggle_comment_step()
        elif act == undo_act:
            self._undo_steps()
        elif act == copy_act:
            self._copy_steps()
        elif act == paste_act:
            self._paste_steps()
        elif act == del_act:
            self._del_step()

    def _toggle_comment_step(self):
        """切换注释状态：支持 Ctrl 多选一次注释 / 取消注释多个。

        多选时的方向按"选中的是不是全都已注释"决定：全已注释 → 取消注释；否则 → 全部
        注释（否则多选里混着两种状态时没法表达）。注释只是标志位，不影响 if/循环的配对。
        做完保持选中，方便接着删除或继续调。
        """
        flow = self._selected_flow()
        if flow is None or self._selected_running():
            return
        rows = [r for r in self._selected_step_rows() if 0 <= r < len(flow.steps)]
        if not rows:
            # 兜底：从右键菜单触发（右键点在某行上）但该行没被选中时
            row = self.step_list.currentRow()
            rows = [row] if 0 <= row < len(flow.steps) else []
        if not rows:
            return
        before = self._snapshot(flow)      # 撤销点
        steps = [flow.steps[r] for r in rows]
        target = not all(s.commented for s in steps)
        for s in steps:
            s.commented = target
        self._record_undo(flow, before)
        self._reload_steps()
        self._select_rows(rows)        # 重建列表后保持选中
        if len(rows) > 1:
            self._status_msg(f"已{'注释' if target else '取消注释'}选中的 "
                             f"{len(rows)} 个模块", 4000)
        else:
            self._status_msg("已注释该模块" if target else "已取消注释该模块", 4000)
        self.stepsChanged.emit()       # 只改了步骤的注释位：同上

    def _select_rows(self, rows) -> None:
        """把给定行重新选中（列表重建后恢复多选状态）。"""
        for r in rows:
            item = self.step_list.item(r)
            if item is not None:
                item.setSelected(True)

    # ---------- 区域框选链（步骤参数对话框 -> 主窗口隐藏 -> 遮罩 -> 回写） ----------
    def _capture_region_for_step(self, dlg: StepParamsDialog):
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, self._start_region_capture)

    def _start_region_capture(self):
        from ..capture_overlay import run_screen_capture
        dlg = self._capture_step_dlg

        def done(rect=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if rect is not None:
                    dlg.set_region(rect)
                dlg.finish_region_capture()
            self._capture_step_dlg = None

        try:
            run_screen_capture(on_region=lambda r: done(r), on_cancelled=lambda: done())
        except Exception:
            done()

    def _capture_template_for_step(self, dlg):
        """模板图屏幕截图选区：与「添加模板」相同流程，成功后回写步骤模板。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_template_capture(dlg))

    def _start_point_capture(self, dlg):
        from ..capture_overlay import run_screen_capture

        def done(point=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if point:
                    dlg.set_point(point[0], point[1])
                dlg.finish_point_capture()
            self._capture_step_dlg = None

        try:
            run_screen_capture(on_point=lambda pt: done(pt),
                               on_cancelled=lambda: done())
        except Exception:
            done()

    def _capture_point_for_step(self, dlg):
        """屏幕点选坐标：主窗口隐藏 -> 遮罩单击取点 -> 回写步骤坐标。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_point_capture(dlg))

    def _capture_window_for_step(self, dlg):
        """拖动识别窗口：主窗口隐藏 -> 实时高亮遮罩 -> 单击确认句柄。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_window_capture(dlg))

    def _capture_color_for_step(self, dlg):
        """屏幕取色：主窗口隐藏 -> 十字 + 像素放大遮罩 -> 单击回填颜色。"""
        self._capture_step_dlg = dlg
        win = self.window()
        if hasattr(win, "_hide_for_capture"):
            win._hide_for_capture()
        QTimer.singleShot(250, lambda: self._start_color_capture(dlg))

    def _start_color_capture(self, dlg):
        """取色遮罩：移动鼠标实时放大附近像素，单击确认取色回填（右键 / Esc 取消）。"""
        from ..capture_overlay import run_color_picker

        def done(rgb=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if rgb:
                    dlg.set_color(*rgb)
                dlg.finish_color_pick()
            self._capture_step_dlg = None

        try:
            run_color_picker(on_picked=lambda r, g, b: done((r, g, b)),
                             on_cancelled=lambda: done())
        except Exception:
            done()

    def _start_window_capture(self, dlg):
        """窗口识别遮罩：移动鼠标实时高亮目标窗口，单击确认句柄回填。"""
        from ..capture_overlay import run_window_picker

        def done(hwnd=0, title=""):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if hwnd:
                    dlg.set_window(hwnd, title)
                dlg.finish_window_capture()
            self._capture_step_dlg = None

        try:
            run_window_picker(on_picked=lambda hwnd, title: done(hwnd, title),
                              on_cancelled=lambda: done())
        except Exception:
            done()

    def _start_template_capture(self, dlg):
        from ..capture_overlay import run_screen_capture

        def done(path=None):
            win = self.window()
            if hasattr(win, "_restore_after_capture"):
                win._restore_after_capture()
            if dlg is not None:
                if path:
                    dlg.set_template_image(os.path.basename(path), path)
                dlg.finish_template_capture()
            self._capture_step_dlg = None

        try:
            run_screen_capture(on_saved=lambda p: done(p), on_cancelled=lambda: done())
        except Exception:
            done()

    # ---------- 流程元信息（新建/编辑） ----------
    def _next_flow_seq(self) -> int:
        """下一个流程创建序号：现有最大序号 + 1，保证新增流程按创建顺序递增。"""
        return max((f.created_seq for f in self._flows), default=0) + 1

    def _new_flow(self, group: str = ""):
        flow = Flow(name=f"流程 {len(self._flows) + 1}", group=group)
        flow.created_seq = self._next_flow_seq()   # 新增流程排在末尾（append），创建序号递增
        dlg = FlowMetaDialog(flow, create=True, parent=self, groups=self.cfg.flow_groups)
        if dlg.exec() == FlowMetaDialog.Accepted:
            dlg.apply_to(flow)
            self._flows.append(flow)
            self.refresh_list()
            self._select_flow_item(flow.id)
            self.changed.emit()

    def _edit_flow(self):
        flow = self._selected_flow()
        if flow is None:
            return
        dlg = FlowMetaDialog(flow, create=False, parent=self, groups=self.cfg.flow_groups)
        if dlg.exec() == FlowMetaDialog.Accepted:
            dlg.apply_to(flow)
            self.refresh_list()
            self.changed.emit()

    def _del_flow(self):
        flow = self._selected_flow()
        if flow is None:
            return
        if QMessageBox.question(self, "删除流程", f"确定删除流程「{flow.name}」吗？"
                                ) != QMessageBox.Yes:
            return
        self.stop_flow(flow.id)
        self._dequeue(flow.id, "流程已删除，已移出队列")
        self._runners.pop(flow.id, None)      # 流程都删了，别再留着它的 runner
        self._single_rows.pop(flow.id, None)
        self._flows.remove(flow)
        self.refresh_list()
        self.changed.emit()

    # ---------- 分组管理 ----------
    def _add_group(self):
        name, ok = QInputDialog.getText(self, "添加分组", "请输入分组名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.cfg.flow_groups:
            QMessageBox.information(self, "分组已存在", f"分组「{name}」已经存在。")
            return
        self.cfg.flow_groups.append(name)
        self.cfg.flow_group_seqs[name] = max(self.cfg.flow_group_seqs.values(), default=0) + 1
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    def _rename_group(self, g: str):
        name, ok = QInputDialog.getText(self, "重命名分组", "新的分组名称：", text=g)
        name = (name or "").strip()
        if not ok or not name or name == g:
            return
        if name in self.cfg.flow_groups:
            QMessageBox.information(self, "分组已存在", f"分组「{name}」已经存在。")
            return
        self.cfg.flow_groups = [name if x == g else x for x in self.cfg.flow_groups]
        if g in self.cfg.flow_group_seqs:   # 分组改名：迁移创建序号，保证排序顺序不变
            self.cfg.flow_group_seqs[name] = self.cfg.flow_group_seqs.pop(g)
        if g in (self.cfg.group_hotkeys or {}):   # 分组热键跟着分组名一起迁移
            self.cfg.group_hotkeys[name] = self.cfg.group_hotkeys.pop(g)
        for f in self._flows:
            if f.group == g:
                f.group = name
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    def _del_group(self, g: str):
        if QMessageBox.question(self, "删除分组",
                                f"确定删除分组「{g}」吗？\n组内流程将移到「未分组」。"
                                ) != QMessageBox.Yes:
            return
        self.cfg.flow_groups = [x for x in self.cfg.flow_groups if x != g]
        self.cfg.flow_group_seqs.pop(g, None)   # 删除分组：清掉它的创建序号
        (self.cfg.group_hotkeys or {}).pop(g, None)   # 顺手清掉该分组的热键
        self.cfg.collapsed_flow_groups = [x for x in self.cfg.collapsed_flow_groups if x != g]
        for f in self._flows:
            if f.group == g:
                f.group = ""
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()

    # ---------- 排序与置顶 ----------
    def _pin_flow(self, flow_id: str):
        """置顶流程：把该流程移到其所属分组内最前（同组其余流程保持相对顺序）。"""
        flow = next((f for f in self._flows if f.id == flow_id), None)
        if flow is None:
            return
        group = flow.group
        rest = [f for f in self._flows if f.id != flow_id]
        # 插到 rest 里第一个同组流程之前：渲染时按分组分桶，组内顺序即 _flows 里的相对顺序
        insert_at = next((i for i, f in enumerate(rest) if f.group == group), len(rest))
        rest.insert(insert_at, flow)
        self._flows[:] = rest
        self.cfg.save()
        self.refresh_list()
        self._select_flow_item(flow.id)
        self.changed.emit()
        self._status_msg(f"已置顶「{flow.name}」", 4000)

    def _sort_flows_in_group(self, group: str):
        """把某分组内的流程按创建顺序（created_seq）升序重排，其余分组不受影响。"""
        group_flows = sorted([f for f in self._flows if f.group == group],
                             key=lambda f: f.created_seq)
        it = iter(group_flows)
        self._flows[:] = [next(it) if f.group == group else f for f in self._flows]
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()
        self._status_msg(f"已按创建顺序排序「{group or '未分组'}」内的流程", 4000)

    def _pin_group(self, g: str):
        """置顶分组：把该分组移到分组列表最前（「未分组」恒在末尾，不参与）。"""
        if g not in self.cfg.flow_groups:
            return
        self.cfg.flow_groups = [g] + [x for x in self.cfg.flow_groups if x != g]
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()
        self._status_msg(f"已置顶分组「{g}」", 4000)

    def _sort_groups(self):
        """按分组创建顺序（flow_group_seqs）重排分组列表。"""
        seqs = self.cfg.flow_group_seqs
        self.cfg.flow_groups.sort(key=lambda g: seqs.get(g, 0))
        self.cfg.save()
        self.refresh_list()
        self.changed.emit()
        self._status_msg("已按创建顺序排序分组", 4000)

    # ---------- 左栏右键菜单 ----------
    def _flow_context_menu(self, pos):
        """流程列表右键：流程项 = 置顶/排序/编辑/删除/导出；分组头 = 分组管理。"""
        item = self.list.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "group":
            self._group_context_menu(data[1], self.list.viewport().mapToGlobal(pos))
            return
        flow = next((f for f in self._flows if f.id == data[1]), None)
        if flow is None:
            return
        self.list.setCurrentItem(item)               # 右键即选中该流程
        menu = QMenu(self)
        self._style_menu(menu)
        pin_act = menu.addAction("↥ 置顶")
        sort_act = menu.addAction("↕ 按创建顺序排序")
        menu.addSeparator()
        edit_act = menu.addAction("✎ 编辑流程")
        del_act = menu.addAction("🗑 删除流程")
        menu.addSeparator()
        export_act = menu.addAction("📤 导出流程")
        act = menu.exec(self.list.viewport().mapToGlobal(pos))
        if act == pin_act:
            self._pin_flow(flow.id)
        elif act == sort_act:
            self._sort_flows_in_group(flow.group)
        elif act == edit_act:
            self._edit_flow()
        elif act == del_act:
            self._del_flow()
        elif act == export_act:
            self._export_flow()

    def _group_context_menu(self, g: str, pos):
        """分组头右键：编辑分组/整组运行 + 置顶/排序/重命名/删除（「未分组」不可操作）。"""
        if not g:
            return
        menu = QMenu(self)
        self._style_menu(menu)
        edit_act = menu.addAction("⚙ 编辑分组（整组运行 / 热键）")
        run_act = menu.addAction("▶ 运行本组全部流程")
        menu.addSeparator()
        pin_act = menu.addAction("↥ 置顶")
        sort_act = menu.addAction("↕ 按创建顺序排序")
        menu.addSeparator()
        rename_act = menu.addAction("✎ 重命名分组")
        del_act = menu.addAction("🗑 删除分组")
        act = menu.exec(pos)
        if act == edit_act:
            self.open_group_dialog(g)
        elif act == run_act:
            self.run_group(g)
        elif act == pin_act:
            self._pin_group(g)
        elif act == sort_act:
            self._sort_groups()
        elif act == rename_act:
            self._rename_group(g)
        elif act == del_act:
            self._del_group(g)

    @staticmethod
    def _style_menu(menu: QMenu):
        menu.setStyleSheet("""
            QMenu {
                background: #ffffff;
                border: 1px solid #d8dee4;
                border-radius: 6px;
                padding: 4px;
                font-size: 10pt;
            }
            QMenu::item {
                color: #24292f;
                padding: 6px 26px 6px 10px;
                margin: 1px 2px;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background: #1668a8;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #a7afb8;
                background: transparent;
            }
        """)

    def _on_flow_double_clicked(self):
        """双击流程条目 = 编辑流程（分组头双击由按钮自身处理）。"""
        item = self.list.currentItem()
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if data and data[0] == "flow":
            self._edit_flow()

    # ---------- 流程导入 / 导出 ----------
    def _import_flow(self):
        start = FLOWS_DIR if os.path.isdir(FLOWS_DIR) else BASE_DIR
        path, _ = QFileDialog.getOpenFileName(self, "导入流程", start,
                                              "流程文件 (*.json);;所有文件 (*)")
        if not path:
            return
        self.import_flow_file(path)

    def import_flow_file(self, path: str) -> bool:
        """从文件导入一个流程；格式非法时提示并返回 False。

        供「导入流程」按钮与外部文件拖放共用。
        """
        flow = flow_from_file(path)
        if flow is None:
            QMessageBox.warning(self, "导入失败",
                                "所选文件不是有效的流程文件（版本不兼容或内容已损坏）。")
            return False
        flow.id = uuid.uuid4().hex[:12]   # 分配新 id，避免与现有流程/流程文件冲突
        flow.created_seq = self._next_flow_seq()   # 导入的流程同样按创建顺序排到末尾
        self._flows.append(flow)
        self.refresh_list()
        self._select_flow_item(flow.id)
        self.changed.emit()
        self._status_msg(f"已导入流程「{flow.name}」（{len(flow.steps)} 步）", 5000)
        return True

    def _export_flow(self):
        flow = self._selected_flow()
        if flow is None:
            self._status_msg("请先在左侧列表选择要导出的流程", 4000)
            return
        default = os.path.join(BASE_DIR, f"{safe_filename(flow.name)}.json")
        path, _ = QFileDialog.getSaveFileName(self, "导出流程", default,
                                              "流程文件 (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(flow_to_dict(flow), f, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "导出失败", f"无法写入文件：{e}")
            return
        self._status_msg(f"已导出「{flow.name}」→ {path}", 6000)

    def _toggle_selected(self):
        flow = self._selected_flow()
        if flow is None:
            sb = self.window().statusBar()
            if hasattr(sb, "showMessage"):
                sb.showMessage("请先在左侧列表选择一个流程", 4000)
            return
        self.toggle_flow(flow.id)

    # ---------- 运行控制 ----------
    def toggle_flow(self, flow_id: str):
        """点「运行/停止」：运行中→停止；排队中→取消排队；否则按执行方式启动或排队。

        特例：流程「已收到停止请求但线程还在收尾」时，再点一次理解为**重新启动**
        （登记 _restart_pending，收尾后自动起）——否则频繁启停时这一下会被当成
        又停一次，用户看到的是「按了运行没反应」。
        """
        flow = self._flow_by_id(flow_id)
        if flow is None:
            return
        runner = self._runners.get(flow_id)
        if runner and runner.is_running:
            if runner.stopping:
                self._restart_pending.add(flow_id)
                self._status_msg(f"「{flow.name}」刚刚停止，正在收尾，稍后自动重新启动", 5000)
                return
            runner.stop()
            return
        if flow_id in self._queue:
            # 排队中再点一次 = 取消排队（与「运行中再点一次 = 停止」对称）
            self._dequeue(flow_id, "已取消排队")
            return
        if not flow.steps:
            QMessageBox.information(self, "无法运行", "流程还没有步骤，请先在右侧拖入模块。")
            return
        self._run_source = ("single", flow_id)
        if self._should_queue(flow):
            self._enqueue(flow)
            return
        self._launch_flow(flow)

    def _launch_flow(self, flow: Flow):
        """真正启动流程（直接启动与排队放行共用这一条路径）。"""
        runner = FlowRunner(_clone_flow(flow))   # 深拷贝：steps 不能和界面共享
        flow_id = flow.id
        runner.stateChanged.connect(lambda state, reason, ok, fid=flow_id:
                                    self._on_state(fid, state, reason, ok))
        runner.stepStarted.connect(lambda idx, name, fid=flow_id:
                                   self._on_step_started(fid, idx, name))
        self._runners[flow_id] = runner
        self.flowStarted.emit(any(st.type == "status_log" for st in flow.steps))
        if not runner.start():
            # 起不来（运行中竞态 / 无步骤）：别在 _runners 里留个假条目
            self._runners.pop(flow_id, None)

    # ---------- 执行方式：同步排队 / 异步并行 ----------
    def _flow_by_id(self, flow_id: str) -> Flow | None:
        return next((f for f in self._flows if f.id == flow_id), None)

    def _any_running(self) -> bool:
        """是否有流程正在运行（含单步执行）。"""
        return any(r.is_running for r in self._runners.values())

    def _should_queue(self, flow: Flow) -> bool:
        """这次启动要不要排队。

        同步流程（默认）与「任何正在运行的流程」互斥：只要还有流程在跑就排队；
        异步流程从不排队，立即开始。
        """
        if flow.async_run:
            return False
        return self._any_running()

    def _enqueue(self, flow: Flow) -> None:
        """同步流程排到队尾，等前面的流程结束再由 _drain_queue 自动放行。"""
        if flow.id in self._queue:
            return
        self._queue.append(flow.id)
        pos = self._queue.index(flow.id) + 1
        busy = self.running_names()
        tail = f"：等「{'」「'.join(busy)}」结束后自动开始" if busy else ""
        log(f"流程「{flow.name}」已加入排队（第 {pos} 位）{tail}")
        self._status_msg(f"「{flow.name}」已加入排队（第 {pos} 位）", 5000)
        self._touch_queued_items()
        self.runningStateChanged.emit()
        self._reload_steps()

    def _dequeue(self, flow_id: str, why: str = "") -> bool:
        """把某个排队中的流程移出队列；不在队列里返回 False。"""
        if flow_id not in self._queue:
            return False
        self._queue.remove(flow_id)
        flow = self._flow_by_id(flow_id)
        log(f"流程「{flow.name if flow else flow_id}」{why or '已取消排队'}")
        self._touch_queued_items({flow_id})
        self.runningStateChanged.emit()
        self._reload_steps()
        return True

    def _cancel_queue(self, why: str = "") -> int:
        """清空整个队列（紧急停止全部用），返回被清掉的条数。"""
        if not self._queue:
            return 0
        ids = list(self._queue)
        names = [f.name for f in (self._flow_by_id(i) for i in ids) if f is not None]
        self._queue.clear()
        if names:
            log(f"{why or '已取消排队'}：{'、'.join('「' + n + '」' for n in names)}")
        self._touch_queued_items(set(ids))
        self.runningStateChanged.emit()
        self._reload_steps()
        return len(ids)

    def _touch_queued_items(self, extra_ids=None) -> None:
        """刷新排队相关条目：位次会随出队变化，队列里其余人也得重画。"""
        for fid in set(extra_ids or ()) | set(self._queue):
            self._update_left_item(fid)

    def _drain_queue(self) -> None:
        """流程结束后推进排队：按顺序放行下一个能跑的。

        异步流程不会被排队（见 _should_queue），但排队期间用户可能把某个流程改成
        「异步」——那它就不该再等，先放行。同步流程严格保证一次只跑一个。
        """
        if self._draining:
            return
        self._draining = True
        try:
            self._release_queued_async()
            if self._any_running():
                return               # 还有流程在跑（异步/单步）：同步流程继续等
            while self._queue:
                flow = self._take_next_queued()
                if flow is None:
                    continue         # 流程已删 / 已空：丢弃这个，看下一个
                self._launch_flow(flow)
                return
        finally:
            self._draining = False

    def _release_queued_async(self) -> None:
        """排队期间被改成「异步」的流程不必再等，直接放行。"""
        for fid in list(self._queue):
            flow = self._flow_by_id(fid)
            if flow is None or not flow.async_run:
                continue
            self._dequeue(fid, "已改为异步执行，不再排队")
            if flow.steps:
                self._launch_flow(flow)

    # ---------- 批量执行：整组 / 全部异步（2026-09-22） ----------
    def flows_in_group(self, group: str) -> list[Flow]:
        """某分组下的流程（左栏顺序）；group="" 表示「未分组」。"""
        return [f for f in self._flows if (f.group or "") == (group or "")]

    def run_flows(self, flows, why: str = "") -> int:
        """批量启动一组流程，返回实际「启动 + 排队」的条数。

        语义与单个运行完全一致（复用 _should_queue）：**异步流程立即并行**，
        同步流程遇忙排队（保证一次只跑一个）。已在运行、已在排队、没步骤的跳过。
        绝不另建 FlowRunner——那会漏掉排队门控与信号接线（见 test_flow_async_queue）。
        """
        started = 0
        for flow in flows:
            if not flow.steps:
                continue
            runner = self._runners.get(flow.id)
            if runner and runner.is_running:
                # 正在收尾的：记下「用户想让它跑」，等它结束后自动起（频繁启停不丢启动）
                if runner.stopping:
                    self._restart_pending.add(flow.id)
                    started += 1
                continue
            if flow.id in self._queue:
                continue
            if self._should_queue(flow):
                self._enqueue(flow)
            else:
                self._launch_flow(flow)
            started += 1
        if started and why:
            n_async = sum(1 for f in flows if f.async_run)
            log(f"{why}：已启动 {started} 个流程（异步 {n_async} 个可并行，其余排队执行）")
        return started

    def run_group(self, group: str) -> int:
        """执行某分组下的全部流程：异步的并行、同步的排队。"""
        name = group or "未分组"
        flows = self.flows_in_group(group)
        if not flows:
            self._status_msg(f"「{name}」分组下还没有流程", 4000)
            return 0
        self._run_source = ("group_all", name)
        n = self.run_flows(flows, why=f"运行分组「{name}」")
        if n:
            n_async = sum(1 for f in flows if f.async_run)
            self._status_msg(f"「{name}」：已启动 {n} 个流程"
                             f"（异步 {n_async} 个并行，其余排队）", 7000)
        else:
            self._status_msg(f"「{name}」下的流程都已在运行或排队中", 5000)
        return n

    def run_group_async(self, group: str) -> int:
        """运行某分组下勾选「异步执行」的所有流程（分组热键 / 按钮共用这一个入口）。

        只启动**本组的异步流程**：本组的同步流程完全不动，别的分组也不受影响。
        """
        name = group or "未分组"
        flows = [f for f in self.flows_in_group(group) if f.async_run]
        if not flows:
            log(f"分组「{name}」没有勾选「异步执行」的流程，未启动任何流程")
            self._status_msg(f"「{name}」里没有勾选「异步执行」的流程", 5000)
            return 0
        self._run_source = ("group_async", name)
        n = self.run_flows(flows, why=f"运行分组「{name}」的异步流程")
        if n:
            self._status_msg(f"「{name}」：已并行启动 {n} 个异步流程", 6000)
        else:
            self._status_msg(f"「{name}」的异步流程都已在运行中", 5000)
        return n

    def group_active_flows(self, group: str) -> list[Flow]:
        """该分组下「活动」的流程：正在运行 + 正在排队。"""
        out = []
        for f in self.flows_in_group(group):
            runner = self._runners.get(f.id)
            if (runner and runner.is_running) or f.id in self._queue:
                out.append(f)
        return out

    def stop_group(self, group: str, only_async: bool = False) -> int:
        """停止该分组下的活动：运行中的停掉、排队中的取消排队，返回处理条数。

        - **排队中的一律取消**（不论同步/异步）：排队意味着"还没开始跑"，取消它不算
          "停了同步流程的工作"。反过来，如果只停异步却留着排队的同步，异步一停队列
          立刻被 `_drain_queue` 放行、同步流程马上开跑——用户按"停止"却看到有流程跑
          起来，就是这个 bug（2026-09-22 用户反馈「再按热键停止不生效」）。
        - **运行中的**才看 only_async：分组热键的停止语义是"停本组的异步流程"，
          正在跑的同步流程不归它管。
        - 只动本分组的流程，别的分组完全不受影响。
        """
        name = group or "未分组"
        stopped = 0
        for f in self.flows_in_group(group):
            runner = self._runners.get(f.id)
            if runner and runner.is_running:
                if only_async and not f.async_run:
                    continue                       # 运行中的同步流程：热键不碰
                runner.stop()
                self._restart_pending.discard(f.id)   # 明确要停，取消待重启
                stopped += 1
            elif self._dequeue(f.id, f"分组「{name}」停止：已取消排队"):
                stopped += 1
        if stopped:
            what = "异步流程" if only_async else "流程"
            log(f"已停止分组「{name}」的 {stopped} 个{what}")
            self._status_msg(f"已停止「{name}」的 {stopped} 个{what}", 6000)
        else:
            self._status_msg(f"「{name}」当前没有正在运行的流程", 4000)
        return stopped

    def _is_stopping(self, flow_id: str) -> bool:
        """该流程是否「已收到停止请求、线程还在收尾」。"""
        runner = self._runners.get(flow_id)
        return bool(runner and runner.stopping)

    def toggle_group_async(self, group: str) -> int:
        """分组热键的开关语义：本组的异步流程在跑/排队 → 停止；否则运行本组异步流程。

        只看**异步**流程（用户要的是"单独运行当前每个分组下的所有异步流程"）；
        同步流程既不会被它启动，也不会被它停掉。
        「正在收尾」的流程不算活动——用户在快速连按时，这一下该理解为"再跑一次"，
        否则会被当成又停一次，表现为"按热键没反应"。
        （分组编辑页的「▶ 运行本组全部流程」按钮仍是整组语义，只启动不做开关。）
        """
        active = [f for f in self.group_active_flows(group)
                  if f.async_run and not self._is_stopping(f.id)]
        if active:
            return self.stop_group(group, only_async=True)
        return self.run_group_async(group)

    def flow_status_text(self, flow: Flow) -> str:
        """流程状态的短文本（分组编辑页展示用）：运行中 / 排队第 N 位 / 空串。"""
        runner = self._runners.get(flow.id)
        if runner and runner.is_running:
            return "运行中"
        if flow.id in self._queue:
            return f"排队第 {self._queue.index(flow.id) + 1} 位"
        return ""

    def rename_group_interactive(self, group: str) -> bool:
        """重命名的对外入口（分组编辑页复用同一套改名 + 归属/状态迁移逻辑）。

        返回是否真的改名了：用户取消、重名、名字没变都返回 False。
        """
        before = list(self.cfg.flow_groups)
        self._rename_group(group)
        return list(self.cfg.flow_groups) != before

    def open_group_dialog(self, group: str) -> None:
        """打开分组编辑页：查看本组流程、整组运行 / 运行本组异步流程、设置本分组热键。"""
        if not group:
            self._status_msg("「未分组」不是可编辑的分组，请先给流程指定分组", 5000)
            return
        GroupRunDialog(self, group, self).exec()

    def _take_next_queued(self) -> Flow | None:
        """取出队首流程；已被删除或没步骤的丢弃（返回 None 让调用方继续下一个）。"""
        flow_id = self._queue.pop(0)
        flow = self._flow_by_id(flow_id)
        if flow is None or not flow.steps:
            log(f"排队中的流程「{flow.name if flow else flow_id}」已不可运行，跳过")
            self._touch_queued_items({flow_id})
            return None
        self._touch_queued_items({flow_id})
        return flow

    def stop_flow(self, flow_id: str):
        runner = self._runners.get(flow_id)
        if runner and runner.is_running:
            runner.stop()

    def start_flow_if_idle(self, flow_id: str, silent: bool = False) -> bool:
        """若流程未在运行/排队则启动，返回是否已启动（含排队）；供定时任务等外部调度触发。

        与 toggle_flow 的区别：已在运行时不停止，而是直接跳过并返回 False，
        避免定时触发把用户手动运行中的流程误停。同步流程遇忙仍会正常排队，
        所以返回 True 时可能只是「已排队」——调用方可用 is_queued() 区分。

        silent=True 表示「无人值守触发」（定时任务等）：本次运行结束失败时
        不弹模态框打断用户，只走状态栏提示与日志；用户手动运行不受影响。
        """
        flow = self._flow_by_id(flow_id)
        if flow is None or not flow.steps:
            return False
        runner = self._runners.get(flow_id)
        if runner and runner.is_running:
            return False
        if flow_id in self._queue:
            return False     # 同一个流程已在排队：不重复入队（定时任务每次触发只排一次）
        if silent:
            self._silent.add(flow_id)
        self.toggle_flow(flow_id)
        return True

    def is_queued(self, flow_id: str) -> bool:
        """该流程是否正在排队（还没开始跑，等前面的流程结束）。"""
        return flow_id in self._queue

    def queued_names(self) -> list[str]:
        """排队中的流程名（按排队顺序），给状态栏等外部模块显示用。"""
        return [f.name for f in (self._flow_by_id(i) for i in self._queue)
                if f is not None]

    def _status_msg(self, text: str, ms: int):
        """向主窗口状态栏发轻提示（无状态栏时静默跳过）。"""
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, ms)

    def _run_single_step(self, row: int):
        """单步执行：只运行步骤列表中的某一步。

        构造一个只含该步骤、loops=1 的临时流程复用 FlowRunner 线程，
        并登记到 _runners —— 运行期间左栏状态/右栏锁定/停止按钮/全停热键全部照常生效。

        单步执行是「编辑时试跑」，不走同步排队（排队了用户按下没反应更费解）：
        本流程在排队中时先要求取消排队，否则 runner 会被随后放行的整流程覆盖。
        """
        flow = self._selected_flow()
        if flow is None or not (0 <= row < len(flow.steps)):
            return
        if self._selected_running():
            self._status_msg("流程运行中，请先停止后再单步执行", 4000)
            return
        if flow.id in self._queue:
            self._status_msg("该流程正在排队，请先取消排队再单步执行", 4000)
            return
        if self._any_running():
            self._status_msg("已有其它流程在运行，本次单步执行将与其并行", 4000)
        self._run_source = ("single", flow.id)
        step = flow.steps[row]
        # 浅拷贝参数字典，避免执行线程与编辑中的步骤共享同一 dict
        step_copy = FlowStep(type=step.type, name=step.name, params=dict(step.params),
                             continue_on_fail=step.continue_on_fail, pair_id=step.pair_id)
        single_flow = Flow(id=flow.id, name=flow.name, steps=[step_copy], loops=1)
        runner = FlowRunner(single_flow)
        runner.stateChanged.connect(lambda state, reason, ok, fid=flow.id, name=step.name:
                                    self._on_single_state(fid, state, reason, ok, name))
        runner.stepStarted.connect(lambda idx, name, fid=flow.id:
                                   self._on_step_started(fid, idx, name))
        self._single_rows[flow.id] = row
        self._runners[flow.id] = runner
        log(f"单步执行「{flow.name}」步骤 {row + 1}：{step.name}")
        self.flowStarted.emit(step.type == "status_log")
        runner.start()

    def _on_single_state(self, flow_id: str, state: str, reason: str,
                         ok: bool, step_name: str):
        """单步执行的状态回调：成败以步骤本身结果为准（勾选“失败继续”的超时也算失败）。"""
        runner = self._runners.get(flow_id)
        if state == "running":
            self._update_left_item(flow_id)
        else:
            self._single_rows.pop(flow_id, None)
            failed = reason != "已手动停止" and not (runner and runner.last_step_ok)
            self._update_left_item(flow_id, failed=failed)
            flow = next((f for f in self._flows if f.id == flow_id), None)
            if flow is not None:
                if reason == "已手动停止":
                    self._status_msg(f"「{flow.name}」单步执行已停止", 4000)
                elif failed:
                    why = runner.last_step_reason if runner else reason
                    QMessageBox.information(self, "单步执行失败",
                                            f"「{flow.name}」· {step_name}\n{why}")
                else:
                    self._status_msg(f"「{flow.name}」单步执行完成：{step_name}", 6000)
            self._runners.pop(flow_id, None)   # 单步跑完，摘掉 runner
            self._drain_queue()                # 单步也算「有流程在跑」，跑完推进排队
        self.runningStateChanged.emit()
        self._reload_steps()

    def stop_all(self):
        """紧急停止：先清空排队，再停掉所有正在运行的流程。"""
        self._cancel_queue("停止全部：已取消排队")
        self._restart_pending.clear()      # 紧急停止要的是"都停下"，取消待重启意图
        for r in self._runners.values():
            if r.is_running:
                r.stop()

    def any_running(self) -> bool:
        return any(r.is_running for r in self._runners.values())

    def running_names(self) -> list[str]:
        """当前正在运行的流程名（给主窗口等外部模块查状态用）。

        外部不要直接读 _runners：那是本 tab 的内部实现细节，依赖它会让以后
        调整 runner 存储方式时牵连到别的模块。
        """
        names = []
        for f in self._flows:
            runner = self._runners.get(f.id)
            if runner is not None and runner.is_running:
                names.append(f.name)
        return names

    def running_overview(self) -> dict:
        """左上角悬浮窗用的结构化状态。

        返回：
          source: "group_async" | "group_all" | "single" | ""   （执行来源，空=无）
          group:  分组名（group_* 时有值，single/空 时为空串）
          flows:  [(流程名, 热键显示串, 是否异步, 分组名), ...] 按左栏顺序，只含正在运行的流程
                  （分组名让浮层能把「分组 - 流程」显示在一行，见 running_overlay）
        热键显示串规则（用户要求「用中括号标识热键，没有就写无」）：
          分组来源 → 取该分组的 group_hotkeys[name]（没有则"无"）；
          单个流程 → 取流程自身的 hotkey（没有则"无"）。
        """
        source = ""
        group = ""
        if self._run_source is not None:
            kind, val = self._run_source
            source = kind
            if kind in ("group_async", "group_all"):
                group = val
        flows = []
        for f in self._flows:
            runner = self._runners.get(f.id)
            if runner is None or not runner.is_running:
                continue
            if source in ("group_async", "group_all"):
                hk = self.group_hotkey(f.group) or ""
            else:
                hk = f.hotkey or ""
            flows.append((f.name, hk, bool(f.async_run), f.group or ""))
        return {"source": source, "group": group, "flows": flows}

    # ---------- 状态回调 ----------
    def _on_step_started(self, flow_id: str, idx: int, name: str):
        self._update_left_item(flow_id)
        self._reload_steps()

    def _on_state(self, flow_id: str, state: str, reason: str, ok: bool = True):
        flow = next((f for f in self._flows if f.id == flow_id), None)
        if state == "running":
            self._update_left_item(flow_id)
        else:
            silent = flow_id in self._silent
            if silent:
                self._silent.discard(flow_id)   # 一次运行结束即摘除标记
            self._runners.pop(flow_id, None)   # 跑完就摘掉，别让字典越攒越大
            failed = bool(reason) and not ok and reason != "已手动停止"
            self._update_left_item(flow_id, failed=failed)
            # 先推进排队再弹提示：失败的模态框不该把队列卡住
            self._drain_queue()
            # 「停止中又被点了运行」：这次收尾完成后立刻重新启动（频繁启停不丢启动）
            restart = (flow_id in self._restart_pending
                       and flow is not None and bool(flow.steps))
            self._restart_pending.discard(flow_id)
            if restart:
                self._launch_flow(flow)
                self._status_msg(f"「{flow.name}」已重新启动", 4000)
            elif flow is None:
                pass
            elif failed and not silent:
                # 手动运行失败：弹窗反馈；静默（定时任务）触发不弹窗打扰
                QMessageBox.information(self, "流程结束", f"「{flow.name}」：{reason}")
            elif reason:
                # 成功完成 / 静默运行失败：只做状态栏轻提示（步骤很快跑完也能看到结果）
                self._status_msg(f"「{flow.name}」{reason}", 6000)
        # 没有任何流程在运行/排队时，执行来源归 None（左上角悬浮窗据此隐藏/复位）
        if not self._any_running() and not self._queue:
            self._run_source = None
        self.runningStateChanged.emit()
        self._reload_steps()

    # ---------- 退出 ----------
    def shutdown(self):
        self.stop_all()
