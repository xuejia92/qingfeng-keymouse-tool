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

from PySide6.QtCore import QLockFile

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
