"""全局中键监听：基于 Win32 低层鼠标钩子（WH_MOUSE_LL）。

作用范围与触发条件
------------------
程序运行期间在**系统范围**内监听鼠标；当用户**松开鼠标中键**时发一次
middleClicked 信号（携带屏幕物理坐标），主窗口据此在光标处弹出中键菜单。

为什么是「抬起」而不是「按下」
------------------------------
若在按下时就弹菜单，用户松手这一下会落到刚弹出的菜单上，极易误选到第一个
条目。改为抬起触发后「按下 → 抬起 → 菜单出现」，松手不会点中任何条目。

为什么不用 pynput
-----------------
pynput 的 mouse.Listener 只有两种选择：`suppress=True` 会把**所有**鼠标事件
都吞掉（鼠标直接失灵），否则完全不拦。而中键菜单需要「只拦中键、其余照常」。
这里直接用 Win32 低层钩子，自己决定是否吞掉中键，行为可控。

合成点击过滤（防反馈环）
------------------------
钩子数据里的 LLMHF_INJECTED 标志能区分「真实硬件点击」与「SendInput 合成的
点击」。本工具流程里的中键步骤（input_actors.click(button="middle")）属于合成
点击，会被直接放行忽略——否则「流程点中键 → 弹菜单 → 再触发流程」会形成反馈环。

拖拽期间必须让位（2026-09-15）
------------------------------
钩子回调是 Python 且在**系统输入通路里同步执行**，一旦抢不到 GIL 就会卡住**所有**
鼠标输入。而本程序自己的拖拽走 OLE `DoDragDrop`：主线程在那段原生模态循环里一直
握着 GIL、不执行 Python 字节码，GIL 没有交接点 → 钩子线程饿死 → 鼠标输入被卡住 →
拖动冻住（实测：装钩子时一次真实拖动只能推进 2~4 个事件、单次卡 1000ms 以上；
同一份代码不装钩子 62 个事件、间隔中位 3ms）。

因此拖拽前调 `pause()` 卸掉钩子、拖完 `unpause()` 装回来（见
`MouseMenuWatcher.suspend`）。拖动过程中左键是按着的，中键菜单本来也不可能用到。
注意：`sys.setswitchinterval(0.001)` 那种降低 GIL 切换间隔的办法**救不了**这种
情况——它只是字节码边界上的检查点，主线程不在跑字节码时形同不存在。
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

# ---- Win32 常量 ----
WH_MOUSE_LL = 14
HC_ACTION = 0
PM_NOREMOVE = 0x0000
WM_QUIT = 0x0012
WM_MOUSEMOVE = 0x0200
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002

_INJECTED_MASK = LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
_MIDDLE_MESSAGES = (WM_MBUTTONDOWN, WM_MBUTTONUP)


class MSLLHOOKSTRUCT(ctypes.Structure):
    """低层鼠标钩子回调中的 MSLLHOOKSTRUCT。"""

    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),    # ULONG_PTR
    ]


def is_injected(flags: int) -> bool:
    """事件是否由软件注入（SendInput 等）而非真实硬件产生。"""
    return bool(int(flags) & _INJECTED_MASK)


def should_trigger(msg: int, flags: int) -> bool:
    """是否应弹出中键菜单：真实的「中键抬起」事件。"""
    return msg == WM_MBUTTONUP and not is_injected(flags)


def should_suppress(msg: int, flags: int) -> bool:
    """是否为需要拦截（不传给目标窗口）的事件：真实的中键按下/抬起。

    只拦中键，不碰其它鼠标事件，避免影响用户正常操作。
    """
    return msg in _MIDDLE_MESSAGES and not is_injected(flags)


# GIL 强制切换间隔：默认 5ms 太长，见 lower_gil_switch_interval() 的说明。
_GIL_SWITCH_INTERVAL = 0.001


def lower_gil_switch_interval() -> None:
    """把 GIL 强制切换间隔降到 1ms（幂等，只在需要时下调）。

    为什么必须做：低层鼠标钩子的回调是 **Python**，而它由系统在装载线程里
    **同步**调用——Windows 会一直等到回调返回才继续处理鼠标输入。钩子线程要跑
    Python 就得抢到 GIL，而 CPython 的 GIL 默认每 5ms 才强制切换一次、交接又
    不公平：主线程一忙（拖动时密集跑原生消息过滤 + 拖放事件），钩子线程就得
    等满 5ms 才轮得上，于是**每一条鼠标事件都被拖慢**、输入队列持续积压。

    实测（本机，注入 ±2px 鼠标移动 + 自装 WH_MOUSE_LL 量
    `GetTickCount() - MSLLHOOKSTRUCT.time`）：

    | 场景 | 采样数 | 投递延迟中位 |
    |---|---|---|
    | 空载 | 868 | 0 ms |
    | 另有一个线程占着 GIL（默认 5ms 间隔） | **44** | **16 ms** |
    | 同上，但把间隔降到 1ms | **872** | **0 ms** |

    这就是「拖动时鼠标发涩、按下与松开都迟钝」的成因：拖动中 OLE 拖拽循环
    拿不到鼠标事件（实测用户机器上单条事件最长延迟 859ms），几秒才处理十几个
    事件；一松手主线程不再跑 Python，钩子线程恢复正常，于是「再点一下就不卡了」。

    代价：GIL 切换更频繁，纯 Python 计算会略有额外开销——本程序不是 CPU 密集
    型，可以忽略；不装钩子时（中键菜单关闭）根本不会调用这里。
    """
    import sys
    if sys.getswitchinterval() > _GIL_SWITCH_INTERVAL:
        sys.setswitchinterval(_GIL_SWITCH_INTERVAL)


# 句柄在 64 位下是 8 字节指针，而 ctypes.windll 默认按 c_int(4 字节) 解释返回值，
# 不显式声明会把 HHOOK/HMODULE 截断（项目里 CreateToolhelp32Snapshot 踩过同一坑）。
_BOUND = False


def bind_win32_functions() -> None:
    """显式声明用到的 Win32 函数签名（幂等，导入时先绑一次）。"""
    global _BOUND
    if _BOUND:
        return
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.SetWindowsHookExW.restype = ctypes.c_void_p      # HHOOK
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t        # LRESULT
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p      # HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        _BOUND = True
    except Exception:
        log.debug("声明 Win32 函数签名失败（继续按默认签名工作）", exc_info=True)


class MouseMenuWatcher(QObject):
    """全局中键监听器：在独立线程里跑低层鼠标钩子，命中时发 Qt 信号。

    用独立线程而不是 Qt 主线程：钩子回调由系统在装载线程的消息泵里同步调用，
    放在主线程会插进 Qt 的事件循环里，回调一旦稍慢就会拖住整个界面。独立线程
    自带一个朴素消息泵，回调只做「判类型 + 发信号」这一件极轻的事。
    """

    middleClicked = Signal(int, int)   # 中键抬起：屏幕物理坐标 (x, y)

    def __init__(self, parent=None):
        super().__init__(parent)
        bind_win32_functions()
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = None          # HHOOK
        self._proc = None          # ctypes 回调对象，必须保留引用，否则被 GC 回收会崩
        self._suppress = False
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._started_ok = False
        # _enabled = 用户希望它开着（开关状态）；_suspended = 临时让位（见 suspend）
        self._enabled = False
        self._suspended = False
        global _watcher
        _watcher = self          # 供拖拽路径临时 pause/unpause（同一进程只有一个）

    # ---------- 对外接口 ----------
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_suppress(self, suppress: bool) -> None:
        """设置是否拦截中键（不让它落到目标窗口）。可在运行中随时切换。"""
        with self._lock:
            self._suppress = bool(suppress)

    def suppress(self) -> bool:
        with self._lock:
            return self._suppress

    def start(self) -> bool:
        """启用监听（用户开关打开、或启动时调用）。"""
        self._enabled = True
        if self._suspended:
            return False
        return self._ensure_running()

    def stop(self) -> None:
        """关闭监听（用户开关关闭、或退出时调用）；同时取消任何临时让位。"""
        self._enabled = False
        self._suspended = False
        self._stop_thread()

    def suspend(self) -> bool:
        """**临时**卸载钩子，返回是否真的卸掉了（配对 restore）。

        为什么必须有这个开关：低层鼠标钩子的回调由系统在装载线程里**同步**调用，
        Windows 会一直等它返回才继续投递鼠标输入；而回调是 Python、必须抢到 GIL。
        本程序自己的拖拽（OLE 的 DoDragDrop）会让**主线程连握着 GIL 待在那个原生
        模态循环里**——那段时间主线程不执行 Python 字节码，GIL 就没有交接点，
        钩子线程永远拿不到 GIL，于是输入被卡住、拖动冻住（实测：装着钩子时一次
        真实拖动只能推进 2~4 个事件、单次卡 1000ms；不装钩子同一份代码 62 个事件、
        间隔中位 3ms）。1ms 的 GIL 切换间隔救不了这种情况（那也只是字节码边界的
        检查点）。

        所以拖拽期间主动把钩子卸掉，拖完再装回来：中键菜单在拖动过程中本来也用不到
        （那时左键按着），而输入通路立刻恢复顺畅。
        """
        if not self._enabled or self._suspended:
            return False
        self._suspended = True
        running = self.is_running()
        self._stop_thread()
        return running

    def restore(self) -> None:
        """与 suspend 配对：恢复监听（用户已把开关关掉时不再恢复）。"""
        if not self._suspended:
            return
        self._suspended = False
        if self._enabled:
            self._ensure_running()

    def _ensure_running(self) -> bool:
        """启动监听线程，返回钩子是否装载成功（失败不影响程序其它功能）。"""
        if self.is_running():
            return True
        # 装钩子前先把 GIL 切换间隔降下来，否则钩子线程会抢不到 GIL 而拖慢
        # 整个鼠标输入通路（详见 lower_gil_switch_interval 的说明）。
        lower_gil_switch_interval()
        self._ready.clear()
        self._started_ok = False
        self._thread = threading.Thread(target=self._run, name="MouseMenuHook",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        if not self._started_ok:
            log.warning("中键菜单监听未启动（低层鼠标钩子装载失败）")
        return bool(self._started_ok)

    def _stop_thread(self) -> None:
        """停止监听线程并卸载钩子。"""
        tid, self._thread_id = self._thread_id, 0
        if tid:
            try:
                self._user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                log.debug("结束中键钩子线程失败", exc_info=True)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # ---------- 钩子线程 ----------
    def _run(self) -> None:
        user32 = self._user32
        try:
            # 先 PeekMessage 建出本线程的消息队列，确保之后 PostThreadMessageW(WM_QUIT)
            # 一定能投递到（消息队列懒创建，不先建可能丢消息导致线程退不出来）。
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
            self._thread_id = int(self._kernel32.GetCurrentThreadId())

            hookproc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                          ctypes.c_size_t, ctypes.c_ssize_t)
            self._proc = hookproc(self._hook_proc)
            self._hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._proc, self._kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                log.warning("SetWindowsHookExW(WH_MOUSE_LL) 失败，GetLastError=%s",
                            self._kernel32.GetLastError())
                return
            self._started_ok = True
            self._ready.set()

            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):      # 0=WM_QUIT，-1=错误
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("中键菜单监听线程异常")
        finally:
            try:
                if self._hook:
                    user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                log.debug("卸载鼠标钩子失败", exc_info=True)
            self._hook = None
            self._proc = None
            self._started_ok = False
            self._ready.set()     # 兜底：装载失败时也要放行 start() 的等待

    def _hook_proc(self, n_code, w_param, l_param):
        """低层鼠标钩子回调：运行在钩子线程，必须极轻（只判断 + 发信号）。

        返回 1 表示吞掉该事件（不再传给目标窗口），返回 0/CallNextHookEx 表示放行。
        整个过程绝不能让异常逃逸：ctypes 回调抛异常时会返回 0，等于静默吞掉鼠标事件。
        """
        try:
            if self._handle_event(n_code, w_param, l_param):
                return 1
        except Exception:
            log.debug("中键钩子回调异常", exc_info=True)
        try:
            return self._user32.CallNextHookEx(None, n_code, w_param, l_param)
        except Exception:
            log.debug("CallNextHookEx 调用失败", exc_info=True)
            return 0

    def _handle_event(self, n_code, w_param, l_param) -> bool:
        """处理一次钩子事件；返回是否需要吞掉（仅真实中键、且开启了拦截）。"""
        if n_code != HC_ACTION or w_param not in _MIDDLE_MESSAGES:
            return False
        data = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        flags = int(data.flags)
        if is_injected(flags):
            return False        # 本工具自己注入的合成点击：放行，不弹菜单也不拦
        if should_trigger(w_param, flags):
            self.middleClicked.emit(int(data.pt.x), int(data.pt.y))
        if self.suppress() and should_suppress(w_param, flags):
            return True
        return False


# ---- 拖拽期间临时让位（详见 MouseMenuWatcher.suspend）----
# 进程内只有一个监听器，由 MouseMenuWatcher.__init__ 登记，供拖拽路径调用。
_watcher: "MouseMenuWatcher | None" = None


def pause() -> bool:
    """临时卸载全局鼠标钩子（拖动开始时调用），返回是否真的卸掉了。

    拖动走 OLE 的 DoDragDrop，主线程会一直握着 GIL 待在那个原生模态循环里，
    Python 钩子回调拿不到 GIL 就把整个鼠标输入通路卡住。拖拽期间先让位。
    """
    return bool(_watcher is not None and _watcher.suspend())


def unpause() -> None:
    """与 pause 配对（拖动结束时调用）。"""
    if _watcher is not None:
        _watcher.restore()
