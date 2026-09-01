# -*- coding: utf-8 -*-
"""打包并发布到 GitHub（tkinter 桌面工具）。

用法：
    python publish_tool.py            # 源码运行
    打包后：打包并发布到github.exe

点击「开始发布」后自动执行：
  1. 用 PyInstaller 打包主程序（调 build.py，日志实时滚动）
  2. 确认 dist\\清风自动化键鼠工具.exe 已生成
  3. 创建 GitHub Release（tag 与显示名 = v{版本号}，仓库 qingfeng-keymouse-tool）
  4. 上传 exe 资产（资产名 QingFeng_KeyMouse_Tool.exe —— 用 ASCII 名绕开
     gh CLI 在 Windows 下把中文资产名改写成 default.exe 的问题）
  5. 同步 dist\\config.json 的 version 为本次发布版本（只改 version 字段，
     其余配置原样保留）
  6. 显示 Release 下载页链接

前置条件：
  - 已安装项目依赖与 PyInstaller（pip install -r requirements-dev.txt）
  - 已登录 gh（gh auth login），工具用 `gh auth token` 取凭证
  - 网络：默认走 http://127.0.0.1:7897 代理，可清空改为直连
"""
from __future__ import annotations

import glob
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext
import urllib.error
import urllib.parse
import urllib.request

REPO = "xuejia92/qingfeng-keymouse-tool"
API = f"https://api.github.com/repos/{REPO}"
UPLOAD = f"https://uploads.github.com/repos/{REPO}"
ASSET_NAME = "QingFeng_KeyMouse_Tool.exe"          # 上传到 Release 的资产名（ASCII）

# 源码运行时 BASE_DIR 是脚本所在目录；打包成 exe 后是 exe 所在目录
# （__file__ 在冻结环境指向临时解压目录，必须用 sys.executable 定位）
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_EXE = os.path.join(BASE_DIR, "dist", "清风自动化键鼠工具.exe")
_UA = "QingFeng-Publisher/1.0"


def find_python() -> str:
    """找一个装有 PyInstaller 与项目依赖的 Python。

    打包成 exe 后 sys.executable 指向工具自身，不能当 python 用，
    必须探测系统里的真实解释器（依赖装在 3.12，见 build.py）。
    """
    cands: list[str] = []
    if sys.executable.lower().endswith("python.exe"):
        cands.append(sys.executable)          # 源码运行时直接用当前解释器
    for tag in ("-3.12", "-3.11", "-3.13"):   # py launcher
        try:
            out = subprocess.run(
                ["py", tag, "-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=15)
            if out.returncode == 0 and out.stdout.strip():
                cands.append(out.stdout.strip())
        except OSError:
            pass
    local = os.environ.get("LOCALAPPDATA", "")
    if local:                                  # LOCALAPPDATA\\Programs\\Python\\Python3*
        cands.extend(sorted(
            glob.glob(os.path.join(local, "Programs", "Python",
                                   "Python3*", "python.exe")), reverse=True))
    check = ("import importlib.util as u,sys;"
             "sys.exit(0 if all(u.find_spec(m) for m in "
             "('PyInstaller','PySide6','cv2')) else 1)")
    seen: set[str] = set()
    for cand in cands:
        if cand in seen or not os.path.isfile(cand):
            continue
        seen.add(cand)
        try:
            r = subprocess.run([cand, "-c", check],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                return cand
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError("未找到装有 PyInstaller 与项目依赖的 Python。"
                       "请先执行 pip install -r requirements-dev.txt")


# ---------- GitHub API ----------

def _http_opener(proxy: str):
    if proxy.strip():
        handler = urllib.request.ProxyHandler(
            {"http": proxy.strip(), "https": proxy.strip()})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def gh_token() -> str:
    """从 gh CLI 取 GitHub 凭证（用户已 gh auth login）。"""
    out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                         text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError("未登录 GitHub：请先运行 gh auth login")
    return out.stdout.strip()


def _api_request(opener, url: str, token: str, method: str,
                 payload=None, data=None, timeout: float = 600.0):
    """请求 GitHub API；返回 (http_code, 解析后的响应)。

    payload：dict，自动 JSON 序列化 + application/json；
    data：原始字节（资产上传用），必须显式 application/octet-stream，
    否则 urllib 会默认加 application/x-www-form-urlencoded 被 GitHub 拒收。
    """
    body = json.dumps(payload).encode("utf-8") if payload is not None else data
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", _UA)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    elif data is not None:
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def create_release(opener, token: str, version: str) -> tuple[int, str, str]:
    """创建 Release；返回 (release_id, html_url, tag)。"""
    tag = version if version.startswith("v") else "v" + version
    code, data = _api_request(opener, f"{API}/releases", token, "POST", {
        "tag_name": tag,
        "name": tag,
        "body": f"清风自动化键鼠工具 {tag}\n\n发布工具自动创建，可在网页补充更新说明。",
    })
    if code == 201 and isinstance(data, dict) and data.get("id"):
        return data["id"], data["html_url"], tag
    if code == 422:
        raise RuntimeError(
            f"创建 Release 失败：tag {tag} 已存在。请换一个版本号，"
            "或先删除 GitHub 上同名的 tag/release。")
    raise RuntimeError(f"创建 Release 失败：HTTP {code} "
                       f"{data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)[:300]}")


def upload_asset(opener, token: str, release_id: int, exe_path: str, log) -> None:
    """上传 exe 资产到 Release。

    一次性读入字节作为 body：urllib 对 file-like body 没有 fileno 时会改用
    chunked 编码，而 GitHub 资产上传 API 只接受固定 Content-Length
    （否则报 Bad Content-Length）。145 MB 一次性读入内存无压力。
    """
    url = (f"{UPLOAD}/releases/{release_id}/assets"
           f"?name={urllib.parse.quote(ASSET_NAME)}")
    size = os.path.getsize(exe_path)
    log(f"   上传中（{size / 1048576:.0f} MB，约 1-2 分钟）…")
    with open(exe_path, "rb") as f:
        payload = f.read()
    code, data = _api_request(opener, url, token, "POST",
                              data=payload, timeout=1800.0)
    if code != 201:
        raise RuntimeError(f"上传资产失败：HTTP {code} "
                           f"{data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)[:300]}")
    log("   上传完成")


# ---------- 打包 ----------

def run_build(log) -> None:
    """调用 build.py（PyInstaller 打包主程序），逐行回传日志。"""
    build_script = os.path.join(BASE_DIR, "build.py")
    if not os.path.isfile(build_script):
        raise RuntimeError(
            f"未找到 {build_script}。\n"
            f"请把「打包并发布到github.exe」放在项目根目录"
            f"（含 build.py / main.py / dist 的目录）下运行。")
    py = find_python()
    log("> 开始打包主程序（PyInstaller，约 1 分钟）…")
    proc = subprocess.Popen(
        [py, build_script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=BASE_DIR,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"打包失败（build.py 返回 {code}）")
    if not os.path.isfile(LOCAL_EXE):
        raise RuntimeError(f"打包完成但未找到产物：{LOCAL_EXE}")


def sync_manifest_version(version: str, log) -> None:
    """发布成功后把 dist/config.json 的 version 同步为本次发布的版本。

    dist/config.json 是程序运行目录下的完整配置（首次运行自动生成，含邮箱、
    找图任务等），同时被在线更新用作最后兜底清单（Gitee raw 直读），所以
    发版后 version 必须跟上。只改 version 字段，其余配置原样保留；写带 v
    前缀的形式，与 Release 显示名及运行时 cfg.version 的写法一致。
    同步失败不影响发布结果（Release 已创建），只告警。
    """
    path = os.path.join(BASE_DIR, "dist", "config.json")
    ver = version if version[:1] in ("v", "V") else "v" + version
    if not os.path.isfile(path):
        log(f"    ⚠️ 未找到 {path}，跳过版本同步")
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("根节点不是 JSON 对象")
        data["version"] = ver
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except (OSError, ValueError) as e:
        log(f"    ⚠️ 同步 dist/config.json 失败（发布已成功，可手动改）：{e}")
        return
    log(f"    dist/config.json version -> {ver}")


# ---------- 发布流程 ----------

def publish_flow(version: str, proxy: str, log) -> dict:
    """完整发布流程；返回结果信息 dict。"""
    log("===== 1/5 打包主程序 =====")
    run_build(log)

    log("===== 2/5 获取 GitHub 凭证 =====")
    token = gh_token()
    log("    gh token 获取成功")

    log("===== 3/5 创建 Release =====")
    opener = _http_opener(proxy)
    release_id, html_url, tag = create_release(opener, token, version)
    log(f"    Release 已创建：{tag}")
    log(f"    {html_url}")

    log("===== 4/5 上传 exe 资产 =====")
    upload_asset(opener, token, release_id, LOCAL_EXE, log)
    log("    上传完成")

    log("===== 5/5 同步版本清单 =====")
    sync_manifest_version(version, log)

    download = (f"https://github.com/{REPO}/releases/download/"
                f"{urllib.parse.quote(tag)}/{ASSET_NAME}")
    return {"tag": tag, "html_url": html_url, "download": download}


# ---------- GUI ----------

class PublishApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.working = False

        root.title("打包并发布到 GitHub")
        root.geometry("720x560")
        root.minsize(560, 420)

        pad = {"padx": 10, "pady": 6}

        frm = tk.Frame(root)
        frm.pack(fill="x", **pad)

        tk.Label(frm, text="版本号：").grid(row=0, column=0, sticky="e")
        self.version_var = tk.StringVar(value="3.4.0")
        tk.Entry(frm, textvariable=self.version_var, width=16).grid(
            row=0, column=1, sticky="w", padx=(0, 18))
        tk.Label(frm, text="（如 3.1.0，自动加 v 前缀作为 tag）").grid(
            row=0, column=2, sticky="w")

        tk.Label(frm, text="代理：").grid(row=1, column=0, sticky="e")
        self.proxy_var = tk.StringVar(value="http://127.0.0.1:7897")
        tk.Entry(frm, textvariable=self.proxy_var, width=32).grid(
            row=1, column=1, sticky="w", padx=(0, 18))
        tk.Label(frm, text="（清空 = 直连，GitHub 需代理）").grid(
            row=1, column=2, sticky="w")

        self.btn = tk.Button(frm, text="开始发布", command=self.start,
                             width=12, bg="#1668a8", fg="white")
        self.btn.grid(row=2, column=1, sticky="w", pady=(8, 0))

        self.status = tk.Label(root, text="就绪", anchor="w")
        self.status.pack(fill="x", padx=10)

        self.log = scrolledtext.ScrolledText(root, height=16, state="disabled",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        root.after(100, self._poll)

    def _log_line(self, text: str) -> None:
        self.q.put(("log", text))

    def _set_status(self, text: str) -> None:
        self.q.put(("status", text))

    def _done(self, ok: bool, text: str) -> None:
        self.q.put(("done", (ok, text)))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "done":
                    ok, text = payload
                    self.working = False
                    self.btn.configure(state="normal")
                    self._set_status(text)
                    if ok:
                        self._log_line(f"\n✅ 发布成功：{text}")
                    else:
                        self._log_line(f"\n❌ {text}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def start(self) -> None:
        if self.working:
            return
        version = self.version_var.get().strip()
        if not version:
            self._log_line("请先填写版本号")
            return
        proxy = self.proxy_var.get().strip()
        self.working = True
        self.btn.configure(state="disabled")
        self._set_status("发布中…")
        self._log_line(f"版本号：{version}    代理：{proxy or '直连'}")
        threading.Thread(target=self._worker, args=(version, proxy),
                         daemon=True).start()

    def _worker(self, version: str, proxy: str) -> None:
        try:
            result = publish_flow(version, proxy, self._log_line)
            msg = (f"{result['tag']}\n"
                   f"Release 页：{result['html_url']}\n"
                   f"直接下载：{result['download']}")
            self._done(True, msg)
        except Exception as e:
            self._done(False, str(e))


def main() -> None:
    root = tk.Tk()
    PublishApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
