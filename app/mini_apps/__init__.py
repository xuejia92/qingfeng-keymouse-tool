# -*- coding: utf-8 -*-
"""小工具（独立小程序）注册表。

「🧰 小工具」页以**九宫格**展示这里登记的全部小程序，点击任一条目会在一个
**与主程序相互独立的窗口**里运行它（窗口宿主见 `app/ui/mini_window.py`）。
本模块只管「有哪些小程序」，不负责界面怎么画。

新增一个小程序：只改两处
------------------------
1. 在 `app/mini_apps/` 下新建模块（照抄 `calculator.py`）：实现一个 `QWidget`
   子类，在模块末尾调用 `register(MiniApp(...))` 登记；
2. 在本文件底部的「内置小程序」导入列表里补一行 `from . import <新模块>`。

约定
----
- `key` 是**稳定标识**（窗口去重、日后记忆用户偏好都靠它），发布后不要再改；
  界面文案（`name` / `desc`）可以随便改。
- `factory` 每次调用都应返回**全新**的控件实例：当前策略是「同一 key 只保留一个
  窗口」（见 mini_window），但工厂保持「每次新实例」的语义，将来放开多开不必改这里。
- 小程序若持有**进程外资源**（临时服务、后台线程、子进程…），在控件上定义一个
  `shutdown()`：宿主窗口关闭时会调它一次（见 `ui/mini_window._release_content`）。
  不定义也可以——没有资源的控件（如计算器）不需要。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class MiniApp:
    """一个小程序的静态描述：元数据 + 创建控件用的工厂。"""

    key: str                        # 稳定标识（去重/持久化用），ASCII 短串
    name: str                       # 卡片与窗口标题上显示的名称
    icon: str                       # emoji（用 Segoe UI Emoji 画成图标）
    desc: str                       # 一句话说明，卡片悬停提示
    factory: Callable[[], QWidget]  # 创建小程序主控件（每次调用都是新实例）

    def create(self) -> QWidget:
        """创建小程序的主控件。"""
        return self.factory()


_APPS: list[MiniApp] = []


def register(app: MiniApp) -> MiniApp:
    """登记一个小程序，返回它本身便于链式书写。

    同 `key` 重复登记时**就地替换**（开发期反复调整一个小程序时不必先注销），
    顺序保持原位置不变。
    """
    for i, old in enumerate(_APPS):
        if old.key == app.key:
            _APPS[i] = app
            return app
    _APPS.append(app)
    return app


def all_apps() -> list[MiniApp]:
    """当前登记的全部小程序（顺序 = 九宫格里的排布顺序）。返回副本，防止外部改内部状态。"""
    return list(_APPS)


def find_app(key: str) -> MiniApp | None:
    """按 key 查一个小程序；没有则返回 None。"""
    for app in _APPS:
        if app.key == key:
            return app
    return None


# ---------------------------------------------------------------------------
# 内置小程序：新增一个就在这里加一行 import
# （导入即登记——各模块在末尾自行调用 register()）
# ---------------------------------------------------------------------------
from . import calculator         # noqa: E402,F401  （必须在 register 定义之后导入）
from . import clipboard_history  # noqa: E402,F401
from . import lan_transfer       # noqa: E402,F401
from . import startup_items      # noqa: E402,F401
from . import task_board         # noqa: E402,F401
