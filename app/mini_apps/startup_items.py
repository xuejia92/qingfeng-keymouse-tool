# -*- coding: utf-8 -*-
"""内置小程序：启动项管理（管理本机开机自启的程序）。

能做什么
--------
- **列出**当前电脑的开机自启项：当前用户 / 所有用户的注册表
  `...\\CurrentVersion\\Run`，以及两个「启动」文件夹（当前用户 / 所有用户）；
- **添加**：把指定程序写进 `Run` 键（当前用户无需管理员；所有用户需要管理员）；
- **禁用 / 启用**：写 Windows 自己那套 `Explorer\\StartupApproved` 标记
  （「任务管理器 -> 启动应用」用的就是这个机制），**可逆**、不删数据；
- **删除**：移除注册表值 / 把启动文件夹里的快捷方式丢进**回收站**（不是直接删）。

⚠️ 刻意**不碰任务计划程序**：那里「登录时触发」的任务数量多、要改得走 `schtasks`，
误删影响面大。本工具只覆盖注册表与启动文件夹这两处主流位置，界面上写明了，
免得用户以为「列表就是全部」。

设计要点（可测性）
------------------
所有注册表读写都收口在 `_reg_*` 五个函数里（`root` 用 `"HKCU"`/`"HKLM"` 符号名），
启动文件夹路径收口在 `_startup_folder`，回收站删除收口在 `_recycle`。
单测整体替换这一层即可，**绝不碰真实注册表**。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..ui import theme
from ..ui.frameless import FramelessDialog
from ..ui.widgets import set_variant
from . import MiniApp, register

# ---------------------------------------------------------------------------
# 位置常量
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_RUN_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                    r"\Explorer\StartupApproved\Run")
APPROVED_FOLDER_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                       r"\Explorer\StartupApproved\StartupFolder")

SCOPE_USER = "user"          # 当前用户（HKCU，无需管理员）
SCOPE_MACHINE = "machine"    # 所有用户（HKLM，需要管理员）

_HIVES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}
_ROOT_OF_SCOPE = {SCOPE_USER: "HKCU", SCOPE_MACHINE: "HKLM"}
_SCOPE_LABEL = {SCOPE_USER: "当前用户", SCOPE_MACHINE: "所有用户"}

# Windows 判定「启用/禁用」的 12 字节标记（首字节奇数 = 已禁用）
_BLOB_ENABLED = bytes([0x02] + [0x00] * 11)
_BLOB_DISABLED = bytes([0x03] + [0x00] * 11)

SOURCE_REG = "reg"
SOURCE_FOLDER = "folder"
_SOURCE_LABEL = {SOURCE_REG: "注册表", SOURCE_FOLDER: "启动文件夹"}

_IS_WIN = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 注册表 / 文件系统这一层（单测整体替换）
# ---------------------------------------------------------------------------
def _hive(root: str):
    import winreg
    return getattr(winreg, _HIVES[root])


def _reg_read_values(root: str, path: str) -> dict[str, str]:
    """读一个注册表项下所有「字符串值」；项不存在返回空 dict。"""
    if not _IS_WIN:
        return {}
    try:
        import winreg
        out: dict[str, str] = {}
        with winreg.OpenKey(_hive(root), path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break                      # 枚举到头（或中途出错）都收工
                out[name] = str(value)
                index += 1
        return out
    except OSError:
        return {}


def _reg_read_binary(root: str, path: str) -> dict[str, bytes]:
    """读一个注册表项下所有「二进制值」（StartupApproved 就存在这里）。"""
    if not _IS_WIN:
        return {}
    try:
        import winreg
        out: dict[str, bytes] = {}
        with winreg.OpenKey(_hive(root), path, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                if isinstance(value, bytes):
                    out[name] = value
                index += 1
        return out
    except OSError:
        return {}


def _reg_write_value(root: str, path: str, name: str, value: str) -> None:
    """写字符串值（项不存在则创建）。失败抛 OSError（含 PermissionError）。"""
    import winreg
    with winreg.CreateKey(_hive(root), path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _reg_write_binary(root: str, path: str, name: str, data: bytes) -> None:
    import winreg
    with winreg.CreateKey(_hive(root), path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, data)


def _reg_delete_value(root: str, path: str, name: str) -> None:
    """删字符串值；本来就没有算成功（幂等）。"""
    import winreg
    with winreg.OpenKey(_hive(root), path, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def _reg_delete_binary(root: str, path: str, name: str) -> None:
    """删二进制值；本来就没有算成功（幂等）。"""
    import winreg
    try:
        with winreg.OpenKey(_hive(root), path, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass


def _startup_folder(scope: str) -> str:
    """「启动」文件夹路径（当前用户 / 所有用户）。"""
    if scope == SCOPE_MACHINE:
        base = os.environ.get("ProgramData", r"C:\ProgramData")
    else:
        base = os.environ.get("APPDATA", "")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def _recycle(path: str) -> None:
    """把文件删到**回收站**；回收站不可用时退回直接删除。

    ⚠️ ctypes 调 Win32 必须显式设 argtypes/restype（项目 2026-10-02 的教训：
    不设参数类型时指针/句柄会被按 32 位截断，静默失效）。
    """
    if not _IS_WIN:
        os.remove(path)
        return
    import ctypes
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 0x0003
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040          # 关键：进回收站而不是彻底删除

    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int

    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    # pFrom 是「双 null 结尾」的路径列表，单个文件也要多一个 \0
    op.pFrom = path + "\0\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    if shell32.SHFileOperationW(ctypes.byref(op)) != 0:
        os.remove(path)             # 网络盘/无回收站等情况：退回直接删


def is_admin() -> bool:
    """当前进程是否有管理员权限（决定能不能改 HKLM / 所有用户位置）。"""
    if not _IS_WIN:
        return False
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = ctypes.c_int
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class StartupItem:
    """一个开机自启项。"""

    name: str
    command: str
    source: str = SOURCE_REG        # SOURCE_REG / SOURCE_FOLDER
    scope: str = SCOPE_USER         # SCOPE_USER / SCOPE_MACHINE
    enabled: bool = True
    path: str = ""                  # 启动文件夹项的 .lnk 完整路径（注册表项为空）

    @property
    def location(self) -> str:
        """界面上「位置」一列的文字。"""
        return f"{_SOURCE_LABEL[self.source]} · {_SCOPE_LABEL[self.scope]}"

    @property
    def registry_root(self) -> str:
        return _ROOT_OF_SCOPE[self.scope]

    @property
    def writable(self) -> bool:
        """本项能不能改（所有用户位置需要管理员）。"""
        return self.scope == SCOPE_USER or is_admin()

    def target_path(self) -> str:
        """尽量解析出它启动的**可执行文件**路径（解析不出返回空串）。

        注册表项从命令行里抠；启动文件夹项从 .lnk 内容里猜（见 `_peek_lnk_target`）。
        """
        if self.source == SOURCE_FOLDER:
            return _peek_lnk_target(self.path)
        return _exe_from_command(self.command)


def _exe_from_command(command: str) -> str:
    """从注册表命令行里取出 exe 路径（兼容带引号 / 不带引号 / 路径含空格三种写法）。"""
    cmd = (command or "").strip()
    if not cmd:
        return ""
    if cmd.startswith('"'):
        parts = cmd.split('"', 2)
        raw = parts[1] if len(parts) > 1 else cmd.strip('"')
    else:
        # ⚠️ 不能只按第一个空格切：`D:\\Program Files\\X\\x.exe -silent` 这种
        # 不带引号但路径含空格的写法很常见，切出来会是 "D:\\Program"（2026-10-03 实测踩到）。
        # 优先匹配「到第一个可执行扩展名为止」，匹配不到再退回按空格切。
        match = re.match(r"^([^\r\n\t]*?\.(?:exe|bat|cmd|com))(?:\s|$)", cmd,
                         re.IGNORECASE)
        raw = match.group(1) if match else cmd.split(" ", 1)[0]
    return raw if raw.lower().endswith((".exe", ".bat", ".cmd", ".com")) else ""


def _peek_lnk_target(path: str) -> str:
    """尽力从 .lnk 文件里猜出目标路径（只读探测，猜不到返回空串）。

    不引第三方库：.lnk 里的目标路径以 UTF-16LE 存着，直接在字节里找
    「盘符:\\...\\.exe」形态的串即可。猜到的结果只用于显示和「打开位置」，
    猜不到就退回显示快捷方式本身，不影响任何管理功能。
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(64 * 1024)
    except OSError:
        return ""
    text = data.decode("utf-16-le", errors="ignore")
    best = ""
    for match in re.finditer(
            r"[A-Za-z]:\\[^\x00\r\n\t\"<>|*?]{0,200}?\.(?:exe|bat|cmd|com)",
            text, re.IGNORECASE):
        if len(match.group(0)) > len(best):
            best = match.group(0)
    return best


def _is_enabled(blob: bytes | None) -> bool:
    """StartupApproved 标记 -> 是否启用。没有标记 = 启用（Windows 默认）。"""
    if not blob:
        return True
    return (blob[0] & 1) == 0        # 首字节奇数 = 已被禁用


# ---------------------------------------------------------------------------
# 对外功能
# ---------------------------------------------------------------------------
def list_items() -> list[StartupItem]:
    """列出全部可管理的开机自启项（注册表 + 启动文件夹）。"""
    items: list[StartupItem] = []
    for scope in (SCOPE_USER, SCOPE_MACHINE):
        root = _ROOT_OF_SCOPE[scope]
        approved = _reg_read_binary(root, APPROVED_RUN_KEY)
        for name, command in sorted(_reg_read_values(root, RUN_KEY).items()):
            items.append(StartupItem(name=name, command=command,
                                     source=SOURCE_REG, scope=scope,
                                     enabled=_is_enabled(approved.get(name))))
        folder = _startup_folder(scope)
        approved_folder = _reg_read_binary(root, APPROVED_FOLDER_KEY)
        try:
            filenames = sorted(os.listdir(folder))
        except OSError:
            filenames = []
        for filename in filenames:
            if filename.lower() == "desktop.ini":
                continue
            full = os.path.join(folder, filename)
            if not os.path.isfile(full):
                continue
            target = _peek_lnk_target(full) if filename.lower().endswith(".lnk") else ""
            items.append(StartupItem(
                name=os.path.splitext(filename)[0],
                command=target or full,
                source=SOURCE_FOLDER, scope=scope, path=full,
                enabled=_is_enabled(approved_folder.get(filename))))
    return items


def set_enabled(item: StartupItem, enabled: bool) -> None:
    """启用 / 禁用一项：写 StartupApproved 标记（可逆，不动原数据）。

    失败抛 OSError（权限不足时是 PermissionError）。
    """
    blob = _BLOB_ENABLED if enabled else _BLOB_DISABLED
    root = item.registry_root
    if item.source == SOURCE_REG:
        _reg_write_binary(root, APPROVED_RUN_KEY, item.name, blob)
    else:
        _reg_write_binary(root, APPROVED_FOLDER_KEY,
                          os.path.basename(item.path), blob)


def delete_item(item: StartupItem) -> None:
    """删除一项：注册表值删掉、启动文件夹里的快捷方式进回收站。"""
    root = item.registry_root
    if item.source == SOURCE_REG:
        _reg_delete_value(root, RUN_KEY, item.name)
        _reg_delete_binary(root, APPROVED_RUN_KEY, item.name)   # 顺手清掉禁用标记
    else:
        _recycle(item.path)
        _reg_delete_binary(root, APPROVED_FOLDER_KEY,
                           os.path.basename(item.path))


def validate_add(name: str, command: str) -> str:
    """校验「添加」的输入，返回错误提示（空串 = 通过）。"""
    name = (name or "").strip()
    if not name:
        return "请填写名称"
    if "\\" in name:
        return "名称里不能有反斜杠（注册表值名不允许）"
    if len(name) > 200:
        return "名称太长"
    if not (command or "").strip():
        return "请填写要启动的程序或命令"
    return ""


def name_exists(scope: str, name: str) -> bool:
    """指定位置里是否已有同名启动项（添加时用来提示覆盖）。"""
    root = _ROOT_OF_SCOPE.get(scope, "HKCU")
    return name in _reg_read_values(root, RUN_KEY)


def add_registry_item(name: str, command: str, scope: str = SCOPE_USER) -> None:
    """把程序写进 Run 键，并清掉可能存在的旧禁用标记（新加的必须有资格启动）。

    失败抛 OSError（改「所有用户」需要管理员）。
    """
    root = _ROOT_OF_SCOPE.get(scope, "HKCU")
    _reg_write_value(root, RUN_KEY, name, command)
    _reg_write_binary(root, APPROVED_RUN_KEY, name, _BLOB_ENABLED)


def build_command(program: str, args: str = "") -> str:
    """把「程序路径 + 参数」拼成写进注册表的命令行（路径一律加引号）。"""
    program = (program or "").strip().strip('"')
    args = (args or "").strip()
    if not program:
        return ""
    quoted = f'"{program}"'
    return f"{quoted} {args}" if args else quoted


# ---------------------------------------------------------------------------
# 添加对话框
# ---------------------------------------------------------------------------
class AddStartupDialog(FramelessDialog):
    """「添加启动项」表单：选程序 + 起名 + 参数 + 生效范围。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加开机启动项")
        self.setMinimumWidth(560)

        form = QFormLayout()
        form.setContentsMargins(16, 14, 16, 8)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row = QHBoxLayout()
        self.program_edit = QLineEdit()
        self.program_edit.setPlaceholderText("例如 C:\\Program Files\\工具\\tool.exe")
        row.addWidget(self.program_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("程序路径", row)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空则用程序文件名")
        form.addRow("名称", self.name_edit)

        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText("可留空，例如 --autostart")
        form.addRow("启动参数", self.args_edit)

        self.scope_box = QComboBox()
        self.scope_box.addItem("当前用户（无需管理员）", SCOPE_USER)
        self.scope_box.addItem("所有用户（需要管理员）", SCOPE_MACHINE)
        form.addRow("生效范围", self.scope_box)

        root = QVBoxLayout(self.body())
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(form)

        self.tip = QLabel("")
        self.tip.setWordWrap(True)
        self.tip.setStyleSheet(f"color:{theme.token('text_muted')};font-size:9pt;")
        self.tip.setContentsMargins(16, 0, 16, 4)
        root.addWidget(self.tip)
        self._sync_tip()
        self.scope_box.currentIndexChanged.connect(self._sync_tip)

        actions = QHBoxLayout()
        actions.setContentsMargins(16, 4, 16, 14)
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        ok = QPushButton("添加")
        set_variant(ok, "primary")
        ok.setDefault(True)
        ok.clicked.connect(self._on_accept)
        actions.addWidget(ok)
        root.addLayout(actions)

        self.warning = ""       # 校验失败时的提示（测试用）

    # ---------- 内部 ----------
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要开机自启的程序", "",
            "程序 (*.exe *.bat *.cmd *.lnk);;所有文件 (*)")
        if path:
            self.program_edit.setText(path)
            if not self.name_edit.text().strip():
                self.name_edit.setText(os.path.splitext(os.path.basename(path))[0])

    def _sync_tip(self) -> None:
        hive = "HKEY_LOCAL_MACHINE" if self.scope() == SCOPE_MACHINE \
            else "HKEY_CURRENT_USER"
        self.tip.setText(f"将写入注册表："
                         rf"{hive}\Software\Microsoft\Windows\CurrentVersion\Run")

    def _on_accept(self) -> None:
        error = validate_add(self.name(), self.command())
        if error:
            self.warning = error
            QMessageBox.warning(self, "添加启动项", error)
            return
        self.warning = ""
        self.accept()

    # ---------- 取值 ----------
    def program(self) -> str:
        return self.program_edit.text().strip()

    def name(self) -> str:
        """名称：留空则回落到程序文件名。"""
        typed = self.name_edit.text().strip()
        if typed:
            return typed
        return os.path.splitext(os.path.basename(self.program()))[0]

    def arguments(self) -> str:
        return self.args_edit.text().strip()

    def scope(self) -> str:
        return self.scope_box.currentData() or SCOPE_USER

    def command(self) -> str:
        return build_command(self.program(), self.arguments())


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
_COLUMNS = ("状态", "名称", "位置", "命令 / 目标")


class StartupManagerWidget(QWidget):
    """启动项管理主控件（放进独立窗口里跑）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[StartupItem] = []
        # 表格天然没有像样的 sizeHint，不给下限的话独立窗口会开成小小一条
        self.setMinimumSize(820, 440)
        self._build_ui()
        self.refresh()
        # 主题/字号变化时重刷列表配色（颜色是按令牌取的，不会自动重映射到行上）
        theme.register_listener(self._restyle)

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self.summary = QLabel("正在读取启动项…")
        self.summary.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 74)
        self.table.setColumnWidth(1, 220)
        self.table.setColumnWidth(2, 160)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.doubleClicked.connect(lambda *_: self._on_open())
        root.addWidget(self.table, 1)

        self.hint = QLabel("禁用 = 写 Windows 的 StartupApproved 标记（可随时启用回来）；"
                           "删除 = 移除注册表值 / 快捷方式进回收站。"
                           "任务计划程序里的自启项不在此列。")
        self.hint.setStyleSheet(f"color:{theme.token('text_muted')};font-size:9pt;")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        self.add_btn = QPushButton("添加程序…")
        set_variant(self.add_btn, "primary")
        self.add_btn.clicked.connect(self._on_add)
        row.addWidget(self.add_btn)
        row.addStretch(1)
        self.open_btn = QPushButton("打开位置")
        self.open_btn.clicked.connect(self._on_open)
        row.addWidget(self.open_btn)
        self.enable_btn = QPushButton("启用")
        set_variant(self.enable_btn, "success")
        self.enable_btn.clicked.connect(lambda: self._set_enabled(True))
        row.addWidget(self.enable_btn)
        self.disable_btn = QPushButton("禁用")
        self.disable_btn.clicked.connect(lambda: self._set_enabled(False))
        row.addWidget(self.disable_btn)
        self.delete_btn = QPushButton("删除")
        set_variant(self.delete_btn, "danger")
        self.delete_btn.clicked.connect(self._on_delete)
        row.addWidget(self.delete_btn)
        root.addLayout(row)

        self._sync_buttons()

    # ---------- 数据刷新 ----------
    def refresh(self, select: StartupItem | None = None) -> None:
        """重新读取启动项并重建表格。

        `select`：重建后要重新选中的项（按「启用/禁用」后保持在原条目上）；
        不传则**按行号**保持不变——刷新一次就从选中状态掉出去很难用。
        """
        keep = self.table.currentRow() if select is None else -1
        self._items = list_items()
        self.table.setRowCount(0)
        for item in self._items:
            self._append_row(item)
        self.table.setRowCount(len(self._items))
        enabled = sum(1 for i in self._items if i.enabled)
        self.summary.setText(
            f"共 {len(self._items)} 项（已启用 {enabled}、已禁用 "
            f"{len(self._items) - enabled}）· 注册表 Run + 启动文件夹")
        if select is not None:
            keep = self._row_of(select)
        if 0 <= keep < len(self._items):
            self.table.selectRow(keep)
        self._sync_buttons()

    def _append_row(self, item: StartupItem) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        status = QTableWidgetItem("已启用" if item.enabled else "已禁用")
        status.setTextAlignment(Qt.AlignCenter)
        status.setForeground(QColor(theme.token(
            "success" if item.enabled else "text_muted")))
        status.setData(Qt.UserRole, item)
        self.table.setItem(row, 0, status)
        for col, text in ((1, item.name), (2, item.location),
                          (3, item.command)):
            cell = QTableWidgetItem(text)
            cell.setToolTip(text)
            self.table.setItem(row, col, cell)

    def _restyle(self) -> None:
        """主题切换后重刷状态列颜色（颜色是按令牌取的，不重刷就停在旧主题）。"""
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is None:
                continue
            item = cell.data(Qt.UserRole)
            if item is None:
                continue
            cell.setForeground(QColor(theme.token(
                "success" if item.enabled else "text_muted")))

    # ---------- 选中项 ----------
    def selected_item(self) -> StartupItem | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _sync_buttons(self) -> None:
        """按选中项刷新按钮可用态（「所有用户」位置没管理员时禁掉改动的按钮）。"""
        item = self.selected_item()
        if item is None:
            for btn in (self.enable_btn, self.disable_btn, self.delete_btn,
                        self.open_btn):
                btn.setEnabled(False)
                btn.setToolTip("")
            return
        allowed = item.writable
        self.open_btn.setEnabled(True)
        self.open_btn.setToolTip("在资源管理器里定位目标程序")
        for btn in (self.enable_btn, self.disable_btn, self.delete_btn):
            btn.setEnabled(allowed)
            btn.setToolTip("" if allowed else
                           "「所有用户」位置需要以管理员身份运行本程序")
        self.enable_btn.setEnabled(allowed and not item.enabled)
        self.disable_btn.setEnabled(allowed and item.enabled)

    # ---------- 操作 ----------
    def _set_enabled(self, enabled: bool) -> None:
        item = self.selected_item()
        if item is None:
            return
        try:
            set_enabled(item, enabled)
        except OSError as err:
            QMessageBox.warning(self, "启动项",
                                f"修改失败：{err}\n\n"
                                "「所有用户」位置需要以管理员身份运行本程序。")
            return
        self.refresh(select=item)

    def _row_of(self, item: StartupItem) -> int:
        for i, other in enumerate(self._items):
            if (other.name == item.name and other.source == item.source
                    and other.scope == item.scope and other.path == item.path):
                return i
        return -1

    def _on_delete(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        where = ("移除注册表值" if item.source == SOURCE_REG
                 else "把快捷方式删到回收站")
        answer = QMessageBox.question(
            self, "删除启动项",
            f"确定要删除「{item.name}」吗？\n\n位置：{item.location}\n"
            f"命令：{item.command}\n\n将{where}，之后它不会再随开机启动。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            delete_item(item)
        except OSError as err:
            QMessageBox.warning(self, "启动项",
                                f"删除失败：{err}\n\n"
                                "「所有用户」位置需要以管理员身份运行本程序。")
            return
        self.refresh()

    def _on_open(self) -> None:
        """在资源管理器里定位到目标程序（找不到就退而求其次打开它所在的目录）。"""
        item = self.selected_item()
        if item is None:
            return
        candidates: list[str] = []
        target = item.target_path()
        if target:
            # 命令里可能是 %windir% 这类环境变量写法，先展开再判断
            candidates.append(os.path.expandvars(target))
        if item.source == SOURCE_FOLDER and item.path:
            candidates.append(item.path)
        for candidate in candidates:
            if os.path.isfile(candidate):
                _reveal_in_explorer(candidate)
                return
            parent = os.path.dirname(candidate)
            if parent and os.path.isdir(parent):
                _reveal_in_explorer(parent)
                return
        QMessageBox.information(
            self, "打开位置",
            f"找不到目标文件：\n{item.command}\n\n位置：{item.location}")

    def _on_add(self) -> None:
        dialog = AddStartupDialog(self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, scope = dialog.name(), dialog.scope()
        command = dialog.command()
        if name_exists(scope, name):
            answer = QMessageBox.question(
                self, "添加启动项",
                f"{_SCOPE_LABEL[scope]}里已有一个叫「{name}」的启动项，要覆盖它吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        try:
            add_registry_item(name, command, scope)
        except OSError as err:
            QMessageBox.warning(self, "启动项",
                                f"添加失败：{err}\n\n"
                                "写入「所有用户」需要以管理员身份运行本程序。")
            return
        self.refresh()


def _reveal_in_explorer(path: str) -> None:
    """在资源管理器里选中某个文件/打开某个目录。"""
    import subprocess
    try:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)          # noqa: S606（仅 Windows）
    except OSError:
        pass


# 登记到「小工具」页（key 一旦发布不要再改）
register(MiniApp(
    key="startup_items",
    name="启动项",
    icon="⚡",
    desc="管理开机自启：查看 / 添加 / 启用 / 禁用 / 删除（注册表 Run + 启动文件夹）",
    factory=StartupManagerWidget,
))
