"""在线更新：版本比对、下载新 exe、运行中替换程序。

- 更新源优先级：GitHub Releases（主渠道，2026-09 起）-> Gitee Releases（旧渠道兜底）
  -> Gitee 仓库 dist/config.json 版本清单（最后兜底）
- 版本号取 Release 的显示名（如 v3.0.5），缺失时用 tag_name；两者都是发布时的
  tag 原文，更新后本地 config.json 的 version 直接写这个原文
- 下载地址：优先取 Release 附件直链（browser_download_url，GitHub 资产名可能是
  ASCII，按「任意 .exe」兜底匹配）；GitHub 兜底失败时回退 Gitee 附件拼地址或
  清单里的 download_url 自定义直链
- 注意：Gitee raw 链接对大文件要求登录，匿名 403，不能用 raw 地址分发 exe
- 替换方式（PyInstaller onefile 适配）：下载的 exe 暂存为 exe.new，写入升级收尾
  bat 并分离运行 —— bat 轮询等待当前进程完全退出后：删除旧 exe、新 exe 顶替、
  重新打开最新程序、自删。不做原地替换、不立即启动新进程，避免新旧 onefile
  进程竞争 _MEI 临时目录触发 bootloader 弹窗；下次启动的 cleanup_old_exe()
  兜底清理历史残留的 *.old

发布新版本流程：在 GitHub 建 Release（显示名带版本号如 v3.0.5），上传新 exe 附件。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from .autostart import current_exe_path

logger = logging.getLogger(__name__)

# GitHub Releases 为主渠道；Gitee 保留作兜底（国内直连稳定，旧版本仍可发现）
GH_API = "https://api.github.com/repos/xuejia92/qingfeng-keymouse-tool"
GITEE_RAW = "https://gitee.com/dusy110/qingfengzidonghuajianshu/raw/master/dist/"
GITEE_RELEASE = "https://gitee.com/dusy110/qingfengzidonghuajianshu/releases/download/"
GITEE_API = "https://gitee.com/api/v5/repos/dusy110/qingfengzidonghuajianshu"
EXE_FILENAME = "清风自动化键鼠工具.exe"
UPDATE_MANIFEST_URL = GITEE_RAW + "config.json"
_UA = "QingFengAutoUpdate/1.0"


def _clean_version(v: str) -> str:
    """去掉版本号开头的 v/V 前缀与空白（发布 tag 常写成 v3.0.1）。"""
    return re.sub(r"^[vV]", "", str(v or "").strip())


def compare_versions(local: str, remote: str) -> int:
    """比较版本号：local<remote 返回 -1，相等 0，大于返回 1；解析不了的段按 0。

    兼容 v/V 前缀（"v3.0.1" 与 "3.0.1" 视为同一版本）。
    """
    def parts(v: str) -> list[int]:
        out = []
        for seg in re.split(r"[.\-+_]", _clean_version(v)):
            m = re.match(r"\d+", seg)
            out.append(int(m.group()) if m else 0)
        return out or [0]

    pa, pb = parts(local), parts(remote)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


def fetch_latest_release(timeout: float = 15.0, api: str = GH_API) -> dict | None:
    """「最新发行版」API（GitHub/Gitee 共用）：name/tag_name 即版本，assets 含附件直链。"""
    try:
        req = urllib.request.Request(api + "/releases/latest",
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and (data.get("tag_name") or data.get("name")):
            return data
        return None
    except Exception:
        logger.debug("获取最新发行版失败：%s", api, exc_info=True)
        return None


def fetch_manifest(timeout: float = 15.0) -> dict | None:
    """拉取远端版本清单（dist/config.json 整个 JSON）；任何失败返回 None。"""
    try:
        req = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.debug("检查更新失败：%s", UPDATE_MANIFEST_URL, exc_info=True)
        return None


def manifest_version(manifest: dict | None) -> str | None:
    """取清单里的 version 字段；缺失/为空返回 None（远端还没写版本号）。"""
    if not manifest:
        return None
    version = str(manifest.get("version") or "").strip()
    return version or None


def _tag_download_urls(version: str) -> list[str]:
    """按版本号拼 Gitee Release 附件候选地址（v{版本} 与 {版本} 两种 tag 写法）。"""
    ver = _clean_version(version)
    enc = urllib.parse.quote(EXE_FILENAME)
    return [GITEE_RELEASE + "v" + ver + "/" + enc,
            GITEE_RELEASE + ver + "/" + enc]


def _release_version(rel: dict | None) -> str:
    """取 Release 版本号：显示名 name 优先（GitHub 的 tag 可能是中文，如「键鼠自动化」），
    tag_name 兜底；都没有返回空串。"""
    for key in ("name", "tag_name"):
        v = str((rel or {}).get(key) or "").strip()
        if v:
            return v
    return ""


def _asset_download_url(assets) -> str | None:
    """挑 exe 附件的下载直链：精确匹配 EXE_FILENAME 优先（Gitee 附件名固定），
    其次任意 .exe（GitHub 资产名可能是 ASCII，如 QingFeng_KeyMouse_Tool.exe）。"""
    if not assets:
        return None
    for a in assets:
        if isinstance(a, dict) and str(a.get("name") or "") == EXE_FILENAME:
            return str(a.get("browser_download_url") or "") or None
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if name.lower().endswith(".exe") and url \
                and not name.endswith((".download", ".old")):
            return url
    return None


def resolve_download_urls(manifest: dict | None, version: str) -> list[str]:
    """下载候选地址列表：清单 download_url 直链优先；否则 Gitee Release 附件候选。"""
    custom = str((manifest or {}).get("download_url") or "").strip()
    if custom:
        return [custom]
    return _tag_download_urls(version)


def resolve_update_sources() -> tuple[str | None, list[str]]:
    """确定待更新版本号与下载候选地址。

    优先级：GitHub 最新发行版（release name/tag 即版本，附件直链）->
    Gitee 最新发行版（旧渠道兜底）-> 远端 dist/config.json 清单（version + download_url）。
    返回 (版本号或 None, 候选地址列表)。
    """
    for api in (GH_API, GITEE_API):
        rel = fetch_latest_release(api=api)
        ver = _release_version(rel)
        if not ver:
            continue
        urls = []
        asset_url = _asset_download_url(rel.get("assets"))
        if asset_url:
            urls.append(asset_url)
        if api == GITEE_API:
            # GitHub 资产名/中文 tag 无法猜测拼 URL，只信附件直链；Gitee 可拼
            urls.extend(_tag_download_urls(ver))
        if urls:
            return ver, urls
    manifest = fetch_manifest()
    ver = manifest_version(manifest)
    if ver:
        return ver, resolve_download_urls(manifest, ver)
    return None, []


def update_download_dest() -> str:
    """下载临时文件位置：放在当前 exe 同目录（替换需要同目录改名）。"""
    exe = current_exe_path()
    if exe:
        return exe + ".download"
    return os.path.join(tempfile.gettempdir(), "qingfeng_update.exe")


def _remove(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _is_pe_file(path: str) -> bool:
    """确认文件是 Windows 可执行文件（MZ 头），避免把错误页存成 exe。"""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def download_update(url: str, dest: str, progress_cb=None, stop_event=None,
                    timeout: float = 30.0) -> tuple[bool, str]:
    """流式下载新程序 url 到 dest，progress_cb(done_bytes, total_bytes)（total 可能为 0）。

    返回 (是否成功, 失败原因)；取消或失败都会清理半成品文件。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        f.close()
                        _remove(dest)
                        return False, "已取消"
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass
        if not _is_pe_file(dest):
            _remove(dest)
            return False, "下载内容不是有效的 Windows 程序"
        return True, ""
    except Exception as e:
        _remove(dest)
        return False, str(e)


def _write_upgrade_bat(exe: str, new: str) -> str:
    """写升级收尾 bat（GBK 编码，兼容中文路径），返回 bat 路径；失败返回 ""。

    逻辑：轮询等待当前 exe 进程完全退出 -> 删除旧 exe -> 新 exe(.new) 顶替 ->
    启动新程序 -> 删除自身。由独立 cmd 进程执行：不碰运行中的 exe、不依赖主程序。
    """
    exe_name = os.path.basename(exe)
    bat = exe + ".upgrade.bat"
    lines = [
        "@echo off",
        "rem 清风自动化键鼠工具升级收尾脚本（由 updater 生成，可安全删除）",
        ":wait",
        f'tasklist /FI "IMAGENAME eq {exe_name}" 2>nul | find /i "{exe_name}" >nul',
        "if not errorlevel 1 (",
        "    timeout /t 1 /nobreak >nul",
        "    goto wait",
        ")",
        f'del /f /q "{exe}" 2>nul',
        f'move /y "{new}" "{exe}" >nul 2>&1',
        f'start "" "{exe}"',
        'del /f /q "%~f0" 2>nul',
    ]
    try:
        with open(bat, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        return bat
    except OSError:
        return ""


def install_update(downloaded: str) -> tuple[bool, str]:
    """用下载好的新程序替换当前 exe，返回 (是否成功, 原因)。

    PyInstaller onefile 适配：运行中的 bootloader 锁定 exe 文件，不能原地
    替换；也不能替换后立即启动新 exe（新旧两个 onefile 进程并存会互相竞争
    _MEI 临时目录、被安全软件扫描锁定，触发 "Failed to start embedded
    python interpreter" 和 _MEI 目录清理失败两个弹窗）。

    改为：新 exe 暂存为 exe.new，写入收尾 bat 并分离运行 —— bat 等当前
    进程完全退出后：删除旧 exe -> 新 exe 顶替 -> 启动新程序 -> 自删。
    调用方随后退出程序即可。
    """
    exe = current_exe_path()
    if not exe or not os.path.isfile(exe):
        return False, "未找到当前程序 exe，无法自动替换"
    if not os.path.isfile(downloaded):
        return False, "下载文件不存在"
    if os.path.abspath(downloaded).lower() == os.path.abspath(exe).lower():
        return False, "下载文件与当前程序是同一个文件"
    if not _is_pe_file(downloaded):
        return False, "下载的文件不是有效的 Windows 程序"
    new = exe + ".new"
    try:
        _remove(new)
        os.replace(downloaded, new)          # 暂存，不动运行中的 exe
    except OSError as e:
        return False, f"暂存更新文件失败：{e}"
    bat = _write_upgrade_bat(exe, new)
    if not bat:
        _remove(new)
        return False, "写入升级脚本失败"
    try:
        subprocess.Popen(
            ["cmd", "/c", bat],
            cwd=os.path.dirname(exe),
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           | getattr(subprocess, "DETACHED_PROCESS", 0)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except OSError as e:
        _remove(new)
        return False, f"启动升级脚本失败：{e}"
    return True, ""


def cleanup_old_exe() -> None:
    """启动时清理上次更新残留的 *.old（旧进程退出后才能删掉，删不掉静默跳过）。"""
    exe = current_exe_path()
    if exe:
        _remove(exe + ".old")
