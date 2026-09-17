"""Unicode 安全的图片读写（cv2 在中文路径下会静默失败）。

**为什么必须有这个模块**

OpenCV 的 `imread` / `imwrite` 在 Windows 上用窄字符 `fopen` 打开文件，路径里
只要有非 ASCII 字符就打不开——而这个程序的目录就叫「清风自动化键鼠工具」，
于是「凡是走磁盘的图片读写」全军覆没，并且是**静默**失败：

    cv2.imwrite(r"D:\\program\\清风自动化键鼠工具\\templates\\jietu\\手动截图_x.png", img)
    # -> False，文件根本没生成（只在 stderr 打一行 OpenCV 警告）
    cv2.imread(同一路径)  # -> None

用户看到的现象就是「手动截图」提示「图片写入失败」。（实测 cv2 5.0.0 / ACP=936。）

**这里的做法**

- 路径是纯 ASCII：直接走 cv2 原生接口，行为与改造前完全一致（不引入回归）；
- 路径含非 ASCII：改成 `cv2.imencode` / `cv2.imdecode` 编解码 + Python 文件对象
  读写。Python 的 `open()` 走宽字符 API，中文路径毫无问题。

所有「按磁盘路径读写图片」的地方都要经过这里，不要再直接调 cv2.imread/imwrite。
"""
from __future__ import annotations

import os

import cv2
import numpy as np

__all__ = ["imread", "imwrite", "write_failure_reason"]


def _as_path(path) -> str:
    """把入参规整成路径字符串；空值/非法值返回 ""。"""
    if not path:
        return ""
    try:
        return os.fspath(path)
    except TypeError:
        return ""


def imread(path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """读图片，失败返回 None（与 cv2.imread 同语义，但支持中文路径）。"""
    p = _as_path(path)
    if not p or not os.path.isfile(p):
        return None
    if p.isascii():
        try:
            return cv2.imread(p, flags)
        except cv2.error:
            return None
    try:
        buf = np.fromfile(p, dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, flags)
    except (OSError, ValueError):
        return None


def imwrite(path, img: np.ndarray, params=None) -> bool:
    """写图片，成功 True / 失败 False（与 cv2.imwrite 同语义，但支持中文路径）。

    params 与 cv2.imwrite 的第三参数一致（如 [cv2.IMWRITE_JPEG_QUALITY, 90]）。
    """
    p = _as_path(path)
    if not p or img is None:
        return False
    params = params or []

    # 快路：纯 ASCII 路径直接交给 cv2，保持原有编码行为
    if p.isascii():
        try:
            if cv2.imwrite(p, img, params):
                return True
        except cv2.error:
            pass        # 落到下面的兜底再试一次（如扩展名不被 cv2.imwrite 支持）

    # 兜底（也是中文路径的必经之路）：自己编码 + Python 文件对象落盘
    ext = os.path.splitext(p)[1].lower() or ".png"
    try:
        ok, buf = cv2.imencode(ext, img, params)
        if not ok or buf is None:
            return False
        with open(p, "wb") as f:        # open() 走宽字符 API，中文路径没问题
            f.write(buf.tobytes())
        return True
    except (OSError, ValueError, cv2.error):
        return False


def write_failure_reason(path) -> str:
    """写盘失败时给用户看的一句话原因（尽力而为的推测，不保证准确）。"""
    p = _as_path(path)
    if not p:
        return "未指定保存路径"
    full = os.path.abspath(p)
    folder = os.path.dirname(full)
    if not os.path.isdir(folder):
        return f"目录不存在：{folder}"
    if not os.access(folder, os.W_OK):
        return f"目录不可写（权限不足或被占用）：{folder}"
    if len(full) > 255:
        return "路径过长（接近 Windows 的 260 字符上限）"
    return "可能是磁盘空间不足、文件被其它程序占用或没有写入权限"
