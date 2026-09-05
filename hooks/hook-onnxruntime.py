"""hook-onnxruntime.py：覆盖 PyInstaller-hooks-contrib 的同名 hook。

hooks-contrib 默认 `collect_dynamic_libs('onnxruntime')` 会把 capi 下所有
provider dll 一起收走——onnxruntime_providers_cuda.dll（约 164MB）与
onnxruntime_providers_tensorrt.dll（数十 MB）。本程序只用 CPU 推理
（RapidOCR 文字识别、yolo .onnx 检测均走 CPUExecutionProvider），
GPU provider 全部剔除，只保留 CPU 推理所需的动态库。
"""
import os

from PyInstaller.utils.hooks import collect_dynamic_libs

# capi 目录下保留下发的动态库（白名单）；providers_cuda/tensorrt 等被过滤。
# onnxruntime_pybind11_state.pyd 属扩展模块，由 import 自动收集，无需在此列出。
_KEEP = {"onnxruntime.dll", "onnxruntime_providers_shared.dll"}

binaries = []
for src, dest in collect_dynamic_libs("onnxruntime"):
    if os.path.basename(src).lower() in _KEEP:
        binaries.append((src, dest))
