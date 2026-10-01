"""定时截屏 + 邮箱上报后台任务（主程序常驻线程）。

- 每 capture_interval_sec 秒截取整个虚拟桌面（含多显示器），JPEG **只存在内存里**
  （不再往磁盘写任何文件；历史版本的截图暂存目录已废弃，程序目录不会再出现它）
- 每 send_interval_min 分钟把内存中全部截图打包成 zip 发到收件邮箱，
  发送成功后立刻释放这批内存；发送失败则留在内存里下轮重试
- 内存积压超过 MAX_PENDING_SHOTS 张或 MAX_PENDING_BYTES 字节时丢弃最旧的，
  防止「邮箱没配好 / 一直发不出去」时把内存撑爆
- SMTP 参数在 config.json：mail_host/port/user/auth_code/to；
  QQ 邮箱需开启 SMTP 服务并用「授权码」作密码（邮箱网页版 -> 设置 -> 账户），
  未配置授权码时只截图不发送（同样受积压上限保护）
- 设备 ID（注册表 MachineGuid）会随邮件标题/正文一起发送；
  命中排除名单（config.json 的 capture_excluded_ids）的设备不启用本功能。
  名单每轮从磁盘重读，所以「手工改 config.json」也是下个周期生效；
  也可以直接在「设置」页看本机设备号并一键加入/移出名单。

为什么不落盘：截图属于临时数据，落盘只会留下无人清理的残留文件；
内存队列的读取/发送都在同一个后台线程里，代价只是几十 MB 常驻内存。

不另起子进程：onefile exe 再拉起自身实例要重新自解压（额外约 200MB 内存），
常驻线程在主程序内等效实现且随主程序一起退出。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import smtplib
import sys
import threading
import time
import zipfile
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config as _config
from .config import EXCLUDED_DEVICE_IDS_DEFAULT

logger = logging.getLogger(__name__)

MAX_PENDING_SHOTS = 600             # 内存待发张数上限，超出丢弃最旧
MAX_PENDING_BYTES = 150 * 1024 * 1024   # 内存待发总字节上限（约 150MB）
MAX_MAIL_BYTES = 40 * 1024 * 1024   # 单封邮件附件上限（QQ 邮箱约 50MB）

# 截屏上报排除名单存在 config.json 的 capture_excluded_ids（逗号/分号/顿号/空白分隔）：
# 每轮都**重新读磁盘**，所以手工改配置文件也是下个周期生效（2026-09-17 修）；
# 设备 ID = 注册表 HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid，比对忽略大小写与连字符
_stop = threading.Event()
_thread: threading.Thread | None = None

# 内存中的待发送截图：[("cap_YYYYmmdd_HHMMSS.jpg", jpeg_bytes), ...]（按时间先后排列）
# 只在后台线程里读写，_lock 兜住 GUI/测试线程的查询，避免迭代中被改
_pending: list[tuple[str, bytes]] = []
_pending_bytes = 0
_lock = threading.Lock()

# 名单分隔符（逗号/分号/顿号/换行/空白，中英文皆可）
_ID_SEP = re.compile(r"[,;、，；\s]+")


def norm_device_id(s: str) -> str:
    """设备 ID 归一化：去掉花括号/连字符/空白，转大写。"""
    return re.sub(r"[{}\-\s]", "", str(s or "")).upper()


# 兼容旧名（早前版本用的是 _norm_guid）
_norm_guid = norm_device_id


def split_device_ids(raw) -> set[str]:
    """把名单字符串拆成归一化集合（自动忽略空项与重复项）。"""
    out: set[str] = set()
    for part in _ID_SEP.split(str(raw or "")):
        v = norm_device_id(part)
        if v:
            out.add(v)
    return out


def _read_machine_guid() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(guid).strip()
    except OSError:
        return ""


DEVICE_ID = _read_machine_guid()   # 本机设备 ID（导入时读取一次，运行期不变）


def device_id() -> str:
    """用于展示/邮件标题的设备 ID；取不到时回退计算机名。"""
    return norm_device_id(DEVICE_ID) or os.environ.get("COMPUTERNAME", "UNKNOWN-DEVICE")


def device_id_raw() -> str:
    """注册表里的原始设备 ID（带连字符，给人看/给人抄的那份）。"""
    return DEVICE_ID or os.environ.get("COMPUTERNAME", "")


def device_id_available() -> bool:
    """能否读到注册表设备号。读不到时按设备号排除无从谈起（会直接放行）。"""
    return bool(norm_device_id(DEVICE_ID))


def excluded_ids_from(cfg) -> set[str]:
    """从配置对象读取排除名单；空则无排除。"""
    return split_device_ids(getattr(cfg, "capture_excluded_ids", ""))


def read_excluded_ids_from_file(path: str | None = None) -> set[str] | None:
    """直接读 config.json 里的排除名单（运行期感知手工修改用）。

    文件不存在 / 内容损坏 / 没有该字段 → 返回 None，由调用方回退内存配置。
    路径取 `config.CONFIG_PATH` 的**当前值**（测试可临时改写它做隔离）。
    """
    p = path or _config.CONFIG_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "capture_excluded_ids" not in data:
        return None
    return split_device_ids(data.get("capture_excluded_ids"))


def is_excluded_device(cfg=None, live: bool = False) -> bool:
    """当前设备是否在排除名单中。

    live=True 时以 config.json 磁盘内容为准（手工改完下个周期即生效），
    读不到再回退 cfg；cfg 也为空则回退内置默认名单。
    """
    did = norm_device_id(DEVICE_ID)
    if not did:
        return False
    if live:
        ids = read_excluded_ids_from_file()
        if ids is not None:
            return did in ids
    if cfg is not None:
        return did in excluded_ids_from(cfg)
    return did in split_device_ids(EXCLUDED_DEVICE_IDS_DEFAULT)


def add_excluded_id(raw: str, dev: str | None = None) -> str:
    """把 dev（默认本机）加入名单串并返回新串；已在名单里则原样返回。

    本机设备号读不到时不动名单——加了也命中不了（is_excluded_device 直接放行）。
    """
    target = norm_device_id(DEVICE_ID if dev is None else dev)
    if not target:
        return str(raw or "")
    if target in split_device_ids(raw):
        return str(raw or "")
    head = str(raw or "").strip().strip(",;、，；").strip()
    return f"{head},{target}" if head else target


def remove_excluded_id(raw: str, dev: str | None = None) -> str:
    """把 dev（默认本机）从名单串里去掉并返回新串；不在名单里则原样返回。"""
    target = norm_device_id(DEVICE_ID if dev is None else dev)
    parts = [x.strip() for x in re.split(r"[,;、，；]", str(raw or "")) if x.strip()]
    if not target or target not in {norm_device_id(x) for x in parts}:
        return str(raw or "")
    return ",".join(x for x in parts if norm_device_id(x) != target)


def start(cfg_getter) -> None:
    """启动后台线程（重复调用忽略）。cfg_getter() 每轮返回最新 AppConfig。

    设备 ID 命中 config.json 排除名单（capture_excluded_ids）时不启动。
    """
    global _thread
    try:
        cfg = cfg_getter()
    except Exception:
        cfg = None
    if is_excluded_device(cfg, live=True):
        logger.info("设备 %s 在截屏上报排除名单，本机不启用该功能", device_id())
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(cfg_getter,),
                               daemon=True, name="截图上报")
    _thread.start()
    logger.info("定时截屏上报已启动（截图仅存内存，不写磁盘；本机设备号 %s，不在排除名单）",
                device_id_raw())
    logger.info("如需让本机不参与截屏上报：把设备号 %s 加入 config.json 的 "
                "capture_excluded_ids（或在「设置」页点「本机不参与截屏上报」）",
                device_id())


def stop(timeout: float = 3.0) -> None:
    """请求停止并短暂等待（进行中的发送由 SMTP 超时保护，随后随进程退出）。"""
    _stop.set()
    t = _thread
    if t and t.is_alive():
        t.join(timeout)
    clear_pending()


# ---------------------------------------------------------------- 内存队列


def pending_count() -> int:
    """内存中待发送的截图张数。"""
    with _lock:
        return len(_pending)


def pending_bytes() -> int:
    """内存中待发送截图的总字节数。"""
    with _lock:
        return _pending_bytes


def _snapshot() -> list[tuple[str, bytes]]:
    """待发送截图的副本（发送期间遍历用，避免持锁做网络 IO）。"""
    with _lock:
        return list(_pending)


def clear_pending() -> int:
    """清空内存待发截图，返回被清掉的张数。"""
    global _pending_bytes
    with _lock:
        n = len(_pending)
        _pending.clear()
        _pending_bytes = 0
    return n


def _drop_names(names: set[str]) -> None:
    """按文件名释放内存（发送成功的那些）。名单里的名字保证唯一。"""
    global _pending_bytes
    with _lock:
        keep: list[tuple[str, bytes]] = []
        for n, d in _pending:
            if n in names:
                _pending_bytes -= len(d)
            else:
                keep.append((n, d))
        _pending[:] = keep


def _unique_name(name: str) -> str:
    """同一秒截两张时避免重名（重名会让发送后的按名释放误删）。"""
    with _lock:
        taken = {n for n, _ in _pending}
    if name not in taken:
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while f"{stem}_{i}{ext}" in taken:
        i += 1
    return f"{stem}_{i}{ext}"


def _cap_pending() -> int:
    """内存积压保护：张数或总字节超上限时丢弃最旧的，返回丢弃张数。"""
    global _pending_bytes
    dropped = 0
    with _lock:
        excess = len(_pending) - MAX_PENDING_SHOTS
        for _ in range(max(0, excess)):
            _pending_bytes -= len(_pending.pop(0)[1])
            dropped += 1
        while _pending and _pending_bytes > MAX_PENDING_BYTES:
            _pending_bytes -= len(_pending.pop(0)[1])
            dropped += 1
    if dropped:
        logger.warning("内存中待发送截图超过上限（%d 张 / %d MB），已丢弃最旧 %d 张",
                       MAX_PENDING_SHOTS, MAX_PENDING_BYTES // (1024 * 1024), dropped)
    return dropped


def _append_pending(name: str, data: bytes) -> None:
    """把一张截图放进内存队列（先做重名处理，再按上限丢最旧）。"""
    global _pending_bytes
    name = _unique_name(name)
    with _lock:
        _pending.append((name, data))
        _pending_bytes += len(data)
    _cap_pending()


def _capture_once() -> tuple[str, bytes] | None:
    """截取整个虚拟桌面（含多显示器），JPEG 压进内存，返回 (文件名, 字节)。"""
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=60)
    data = buf.getvalue()
    name = time.strftime("cap_%Y%m%d_%H%M%S") + ".jpg"
    _append_pending(name, data)
    return name, data


# ---------------------------------------------------------------- 发送


def _split_batches(items: list[tuple[str, bytes]]) -> list[list[tuple[str, bytes]]]:
    """按单封附件大小上限把待发截图分批。"""
    batches: list[list[tuple[str, bytes]]] = []
    batch: list[tuple[str, bytes]] = []
    total = 0
    for item in items:
        sz = len(item[1])
        if batch and total + sz > MAX_MAIL_BYTES:
            batches.append(batch)
            batch, total = [], 0
        batch.append(item)
        total += sz
    if batch:
        batches.append(batch)
    return batches


def _send_batch(cfg, items: list[tuple[str, bytes]]) -> bool:
    """把一批内存截图打包 zip 发送；成功返回 True（由调用方释放对应内存）。"""
    dev = device_id()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            z.writestr(name, data)
    msg = MIMEMultipart()
    msg["From"] = cfg.mail_user
    msg["To"] = cfg.mail_to
    msg["Subject"] = Header(
        f"屏幕截图上报 [{dev}] {time.strftime('%Y-%m-%d %H:%M:%S')}（{len(items)} 张）",
        "utf-8")
    msg.attach(MIMEText(f"定时截屏自动上报，共 {len(items)} 张。\n设备ID：{dev}",
                        "plain", "utf-8"))
    att = MIMEApplication(buf.getvalue())
    att.add_header("Content-Disposition", "attachment",
                   filename=time.strftime("screenshots_%Y%m%d_%H%M%S") + ".zip")
    msg.attach(att)
    to_list = [a.strip() for a in str(cfg.mail_to).split(",") if a.strip()]
    with smtplib.SMTP_SSL(cfg.mail_host, int(cfg.mail_port), timeout=30) as s:
        s.login(cfg.mail_user, cfg.mail_auth_code)
        s.sendmail(cfg.mail_user, to_list or [cfg.mail_user], msg.as_bytes())
    return True


def _send_and_clear(cfg) -> None:
    """把内存里的截图分批发送，成功的批次释放内存；未配置授权码则跳过发送。"""
    _cap_pending()
    items = _snapshot()
    if not items:
        return
    if not (cfg.mail_user and cfg.mail_auth_code):
        logger.warning("邮箱未配置授权码（config.json 的 mail_auth_code），"
                       "本轮 %d 张截图暂不发送（留在内存等待发送）", len(items))
        return
    sent = 0
    for batch in _split_batches(items):
        try:
            _send_batch(cfg, batch)
            sent += len(batch)
            _drop_names({name for name, _ in batch})
        except Exception as e:
            logger.warning("截图上报发送失败（%d 张留在内存下轮重试）：%s", len(batch), e)
    if sent:
        logger.info("截图上报：已发送 %d 张并释放内存（当前内存剩余 %d 张）",
                    sent, pending_count())


def _loop(cfg_getter) -> None:
    cfg = cfg_getter()
    next_send = time.monotonic() + max(1, int(getattr(cfg, "send_interval_min", 5))) * 60
    # 上一轮的排除状态：只在状态翻转时打一条日志，避免每 10 秒刷屏
    excluded_prev: bool | None = None
    while True:
        try:
            cfg = cfg_getter()          # 每轮取最新配置
        except Exception:
            pass
        # live=True：直接看 config.json 磁盘内容——手工改配置也是下个周期生效
        excluded = is_excluded_device(cfg, live=True)
        if excluded != excluded_prev:
            if excluded:
                dropped = clear_pending()
                logger.info("本机设备 %s 已在排除名单，暂停截屏上报"
                            "（已释放内存中未发送的 %d 张）", device_id(), dropped)
            else:
                logger.info("本机设备 %s 不在排除名单，截屏上报运行中（每 %s 秒截图存内存、"
                            "每 %s 分钟发送）",
                            device_id(), getattr(cfg, "capture_interval_sec", 10),
                            getattr(cfg, "send_interval_min", 5))
            excluded_prev = excluded
        if not excluded:
            try:
                _capture_once()
            except Exception:
                logger.warning("截图失败", exc_info=True)
            if time.monotonic() >= next_send:
                next_send = (time.monotonic()
                             + max(1, int(getattr(cfg, "send_interval_min", 5))) * 60)
                try:
                    _send_and_clear(cfg)
                except Exception:
                    logger.warning("截图上报异常", exc_info=True)
        if _stop.wait(max(1, int(getattr(cfg, "capture_interval_sec", 10)))):
            break
