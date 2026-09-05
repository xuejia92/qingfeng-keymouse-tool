"""YOLOv5 目标检测执行器（流程「目标检测」步骤的后端）。

懒加载依赖：torch / ultralytics / onnxruntime 体积大且打包时被排除
（见 build.py excludes），只有真正执行检测步骤时才 import；
缺依赖时抛出面向用户的 YoloError 而不是崩溃。

模型缓存：同一 (模型绝对路径, 设备) 只加载一次，流程里反复执行检测步骤不重复加载。

后端选择（按模型文件类型）：
- .onnx -> onnxruntime 后端（最轻量：无需 torch/ultralytics/yolov5 仓库，
  pip install onnxruntime 即可完全离线运行；旧版 yolov5 模型用 export.py
  导出 onnx 后即可绕过 v5~v7 与 YOLOv8+ 的不兼容问题）；
- .pt   -> 优先 ultralytics（YOLO 类，适合新版/官方模型）；加载失败
  （含旧版 yolov5 v5~v7 模型的不向前兼容 TypeError）自动退回 yolov5 原生仓库
  后端——本地仓库优先（模型同目录/上级目录含 hubconf.py 即离线使用），
  torch.hub 联网拉取兜底。
三种后端统一包装成 predict(img_bgr, conf) -> [(x1, y1, x2, y2, conf, label), ...]。
"""
from __future__ import annotations

import os

import numpy as np


def _import_lib(name: str):
    """以 importlib 动态导入（字符串形式，专供 torch/ultralytics 等大体积可选依赖）。

    PyInstaller 只做静态字节码分析，看不到 importlib.import_module 的字符串
    参数，因此这些依赖不会被打进 exe（实测 ultralytics 一旦被静态收集，
    hook 会把整片 ML/AI 生态带进包，体积暴涨数倍）；源码运行时若机器上
    已安装仍可正常使用。目标库缺失时与 import 语句一致地抛 ImportError。
    """
    import importlib
    return importlib.import_module(name)

# (模型绝对路径, 设备) -> 已加载模型（_OnnxModel / _UltralyticsModel / _HubModel）
_models: dict = {}


class YoloError(Exception):
    """检测前置条件不满足（缺依赖/路径无效/设备不可用/区域越界等），消息直接面向用户。"""


# ---------------- 依赖与设备 ----------------

def _import_torch():
    try:
        return _import_lib("torch")
    except ImportError:
        raise YoloError(
            "缺少依赖 torch：目标检测需要 PyTorch 与 YOLOv5，请先安装\n"
            "pip install torch ultralytics")


def _check_device(torch, device: str) -> str:
    device = (device or "cuda").strip().lower()
    if device not in ("cpu", "cuda"):
        raise YoloError(f"推理设备无效：「{device}」（仅支持 cpu / cuda）")
    if device == "cuda" and not torch.cuda.is_available():
        raise YoloError(
            "推理设备选择了 cuda，但当前 CUDA 不可用（无 NVIDIA 显卡/驱动，"
            "或安装的是 CPU 版 torch），请在步骤参数中改用 cpu")
    return device


# ---------------- 模型后端 ----------------

def _load_onnx_class_names(model_path: str) -> list:
    """onnx 文件不携带类别名：在模型目录及上两层找 classes.txt（每行一个类别名）。

    找不到返回空列表，predict 里用 class0/class1... 占位（此时按类别名过滤
    自然匹配不到，结果为空——需要类别过滤时请放好 classes.txt）。
    """
    d = os.path.dirname(os.path.abspath(model_path))
    for _ in range(3):
        f = os.path.join(d, "classes.txt")
        if os.path.isfile(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    names = [ln.strip() for ln in fh if ln.strip()]
                if names:
                    return names
            except OSError:
                pass
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return []


class _OnnxModel:
    """onnxruntime 后端：直接推理 yolov5 export.py 导出的 .onnx（v5~v7 Detect 头）。

    不依赖 torch / ultralytics / yolov5 仓库，只需 onnxruntime（NMS 用 cv2，
    是找图模块的既有依赖）。预处理 letterbox + 后处理 obj_conf*cls_conf +
    分类别 NMS，与 yolov5 原生推理一致（标准导出的输出已过 sigmoid，
    坐标是输入图像素空间的 cx,cy,w,h）。
    """

    def __init__(self, path: str, device: str):
        try:
            ort = _import_lib("onnxruntime")
        except ImportError:
            raise YoloError(
                "缺少依赖 onnxruntime：加载 .onnx 模型请先安装\n"
                "pip install onnxruntime（有 NVIDIA 显卡可装 onnxruntime-gpu）")
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if device == "cuda" else ["CPUExecutionProvider"])
        try:
            self._sess = ort.InferenceSession(path, providers=providers)
        except Exception as e:
            raise YoloError(f"onnx 模型加载失败：{type(e).__name__}: {e}")
        inp = self._sess.get_inputs()[0]
        self._input_name = inp.name
        shape = inp.shape or []
        # 动态维度（字符串/None）时按 yolov5 默认 640 处理
        self._ih = int(shape[2]) if len(shape) > 2 and isinstance(shape[2], int) else 640
        self._iw = int(shape[3]) if len(shape) > 3 and isinstance(shape[3], int) else 640
        self._names = _load_onnx_class_names(path)

    def predict(self, img_bgr: np.ndarray, conf: float) -> list:
        import cv2
        h0, w0 = img_bgr.shape[:2]
        # letterbox 到模型输入尺寸（与 yolov5 推理预处理一致）
        r = min(self._iw / w0, self._ih / h0)
        nw, nh = max(1, int(round(w0 * r))), max(1, int(round(h0 * r)))
        canvas = np.full((self._ih, self._iw, 3), 114, dtype=np.uint8)
        dw, dh = (self._iw - nw) // 2, (self._ih - nh) // 2
        canvas[dh:dh + nh, dw:dw + nw] = cv2.resize(img_bgr, (nw, nh))
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        pred = self._sess.run(None, {self._input_name: blob})[0]
        pred = pred[0] if pred.ndim == 3 else pred      # (N, 5+nc)
        if pred.ndim != 2 or pred.shape[1] < 6:
            raise YoloError(
                f"onnx 输出格式异常：{pred.shape}（应为 (N, 5+类别数) 的 yolov5 Detect 输出）")
        scores = pred[:, 5:] * pred[:, 4:5]             # obj_conf * cls_conf
        cls_ids = scores.argmax(axis=1)
        cls_conf = scores.max(axis=1)
        keep = np.where(cls_conf >= conf)[0]
        if keep.size == 0:
            return []
        # 坐标从 letterbox 空间映射回原图
        cx, cy = pred[keep, 0], pred[keep, 1]
        w, h = pred[keep, 2], pred[keep, 3]
        x1 = (cx - w / 2 - dw) / r
        y1 = (cy - h / 2 - dh) / r
        x2 = (cx + w / 2 - dw) / r
        y2 = (cy + h / 2 - dh) / r
        boxes = [[float(a), float(b), float(c - a), float(d - b)]
                 for a, b, c, d in zip(x1, y1, x2, y2)]   # NMSBoxes 要 xywh
        confs = [float(cls_conf[i]) for i in keep]
        kept_cls = [int(cls_ids[i]) for i in keep]
        # 分类别 NMS（与 yolov5 batched NMS 语义一致，避免异类框互相压制）
        out = []
        for cid in sorted(set(kept_cls)):
            sel = [j for j, c in enumerate(kept_cls) if c == cid]
            sub_boxes = [boxes[j] for j in sel]
            sub_confs = [confs[j] for j in sel]
            idxs = np.array(cv2.dnn.NMSBoxes(sub_boxes, sub_confs, conf, 0.45)).flatten()
            name = self._names[cid] if 0 <= cid < len(self._names) else f"class{cid}"
            for j in idxs:
                bx, by, bw, bh = sub_boxes[j]
                out.append((bx, by, bx + bw, by + bh, sub_confs[j], name))
        return out


class _UltralyticsModel:
    """ultralytics 后端（YOLO 类可加载 yolov5 系列 .pt）。

    构造时做一次预热推理：ultralytics 对部分模型是「构造不报错、predict 才炸」
    （如无 metadata 的 yolov5 onnx，AutoBackend 在 predict 时 metadata['task']
    KeyError）——加载期就验证可用性，让 _load_model 的兜底/合并报错能接住，
    而不是流程跑到一半才失败。
    """

    def __init__(self, path: str, device: str):
        YOLO = _import_lib("ultralytics").YOLO
        self._m = YOLO(path)
        self._device = device
        self.predict(np.zeros((8, 8, 3), dtype=np.uint8), 0.99)   # 预热校验

    def predict(self, img_bgr: np.ndarray, conf: float) -> list:
        res = self._m.predict(img_bgr, conf=conf, device=self._device, verbose=False)
        out = []
        r = res[0]
        names = r.names
        for b in r.boxes:
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
            out.append((x1, y1, x2, y2, float(b.conf[0]), str(names[int(b.cls[0])])))
        return out


class _HubModel:
    """yolov5 原生仓库后端（旧版 yolov5 训练模型的兜底）。

    ultralytics（YOLOv8+）对 yolov5 v5~v7 训练的 .pt 不向前兼容（报 TypeError
    "NOT forwards compatible"），这类模型要用 yolov5 原生仓库加载：
      - 优先模型同目录/上级目录里的本地 yolov5 仓库（含 hubconf.py 的目录，免联网）；
      - 找不到本地仓库时 torch.hub 联网拉取 ultralytics/yolov5（首次需联网）。
    两种来源返回的都是 AutoShape 包装模型（.conf/.names + numpy 输入 + NMS）。
    """

    def __init__(self, torch, path: str, device: str):
        # 本地仓库优先（模型附近），其次 torch hub 缓存仓库（免联网、跳过
        # hubconf 里会 pip 装包的 check_requirements），最后才 torch.hub 联网拉取
        repo = _find_local_yolov5_repo(path) or _find_cached_hub_repo(torch)
        if repo:
            self._m = self._load_from_local_repo(torch, repo, path, device)
        else:
            self._m = torch.hub.load("ultralytics/yolov5", "custom", path=path,
                                     trust_repo=True)
            self._m.to(device)

    @staticmethod
    def _load_from_local_repo(torch, repo: str, path: str, device: str):
        """从本地 yolov5 仓库加载：仓库目录进 sys.path 后用其 attempt_load + AutoShape。

        AutoShape 包装提供 .conf/.names 与 numpy 输入 + NMS（与 hub custom 返回一致）。
        torch>=2.6 的 torch.load 默认 weights_only=True，会拒绝 yolov5 旧版
        checkpoint（pickle 的 nn 模块）；本地模型是用户自己训练的可信文件，
        加载期间临时回退 weights_only=False，加载完恢复。
        """
        import sys
        if repo not in sys.path:
            sys.path.insert(0, repo)
        AutoShape = _import_lib("models.common").AutoShape
        attempt_load = _import_lib("models.experimental").attempt_load
        real_load = torch.load

        def _load_compat(*a, **kw):
            kw.setdefault("weights_only", False)
            return real_load(*a, **kw)

        torch.load = _load_compat
        try:
            m = attempt_load(path, device=device)
        finally:
            torch.load = real_load
        return AutoShape(m)

    def predict(self, img_bgr: np.ndarray, conf: float) -> list:
        self._m.conf = conf
        res = self._m(img_bgr)
        names = self._m.names
        out = []
        for row in res.xyxy[0].tolist():
            x1, y1, x2, y2, c, cls = row
            out.append((float(x1), float(y1), float(x2), float(y2),
                        float(c), str(names[int(cls)])))
        return out


def _find_local_yolov5_repo(model_path: str, max_up: int = 4) -> str | None:
    """从模型文件目录向上找本地 yolov5 仓库根目录（同时含 hubconf.py/models/utils）。

    例如模型在 .../dadishu_yolo/v5-7.0/yolov5s_best.pt，v5-7.0 若是 yolov5
    v7.0 仓库检出（含 hubconf.py），直接用它离线加载，无需联网拉取。
    最多向上 max_up 层，找不到返回 None。
    """
    d = os.path.dirname(os.path.abspath(model_path))
    for _ in range(max_up + 1):
        if (os.path.isfile(os.path.join(d, "hubconf.py"))
                and os.path.isdir(os.path.join(d, "models"))
                and os.path.isdir(os.path.join(d, "utils"))):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _find_cached_hub_repo(torch) -> str | None:
    """torch hub 缓存目录里已下载的 ultralytics/yolov5 仓库（此前 torch.hub.load 拉过）。

    用 _load_from_local_repo 直接加载它：不走 hubconf._create，跳过其中的
    check_requirements（会跑 pip 检查/装包，离线时卡死），也无需联网。
    """
    try:
        d = os.path.join(torch.hub.get_dir(), "ultralytics_yolov5_master")
    except Exception:
        return None
    if (os.path.isfile(os.path.join(d, "hubconf.py"))
            and os.path.isdir(os.path.join(d, "models"))
            and os.path.isdir(os.path.join(d, "utils"))):
        return d
    return None


def _load_model(model_path: str, device: str):
    """加载模型（按 路径+设备 缓存）。

    .onnx 直接走 onnxruntime 后端（无需 torch）；
    .pt 后端顺序：ultralytics（YOLOv8+，适合新版/官方模型）→ yolov5 原生仓库
    （本地仓库优先、torch.hub 兜底，适合 yolov5 v5~v7 训练的旧模型——
    ultralytics 对这类模型不向前兼容）。.pt 两个后端都失败时合并报错，
    同目录有同名 .onnx 时附提示（改用 onnx 可绕过兼容问题且无需 torch）。
    """
    path = (model_path or "").strip()
    if not path:
        raise YoloError("未设置模型路径")
    if not os.path.isfile(path):
        raise YoloError(f"模型文件不存在：{path}")
    key = (os.path.abspath(path), (device or "cuda").strip().lower())
    if key in _models:
        return _models[key]

    if os.path.splitext(path)[1].lower() == ".onnx":
        dev = (device or "cuda").strip().lower()
        if dev not in ("cpu", "cuda"):
            raise YoloError(f"推理设备无效：「{device}」（仅支持 cpu / cuda）")
        model = _OnnxModel(path, dev)
        _models[key] = model
        return model

    torch = _import_torch()
    device = _check_device(torch, device)
    errors = []
    try:
        model = _UltralyticsModel(path, device)
    except Exception as e:     # 含未安装(ImportError)与旧模型不兼容(TypeError)
        errors.append(f"ultralytics 后端：{type(e).__name__}: {e}")
    else:
        _models[key] = model
        return model
    try:
        model = _HubModel(torch, path, device)
    except Exception as e:
        errors.append(f"yolov5 仓库后端：{type(e).__name__}: {e}")
    else:
        _models[key] = model
        return model
    hint = ""
    sib = os.path.splitext(path)[0] + ".onnx"
    if os.path.isfile(sib):
        hint = (f"\n检测到同目录存在 {os.path.basename(sib)}：把模型路径改为该 onnx 文件，"
                "只需 pip install onnxruntime 即可离线运行（无需 torch/ultralytics/yolov5 仓库）")
    raise YoloError(
        "模型加载失败（两种后端都未成功）：\n· " + "\n· ".join(errors) + "\n"
        "提示：yolov5 v5~v7 训练的旧模型，可①把 yolov5 仓库放到模型同目录或上级目录"
        "（含 hubconf.py）离线加载；②联网由 torch.hub 自动拉取仓库；"
        "③用 yolov5 export.py 导出 .onnx 后改用 onnx 文件（推荐，最轻量）" + hint)


def clear_model_cache() -> None:
    """清空模型缓存（测试或模型文件更新后用）。"""
    _models.clear()


# ---------------- 检测 ----------------

def detect(model_path: str, region: str = "", classes: str = "",
           confidence: float = 0.5, device: str = "cuda") -> list:
    """抓屏并做 YOLOv5 目标检测，返回检测结果列表（按置信度从高到低）。

    每项：{"class": 类别名, "confidence": 置信度, "region": "左上x,左上y,右下x,右下y"}
    坐标为虚拟桌面物理像素（与找图/截图同一坐标系）。

    region="x,y,w,h"，空=全屏；完全落在屏幕外抛 YoloError，部分越界自动裁剪。
    classes 逗号分隔类别名过滤（空=全部类别）；confidence 为置信度阈值。
    前置条件不满足（路径/依赖/设备/区域）抛 YoloError，消息面向用户。
    """
    from . import finder
    from .config import parse_region_str

    model = _load_model(model_path, device)

    screen = finder.grab_full_screen()
    sh, sw = screen.shape[:2]
    r = parse_region_str(region)
    if r is None:
        sub, ox, oy = screen, 0, 0
    else:
        x, y, w, h = r
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(sw, x + w), min(sh, y + h)
        if x2 <= x1 or y2 <= y1:
            raise YoloError(
                f"检测区域超出屏幕范围：区域 ({x},{y},{w},{h})，"
                f"当前屏幕 {sw}x{sh}，请重新框选或修正坐标")
        sub = np.ascontiguousarray(screen[y1:y2, x1:x2])
        ox, oy = x1, y1

    raw = model.predict(sub, float(confidence))
    wanted = {c.strip() for c in (classes or "").replace("，", ",").split(",")
              if c.strip()}
    results = []
    for bx1, by1, bx2, by2, conf, label in raw:
        if wanted and label not in wanted:
            continue
        gx1, gy1 = int(round(bx1)) + ox, int(round(by1)) + oy
        gx2, gy2 = int(round(bx2)) + ox, int(round(by2)) + oy
        results.append({"class": label,
                        "confidence": round(float(conf), 4),
                        "region": f"{gx1},{gy1},{gx2},{gy2}"})
    results.sort(key=lambda d: -d["confidence"])
    return results
