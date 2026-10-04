"""截图步骤的执行：抓图 + 保存，以及「需要主线程 UI」的桥接。

截图步骤在 FlowRunner 的后台线程里执行，但有两个交互环节只能在主线程做：
- 「自己框选」：隐藏主窗口后启动屏幕遮罩让用户拖拽框选（遮罩是 QWidget，
  QApplication 事件循环只在主线程跑）；
- 「自选保存」：弹系统的「另存为」对话框。

这里提供 ui_call(fn)：后台线程把函数交给主线程事件循环执行并阻塞等待结果，
主线程里用嵌套 QEventLoop 处理遮罩交互（与 QDialog.exec 同理），不会死锁。

⚠️ **框选和抓图必须待在"主窗口仍然隐藏"的同一段里**：遮罩给用户看的是打开遮罩
那一刻的快照（那时主窗口已藏起来），一旦先把窗口恢复出来再抓图，就会把主窗口
截进去、而且是竞态。所以对外只提供 `select_region_and_grab()` 这一个入口
（2026-10-04 用户反馈「截的图有时候不对」后改成这样，见该函数说明）。

目录规则：默认保存时写入 <程序目录>/templates/jietu/（不存在自动创建），
与 templates/（找图模板）同根，都随程序目录走（打包版 = exe 同级）。
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal

from .config import TEMPLATE_DIR, parse_region_str

# 截图步骤「默认保存」的保存目录：<程序目录>/templates/jietu/
JIETU_DIR = os.path.join(TEMPLATE_DIR, "jietu")

# 框选确认后、真正抓图前的短暂等待（秒）。
# 遮罩虽然 close() 了，但要给 Qt 一点时间把它从屏幕上真的撤掉、把主窗口的
# 隐藏状态刷下去，否则会把遮罩/窗口残影截进图里。独立成模块常量而不是内联字面量，
# 方便测试直接置 0（测试里绝不能去 patch 全局 time.sleep，见 tests/_env 的约定）。
SELECT_SETTLE_SEC = 0.12


# ---------------------------------------------------------------------------
# 后台线程 -> 主线程桥接
# ---------------------------------------------------------------------------

class _UiCall:
    """一次 `ui_call` 的请求上下文：**自带独立的等待事件与结果槽**。

    并行的异步流程会同时用到这个桥接（截图 / 取色 / 通知 / 关机倒计时 / 找图红框…），
    所以 Event 与结果**绝不能是桥接上的单槽**：共用会让 B 在 A 醒来之后 `clear()`
    掉事件（把 A 永久掐醒不了、双方一起耗满 300s 超时），两条请求还会读到同一个
    `_result`——A 拿到的是 B 的截图区域（2026-10-02 review）。
    """

    __slots__ = ("fn", "event", "result")

    def __init__(self, fn):
        self.fn = fn
        self.event = threading.Event()
        self.result = None

    def done(self, result) -> None:
        self.result = result
        self.event.set()


class _UiBridge(QObject):
    """把函数调度到主线程执行并阻塞等待结果（供后台线程调用）。

    桥接对象创建后 moveToThread 到主线程；后台线程 emit _request 时按
    auto 连接规则自动走 QueuedConnection -> 主线程事件循环执行 fn，
    fn 的结果经 _reply 信号回填**到那次调用自己的** `_UiCall` 并唤醒它。
    创建时可能从后台线程首次调用，因此创建后立即 moveToThread 校正归属。
    """

    _request = Signal(object)       # _UiCall
    _reply = Signal(object, object)  # (_UiCall, 结果)

    def __init__(self):
        super().__init__()
        self._request.connect(self._run, Qt.QueuedConnection)
        self._reply.connect(self._receive, Qt.QueuedConnection)

    def call(self, fn) -> object:
        call = _UiCall(fn)
        self._request.emit(call)
        if not call.event.wait(timeout=300):   # 超时兜底：交互挂起时不永久卡死步骤
            return None
        return call.result

    def _run(self, call: _UiCall):
        try:
            result = call.fn()
        except Exception as e:      # 把异常带回调用线程，避免主线程静默吞掉
            result = ("__ui_error__", type(e).__name__, str(e))
        self._reply.emit(call, result)

    def _receive(self, call: _UiCall, result):
        call.done(result)


_bridge: _UiBridge | None = None
_bridge_lock = threading.Lock()


def _get_bridge() -> _UiBridge:
    """惰性创建桥接对象，并确保它归属于主线程（QApplication 所在线程）。"""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                raise RuntimeError("截图步骤需要 Qt 应用实例")
            b = _UiBridge()
            if threading.current_thread() is not app.thread():
                b.moveToThread(app.thread())
            _bridge = b
        return _bridge


def ui_call(fn):
    """在后台线程调用：把 fn 交给主线程执行并返回其返回值。

    主线程直接调用时原样执行（测试环境也在主线程，走这里避免桥接依赖）。
    """
    if threading.current_thread() is threading.main_thread():
        return fn()
    result = _get_bridge().call(fn)
    if isinstance(result, tuple) and len(result) == 3 and result[0] == "__ui_error__":
        raise RuntimeError(f"{result[1]}: {result[2]}")
    return result


# ---------------------------------------------------------------------------
# 抓图与保存（任意线程可调用）
# ---------------------------------------------------------------------------

def grab_image(mode: str, region: str = "") -> np.ndarray:
    """按模式抓取屏幕，返回 BGR ndarray（可在任意线程调用）。

    mode:
      - fullscreen：整个虚拟桌面（多显示器全部，含负坐标区域）
      - region    ：指定区域 "x,y,w,h"（虚拟桌面物理像素）
    区域无效时退回全屏，与找图/OCR 的约定一致。
    """
    import mss
    with mss.mss() as sct:
        r = parse_region_str(region)
        if mode == "region" and r is not None:
            x, y, w, h = r
            monitor = {"left": x, "top": y,
                       "width": max(w, 1), "height": max(h, 1)}
        else:
            monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        img = np.asarray(shot)[:, :, :3]
        return np.ascontiguousarray(img)


def save_jietu(img: np.ndarray) -> str:
    """保存截图到 <程序目录>/templates/jietu/（目录不存在自动创建）。

    文件名：截图_yyyyMMdd_HHmmss.png。返回绝对路径；**写盘失败抛 OSError**
    （不再像以前那样默默返回一个并不存在的路径，让流程误以为保存成功）。
    """
    from . import imgio
    os.makedirs(JIETU_DIR, exist_ok=True)
    name = f"截图_{time.strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join(JIETU_DIR, name)
    if not imgio.imwrite(path, img):
        raise OSError(f"写入失败：{path}（{imgio.write_failure_reason(path)}）")
    return path


# ---------------------------------------------------------------------------
# 主线程交互（只能经 ui_call 调用）
# ---------------------------------------------------------------------------

def _main_window():
    """找到支持截屏隐藏/恢复的主窗口（MainWindow）。"""
    from PySide6.QtWidgets import QApplication
    for w in QApplication.topLevelWidgets():
        if hasattr(w, "_hide_for_capture") and hasattr(w, "_restore_after_capture"):
            return w
    return None


def select_region_and_grab() -> tuple[tuple[int, int, int, int] | None,
                                      np.ndarray | None]:
    """主线程执行：隐藏主窗口 -> 屏幕遮罩框选 -> **趁窗口还藏着**抓图 -> 恢复窗口。

    返回 `((x, y, w, h) | None, 图像 | None)`；取消框选时两者都是 None。

    为什么「框选」和「抓图」必须是**同一次隐藏窗口**里的连续动作
    （2026-10-04 用户反馈「截的图有时候不对」）：
    遮罩显示的是**打开遮罩那一刻的屏幕快照**，而那时主窗口已经被藏起来了；
    可 `select_region` 返回之前就会 `show() + raise_() + activateWindow()` 把主窗口
    恢复。调用方拿着 rect 再去 `grab_image`，截到的就是**刚弹回来的主窗口**——
    和用户框选时看到的画面根本不是一个东西。更糟的是「窗口有没有来得及画出来」
    是竞态，所以表现成**有时候对、有时候不对**（选中的区域越靠近主窗口越容易中）。

    抓图放在 `finally` 里 `_restore_after_capture()` **之前**，从根上消掉这个竞态。
    """
    win = _main_window()
    if win is not None:
        win._hide_for_capture()
    from PySide6.QtCore import QEventLoop
    from .capture_overlay import run_screen_capture

    loop = QEventLoop()
    result = {"rect": None}

    def on_region(rect):
        result["rect"] = rect
        loop.quit()

    def on_cancelled():
        loop.quit()

    image = None
    try:
        run_screen_capture(on_region=on_region, on_cancelled=on_cancelled)
        loop.exec()
        rect = result["rect"]
        if rect:
            _settle_after_overlay()
            x, y, w, h = (int(v) for v in rect)
            image = grab_image("region", f"{x},{y},{w},{h}")
    finally:
        if win is not None:
            win._restore_after_capture()
    return result["rect"], image


def _settle_after_overlay() -> None:
    """遮罩关闭后、抓图前的小停顿：让遮罩真的从屏幕上撤掉。

    遮罩走的是 `close()`（同步 hide），正常情况下一关就没影了；这里再做一次
    `processEvents()` + 极短等待纯粹是兜底——万一遇上「隐藏事件还在队列里、
    屏幕还没重绘」的时机，截出来就会多一层遮罩底色（用户看到的就是"截图发暗/不对"）。
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    if SELECT_SETTLE_SEC > 0:
        time.sleep(SELECT_SETTLE_SEC)


def ask_save_path(default_name: str) -> str | None:
    """主线程执行：弹「另存为」对话框，返回用户选择的路径（取消返回 None）。"""
    from PySide6.QtWidgets import QFileDialog
    start = JIETU_DIR if os.path.isdir(JIETU_DIR) else TEMPLATE_DIR
    path, _ = QFileDialog.getSaveFileName(
        None, "保存截图", os.path.join(start, default_name),
        "PNG 图片 (*.png);;所有文件 (*)")
    if not path:
        return None
    if not path.lower().endswith(".png"):
        path += ".png"
    return path
