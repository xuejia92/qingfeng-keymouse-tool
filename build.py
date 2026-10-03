# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本：生成单文件 dist\\清风自动化键鼠工具.exe

用法（双击 build.bat 等价于无参数调用）：
    python build.py              打包（onefile 单文件）
    python build.py --dir        onedir 目录模式，启动更快，适合改完代码快速验证
    python build.py --console    保留控制台窗口（排查启动崩溃用）
    python build.py --clean      清空 PyInstaller 缓存后全量重打
    python build.py --sync-only  只把最新的 templates\\ / flows\\ 同步到 dist，不打包

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
# segno：二维码编码（「小工具 -> 局域网文件传输」）；纯 Python，静态 import，
# 缺了会导致打包后的 exe 生成不了二维码（界面会降级，但那是缺陷不是设计）。
REQUIRED_MODULES = ("PySide6", "cv2", "keyboard", "pynput", "mss", "PIL",
                    "DrissionPage", "rapidocr_onnxruntime", "onnxruntime",
                    "segno")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "清风自动化键鼠工具"
DIST_DIR = os.path.join(BASE_DIR, "dist")
WORK_DIR = os.path.join(BASE_DIR, "build_pyinstaller")
EXE_NAME = f"{APP_NAME}.exe"

# 打包后要同步到 dist 的数据目录：exe 读的是同级目录里的这些文件夹，
# 只留在工作区的改动同步过去才算"打包即最新"（见 sync_data_dirs）。
SYNC_DIRS = ("templates", "flows")

# 按 sys.platform 拼模块名做动态导入的平台后端，静态分析扫不到
HIDDEN_MODULES = [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "mss.windows",
    # pyttsx3 按驱动名动态 __import__（'pyttsx3.drivers.sapi5'），静态扫描扫不到，
    # 不显式声明的话打包后语音播报会报「找不到 driver」
    "pyttsx3.drivers.sapi5",
    # rapidocr 的三个实现子包是运行时才动态导入的（见 RAPIDOCR_SUBPACKAGES），
    # 它们内部用的这几个库同样扫不到 —— 漏了会在 OCR 初始化时报
    # ModuleNotFoundError（2026-09-26 修好 ch_ppocr_v3_det 后又撞上 pyclipper）
    "pyclipper",
    "shapely",
    "six",
]

# onnxruntime 的条件导入链（cpuinfo+py3nvml 存在时 import transformers 进而
# 拉入 tensorflow/keras），排除避免体积暴涨
# 另：本机同时装有 PyQt5/PySide6 时，PyInstaller 会因「多 Qt 绑定」报错中止
# （attempt to collect multiple Qt bindings packages），程序只用 PySide6，
# 把其它 Qt 绑定一并排除（官方推荐的 exclude 机制）。
# 再另：目标检测（yolo_actor）对 torch/ultralytics 已改用 importlib 字符串
# 动态导入（静态分析扫不到，见 app/yolo_actor.py::_import_lib），本机若残留
# 任何静态 import（如第三方 hook 的 collect）都会被下面整份防御清单拦住，
# 防止开发机 site-packages 里整片 ML/AI 生态（paddle/diffusers/ray 等）卷进包。
EXCLUDED_MODULES = [
    "transformers", "torch", "tensorflow", "keras",
    "cpuinfo", "py3nvml",
    "PyQt5", "PyQt5.sip", "PyQt6", "PyQt6.sip", "PySide2", "qtpy",
    # --- AI/ML 生态防御清单（2026-09-03：曾把 exe 撑到 794MB）---
    "ultralytics", "torchvision", "torchaudio", "torchtext", "torchhub",
    "paddle", "paddleocr", "paddlex", "paddlenlp",
    "diffusers", "flax", "gradio", "ray", "streamlit",
    "yt_dlp", "onnx2tf", "onnxslim", "onnxoptimizer", "onnx_graphsurgeon",
    "scipy", "pandas", "pyarrow", "polars",
    "llvmlite", "numba", "matplotlib", "seaborn", "PIL.ImageShow",
    "sympy", "networkx", "sqlalchemy", "twisted", "openai", "tf_keras",
    "sentry_sdk", "openpyxl", "peft", "trio", "trl", "bitsandbytes",
    "sklearn", "skimage", "statsmodels", "xgboost", "lightgbm",
    "playwright", "patchright", "pygame", "pypinyin",  # 机器上有但程序不用
    # 注意：不要排除 psutil（DrissionPage 运行依赖）与 onnx 本体（onnxruntime
    # 的 capi 只依赖自身 dll；onnx 包若有 hook 收集再单独处理）
]

# DrissionPage 随包分发的非 .py 数据文件：包内子目录 -> 文件名
DRISSION_DATA_FILES = (
    ("_configs", "configs.ini"),
    ("_functions", "suffixes.dat"),
)

# RapidOCR 的模型与配置（运行时按「包目录」拼路径读取）
# rapidocr 的运行时资源：模型清单 config.yaml + models/ 下的 onnx 模型。
# ⚠️ 模型文件名**不能硬编码**：上游换过版本（v3 → v4），写死会静默漏收 ——
# 2026-09-26 就踩到「build.py 里写着 v4、实际装的只有 v3」这种情况，所以这里
# 改成扫描 models/ 目录，把所有 .onnx 都带上。
RAPIDOCR_CONFIG_FILE = "config.yaml"
RAPIDOCR_MODEL_DIR = "models"
RAPIDOCR_MODEL_EXT = ".onnx"

# rapidocr 的三个模型实现子包是**动态导入**的，静态分析扫不到：
#   rapid_ocr_api.py: sys.path.append(包目录) + importlib.import_module("ch_ppocr_v3_det")
# config.yaml 里 Det/Cls/Rec 的 module_name 就是这三个名字。必须把整个目录当
# **数据**原样带进包里（运行时 sys.path 才 import 得到）。
# 漏带任何一个 → OCR 时报 ModuleNotFoundError: No module named 'ch_ppocr_v3_det'
RAPIDOCR_SUBPACKAGES = ("ch_ppocr_v3_det", "ch_ppocr_v2_cls", "ch_ppocr_v3_rec")


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
    """exe 正在运行时无法被覆盖，构建前先查一次。

    ⚠️ 中文 Windows 的 tasklist 输出是**本地代码页**（GBK），而 subprocess 的 text=True
    默认按 UTF-8 解码 → 会抛 UnicodeDecodeError，且 `.stdout` 变成 None 引发
    AttributeError（2026-09-26 实际把 `python build.py` 整个打断）。这里显式指定 mbcs
    解码 + errors="replace" 兜底；任何异常一律当作「没在运行」，绝不拦住打包。
    """
    try:
        kwargs = {"capture_output": True, "text": True, "timeout": 15,
                  "errors": "replace"}
        if os.name == "nt":
            kwargs["encoding"] = "mbcs"
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
                             **kwargs).stdout or ""
        return exe_name.lower() in out.lower()
    except Exception:
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
        # ① 模型清单（config.yaml）：列着 Det/Cls/Rec 用哪个模块、哪个模型
        cfg_src = os.path.join(pkg, RAPIDOCR_CONFIG_FILE)
        if os.path.isfile(cfg_src):
            args.append(cfg_src + sep + "rapidocr_onnxruntime")
        else:
            print(f"[警告] 缺少 RapidOCR 配置：{cfg_src}")
        # ② 模型本体：models/ 下的 .onnx 全收（不写死文件名，换版本也不会漏）
        model_dir = os.path.join(pkg, RAPIDOCR_MODEL_DIR)
        models = []
        if os.path.isdir(model_dir):
            models = sorted(f for f in os.listdir(model_dir)
                            if f.lower().endswith(RAPIDOCR_MODEL_EXT))
        for name in models:
            args.append(os.path.join(model_dir, name)
                        + sep + f"rapidocr_onnxruntime/{RAPIDOCR_MODEL_DIR}")
        if not models:
            print(f"[警告] 没找到 RapidOCR 模型（{model_dir} 下没有 {RAPIDOCR_MODEL_EXT}）："
                  "文字识别会失败")
        # ③ 动态导入的实现子包：整目录当数据带进去（缺了会 ModuleNotFoundError）
        for sub in RAPIDOCR_SUBPACKAGES:
            sub_dir = os.path.join(pkg, sub)
            if os.path.isdir(sub_dir):
                args.append(sub_dir + sep + f"rapidocr_onnxruntime/{sub}")
            else:
                print(f"[警告] 缺少 RapidOCR 子包 {sub}\\：OCR 会报 "
                      f"ModuleNotFoundError: No module named '{sub}'")

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
        "--additional-hooks-dir", os.path.join(BASE_DIR, "hooks"),
        *[f"--add-data={d}" for d in _add_data_args()],
        os.path.join(BASE_DIR, "main.py"),
    ]
    return cmd


def _sync_one_dir(name: str, backup_root: str | None = None,
                  quiet: bool = False) -> dict:
    """把工作区的 <name> 目录同步到 dist/<name>，返回统计。

    只做「源比目标新」的覆盖复制（按文件大小 + 修改时间判断），不删除目标里
    多出来的文件——dist 里可能有用户自己放的东西，静默删除风险太大，改为只报告。

    若某个文件**目标比源还新**（说明用户直接改过 dist 里那份，比如在打包后的
    程序界面里调过流程），覆盖前先复制一份到 backup_root 下，避免静默丢改动。
    """
    src_root = os.path.join(BASE_DIR, name)
    dst_root = os.path.join(DIST_DIR, name)
    stat = {"added": 0, "updated": 0, "same": 0, "extra": [], "backed_up": []}
    if not os.path.isdir(src_root):
        if not quiet:
            print(f"  [跳过] 工作区没有 {name}\\ 目录")
        return stat
    src_rel = set()
    for dirpath, _dirnames, filenames in os.walk(src_root):
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, src_root)
            src_rel.add(rel)
            dst = os.path.join(dst_root, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    stat["added"] += 1
                    continue
                s, d = os.stat(src), os.stat(dst)
                # 大小不同必定要更新；大小相同再比修改时间（按秒取整，躲开精度抖动）
                if s.st_size != d.st_size or int(s.st_mtime) > int(d.st_mtime):
                    if backup_root and int(d.st_mtime) > int(s.st_mtime):
                        bdst = os.path.join(backup_root, name, rel)
                        os.makedirs(os.path.dirname(bdst), exist_ok=True)
                        shutil.copy2(dst, bdst)
                        stat["backed_up"].append(rel)
                    shutil.copy2(src, dst)
                    stat["updated"] += 1
                else:
                    stat["same"] += 1
            except OSError as e:
                print(f"  [警告] 同步 {name}\\{rel} 失败：{e}")
    if os.path.isdir(dst_root):
        for dirpath, _dirnames, filenames in os.walk(dst_root):
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), dst_root)
                if rel not in src_rel:
                    stat["extra"].append(rel)
    return stat


def sync_data_dirs(names=SYNC_DIRS, quiet: bool = False) -> dict:
    """把工作区的 templates\\ / flows\\ 同步到 dist，保证 exe 旁边用的是最新文件。

    exe 运行时读的是**自己同级目录**的 templates / flows；改完模板或流程如果只
    留在工作区，打出来的包旁边还是旧文件，跑起来就是"改了没生效"。

    覆盖掉「dist 里那份反而更新」的文件时，会先备份到 dist\\_sync_backup\\<时间戳>\\。

    quiet=True 时**不打印任何正常信息**（只保留同步失败这类警告），用于每次启动都
    调一次的场景（restart_watchdog 启动主程序前会调，别刷屏）；返回值汇总：
    {"added", "updated", "same", "extra", "backed_up", "backup_dir"}。
    """
    if not quiet:
        print("\n[同步] 把工作区的模板 / 流程同步到 dist")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_root = os.path.join(DIST_DIR, "_sync_backup", stamp)
    summary = {"added": 0, "updated": 0, "same": 0, "extra": 0, "backed_up": 0,
               "backup_dir": ""}
    used_backup = False
    for name in names:
        st = _sync_one_dir(name, backup_root=backup_root, quiet=quiet)
        summary["added"] += st["added"]
        summary["updated"] += st["updated"]
        summary["same"] += st["same"]
        summary["backed_up"] += len(st["backed_up"])
        summary["extra"] += len(st["extra"])
        if not quiet:
            print(f"  {name}\\：新增 {st['added']} · 更新 {st['updated']} · "
                  f"已最新 {st['same']}")
        if st["backed_up"]:
            used_backup = True
            if not quiet:
                print(f"    注意：{len(st['backed_up'])} 个文件在 dist 里比工作区新"
                      f"（像是直接在程序里改过），已备份后再覆盖")
                for rel in st["backed_up"][:5]:
                    print(f"      - {rel}")
                if len(st["backed_up"]) > 5:
                    print(f"      …等 {len(st['backed_up'])} 个")
        if st["extra"] and not quiet:
            print(f"    提示：dist\\{name}\\ 里有 {len(st['extra'])} 个源目录已不存在的文件"
                  f"（未自动删除，需要清理请手动处理）")
            for rel in st["extra"][:5]:
                print(f"      - {rel}")
            if len(st["extra"]) > 5:
                print(f"      …等 {len(st['extra'])} 个")
    if used_backup:
        summary["backup_dir"] = backup_root
        if not quiet:
            print(f"    备份位置：{os.path.relpath(backup_root, BASE_DIR)}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="PyInstaller 打包")
    ap.add_argument("--dir", action="store_true",
                    help="onedir 目录模式，启动更快，适合改完代码快速验证")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口（排查启动崩溃用）")
    ap.add_argument("--clean", action="store_true",
                    help="清空 PyInstaller 缓存后全量重打")
    ap.add_argument("--sync-only", action="store_true",
                    help="只把最新的 templates / flows 同步到 dist，不重新打包")
    args = ap.parse_args()

    ensure_interpreter()          # 必须在 os.chdir 之前：sys.argv[0] 可能是相对路径
    os.chdir(BASE_DIR)

    if args.sync_only:
        os.makedirs(DIST_DIR, exist_ok=True)
        sync_data_dirs()
        return 0

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

    # 打包成功后：把工作区最新的 templates / flows 同步到 dist，
    # 免得 exe 旁边还是旧模板/旧流程（改了没生效最容易踩的坑）
    sync_data_dirs()

    size_mb = os.path.getsize(exe) / 1048576
    print(f"\n[完成] 耗时 {_fmt(elapsed)}")
    print(f"       {os.path.relpath(exe, BASE_DIR)}（{size_mb:.1f} MB）")
    print("       说明：config.json / templates\\ / flows\\ / app.log 在 exe 同级目录；"
          "本次已把工作区最新的 templates 与 flows 同步过去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
