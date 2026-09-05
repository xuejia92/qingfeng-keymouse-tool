"""语音播报步骤：基于 pyttsx3（TTS）的封装。

两个必须处理的技术点：

1. pyttsx3 引擎非线程安全 + Windows SAPI5 走 COM（COM 对象绑定创建线程，
   跨线程调用抛 RPC_E_WRONG_THREAD）→ 引擎操作收敛到**单个常驻的专用播放
   线程**里：任务经队列排队串行，同一时刻只播一段，结果经事件带回。

2. pyttsx3 引擎在首次 runAndWait() 后内部状态机即被破坏：第二次 runAndWait
   一进 driver.startLoop() 就把队列里的 endLoop 命令消费掉，不等待当前文本
   播完就返回（实测首轮 1.4s 正常朗读、第二轮 0.08s 直接跳过）。因此**不复用
   引擎**——每个播报任务都新建一个引擎（实测初始化仅 1~150ms，代价可忽略），
   用完即弃，从根上规避状态残留。

   但「每个任务新建引擎」单独还不够：win32com 把旧引擎的 COM 事件连接挂在
   进程级全局状态上，只要旧引擎对象还活着，紧随其后新建的引擎第二次
   runAndWait 依旧 0.08s 直接跳过（实测）。所以每次播报收尾必须显式
   `del engine` 断引用并 `gc.collect()` 触发 COM 事件连接拆除——实测释放后
   连续多次播报均 ~1.5s 正常朗读（对比：不释放第二轮 74ms/跳过）。

pyttsx3 属可选依赖：未安装或初始化失败，结果会把原因带回，由调用方决定
是否判步骤失败。
"""
from __future__ import annotations

import ctypes
import gc
import queue
import threading

_jobs: queue.Queue = queue.Queue()
_worker_started = False
_state_lock = threading.Lock()   # 保护惰性启动逻辑（主调用线程侧）
_WAIT_TIMEOUT = 600.0            # 同步等待播完的超时（秒），防播放线程卡死


class _Job:
    """一次播报任务：text=播报内容；done 事件 + 结果带回给等待方。"""

    def __init__(self, text: str):
        self.text = text
        self.done = threading.Event()
        self.ok = False
        self.why = ""


def _new_engine():
    """新建一个 pyttsx3 引擎并优先中文语音；失败抛异常由 worker 带回。

    为什么每次播报都新建：pyttsx3 引擎复用超过一次就会「第二次 runAndWait
    不等待播完直接返回」，属于引擎自身的状态机问题，无法在本层修，只能
    用后即弃（实测 init 仅 1~150ms）。
    """
    import pyttsx3
    engine = pyttsx3.init()
    # 优先中文语音（Windows SAPI5 常见 Microsoft Huihui 等），找不到保持默认
    for v in engine.getProperty("voices"):
        vid = (v.id or "").lower()
        if "zh" in vid or "chinese" in vid or "huihui" in vid:
            engine.setProperty("voice", v.id)
            break
    return engine


def _worker_loop() -> None:
    """常驻播放线程：循环消费队列，每个任务新建引擎串行播报。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED
    except Exception:
        pass

    while True:
        job = _jobs.get()
        try:
            engine = _new_engine()
        except Exception as exc:     # 未装 pyttsx3 / 无声卡驱动等
            job.ok = False
            job.why = f"语音引擎初始化失败（{type(exc).__name__}: {exc}）"
            job.done.set()
            continue
        try:
            engine.say(job.text)
            engine.runAndWait()      # 阻塞直到本段播完（新引擎首次运行，可靠）
            job.ok = True
            job.why = "语音播报完成"
        except Exception as exc:     # 播报阶段异常
            job.ok = False
            job.why = f"语音播报失败（{type(exc).__name__}: {exc}）"
        finally:
            job.done.set()
            # 关键收尾：用完即弃 + 主动断引用。win32com 会把 COM 事件连接挂在
            # 进程级全局状态上，旧引擎不释放，下一个新引擎的 runAndWait 会不
            # 等待播完直接返回（实测第二轮 0.08s 跳过；del+gc 后正常 ~1.5s）。
            del engine
            gc.collect()


def _submit(text: str) -> _Job:
    """把播报任务交给播放线程；必要时惰性启动线程。"""
    global _worker_started
    with _state_lock:
        if not _worker_started:
            _worker_started = True
            threading.Thread(target=_worker_loop, name="speech-worker",
                             daemon=True).start()
    job = _Job(text)
    _jobs.put(job)
    return job


def speak(text: str) -> tuple[bool, str]:
    """同步播报一段文本，阻塞直到播完（或超时）。返回 (成功?, 原因)。

    text：播报内容（调用方负责 $变量名 解析）；空内容直接判失败。
    """
    text = (text or "").strip()
    if not text:
        return False, "播报内容为空"
    job = _submit(text)
    job.done.wait(_WAIT_TIMEOUT)
    if not job.done.is_set():
        return False, "语音播报超时"
    return job.ok, job.why


def speak_async(text: str) -> tuple[bool, str]:
    """后台排队播报，不阻塞当前线程。返回 (是否已提交?, 说明)。"""
    text = (text or "").strip()
    if not text:
        return False, "播报内容为空"
    _submit(text)
    return True, "已提交后台语音播报（不等待）"


def _reset_for_test() -> None:
    """仅供测试：清空队列与启动状态，避免测试间互相污染。"""
    global _worker_started
    _worker_started = False
    while True:
        try:
            _jobs.get_nowait()
        except queue.Empty:
            break
