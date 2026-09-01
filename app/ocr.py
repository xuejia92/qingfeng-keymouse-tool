"""屏幕文字识别（RapidOCR / onnxruntime）。

- 用 RapidOCR（onnxruntime 推理版）跑 PP-OCR 模型：识别精度与 PaddleOCR 一致
  （同一个 PP-OCRv4 模型），但依赖只有 onnxruntime（约 50MB）+ 模型（约 15MB），
  远小于 paddlepaddle + torch（合计约 1GB），因此可以打进打包版 exe。
- 采用惰性导入：只有实际执行文字识别步骤时才加载。
- 识别区域使用虚拟桌面物理像素坐标（与 mss 抓屏、找图区域一致）。
- 结果格式：
  - multi_ocr=True：list[str]，每行一个识别文本
  - multi_ocr=False：把识别结果按行拼接为一个字符串
"""
from __future__ import annotations

import threading

import numpy as np

from .config import parse_region_str

_ocr = None
_ocr_lock = threading.Lock()
_import_error = ""


def is_available() -> tuple[bool, str]:
    """RapidOCR 是否可用。返回 (可用?, 不可用原因)。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True, ""
    except Exception as e:
        return False, (f"缺少 RapidOCR 库：pip install rapidocr_onnxruntime "
                       f"（需装到运行本程序的 Python 里）。详情：{e}")


def _get_ocr(lang: str = "ch"):
    """惰性创建 RapidOCR 单例。

    RapidOCR 默认内置 PP-OCRv4 中英混合模型，英文同样能识别，无需按 lang
    切换模型，故 lang 参数保留仅为兼容调用方，不实际区分。
    """
    global _ocr, _import_error
    with _ocr_lock:
        try:
            from rapidocr_onnxruntime import RapidOCR
            if _ocr is not None:
                return _ocr
            _ocr = RapidOCR()
            _import_error = ""
            return _ocr
        except Exception as e:
            _import_error = f"RapidOCR 初始化失败：{type(e).__name__}: {e}"
            raise


def grab_region(region: str) -> np.ndarray:
    """抓取指定屏幕区域（物理像素），空 region 表示全屏。返回 BGR ndarray。"""
    import mss
    with mss.mss() as sct:
        r = parse_region_str(region)
        if r is not None:
            x, y, w, h = r
            monitor = {"left": x, "top": y, "width": max(w, 1), "height": max(h, 1)}
        else:
            monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        img = np.asarray(shot)[:, :, :3]
        return np.ascontiguousarray(img)


def recognize(region: str = "", lang: str = "ch", multi_ocr: bool = True) -> tuple[bool, object, str]:
    """识别区域文字。

    返回 (成功?, 结果, 说明)。失败时结果可能为 None。
    成功结果：
      multi_ocr=True  -> list[str]
      multi_ocr=False -> str
    """
    ok, why = is_available()
    if not ok:
        return False, None, why
    try:
        img = grab_region(region)
        ocr = _get_ocr(lang)
        # RapidOCR 返回 (result, elapse)，result 为 [[box, text, score], ...] 或 None
        result, _elapse = ocr(img)
        lines: list[str] = _extract_texts(result)
        value: object = lines if multi_ocr else "\n".join(lines)
        return True, value, f"识别到 {len(lines)} 行文字"
    except Exception as e:
        return False, None, f"文字识别失败：{type(e).__name__}: {e}"


def _extract_texts(result) -> list[str]:
    """从 RapidOCR 结果里提取文本行。

    RapidOCR 返回 [[box, text, score], ...]：box 为 4 点坐标、text 为字符串、
    score 为置信度。这里只取每项的 text（index 1）。
    """
    lines: list[str] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text = item[1]
        if isinstance(text, (list, tuple)):
            text = str(text[0]) if text else ""
        text = str(text).strip()
        if text:
            lines.append(text)
    return lines


def shutdown() -> None:
    """释放 RapidOCR 占用的模型内存（程序退出时可选调用）。"""
    global _ocr
    with _ocr_lock:
        _ocr = None
