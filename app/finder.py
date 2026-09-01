"""屏幕抓取与 OpenCV 模板匹配找图。"""
from __future__ import annotations

import threading

import cv2
import numpy as np

_local = threading.local()  # mss 实例必须绑定创建它的线程


def _get_sct():
    if not hasattr(_local, "sct"):
        import mss
        _local.sct = mss.mss()
    return _local.sct


def grab_full_screen() -> np.ndarray:
    """抓取整个虚拟桌面，返回 BGR ndarray。"""
    sct = _get_sct()
    shot = sct.grab(sct.monitors[0])  # monitors[0] = 全部显示器的虚拟屏
    img = np.asarray(shot)[:, :, :3]  # BGRA -> BGR
    return np.ascontiguousarray(img)


def locate(template_bgr: np.ndarray, screen_bgr: np.ndarray, confidence: float):
    """在屏幕图中匹配模板。

    返回 (center_x, center_y, score)（屏幕图像坐标系），未达阈值返回 None。
    """
    th, tw = template_bgr.shape[:2]
    sh, sw = screen_bgr.shape[:2]
    if th <= 0 or tw <= 0 or th > sh or tw > sw:
        return None
    res = cv2.matchTemplate(screen_bgr, template_bgr, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val < confidence:
        return None
    cx = max_loc[0] + tw // 2
    cy = max_loc[1] + th // 2
    return cx, cy, float(max_val)


def locate_in_region(template_bgr: np.ndarray, screen_bgr: np.ndarray,
                     confidence: float, region: tuple[int, int, int, int]):
    """限定区域内匹配。region=(x,y,w,h) 为屏幕图像素坐标（虚拟桌面物理像素）。

    返回全局坐标 (center_x, center_y, score)，未命中返回 None。
    """
    x, y, w, h = region
    sh, sw = screen_bgr.shape[:2]
    x, y = max(0, int(x)), max(0, int(y))
    w, h = min(int(w), sw - x), min(int(h), sh - y)
    if w <= 0 or h <= 0:
        return None
    sub = np.ascontiguousarray(screen_bgr[y:y + h, x:x + w])
    hit = locate(template_bgr, sub, confidence)
    if hit is None:
        return None
    return hit[0] + x, hit[1] + y, hit[2]


def load_template(path: str) -> np.ndarray | None:
    """读取模板图，失败返回 None。"""
    try:
        return cv2.imread(path, cv2.IMREAD_COLOR)
    except Exception:
        return None
