# -*- coding: utf-8 -*-
"""内置小程序：任务看板（看板式任务管理，数据永久保存在本地）。

能做什么
--------
- 三列看板：**待办 / 进行中 / 已完成**（默认就是这三列，不提供「新建列」）；
  三列**平分窗口宽度并随窗口动态伸缩**（窗口窄到放不下才横向滚动）；
  列可以重命名、左右移动、标记为完成列、清空，也可以删除（列菜单「删除本列…」，
  最后一列不给删）；
- 任务可**拖动**：拖到别的列即改状态，拖到列内某个位置即调整顺序；
- 任务字段：标题、备注、优先级（高/中/低 = 红/绿/灰实心徽标）、截止日期；
  双击编辑、右键菜单、Delete 删除；卡片上备注**最多显示一行**（完整内容看悬停提示）；
- 顶部搜索框按标题/备注/优先级实时筛选；
- 底部状态栏实时显示任务总数与已完成数。

数据存哪
--------
`<BASE_DIR>/task_board.json`（BASE_DIR 见 `app/config.py`：源码运行 = 项目根目录，
打包后 = exe 所在目录）。**每一次改动立刻落盘**，采用「先写 .tmp 再 os.replace」
的原子写，写到一半崩溃也不会留下半个 json（同 config.py 的做法）。
文件损坏/被手改坏时退回默认看板，不会让小程序打不开。

**外部改动优先**：窗口开着时每 1.5 秒比对一次磁盘内容，发现文件被外部改过
（手改 json、另一份程序在跑）就按磁盘内容重新载入，绝不拿内存里的旧状态盖回去；
关窗时也不再无条件重写文件（改动本来就即时落盘）。

为什么要自己实现拖放，而不是用 QListWidget 自带的 InternalMove
--------------------------------------------------------------
本页的顺序**以 store（Python 数据）为准**：任何改动都改 store、落盘、再重建视图。
而 Qt 自带的 InternalMove 会在拖放结束时自己再动一遍视图内部的模型——两套状态
各改各的，顺序必然错位（还会出现「拖一下少一行」）。所以这里：
- 列表 `setDragEnabled(False)`，鼠标按住移动时自己 `QDrag.exec()`；
- mime 里只放**任务 id**，落点用「锚点任务 id + 在其前/后」表达（不是行号，
  因为搜索会把部分行藏起来，行号会错位）；
- 看板收到 (任务 id, 目标列, 锚点, 前后) 后改 store、落盘、重建。

重建视图必须在**拖放彻底结束之后**：`QDrag.exec()` 是个嵌套事件循环，
在 dropEvent 里同步重建会把正在拖动的那个列表控件删掉，控件回到自己的
mouseMoveEvent 时就是一个悬空对象。所以 drop 只置一个 pending 标记，
等拖动方在 `exec()` 返回后通知看板（`TaskBoardWidget.end_drag`）再异步重建一次。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from PySide6.QtCore import (QDate, QMimeData, QPoint, QRect, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtGui import (QColor, QDrag, QFont, QFontMetrics, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                               QFormLayout, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from .. import config as config_mod
from ..ui import theme
from ..ui.widgets import set_variant
from . import MiniApp, register

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BOARD_FILENAME = "task_board.json"
BOARD_VERSION = 1
TASK_MIME = "application/x-qf-task-board-task"

# 优先级：(值, 显示名)。值要持久化，**发布后不要改**；显示名随便改。
PRIORITIES: tuple[tuple[str, str], ...] = (
    ("high", "高"),
    ("normal", "中"),
    ("low", "低"),
)
PRIORITY_LABELS = dict(PRIORITIES)
DEFAULT_PRIORITY = "normal"
# 优先级 -> 主题令牌：**高=红、中=绿、低=灰**（左侧色条与右侧徽标底色共用同一套）
PRIORITY_TOKENS = {"high": "danger", "normal": "success", "low": "text_muted"}

MIN_COLUMN_W = 210          # 列的最小宽度；**正常状态下列会平分窗口宽度**（见 reload 的 stretch）
CARD_PAD = 10               # 卡片内左右留白
CARD_H = 54                 # 卡片基础高度（标题行 + 元信息行 + 上下留白）
NOTE_LINE_H = 15            # 备注每行的高度
NOTE_MAX_LINES = 1          # 卡片上最多显示几行备注（超出部分用「…」收尾，完整内容看悬停提示）
BADGE_H = 18                # 优先级徽标高度（底色块，比文字宽出一圈）
BADGE_PAD_X = 9             # 徽标内左右留白
PLACEHOLDER_H = 44          # 空列占位提示的高度
TITLE_MAX = 200             # 标题长度上限（对话框里限制输入）
DISK_SYNC_MS = 1500         # 窗口可见时每隔多久比对一次磁盘数据（见 _sync_from_disk）

# 默认三列：id 固定成 todo/doing/done，方便脚本/测试引用；名字用户随时能改。
DEFAULT_COLUMN_IDS = (("todo", "待办"), ("doing", "进行中"), ("done", "已完成"))


# ---------------------------------------------------------------------------
# 小工具函数
# ---------------------------------------------------------------------------
def board_path() -> str:
    """看板数据文件路径。

    ⚠️ 必须**每次调用时**读 `config_mod.BASE_DIR`（而不是 import 期缓存成常量）：
    测试用 `tests/_env.TempConfigPaths` 换掉 BASE_DIR 之后要能立刻生效，
    打包成 exe 后 BASE_DIR 又是 exe 所在目录。
    """
    return os.path.join(config_mod.BASE_DIR, BOARD_FILENAME)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_due(value) -> str:
    """把截止日期规整成 `YYYY-MM-DD`；空值/非法值一律变成空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def task_matches(task: "Task", kw: str) -> bool:
    """任务是否命中搜索词（标题 / 备注 / 优先级显示名，都不区分大小写）。"""
    hay = f"{task.title}\n{task.note}\n{task.priority_label}".lower()
    return kw in hay


def _looks_like_board(text: str) -> bool:
    """外部同步前的安全闸：只接受「能解析成 json 且 columns 是列表」的内容。

    为什么要这么严：用户手改 json 到一半时文件是**半截**的。若把它当坏数据回落到
    默认看板，下一次保存就会把用户文件彻底盖成默认三列 —— 那才是真丢数据。
    宁可这一轮不动，等用户写完再同步。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and isinstance(data.get("columns"), list)


def wrap_lines(text: str, fm: QFontMetrics, width: int,
               max_lines: int) -> list[str]:
    """按像素宽度把文本折成最多 `max_lines` 行；放不下的部分在最后一行用「…」收尾。

    为什么要自己折：Qt 的 `drawText` 只能「自动换行」**或者**「省略号」，两者不能同时用；
    而卡片上的备注既要限制行数、又要保证不溢出。中文没有词边界，所以按**字符**推进；
    英文尽量在空格处断开，避免把单词劈成两半。

    同一个函数同时供 `sizeHint`（算卡片多高）和 `paint`（画第几行）使用——
    两边必须用同一套折行结果，否则会出现「算出来两行、画出来三行」的错位。
    """
    if width <= 0 or max_lines <= 0:
        return []
    rest = str(text or "").strip()
    if not rest:
        return []
    lines: list[str] = []
    while rest and len(lines) < max_lines:
        if fm.horizontalAdvance(rest) <= width:
            lines.append(rest)
            return lines
        lo, hi = 1, len(rest)                 # 二分找这一行放得下的最长前缀
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fm.horizontalAdvance(rest[:mid]) <= width:
                lo = mid
            else:
                hi = mid - 1
        cut = max(1, lo)
        space = rest.rfind(" ", 0, cut)       # 英文尽量不劈开单词
        if space >= max(1, int(cut * 0.6)):
            cut = space
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest and lines:                        # 还有剩 -> 最后一行省略号收尾
        lines[-1] = fm.elidedText(lines[-1] + rest, Qt.ElideRight, width)
    return lines


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Task:
    """一个任务卡片。字段名即 json 里的键（持久化契约）。"""

    id: str = ""
    title: str = ""
    note: str = ""
    priority: str = DEFAULT_PRIORITY
    due: str = ""               # "YYYY-MM-DD"，空 = 没设截止
    created: str = ""           # "YYYY-MM-DD HH:MM:SS"

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.title = str(self.title or "").strip()
        self.note = str(self.note or "").strip()
        if self.priority not in PRIORITY_LABELS:
            self.priority = DEFAULT_PRIORITY
        self.due = _clean_due(self.due)
        self.created = str(self.created or "").strip() or _now_text()

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS.get(self.priority, PRIORITY_LABELS[DEFAULT_PRIORITY])

    def is_overdue(self) -> bool:
        """截止日期已经过去（是否算「未完成」由所在列决定，模型不管）。"""
        return bool(self.due) and self.due < date.today().isoformat()


@dataclass
class Column:
    """看板的一列。`done=True` 的列视作「完成列」（卡片标题加删除线，可一键清理）。"""

    id: str = ""
    name: str = ""
    done: bool = False
    tasks: list = field(default_factory=list)      # list[Task]

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        self.name = str(self.name or "").strip() or "未命名列"
        self.done = bool(self.done)
        self.tasks = [t for t in self.tasks if isinstance(t, Task)]


def default_columns() -> list[Column]:
    """首次运行（或数据坏了）时的默认看板。"""
    return [Column(id=cid, name=name, done=(cid == "done"))
            for cid, name in DEFAULT_COLUMN_IDS]


# ---------------------------------------------------------------------------
# 存储 + 数据操作（纯 Python，不碰 Qt —— 可单独测）
# ---------------------------------------------------------------------------
class TaskBoardStore:
    """看板数据与全部改动操作；每次改动由调用方 `save()` 落盘。"""

    def __init__(self, path: str | None = None):
        self.path = path or board_path()
        self.columns: list[Column] = []
        self.load()

    # ---------- 载入 / 保存 ----------
    def load(self) -> None:
        """从磁盘载入；文件不存在/读不动/格式不对 -> 默认看板（不抛异常）。"""
        first_run = not os.path.exists(self.path)
        data = None
        if not first_run:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                log.warning("任务看板数据读取失败，改用默认看板: %s", self.path,
                            exc_info=True)
                data = None
        self.columns = self._parse(data)
        if first_run:
            self.save()      # 首次运行落一份默认看板：用户一眼能看到数据存在哪

    def _parse(self, data) -> list[Column]:
        if not isinstance(data, dict):
            return default_columns()
        raw = data.get("columns")
        if not isinstance(raw, list):
            return default_columns()
        columns: list[Column] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            col = Column(id=str(item.get("id") or ""),
                         name=str(item.get("name") or ""),
                         done=bool(item.get("done", False)))
            for t in (item.get("tasks") or []):
                if not isinstance(t, dict):
                    continue
                title = str(t.get("title") or "").strip()
                if not title:                 # 没有标题的任务没有意义，丢掉
                    continue
                col.tasks.append(Task(id=str(t.get("id") or ""), title=title,
                                      note=str(t.get("note") or ""),
                                      priority=str(t.get("priority") or ""),
                                      due=str(t.get("due") or ""),
                                      created=str(t.get("created") or "")))
            columns.append(col)
        return columns or default_columns()

    def to_dict(self) -> dict:
        return {
            "version": BOARD_VERSION,
            "columns": [{"id": c.id, "name": c.name, "done": c.done,
                         "tasks": [asdict(t) for t in c.tasks]}
                        for c in self.columns],
        }

    def save(self) -> bool:
        """原子写 json（先 .tmp 再 os.replace）；失败只记日志，不打断界面操作。"""
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError:
            log.warning("任务看板保存失败: %s", self.path, exc_info=True)
            return False

    # ---------- 查询 ----------
    def find_column(self, cid: str) -> Column | None:
        cid = str(cid or "")
        for col in self.columns:
            if col.id == cid:
                return col
        return None

    def find_task(self, tid: str) -> tuple[Column, int, Task] | None:
        """按任务 id 找 `(所在列, 列内下标, 任务)`；找不到返回 None。"""
        tid = str(tid or "")
        for col in self.columns:
            for i, task in enumerate(col.tasks):
                if task.id == tid:
                    return col, i, task
        return None

    def task_count(self) -> int:
        return sum(len(c.tasks) for c in self.columns)

    def done_count(self) -> int:
        """完成列里的任务数（有多列被标成完成列时全部计入）。"""
        return sum(len(c.tasks) for c in self.columns if c.done)

    def match_count(self, kw: str) -> int:
        kw = (kw or "").strip().lower()
        if not kw:
            return self.task_count()
        return sum(1 for c in self.columns for t in c.tasks if task_matches(t, kw))

    # ---------- 列操作 ----------
    # 说明：`add_column` 是**数据层**能力（界面已不提供「新建列」，看板默认就是
    # 待办/进行中/已完成，见模块 docstring）；保留它是为了数据模型完整、便于将来恢复入口。
    # `delete_column` 界面仍有入口（列菜单「删除本列」）——否则误建/多余的列只能手改 json。
    def add_column(self, name: str = "新列", done: bool = False) -> Column:
        col = Column(name=name, done=done)
        self.columns.append(col)
        return col

    def rename_column(self, cid: str, name: str) -> bool:
        col = self.find_column(cid)
        name = str(name or "").strip()
        if col is None or not name or name == col.name:
            return False
        col.name = name
        return True

    def set_column_done(self, cid: str, done: bool) -> bool:
        col = self.find_column(cid)
        if col is None or col.done == bool(done):
            return False
        col.done = bool(done)
        return True

    def delete_column(self, cid: str) -> bool:
        """删列（连同列内任务）。**最后一列不允许删**：看板至少要有一列可放任务。"""
        if len(self.columns) <= 1:
            return False
        for i, col in enumerate(self.columns):
            if col.id == cid:
                del self.columns[i]
                return True
        return False

    def clear_column(self, cid: str) -> int:
        """清空一列，返回删掉的任务数。"""
        col = self.find_column(cid)
        if col is None or not col.tasks:
            return 0
        n = len(col.tasks)
        col.tasks = []
        return n

    def move_column(self, cid: str, delta: int) -> bool:
        """把一列左右移动 `delta` 格（越界不动），返回是否真的变了。"""
        idx = next((i for i, c in enumerate(self.columns) if c.id == cid), None)
        if idx is None:
            return False
        target = max(0, min(len(self.columns) - 1, idx + int(delta)))
        if target == idx:
            return False
        self.columns.insert(target, self.columns.pop(idx))
        return True

    # ---------- 任务操作 ----------
    def add_task(self, cid: str, title: str, note: str = "",
                 priority: str = DEFAULT_PRIORITY, due: str = "") -> Task | None:
        col = self.find_column(cid)
        title = str(title or "").strip()
        if col is None or not title:
            return None
        task = Task(title=title, note=note, priority=priority, due=due)
        col.tasks.append(task)
        return task

    def update_task(self, tid: str, **fields) -> bool:
        """改任务字段（只认 title/note/priority/due）。返回是否真的变了。"""
        found = self.find_task(tid)
        if found is None:
            return False
        task = found[2]
        changed = False
        if "title" in fields:
            title = str(fields["title"] or "").strip()
            if title and title != task.title:
                task.title = title
                changed = True
        if "note" in fields:
            note = str(fields["note"] or "").strip()
            if note != task.note:
                task.note = note
                changed = True
        if "priority" in fields:
            prio = str(fields["priority"] or "")
            if prio in PRIORITY_LABELS and prio != task.priority:
                task.priority = prio
                changed = True
        if "due" in fields:
            due = _clean_due(fields["due"])
            if due != task.due:
                task.due = due
                changed = True
        return changed

    def delete_task(self, tid: str) -> bool:
        found = self.find_task(tid)
        if found is None:
            return False
        col, idx, _task = found
        del col.tasks[idx]
        return True

    def move_task(self, tid: str, to_cid: str, anchor_tid: str | None = None,
                  after: bool = False) -> bool:
        """把任务移到 `to_cid` 列，插在 `anchor_tid` 前/后（锚点为空 = 排到末尾）。

        用**锚点任务 id** 而不是行号：搜索过滤会把部分行藏起来，行号会错位；
        任务 id 与过滤无关，落点永远对得上。返回是否真的动了位置。
        """
        found = self.find_task(tid)
        target = self.find_column(to_cid)
        if found is None or target is None or anchor_tid == tid:
            return False
        src_col, src_idx, task = found
        same = src_col is target
        before_ids = [t.id for t in target.tasks] if same else None

        anchor_idx = None
        if anchor_tid:
            anchor_idx = next((i for i, t in enumerate(target.tasks)
                               if t.id == anchor_tid), None)
        insert_at = (len(target.tasks) if anchor_idx is None
                     else (anchor_idx + 1 if after else anchor_idx))

        src_col.tasks.pop(src_idx)
        if same and src_idx < insert_at:      # 同列内：先删会把后面的下标整体前移
            insert_at -= 1
        insert_at = max(0, min(insert_at, len(target.tasks)))
        target.tasks.insert(insert_at, task)

        if same and before_ids == [t.id for t in target.tasks]:
            return False                      # 位置没变（拖回原处）：不算改动
        return True

    def move_task_by(self, tid: str, delta: int) -> bool:
        """任务在**本列内**上/下移 `delta` 格（右键菜单用）。"""
        found = self.find_task(tid)
        if found is None:
            return False
        col, idx, _task = found
        target = max(0, min(len(col.tasks) - 1, idx + int(delta)))
        if target == idx:
            return False
        col.tasks.insert(target, col.tasks.pop(idx))
        return True

    def clear_done(self) -> int:
        """清空所有「完成列」，返回清掉的任务数。"""
        n = 0
        for col in self.columns:
            if col.done:
                n += len(col.tasks)
                col.tasks = []
        return n


# ---------------------------------------------------------------------------
# 任务卡片自绘
# ---------------------------------------------------------------------------
class TaskCardDelegate(QStyledItemDelegate):
    """任务卡片：左侧优先级色条 + 标题 + 「截止日期 … 优先级徽标」+ 备注预览。

    备注只占**一行**（`NOTE_MAX_LINES`），放不下用「…」收尾，完整内容看悬停提示。

    优先级做成**实心底色徽标**并靠右（文字在后、色块大、红/绿/灰一眼可分），
    左侧同色细条只是再补一刀视觉锚点。

    自绘而不是塞 item widget：item widget 会吃掉鼠标事件，拖动就起不来了
    （要额外开 WA_TransparentForMouseEvents 才绕得过去），自绘没有这个问题，
    顺带能把选中/悬停/完成列的删除线画得很干净。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.done = False        # 由所在列决定：完成列的卡片标题加删除线

    # ---------- 字号与尺寸（sizeHint 与 paint 必须用同一套）----------
    @staticmethod
    def _fonts(base: QFont) -> tuple[QFont, QFont, QFont]:
        title = QFont(base)
        title.setBold(True)
        meta = QFont(base)
        meta.setPointSizeF(max(7.5, base.pointSizeF() - 1.0))
        badge = QFont(meta)
        badge.setBold(True)
        return title, meta, badge

    @staticmethod
    def _text_width(option_width: int) -> int:
        """卡片内的可用文字宽度（与 paint 里的算法一致：去掉两侧 2px 边距 + 左右留白）。"""
        return max(10, option_width - CARD_PAD - 12)

    def note_lines(self, task: Task, fm: QFontMetrics, option_width: int) -> list[str]:
        if not task.note:
            return []
        return wrap_lines(task.note.replace("\n", "  "), fm,
                          self._text_width(option_width), NOTE_MAX_LINES)

    def sizeHint(self, option, index):        # noqa: N802（Qt 命名）
        width = option.rect.width() if option.rect.width() > 0 else MIN_COLUMN_W
        task = index.data(Qt.UserRole)
        if task is None:
            return QSize(width, PLACEHOLDER_H)
        _title, meta_font, _badge = self._fonts(option.font)
        rows = len(self.note_lines(task, QFontMetrics(meta_font), width))
        return QSize(width, CARD_H + NOTE_LINE_H * rows)

    def paint(self, painter, option, index):  # noqa: N802（Qt 命名）
        task = index.data(Qt.UserRole)
        if task is None:
            self._paint_placeholder(painter, option, index)
            return
        t = theme.token
        rect = option.rect.adjusted(2, 2, -2, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 卡片底 + 边框（选中/悬停都提亮描边，一眼看出当前卡片）
        if selected:
            bg, border = t("primary_soft"), t("primary")
        elif hovered:
            bg, border = t("hover_bg"), t("primary")
        else:
            bg, border = t("card_bg"), t("border")
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(0, 0, -1, -1), 7, 7)
        painter.setPen(QPen(QColor(border), 1))
        painter.setBrush(QColor(bg))
        painter.drawPath(path)

        priority_color = QColor(t(PRIORITY_TOKENS.get(task.priority, "primary")))

        # 左侧优先级色条：裁进卡片圆角里，免得方角戳出圆角外
        painter.setClipPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(priority_color)
        painter.drawRect(rect.left(), rect.top(), 4, rect.height())
        painter.setClipping(False)

        title_font, meta_font, badge_font = self._fonts(option.font)
        title_font.setStrikeOut(self.done)
        fm_title = QFontMetrics(title_font)
        fm_meta = QFontMetrics(meta_font)
        fm_badge = QFontMetrics(badge_font)

        left = rect.left() + CARD_PAD
        width = max(10, rect.width() - CARD_PAD - 8)

        title = fm_title.elidedText(task.title or "(无标题)", Qt.ElideRight, width)
        painter.setFont(title_font)
        painter.setPen(QColor(t("text_muted") if self.done else t("text")))
        painter.drawText(QRect(left, rect.top() + 6, width, 19),
                         Qt.AlignLeft | Qt.AlignVCenter, title)

        # ---- 元信息行：截止日期在左，优先级徽标靠右（文字放后面、底色块大）----
        meta_top = rect.top() + 26
        label = task.priority_label
        badge_w = max(fm_badge.horizontalAdvance(label) + 2 * BADGE_PAD_X, BADGE_H + 10)
        badge_rect = QRect(rect.right() - 8 - badge_w, meta_top, badge_w, BADGE_H)
        painter.setPen(Qt.NoPen)
        painter.setBrush(priority_color)
        painter.drawRoundedRect(badge_rect, BADGE_H / 2.0, BADGE_H / 2.0)
        painter.setFont(badge_font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(badge_rect, Qt.AlignCenter, label)

        if task.due:
            painter.setFont(meta_font)
            painter.setPen(QColor(t("danger") if task.is_overdue()
                                  else t("text_muted")))
            avail = max(0, badge_rect.left() - left - 6)
            painter.drawText(
                QRect(left, meta_top, avail, BADGE_H),
                Qt.AlignLeft | Qt.AlignVCenter,
                fm_meta.elidedText(f"截止 {task.due}", Qt.ElideRight, avail))

        # ---- 备注：默认一行（NOTE_MAX_LINES），放不下在行尾用「…」收尾 ----
        rows = self.note_lines(task, fm_meta, option.rect.width())
        painter.setFont(meta_font)
        painter.setPen(QColor(t("text_muted")))
        y = rect.top() + 46
        for line in rows:
            painter.drawText(QRect(left, y, width, NOTE_LINE_H),
                             Qt.AlignLeft | Qt.AlignVCenter, line)
            y += NOTE_LINE_H

        painter.restore()

    def _paint_placeholder(self, painter, option, index):
        """空列 / 无匹配时的占位提示（不是任务，不可拖不可选）。"""
        text = index.data(Qt.DisplayRole) or ""
        if not text:
            return
        painter.save()
        font = QFont(option.font)
        font.setPointSizeF(max(8.0, option.font.pointSizeF() - 1.0))
        painter.setFont(font)
        painter.setPen(QColor(theme.token("text_muted")))
        painter.drawText(option.rect.adjusted(6, 0, -6, 0),
                         Qt.AlignCenter | Qt.TextWordWrap, text)
        painter.restore()


# ---------------------------------------------------------------------------
# 一列里的任务列表（自实现拖放）
# ---------------------------------------------------------------------------
class TaskListWidget(QListWidget):
    """单列任务列表：列内排序 + 跨列移动（拖放只传任务 id，顺序由 store 决定）。"""

    taskDropped = Signal(str, str, bool)        # 任务 id, 锚点任务 id, 插到其后
    taskActivated = Signal(str)                 # 任务 id（双击 -> 编辑）
    taskMenuRequested = Signal(str, QPoint)     # 任务 id, 全局坐标（右键 -> 菜单）
    taskDeleteRequested = Signal(str)           # 任务 id（Delete 键）

    def __init__(self, board, parent=None):
        super().__init__(parent)
        self.board = board
        self.setObjectName("taskList")
        self.setDragEnabled(False)      # 拖动自己实现（见 mouseMoveEvent）
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)   # 落点线自己画（见 paintEvent）
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setSpacing(0)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)   # 悬停高亮要它
        self.setItemDelegate(TaskCardDelegate(self))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.itemDoubleClicked.connect(self._on_double_clicked)
        self._press_pos = QPoint()
        self._drop_line: int | None = None

    # ---------- 落点线自绘 ----------
    def paintEvent(self, event):        # noqa: N802（Qt 命名）
        super().paintEvent(event)
        if self._drop_line is None:
            return
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor(theme.token("primary")), 2))
        y = max(1, min(int(self._drop_line), self.viewport().height() - 2))
        painter.drawLine(2, y, self.viewport().width() - 3, y)
        painter.end()

    # ---------- 拖动 ----------
    def mousePressEvent(self, event):   # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):    # noqa: N802
        if not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        if (event.position().toPoint() - self._press_pos).manhattanLength() \
                < QApplication.startDragDistance():
            return
        item = self.itemAt(self._press_pos)
        task = item.data(Qt.UserRole) if item is not None else None
        if task is None:                # 占位提示 / 空白：不给拖
            return
        self._begin_drag(task, event.position().toPoint())

    def _begin_drag(self, task: Task, pos: QPoint) -> None:
        mime = QMimeData()
        mime.setData(TASK_MIME, str(task.id).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap, hotspot = self._drag_visual(task, pos)
        if pixmap is not None:
            drag.setPixmap(pixmap)
            drag.setHotSpot(hotspot)
        board = self.board
        if board is not None:
            board.begin_drag()
        try:
            drag.exec(Qt.MoveAction)
        finally:
            # exec 返回 = 这次拖动彻底结束，这时才允许重建视图（见模块 docstring）
            self._drop_line = None
            self.viewport().update()
            if board is not None:
                board.end_drag()

    def _drag_visual(self, task: Task, pos: QPoint):
        """拖动时跟着鼠标的那张半透明卡片（抓不到就返回 None，不影响拖动）。"""
        item = self.item_for(task.id)
        if item is None:
            return None, QPoint()
        rect = self.visualItemRect(item)
        if rect.isEmpty():
            return None, QPoint()
        try:
            shot = self.viewport().grab(rect)
        except Exception:               # noqa: BLE001（离屏/无窗口时抓不到就算了）
            return None, QPoint()
        faded = QPixmap(shot.size())
        faded.fill(Qt.transparent)
        painter = QPainter(faded)
        painter.setOpacity(0.85)
        painter.drawPixmap(0, 0, shot)
        painter.end()
        hotspot = QPoint(max(0, min(pos.x() - rect.left(), rect.width())),
                         max(0, min(pos.y() - rect.top(), rect.height())))
        return faded, hotspot

    # ---------- 放下 ----------
    @staticmethod
    def _carries_task(event) -> bool:
        return event.mimeData().hasFormat(TASK_MIME)

    def dragEnterEvent(self, event):    # noqa: N802
        if self._carries_task(event):
            event.setDropAction(Qt.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):     # noqa: N802
        if not self._carries_task(event):
            event.ignore()
            return
        event.setDropAction(Qt.MoveAction)
        event.accept()
        self._update_drop_line(event.position().toPoint())

    def dragLeaveEvent(self, event):    # noqa: N802
        self._drop_line = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):         # noqa: N802
        if not self._carries_task(event):
            event.ignore()
            return
        task_id = bytes(event.mimeData().data(TASK_MIME)).decode("utf-8")
        anchor, after = self.drop_anchor(event.position().toPoint())
        self._drop_line = None
        self.viewport().update()
        event.setDropAction(Qt.MoveAction)
        event.accept()
        self.taskDropped.emit(task_id, anchor, after)

    def drop_anchor(self, pos: QPoint) -> tuple[str, bool]:
        """把落点翻译成 `(锚点任务 id, 插到它后面?)`；锚点为空 = 排到末尾。

        抽成独立方法是为了能直接测：真跑一次 QDrag 在离屏环境里不可靠。
        """
        index = self.indexAt(pos)
        if not index.isValid():
            return "", False
        item = self.item(index.row())
        task = item.data(Qt.UserRole) if item is not None else None
        if task is None:                # 落在占位提示上：当作落在末尾
            return "", False
        rect = self.visualRect(index)
        return task.id, pos.y() >= rect.center().y()

    def _update_drop_line(self, pos: QPoint) -> None:
        anchor, after = self.drop_anchor(pos)
        y = None
        if anchor:
            item = self.item_for(anchor)
            if item is not None:
                rect = self.visualItemRect(item)
                y = rect.bottom() if after else rect.top()
        else:
            y = self._tail_y()
        if y != self._drop_line:
            self._drop_line = y
            self.viewport().update()

    def _tail_y(self) -> int:
        last = None
        for i in range(self.count()):
            item = self.item(i)
            if not item.isHidden() and item.data(Qt.UserRole) is not None:
                last = item
        if last is None:
            return 6
        return self.visualItemRect(last).bottom() - 1

    # ---------- 交互 ----------
    def _on_double_clicked(self, item) -> None:
        task = item.data(Qt.UserRole)
        if task is not None:
            self.taskActivated.emit(task.id)

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        task = item.data(Qt.UserRole) if item is not None else None
        if task is None:
            return
        self.taskMenuRequested.emit(task.id, self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):     # noqa: N802
        if event.key() == Qt.Key_Delete:
            item = self.currentItem()
            task = item.data(Qt.UserRole) if item is not None else None
            if task is not None:
                self.taskDeleteRequested.emit(task.id)
                event.accept()
                return
        super().keyPressEvent(event)

    # ---------- 小工具 ----------
    def item_for(self, task_id: str) -> QListWidgetItem | None:
        for i in range(self.count()):
            item = self.item(i)
            task = item.data(Qt.UserRole)
            if task is not None and task.id == task_id:
                return item
        return None


# ---------------------------------------------------------------------------
# 可双击的标签（列标题双击重命名）
# ---------------------------------------------------------------------------
class _DoubleClickLabel(QLabel):
    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event):     # noqa: N802
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------
# 新建 / 编辑任务对话框
# ---------------------------------------------------------------------------
class TaskDialog(QDialog):
    """任务编辑框：标题 / 备注 / 优先级 / 截止日期。"""

    def __init__(self, parent=None, task: Task | None = None,
                 column_name: str = ""):
        super().__init__(parent)
        editing = task is not None
        self.setWindowTitle("编辑任务" if editing else "新建任务")
        self.setMinimumWidth(430)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.title_edit = QLineEdit(task.title if editing else "")
        self.title_edit.setPlaceholderText("要做什么？（必填）")
        self.title_edit.setMaxLength(TITLE_MAX)
        form.addRow("标题", self.title_edit)

        self.note_edit = QPlainTextEdit(task.note if editing else "")
        self.note_edit.setPlaceholderText("补充说明、验收标准…（可空）")
        self.note_edit.setFixedHeight(90)
        form.addRow("备注", self.note_edit)

        self.priority_box = QComboBox()
        for key, label in PRIORITIES:
            self.priority_box.addItem(label, key)
        current = task.priority if editing else DEFAULT_PRIORITY
        self.priority_box.setCurrentIndex(
            max(0, self.priority_box.findData(current)))
        form.addRow("优先级", self.priority_box)

        due_row = QWidget()
        due_lay = QHBoxLayout(due_row)
        due_lay.setContentsMargins(0, 0, 0, 0)
        due_lay.setSpacing(8)
        self.due_box = QCheckBox("设置截止日期")
        self.due_edit = QDateEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("yyyy-MM-dd")
        if editing and task.due:
            self.due_box.setChecked(True)
            self.due_edit.setDate(QDate.fromString(task.due, "yyyy-MM-dd"))
        else:
            self.due_edit.setDate(QDate.currentDate())
        self.due_edit.setEnabled(self.due_box.isChecked())
        self.due_box.toggled.connect(self.due_edit.setEnabled)
        due_lay.addWidget(self.due_box)
        due_lay.addWidget(self.due_edit, 1)
        form.addRow("截止", due_row)

        root.addLayout(form)

        if editing:
            where = f" · 所在列：{column_name}" if column_name else ""
            hint = QLabel(f"创建于 {task.created}{where}")
            hint.setStyleSheet(
                f"color:{theme.token('text_muted')};font-size:9pt;")
            root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.title_edit.setFocus()

    def _on_accept(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.information(self, "任务标题", "标题不能为空。")
            self.title_edit.setFocus()
            return
        self.accept()

    def values(self) -> dict:
        return {
            "title": self.title_edit.text().strip(),
            "note": self.note_edit.toPlainText().strip(),
            "priority": self.priority_box.currentData() or DEFAULT_PRIORITY,
            "due": (self.due_edit.date().toString("yyyy-MM-dd")
                    if self.due_box.isChecked() else ""),
        }


# ---------------------------------------------------------------------------
# 一列
# ---------------------------------------------------------------------------
class TaskColumn(QFrame):
    """看板的一列：标题 + 计数 + ＋/⋯ 按钮 + 任务列表。"""

    def __init__(self, board: "TaskBoardWidget", column: Column, parent=None):
        super().__init__(parent)
        self.board = board
        self.column_id = column.id
        self.setObjectName("taskColumn")
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 不给固定宽度：列**平分窗口宽度**并随窗口伸缩（见 TaskBoardWidget.reload 的
        # stretch=1），只在窗口窄到放不下时才退化成「最小宽度 + 横向滚动」。
        self.setMinimumWidth(MIN_COLUMN_W)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title_label = _DoubleClickLabel(column.name)
        self.title_label.setObjectName("columnTitle")
        self.title_label.setToolTip("双击重命名本列")
        self.title_label.doubleClicked.connect(
            lambda: self.board.rename_column(self.column_id))
        head.addWidget(self.title_label)
        self.count_label = QLabel("0")
        self.count_label.setObjectName("columnCount")
        self.count_label.setToolTip("本列任务数")
        head.addWidget(self.count_label)
        head.addStretch(1)

        self.add_btn = self._head_button(
            "＋", "在本列新建任务",
            lambda: self.board.new_task(self.column_id))
        head.addWidget(self.add_btn)
        self.menu_btn = self._head_button(
            "⋯", "列操作：重命名 / 左移 / 右移 / 清空 / 删除", self._show_menu)
        head.addWidget(self.menu_btn)
        root.addLayout(head)

        self.list = TaskListWidget(board, self)
        self.list.taskDropped.connect(
            lambda tid, anchor, after: board.drop_task(tid, self.column_id,
                                                       anchor, after))
        self.list.taskActivated.connect(board.edit_task)
        self.list.taskMenuRequested.connect(board.show_task_menu)
        self.list.taskDeleteRequested.connect(board.delete_task)
        root.addWidget(self.list, 1)

    @staticmethod
    def _head_button(text: str, tip: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("columnBtn")
        btn.setToolTip(tip)
        btn.setFixedSize(22, 22)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False: slot())
        return btn

    def _show_menu(self) -> None:
        sender = self.sender()
        anchor = sender if isinstance(sender, QWidget) else self.menu_btn
        self.board.show_column_menu(self.column_id, anchor)


# ---------------------------------------------------------------------------
# 看板
# ---------------------------------------------------------------------------
class TaskBoardWidget(QWidget):
    """任务看板主控件（小程序窗口里的内容）。"""

    def __init__(self, store: TaskBoardStore | None = None, parent=None):
        super().__init__(parent)
        self.store = store or TaskBoardStore()
        self._columns: dict[str, TaskColumn] = {}
        self._lists: dict[str, TaskListWidget] = {}
        self._filter = ""
        self._dragging = False          # 有拖动正在进行（此期间不重建视图）
        self._reload_pending = False    # 拖动期间来了重建请求，等拖动结束再做
        # 磁盘同步（防止「手改 json 被内存里的旧状态盖回去」，见 _sync_from_disk）
        self._disk_text = self._read_disk_text()
        self._dirty = False             # 上次落盘失败时置位，关窗时补写一次
        self.setObjectName("taskBoard")
        # 最小宽度按「三列各 MIN_COLUMN_W + 间距 + 页边距」留够：窗口再窄就横向滚动
        self.setMinimumSize(700, 420)
        self._build_ui()
        self.reload()
        # 窗口可见期间定期比对磁盘（关窗时停掉，见 showEvent/hideEvent）
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(DISK_SYNC_MS)
        self._sync_timer.timeout.connect(self._sync_from_disk)

    def sizeHint(self) -> QSize:        # noqa: N802（Qt 命名）
        # 宿主窗口按它开窗（见 ui/mini_window）；三列会平分这个宽度，之后随窗口伸缩
        return QSize(900, 560)

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        self.setStyleSheet(_board_qss())
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.new_task_btn = QPushButton("＋ 新建任务")
        self.new_task_btn.setToolTip("在第一列新建一个任务")
        self.new_task_btn.setCursor(Qt.PointingHandCursor)
        set_variant(self.new_task_btn, "primary")
        self.new_task_btn.clicked.connect(
            lambda: self.new_task(self._first_column_id()))
        bar.addWidget(self.new_task_btn)

        self.clean_btn = QPushButton("🧹 清理已完成")
        self.clean_btn.setToolTip("删除所有「完成列」里的任务")
        self.clean_btn.setCursor(Qt.PointingHandCursor)
        self.clean_btn.clicked.connect(self.clear_done)
        bar.addWidget(self.clean_btn)

        bar.addStretch(1)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("boardSearch")
        self.search_edit.setPlaceholderText("搜索任务（标题 / 备注 / 优先级）…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(250)
        self.search_edit.textChanged.connect(self._on_filter_changed)
        bar.addWidget(self.search_edit)
        root.addLayout(bar)

        self.area = QScrollArea()
        self.area.setObjectName("boardArea")
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.strip = QWidget()
        self.strip.setObjectName("boardStrip")
        self.strip_lay = QHBoxLayout(self.strip)
        self.strip_lay.setContentsMargins(0, 0, 0, 0)
        self.strip_lay.setSpacing(10)
        self.area.setWidget(self.strip)
        root.addWidget(self.area, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("boardStatus")
        root.addWidget(self.status_label)

    def _first_column_id(self) -> str:
        return self.store.columns[0].id if self.store.columns else ""

    # ---------- 重建视图 ----------
    def reload(self) -> None:
        """按 store 重建全部列与卡片（唯一的数据 -> 视图同步入口）。"""
        while self.strip_lay.count():
            item = self.strip_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._columns.clear()
        self._lists.clear()

        for column in self.store.columns:
            widget = TaskColumn(self, column)
            self._columns[column.id] = widget
            self._lists[column.id] = widget.list
            # stretch=1：列**平分**横向空间，窗口变宽就跟着变宽（不额外加尾部弹簧，
            # 否则多出来的宽度会全被弹簧吃掉，列还是原来那么窄）。
            self.strip_lay.addWidget(widget, 1)
        self._fill_lists()
        self._update_status()

    def _fill_lists(self) -> None:
        """把任务灌进各列列表；搜索命中之外的行隐藏（顺序仍以 store 为准）。"""
        kw = self._filter
        for column in self.store.columns:
            widget = self._columns.get(column.id)
            if widget is None:
                continue
            listw = widget.list
            listw.setUpdatesEnabled(False)
            listw.clear()
            listw.itemDelegate().done = column.done
            visible = 0
            for task in column.tasks:
                item = QListWidgetItem(task.title)
                item.setData(Qt.UserRole, task)
                item.setToolTip(_task_tooltip(task, column.name))
                listw.addItem(item)
                # ⚠️ setHidden 必须在 addItem **之后**：item 还没进视图时
                # QListWidgetItem.setHidden() 是空操作（视图指针为空），
                # 先设后加等于没设——筛选会看起来完全没生效。
                if kw and not task_matches(task, kw):
                    item.setHidden(True)
                else:
                    visible += 1
            if not column.tasks:
                self._add_placeholder(listw, "暂无任务\n点右上角 ＋ 或把任务拖进来")
            elif visible == 0:
                self._add_placeholder(listw, "没有匹配的任务")
            widget.count_label.setText(str(len(column.tasks)))
            listw.setUpdatesEnabled(True)

    @staticmethod
    def _add_placeholder(listw: QListWidget, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)        # 不可选、不可拖
        item.setData(Qt.UserRole, None)
        listw.addItem(item)

    def _update_status(self) -> None:
        total = self.store.task_count()
        done = self.store.done_count()
        parts = [f"共 {total} 个任务", f"已完成 {done}"]
        if self._filter:
            parts.append(f"筛选出 {self.store.match_count(self._filter)}")
        parts.append(f"已自动保存到 {os.path.basename(self.store.path)}")
        self.status_label.setText("  ·  ".join(parts))
        self.status_label.setToolTip(f"数据文件：{self.store.path}")
        self.clean_btn.setEnabled(done > 0)

    # ---------- 重建节流（拖放期间不重建，见模块 docstring）----------
    def begin_drag(self) -> None:
        self._dragging = True

    def end_drag(self) -> None:
        self._dragging = False
        if self._reload_pending:
            self._reload_pending = False
            # 异步一次：拖动方的 mouseMoveEvent 还在栈上，同步重建会把它自己删掉
            QTimer.singleShot(0, self._deferred_reload)

    def _deferred_reload(self) -> None:
        try:
            self.reload()
        except RuntimeError:            # 窗口已销毁：没什么可重建的了
            pass

    def request_reload(self) -> None:
        if self._dragging:
            self._reload_pending = True
            return
        self.reload()

    def _commit(self) -> None:
        """改动落盘 + 刷新视图（所有操作统一走这里）。"""
        self._dirty = not self.store.save()
        self._disk_text = self._read_disk_text()
        self.request_reload()

    # ---------- 磁盘同步 ----------
    def _read_disk_text(self) -> str:
        try:
            with open(self.store.path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return ""

    def _sync_from_disk(self) -> None:
        """磁盘上的数据被外部改过就重新载入（**以磁盘为准**）。

        为什么需要：看板状态常驻内存、每次改动都整份回写。用户手改 json 之后，
        内存里的旧状态会在下一次改动或关窗时把文件盖回去——表现就是
        「手动删掉的列又自己回来了」。这里定期比对磁盘内容，发现外部改动就重载。

        两个刻意的选择：
        - 用**内容比对**而不是 mtime：有些同步盘/编辑器会无谓地刷新时间戳，
          只看时间戳会「内容没变也重载」，把滚动位置和选中态一起清掉；
        - 只接受通过 `_looks_like_board` 的内容（能解析 + columns 是列表）：
          用户改到一半的半截文件不当坏数据，等写完再同步，免得反手把文件盖成默认看板。
        """
        if self._dragging:
            return                      # 拖动中不碰数据，等下一轮
        text = self._read_disk_text()
        if not text or text == self._disk_text or not _looks_like_board(text):
            return
        log.info("任务看板数据被外部修改，按磁盘内容重新载入: %s", self.store.path)
        self.store.load()
        self._disk_text = self._read_disk_text()
        self._dirty = False
        self.request_reload()

    def showEvent(self, event):         # noqa: N802（Qt 命名）
        super().showEvent(event)
        self._sync_from_disk()
        self._sync_timer.start()

    def hideEvent(self, event):         # noqa: N802（Qt 命名）
        self._sync_timer.stop()
        super().hideEvent(event)

    # ---------- 任务操作 ----------
    def new_task(self, column_id: str) -> None:
        column = self.store.find_column(column_id)
        if column is None:
            column = self.store.columns[0] if self.store.columns else None
        if column is None:
            return
        dialog = TaskDialog(self, column_name=column.name)
        if dialog.exec() != QDialog.Accepted:
            return
        if self.store.add_task(column.id, **dialog.values()) is not None:
            self._commit()

    def edit_task(self, task_id: str) -> None:
        found = self.store.find_task(task_id)
        if found is None:
            return
        column, _idx, task = found
        dialog = TaskDialog(self, task=task, column_name=column.name)
        if dialog.exec() != QDialog.Accepted:
            return
        if self.store.update_task(task_id, **dialog.values()):
            self._commit()

    def delete_task(self, task_id: str) -> None:
        found = self.store.find_task(task_id)
        if found is None:
            return
        task = found[2]
        answer = QMessageBox.question(
            self, "删除任务", f"删除「{task.title}」？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self.store.delete_task(task_id):
            self._commit()

    def drop_task(self, task_id: str, to_column_id: str, anchor: str,
                  after: bool) -> None:
        """列表把一次拖放翻译成这里：改 store -> 落盘 -> （拖动结束后）重建。"""
        if self.store.move_task(task_id, to_column_id, anchor or None, after):
            self._commit()
        else:
            self.request_reload()       # 位置没变也重建一次，清掉拖动残留的落点线

    def build_task_menu(self, task_id: str) -> QMenu | None:
        """构造某个任务的右键菜单（抽出来便于测菜单项，不用真的弹窗）。"""
        found = self.store.find_task(task_id)
        if found is None:
            return None
        column, _idx, _task = found
        menu = QMenu(self)
        menu.addAction("编辑…").triggered.connect(
            lambda _=False, tid=task_id: self.edit_task(tid))
        move_menu = menu.addMenu("移到")
        others = [c for c in self.store.columns if c.id != column.id]
        for other in others:
            action = move_menu.addAction(other.name)
            action.triggered.connect(
                lambda _=False, tid=task_id, cid=other.id:
                self._move_task_to(tid, cid))
        move_menu.setEnabled(bool(others))
        menu.addSeparator()
        menu.addAction("上移").triggered.connect(
            lambda _=False, tid=task_id: self._nudge_task(tid, -1))
        menu.addAction("下移").triggered.connect(
            lambda _=False, tid=task_id: self._nudge_task(tid, 1))
        menu.addSeparator()
        menu.addAction("删除").triggered.connect(
            lambda _=False, tid=task_id: self.delete_task(tid))
        return menu

    def show_task_menu(self, task_id: str, global_pos: QPoint) -> None:
        menu = self.build_task_menu(task_id)
        if menu is not None:
            menu.exec(global_pos)

    def _move_task_to(self, task_id: str, column_id: str) -> None:
        if self.store.move_task(task_id, column_id):
            self._commit()

    def _nudge_task(self, task_id: str, delta: int) -> None:
        if self.store.move_task_by(task_id, delta):
            self._commit()

    # ---------- 列操作 ----------
    def rename_column(self, column_id: str) -> None:
        column = self.store.find_column(column_id)
        if column is None:
            return
        name, ok = QInputDialog.getText(self, "重命名列", "列名：",
                                        text=column.name)
        if not ok:
            return
        if self.store.rename_column(column_id, name):
            self._commit()

    def build_column_menu(self, column_id: str) -> QMenu | None:
        """构造某一列的右键菜单（抽出来便于测菜单项，不用真的弹窗）。"""
        column = self.store.find_column(column_id)
        if column is None:
            return None
        menu = QMenu(self)
        menu.addAction("重命名…").triggered.connect(
            lambda _=False, cid=column_id: self.rename_column(cid))
        menu.addAction("左移").triggered.connect(
            lambda _=False, cid=column_id: self._move_column(cid, -1))
        menu.addAction("右移").triggered.connect(
            lambda _=False, cid=column_id: self._move_column(cid, 1))
        menu.addSeparator()
        done_action = menu.addAction("标记为完成列")
        done_action.setCheckable(True)
        done_action.setChecked(column.done)
        done_action.triggered.connect(
            lambda checked, cid=column_id: self._set_column_done(cid, checked))
        menu.addSeparator()
        menu.addAction("清空本列…").triggered.connect(
            lambda _=False, cid=column_id: self.clear_column(cid))
        menu.addAction("删除本列…").triggered.connect(
            lambda _=False, cid=column_id: self.delete_column(cid))
        return menu

    def show_column_menu(self, column_id: str, anchor: QWidget) -> None:
        menu = self.build_column_menu(column_id)
        if menu is None:
            return
        menu.exec(anchor.mapToGlobal(QPoint(anchor.width() // 2,
                                            anchor.height() // 2)))

    def _move_column(self, column_id: str, delta: int) -> None:
        if self.store.move_column(column_id, delta):
            self._commit()

    def _set_column_done(self, column_id: str, done: bool) -> None:
        if self.store.set_column_done(column_id, done):
            self._commit()

    def clear_column(self, column_id: str) -> None:
        column = self.store.find_column(column_id)
        if column is None or not column.tasks:
            return
        answer = QMessageBox.question(
            self, "清空本列",
            f"删除「{column.name}」列里的 {len(column.tasks)} 个任务？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self.store.clear_column(column_id):
            self._commit()

    def delete_column(self, column_id: str) -> None:
        """删除一列（连同列内任务，先问一声）。**最后一列不给删**：看板至少要留一列。

        保留这个入口的理由：没有它，用户遇到误建的列（比如早先「新建列」按钮留下的
        「新列」）就只能手改 json。新建列已取消（见模块 docstring），但删除仍要能用。
        """
        column = self.store.find_column(column_id)
        if column is None:
            return
        if len(self.store.columns) <= 1:
            QMessageBox.information(self, "删除本列", "看板至少要保留一列。")
            return
        tip = f"删除「{column.name}」列？"
        if column.tasks:
            tip += f"\n列里的 {len(column.tasks)} 个任务会一起删掉。"
        answer = QMessageBox.question(self, "删除本列", tip,
                                      QMessageBox.Yes | QMessageBox.No,
                                      QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self.store.delete_column(column_id):
            self._commit()

    def clear_done(self) -> None:
        n = self.store.done_count()
        if n <= 0:
            return
        answer = QMessageBox.question(
            self, "清理已完成", f"删除完成列里的 {n} 个任务？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self.store.clear_done():
            self._commit()

    # ---------- 搜索 ----------
    def _on_filter_changed(self, text: str) -> None:
        self._filter = (text or "").strip().lower()
        self._fill_lists()
        self._update_status()

    def filter_text(self) -> str:
        return self._filter

    # ---------- 收尾 ----------
    def shutdown(self) -> None:
        """小程序窗口关闭前调一次（约定见 mini_window）。

        **不再无条件落盘**：每次改动本来就即时落盘，关窗前再整份写一遍，只会在
        「用户手改了 json」时把内存里的旧状态盖回去（那正是「删掉的列又回来」的成因）。
        只有上一次落盘失败（磁盘满 / 权限问题）时才补写一次，避免真的丢改动。
        """
        if self._dirty:
            self._dirty = not self.store.save()


def _task_tooltip(task: Task, column_name: str) -> str:
    lines = [task.title, f"列：{column_name}", f"优先级：{task.priority_label}"]
    if task.due:
        lines.append(f"截止：{task.due}" + ("（已逾期）" if task.is_overdue() else ""))
    lines.append(f"创建于 {task.created}")
    if task.note:
        lines.extend(("", task.note))
    lines.extend(("", "双击编辑 · 右键更多操作 · 拖动可换列/调序"))
    return "\n".join(lines)


def _board_qss() -> str:
    """看板配色（页面级内联样式，颜色全部走主题令牌 -> 切主题自动跟随）。"""
    t = theme.token
    return f"""
    QWidget#taskBoard {{ background: {t('panel_bg')}; }}
    QWidget#boardStrip {{ background: transparent; }}
    QScrollArea#boardArea {{ background: transparent; border: none; }}
    QFrame#taskColumn {{
        background: {t('card_bg')};
        border: 1px solid {t('border')};
        border-radius: 8px;
    }}
    QLabel#columnTitle {{ font-size: 10pt; font-weight: 700; color: {t('text')}; }}
    QLabel#columnCount {{
        color: {t('text_muted')}; font-size: 9pt;
        background: {t('hover_bg')}; border-radius: 7px; padding: 0px 6px;
    }}
    QLabel#boardStatus {{ color: {t('text_muted')}; font-size: 9pt; }}
    QPushButton#columnBtn {{
        border: none; border-radius: 4px;
        color: {t('text_dim')}; background: transparent; font-size: 10pt;
    }}
    QPushButton#columnBtn:hover {{ background: {t('primary_soft')}; color: {t('primary')}; }}
    QListWidget#taskList {{ background: transparent; border: none; outline: none; }}
    QLineEdit#boardSearch {{
        background: {t('input_bg')}; color: {t('text')};
        border: 1px solid {t('input_border')}; border-radius: 6px;
        padding: 4px 8px; font-size: 9pt;
    }}
    QLineEdit#boardSearch:focus {{ border-color: {t('primary')}; }}
    """


# 登记到「小工具」页（key 一旦发布不要再改）
register(MiniApp(
    key="task_board",
    name="任务看板",
    icon="📋",
    desc="看板式任务管理：多列、拖动换列调序、优先级与截止日期，数据保存在本地",
    factory=TaskBoardWidget,
))
