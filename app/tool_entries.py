# -*- coding: utf-8 -*-
"""「工具条目」：把可放进九宫格的两类东西统一成一种描述。

来源有两处：
1. **内置小程序**（`app/mini_apps` 注册表，如计算器、剪贴板）——点击在独立窗口里运行；
2. **用户自定义程序**（`AppConfig.custom_tools`，用户在小工具页加进来的电脑里的程序）
   ——点击用系统默认方式打开它（等价于在资源管理器里双击）。

中键菜单的九宫格、管理页的九宫格编辑区、小工具页都要同时认这两类，所以统一到一个
轻量描述 `ToolEntry` 上；字段名与 `MiniApp` 对齐（key/name/icon/desc），
原来只认 MiniApp 的代码换成条目后基本不用改。

自定义程序的 key 为什么不用路径本身
------------------------------------
`"custom:" + sha1(规范化路径)[:12]`：
- key 要存进 `config.middle_menu_tools`，而 `clean_tool_keys` 会把 key **截断到 40 字符**
  ——路径往往比这长，直接存路径会被截断成另一个错的 key；
- 哈希天然稳定：同一路径永远同一个 key，用户给卡片改名也不影响；
- 规范化用 `normpath + casefold`：Windows 路径大小写不敏感，
  `C:\\X\\a.exe` 与 `c:/x/A.exe` 必须算同一条。

认不出的 key（自定义条目被删掉、手改配置写错）解析时直接跳过，与内置小程序同一策略：
坏数据不该让菜单弹不出来。
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Iterable

from PySide6.QtCore import QFileInfo, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QFileIconProvider, QMessageBox

from .mini_apps import MiniApp, all_apps, find_app
from .ui.widgets import emoji_icon

log = logging.getLogger(__name__)

CUSTOM_PREFIX = "custom:"
DEFAULT_PROGRAM_ICON = "📄"


@dataclass(frozen=True)
class ToolEntry:
    """一个可放进九宫格的工具条目（内置小程序 或 用户自定义程序）。"""

    key: str                        # 稳定标识（见模块说明）
    name: str                       # 显示名
    icon: str                       # emoji（自定义条目用 📄 兜底，卡片上会换成真图标）
    desc: str                       # 说明/路径（悬停提示）
    path: str = ""                  # 非空 = 自定义程序
    app: MiniApp | None = None      # 非空 = 内置小程序

    @property
    def is_custom(self) -> bool:
        return bool(self.path)


# ---------- 自定义程序的 key ----------
def custom_key(path: str) -> str:
    """自定义程序的稳定 key（见模块说明：用哈希，不用路径本身）。"""
    norm = os.path.normpath(str(path or "").strip()).casefold()
    digest = hashlib.sha1(norm.encode("utf-8", "surrogatepass")).hexdigest()
    return CUSTOM_PREFIX + digest[:12]


def is_custom_key(key: str) -> bool:
    return str(key or "").startswith(CUSTOM_PREFIX)


# ---------- 自定义程序的显示与打开 ----------
def display_name(path: str) -> str:
    """从路径推默认显示名（`Everything.lnk` -> `Everything`）。"""
    try:
        info = QFileInfo(path)
        name = info.completeBaseName() or info.fileName()
    except Exception:               # noqa: BLE001（路径怪东西不该让加卡片失败）
        name = ""
    return (name or os.path.basename(path or "") or "程序").strip()


def file_icon(path: str, size: int = 24) -> QIcon:
    """取该文件在资源管理器里显示的图标（程序自己的图标最有辨识度）。

    取不到（路径不存在/非 Windows/离屏平台）就退回一个通用 emoji，卡片照样能用。
    """
    try:
        icon = QFileIconProvider().icon(QFileInfo(path))
        if icon is not None and not icon.isNull():
            return icon
    except Exception:               # noqa: BLE001
        log.debug("取文件图标失败: %s", path, exc_info=True)
    return emoji_icon(DEFAULT_PROGRAM_ICON, size)


def launch_program(path: str, parent=None) -> bool:
    """用系统默认方式打开一个程序/快捷方式；失败时弹提示并返回 False。

    - 路径不存在（已被移动/卸载）**不硬打开**，直接告诉用户；
    - `QDesktopServices.openUrl` 返回 False（系统拒绝）也弹一次提示。
    """
    path = str(path or "")
    if not path or not os.path.exists(path):
        QMessageBox.warning(
            parent, "无法打开",
            f"找不到这个程序：\n{path}\n\n它可能已被移动或卸载。")
        return False
    try:
        if QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            return True
    except Exception:               # noqa: BLE001
        log.exception("打开程序失败: %s", path)
    QMessageBox.warning(parent, "无法打开", f"Windows 拒绝打开：\n{path}")
    return False


# ---------- 条目 ----------
def entry_from_app(app: MiniApp) -> ToolEntry:
    return ToolEntry(key=app.key, name=app.name, icon=app.icon, desc=app.desc,
                     app=app)


def entry_from_custom(item: dict) -> ToolEntry | None:
    """把 `config.custom_tools` 里的一条转成条目；脏数据返回 None。"""
    if not isinstance(item, dict):
        return None
    path = str(item.get("path") or "").strip()
    if not path:
        return None
    name = str(item.get("name") or "").strip() or display_name(path)
    return ToolEntry(key=custom_key(path), name=name, icon=DEFAULT_PROGRAM_ICON,
                     desc=path, path=path)


def all_entries(custom_tools: Iterable | None = None) -> list[ToolEntry]:
    """全部可用条目：**内置小程序在前、自定义程序在后**（九宫格里的顺序）。"""
    entries = [entry_from_app(app) for app in all_apps()]
    for item in (custom_tools or []):
        entry = entry_from_custom(item)
        if entry is not None:
            entries.append(entry)
    return entries


def find_entry(key: str, custom_tools: Iterable | None = None) -> ToolEntry | None:
    """按 key 找条目（内置先找注册表，避免每次遍历自定义列表）。"""
    key = str(key or "")
    if not key:
        return None
    if not is_custom_key(key):
        app = find_app(key)
        return entry_from_app(app) if app is not None else None
    for entry in all_entries(custom_tools):
        if entry.key == key:
            return entry
    return None


def resolve_entries(keys: Iterable | None, custom_tools: Iterable | None = None
                    ) -> list[ToolEntry]:
    """把配置里的 key 列表解析成条目列表；认不出的 key 跳过（顺序保持）。"""
    out: list[ToolEntry] = []
    seen: set[str] = set()
    for key in (keys or []):
        key = str(key or "").strip()
        if not key or key in seen:
            continue
        entry = find_entry(key, custom_tools)
        if entry is None:
            continue
        seen.add(key)
        out.append(entry)
    return out


def open_entry(entry: ToolEntry, anchor=None) -> bool:
    """打开一个条目：内置小程序开独立窗口，自定义程序交给系统打开。"""
    if entry.path:
        return launch_program(entry.path, parent=anchor)
    if entry.app is not None:
        from .ui.mini_window import open_mini_app   # 延迟导入，避免模块环
        open_mini_app(entry.app, anchor=anchor)
        return True
    return False
