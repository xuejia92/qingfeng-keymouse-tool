"""全局「按 Esc 取消」监听：临时启停，只监听不拦截。

场景：定时关机的提醒倒计时期间，用户想反悔不用去找流程面板的「停止」按钮，
直接按 Esc 即可（调用方见 `tasks._warn_before_power`）。

为什么单开一个模块：`keyboard` 的全局钩子有几条必须遵守的约束，集中在一处好测也好改。

- **绝不 suppress**：Esc 是用户日常在用的键（关弹窗、退全屏、收输入法候选…），
  一旦拦截，全系统按 Esc 就再也传不到别的程序了。`hotkey_manager` 里单键热键默认
  `suppress=True`，那是给 F6 这类几乎没别处用途的键准备的，**不能照搬**。
- **注销只摘自己那一个**：绝不能图省事用 `keyboard.unhook_all()`，那会把程序里
  所有全局热键（显示/隐藏、紧急停止、连点…）一起干掉。
- **同一个键绝不向库挂第二次**：`on_press_key` 走的 `hook_key` 把注销函数写进按
  **键名**索引的全局表（`_hooks[key] = remove_`），同一个键挂两次，第二次注销时
  先 `del` 表项再摘回调 —— `KeyError` 让回调留在了系统里，变成一个甩不掉的全局
  Esc 钩子（`add_hotkey` 那条路更糟：它直接把 `_hotkeys[hotkey]` 覆盖掉）。
  所以本模块自己记引用计数：**库上永远只挂一个钩子**，回调在 `_fire` 里分发给
  所有监听者，最后一个监听者走人时才注销。
- **注销不在键盘回调线程里做**：回调用 `with` 退出时的调用方线程注销，避免在
  `keyboard` 钩子线程里改它自己的状态。
- **失败一律静默降级成 False**：没装 keyboard 库 / 没权限 / 系统不让挂钩，
  后果只是「按 Esc 没反应」，**绝不能让整个关机流程跟着判失败**。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

# keyboard 库的键名（与 app/keymap.py 的映射、hotkey_manager 的注册格式一致）
ESC_KEY = "esc"

_lock = threading.Lock()
_listeners: list["EscListener"] = []      # 当前所有监听者（顺序无关）
_unregister: Callable[[], None] | None = None   # 库级注销函数；没有监听者时为 None


def _fire(event=None) -> None:
    """库级键盘回调（跑在 keyboard 的钩子线程里）：分发给所有监听者，异常逐个吞掉。"""
    for listener in list(_listeners):
        listener.deliver()


def _install() -> tuple[bool, Callable[[], None] | None]:
    """挂库级钩子（整个进程只会挂这一个）。返回 (是否成功, 注销函数)。"""
    try:
        import keyboard
    except Exception:
        log.warning("未安装 keyboard 库，按 Esc 取消不可用")
        return False, None
    try:
        # 只监听不拦截：suppress 必须显式为 False（见模块说明）
        handler = keyboard.on_press_key(ESC_KEY, _fire, suppress=False)
    except Exception:
        log.exception("注册 Esc 取消监听失败")
        return False, None
    log.info("已开启 Esc 取消监听（提醒倒计时期间按 Esc 可取消）")

    def unregister() -> None:
        try:
            keyboard.unhook(handler)
            log.info("已关闭 Esc 取消监听")
        except Exception:
            log.exception("注销 Esc 取消监听失败")

    return True, unregister


def _warn_if_hotkey_conflict() -> None:
    """Esc 若已被别的热键占用，提示一句：倒计时期间按 Esc 会同时触发两边。"""
    try:
        from .hotkey_policy import check
        owner = check(ESC_KEY)
    except Exception:
        return
    if owner:
        log.warning("Esc 已被「%s」占用：倒计时期间按 Esc 会同时触发它", owner)


class EscListener:
    """一次性的全局 Esc 监听器；用 `with` 保证「注册了就一定注销」。

        with EscListener(on_esc) as ok:
            ...   # ok=False 表示没挂上，此时提示文案里不能写「按 Esc 取消」

    回调 `on_esc` 在 keyboard 的钩子线程里执行，必须短小且不抛异常；
    多个监听器可并存（各跑一步的多个流程），互不干扰。
    """

    def __init__(self, on_esc: Callable[[], None]):
        self._on_esc = on_esc
        self._active = False

    def deliver(self) -> None:
        """把一次按键转交给自己的回调；异常就地吞掉（不能让钩子线程崩）。"""
        try:
            self._on_esc()
        except Exception:
            log.exception("Esc 取消回调出错")

    def start(self) -> bool:
        """开始监听，返回是否成功（任何失败都不抛异常）。重复调用是幂等的。"""
        global _unregister
        with _lock:
            if self._active:
                return True
            if not _listeners:
                ok, unregister = _install()
                if not ok:
                    return False
                _unregister = unregister
            _listeners.append(self)
            self._active = True
        _warn_if_hotkey_conflict()
        return True

    def stop(self) -> None:
        """结束监听；没挂上 / 已结束都静默返回。最后一个监听者走人时才摘钩子。"""
        global _unregister
        with _lock:
            if not self._active:
                return
            self._active = False
            try:
                _listeners.remove(self)
            except ValueError:
                pass
            if _listeners or _unregister is None:
                return
            unregister, _unregister = _unregister, None
        unregister()      # 锁外调用：keyboard 内部有自己的锁，别嵌套

    def active(self) -> bool:
        """当前是否在监听。"""
        return self._active

    def __enter__(self) -> bool:
        # start() 自己已经吞了所有异常；这里再兜一层，保证「进入监听」这一步
        # 在任何意外下都只是返回 False，绝不会把调用方的流程掀翻。
        try:
            return self.start()
        except Exception:
            log.exception("开启 Esc 取消监听异常")
            self._active = False
            return False

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False
