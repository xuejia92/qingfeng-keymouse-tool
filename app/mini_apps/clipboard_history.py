# -*- coding: utf-8 -*-
"""内置小程序：剪贴板历史。

功能边界（**刻意划的线**，2026-10-03）
------------------------------------
只做这些：
- **窗口打开期间**实时记录剪贴板变化，支持**文本 / 图片 / 文件**三类；
- 按时间倒序展示，每条可**复制回剪贴板 / 修改（仅文本）/ 收藏 / 删除**；
- 历史条数上限 `MAX_ENTRIES`，超出丢最旧的（收藏项不参与淘汰）。

刻意**不做**（都在这里写清楚，免得以为是漏了）：
- **不常驻后台**：记录只在窗口开着时进行，关窗即停止（小程序的模型就是"用完即走"）；
- **不落盘**：剪贴板里经常有密码、验证码这类东西，写进磁盘风险太大，所以历史与收藏
  都只在内存里、随进程结束消失。历史本身在**同一个进程内**跨窗口开关是保留的
  （模块级单例 store，见 `default_store`），方便"关了又开还在"；
- 不做富文本 / HTML 片段 / 自定义格式的记录，只认上面三类；
- 不做全局快捷键唤起、不做搜索（"界面简洁"优先；真需要再加）。

数据结构
--------
`ClipEntry`：uid + kind(text/image/files) + 对应载荷 + 时间戳 + 收藏标记。
类型判定优先级 **文件 > 图片 > 文本**：从资源管理器复制文件时，很多程序同时会往
剪贴板塞一个文本（甚至图片），取"信息量最大"的那个才算复制的本意。
去重按**内容**做：同一份内容再复制一次不会新增，而是把它**提到最前面**并刷新时间
（标准剪贴板历史的行为）。

交互
----
列表一行 = `[★] 时间 类型 摘要`；选中后下方预览完整内容（图片显示缩略图），
底部一排按钮：复制 / 修改 / 收藏 / 删除。**双击一行 = 复制回剪贴板**（最常用动作）。
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QMimeData, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from ..ui import theme
from ..ui.frameless import FramelessDialog
from ..ui.widgets import human_size, set_variant
from . import MiniApp, register

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_FILES = "files"

KIND_LABELS = {KIND_TEXT: "文本", KIND_IMAGE: "图片", KIND_FILES: "文件"}

MAX_ENTRIES = 200                   # 历史条数上限（收藏项不参与淘汰）
MAX_TEXT_CHARS = 500_000            # 单条文本上限，超出截断（防止复制巨型日志吃内存）
PREVIEW_CHARS = 300                 # 预览区最多展示多少字
LIST_SUMMARY_CHARS = 60             # 列表行摘要最多展示多少字
POLL_MS = 500                       # 兜底轮询间隔（见 clipboard_sequence 的说明）

_ROLE_UID = Qt.UserRole


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
    image: QImage | None = None             # kind=image（内存里的 QImage，不落盘）
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
        flat = " ".join(self.text.split())
        return flat[:limit] + ("…" if len(flat) > limit else "")

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
# 历史仓库（含剪贴板监听）
# ---------------------------------------------------------------------------
class ClipboardStore(QObject):
    """历史记录 + 剪贴板监听。

    用法：`start_watching()` / `stop_watching()` 成对调用；历史留在内存里，
    所以窗口关了再开还在（同进程内），退出程序即消失。

    ⚠️ 一律按**索引**遍历 `entries`：`ClipEntry` 是 dataclass，`__eq__` 比的是字段值，
    两条内容相同的记录用 `list.index()` 会命错行（2026-10-03 写的时候差点踩到）。
    """

    changed = Signal()          # 记录增删改（界面据此刷新）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[ClipEntry] = []
        self._watching = False
        self._digests: list[str] = []       # 与 entries 一一对应的指纹（去重用）
        self._last_seq = -1                 # 上一次看到的剪贴板序号（兜底轮询用）
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_tick)

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
        return self.add(entry)

    # ---------- 增删改 ----------
    def add(self, entry: ClipEntry) -> ClipEntry | None:
        """入库：同内容不新增，改为**提到最前面**并刷新时间。

        返回真正新增的那条；命中重复时返回 None（但它已经被移到最前了）。
        """
        digest = entry.digest()
        for index, existing in enumerate(self._digests):
            if existing == digest:
                moved = self.entries.pop(index)
                self._digests.pop(index)
                moved.created_at = entry.created_at
                self.entries.insert(0, moved)
                self._digests.insert(0, digest)
                self.changed.emit()
                return None
        self.entries.insert(0, entry)
        self._digests.insert(0, digest)
        self._trim()
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
        for index, entry in enumerate(self.entries):
            if entry.uid == uid:
                self.entries.pop(index)
                self._digests.pop(index)
                self.changed.emit()
                return True
        return False

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
            self.changed.emit()
        return removed

    def toggle_favorite(self, uid: str) -> bool | None:
        """切换收藏；返回切换后的状态（找不到返回 None）。"""
        for entry in self.entries:
            if entry.uid == uid:
                entry.favorite = not entry.favorite
                self.changed.emit()
                return entry.favorite
        return None

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
            self.changed.emit()
            return True
        return False

    def get(self, uid: str) -> ClipEntry | None:
        return next((e for e in self.entries if e.uid == uid), None)

    def visible_entries(self, favorites_only: bool = False) -> list[ClipEntry]:
        """给界面用的列表：收藏项**置顶**，其余保持时间倒序。"""
        items = [e for e in self.entries if e.favorite] if favorites_only \
            else list(self.entries)
        return sorted(items, key=lambda e: (not e.favorite, -e.created_at))


_DEFAULT_STORE: ClipboardStore | None = None


def default_store() -> ClipboardStore:
    """进程内共享的历史仓库（窗口关了再开，历史还在）。"""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = ClipboardStore()
    return _DEFAULT_STORE


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
# 主界面
# ---------------------------------------------------------------------------
class ClipboardHistoryWidget(QWidget):
    """剪贴板历史主控件（独立窗口里跑）。"""

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self.store = store if store is not None else default_store()
        self.setMinimumSize(760, 560)
        self._build_ui()
        self.store.changed.connect(self.refresh)
        self.store.start_watching()
        self.refresh()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.fav_only = QCheckBox("只看收藏")
        self.fav_only.toggled.connect(lambda _=False: self.refresh())
        top.addWidget(self.fav_only)
        top.addStretch(1)
        self.count_label = QLabel()
        self.count_label.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        top.addWidget(self.count_label)
        self.clear_btn = QPushButton("清空历史")
        self.clear_btn.setToolTip("清空非收藏的历史记录（收藏项会保留）")
        self.clear_btn.clicked.connect(self._on_clear)
        top.addWidget(self.clear_btn)
        root.addLayout(top)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        self.list = QListWidget()
        self.list.setObjectName("clipList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setIconSize(QSize(28, 28))
        self.list.setAlternatingRowColors(True)
        self.list.itemSelectionChanged.connect(self._sync_detail)
        self.list.itemDoubleClicked.connect(lambda *_: self._on_copy())
        self.list.setStyleSheet(
            f"QListWidget#clipList{{font-size:9.5pt;outline:none;"
            f"border:1px solid {theme.token('border')};border-radius:6px;}}"
            f"QListWidget#clipList::item{{height:30px;padding:2px 6px;}}")
        splitter.addWidget(self.list)

        detail = QWidget()
        detail_lay = QVBoxLayout(detail)
        detail_lay.setContentsMargins(0, 6, 0, 0)
        detail_lay.setSpacing(6)
        self.detail_head = QLabel()
        self.detail_head.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        detail_lay.addWidget(self.detail_head)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview.setStyleSheet(
            f"color:{theme.token('text')};background:{theme.token('card_bg')};"
            f"border:1px solid {theme.token('border')};border-radius:6px;"
            f"padding:8px;font-size:9.5pt;")
        self.preview.setMinimumHeight(120)
        detail_lay.addWidget(self.preview, 1)
        splitter.addWidget(detail)
        splitter.setSizes([340, 160])

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
        self.fav_btn.clicked.connect(self._on_toggle_favorite)
        buttons.addWidget(self.fav_btn)
        buttons.addStretch(1)
        self.del_btn = QPushButton("删除")
        set_variant(self.del_btn, "danger")
        self.del_btn.clicked.connect(self._on_delete)
        buttons.addWidget(self.del_btn)
        root.addLayout(buttons)

        self._selected_uid = ""

    # ---------- 刷新 ----------
    def refresh(self) -> None:
        keep = self._selected_uid
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.store.visible_entries(self.fav_only.isChecked()):
            item = QListWidgetItem(
                f"{'★' if entry.favorite else '☆'}  {entry.clock()}  "
                f"{entry.kind_label()}  {entry.summary()}"
                + ("  ·已修改" if entry.edited else ""))
            item.setData(_ROLE_UID, entry.uid)
            item.setToolTip(f"{entry.stamp()} · {entry.kind_label()}")
            thumb = self._thumbnail(entry)
            if thumb is not None:
                item.setIcon(thumb)
            self.list.addItem(item)
            if entry.uid == keep:
                item.setSelected(True)
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        total = len(self.store.entries)
        favorites = len([e for e in self.store.entries if e.favorite])
        self.count_label.setText(f"共 {total} 条 · 收藏 {favorites} 条"
                                 f"（上限 {MAX_ENTRIES}）")
        self.clear_btn.setEnabled(total > 0)
        self._sync_detail()

    @staticmethod
    def _thumbnail(entry: ClipEntry) -> QPixmap | None:
        if entry.kind != KIND_IMAGE or entry.image is None:
            return None
        return QPixmap.fromImage(entry.image).scaled(
            28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    # ---------- 选中态 ----------
    def selected_entry(self) -> ClipEntry | None:
        item = self.list.currentItem()
        if item is None:
            return None
        return self.store.get(str(item.data(_ROLE_UID) or ""))

    def _sync_detail(self) -> None:
        entry = self.selected_entry()
        self._selected_uid = entry.uid if entry else ""
        has = entry is not None
        for button in (self.copy_btn, self.edit_btn, self.fav_btn, self.del_btn):
            button.setEnabled(has)
        if entry is None:
            self.detail_head.setText("选中一条记录查看完整内容")
            self.preview.setPixmap(QPixmap())
            self.preview.setText("")
            return
        self.fav_btn.setText("取消收藏" if entry.favorite else "收藏")
        self.edit_btn.setEnabled(entry.kind == KIND_TEXT)
        self.detail_head.setText(
            f"{entry.stamp()} · {entry.kind_label()} · {human_size(entry.byte_size())}"
            + ("　·　已修改" if entry.edited else ""))
        if entry.kind == KIND_IMAGE and entry.image is not None:
            self.preview.setText("")
            self.preview.setPixmap(QPixmap.fromImage(entry.image).scaled(
                420, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        elif entry.kind == KIND_FILES:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("复制的文件：\n" + "\n".join(entry.files))
        else:
            self.preview.setPixmap(QPixmap())
            text = entry.text
            if len(text) > PREVIEW_CHARS:
                text = text[:PREVIEW_CHARS] + f"\n…（共 {len(entry.text)} 字）"
            self.preview.setText(text)

    # ---------- 操作 ----------
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
        entry = self.selected_entry()
        if entry is not None:
            self.store.toggle_favorite(entry.uid)

    def _on_delete(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        answer = QMessageBox.question(
            self, "删除记录",
            f"确定删除这条{entry.kind_label()}记录吗？\n\n{entry.summary()}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.store.remove(entry.uid)

    def _on_clear(self) -> None:
        answer = QMessageBox.question(
            self, "清空历史",
            "清空所有非收藏的历史记录？（收藏项会保留）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.store.clear(keep_favorites=True)

    # ---------- 收尾 ----------
    def shutdown(self) -> None:
        """关窗时停止监听（历史留在进程内的 store 里，重新打开还在）。

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
    desc="实时记录剪贴板历史（文本/图片/文件），可复制、修改、收藏、删除",
    factory=ClipboardHistoryWidget,
))
