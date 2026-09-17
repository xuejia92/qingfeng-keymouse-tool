"""单实例锁：保证同时只有一个程序实例在运行。

为什么不能只写 `QLockFile(p); if not tryLock(0): 弹窗; return`：
原代码就是这么写的，结果只有一条死路——只要 tryLock 失败，无论原因是什么，
用户都只能看到一句「程序已在运行中」然后程序退出，没有任何继续的入口。

实际机制（Windows 实测，PySide6 6.x）：
- QLockFile 在 Windows 上靠**文件独占句柄**判断，而不是靠读文件里的 PID。
  持有锁的进程一死，句柄由内核回收，下一次 tryLock 通常直接成功
  （实测：硬杀进程后锁文件残留、文件才 4 秒新，重启照样能拿到锁）。
- 也就是说「进程被强杀 → 锁永久残留」这个常见说法在 Windows 上并不成立，
  getLockInfo() 里的 PID 只是给人看的附加信息。

那这个文件还剩什么用？两件事：
1. **留一个逃生舱**：确实有另一个实例在跑时，把对方 PID 报给用户，
   并给「强制启动」按钮，而不是一棍子打死。这是原代码最缺的东西。
2. **兜底陈旧锁**：不把 Qt 的实现细节当保证。万一锁文件处于异常状态
   （文件系统异常、句柄泄漏、将来换平台），这里主动查 PID 存活并清理。
   PID 复用也要处理——光看「进程在不在」会把回收后的 PID 误判成自己人。

策略（try_acquire）：
- tryLock 成功 → 直接返回
- tryLock 失败 → 取出锁里记录的 PID，查它是否存活且确实是本程序
  - 已死 / PID 被别的程序复用 → 判定陈旧锁，删文件后重试一次
  - 确实是本程序在跑 → 返回该 PID，由调用方询问用户是否强制启动
"""
from __future__ import annotations

import logging
import os
import sys
import time

from PySide6.QtCore import QAbstractNativeEventFilter, QLockFile

_LOG = logging.getLogger(__name__)

# 本程序的可执行文件名（源码运行是 python.exe，打包后是程序名.exe）
_SELF_EXE = os.path.basename(sys.executable or "").lower()


def _extract_pid(info) -> int | None:
    """从 getLockInfo() 的返回值里取出 pid。

    注意顺序陷阱：PySide6 6.x 实测返回 (pid, hostname, appname)，而 Qt C++ 文档的
    形参顺序是 (hostname, pid, appname)。这里不按下标取，而是挑出整数项，
    免得绑死在某一版的顺序约定上。
    """
    if not info:
        return None
    if isinstance(info, (tuple, list)):
        for v in info:
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return int(v)
    return None


def _safe_get_lock_info(lock: QLockFile):
    try:
        return lock.getLockInfo()
    except (AttributeError, TypeError, RuntimeError) as e:
        _LOG.debug("读取锁信息失败: %s", e)
        return None


def _process_is_self(pid: int) -> bool:
    """pid 是否存在，且它的可执行文件与本程序一致。

    只看「进程是否存在」是不够的：PID 会被系统回收复用，回收后光凭存在性
    会把陈旧锁误判成「本程序还在运行」，从而继续拒绝启动。
    """
    if not _SELF_EXE:
        return _pid_exists(pid)
    return _pid_exists(pid) and _SELF_EXE in (_pid_exe_name(pid) or "").lower()


def _pid_exists(pid: int) -> bool:
    """进程是否还活着（跨平台，不依赖 psutil）。"""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)                      # 信号 0：只探测进程是否存在
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                          # 进程存在，只是没权限发信号
    except OSError:
        return False
    return True


def _pid_exe_name(pid: int) -> str | None:
    """进程的可执行文件名（仅文件名，不含路径）；取不到返回 None。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value) if ok else None
    finally:
        kernel32.CloseHandle(handle)


def _remove_lock_file(lock_path: str) -> bool:
    try:
        if os.path.isfile(lock_path):
            os.remove(lock_path)
        return True
    except OSError as e:
        _LOG.warning("删除单实例锁文件失败: %s", e)
        return False


def try_acquire(lock_path: str) -> tuple[QLockFile | None, int | None]:
    """尝试获取单实例锁。

    返回 (lock, holder_pid)：
    - lock 非 None              获取成功，调用方需持有该对象直到程序退出
    - lock 为 None、holder 为 pid  确实有另一个实例在运行
    - lock 为 None、holder 为 None 锁文件无法创建/清理（权限或路径问题）
    """
    lock = QLockFile(lock_path)
    if lock.tryLock(0):
        return lock, None

    holder = _extract_pid(_safe_get_lock_info(lock))
    if holder is None or _process_is_self(holder):
        return None, holder

    # 到这里有两种情况，必须分开处理，否则会把用户推进死胡同：
    #  a) 进程真没了 —— 陈旧锁，删掉重来就行
    #  b) 进程活着，但**不是本程序** —— 比如源码运行时 _SELF_EXE 是 python.exe，
    #     而锁是打包版 exe（或另一种启动方式）留下的。文件被占着删不掉，
    #     但它一点都不"陈旧"。（实测踩过：直接报"权限不足"把人堵死。）
    holder_alive = _pid_exists(holder)

    # 删不掉也要重试加锁：删除失败可能只是杀软/索引器短暂占用，
    # 而 QLockFile 能不能拿到锁取决于有没有活句柄，与文件能否被删是两回事。
    for attempt in range(4):
        _remove_lock_file(lock_path)
        retry = QLockFile(lock_path)
        if retry.tryLock(0):
            if not holder_alive:
                _LOG.warning("检测到陈旧的单实例锁（原持有进程 %s 已不存在），已自动清理",
                             holder)
            else:
                _LOG.warning("锁记录的是进程 %s（非本程序），已接管", holder)
            return retry, None
        if attempt < 3:
            time.sleep(0.25)

    # 反复拿不到：确实有活进程占着。把 PID 交回调用方，让它问用户要不要
    # 强制启动——总比一句"权限不足"把人堵死强。
    return None, holder if holder_alive else None


def force_acquire(lock_path: str) -> QLockFile | None:
    """强制接管锁：无条件删除锁文件后重新加锁。

    只在用户明确选择「强制启动」时调用——两个实例同时跑会导致全局热键冲突、
    配置文件互相覆盖，所以风险要由用户自己确认。
    """
    _remove_lock_file(lock_path)
    lock = QLockFile(lock_path)
    return lock if lock.tryLock(0) else None


# ---- 二次启动置前：通过广播消息通知已运行实例显示主窗口 ----
#
# 用户双击再次打开程序时，与其弹「程序已在运行中 / 强制启动」对话框，
# 不如直接让已经在跑的实例把主窗口显示并置前（隐藏到托盘时也能唤醒）。
# 用 RegisterWindowMessageW 注册一个跨进程共享的消息 id（同名注册返回同一 id），
# 再由新实例 PostMessage(HWND_BROADCAST) 广播；已运行实例用 QAbstractNativeEventFilter
# 监听，收到后调用自身 show_window()（Qt 状态正确、置前最稳）。

_SHOW_MESSAGE_NAME = "QingFengKeyMouseTool_ShowWindow"


def show_request_message_id() -> int:
    """注册「显示主窗口」跨进程消息，返回系统内唯一 id（同名注册返回同一 id）。"""
    if sys.platform != "win32":
        return 0
    import ctypes
    return int(ctypes.windll.user32.RegisterWindowMessageW(_SHOW_MESSAGE_NAME))


def broadcast_show_request() -> bool:
    """广播「显示主窗口」消息，由已在运行的实例接收后置前显示。"""
    msg = show_request_message_id()
    if not msg:
        return False
    import ctypes
    HWND_BROADCAST = 0xFFFF
    return bool(ctypes.windll.user32.PostMessageW(HWND_BROADCAST, msg, 0, 0))


# ---- 退出请求：让 restart_watchdog 能请主程序「优雅退出」而不是硬杀 ----
#
# Ctrl+R 重启时如果用 TerminateProcess 硬杀，aboutToQuit 不会触发：
# 流程开着的浏览器（DrissionPage/Chrome）会残留、配置可能写一半。
# 这里复用上面同一套注册消息机制：看门狗广播退出消息 → 主程序收到后调用
# QApplication.quit()，正常走 aboutToQuit（关浏览器、收尾）再退出。
# 消息名必须与 restart_watchdog.py 里的 QUIT_MESSAGE_NAME 完全一致。

_QUIT_MESSAGE_NAME = "QingFengKeyMouseTool_Quit"


def quit_request_message_id() -> int:
    """注册「退出」跨进程消息，返回系统内唯一 id（同名注册返回同一 id）。"""
    if sys.platform != "win32":
        return 0
    import ctypes
    return int(ctypes.windll.user32.RegisterWindowMessageW(_QUIT_MESSAGE_NAME))


def broadcast_quit_request() -> bool:
    """广播「退出」消息，由已在运行的实例接收后正常退出（幂等，没人监听也无副作用）。"""
    msg = quit_request_message_id()
    if not msg:
        return False
    import ctypes
    HWND_BROADCAST = 0xFFFF
    return bool(ctypes.windll.user32.PostMessageW(HWND_BROADCAST, msg, 0, 0))


class ShowRequestFilter(QAbstractNativeEventFilter):
    """监听广播消息：命中「显示主窗口」就显示并置前，命中「退出」就正常退出。

    两个消息共用同一个过滤器（都是跨进程广播、都只在主线程回调里做一件事）：

    - 「显示主窗口」（show_request_message_id）：二次启动时把已运行实例唤到前台。
    - 「退出」（quit_request_message_id）：restart_watchdog 重启前请主程序优雅退出。
      这是可选回调——不传 on_quit 时行为和以前完全一样。

    ⚠️ 这个函数会被**每一条** Windows 消息调用一次——包括拖动时 OLE 拖拽循环
    里那一大片消息。所以它必须是全程序最便宜的 Python 函数之一：早退分支只做
    整数比较，绝不做 bytes() / ctypes.POINTER() / 结构体实例化这些每次都要
    分配对象的事。

    原来的写法每条消息都走 `bytes(event_type)` + `_win_msg_id()` 里的
    `ctypes.cast(ptr, POINTER(MSG)).contents`（**每次都新建一个 MSG 对象**），
    实测 3.66 µs/条，是现在这版的 14 倍。拖动时消息量极大，主线程几乎全耗在
    这个过滤器里。
    """

    _WIN_GENERIC = b"windows_generic_MSG"

    def __init__(self, on_show, on_quit=None):
        super().__init__()
        self._on_show = on_show
        self._on_quit = on_quit          # 为空则忽略退出请求（老调用方 behavior 不变）
        self._msg = show_request_message_id()
        self._quit_id = quit_request_message_id()
        # 直接按字节偏移读 MSG.message，避免每次调用新建 MSG 结构体对象。
        # 偏移从 ctypes 的类型描述里取，不写死魔数（x64 上是 8）。
        try:
            from ctypes import wintypes
            self._id_off = wintypes.MSG.message.offset
        except Exception:
            self._id_off = 8

    def nativeEventFilter(self, event_type, message):
        show_id = self._msg
        quit_id = self._quit_id if self._on_quit is not None else 0
        if event_type != self._WIN_GENERIC or not (show_id or quit_id):
            return False, 0
        try:
            import ctypes
            got = ctypes.c_uint.from_address(int(message) + self._id_off).value
        except Exception:
            return False, 0
        if show_id and got == show_id:
            try:
                self._on_show()
            except Exception:
                _LOG.exception("响应二次启动置前失败")
            return True, 0
        if quit_id and got == quit_id:
            try:
                self._on_quit()
            except Exception:
                _LOG.exception("响应退出请求失败")
            return True, 0
        return False, 0
