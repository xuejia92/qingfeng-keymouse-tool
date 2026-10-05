# -*- coding: utf-8 -*-
"""内置小程序：剪贴板历史（文本 / 图片 / 文件，**永久保存在本地**）。

功能
----
- 窗口打开期间实时记录剪贴板变化，支持**文本 / 图片 / 文件**三类；
- 左侧「历史」、右侧「收藏」两个**独立面板**（收藏单独放一侧，不混在历史里）；
- 顶部横幅常显**当前可粘贴的剪贴板内容**；历史/收藏里对应的那条会用 `●` + 主色
  加粗**特殊标记**出来，一眼能看出"现在按 Ctrl+V 会贴出哪条"；
- 下方预览区：**图片直接看图**（单张或多张铺成网格、点缩略图放大），文本看全文；
  没选中记录时预览的就是**当前剪贴板**——刚复制完一张图也能马上看到；
  从资源管理器一次复制**多个图片文件**时，会一起铺出来预览；
- 每条可复制回剪贴板 / 修改（仅文本）/ 收藏 / 删除；两个面板都支持
  **多选批量删除**（Ctrl / Shift 多选、Ctrl+A 全选，或按 Delete）；
- 历史条数上限 `MAX_ENTRIES`，超出丢最旧的非收藏项（收藏项不参与淘汰）。

数据存在哪（2026-10-06 起：永久本地存储）
-----------------------------------------
`<BASE_DIR>/clipboard_data/`：
- `history.json`：全部记录（文本 / 文件路径 / 收藏与已修改标记 / 时间戳）；
- `images/<uid>.png`：图片记录的原图（不进 json，免得文件膨胀）。
BASE_DIR 见 `app/config.py`（源码运行 = 项目根目录，打包后 = exe 所在目录）。
每次改动**立刻落盘**，原子写（先写 `.tmp` 再 `os.replace`），写一半崩溃不会留下
半个 json；删记录时顺手清理不再被引用的图片文件。文件损坏 / 读不动按「空历史」
处理，不会让小程序打不开。

⚠️ 安全提示（边界变更）：早期版本**刻意不落盘**（剪贴板里常有密码、验证码）。
现在按需求改为永久保存 —— 也就是说**复制过的密码会留在磁盘上**。要清干净就
「清空历史」（收藏也一并勾掉），或直接删掉 `clipboard_data` 目录。

数据结构
--------
`ClipEntry`：uid + kind(text/image/files) + 对应载荷 + 时间戳 + 收藏/已修改标记。
类型判定优先级 **文件 > 图片 > 文本**：从资源管理器复制文件时，很多程序同时会往
剪贴板塞一个文本（甚至图片），取"信息量最大"的那个才算复制的本意。
去重按**内容**做：同一份内容再复制一次不会新增，而是把它**提到最前面**并刷新时间。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QMimeData, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import config as config_mod
from ..ui import theme
from ..ui.frameless import FramelessDialog
from ..ui.widgets import human_size, set_variant
from . import MiniApp, register

log = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_FILES = "files"

KIND_LABELS = {KIND_TEXT: "文本", KIND_IMAGE: "图片", KIND_FILES: "文件"}

MAX_ENTRIES = 200                   # 历史条数上限（收藏项不参与淘汰）
MAX_TEXT_CHARS = 500_000            # 单条文本上限，超出截断（防止复制巨型日志吃内存）
PREVIEW_CHARS = 300                 # 预览区最多展示多少字
LIST_SUMMARY_CHARS = 60             # 列表行摘要最多展示多少字
BANNER_CHARS = 48                   # 顶部「当前可粘贴」横幅摘要最多展示多少字
POLL_MS = 500                       # 兜底轮询间隔（见 clipboard_sequence 的说明）

# 图片预览
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff",
              ".ico", ".jfif", ".avif")
THUMB_W, THUMB_H = 152, 116         # 一张缩略图的格子尺寸
THUMB_GAP = 8                       # 缩略图间距
MAX_PREVIEW_IMAGES = 12             # 一次最多预览几张（一次读一堆大图会卡住界面）
PREVIEW_AREA_H = 150                # 预览区最小高度

# 落盘位置（相对 BASE_DIR）
DATA_DIR_NAME = "clipboard_data"
HISTORY_FILE = "history.json"
IMAGES_SUBDIR = "images"
HISTORY_VERSION = 1

_ROLE_UID = Qt.UserRole


def clipboard_data_dir() -> str:
    """剪贴板数据的存放目录。

    ⚠️ 必须**每次调用时**读 `config_mod.BASE_DIR`（而不是 import 期缓存成常量）：
    测试用 `tests/_env.TempConfigPaths` 换掉 BASE_DIR 之后要能立刻生效，
    打包成 exe 后 BASE_DIR 又是 exe 所在目录。
    """
    return os.path.join(config_mod.BASE_DIR, DATA_DIR_NAME)


def _short(text: str, limit: int) -> str:
    """压成单行并截断（换行会让单行标签/列表行变形）。"""
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def is_image_path(path: str) -> bool:
    """这个本地路径是不是图片（按后缀判断，不读文件）。"""
    return os.path.splitext(str(path or ""))[1].lower() in IMAGE_EXTS


def previewable_count(entries) -> int:
    """这批记录里**能预览的图片**总数（只数不读，用来提示"只显示前 N 张"）。"""
    total = 0
    for entry in entries or []:
        if entry.kind == KIND_IMAGE and entry.image is not None:
            total += 1
        elif entry.kind == KIND_FILES:
            total += sum(1 for p in entry.files if is_image_path(p))
    return total


def preview_images(entries,
                   limit: int = MAX_PREVIEW_IMAGES) -> list[tuple[str, QImage]]:
    """把若干条记录里能预览的图片收成 `[(标题, QImage), ...]`，最多 `limit` 张。

    - 图片记录：直接用内存里那份 QImage；
    - 文件记录：挑出图片后缀的本地文件读进来看 —— 从资源管理器一次复制多张图时，
      剪贴板里是"多个文件"，这里就能**多图一起预览**；
    - 文本记录：跳过。
    最多收 `limit` 张：一次读一堆大图会卡住界面（完整数量看 `previewable_count`）。
    """
    out: list[tuple[str, QImage]] = []
    for entry in entries or []:
        if len(out) >= limit:
            break
        if entry.kind == KIND_IMAGE and entry.image is not None:
            out.append((f"{entry.clock()} 图片", entry.image))
            continue
        if entry.kind == KIND_FILES:
            for path in entry.files:
                if len(out) >= limit:
                    break
                if not is_image_path(path):
                    continue
                image = QImage(path)
                if not image.isNull():
                    out.append((os.path.basename(path) or path, image))
    return out


def clipboard_sequence() -> int:
    """Windows 剪贴板序号：内容每变一次 +1。拿不到返回 -1。

    为什么要这个东西：主力监听是 Qt 的 `QClipboard.dataChanged`，但"它对**别的程序**
    的复制是否一定触发"这件事在不同 Qt/平台组合上并非铁板一块，而"实时记录"恰恰是
    本工具的核心。于是每 500ms 比一次序号作为兜底：序号变了才真去读剪贴板，
    一次 ctypes 调用、几乎零开销，两条路同时命中也不会重复入库（去重挡掉）。
    ⚠️ ctypes 调 Win32 必须显式设 argtypes/restype（工程铁律）。
    """
    if sys.platform != "win32":
        return -1
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32
        return int(user32.GetClipboardSequenceNumber())
    except Exception:               # noqa: BLE001（拿不到就只靠信号，不影响功能）
        return -1


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class ClipEntry:
    """一条剪贴板记录。三类载荷互斥，用 kind 区分。"""

    kind: str = KIND_TEXT
    text: str = ""                          # kind=text
    image: QImage | None = None             # kind=image
    files: list[str] = field(default_factory=list)   # kind=files（本地路径）
    created_at: float = field(default_factory=time.time)
    favorite: bool = False
    edited: bool = False                    # 用户改过内容（列表上打个小标记）
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # ---------- 展示 ----------
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, "未知")

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.created_at))

    def stamp(self) -> str:
        """完整时间（悬停提示用，跨天时能看出是哪天）。"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))

    def summary(self) -> str:
        """列表行里那一段摘要（单行、截断）。"""
        if self.kind == KIND_FILES:
            names = "、".join(os.path.basename(p) or p for p in self.files[:3])
            more = f" 等 {len(self.files)} 个" if len(self.files) > 3 else ""
            return f"{len(self.files)} 个文件 · {names}{more}"
        if self.kind == KIND_IMAGE:
            size = self.image.size() if self.image is not None else QSize(0, 0)
            return f"{size.width()}×{size.height()} · {human_size(self.byte_size())}"
        return self.one_line(LIST_SUMMARY_CHARS)

    def one_line(self, limit: int = LIST_SUMMARY_CHARS) -> str:
        """文本压成单行并截断（换行会让列表行变形）。"""
        return _short(self.text, limit)

    def byte_size(self) -> int:
        """体积估算：文本按 UTF-8 长度，图片按像素数 ×4，文件按路径长度之和。"""
        if self.kind == KIND_IMAGE and self.image is not None:
            return int(self.image.sizeInBytes())
        if self.kind == KIND_FILES:
            return sum(len(p.encode("utf-8")) for p in self.files)
        return len(self.text.encode("utf-8"))

    # ---------- 内容指纹（去重用） ----------
    def digest(self) -> str:
        """内容指纹：同指纹视为同一条记录。"""
        if self.kind == KIND_IMAGE:
            return "image:" + image_digest(self.image)
        if self.kind == KIND_FILES:
            joined = "\n".join(os.path.normcase(os.path.abspath(p))
                               for p in self.files)
            return "files:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()
        return "text:" + hashlib.sha1(self.text.encode("utf-8")).hexdigest()


def image_digest(image: QImage | None) -> str:
    """QImage 的内容指纹（同内容同指纹）。

    ⚠️ 不能用 `cacheKey()`——那是**每个 QImage 对象**的编号，同一张图复制两次
    也会不同，去重就失效了。这里对像素数据取 sha1：剪贴板变化是低频动作，
    一次几十毫秒可以接受；调用方还会先用尺寸做预筛，进一步少算。
    """
    if image is None or image.isNull():
        return "empty"
    raw = bytes(image.constBits())
    return hashlib.sha1(f"{image.width()}x{image.height()}:".encode() + raw).hexdigest()


# ---------------------------------------------------------------------------
# 从剪贴板读取 / 写回
# ---------------------------------------------------------------------------
def entry_from_mime(mime: QMimeData) -> ClipEntry | None:
    """把剪贴板内容转成一条记录；认不出的格式返回 None。

    判定优先级 **文件 > 图片 > 文本**（见模块开头的说明）。
    """
    if mime is None:
        return None
    if mime.hasUrls():
        paths = [u.toLocalFile() for u in mime.urls()
                 if u.isLocalFile() and u.toLocalFile()]
        if paths:
            return ClipEntry(kind=KIND_FILES, files=paths)
    if mime.hasImage():
        image = QImage(mime.imageData())
        if not image.isNull():
            return ClipEntry(kind=KIND_IMAGE, image=image)
    if mime.hasText():
        text = mime.text()
        if text:
            return ClipEntry(kind=KIND_TEXT, text=text[:MAX_TEXT_CHARS])
    return None


def apply_entry_to_clipboard(entry: ClipEntry, clipboard=None) -> bool:
    """把一条记录写回系统剪贴板（复制按钮/双击走这里）。"""
    clipboard = clipboard or QGuiApplication.clipboard()
    if entry.kind == KIND_IMAGE and entry.image is not None:
        clipboard.setImage(entry.image)
        return True
    if entry.kind == KIND_FILES and entry.files:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in entry.files])
        clipboard.setMimeData(mime)
        return True
    if entry.kind == KIND_TEXT and entry.text:
        clipboard.setText(entry.text)
        return True
    return False


# ---------------------------------------------------------------------------
# 历史仓库（含剪贴板监听 + 本地持久化）
# ---------------------------------------------------------------------------
class ClipboardStore(QObject):
    """历史记录 + 剪贴板监听 + 本地存储。

    用法：`start_watching()` / `stop_watching()` 成对调用。记录**永久保存在
    `<BASE_DIR>/clipboard_data/`**（见模块 docstring），每次改动立刻落盘。

    ⚠️ 一律按**索引**遍历 `entries`：`ClipEntry` 是 dataclass，`__eq__` 比的是字段值，
    两条内容相同的记录用 `list.index()` 会命错行（2026-10-03 写的时候差点踩到）。
    """

    changed = Signal()          # 记录增删改（界面据此刷新）

    def __init__(self, data_dir: str | None = None, persist: bool = True,
                 parent=None):
        super().__init__(parent)
        self.persist = bool(persist)
        self.data_dir = data_dir or clipboard_data_dir()
        self.history_path = os.path.join(self.data_dir, HISTORY_FILE)
        self.images_dir = os.path.join(self.data_dir, IMAGES_SUBDIR)
        self.entries: list[ClipEntry] = []
        # 当前剪贴板内容（**未必在 entries 里**：比如刚清空过历史）。
        # 界面用它做顶部横幅 + 列表里的「● 当前」标记，所以只留摘要够用的那份。
        self.current: ClipEntry | None = None
        self.current_digest = ""
        self._watching = False
        self._digests: list[str] = []       # 与 entries 一一对应的指纹（去重用）
        self._last_seq = -1                 # 上一次看到的剪贴板序号（兜底轮询用）
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_tick)
        self.load()

    # ---------- 落盘 / 读回 ----------
    def save(self) -> bool:
        """整份落盘：索引写 history.json，图片另存 images/<uid>.png。

        原子写（.tmp + os.replace）。图片只在**文件还不存在**时才编码写入——
        图片记录没有"改内容"的语义，同一个 uid 的 png 内容不会变，省掉每次
        保存都重新编码一遍的开销。
        """
        if not self.persist:
            return True
        try:
            os.makedirs(self.images_dir, exist_ok=True)
            items = []
            for entry in self.entries:
                item = {"uid": entry.uid, "kind": entry.kind, "text": entry.text,
                        "files": list(entry.files),
                        "created_at": float(entry.created_at),
                        "favorite": bool(entry.favorite),
                        "edited": bool(entry.edited), "image": ""}
                if entry.kind == KIND_IMAGE and entry.image is not None:
                    name = f"{entry.uid}.png"
                    path = os.path.join(self.images_dir, name)
                    if os.path.exists(path) or entry.image.save(path, "PNG"):
                        item["image"] = name
                items.append(item)
            payload = {"version": HISTORY_VERSION, "entries": items}
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.history_path)
            self._prune_images()
            return True
        except OSError:
            log.warning("剪贴板历史保存失败: %s", self.history_path, exc_info=True)
            return False

    def load(self) -> int:
        """从磁盘读回历史；读不到 / 坏了就当空历史。返回读回的条数。"""
        if not self.persist:
            return 0
        try:
            with open(self.history_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            log.warning("剪贴板历史读取失败，按空历史处理: %s",
                        self.history_path, exc_info=True)
            return 0
        raw = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return 0
        entries: list[ClipEntry] = []
        for item in raw:
            entry = self._entry_from_dict(item)
            if entry is not None:
                entries.append(entry)
        self.entries = entries
        self._digests = [e.digest() for e in entries]
        return len(entries)

    def _entry_from_dict(self, item) -> ClipEntry | None:
        """把 json 里的一条还原成 ClipEntry；形状不对返回 None（坏数据直接跳过）。"""
        if not isinstance(item, dict):
            return None
        kind = str(item.get("kind") or "")
        try:
            created = float(item.get("created_at") or 0)
        except (TypeError, ValueError):
            created = 0.0
        common = {"uid": str(item.get("uid") or ""),
                  "created_at": created or time.time(),
                  "favorite": bool(item.get("favorite")),
                  "edited": bool(item.get("edited"))}
        if kind == KIND_IMAGE:
            name = os.path.basename(str(item.get("image") or ""))
            if not name:
                return None
            image = QImage(os.path.join(self.images_dir, name))
            if image.isNull():
                return None                 # 图片文件丢了：这条就没意义了
            return ClipEntry(kind=KIND_IMAGE, image=image, **common)
        if kind == KIND_FILES:
            files = [str(p) for p in (item.get("files") or []) if str(p).strip()]
            if not files:
                return None
            return ClipEntry(kind=KIND_FILES, files=files, **common)
        text = str(item.get("text") or "")
        if not text:
            return None
        return ClipEntry(kind=KIND_TEXT, text=text[:MAX_TEXT_CHARS], **common)

    def _prune_images(self) -> None:
        """删掉不再被任何记录引用的图片文件（删记录/淘汰后留下的孤儿）。"""
        try:
            used = {f"{e.uid}.png" for e in self.entries if e.kind == KIND_IMAGE}
            for name in os.listdir(self.images_dir):
                if name not in used:
                    try:
                        os.remove(os.path.join(self.images_dir, name))
                    except OSError:
                        pass
        except OSError:
            pass

    # ---------- 监听 ----------
    @property
    def watching(self) -> bool:
        return self._watching

    def start_watching(self) -> None:
        if self._watching:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.dataChanged.connect(self._on_clipboard_changed)
        self._watching = True
        self._last_seq = clipboard_sequence()
        self._poll.start()
        self.capture_now()                  # 打开窗口时先把当前剪贴板收进来

    def stop_watching(self) -> None:
        if not self._watching:
            return
        self._poll.stop()
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            try:
                clipboard.dataChanged.disconnect(self._on_clipboard_changed)
            except (RuntimeError, TypeError):
                pass                        # 已经断开/对象没了，忽略
        self._watching = False

    def _on_clipboard_changed(self) -> None:
        self.capture_now()

    def _poll_tick(self) -> None:
        """兜底轮询：序号变了才读剪贴板（见 clipboard_sequence）。"""
        sequence = clipboard_sequence()
        if sequence < 0 or sequence == self._last_seq:
            return
        self._last_seq = sequence
        self.capture_now()

    def capture_now(self) -> ClipEntry | None:
        """读一次当前剪贴板并入库（返回新记录；重复内容返回 None）。"""
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return None
        entry = entry_from_mime(clipboard.mimeData())
        if entry is None:
            return None
        # 先记「当前剪贴板」：add() 里会 emit，界面刷新时就得能读到它
        digest = entry.digest()
        self.current = entry
        self.current_digest = digest
        added = self.add(entry, digest=digest)
        if added is None:
            # 重复内容：历史里已有同一条，把 current 指回它（省一份图片内存）
            existing = self._find_by_digest(digest)
            if existing is not None:
                self.current = existing
        return added

    # ---------- 查询 ----------
    def _find_by_digest(self, digest: str) -> ClipEntry | None:
        for index, known in enumerate(self._digests):
            if known == digest:
                return self.entries[index]
        return None

    def current_uid(self) -> str:
        """当前剪贴板对应的历史记录 uid（不在历史里则空串）。"""
        if not self.current_digest:
            return ""
        entry = self._find_by_digest(self.current_digest)
        return entry.uid if entry is not None else ""

    def is_current(self, entry: ClipEntry) -> bool:
        """这条是不是"现在按 Ctrl+V 会贴出来的内容"。"""
        return bool(self.current_digest) and entry.uid == self.current_uid()

    def get(self, uid: str) -> ClipEntry | None:
        return next((e for e in self.entries if e.uid == uid), None)

    def history_entries(self) -> list[ClipEntry]:
        """左侧「历史」面板：**非收藏**项，按时间倒序（收藏在右边单独一侧）。"""
        return sorted((e for e in self.entries if not e.favorite),
                      key=lambda e: -e.created_at)

    def favorite_entries(self) -> list[ClipEntry]:
        """右侧「收藏」面板：收藏项，按时间倒序。"""
        return sorted((e for e in self.entries if e.favorite),
                      key=lambda e: -e.created_at)

    # ---------- 增删改 ----------
    def add(self, entry: ClipEntry, digest: str | None = None) -> ClipEntry | None:
        """入库：同内容不新增，改为**提到最前面**并刷新时间。

        返回真正新增的那条；命中重复时返回 None（但它已经被移到最前了）。
        """
        digest = digest or entry.digest()
        for index, existing in enumerate(self._digests):
            if existing == digest:
                moved = self.entries.pop(index)
                self._digests.pop(index)
                moved.created_at = entry.created_at
                self.entries.insert(0, moved)
                self._digests.insert(0, digest)
                self.save()
                self.changed.emit()
                return None
        self.entries.insert(0, entry)
        self._digests.insert(0, digest)
        self._trim()
        self.save()
        self.changed.emit()
        return entry

    def _trim(self) -> None:
        """超出上限时从**最旧的非收藏项**开始丢（收藏项一直留着）。"""
        while len(self.entries) > MAX_ENTRIES:
            for index in range(len(self.entries) - 1, -1, -1):
                if not self.entries[index].favorite:
                    self.entries.pop(index)
                    self._digests.pop(index)
                    break
            else:
                return                      # 全是收藏项：那就都留着

    def remove(self, uid: str) -> bool:
        return self.remove_many([uid]) > 0

    def remove_many(self, uids) -> int:
        """批量删除，返回删掉的条数（批量选择删除走这里）。"""
        targets = {str(u) for u in (uids or []) if str(u)}
        if not targets:
            return 0
        keep = [(e, d) for e, d in zip(self.entries, self._digests)
                if e.uid not in targets]
        removed = len(self.entries) - len(keep)
        if not removed:
            return 0
        self.entries = [e for e, _ in keep]
        self._digests = [d for _, d in keep]
        self.save()
        self.changed.emit()
        return removed

    def clear(self, keep_favorites: bool = True) -> int:
        """清空历史；默认保留收藏项。返回清掉的条数。"""
        before = len(self.entries)
        if keep_favorites:
            kept = [(e, d) for e, d in zip(self.entries, self._digests)
                    if e.favorite]
            self.entries = [e for e, _ in kept]
            self._digests = [d for _, d in kept]
        else:
            self.entries = []
            self._digests = []
        removed = before - len(self.entries)
        if removed:
            self.save()
            self.changed.emit()
        return removed

    def toggle_favorite(self, uid: str) -> bool | None:
        """切换收藏；返回切换后的状态（找不到返回 None）。"""
        for entry in self.entries:
            if entry.uid == uid:
                entry.favorite = not entry.favorite
                self.save()
                self.changed.emit()
                return entry.favorite
        return None

    def set_favorite_many(self, uids, favorite: bool) -> int:
        """把一批记录统一设为收藏 / 取消收藏（多选时用）。返回改动的条数。"""
        targets = {str(u) for u in (uids or []) if str(u)}
        if not targets:
            return 0
        changed = 0
        for entry in self.entries:
            if entry.uid in targets and entry.favorite != bool(favorite):
                entry.favorite = bool(favorite)
                changed += 1
        if changed:
            self.save()
            self.changed.emit()
        return changed

    def update_text(self, uid: str, text: str) -> bool:
        """修改文本记录（只有文本能改；图片/文件没有"改内容"的语义）。"""
        text = str(text or "")
        if not text:
            return False
        for index, entry in enumerate(self.entries):
            if entry.uid != uid:
                continue
            if entry.kind != KIND_TEXT:
                return False
            entry.text = text[:MAX_TEXT_CHARS]
            entry.edited = True
            self._digests[index] = entry.digest()   # 内容变了，指纹跟着变
            self.save()
            self.changed.emit()
            return True
        return False


_DEFAULT_STORE: ClipboardStore | None = None


def default_store() -> ClipboardStore:
    """进程内共享的历史仓库（窗口关了再开，历史还在）。"""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = ClipboardStore()
    return _DEFAULT_STORE


# ---------------------------------------------------------------------------
# 小组件
# ---------------------------------------------------------------------------
class _ClickLabel(QLabel):
    """可点击的标签（顶部「当前剪贴板」横幅用它）。

    横向 size policy 设成 **Ignored**：标签的 sizeHint 是整段文字的宽度，
    剪贴板里一条长文本（甚至 50 万字）会直接把窗口最小宽度顶到屏幕外。
    横幅只做"一眼看个大概"，完整内容看悬停提示。
    """

    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def mouseReleaseEvent(self, event):     # noqa: N802（Qt 命名）
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _ClipList(QListWidget):
    """列表：额外支持按 Delete 删除选中项（配合多选批量删除）。"""

    deleteRequested = Signal()

    def keyPressEvent(self, event):         # noqa: N802（Qt 命名）
        if event.key() == Qt.Key_Delete:
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 修改对话框
# ---------------------------------------------------------------------------
class EditTextDialog(FramelessDialog):
    """修改一条文本记录。"""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改内容")
        self.setMinimumSize(560, 380)

        root = QVBoxLayout(self.body())
        root.setContentsMargins(14, 14, 14, 12)
        root.setSpacing(8)

        self.editor = QPlainTextEdit(text)
        root.addWidget(self.editor, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        ok = QPushButton("保存")
        set_variant(ok, "primary")
        ok.setDefault(True)
        ok.clicked.connect(self._on_accept)
        actions.addWidget(ok)
        root.addLayout(actions)
        self.warning = ""

    def _on_accept(self) -> None:
        if not self.text().strip():
            self.warning = "内容不能为空"
            QMessageBox.warning(self, "修改内容", self.warning)
            return
        self.warning = ""
        self.accept()

    def text(self) -> str:
        return self.editor.toPlainText()


# ---------------------------------------------------------------------------
# 图片预览（单张缩略图 / 多图网格 / 点开放大）
# ---------------------------------------------------------------------------
class _ThumbTile(QFrame):
    """一张缩略图（图片 + 尺寸说明），点一下放大。"""

    clicked = Signal(QImage)

    def __init__(self, title: str, image: QImage, parent=None):
        super().__init__(parent)
        self._image = image
        self.setObjectName("clipThumb")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(THUMB_W, THUMB_H)
        self.setCursor(Qt.PointingHandCursor)
        t = theme.token
        self.setStyleSheet(
            f"QFrame#clipThumb{{background:{t('hover_bg')};"
            f"border:1px solid {t('border')};border-radius:6px;}}"
            f"QFrame#clipThumb:hover{{border:1px solid {t('primary')};}}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 3)
        lay.setSpacing(2)

        pic = QLabel()
        pic.setAlignment(Qt.AlignCenter)
        pic.setPixmap(QPixmap.fromImage(image).scaled(
            THUMB_W - 12, THUMB_H - 30, Qt.KeepAspectRatio,
            Qt.SmoothTransformation))
        lay.addWidget(pic, 1)

        cap = QLabel(_short(f"{title} · {image.width()}×{image.height()}", 24))
        cap.setAlignment(Qt.AlignCenter)
        cap.setStyleSheet(f"color:{t('text_muted')};font-size:8pt;")
        lay.addWidget(cap)
        self.setToolTip(f"{title}\n{image.width()}×{image.height()}\n点一下放大看")

    def mouseReleaseEvent(self, event):     # noqa: N802（Qt 命名）
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._image)
        super().mouseReleaseEvent(event)


class ImageGallery(QScrollArea):
    """多图预览：把若干张图铺成网格（列数随宽度自适应），点一张放大看。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("clipGallery")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"QScrollArea#clipGallery{{background:{theme.token('card_bg')};"
            f"border:1px solid {theme.token('border')};border-radius:6px;}}")
        self._host = QWidget()
        self._host.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._host)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setSpacing(THUMB_GAP)
        self.setWidget(self._host)
        self._images: list[tuple[str, QImage]] = []
        self._columns = 0

    # ---------- 内容 ----------
    def show_images(self, images) -> None:
        """换一批图（列数缓存作废 -> 强制重排）。"""
        self._images = list(images)
        self._columns = 0
        self._relayout()

    def images(self) -> list[tuple[str, QImage]]:
        """当前展示的图片（测试用）。"""
        return list(self._images)

    def tiles(self) -> list[_ThumbTile]:
        """当前网格里的缩略图控件（测试用）。"""
        out = []
        for index in range(self._grid.count()):
            widget = self._grid.itemAt(index).widget()
            if isinstance(widget, _ThumbTile):
                out.append(widget)
        return out

    # ---------- 排版 ----------
    def resizeEvent(self, event):           # noqa: N802（Qt 命名）
        super().resizeEvent(event)
        self._relayout()

    def _column_count(self) -> int:
        width = max(THUMB_W, self.viewport().width())
        return max(1, (width + THUMB_GAP) // (THUMB_W + THUMB_GAP))

    def _relayout(self) -> None:
        columns = self._column_count()
        if columns == self._columns:
            return                          # 列数没变就不重排（拖动窗口别反复重建）
        self._columns = columns
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for col in range(self._grid.columnCount() + 1):
            self._grid.setColumnStretch(col, 0)
        for index, (title, image) in enumerate(self._images):
            tile = _ThumbTile(title, image)
            tile.clicked.connect(self._open_viewer)
            self._grid.addWidget(tile, index // columns, index % columns)
        self._grid.setColumnStretch(columns, 1)     # 多余宽度留到右边
        self._grid.setRowStretch(self._grid.rowCount(), 1)

    def _open_viewer(self, image: QImage) -> None:
        ImagePreviewDialog(image, self.window()).exec()


class ImagePreviewDialog(FramelessDialog):
    """放大看一张图（按屏幕可用区域缩放，不放大超过原尺寸）。"""

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self._image = image
        self.setWindowTitle(f"图片预览 · {image.width()}×{image.height()}")

        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        max_w = int((avail.width() if avail else 1280) * 0.72)
        max_h = int((avail.height() if avail else 800) * 0.72)
        width = min(max(image.width() + 40, 460), max_w)
        height = min(max(image.height() + 80, 340), max_h)
        self.resize(width, height)

        view = QLabel()
        view.setAlignment(Qt.AlignCenter)
        view.setPixmap(QPixmap.fromImage(image).scaled(
            width - 40, height - 100, Qt.KeepAspectRatio,
            Qt.SmoothTransformation))
        root = QVBoxLayout(self.body())
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(view, 1)

    def image(self) -> QImage:
        """当前预览的图（测试用）。"""
        return self._image


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class ClipboardHistoryWidget(QWidget):
    """剪贴板历史主控件（独立窗口里跑）。"""

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self.store = store if store is not None else default_store()
        self.setMinimumSize(840, 600)
        self._active_list: QListWidget | None = None
        # 预览图片缓存（键 = 目标记录的 uid 组合）：refresh 很频繁，别每次读磁盘
        self._preview_sig: tuple = ()
        self._preview_images: list = []
        self._build_ui()
        self.store.changed.connect(self.refresh)
        self.store.start_watching()
        self.refresh()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 顶部：当前可粘贴的剪贴板内容（特殊标记 = 主色底 + ● 圆点）
        self.current_banner = _ClickLabel()
        self.current_banner.setObjectName("clipCurrent")
        self.current_banner.setCursor(Qt.PointingHandCursor)
        self.current_banner.clicked.connect(self._on_banner_clicked)
        root.addWidget(self.current_banner)

        top = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        top.addWidget(self.count_label)
        top.addStretch(1)
        self.clear_btn = QPushButton("清空历史")
        self.clear_btn.setToolTip("清空非收藏的历史记录（收藏项会保留）")
        self.clear_btn.clicked.connect(self._on_clear)
        top.addWidget(self.clear_btn)
        root.addLayout(top)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        # ---- 上：历史 | 收藏 两个独立面板（收藏单独放一侧）----
        panes = QSplitter(Qt.Horizontal)
        panes.setChildrenCollapsible(False)
        panes.setHandleWidth(6)

        self.hist_title = QLabel("历史")
        self.list = _ClipList()
        panes.addWidget(self._pane(self.hist_title, self.list))
        self.fav_title = QLabel("收藏")
        self.fav_list = _ClipList()
        panes.addWidget(self._pane(self.fav_title, self.fav_list))
        panes.setSizes([440, 340])
        splitter.addWidget(panes)

        # ---- 下：详情 / 预览 ----
        detail = QWidget()
        detail_lay = QVBoxLayout(detail)
        detail_lay.setContentsMargins(0, 6, 0, 0)
        detail_lay.setSpacing(6)
        self.detail_head = QLabel()
        self.detail_head.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        detail_lay.addWidget(self.detail_head)

        # 预览区两页：文本一页、图片网格一页（见 _sync_detail 决定翻哪页）
        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(PREVIEW_AREA_H)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview.setStyleSheet(
            f"color:{theme.token('text')};background:{theme.token('card_bg')};"
            f"border:1px solid {theme.token('border')};border-radius:6px;"
            f"padding:8px;font-size:9.5pt;")
        self.gallery = ImageGallery()
        self.stack.addWidget(self.preview)
        self.stack.addWidget(self.gallery)
        detail_lay.addWidget(self.stack, 1)
        splitter.addWidget(detail)
        splitter.setSizes([300, 200])

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.copy_btn = QPushButton("复制到剪贴板")
        set_variant(self.copy_btn, "primary")
        self.copy_btn.setToolTip("把这条记录重新放进剪贴板（双击列表行同效）")
        self.copy_btn.clicked.connect(self._on_copy)
        buttons.addWidget(self.copy_btn)
        self.edit_btn = QPushButton("修改")
        self.edit_btn.setToolTip("修改文本内容（图片/文件没有可改的内容）")
        self.edit_btn.clicked.connect(self._on_edit)
        buttons.addWidget(self.edit_btn)
        self.fav_btn = QPushButton("收藏")
        self.fav_btn.setToolTip("收藏的记录会移到右侧「收藏」面板")
        self.fav_btn.clicked.connect(self._on_toggle_favorite)
        buttons.addWidget(self.fav_btn)
        buttons.addStretch(1)
        self.del_btn = QPushButton("删除")
        set_variant(self.del_btn, "danger")
        self.del_btn.setToolTip("删除选中的记录（按住 Ctrl / Shift 多选，"
                                "Ctrl+A 全选，或直接按 Delete）")
        self.del_btn.clicked.connect(self._on_delete)
        buttons.addWidget(self.del_btn)
        root.addLayout(buttons)

        self._active_list = self.list
        self._sync_detail()

    def _pane(self, title: QLabel, listw: _ClipList) -> QWidget:
        """一个面板 = 标题 + 列表（历史 / 收藏共用同一套样式与行为）。"""
        title.setStyleSheet(
            f"color:{theme.token('primary')};font-size:10pt;font-weight:700;")
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(title)

        listw.setObjectName("clipList")
        listw.setSelectionMode(QAbstractItemView.ExtendedSelection)  # 批量选择
        listw.setIconSize(QSize(28, 28))
        listw.setAlternatingRowColors(True)
        listw.setTextElideMode(Qt.ElideRight)
        # 横向不滚动：行文字按宽度省略（否则长摘要会顶出一条横向滚动条）
        listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        listw.setToolTip("按住 Ctrl / Shift 可多选，Ctrl+A 全选，Delete 删除选中")
        listw.setStyleSheet(
            f"QListWidget#clipList{{font-size:9.5pt;outline:none;"
            f"border:1px solid {theme.token('border')};border-radius:6px;}}"
            f"QListWidget#clipList::item{{height:30px;padding:2px 6px;}}")
        listw.itemSelectionChanged.connect(
            lambda w=listw: self._on_selection_changed(w))
        listw.itemDoubleClicked.connect(lambda *_: self._on_copy())
        listw.deleteRequested.connect(self._on_delete)
        lay.addWidget(listw, 1)
        return box

    # ---------- 刷新 ----------
    def refresh(self) -> None:
        self._refresh_list(self.list, self.store.history_entries())
        self._refresh_list(self.fav_list, self.store.favorite_entries())
        self._refresh_banner()
        total = len(self.store.entries)
        favorites = len([e for e in self.store.entries if e.favorite])
        self.count_label.setText(f"共 {total} 条 · 收藏 {favorites} 条"
                                 f"（上限 {MAX_ENTRIES}）")
        self.hist_title.setText(f"历史（{total - favorites}）")
        self.fav_title.setText(f"收藏（{favorites}）")
        self.clear_btn.setEnabled(total > 0)
        self._sync_detail()

    def _refresh_list(self, listw: QListWidget, entries: list[ClipEntry]) -> None:
        keep = {str(i.data(_ROLE_UID)) for i in listw.selectedItems()}
        current_uid = self.store.current_uid()
        listw.blockSignals(True)
        listw.clear()
        for entry in entries:
            is_current = bool(current_uid) and entry.uid == current_uid
            item = QListWidgetItem(self._row_text(entry, is_current))
            item.setData(_ROLE_UID, entry.uid)
            item.setToolTip(self._row_tooltip(entry, is_current))
            thumb = self._thumbnail(entry)
            if thumb is not None:
                item.setIcon(thumb)
            if is_current:
                # 特殊标记：加粗 + 主色，和普通行一眼分得开
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setForeground(QColor(theme.token("primary")))
            listw.addItem(item)
            if entry.uid in keep:
                item.setSelected(True)
        listw.blockSignals(False)

    @staticmethod
    def _row_text(entry: ClipEntry, is_current: bool) -> str:
        mark = "● " if is_current else ""
        star = "★" if entry.favorite else "☆"
        return (f"{mark}{star}  {entry.clock()}  {entry.kind_label()}  "
                f"{entry.summary()}" + ("  ·已修改" if entry.edited else ""))

    @staticmethod
    def _row_tooltip(entry: ClipEntry, is_current: bool) -> str:
        lines = [f"{entry.stamp()} · {entry.kind_label()}"]
        if is_current:
            lines.append("● 这就是当前剪贴板里的内容，可以直接粘贴")
        if entry.kind == KIND_TEXT:
            lines.append(entry.text[:500])
        elif entry.kind == KIND_FILES:
            lines.extend(entry.files[:10])
        return "\n".join(lines)

    def _refresh_banner(self) -> None:
        """顶部横幅：当前剪贴板里到底是什么（也就是此刻能粘贴的东西）。

        摘要截到 `BANNER_CHARS`：横幅只做"一眼看个大概"，太长会把窗口顶宽
        （横向 size policy 已设 Ignored，这里再截一刀，免得贴着边缘被生硬裁掉）。
        """
        t = theme.token
        entry = self.store.current
        if entry is None:
            text = "剪贴板为空 · 没有可粘贴的内容"
            style = (f"color:{t('text_muted')};background:{t('hover_bg')};"
                     f"border:1px solid {t('border_light')};")
            tooltip = ""
        else:
            text = (f"● 当前可粘贴 · {entry.kind_label()} · "
                    f"{_short(entry.summary(), BANNER_CHARS)}")
            style = (f"color:{t('primary')};background:{t('primary_soft')};"
                     f"border:1px solid {t('primary')};font-weight:600;")
            tooltip = self._row_tooltip(entry, True)
        self.current_banner.setText(text)
        self.current_banner.setToolTip(tooltip)
        self.current_banner.setStyleSheet(
            "QLabel{border-radius:6px;padding:6px 10px;font-size:9.5pt;"
            + style + "}")

    @staticmethod
    def _thumbnail(entry: ClipEntry) -> QPixmap | None:
        if entry.kind != KIND_IMAGE or entry.image is None:
            return None
        return QPixmap.fromImage(entry.image).scaled(
            28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    # ---------- 选中态 ----------
    def _on_selection_changed(self, source: QListWidget) -> None:
        if source.selectedItems():
            self._active_list = source
        self._sync_detail()

    def selected_entries(self) -> list[ClipEntry]:
        """当前选中的记录（可能多条；取自最近操作的那个面板）。"""
        listw = self._active_list
        if listw is None:
            return []
        out = []
        for item in listw.selectedItems():
            entry = self.store.get(str(item.data(_ROLE_UID) or ""))
            if entry is not None:
                out.append(entry)
        return out

    def selected_entry(self) -> ClipEntry | None:
        """只选中一条时返回它，否则 None（多选时没有"唯一那条"）。"""
        entries = self.selected_entries()
        return entries[0] if len(entries) == 1 else None

    def _sync_detail(self) -> None:
        entries = self.selected_entries()
        count = len(entries)
        self.copy_btn.setEnabled(count == 1)
        self.edit_btn.setEnabled(count == 1 and entries[0].kind == KIND_TEXT)
        self.fav_btn.setEnabled(count >= 1)
        self.del_btn.setEnabled(count >= 1)
        if count > 1:
            self.fav_btn.setText("收藏选中")
            self.del_btn.setText(f"删除选中（{count}）")
        else:
            self.fav_btn.setText(
                "取消收藏" if count == 1 and entries[0].favorite else "收藏")
            self.del_btn.setText("删除")

        targets, source = self._preview_targets()
        images = self._preview_images_for(targets)
        if images:
            self._show_image_preview(images, targets, source)
        else:
            self._show_text_preview(targets, source)

    # ---------- 预览区 ----------
    def _preview_targets(self) -> tuple[list[ClipEntry], str]:
        """预览区展示谁：优先**选中的记录**；没选中就展示**当前剪贴板**。

        「当前剪贴板」这一路是「粘贴的是图片时下面能直接看到图」的关键 ——
        刚复制完一张图时列表里未必选中着什么。
        """
        entries = self.selected_entries()
        if entries:
            return entries, "selected"
        if self.store.current is not None:
            return [self.store.current], "current"
        return [], "none"

    def _preview_images_for(self, targets: list[ClipEntry]) -> list:
        """带缓存地取可预览图片：refresh() 很频繁，别每次都去读一遍磁盘上的图。"""
        signature = tuple(e.uid for e in targets)
        if signature != self._preview_sig:
            self._preview_sig = signature
            self._preview_images = preview_images(targets)
        return self._preview_images

    def _show_image_preview(self, images, targets, source: str) -> None:
        total = previewable_count(targets)
        head = f"图片预览：{len(images)} 张"
        if total > len(images):
            head += f"（共 {total} 张，只显示前 {len(images)} 张）"
        if source == "current":
            head = "● 当前剪贴板 · " + head
        elif len(targets) > 1:
            head = f"已选中 {len(targets)} 条 · " + head
        self.detail_head.setText(head + " · 点缩略图放大")
        self.gallery.show_images(images)
        self.stack.setCurrentWidget(self.gallery)

    def _show_text_preview(self, targets, source: str) -> None:
        self.stack.setCurrentWidget(self.preview)
        self.preview.setPixmap(QPixmap())
        if not targets:
            self.detail_head.setText("选中一条记录查看完整内容（可 Ctrl / Shift 多选）")
            self.preview.setText("")
            return
        if len(targets) > 1:
            self.detail_head.setText(f"已选中 {len(targets)} 条记录")
            self.preview.setText("\n".join(
                f"· {e.kind_label()}  {e.summary()}" for e in targets[:20]))
            return

        entry = targets[0]
        head = (f"{entry.stamp()} · {entry.kind_label()} · "
                f"{human_size(entry.byte_size())}")
        if entry.edited:
            head += "　·　已修改"
        if self.store.is_current(entry):
            head += "　·　● 当前剪贴板"
        if source == "current":
            head = "● 当前剪贴板 · " + head
        self.detail_head.setText(head)
        if entry.kind == KIND_FILES:
            self.preview.setText("复制的文件：\n" + "\n".join(entry.files))
        else:
            text = entry.text
            if len(text) > PREVIEW_CHARS:
                text = text[:PREVIEW_CHARS] + f"\n…（共 {len(entry.text)} 字）"
            self.preview.setText(text)

    # ---------- 操作 ----------
    def _on_banner_clicked(self) -> None:
        """点横幅：在列表里选中当前剪贴板那条（好看到它的完整内容）。"""
        uid = self.store.current_uid()
        if not uid:
            return
        for listw in (self.list, self.fav_list):
            for index in range(listw.count()):
                item = listw.item(index)
                if str(item.data(_ROLE_UID)) == uid:
                    listw.setCurrentItem(item)
                    listw.setFocus()
                    return

    def _on_copy(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        apply_entry_to_clipboard(entry)

    def _on_edit(self) -> None:
        entry = self.selected_entry()
        if entry is None or entry.kind != KIND_TEXT:
            return
        dialog = EditTextDialog(entry.text, self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.store.update_text(entry.uid, dialog.text())

    def _on_toggle_favorite(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        if len(entries) == 1:
            self.store.toggle_favorite(entries[0].uid)
            return
        # 多选：全是收藏 -> 全部取消收藏；否则全部收藏
        self.store.set_favorite_many([e.uid for e in entries],
                                     not all(e.favorite for e in entries))

    def _on_delete(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        if len(entries) == 1:
            entry = entries[0]
            title = "删除记录"
            text = f"确定删除这条{entry.kind_label()}记录吗？\n\n{entry.summary()}"
        else:
            title = "批量删除"
            text = f"确定删除选中的 {len(entries)} 条记录吗？"
        answer = QMessageBox.question(self, title, text,
                                      QMessageBox.Yes | QMessageBox.No,
                                      QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.store.remove_many([e.uid for e in entries])

    def _on_clear(self) -> None:
        answer = QMessageBox.question(
            self, "清空历史",
            "清空所有非收藏的历史记录？（收藏项会保留）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.store.clear(keep_favorites=True)

    # ---------- 收尾 ----------
    def shutdown(self) -> None:
        """关窗时停止监听（记录已经即时落盘，重新打开还在）。

        幂等：宿主可能调一次、测试清理又调一次，重复 disconnect 会打
        PySide 的 RuntimeWarning（不是异常，catch 不到），所以自己记个标志。
        """
        if not getattr(self, "_shut_down", False):
            self._shut_down = True
            try:
                self.store.changed.disconnect(self.refresh)
            except (RuntimeError, TypeError):
                pass
        self.store.stop_watching()


# 登记到「小工具」页（key 一旦发布不要再改）
register(MiniApp(
    key="clipboard_history",
    name="剪贴板",
    icon="📋",
    desc="实时记录剪贴板历史（文本/图片/文件），收藏单独一侧，可多选批量删除，"
         "数据永久保存在本地",
    factory=ClipboardHistoryWidget,
))
