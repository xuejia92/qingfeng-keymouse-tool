# -*- coding: utf-8 -*-
"""「局域网文件传输」小程序（app/mini_apps/lan_transfer.py）的测试。

覆盖：
- 局域网地址探测（多网卡去重、过滤回环/公网、断网兜底）；
- 临时会话（内存白名单：去重、跳过目录/不存在的路径、增删清空）；
- 下载响应头（中文文件名的 RFC 5987 写法、控制字符清理）；
- **真实 socket 回环**跑通整个 HTTP 服务：下载页 HTML、文件字节、Content-Length、
  错误令牌/裸地址/未知 uid/目录穿越全部 404、停止后端口关闭；
- 二维码：**用 OpenCV 的 QRCodeDetector 把画出来的位图解码回 URL**（真正的回环验证），
  以及「纯黑白」这条硬要求；
- 界面：拖拽事件、自动启动服务、移除/清空、停止、关窗收尾。

本文件只在本机回环（127.0.0.1）上开临时端口；文件都在自己创建的临时目录里。
"""
from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import mini_apps  # noqa: E402
from app.mini_apps import lan_transfer as lt  # noqa: E402


# ---------------------------------------------------------------------------
# 局域网地址
# ---------------------------------------------------------------------------
class TestLanAddress(unittest.TestCase):

    def test_usable_filter(self):
        self.assertTrue(lt._is_usable_lan_ip("192.168.1.10"))
        self.assertTrue(lt._is_usable_lan_ip("10.0.0.5"))
        self.assertTrue(lt._is_usable_lan_ip("172.16.3.9"))
        self.assertTrue(lt._is_usable_lan_ip("169.254.1.2"))    # 链路本地
        self.assertFalse(lt._is_usable_lan_ip("127.0.0.1"))     # 回环
        self.assertFalse(lt._is_usable_lan_ip("8.8.8.8"))       # 公网
        self.assertFalse(lt._is_usable_lan_ip("::1"))           # IPv6
        self.assertFalse(lt._is_usable_lan_ip("not-an-ip"))

    def test_candidates_prefers_route_address_and_dedupes(self):
        """默认出口网卡排第一，重复地址只出现一次。"""
        fake_sock = mock.MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        fake_sock.getsockname.return_value = ("192.168.1.5", 54321)
        infos = [
            (2, 1, 6, "", ("192.168.1.5", 0)),      # 与出口地址重复
            (2, 1, 6, "", ("10.1.2.3", 0)),         # 另一块网卡
            (2, 1, 6, "", ("127.0.0.1", 0)),        # 回环要被过滤
        ]
        with mock.patch.object(lt.socket, "socket", return_value=fake_sock), \
             mock.patch.object(lt.socket, "getaddrinfo", return_value=infos):
            self.assertEqual(lt.local_ipv4_candidates(),
                             ["192.168.1.5", "10.1.2.3"])

    def test_candidates_fallback_when_offline(self):
        with mock.patch.object(lt.socket, "socket",
                               side_effect=OSError("没网")), \
             mock.patch.object(lt.socket, "getaddrinfo",
                               side_effect=OSError("没网")):
            self.assertEqual(lt.local_ipv4_candidates(), ["127.0.0.1"])

    def test_download_url(self):
        self.assertEqual(lt.download_url("192.168.1.5", 8765, "ab12cd"),
                         "http://192.168.1.5:8765/ab12cd/")


# ---------------------------------------------------------------------------
# 响应头 / 工具函数
# ---------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):

    def test_content_disposition_ascii(self):
        value = lt.content_disposition("report.pdf")
        self.assertIn('filename="report.pdf"', value)
        self.assertIn("filename*=UTF-8''report.pdf", value)
        self.assertTrue(value.startswith("attachment; "))

    def test_content_disposition_chinese(self):
        """中文名要给 RFC 5987 的 UTF-8 形式，否则手机上下载下来是乱码名。"""
        value = lt.content_disposition("测试 报告.pdf")
        self.assertIn("%E6%B5%8B%E8%AF%95", value)          # 「测试」的百分号编码
        self.assertIn("%20", value)                        # 空格
        self.assertIn("filename*=UTF-8''", value)
        self.assertIn('filename=".pdf"', value)            # ASCII 兜底不炸

    def test_content_disposition_is_header_safe(self):
        """换行必须清掉，否则可以伪造 HTTP 头（名里的普通文字保留不影响安全）。"""
        value = lt.content_disposition('evil\r\nX-Inject: 1.txt')
        self.assertNotIn("\r", value)
        self.assertNotIn("\n", value)
        self.assertEqual(value.count('"'), 2, "只应有 ASCII 兜底那一对引号")

    def test_content_disposition_strips_quotes(self):
        value = lt.content_disposition('a"b\\c.txt')
        self.assertEqual(value.count('"'), 2, value)
        self.assertNotIn("\\", value.split("filename*=UTF-8''")[0])

    def test_content_disposition_empty(self):
        self.assertIn('filename="download"', lt.content_disposition(""))
        self.assertIn('filename="download"', lt.content_disposition(None))

    def test_human_size(self):
        self.assertEqual(lt.human_size(0), "0 B")
        self.assertEqual(lt.human_size(512), "512 B")
        self.assertEqual(lt.human_size(2048), "2.0 KB")
        self.assertEqual(lt.human_size(5 * 1024 ** 2), "5.0 MB")


# ---------------------------------------------------------------------------
# 会话（内存白名单）
# ---------------------------------------------------------------------------
class TestSession(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qf_lan_test_")
        self.a = os.path.join(self.dir, "甲.txt")
        self.b = os.path.join(self.dir, "b.txt")
        for path, text in ((self.a, "hello 甲"), (self.b, "world")):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_token_is_random_and_url_safe(self):
        tokens = {lt.TransferSession().token for _ in range(20)}
        self.assertEqual(len(tokens), 20, "令牌必须每次会话都不同")
        for token in tokens:
            self.assertEqual(len(token), lt.TOKEN_LEN)
            self.assertTrue(token.isalnum(), token)

    def test_add_and_list(self):
        session = lt.TransferSession()
        added, skipped = session.add_paths([self.a, self.b])
        self.assertEqual(len(added), 2)
        self.assertEqual(skipped, [])
        self.assertEqual([f.name for f in session.files], ["甲.txt", "b.txt"])
        self.assertEqual(session.total_size(),
                         os.path.getsize(self.a) + os.path.getsize(self.b))
        self.assertTrue(all(f.exists() for f in session.files))

    def test_add_skips_duplicates_dirs_and_missing(self):
        session = lt.TransferSession()
        sub = os.path.join(self.dir, "sub")
        os.makedirs(sub, exist_ok=True)
        added, skipped = session.add_paths([self.a, self.a, sub,
                                            os.path.join(self.dir, "没有.txt"),
                                            ""])
        self.assertEqual(len(added), 1)
        self.assertEqual(len(skipped), 3)
        self.assertTrue(any("已在列表中" in s for s in skipped))
        self.assertTrue(any("文件夹" in s for s in skipped))
        self.assertTrue(any("不存在" in s for s in skipped))

    def test_duplicate_detection_is_path_normalized(self):
        """同一文件的不同写法（相对/大小写差异）也算重复。"""
        session = lt.TransferSession()
        session.add_paths([self.b])
        tricky = os.path.join(self.dir, ".", "..", os.path.basename(self.dir),
                              "b.txt")
        added, skipped = session.add_paths([tricky])
        self.assertEqual(added, [])
        self.assertTrue(skipped)

    def test_remove_clear_find(self):
        session = lt.TransferSession()
        added, _ = session.add_paths([self.a, self.b])
        self.assertIs(session.find(added[0].uid), added[0])
        self.assertIsNone(session.find("不存在"))
        self.assertTrue(session.remove(added[0].uid))
        self.assertFalse(session.remove(added[0].uid))       # 再删一次返回 False
        self.assertEqual(len(session.files), 1)
        session.clear()
        self.assertEqual(session.files, [])

    def test_moved_file_is_reported(self):
        session = lt.TransferSession()
        added, _ = session.add_paths([self.a])
        os.remove(self.a)
        self.assertFalse(added[0].exists())
        self.assertEqual(added[0].size(), 0)


# ---------------------------------------------------------------------------
# HTTP 服务（真实回环）
# ---------------------------------------------------------------------------
class TestServerRoundTrip(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qf_lan_http_")
        self.payload = ("局域网传输\x00" * 100).encode("utf-8")   # 含中文 + 二进制
        self.file = os.path.join(self.dir, "中文 & 文件.bin")
        with open(self.file, "wb") as handle:
            handle.write(self.payload)
        self.session = lt.TransferSession()
        self.session.add_paths([self.file])
        self.server = lt.TransferServer(self.session, host="127.0.0.1")
        self.port = self.server.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _get(self, path: str):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def _status(self, path: str) -> int:
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            code = err.code
            err.close()          # HTTPError 也是响应对象，不关会漏 ResourceWarning
            return code

    def test_server_starts_on_free_port(self):
        self.assertTrue(self.server.running)
        self.assertGreater(self.port, 0)

    def test_index_lists_file(self):
        with self._get(f"/{self.session.token}/") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            body = resp.read().decode("utf-8")
        self.assertIn("中文 &amp; 文件.bin", body, "文件名要 HTML 转义")
        self.assertIn(f"/{self.session.token}/f/{self.session.files[0].uid}", body)
        self.assertIn("下载", body)

    def test_download_returns_exact_bytes(self):
        uid = self.session.files[0].uid
        with self._get(f"/{self.session.token}/f/{uid}") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Length"],
                             str(len(self.payload)))
            disposition = resp.headers["Content-Disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn("filename*=UTF-8''", disposition)
            self.assertEqual(resp.read(), self.payload)

    def test_head_request_has_no_body(self):
        uid = self.session.files[0].uid
        request = urllib.request.Request(
            self.base + f"/{self.session.token}/f/{uid}", method="HEAD")
        with urllib.request.urlopen(request, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Length"],
                             str(len(self.payload)))
            self.assertEqual(resp.read(), b"")

    def test_bare_address_and_wrong_token_are_404(self):
        self.assertEqual(self._status("/"), 404)
        self.assertEqual(self._status("/wrongtoken/"), 404)
        self.assertEqual(self._status(f"/{self.session.token}"), 200)

    def test_unknown_and_traversal_uids_are_404(self):
        """uid 只能命中华名单里的对象——客户端给什么都当不了路径。"""
        self.assertEqual(self._status(f"/{self.session.token}/f/abcdef"), 404)
        for evil in ("../../etc/passwd", "..%2F..%2Fwindows%2Fwin.ini",
                     "%2e%2e%2f%2e%2e%2fsecret"):
            self.assertEqual(
                self._status(f"/{self.session.token}/f/{evil}"), 404, evil)

    def test_directory_is_never_served(self):
        """加入的是文件；即便有人拿目录路径当 uid 也不可能被列出来。"""
        session = lt.TransferSession()
        added, skipped = session.add_paths([self.dir])
        self.assertEqual(added, [])
        self.assertTrue(skipped)

    def test_removed_file_becomes_404_without_restart(self):
        uid = self.session.files[0].uid
        self.session.clear()
        self.assertEqual(self._status(f"/{self.session.token}/f/{uid}"), 404)

    def test_stop_closes_port_and_is_idempotent(self):
        self.server.stop()
        self.assertFalse(self.server.running)
        self.assertEqual(self.server.port, 0)
        self.server.stop()                      # 再停一次不能抛
        with self.assertRaises(OSError):
            urllib.request.urlopen(
                f"{self.base}/{self.session.token}/", timeout=2)


# ---------------------------------------------------------------------------
# 二维码
# ---------------------------------------------------------------------------
class _QtCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])


class TestQrCode(_QtCase):

    def _decode(self, pixmap) -> str:
        """把 QPixmap 编码成 PNG 字节后交给 OpenCV 解码（不落盘）。"""
        from PySide6.QtCore import QBuffer, QByteArray
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QBuffer.WriteOnly)
        pixmap.toImage().save(buffer, "PNG")
        array = np.frombuffer(bytes(payload.data()), dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        text, _points, _ = cv2.QRCodeDetector().detectAndDecode(image)
        return text

    def test_qr_is_black_and_white_only(self):
        """扫码靠对比度：不能跟主题换色，必须是纯黑白。"""
        pixmap = lt.qr_pixmap("http://192.168.1.5:8765/ab12cd/")
        self.assertIsNotNone(pixmap)
        image = pixmap.toImage()
        colors = {image.pixelColor(x, y).name()
                  for x in range(0, image.width(), 3)
                  for y in range(0, image.height(), 3)}
        self.assertTrue(colors <= {"#ffffff", "#000000"}, colors)

    def test_qr_has_quiet_zone(self):
        """四周静默区必须留白，否则贴边的二维码扫不出来。"""
        pixmap = lt.qr_pixmap("http://192.168.1.5:8765/ab12cd/", pixels=200)
        image = pixmap.toImage()
        self.assertEqual(image.pixelColor(0, 0).name(), "#ffffff")
        self.assertEqual(image.pixelColor(image.width() - 1, 0).name(), "#ffffff")

    def test_qr_round_trip_decodes_back_to_url(self):
        """★ 真正的回环验证：画出来的二维码能被解码器读回同一个 URL。"""
        if not hasattr(cv2, "QRCodeDetector"):
            self.skipTest("当前 OpenCV 没有二维码解码器")
        for url in ("http://192.168.1.5:8765/ab12cd/",
                    "http://10.0.0.9:1024/x1y2z3/",
                    "http://192.168.31.150:54321/A9bC3d/"):
            decoded = self._decode(lt.qr_pixmap(url, pixels=320))
            self.assertEqual(decoded, url)

    def test_empty_url_returns_none(self):
        self.assertIsNone(lt.qr_pixmap(""))


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
class TestDropList(_QtCase):

    def _make_mime(self, urls=None, keep=True):
        """造一个 QMimeData。

        ⚠️ QDropEvent **不持有** QMimeData 的所有权，mime 一旦被回收，
        `event.mimeData()` 会返回悬垂指针（PySide6 会退化成一个裸 QObject，
        报 "no attribute hasUrls"）。所以挂到 self 上保活到用例结束。
        """
        from PySide6.QtCore import QMimeData, QUrl

        mime = QMimeData()
        if urls:
            mime.setUrls([QUrl.fromLocalFile(p) for p in urls])
        if keep:
            self._mime = mime
        return mime

    def _drop_event(self, mime):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QDropEvent

        return QDropEvent(QPointF(5, 5), Qt.CopyAction, mime,
                          Qt.LeftButton, Qt.NoModifier)

    def test_drop_emits_file_paths(self):
        widget = lt.FileDropList()
        seen = []
        widget.filesDropped.connect(seen.append)
        widget.dropEvent(self._drop_event(
            self._make_mime([r"C:\a\1.txt", r"C:\a\2.txt"])))
        # QUrl 会把分隔符统一成 "/"，比较时按平台规范化
        self.assertEqual([os.path.normpath(p) for p in seen[0]],
                         [os.path.normpath(r"C:\a\1.txt"),
                          os.path.normpath(r"C:\a\2.txt")])

    def test_drop_ignores_non_local_urls(self):
        """http:// 之类的网络地址不是本地文件，不能当路径处理。"""
        from PySide6.QtCore import QMimeData, QUrl

        mime = QMimeData()
        mime.setUrls([QUrl("http://example.com/a.txt"),
                      QUrl.fromLocalFile(r"C:\a\b.txt")])
        self._mime = mime
        widget = lt.FileDropList()
        seen = []
        widget.filesDropped.connect(seen.append)
        widget.dropEvent(self._drop_event(mime))
        self.assertEqual([os.path.normpath(p) for p in seen[0]],
                         [os.path.normpath(r"C:\a\b.txt")])

    def test_drop_without_urls_does_not_emit(self):
        widget = lt.FileDropList()
        seen = []
        widget.filesDropped.connect(seen.append)
        widget.dropEvent(self._drop_event(self._make_mime()))
        self.assertEqual(seen, [])

    def test_border_switches_on_drag_state(self):
        """拖拽悬停要有可见反馈（颜色用主题令牌，能跟着换主题）。"""
        widget = lt.FileDropList()
        normal = widget.styleSheet()
        widget._set_drag_active(True)
        active = widget.styleSheet()
        self.assertNotEqual(normal, active)
        widget._set_drag_active(False)
        self.assertEqual(widget.styleSheet(), normal)


class TestWidget(_QtCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qf_lan_ui_")
        self.file = os.path.join(self.dir, "报告.txt")
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write("内容")
        self.widget = lt.LanTransferWidget()

    def tearDown(self):
        self.widget.shutdown()
        self.widget.deleteLater()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_initial_state(self):
        """初始态：没有文件、服务没起、动作按钮该灰的都灰掉。"""
        self.assertIn("服务未启动", self.widget.status.text())
        self.assertIn("把文件拖到这里", self.widget.hint.text())
        self.assertEqual(self.widget.list.count(), 0)
        self.assertFalse(self.widget.remove_btn.isEnabled())
        self.assertFalse(self.widget.clear_btn.isEnabled())
        self.assertFalse(self.widget.copy_btn.isEnabled())
        self.assertFalse(self.widget.open_btn.isEnabled())

    def test_drop_starts_server_and_shows_qr(self):
        with mock.patch.object(lt.QMessageBox, "information"):
            added = self.widget.add_paths([self.file])
        self.assertEqual(len(added), 1)
        self.assertTrue(self.widget.server.running, "添加文件应自动启动服务")
        self.assertIn("服务运行中", self.widget.status.text())
        self.assertTrue(self.widget._url.startswith("http://"))
        self.assertFalse(self.widget.qr_label.pixmap().isNull())
        self.assertEqual(self.widget.list.count(), 1)
        self.assertIn("报告.txt", self.widget.list.item(0).text())

    def test_duplicate_add_reports_and_skips(self):
        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        with mock.patch.object(lt.QMessageBox, "information") as info:
            added = self.widget.add_paths([self.file])
        self.assertEqual(added, [])
        self.assertTrue(info.called)
        self.assertEqual(self.widget.list.count(), 1)

    def test_directory_add_is_rejected(self):
        with mock.patch.object(lt.QMessageBox, "information") as info:
            added = self.widget.add_paths([self.dir])
        self.assertEqual(added, [])
        self.assertTrue(info.called)
        self.assertFalse(self.widget.server.running, "什么都没加就别起服务")

    def test_remove_and_clear(self):
        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        self.widget.list.item(0).setSelected(True)
        self.widget._sync_state()
        self.assertTrue(self.widget.remove_btn.isEnabled())
        self.widget._on_remove_selected()
        self.assertEqual(self.widget.list.count(), 0)
        self.assertFalse(self.widget.remove_btn.isEnabled())

        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        self.widget._on_clear()
        self.assertEqual(self.widget.list.count(), 0)

    def test_toggle_stops_and_clears_url(self):
        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        self.widget._on_toggle()                     # 停
        self.assertFalse(self.widget.server.running)
        self.assertEqual(self.widget._url, "")
        self.assertIn("服务未启动", self.widget.status.text())
        self.assertFalse(self.widget.copy_btn.isEnabled())
        self.widget._on_toggle()                     # 再起
        self.assertTrue(self.widget.server.running)
        self.assertTrue(self.widget.copy_btn.isEnabled())

    def test_ip_change_rebuilds_url(self):
        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        first = self.widget._url
        self.widget.ip_box.addItem("10.9.9.9")       # 模拟多网卡
        self.widget.ip_box.setCurrentText("10.9.9.9")
        self.assertIn("10.9.9.9", self.widget._url)
        self.assertNotEqual(first, self.widget._url)

    def test_clear_keeps_server_running(self):
        """清空文件不该顺手把服务停掉（用户可能只是想换一批文件）。"""
        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        self.widget._on_clear()
        self.assertTrue(self.widget.server.running)

    def test_shutdown_stops_server(self):
        with mock.patch.object(lt.QMessageBox, "information"):
            self.widget.add_paths([self.file])
        self.widget.shutdown()
        self.assertFalse(self.widget.server.running)
        self.widget.shutdown()                       # 幂等


class TestWindowShutdownHook(_QtCase):
    """宿主窗口关窗时要调小程序的 shutdown()（否则 HTTP 服务会留在后台）。"""

    def test_release_content_calls_shutdown(self):
        from app.ui import mini_window

        calls = []

        class _Content:
            def shutdown(self):
                calls.append(1)

        mini_window._release_content(_Content())
        self.assertEqual(calls, [1])

    def test_release_content_ignores_widgets_without_hook(self):
        from PySide6.QtWidgets import QLabel

        from app.ui import mini_window
        mini_window._release_content(QLabel())       # 不该抛

    def test_release_content_swallows_errors(self):
        from app.ui import mini_window

        class _Bad:
            def shutdown(self):
                raise RuntimeError("boom")

        with mock.patch("app.ui.mini_window.logging"):  # 别把 traceback 打到测试输出里
            mini_window._release_content(_Bad())        # 收尾失败不能挡住关窗

    def test_close_event_calls_release(self):
        from app.ui import mini_window
        source = inspect.getsource(mini_window.MiniAppWindow.closeEvent)
        self.assertIn("_release_content", source)

    def test_closing_real_window_triggers_hook(self):
        from app.ui import mini_window

        calls = []

        class _Content(lt.QWidget):
            def shutdown(self):
                calls.append(1)

        with mock.patch.object(lt.QMessageBox, "information"):
            win = mini_window.open_mini_app(mini_apps.MiniApp(
                key="hook-demo", name="钩子演示", icon="🧪", desc="",
                factory=_Content))
        win.close()
        self.assertEqual(calls, [1])


# ---------------------------------------------------------------------------
# 打包 / 依赖契约
# ---------------------------------------------------------------------------
class TestPackagingContracts(unittest.TestCase):

    def test_segno_is_declared_in_requirements(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("segno", text, "新增依赖必须写进 requirements.txt")

    def test_segno_imported_at_module_level(self):
        """静态 import 才能被 PyInstaller 静态分析扫到（动态导入要配 hidden import）。"""
        source = inspect.getsource(lt)
        self.assertIn("import segno", source)
        self.assertNotIn("importlib", source)
        self.assertIn("except ImportError", source, "缺依赖要降级而不是崩")

    def test_server_only_serves_whitelisted_uid(self):
        """处理器必须用 uid 反查白名单，绝不能把请求路径当成文件路径。"""
        source = inspect.getsource(lt._TransferHandler._send_file)
        self.assertIn("session.find(uid)", source)
        self.assertNotIn("os.path.join", source)

    def test_no_disk_copy_of_shared_files(self):
        """「临时存储」= 内存白名单：加入文件不复制、不落盘。"""
        source = inspect.getsource(lt.TransferSession.add_paths)
        for forbidden in ("shutil.copy", "copy2", "mkdtemp", "tempfile"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_registered_as_mini_app(self):
        app = mini_apps.find_app("lan_transfer")
        self.assertIsNotNone(app)
        self.assertEqual(app.name, "局域网文件传输")
        self.assertTrue(app.icon and app.desc)


if __name__ == "__main__":
    unittest.main()
