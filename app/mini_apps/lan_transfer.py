# -*- coding: utf-8 -*-
"""内置小程序：局域网文件传输（电脑 → 手机，扫码即下）。

一句话流程
----------
添加文件（拖拽 / 选文件）→ 自动起一个临时 HTTP 服务 → 生成指向**局域网地址**的
二维码 → 手机扫码打开下载页 → 点文件名下载。关掉窗口服务即停。

界面入口
--------
「🧰 小工具」页的第三张卡片（卡片点开是一个**独立窗口**，见 `app/ui/mini_window.py`）。
窗口内部左右两栏：左边是拖拽投放区 + 文件列表，右边是二维码与访问地址。

文件添加与拖拽
--------------
- **拖拽**：把文件从资源管理器拖到左侧列表上；按下期间列表边框变主色虚线 + 底色变浅，
  松手即加入；文件夹会被忽略并提示（不支持整目录）。
- **手动**：点「添加文件…」多选。
- 重复添加同一个文件（按**规范化后的绝对路径**判定）会被跳过并提示。

中转服务与「临时存储」逻辑
--------------------------
服务用标准库 `http.server.ThreadingHTTPServer` 起，绑 `0.0.0.0` 的**随机空闲端口**
（端口 0 让系统分配，避免与其它程序抢）；只接受 `/{token}/...` 形式的路径，令牌是
每次会话随机生成的 6 位串——**裸 `IP:端口` 访问只会得到 404**，避免同一局域网里
别人扫端口就翻到你的文件。

**文件不复制、不落盘**：会话里只保留一张内存中的**白名单**（`uid -> 绝对路径`），
服务器只肯发白名单里的文件（路径永远由 uid 反查，不接收客户端给的路径，因此天然
没有目录穿越/任意文件读取）。这样做的理由：
1. 传大文件前先复制一份等于**等两遍**、还占双份磁盘，而手机下载本来就是从原文件读；
2. 「临时」的语义落在**生命周期**上——关掉窗口（或点「停止服务」）白名单立即失效，
   磁盘上不会留下任何残留物，比往 %TEMP% 里拷一份再删更干净。
代价是被加入的文件在传输期间不能移动/删除（移了会 404）。若要改成「先复制到临时目录」
的形态，只需在 `TransferSession.add_paths` 里把 path 换成一份拷贝即可，界面无感。

二维码与局域网地址
------------------
局域网地址靠两条来源合并：① UDP 连接技巧（`connect(("8.8.8.8", 80))` 不实际发包，
只让系统选出默认出口网卡的地址）② `getaddrinfo(hostname)` 解析出的其它地址。
只保留**私网/链路本地**且非回环的地址，去重后放进下拉框——多网卡（有线/无线/虚拟机/
VPN）时用户可以手动切到手机实际能到的那一块网卡。
二维码用 `segno` 编码成模块矩阵，再由 **QPainter 自绘**成 QPixmap（**纯黑白**，
不跟主题换色——扫码依赖对比度，深色主题下也必须黑底白面）。

扫码后的下载页
--------------
`GET /{token}/` 返回一张手机友好的 HTML（自适应、大按钮、UTF-8 文件名），列出每个
文件的**名称、大小、下载按钮**；`GET /{token}/f/{uid}` 以
`Content-Disposition: attachment` + `filename*=UTF-8''...` 发送原文件流（中文名不乱码），
并带准确的 `Content-Length`，手机上能看到下载进度。
"""
from __future__ import annotations

import html
import http.server
import ipaddress
import os
import shutil
import socket
import secrets
import string
import threading
import webbrowser
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from ..ui import theme
from ..ui.widgets import set_variant
from . import MiniApp, register

try:                                    # 静态导入，PyInstaller 能扫到
    import segno
except ImportError:                     # pragma: no cover - 缺依赖时降级，不崩界面
    segno = None

TOKEN_LEN = 6                           # 访问令牌长度（随机、每次会话不同）
QR_PIXELS = 240                         # 二维码目标边长（像素）
QR_QUIET_ZONE = 4                       # 二维码四周静默区（模块数，规范要求 >= 4）
CHUNK = 64 * 1024                       # 发送文件的分块大小


# ---------------------------------------------------------------------------
# 局域网地址
# ---------------------------------------------------------------------------
def _is_usable_lan_ip(ip: str) -> bool:
    """是不是手机可能访问到的地址（排除回环/公网/非法）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4 or addr.is_loopback:
        return False
    return bool(addr.is_private or addr.is_link_local)


def local_ipv4_candidates() -> list[str]:
    """本机可供局域网访问的 IPv4 候选地址（默认出口网卡排第一）。

    多网卡时返回多个：用户可以在界面上切换，选手机实际能到的那一个。
    """
    found: list[str] = []

    def _push(ip: str) -> None:
        if ip and ip not in found:
            found.append(ip)

    # ① 连一个外部地址（UDP 不发包）让系统挑出默认出口网卡
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.3)
            probe.connect(("8.8.8.8", 80))
            _push(probe.getsockname()[0])
    except OSError:
        pass

    # ② 主机名解析出来的其它地址（覆盖多网卡 / 虚拟机 / VPN）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            _push(info[4][0])
    except OSError:
        pass

    usable = [ip for ip in found if _is_usable_lan_ip(ip)]
    if usable:
        return usable
    # 都不可用（例如完全断网）：至少给一个能自测的地址
    return [ip for ip in found if not ip.startswith("127.")] or ["127.0.0.1"]


def download_url(ip: str, port: int, token: str) -> str:
    """手机扫码后要打开的地址。"""
    return f"http://{ip}:{port}/{token}/"


# ---------------------------------------------------------------------------
# 会话：内存白名单（「临时存储」就体现在这里）
# ---------------------------------------------------------------------------
@dataclass
class SharedFile:
    """一个待分享的文件（只记路径，不复制）。"""

    uid: str
    name: str
    path: str

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def exists(self) -> bool:
        return os.path.isfile(self.path)


def human_size(num: int) -> str:
    """字节数 -> 人类可读。实现已挪到 ui.widgets（两个小工具共用），这里转发。"""
    from ..ui.widgets import human_size as _human_size
    return _human_size(num)


@dataclass
class TransferSession:
    """一次传输会话：随机令牌 + 文件白名单。

    ⚠️ 全部状态只在内存里，且**不作为任何形式的磁盘副本**：停止服务/关窗即失效。
    """

    token: str = field(default_factory=lambda: secrets.token_urlsafe(TOKEN_LEN)
                       .replace("-", "a").replace("_", "b")[:TOKEN_LEN])
    files: list[SharedFile] = field(default_factory=list)

    # ---------- 增删 ----------
    def add_paths(self, paths) -> tuple[list[SharedFile], list[str]]:
        """把一批路径加入白名单。

        返回 `(新增的, 被跳过的说明)`：不存在的、目录、重复的都会被跳过并给理由，
        让界面能明确告诉用户「哪些没加进去、为什么」。
        """
        added: list[SharedFile] = []
        skipped: list[str] = []
        known = {os.path.normcase(os.path.abspath(item.path))
                 for item in self.files}
        for raw in paths:
            path = (raw or "").strip()
            if not path:
                continue
            if not os.path.exists(path):
                skipped.append(f"{path}（不存在）")
                continue
            if os.path.isdir(path):
                skipped.append(f"{os.path.basename(path)}（暂不支持文件夹）")
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in known:
                skipped.append(f"{os.path.basename(path)}（已在列表中）")
                continue
            known.add(key)
            item = SharedFile(uid=secrets.token_urlsafe(8),
                              name=os.path.basename(path), path=os.path.abspath(path))
            self.files.append(item)
            added.append(item)
        return added, skipped

    def remove(self, uid: str) -> bool:
        before = len(self.files)
        self.files = [item for item in self.files if item.uid != uid]
        return len(self.files) != before

    def clear(self) -> None:
        self.files = []

    def find(self, uid: str) -> SharedFile | None:
        for item in self.files:
            if item.uid == uid:
                return item
        return None

    def total_size(self) -> int:
        return sum(item.size() for item in self.files)


# ---------------------------------------------------------------------------
# HTTP 中转服务
# ---------------------------------------------------------------------------
# ⚠️ 用 string.Template（$占位符）而不是 str.format：模板里全是 CSS 的 {}，
# 走 .format 会把 `{ box-sizing: border-box; }` 当成格式字段直接抛 KeyError，
# 服务端就地崩、客户端只看到连接被关（2026-10-03 踩过）。
_PAGE_TEMPLATE = string.Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>局域网文件传输</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; background:#f4f6f8; color:#24292f;
         font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  .wrap { max-width:680px; margin:0 auto; padding:18px 14px 40px; }
  h1 { font-size:19px; margin:6px 0 4px; }
  .sub { color:#6b7280; font-size:13px; margin-bottom:16px; }
  .item { display:flex; align-items:center; gap:12px; background:#fff;
          border:1px solid #e3e7eb; border-radius:12px; padding:14px 16px;
          margin-bottom:10px; }
  .info { flex:1; min-width:0; }
  .info b { display:block; font-size:15px; word-break:break-all; line-height:1.4; }
  .info span { color:#6b7280; font-size:12px; }
  a.btn { background:#1668a8; color:#fff; text-decoration:none; border-radius:8px;
          padding:10px 16px; font-size:14px; white-space:nowrap; flex:none; }
  a.btn:active { background:#125a93; }
  .empty { color:#6b7280; text-align:center; padding:36px 12px;
           background:#fff; border:1px dashed #d8dee4; border-radius:12px; }
  footer { color:#9ca3af; font-size:12px; text-align:center; margin-top:22px;
           line-height:1.6; }
</style>
</head>
<body><div class="wrap">
<h1>📡 局域网文件传输</h1>
<div class="sub">共 $count 个文件 · 合计 $total · 来自电脑「$host」</div>
$items
<footer>本页面由电脑端临时开启。<br>在电脑上关闭「局域网文件传输」后链接立即失效。</footer>
</div></body></html>
""")

_ITEM_TEMPLATE = string.Template("""<div class="item">
  <div class="info"><b>$name</b><span>$size</span></div>
  <a class="btn" href="$href" download>下载</a>
</div>""")


def _safe_header_text(name: str) -> str:
    """清掉文件名里可能破坏 HTTP 头的控制字符。"""
    return "".join(ch for ch in (name or "") if ch.isprintable() and ch not in '"\r\n')


def content_disposition(name: str) -> str:
    """构造带「中文名不乱码」的 Content-Disposition。

    同时给 `filename=`（ASCII 兜底）与 `filename*=UTF-8''...`（RFC 5987），
    手机浏览器普遍认后者。
    """
    name = _safe_header_text(os.path.basename(name or "")) or "download"
    fallback = name.encode("ascii", "ignore").decode().strip() or "download"
    fallback = fallback.replace("\\", "_")
    return (f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(name, safe='')}")


class _TransferHandler(http.server.BaseHTTPRequestHandler):
    """只认 `/{token}/...` 的极简只读处理器。

    刻意**不提供目录浏览**：路径一律靠 uid 反查白名单，客户端给的字符串永远不会
    被当成文件路径使用，因此不会出现目录穿越 / 任意文件读取。
    """

    server_version = "QingfengLanTransfer"
    sys_version = ""

    def log_message(self, fmt, *args):          # noqa: A003（基类签名）
        """静音：默认会往 stderr 刷访问日志，GUI 程序不需要。"""

    # ---------- 响应助手 ----------
    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        self._send_bytes(code, text.encode("utf-8"),
                         "text/plain; charset=utf-8")

    # ---------- 路由 ----------
    def do_HEAD(self):                          # noqa: N802（Qt/基类命名）
        self.do_GET()

    def do_GET(self):                           # noqa: N802
        session: TransferSession = self.server.session
        prefix = f"/{session.token}"
        path = unquote(urlsplit(self.path).path)
        try:
            if path.rstrip("/") == prefix:
                self._send_index(session)
            elif path.startswith(prefix + "/f/"):
                self._send_file(session, path[len(prefix) + 3:])
            else:
                # 裸 IP/端口、错误令牌、随手扫到的路径：一律 404，不泄露任何信息
                self._send_text(404, "404 Not Found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True        # 手机端中途取消下载
        except OSError:
            self.close_connection = True
            self._send_text(500, "500 读取文件失败")

    # ---------- 页面 ----------
    def _send_index(self, session: TransferSession) -> None:
        items = []
        for item in session.files:
            items.append(_ITEM_TEMPLATE.substitute(
                name=html.escape(item.name),
                size=(human_size(item.size()) if item.exists() else "文件已移动/删除"),
                href=f"/{session.token}/f/{quote(item.uid)}"))
        body = _PAGE_TEMPLATE.substitute(
            count=len(session.files),
            total=human_size(session.total_size()),
            host=html.escape(socket.gethostname()),
            items="\n".join(items) if items
            else '<div class="empty">电脑端还没有添加文件</div>')
        self._send_bytes(200, body.encode("utf-8"),
                         "text/html; charset=utf-8")

    def _send_file(self, session: TransferSession, uid: str) -> None:
        item = session.find(uid)
        if item is None or not item.exists():
            self._send_text(404, "文件不存在或已被移除")
            return
        size = item.size()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", content_disposition(item.name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(item.path, "rb") as handle:   # 分块发送：大文件也不吃内存
            shutil.copyfileobj(handle, self.wfile, CHUNK)


class _ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True       # 关服务时不等在途连接
    allow_reuse_address = True


class TransferServer:
    """把会话挂到一个临时 HTTP 服务上（起/停都由界面控制）。"""

    def __init__(self, session: TransferSession, host: str = ""):
        self.session = session
        self.host = host                     # ""=所有网卡；测试里传 127.0.0.1
        self._httpd: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd is not None else 0

    def start(self) -> int:
        """启动服务并返回端口；已在运行则直接返回现有端口。

        端口用 0 -> 让系统分配空闲端口：既不会被别的程序占用，也不会要求用户
        去改防火墙放行某个固定端口。
        """
        if self._httpd is not None:
            return self.port
        httpd = _ThreadingServer((self.host, 0), _TransferHandler)
        httpd.session = self.session
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="lan-transfer", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """停止服务（幂等；没在跑时什么也不做）。"""
        httpd, self._httpd = self._httpd, None
        self._thread, thread = None, self._thread
        if httpd is not None:
            try:
                httpd.shutdown()
            except OSError:
                pass
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 二维码
# ---------------------------------------------------------------------------
def qr_pixmap(url: str, pixels: int = QR_PIXELS) -> QPixmap | None:
    """把 URL 编码成二维码位图（**纯黑白**，不跟主题换色）。

    ⚠️ 颜色必须固定为黑/白：扫码靠对比度，深色主题下若用面板底色画，手机根本扫不出来。
    `segno` 缺失时返回 None（界面会退回「只显示网址」）。
    """
    if segno is None or not url:
        return None
    code = segno.make(url, error="m")
    matrix = [list(row) for row in code.matrix]
    modules = len(matrix)
    total = modules + QR_QUIET_ZONE * 2
    scale = max(1, int(pixels) // total)
    side = total * scale
    pixmap = QPixmap(side, side)
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    try:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#000000"))
        offset = QR_QUIET_ZONE * scale
        for y, row in enumerate(matrix):
            for x, value in enumerate(row):
                if value:
                    painter.drawRect(offset + x * scale, offset + y * scale,
                                     scale, scale)
    finally:
        painter.end()
    return pixmap


# ---------------------------------------------------------------------------
# 拖拽文件列表
# ---------------------------------------------------------------------------
class FileDropList(QListWidget):
    """支持从资源管理器拖入文件的列表（文件夹会被忽略）。"""

    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setUniformItemSizes(True)
        self._drag_active = False
        self._apply_border(False)

    @staticmethod
    def _dropped_paths(event) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls()
                if url.isLocalFile() and url.toLocalFile()]

    def _apply_border(self, active: bool) -> None:
        """拖拽悬停时换成主色虚线框，给用户一个「这里能松手」的反馈。"""
        token = theme.token
        border = (f"2px dashed {token('primary')}" if active
                  else f"1px dashed {token('border')}")
        background = token("primary_soft") if active else token("card_bg")
        self.setStyleSheet(
            f"QListWidget{{background:{background};border:{border};"
            f"border-radius:8px;padding:4px;}}"
            f"QListWidget::item{{padding:6px 8px;border-radius:4px;}}")

    def _set_drag_active(self, active: bool) -> None:
        if active != self._drag_active:
            self._drag_active = active
            self._apply_border(active)

    def dragEnterEvent(self, event):            # noqa: N802（Qt 命名）
        if self._dropped_paths(event):
            event.acceptProposedAction()
            self._set_drag_active(True)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):             # noqa: N802
        if self._dropped_paths(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):            # noqa: N802
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):                 # noqa: N802
        paths = self._dropped_paths(event)
        self._set_drag_active(False)
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        self.filesDropped.emit(paths)


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------
class LanTransferWidget(QWidget):
    """局域网文件传输主控件（独立窗口里跑）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = TransferSession()
        self.server = TransferServer(self.session)
        self._ip = ""
        self._url = ""
        self.setMinimumSize(900, 540)
        self._build_ui()
        self._reload_ips()
        self._sync_state()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        self.status = QLabel()
        self.status.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        root.addLayout(columns, 1)

        # ---- 左栏：拖拽区 + 文件列表 ----
        left = QVBoxLayout()
        left.setSpacing(8)
        self.list = FileDropList()
        self.list.setToolTip("把文件拖到这里，或点下面的「添加文件…」")
        self.list.filesDropped.connect(self.add_paths)
        self.list.itemSelectionChanged.connect(self._sync_state)
        left.addWidget(self.list, 1)
        self.hint = QLabel()
        self.hint.setStyleSheet(
            f"color:{theme.token('text_muted')};font-size:9pt;")
        self.hint.setWordWrap(True)
        left.addWidget(self.hint)
        pick_row = QHBoxLayout()
        pick_row.setSpacing(8)
        self.add_btn = QPushButton("添加文件…")
        set_variant(self.add_btn, "primary")
        self.add_btn.clicked.connect(self._on_pick_files)
        pick_row.addWidget(self.add_btn)
        self.remove_btn = QPushButton("移除选中")
        self.remove_btn.clicked.connect(self._on_remove_selected)
        pick_row.addWidget(self.remove_btn)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self._on_clear)
        pick_row.addWidget(self.clear_btn)
        pick_row.addStretch(1)
        left.addLayout(pick_row)
        columns.addLayout(left, 1)

        # ---- 右栏：二维码 + 地址 ----
        right = QVBoxLayout()
        right.setSpacing(8)
        self.qr_label = QLabel()
        self.qr_label.setFixedSize(QR_PIXELS + 16, QR_PIXELS + 16)
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet(
            "background:#ffffff;border:1px solid #d8dee4;border-radius:8px;")
        right.addWidget(self.qr_label, 0, Qt.AlignHCenter)
        self.qr_tip = QLabel("手机扫码打开下载页")
        self.qr_tip.setAlignment(Qt.AlignCenter)
        self.qr_tip.setStyleSheet(
            f"color:{theme.token('text_dim')};font-size:9pt;")
        right.addWidget(self.qr_tip)

        ip_row = QHBoxLayout()
        ip_row.setSpacing(6)
        ip_label = QLabel("访问地址")
        ip_label.setStyleSheet(f"color:{theme.token('text_dim')};font-size:9pt;")
        ip_row.addWidget(ip_label)
        self.ip_box = QComboBox()
        self.ip_box.setMinimumWidth(140)
        self.ip_box.currentIndexChanged.connect(lambda _=0: self._rebuild_url())
        ip_row.addWidget(self.ip_box)
        right.addLayout(ip_row)

        self.url_label = QLabel()
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setStyleSheet(
            f"color:{theme.token('primary')};font-size:9pt;")
        right.addWidget(self.url_label)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        self.copy_btn = QPushButton("复制链接")
        self.copy_btn.clicked.connect(self._on_copy)
        url_row.addWidget(self.copy_btn)
        self.open_btn = QPushButton("电脑上打开")
        self.open_btn.clicked.connect(self._on_open_browser)
        url_row.addWidget(self.open_btn)
        url_row.addStretch(1)
        right.addLayout(url_row)

        right.addStretch(1)
        self.toggle_btn = QPushButton("启动服务")
        set_variant(self.toggle_btn, "success")
        self.toggle_btn.clicked.connect(self._on_toggle)
        right.addWidget(self.toggle_btn)
        columns.addLayout(right, 0)

    # ---------- 地址 ----------
    def _reload_ips(self) -> None:
        """重新探测本机局域网地址并填进下拉框（保持当前选择）。"""
        candidates = local_ipv4_candidates()
        current = self._ip
        self.ip_box.blockSignals(True)
        self.ip_box.clear()
        self.ip_box.addItems(candidates)
        self.ip_box.blockSignals(False)
        if current in candidates:
            self.ip_box.setCurrentIndex(candidates.index(current))
        self._ip = self.ip_box.currentText()

    def _rebuild_url(self) -> None:
        self._ip = self.ip_box.currentText()
        self._url = (download_url(self._ip, self.server.port, self.session.token)
                     if self.server.running and self._ip else "")
        self._refresh_qr()

    def _refresh_qr(self) -> None:
        if not self._url:
            self.qr_label.clear()
            self.qr_label.setPixmap(QPixmap())      # 空图，保留白底边框
            self.url_label.setText("服务未启动")
            return
        pixmap = qr_pixmap(self._url)
        if pixmap is None:
            self.qr_label.clear()
            self.url_label.setText(f"{self._url}（未安装 segno，无法生成二维码）")
            return
        self.qr_label.setPixmap(pixmap)
        self.url_label.setText(self._url)

    # ---------- 列表 ----------
    def _reload_list(self, select_uid: str = "") -> None:
        self.list.clear()
        for item in self.session.files:
            row = QListWidgetItem(f"{item.name}　·　{human_size(item.size())}")
            row.setData(Qt.UserRole, item.uid)
            row.setToolTip(item.path)
            self.list.addItem(row)
            if item.uid == select_uid:
                row.setSelected(True)
        self._sync_state()

    def _sync_state(self) -> None:
        """按「有几个文件 / 服务在跑 / 选了哪些行」刷新状态行与按钮可用态。"""
        count = len(self.session.files)
        total = human_size(self.session.total_size())
        if self.server.running:
            self.status.setText(
                f"● 服务运行中 · 端口 {self.server.port} · 共 {count} 个文件"
                f"（{total}）· 手机需与本机在同一局域网")
            self.status.setStyleSheet(
                f"color:{theme.token('success')};font-size:9pt;")
            self.toggle_btn.setText("停止服务")
            set_variant(self.toggle_btn, "danger")
        else:
            self.status.setText(
                f"● 服务未启动 · 已备好 {count} 个文件（{total}）；"
                "添加文件会自动启动服务" if count else "● 服务未启动 · 还没有添加文件")
            self.status.setStyleSheet(
                f"color:{theme.token('text_dim')};font-size:9pt;")
            self.toggle_btn.setText("启动服务")
            set_variant(self.toggle_btn, "success")
        self.hint.setText(
            "把文件拖到这里，或点「添加文件…」（可多选）"
            if not count else
            "拖拽可继续添加；重复文件会被自动跳过。传输期间请勿移动或删除这些文件")
        selected = len(self.list.selectedItems())
        self.remove_btn.setEnabled(selected > 0)
        self.clear_btn.setEnabled(count > 0)
        has_url = bool(self._url)
        for btn in (self.copy_btn, self.open_btn):
            btn.setEnabled(has_url)
        self.add_btn.setEnabled(True)

    # ---------- 文件操作 ----------
    def add_paths(self, paths) -> list[SharedFile]:
        """加入文件（拖拽与手动选择共用），返回真正新增的那些。"""
        added, skipped = self.session.add_paths(paths)
        self._reload_list(select_uid=added[-1].uid if added else "")
        if skipped:
            QMessageBox.information(
                self, "局域网文件传输",
                "以下内容没有加入：\n\n" + "\n".join(f"· {s}" for s in skipped[:12])
                + ("\n…" if len(skipped) > 12 else ""))
        if added and not self.server.running:
            self._start_server()
        else:
            self._sync_state()
        return added

    def _on_pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要分享到手机的文件", "", "所有文件 (*)")
        if paths:
            self.add_paths(paths)

    def _on_remove_selected(self) -> None:
        for row in self.list.selectedItems():
            self.session.remove(str(row.data(Qt.UserRole) or ""))
        self._reload_list()

    def _on_clear(self) -> None:
        self.session.clear()
        self._reload_list()

    # ---------- 服务 ----------
    def _start_server(self) -> None:
        try:
            self.server.start()
        except OSError as err:
            QMessageBox.warning(self, "局域网文件传输",
                                f"服务启动失败：{err}\n\n"
                                "端口可能被安全软件拦截，稍后重试即可。")
            self._sync_state()
            return
        self._rebuild_url()
        self._sync_state()

    def _on_toggle(self) -> None:
        if self.server.running:
            self.server.stop()
            self._url = ""
            self._refresh_qr()
        else:
            self._start_server()
        self._sync_state()

    # ---------- 链接 ----------
    def _on_copy(self) -> None:
        if not self._url:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._url)
        self.url_label.setText(f"{self._url}　（已复制）")

    def _on_open_browser(self) -> None:
        if self._url:
            webbrowser.open(self._url)

    # ---------- 收尾 ----------
    def shutdown(self) -> None:
        """窗口关闭时由宿主调用（见 mini_window.MiniAppWindow.closeEvent）。

        会话是纯内存的，所以这里只要停掉服务即可；磁盘上没有任何残留物要清。
        """
        self.server.stop()


# 登记到「小工具」页（key 一旦发布不要再改）
register(MiniApp(
    key="lan_transfer",
    name="局域网文件传输",
    icon="📡",
    desc="拖入文件即生成二维码，手机扫码在同一局域网内直接下载",
    factory=LanTransferWidget,
))
