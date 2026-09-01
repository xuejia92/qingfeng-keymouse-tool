# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本：生成单文件 dist\\清风自动化键鼠工具.exe

用法（双击 build.bat 等价于无参数调用）：
    python build.py              打包（onefile 单文件）
    python build.py --dir        onedir 目录模式，启动更快，适合改完代码快速验证
    python build.py --console    保留控制台窗口（排查启动崩溃用）
    python build.py --clean      清空 PyInstaller 缓存后全量重打

实测（20 核 / PyInstaller 6.15.0 / Python 3.12.10）：
- 打包约 54 秒，产物约 105 MB 单文件（含 OCR 模型）。
- 增量构建几乎不省时间：大头是把 100 多 MB 内容压缩成单文件。

为什么用 PyInstaller（2026-09 起，从 Nuitka 切回）：
1. **构建快**：约 54 秒 vs Nuitka 的 7~25 分钟，且不需要 C 编译器（MinGW）。
2. 代价：代码是字节码可被反编译；体积 ~105 MB（Nuitka 约 101 MB），差距不大。
3. 若以后需要抗反编译，再切回 Nuitka 或对关键模块 Cython 化。

关键处理（都写在下面）：
- 动态导入：pynput.keyboard._win32 / pynput.mouse._win32 / mss.windows
  按 sys.platform 拼模块名，静态扫描不到，用 --hidden-import 显式声明
- onnxruntime：__init__.py 在 cpuinfo+py3nvml 存在时会链式 import
  transformers/tensorflow/keras 巨型库，用 --exclude-module 全部排除
- 数据文件：DrissionPage 的 configs.ini/suffixes.dat、RapidOCR 的 config.yaml +
  三个 .onnx 模型、assets 图标目录，用 --add-data 显式收集（.py 之外的
  文件 PyInstaller 不会自动带上）
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

# 打包期必须能 import 到的模块（缺任何一个都说明这个解释器没装项目依赖）
REQUIRED_MODULES = ("PySide6", "cv2", "keyboard", "pynput", "mss", "PIL",
                    "DrissionPage", "rapidocr_onnxruntime", "onnxruntime")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "清风自动化键鼠工具"
DIST_DIR = os.path.join(BASE_DIR, "dist")
WORK_DIR = os.path.join(BASE_DIR, "build_pyinstaller")
EXE_NAME = f"{APP_NAME}.exe"

# 按 sys.platform 拼模块名做动态导入的平台后端，静态分析扫不到
HIDDEN_MODULES = [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "mss.windows",
]

# onnxruntime 的条件导入链（cpuinfo+py3nvml 存在时 import transformers 进而
# 拉入 tensorflow/keras），排除避免体积暴涨
EXCLUDED_MODULES = [
    "transformers", "torch", "tensorflow", "keras",
    "cpuinfo", "py3nvml",
]

# DrissionPage 随包分发的非 .py 数据文件：包内子目录 -> 文件名
DRISSION_DATA_FILES = (
    ("_configs", "configs.ini"),
    ("_functions", "suffixes.dat"),
)

# RapidOCR 的模型与配置（运行时按「包目录」拼路径读取）
RAPIDOCR_DATA_FILES = (
    "config.yaml",
    os.path.join("models", "ch_PP-OCRv4_det_infer.onnx"),
    os.path.join("models", "ch_PP-OCRv4_rec_infer.onnx"),
    os.path.join("models", "ch_ppocr_mobile_v2.0_cls_infer.onnx"),
)


def _fmt(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} 秒"
    return f"{s // 60} 分 {s % 60:02d} 秒"


def _has_deps(executable: str) -> bool:
    """该解释器能否 import 到全部打包依赖。"""
    code = ("import importlib.util as u, sys; "
            f"sys.exit(0 if all(u.find_spec(m) for m in {REQUIRED_MODULES!r}) else 1)")
    try:
        return subprocess.run([executable, "-c", code],
                              capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _candidate_interpreters() -> list:
    """按可能性排序的其他 Python 解释器路径。"""
    cands = []
    py = shutil.which("py")
    if py:
        for tag in ("-3.12", "-3.11", "-3.13", "-3.10"):
            try:
                out = subprocess.run(
                    [py, tag, "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, timeout=60).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if out:
                cands.append(out)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.extend(sorted(
            glob.glob(os.path.join(local, "Programs", "Python",
                                   "Python3*", "python.exe")), reverse=True))
    return cands


def ensure_interpreter() -> None:
    """当前解释器缺依赖时自动切到装了依赖的那个 Python 重跑。

    这台机器上装了多个 Python，项目依赖只装在 3.12 上，而 PATH 上排在前面的
    可能是别的版本；双击 build.bat 时用的就是 PATH 上的第一个。
    """
    if _has_deps(sys.executable):
        return
    print(f"[环境] 当前解释器缺少项目依赖：{sys.executable}")
    print("       正在查找装了依赖的 Python…")
    seen = {os.path.abspath(sys.executable)}
    for cand in _candidate_interpreters():
        path = os.path.abspath(cand)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        if _has_deps(path):
            print(f"[环境] 切换到 {path}")
            script = os.path.abspath(sys.argv[0])
            sys.exit(subprocess.call([path, script] + sys.argv[1:]))
    print("\n[错误] 没找到装有项目依赖的 Python。请先执行：")
    print("       pip install -r requirements-dev.txt")
    sys.exit(1)


def _is_running(exe_name: str) -> bool:
    """exe 正在运行时无法被覆盖，构建前先查一次。"""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
                             capture_output=True, text=True, timeout=15).stdout
        return exe_name.lower() in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def _add_data_args() -> list:
    """--add-data 参数（"源路径" + os.pathsep + "包内目标目录"）。

    assets 目录整包；DrissionPage / RapidOCR 的数据文件从 site-packages
    里按实际安装路径收集。
    """
    sep = os.pathsep
    args = [os.path.join(BASE_DIR, "assets") + sep + "assets"]

    try:
        import DrissionPage
    except ImportError:
        print("[警告] 没找到 DrissionPage，跳过它的数据文件（网页步骤将不可用）")
    else:
        pkg = os.path.dirname(os.path.abspath(DrissionPage.__file__))
        for sub, name in DRISSION_DATA_FILES:
            src = os.path.join(pkg, sub, name)
            if os.path.isfile(src):
                args.append(src + sep + f"DrissionPage/{sub}")
            else:
                print(f"[警告] 缺少 DrissionPage 数据文件：{src}")

    try:
        import rapidocr_onnxruntime
    except ImportError:
        print("[警告] 没找到 rapidocr_onnxruntime，跳过它的模型（文字识别将不可用）")
    else:
        pkg = os.path.dirname(os.path.abspath(rapidocr_onnxruntime.__file__))
        for rel in RAPIDOCR_DATA_FILES:
            src = os.path.join(pkg, rel)
            if os.path.isfile(src):
                target = os.path.dirname(f"rapidocr_onnxruntime/{rel}")
                args.append(src + sep + target)
            else:
                print(f"[警告] 缺少 RapidOCR 数据文件：{src}")

    return args


def build_cmd(args) -> list:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if not args.dir else "--onedir",
        "--noconsole" if not args.console else "--console",
        "--name", APP_NAME,
        "--icon", os.path.join(BASE_DIR, "assets", "icon.ico"),
        "--distpath", DIST_DIR,
        "--workpath", WORK_DIR,
        "--specpath", WORK_DIR,
        *([] if not args.clean else ["--clean"]),
        *[f"--hidden-import={m}" for m in HIDDEN_MODULES],
        *[f"--exclude-module={m}" for m in EXCLUDED_MODULES],
        *[f"--add-data={d}" for d in _add_data_args()],
        os.path.join(BASE_DIR, "main.py"),
    ]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="PyInstaller 打包")
    ap.add_argument("--dir", action="store_true",
                    help="onedir 目录模式，启动更快，适合改完代码快速验证")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口（排查启动崩溃用）")
    ap.add_argument("--clean", action="store_true",
                    help="清空 PyInstaller 缓存后全量重打")
    args = ap.parse_args()

    ensure_interpreter()          # 必须在 os.chdir 之前：sys.argv[0] 可能是相对路径
    os.chdir(BASE_DIR)

    if _is_running(EXE_NAME):
        print(f"[错误] {EXE_NAME} 正在运行，exe 被占用会导致打包失败。")
        print("       请从托盘图标右键 -> 退出，然后重新构建。")
        return 1

    if args.clean:
        print("[清理] 删除 PyInstaller 工作目录与旧产物")
        for p in (WORK_DIR,):
            try:
                shutil.rmtree(p, ignore_errors=True)
            except OSError as e:
                print(f"[警告] 清理失败（可手动删除后重试）：{e}")
        os.makedirs(WORK_DIR, exist_ok=True)

    print(f"[模式] {'onedir 目录' if args.dir else 'onefile 单文件'} · "
          f"{'带控制台' if args.console else '无控制台'} · "
          f"{'全量' if args.clean else '复用缓存'}")

    cmd = build_cmd(args)
    print("[执行]", " ".join(cmd), flush=True)

    t0 = time.monotonic()
    code = subprocess.call(cmd)
    elapsed = time.monotonic() - t0

    if code != 0:
        print(f"\n[失败] PyInstaller 返回 {code}，耗时 {_fmt(elapsed)}")
        print("       若提示找不到 PyInstaller，请先执行: pip install pyinstaller")
        return code

    exe = os.path.join(DIST_DIR, EXE_NAME)
    if not os.path.isfile(exe):
        print(f"\n[失败] 未找到产物: {exe}")
        return 1

    size_mb = os.path.getsize(exe) / 1048576
    print(f"\n[完成] 耗时 {_fmt(elapsed)}")
    print(f"       {os.path.relpath(exe, BASE_DIR)}（{size_mb:.1f} MB）")
    print("       说明：config.json / templates\\ / flows\\ / app.log 会在 exe "
          "同级目录自动生成；assets 图标已内嵌，无需随 exe 分发。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
