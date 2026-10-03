# -*- coding: utf-8 -*-
"""小程序独立窗口宿主（「🧰 小工具」页的落地实现）。

为什么要**独立窗口**而不是内嵌标签页：小程序是"能单独用"的小程序——用户
希望算完数就把它挪到屏幕角落、单独最小化、被别的窗口盖住，而不是永远被主
窗口框住；关掉它也不该影响主程序的任何运行状态。

与主程序解耦的三个要点
----------------------
1. **不设 Qt 父窗口**（`parent=None`）：它是真正的顶层窗口——独立任务栏项、
   能单独最小化/移动/被遮挡；`main.py` 里 `setQuitOnLastWindowClosed(False)`，
   所以关掉它（哪怕是最后一个窗口）也不会退出程序。
2. **关掉即销毁**：`WA_DeleteOnClose`，并在 `closeEvent` 里把自己从登记表摘掉，
   下次点同一张卡片是全新实例（不留幽灵引用）。
3. **主题/字号自动跟随**：主题引擎的 Show 过滤器会给任何顶层窗口套上全局
   基线样式，切主题时也会重刷可见顶层窗口（见 theme._ShowReplayFilter），
   这里一行都不用写。
4. **关窗前给小程序的收尾钩子**：主控件若定义了 `shutdown()`，关窗时会调一次
   （小程序可能持有进程外资源，比如局域网文件传输开的 HTTP 服务）。

窗口生命周期由模块级登记表 `_OPEN` 管理（key -> 窗口）：
同一 key 只保留**一个**窗口，重复点击只是把它置前——避免用户连点几下就攒出
一堆一模一样的计算器。要改成允许多开，只需去掉这里的复用分支。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ..mini_apps import MiniApp

CASCADE_STEP = 28       # 同时开多个小程序时的层叠偏移（像素）
_WINDOW_MARGIN = 10     # 内容与窗口边缘的留白


def _release_content(content) -> None:
    """关窗前给小程序一次收尾机会（约定：主控件有 `shutdown()` 就调它）。

    为什么需要：小程序可能持有进程外资源（局域网文件传输会开一个 HTTP 服务、
    将来可能有串口/子进程）。窗口关掉后控件会被销毁，但**服务线程不会自己停**，
    所以约定一个可选钩子，由宿主在 closeEvent 里统一调用。
    钩子里出任何问题都不该挡住关窗，所以这里吞掉异常只记日志。
    """
    shutdown = getattr(content, "shutdown", None)
    if not callable(shutdown):
        return
    try:
        shutdown()
    except Exception:               # noqa: BLE001（收尾失败不能影响关窗）
        logging.getLogger(__name__).exception("小程序收尾失败")


class MiniAppWindow(QWidget):
    """承载单个小程序主控件的独立顶层窗口。"""

    def __init__(self, app: MiniApp):
        super().__init__(None)          # 无父窗口 = 与主程序相互独立
        self.app = app
        self.setObjectName("miniAppWindow")
        self.setWindowTitle(app.name)
        self.setWindowFlag(Qt.Window, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(_WINDOW_MARGIN, _WINDOW_MARGIN,
                               _WINDOW_MARGIN, _WINDOW_MARGIN)
        lay.setSpacing(0)
        # 小程序主控件每次都是新实例（MiniApp.create 的语义），互不干扰
        self.content = app.create()
        lay.addWidget(self.content)

        hint = self.content.sizeHint()
        # ⚠️ 不能只看 sizeHint：QTableWidget 这类控件的 sizeHint 很小，按它开窗口会
        # 开成小小一条，把内容挤变形（2026-10-03 实测）。显式设了 minimumSize 的小程序
        # 必须被尊重 —— `minimumSizeHint()` 不包含显式 minimumSize，两者都要取。
        floor = self.content.minimumSize().expandedTo(
            self.content.minimumSizeHint())
        preferred = hint.expandedTo(floor)
        self.resize(preferred.width() + 2 * _WINDOW_MARGIN,
                    preferred.height() + 2 * _WINDOW_MARGIN)
        self.setMinimumSize(floor)

    @property
    def app_key(self) -> str:
        return self.app.key

    def closeEvent(self, event) -> None:        # noqa: N802（Qt 命名）
        # 先从登记表摘掉再关：WA_DeleteOnClose 会把 C++ 对象删掉，
        # 登记表里留下幽灵条目的话，下次点击会拿到已销毁的对象。
        _OPEN.pop(self.app.key, None)
        _release_content(self.content)
        super().closeEvent(event)


# key -> 当前活着的窗口
_OPEN: dict[str, MiniAppWindow] = {}


def open_mini_app(app: MiniApp, anchor: QWidget | None = None) -> MiniAppWindow:
    """打开（或置前）一个小程序窗口，返回它。

    `anchor` 只用来挑显示器（主窗口在哪块屏，弹窗就落在哪块屏），**不作为
    Qt 父对象**——否则窗口之间就不再独立了。
    """
    existing = _OPEN.get(app.key)
    if existing is not None:
        try:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return existing
        except RuntimeError:        # 已被销毁的幽灵引用：清掉后重新创建
            _OPEN.pop(app.key, None)

    win = MiniAppWindow(app)
    _OPEN[app.key] = win
    win.show()
    _place(win, anchor)
    win.raise_()
    win.activateWindow()
    return win


def open_apps() -> list[MiniAppWindow]:
    """当前开着的小程序窗口（按打开顺序）。"""
    return list(_OPEN.values())


def close_all() -> None:
    """关闭全部小程序窗口（程序退出时收尾，幂等）。"""
    for win in list(_OPEN.values()):
        try:
            win.close()
        except RuntimeError:
            pass
    _OPEN.clear()


def _place(win: QWidget, anchor: QWidget | None) -> None:
    """把新窗口放到 anchor 所在屏幕的可用区域中央；多开时轻微层叠错开。"""
    screen = None
    if anchor is not None:
        try:
            screen = anchor.screen()
        except RuntimeError:
            screen = None
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return
    avail = screen.availableGeometry()
    geo = win.frameGeometry()
    # 屏幕装不下就缩到屏幕内（超大窗口/小屏），再居中并限位
    geo.setWidth(min(geo.width(), avail.width()))
    geo.setHeight(min(geo.height(), avail.height()))
    geo.moveCenter(avail.center())
    step = CASCADE_STEP * max(0, len(_OPEN) - 1)     # 第一个窗口就是正中，不开偏
    geo.translate(step, step)
    geo.moveLeft(max(avail.left(),
                     min(geo.left(), avail.right() - geo.width() + 1)))
    geo.moveTop(max(avail.top(),
                    min(geo.top(), avail.bottom() - geo.height() + 1)))
    win.move(geo.topLeft())
