"""截图步骤的执行：抓图 + 保存，以及「需要主线程 UI」的桥接。

截图步骤在 FlowRunner 的后台线程里执行，但有两个交互环节只能在主线程做：
- 「自己框选」：隐藏主窗口后启动屏幕遮罩让用户拖拽框选（遮罩是 QWidget，
  QApplication 事件循环只在主线程跑）；
- 「自选保存」：弹系统的「另存为」对话框。

这里提供 ui_call(fn)：后台线程把函数交给主线程事件循环执行并阻塞等待结果，
主线程里用嵌套 QEventLoop 处理遮罩交互（与 QDialog.exec 同理），不会死锁。

目录规则：默认保存时写入 <程序目录>/templates/jietu/（不存在自动创建），
与 templates/（找图模板）同根，都随程序目录走（打包版 = exe 同级）。
"""
from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import QObject, Qt, Signal

from .config import TEMPLATE_DIR, parse_region_str

# 截图步骤「默认保存」的保存目录：<程序目录>/templates/jietu/
JIETU_DIR = os.path.join(TEMPLATE_DIR, "jietu")


# ---------------------------------------------------------------------------
# 后台线程 -> 主线程桥接
# ---------------------------------------------------------------------------

class _UiBridge(QObject):
    """把函数调度到主线程执行并阻塞等待结果（供后台线程调用）。

    桥接对象创建后 moveToThread 到主线程；后台线程 emit _request 时按
    auto 连接规则自动走 QueuedConnection -> 主线程事件循环执行 fn，
    fn 的结果经 _reply 信号（同线程 direct）回填并唤醒等待方。
    创建时可能从后台线程首次调用，因此创建后立即 moveToThread 校正归属。
    """

    _request = Signal(object)   # fn
    _reply = Signal(object)     # 结果

    def __init__(self):
        super().__init__()
        self._event = threading.Event()
        self._result = None
        self._request.connect(self._run, Qt.QueuedConnection)
        self._reply.connect(self._receive, Qt.QueuedConnection)

    def call(self, fn) -> object:
        self._result = None
        self._event.clear()
        self._request.emit(fn)
        if not self._event.wait(timeout=300):   # 超时兜底：交互挂起时不永久卡死步骤
            return None
        return self._result

    def _run(self, fn):
        try:
            result = fn()
        except Exception as e:      # 把异常带回调用线程，避免主线程静默吞掉
            result = ("__ui_error__", type(e).__name__, str(e))
        self._reply.emit(result)

    def _receive(self, result):
        self._result = result
        self._event.set()


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

    文件名：截图_yyyyMMdd_HHmmss.png。返回绝对路径。
    """
    os.makedirs(JIETU_DIR, exist_ok=True)
    name = f"截图_{time.strftime('%Y%m%d_%H%M%S')}.png"
    path = os.path.join(JIETU_DIR, name)
    cv2.imwrite(path, img)
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


def select_region() -> tuple[int, int, int, int] | None:
    """主线程执行：隐藏主窗口 -> 屏幕遮罩框选 -> 恢复，返回 (x,y,w,h) 或 None。

    用嵌套 QEventLoop 处理遮罩交互（与 QDialog.exec 同理），不会阻塞事件循环。
    """
    from PySide6.QtCore import QEventLoop
    from .capture_overlay import run_screen_capture

    win = _main_window()
    if win is not None:
        win._hide_for_capture()
    loop = QEventLoop()
    result = {"rect": None}

    def on_region(rect):
        result["rect"] = rect
        loop.quit()

    def on_cancelled():
        loop.quit()

    try:
        run_screen_capture(on_region=on_region, on_cancelled=on_cancelled)
        loop.exec()
    finally:
        if win is not None:
            win._restore_after_capture()
    return result["rect"]


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
