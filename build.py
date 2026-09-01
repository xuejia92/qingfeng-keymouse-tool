# -*- coding: utf-8 -*-
"""Nuitka 打包脚本：生成单文件 dist\\清风自动化键鼠工具.exe

用法（双击 build.bat 等价于无参数调用）：
    python build.py              打包（默认 onefile 单文件，复用缓存）
    python build.py --fast       跳过 onefile 压缩（产物约 320 MB，构建更快）
    python build.py --clean      清空缓存后全量重打
    python build.py --console    保留控制台窗口（排查启动崩溃用）
    python build.py -v           Nuitka 详细日志（输出很长）

提速说明（实测 20 核 / Nuitka 2.7.11 / Python 3.12.10）：
- 首次全量约 25 分钟；此后 ccache 命中（缓存存于 %LOCALAPPDATA%\\nuitka），
  常规构建约 7 分钟（C 代码生成 + 链接 + onefile 压缩是固定开销）。
- --fast 跳过压缩阶段，能再省约 1-2 分钟，代价是产物从 ~77 MB 涨到 ~320 MB。
- 缓存目录别删；build.bat 的 --clean 只清 build_nuitka\\，不影响全局缓存。

为什么用 Nuitka：
1. **体积小**：只编译进实际用到的代码路径，产物约 64 MB，比 PyInstaller 整包
   复制（约 105 MB）小很多。
2. **抗反编译**：代码被编译成 C 再链接，不是 .pyc 字节码，泄露风险低。
3. **性能更好**：热路径（找图、点击循环）被编译成机器码，运行时更快。

代价（如实说明）：
- 慢：把 320 个模块逐个翻译成 C 再用 gcc 编译链接，实测约 7 分钟。
  cv2 因动态加载子模块较多是编译大头。
- 需要 C 编译器：首次会自动下载 MinGW（--assume-yes-for-downloads）。
- 依赖分析是静态的，动态 import（pynput/mss 的平台后端、DrissionPage 数据
  文件）需要显式声明，见下方 HIDDEN_MODULES / DrissionPage 数据文件参数。
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
REQUIRED_MODULES = ("nuitka", "PySide6", "cv2", "keyboard",
                    "pynput", "mss", "PIL", "DrissionPage",
                    "rapidocr_onnxruntime", "onnxruntime")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "清风自动化键鼠工具"
BUILD_DIR = os.path.join(BASE_DIR, "build_nuitka")
DIST_DIR = os.path.join(BASE_DIR, "dist")
EXE_NAME = f"{APP_NAME}.exe"
FILE_VERSION = "1.0.0.0"

# 静态分析扫不到、必须显式声明的隐藏模块
# - pynput/mss 按 sys.platform 拼模块名动态导入平台后端（_win32 / windows）
# - cv2 动态加载全部子模块（Nuitka 对 cv2 用 --include-package=cv2 全量包含）
HIDDEN_MODULES = [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "mss.windows",
]

# DrissionPage 随包分发的非 .py 数据文件：源文件 -> 包内目标（相对 site-packages）
DRISSION_DATA_FILES = (
    ("_configs", "configs.ini"),
    ("_functions", "suffixes.dat"),
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
    可能是别的版本；双击 build.bat 时用的就是 PATH 上的第一个。与其让人困惑
    于「找不到 nuitka」，不如脚本自己找对解释器。
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


def _drission_data_args() -> list:
    """DrissionPage 数据文件参数。

    configs.ini / suffixes.dat 是运行时按「模块文件所在目录」拼路径读的
    （Path(__file__).parent / 'configs.ini'），Nuitka 不会自动收 .ini/.dat，
    漏掉的话打包后一碰到 ChromiumOptions 就 FileNotFoundError。
    Nuitka 的 --include-data-files 格式：源路径=包内路径（相对 site-packages）。
    """
    try:
        import DrissionPage
    except ImportError:
        print("[警告] 没找到 DrissionPage，跳过它的数据文件（网页步骤将不可用）")
        return []
    pkg = os.path.dirname(os.path.abspath(DrissionPage.__file__))
    args = []
    for sub, name in DRISSION_DATA_FILES:
        src = os.path.join(pkg, sub, name)
        if not os.path.isfile(src):
            print(f"[警告] 缺少 DrissionPage 数据文件：{src}")
            continue
        args.append(f"--include-data-files={src}=DrissionPage/{sub}/{name}")
    return args


def _rapidocr_data_args() -> list:
    """RapidOCR 的模型（.onnx）与配置（.yaml）数据文件参数。

    RapidOCR 运行时按「包目录 / models / xxx.onnx」和「包目录 / config.yaml」
    拼路径读取，Nuitka 不会自动收 .onnx/.yaml，漏掉则打包后识别崩溃。
    """
    try:
        import rapidocr_onnxruntime
    except ImportError:
        print("[警告] 没找到 rapidocr_onnxruntime，跳过它的模型（文字识别将不可用）")
        return []
    pkg = os.path.dirname(os.path.abspath(rapidocr_onnxruntime.__file__))
    rels = [
        "config.yaml",
        os.path.join("models", "ch_PP-OCRv4_det_infer.onnx"),
        os.path.join("models", "ch_PP-OCRv4_rec_infer.onnx"),
        os.path.join("models", "ch_ppocr_mobile_v2.0_cls_infer.onnx"),
    ]
    args = []
    for rel in rels:
        src = os.path.join(pkg, rel)
        if os.path.isfile(src):
            args.append(f"--include-data-files={src}=rapidocr_onnxruntime/{rel}")
        else:
            print(f"[警告] 缺少 RapidOCR 数据文件：{src}")
    return args


def _onnxruntime_dll_args() -> list:
    """onnxruntime 的 DLL：交给 Nuitka 自动检测（dll-files 插件），无需显式带。

    之前手动用 --include-data-files 指定 onnxruntime.dll，会与 Nuitka 插件
    自动放入的同一 DLL 冲突（FATAL: conflicts with dll）。Nuitka 会扫描
    onnxruntime 包并自动包含它的 DLL（onnxruntime.dll / providers_shared.dll）。
    """
    return []


def _ocr_include_args() -> list:
    """文字识别的打包参数：**包含** RapidOCR（onnxruntime 推理）。

    用 RapidOCR 替代 PaddleOCR：两者跑的是同一个 PP-OCR 模型、识别精度一致，
    但 RapidOCR 依赖只有 onnxruntime（CPU 版约 43MB）+ 模型（约 15MB），
    远小于 PaddleOCR 的 paddlepaddle + torch（合计约 1GB）。因此文字识别
    可以打进 onefile：产物约从 80MB 涨到 ~140MB，构建时间基本不变。

    注意 onnxruntime 的坑：它 __init__.py 会在 cpuinfo+py3nvml 存在时导入
    transformers.machine_info（进而 import tensorflow/keras 巨型库），且包内
    transformers/quantization/tools 等工具子包 import torch。所以：
    - 只包含核心模块（onnxruntime + capi 子包），不整包分析
    - nofollow 掉 transformers/torch/tensorflow/keras/cpuinfo/py3nvml，
      既避免 Nuitka 递归分析卡死，也保证打包后 find_spec 检查不成立、
      运行时不会真的去 import 那些工具模块
    """
    return [
        "--include-package=rapidocr_onnxruntime",
        "--include-module=onnxruntime",
        "--include-package=onnxruntime.capi",
        "--nofollow-import-to=onnxruntime.transformers",
        "--nofollow-import-to=torch",
        "--nofollow-import-to=tensorflow",
        "--nofollow-import-to=keras",
        "--nofollow-import-to=cpuinfo",
        "--nofollow-import-to=py3nvml",
        *_rapidocr_data_args(),
        *_onnxruntime_dll_args(),
    ]


def build_cmd(args) -> list:
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone", "--onefile",
        "--windows-console-mode=disable" if not args.console else "--windows-console-mode=force",
        "--enable-plugin=pyside6",
        # cv2 动态加载全部子模块，必须整包包含
        "--include-package=cv2",
        "--include-package=keyboard",
        "--include-package=pynput",
        "--include-package=mss",
        # 平台动态后端
        *[f"--include-module={m}" for m in HIDDEN_MODULES],
        # 文字识别（RapidOCR + onnxruntime）：依赖小，打进 onefile
        *_ocr_include_args(),
        # 数据文件
        *[f"--include-data-dir={os.path.join(BASE_DIR, 'assets')}=assets"],
        *_drission_data_args(),
        f"--windows-icon-from-ico={os.path.join(BASE_DIR, 'assets', 'icon.ico')}",
        "--company-name=QingFeng",
        f"--product-name={APP_NAME}",
        f"--file-version={FILE_VERSION}",
        f"--file-description={APP_NAME}",
        f"--output-dir={BUILD_DIR}",
        f"--output-filename={EXE_NAME}",
        "--remove-output",
        "--assume-yes-for-downloads",
        "--lto=no",
        # ---- 提速优化 ----
        # 并行编译：默认就是全核，这里显式指定，避免低内存模式下退化到 1
        "--jobs=16",
        # 跳过 .pyi 存根生成（本项目不需要 IDE 提示用的 pyi）
        "--no-pyi-file",
        *([] if not args.fast else ["--onefile-no-compression"]),
        "main.py",
    ]
    if args.verbose:
        cmd.append("--verbose")
    return cmd


def _prune_old_pyinstaller() -> None:
    """提示用户 PyInstaller 的旧构建目录已无用（不自动删除，避免误删）。"""
    old = os.path.join(BASE_DIR, "build_pyinstaller")
    if os.path.isdir(old):
        try:
            size = sum(
                os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(old)
                for f in files
            ) / 1048576
        except OSError:
            size = 0
        print(f"[提示] 发现旧的 PyInstaller 构建目录 build_pyinstaller\\（约 {size:.0f} MB），"
              f"已不再使用，确认无误后可手动删除。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Nuitka 打包")
    ap.add_argument("--clean", action="store_true",
                    help="清空构建缓存后全量重打")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口（排查启动崩溃用）")
    ap.add_argument("--fast", action="store_true",
                    help="跳过 onefile 压缩（产物约 320 MB，但构建更快）")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Nuitka 详细日志（输出很长）")
    args = ap.parse_args()

    ensure_interpreter()          # 必须在 os.chdir 之前：sys.argv[0] 可能是相对路径
    os.chdir(BASE_DIR)

    if _is_running(EXE_NAME):
        print(f"[错误] {EXE_NAME} 正在运行，exe 被占用会导致打包失败。")
        print("       请从托盘图标右键 -> 退出，然后重新构建。")
        return 1

    os.makedirs(BUILD_DIR, exist_ok=True)

    if args.clean:
        print("[清理] 删除 build_nuitka 与旧产物（全量重打）")
        for p in (BUILD_DIR, os.path.join(DIST_DIR, APP_NAME)):
            try:
                shutil.rmtree(p, ignore_errors=True)
            except OSError as e:
                print(f"[警告] 清理失败（可手动删除后重试）：{e}")
        os.makedirs(BUILD_DIR, exist_ok=True)

    print(f"[模式] onefile 单文件 · "
          f"{'带控制台' if args.console else '无控制台'} · "
          f"{'全量' if args.clean else '复用缓存'}")

    cmd = build_cmd(args)
    print("[执行]", " ".join(cmd), flush=True)

    t0 = time.monotonic()
    code = subprocess.call(cmd)
    elapsed = time.monotonic() - t0

    if code != 0:
        print(f"\n[失败] Nuitka 返回 {code}，耗时 {_fmt(elapsed)}")
        print("       若提示找不到 nuitka，请先执行: pip install nuitka")
        print("       若报缺少模块或 C 编译器，可先试 python build.py --console 看崩溃信息")
        return code

    exe = os.path.join(BUILD_DIR, EXE_NAME)
    if os.path.isfile(exe):
        dist_exe = os.path.join(DIST_DIR, EXE_NAME)
        os.makedirs(DIST_DIR, exist_ok=True)
        # 旧产物先改名备份再覆盖：直接 os.remove 在部分环境（如杀软锁定/沙箱
        # 回收站不可用）会抛异常导致构建明明成功却报失败
        if os.path.isfile(dist_exe):
            backup = dist_exe + ".old"
            try:
                if os.path.isfile(backup):
                    os.remove(backup)
            except OSError:
                pass
            try:
                os.replace(dist_exe, backup)
            except OSError:
                try:
                    os.remove(dist_exe)
                except OSError as e:
                    print(f"[警告] 旧产物删除失败（可手动删除）：{e}")
        try:
            os.replace(exe, dist_exe)
        except OSError as e:
            print(f"\n[失败] 无法把产物移动到 dist：{e}")
            print(f"       产物在：{exe}")
            return 1
        size_mb = os.path.getsize(dist_exe) / 1048576
        print(f"\n[完成] 耗时 {_fmt(elapsed)}")
        print(f"       {os.path.relpath(dist_exe, BASE_DIR)}（{size_mb:.1f} MB）")
        print("       说明：config.json / templates\\ / flows\\ / app.log 会在 exe "
              "同级目录自动生成；assets 图标已内嵌，无需随 exe 分发。")
        _prune_old_pyinstaller()
        return 0

    print(f"\n[失败] 未找到产物: {exe}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
