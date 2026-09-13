"""「图片悬浮」模块的测试。

这个模块与流程里其它步骤最大的不同是**异步**：执行时只在桌面最前端贴一张图，
然后**立刻返回**，后续步骤照常往下跑；图片一直留着，直到用户手动关闭。

因此测试分两层：

1. 悬浮窗本体（app/overlay_actor.py）——窗口能不能建出来、能不能被关掉、
   重复悬浮会不会叠窗、缩放/定位是否按配置生效、窗口生命周期是否被模块级
   字典稳稳持有（GC 收走就会「一闪就没」）。
2. 执行器（app/tasks.run_float_image_step）——只经一次 ui_call 回主线程建窗、
   **不阻塞等待用户关闭**、图片缺失/加载失败/手动停止等错误分支。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import (QEnterEvent, QKeyEvent, QMouseEvent, QPixmap,
                           QWheelEvent)
from PySide6.QtWidgets import QApplication

import app.config as config_mod
import app.overlay_actor as overlay_actor
import app.screenshot_actor as shot_actor
import app.tasks as tasks_mod
from app.overlay_actor import (EDGE_MARGIN, POSITIONS, FloatingImage,
                               close_all, close_image, live_count, show_image)
from tests._env import TempConfigPaths


def _make_png(path: str, w: int = 40, h: int = 30, color=Qt.red) -> str:
    pm = QPixmap(w, h)
    pm.fill(color)
    if not pm.save(path, "PNG"):
        raise RuntimeError(f"测试用图片写盘失败：{path}")
    return path


def _press(win, gx, gy):
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(gx, gy), QPointF(gx, gy),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    win.mousePressEvent(ev)


def _move(win, gx, gy):
    ev = QMouseEvent(QEvent.MouseMove, QPointF(gx, gy), QPointF(gx, gy),
                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    win.mouseMoveEvent(ev)


def _release(win, gx, gy):
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(gx, gy), QPointF(gx, gy),
                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    win.mouseReleaseEvent(ev)


class _QtTestCase(unittest.TestCase):
    """所有用例共享一个 QApplication。

    QPixmap 在没有 QGuiApplication 时构造会直接让进程 abort（不是抛异常，
    测试根本拿不到 traceback），所以哪怕只是「造一张测试图片」也必须先有 app。
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])


class TestFloatingImageWidget(_QtTestCase):
    """悬浮窗本体。"""

    def setUp(self):
        close_all()
        self.tmp = tempfile.mkdtemp(prefix="float_image_test_")
        self.png = _make_png(os.path.join(self.tmp, "a.png"))

    def tearDown(self):
        close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 显示 / 注册 ----------
    def test_show_returns_window_and_registers_it(self):
        win = show_image(self.png)
        self.assertIsNotNone(win)
        self.assertIsInstance(win, FloatingImage)
        self.assertTrue(win.isVisible())
        self.assertEqual(live_count(), 1)

    def test_show_is_frontmost_and_frameless(self):
        """必须无边框 + 置顶，否则「悬浮在桌面最前端」无从谈起。"""
        win = show_image(self.png)
        flags = win.windowFlags()
        self.assertTrue(flags & Qt.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)

    def test_shown_image_matches_file_size(self):
        win = show_image(self.png)
        self.assertEqual((win.width(), win.height()), (40, 30))

    def test_show_returns_immediately_without_waiting(self):
        """异步：调用返回时窗口已可见，但没有任何等待/事件循环。"""
        win = show_image(self.png)
        self.assertTrue(win.isVisible())
        # 没有跑事件循环，窗口仍然“在”——说明 show 只负责建窗后立即交还控制权

    def test_empty_path_is_rejected(self):
        self.assertIsNone(show_image(""))
        self.assertEqual(live_count(), 0)

    def test_missing_file_is_rejected(self):
        self.assertIsNone(show_image(os.path.join(self.tmp, "nope.png")))
        self.assertEqual(live_count(), 0)

    def test_non_image_file_is_rejected(self):
        bad = os.path.join(self.tmp, "not_an_image.png")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("hello")
        self.assertIsNone(show_image(bad))
        self.assertEqual(live_count(), 0)

    # ---------- 重复与关闭 ----------
    def test_repeat_show_replaces_instead_of_stacking(self):
        first = show_image(self.png)
        second = show_image(self.png)
        self.assertEqual(live_count(), 1, "同一张图片不应在桌面上叠出多个窗口")
        self.assertIsNot(first, second)
        self.assertFalse(first.isVisible())

    def test_two_different_images_coexist(self):
        other = _make_png(os.path.join(self.tmp, "b.png"), color=Qt.blue)
        show_image(self.png)
        show_image(other)
        self.assertEqual(live_count(), 2)

    def test_close_image_removes_only_that_one(self):
        other = _make_png(os.path.join(self.tmp, "b.png"), color=Qt.blue)
        show_image(self.png)
        show_image(other)
        close_image(self.png)
        self.assertEqual(live_count(), 1)

    def test_close_image_on_unknown_path_is_noop(self):
        close_image(os.path.join(self.tmp, "unknown.png"))   # 不应抛异常
        close_image("")
        self.assertEqual(live_count(), 0)

    def test_close_all_clears_everything(self):
        show_image(self.png)
        show_image(_make_png(os.path.join(self.tmp, "b.png"), color=Qt.blue))
        close_all()
        self.assertEqual(live_count(), 0)

    def test_widget_close_unregisters_itself(self):
        """用户手动关窗（不走 close_image）也要从注册表里摘掉。"""
        win = show_image(self.png)
        win.close()
        self._app.processEvents()     # WA_DeleteOnClose 是延迟删除，需要转一圈
        self.assertEqual(live_count(), 0)

    # ---------- 关闭入口 ----------
    def test_close_button_hidden_until_hover(self):
        win = show_image(self.png)
        self.assertFalse(win.close_btn.isVisible())
        win.enterEvent(QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        self.assertTrue(win.close_btn.isVisible())

    def test_close_button_closes_window(self):
        win = show_image(self.png)
        win.close_btn.click()
        self.assertFalse(win.isVisible())

    def test_escape_closes_window(self):
        win = show_image(self.png)
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertFalse(win.isVisible())

    # ---------- 单击 / 拖动 ----------
    def test_click_closes_when_click_to_close_enabled(self):
        win = show_image(self.png, pos="custom", x=100, y=100, click_to_close=True)
        _press(win, 110, 110)
        _release(win, 110, 110)
        self.assertFalse(win.isVisible())

    def test_click_does_not_close_when_disabled(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        _press(win, 110, 110)
        _release(win, 110, 110)
        self.assertTrue(win.isVisible())

    def test_drag_moves_window_and_never_closes_it(self):
        """拖拽 = 移动窗口；即使开了「单击即关闭」，拖动后松手也不能关。"""
        win = show_image(self.png, pos="custom", x=100, y=100, click_to_close=True)
        _press(win, 110, 110)
        _move(win, 160, 160)
        _release(win, 160, 160)
        self.assertTrue(win.isVisible(), "拖动不应触发关闭")
        self.assertEqual(win.pos(), QPoint(150, 150))

    # ---------- 缩放 / 定位 ----------
    def test_scale_percent_is_applied(self):
        win = show_image(self.png, scale=200)
        self.assertEqual((win.width(), win.height()), (80, 60))

    def test_scale_is_clamped_to_10_400(self):
        small = overlay_actor._apply_scale(QPixmap(40, 30), 0)
        self.assertEqual(small.width(), 4)          # 0% -> 下限 10%
        big = overlay_actor._apply_scale(QPixmap(40, 30), 1000)
        self.assertEqual(big.width(), 160)          # 1000% -> 上限 400%

    def test_apply_scale_100_returns_same_pixmap(self):
        pm = QPixmap(40, 30)
        self.assertEqual(overlay_actor._apply_scale(pm, 100).cacheKey(), pm.cacheKey())

    def test_apply_scale_tolerates_garbage(self):
        pm = QPixmap(40, 30)
        for bad in (None, "", "abc"):
            self.assertEqual(overlay_actor._apply_scale(pm, bad).cacheKey(), pm.cacheKey())

    def test_fit_to_screen_shrinks_oversized_image(self):
        """比屏幕还大的图必须等比缩小，否则连关闭按钮都点不到。"""
        area = overlay_actor._available_area()
        if area is None:
            self.skipTest("离屏环境没有可用屏幕")
        big = QPixmap(area.width() + 600, 80)
        fitted = overlay_actor._fit_to_screen(big)
        self.assertLessEqual(fitted.width(), area.width() - EDGE_MARGIN * 2)

    def test_fit_to_screen_keeps_small_image_untouched(self):
        pm = QPixmap(40, 30)
        self.assertEqual(overlay_actor._fit_to_screen(pm).cacheKey(), pm.cacheKey())

    def test_custom_position_is_exact(self):
        win = show_image(self.png, pos="custom", x=123, y=456)
        self.assertEqual(win.pos(), QPoint(123, 456))

    def test_pos_for_custom(self):
        win = show_image(self.png)
        self.assertEqual(overlay_actor._pos_for(win, "custom", 7, 8), (7, 8))

    def test_pos_for_custom_falls_back_when_coords_bad(self):
        """custom 但坐标非法时不能崩，退回默认角落。"""
        win = show_image(self.png)
        got = overlay_actor._pos_for(win, "custom", None, None)
        area = overlay_actor._available_area()
        if area is not None:
            self.assertEqual(got[0], area.right() - win.width() - EDGE_MARGIN + 1)

    def test_predefined_positions_inside_screen(self):
        area = overlay_actor._available_area()
        if area is None:
            self.skipTest("离屏环境没有可用屏幕")
        for key, _label in POSITIONS:
            if key == "custom":
                continue
            win = show_image(self.png, pos=key)
            x, y = win.x(), win.y()
            self.assertGreaterEqual(x, area.left())
            self.assertGreaterEqual(y, area.top())
            self.assertLessEqual(x + win.width(), area.right() + 1)
            self.assertLessEqual(y + win.height(), area.bottom() + 1)

    def test_positions_tuple_covers_documented_keys(self):
        keys = [k for k, _ in POSITIONS]
        self.assertEqual(keys, ["right_bottom", "right_top", "left_top",
                                "left_bottom", "center", "custom"])

    def test_center_position_is_centered(self):
        area = overlay_actor._available_area()
        if area is None:
            self.skipTest("离屏环境没有可用屏幕")
        win = show_image(self.png, pos="center")
        self.assertAlmostEqual(win.x() + win.width() // 2, area.center().x(), delta=1)
        self.assertAlmostEqual(win.y() + win.height() // 2, area.center().y(), delta=1)


class TestFloatingImageZoom(_QtTestCase):
    """运行时手动缩放：默认原图尺寸、滚轮与快捷键、锚点、边界、缩到超大后仍可关闭。"""

    def setUp(self):
        close_all()
        self.tmp = tempfile.mkdtemp(prefix="float_zoom_")
        self.png = _make_png(os.path.join(self.tmp, "a.png"), 200, 100)

    def tearDown(self):
        close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wheel(self, win, direction, *, cursor=(150, 150)):
        """direction>0 = 向上滚（放大），<0 = 向下滚（缩小）。"""
        ev = QWheelEvent(QPointF(*cursor), QPointF(*cursor), QPoint(0, 0),
                         QPoint(0, 120 if direction > 0 else -120),
                         Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
        win.wheelEvent(ev)

    def _key(self, win, key):
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))

    # ---------- 默认尺寸 ----------
    def test_default_keeps_the_original_size(self):
        """默认就是原图尺寸，不放大也不缩小。"""
        win = show_image(self.png)
        self.assertEqual(win.scale_pct, 100)
        self.assertEqual((win.width(), win.height()), (200, 100))

    def test_small_image_is_not_stretched_to_fill(self):
        """原图尺寸 ≠ 铺满屏幕：小图不能被拉伸。"""
        small = _make_png(os.path.join(self.tmp, "s.png"), 12, 8)
        win = show_image(small)
        self.assertEqual((win.width(), win.height()), (12, 8))

    def test_oversized_image_starts_fitted(self):
        """比屏幕还大的图，初始先收敛到能完整显示（否则 ✕ 够不着）。"""
        big = _make_png(os.path.join(self.tmp, "big.png"), 2000, 1000)
        win = show_image(big)
        area = overlay_actor._available_area()
        self.assertLess(win.scale_pct, 100)
        self.assertLessEqual(win.width(), area.width() - EDGE_MARGIN * 2)
        self.assertLessEqual(win.height(), area.height() - EDGE_MARGIN * 2)

    # ---------- 滚轮 ----------
    def test_wheel_up_zooms_in(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        self._wheel(win, +1)
        self.assertEqual(win.scale_pct, 110)
        self.assertEqual((win.width(), win.height()), (220, 110))

    def test_wheel_down_zooms_out(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        self._wheel(win, -1)
        self.assertEqual(win.scale_pct, 90)
        self.assertEqual((win.width(), win.height()), (180, 90))

    def test_wheel_without_delta_is_ignored(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        ev = QWheelEvent(QPointF(150, 150), QPointF(150, 150), QPoint(0, 0),
                         QPoint(0, 0), Qt.NoButton, Qt.NoModifier,
                         Qt.ScrollUpdate, False)
        win.wheelEvent(ev)
        self.assertEqual(win.scale_pct, 100)

    def test_zoom_is_clamped_to_the_limits(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        for _ in range(40):
            self._wheel(win, +1)
        self.assertEqual(win.scale_pct, overlay_actor.MAX_SCALE)
        for _ in range(60):
            self._wheel(win, -1)
        self.assertEqual(win.scale_pct, overlay_actor.MIN_SCALE)

    def test_zoom_anchors_on_the_cursor(self):
        """光标底下那块内容，缩放前后应停在原地。"""
        win = show_image(self.png, pos="custom", x=100, y=100)
        self._wheel(win, +1, cursor=(150, 150))
        # 窗口左上角按 光标局部坐标 ×(1-1/1.1) 反向平移
        self.assertEqual((win.x(), win.y()), (95, 95))

    def test_zoom_in_then_out_returns_to_the_original_size(self):
        """缩放必须基于原图重算，不能在上一次结果上再缩（否则糊图 + 误差累积）。"""
        win = show_image(self.png, pos="custom", x=100, y=100)
        for _ in range(5):
            self._wheel(win, +1, cursor=(100, 100))
        for _ in range(5):
            self._wheel(win, -1, cursor=(100, 100))
        self.assertEqual(win.scale_pct, 100)
        self.assertEqual((win.width(), win.height()), (200, 100))

    # ---------- 百分比浮标 ----------
    def test_badge_is_hidden_at_start(self):
        win = show_image(self.png)
        self.assertFalse(win.zoom_badge.isVisible())

    def test_badge_shows_the_current_percentage(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        self._wheel(win, +1)
        self.assertTrue(win.zoom_badge.isVisible())
        self.assertEqual(win.zoom_badge.text(), "110%")

    # ---------- 快捷键 ----------
    def test_keyboard_shortcuts(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        self._key(win, Qt.Key_Plus)
        self.assertEqual(win.scale_pct, 110)
        self._key(win, Qt.Key_Minus)
        self.assertEqual(win.scale_pct, 100)
        self._key(win, Qt.Key_Equal)        # 「=」同键位，不按 Shift 也能放大
        self.assertEqual(win.scale_pct, 110)
        self._key(win, Qt.Key_0)            # 一键回到原图尺寸
        self.assertEqual(win.scale_pct, 100)
        self.assertEqual((win.width(), win.height()), (200, 100))

    def test_escape_still_closes_after_zooming(self):
        win = show_image(self.png, pos="custom", x=100, y=100)
        self._wheel(win, +1)
        self._key(win, Qt.Key_Escape)
        self.assertFalse(win.isVisible())

    # ---------- 缩到比屏幕还大之后 ----------
    def test_close_button_stays_on_screen_when_zoomed_beyond_it(self):
        """放大到超出屏幕后 ✕ 仍要留在屏内，否则用户只能靠 Esc 关。"""
        wide = _make_png(os.path.join(self.tmp, "wide.png"), 300, 100)
        win = show_image(wide, pos="custom", x=0, y=0)
        for _ in range(30):
            self._wheel(win, +1, cursor=(400, 400))
        area = overlay_actor._available_area()
        self.assertGreater(win.width(), area.width(), "构造条件：窗口应已宽过屏幕")
        self.assertLessEqual(win.x() + win.width() - 4, area.right() + 1,
                             "✕ 被挤到屏幕右边外面了")
        self.assertLessEqual(win.y() + 4, area.bottom() + 1)


class TestFloatImageZoomHelpers(_QtTestCase):
    """缩放相关的纯函数。"""

    def test_clamp_scale(self):
        self.assertEqual(overlay_actor._clamp_scale(100), 100)
        self.assertEqual(overlay_actor._clamp_scale(110.6), 111)
        self.assertEqual(overlay_actor._clamp_scale(0), overlay_actor.MIN_SCALE)
        self.assertEqual(overlay_actor._clamp_scale(9999), overlay_actor.MAX_SCALE)
        for bad in ("abc", None, ""):
            self.assertEqual(overlay_actor._clamp_scale(bad), overlay_actor.DEFAULT_SCALE)

    def test_initial_pct_keeps_100_for_images_that_fit(self):
        self.assertEqual(overlay_actor._initial_pct(QPixmap(40, 30), 100), 100)

    def test_initial_pct_respects_an_explicit_scale(self):
        self.assertEqual(overlay_actor._initial_pct(QPixmap(40, 30), 200), 200)

    def test_initial_pct_shrinks_an_oversized_image(self):
        area = overlay_actor._available_area()
        big = QPixmap(area.width() * 2, area.height())
        pct = overlay_actor._initial_pct(big, 100)
        self.assertLess(pct, 100)
        self.assertLessEqual(big.width() * pct // 100,
                             area.width() - EDGE_MARGIN * 2)

    def test_initial_pct_never_goes_below_the_minimum(self):
        """极端长条图（连 10% 都放不下）收敛到下限为止，不返回 0。"""
        tall = QPixmap(50, 8000)
        self.assertEqual(overlay_actor._initial_pct(tall, 100),
                         overlay_actor.MIN_SCALE)


class TestFloatImageHighDpi(_QtTestCase):
    """高分屏（Windows 显示缩放 125%/150%）下图片必须 1:1 清晰，不被 Qt 拉伸。

    根因回顾：QPixmap 的 devicePixelRatio 默认为 1，Qt 便认定它「是给 100% 屏
    用的图」，于是在 150% 的屏上把它放大 1.5 倍去绘制——每个图片像素被插值成
    1.5 个屏幕像素，看起来就是糊的。修法是：给渲染用的 pixmap 打上真实 dpr，
    并把窗口/标签尺寸按 deviceIndependentSize（= 图片像素 / dpr）折算成逻辑像素。

    离屏测试环境的 dpr 恒为 1.0，所以这里用 mock 把 dpr 顶成 1.5 / 2.0 验行为。
    """

    def setUp(self):
        close_all()
        self._wins = []

    def tearDown(self):
        for win in self._wins:
            try:
                win.close()
            except RuntimeError:
                pass
        close_all()

    # ---------- 纯函数 ----------

    def test_screen_dpr_is_a_positive_float(self):
        dpr = overlay_actor._screen_dpr()
        self.assertIsInstance(dpr, float)
        self.assertGreater(dpr, 0)

    def test_display_size_folds_in_the_pixel_ratio(self):
        pm = QPixmap(200, 100)
        self.assertEqual(overlay_actor._display_size(pm, 100, 1.0), QSize(200, 100))
        # 2 倍屏：200 个图片像素只占 100 个逻辑像素
        self.assertEqual(overlay_actor._display_size(pm, 100, 2.0), QSize(100, 50))
        # 缩放与 dpr 叠加：50% × 2 倍屏 → 200*0.5/2 = 50
        self.assertEqual(overlay_actor._display_size(pm, 50, 2.0), QSize(50, 25))
        # 非法比例退回默认值（原图尺寸）
        self.assertEqual(overlay_actor._display_size(pm, "bad", 1.0),
                         QSize(200, 100))

    def test_fit_to_screen_compares_logical_size_on_high_dpi(self):
        """1500 设备像素的图在 2 倍屏上只占 750 逻辑像素，不该被误判成超屏。"""
        area = overlay_actor._available_area()
        pm = QPixmap(area.width() * 3 // 2, 100)
        with mock.patch.object(overlay_actor, "_screen_dpr", return_value=2.0):
            self.assertIs(overlay_actor._fit_to_screen(pm), pm)

    def test_fit_to_screen_still_shrinks_a_genuinely_oversized_image(self):
        area = overlay_actor._available_area()
        pm = QPixmap(area.width() * 2, 100)
        with mock.patch.object(overlay_actor, "_screen_dpr", return_value=2.0):
            fitted = overlay_actor._fit_to_screen(pm)
        self.assertIsNot(fitted, pm)
        shown = overlay_actor._display_size(fitted, 100, 2.0)
        self.assertLessEqual(shown.width(), area.width() - EDGE_MARGIN * 2)

    def test_render_pixmap_carries_the_pixel_ratio(self):
        """交给 QLabel 的那张图必须带真实 dpr，否则 Qt 会把它拉伸着画。"""
        pm = QPixmap(200, 100)
        out = overlay_actor._render_pixmap(pm, 100, 1.5)
        self.assertEqual(out.devicePixelRatio(), 1.5)
        self.assertEqual(out.size(), QSize(200, 100))          # 设备像素不变
        self.assertEqual(out.deviceIndependentSize().toSize(),
                         QSize(round(200 / 1.5), round(100 / 1.5)))

    def test_render_pixmap_scales_and_stamps_together(self):
        out = overlay_actor._render_pixmap(QPixmap(200, 100), 200, 2.0)
        self.assertEqual(out.size(), QSize(400, 200))
        self.assertEqual(out.devicePixelRatio(), 2.0)
        self.assertEqual(out.deviceIndependentSize().toSize(), QSize(200, 100))

    def test_render_pixmap_does_not_mutate_the_source(self):
        """dpr 只打在副本上，_src 保持原样，否则之后再缩放会跑偏。"""
        pm = QPixmap(200, 100)
        overlay_actor._render_pixmap(pm, 100, 1.5)
        self.assertEqual(pm.devicePixelRatio(), 1.0)
        self.assertEqual(pm.size(), QSize(200, 100))

    # ---------- 窗口渲染 ----------

    def _show(self, w=200, h=100, **kw):
        with mock.patch.object(overlay_actor, "_screen_dpr", return_value=1.5):
            win = FloatingImage(QPixmap(w, h), **kw)
        self._wins.append(win)
        return win

    def test_window_dpr_comes_from_the_screen(self):
        win = self._show()
        self.assertEqual(win._dpr, 1.5)

    def test_window_is_sized_in_logical_pixels(self):
        """200 设备像素的图在 1.5 倍屏上只该占 133 逻辑像素宽。"""
        win = self._show(200, 100)
        want = QSize(round(200 / 1.5), round(100 / 1.5))
        self.assertEqual(win.size(), want)
        self.assertEqual(win.image_label.size(), want)

    def test_original_pixmap_is_not_mutated(self):
        win = self._show(200, 100)
        self.assertEqual(win._src.devicePixelRatio(), 1.0)
        self.assertEqual(win._src.size(), QSize(200, 100))

    def test_zoom_keeps_the_pixel_ratio_and_logical_sizing(self):
        win = self._show(200, 100)
        win._apply_zoom(200)
        self.assertEqual(win._scale_pct, 200)
        self.assertEqual(win._dpr, 1.5)              # 缩放不改屏的 dpr
        self.assertEqual(win.size(),
                         QSize(round(200 * 2 / 1.5), round(100 * 2 / 1.5)))

    def test_zoom_back_to_100_restores_the_original_box(self):
        win = self._show(200, 100)
        win._apply_zoom(300)
        win._apply_zoom(overlay_actor.DEFAULT_SCALE)
        self.assertEqual(win.size(), QSize(round(200 / 1.5), round(100 / 1.5)))
        self.assertEqual(win._src.devicePixelRatio(), 1.0)


class TestFloatImageExecutor(_QtTestCase):
    """执行器 run_float_image_step。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="float_step_test_")
        self.png = _make_png(os.path.join(self.tmp, "pic.png"))
        self.vars: dict = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, params, show_result=True, ui_error=None, stop=None):
        """跑一次步骤，返回 ((成功?, 原因), show_image_mock, ui_call_mock, resolve_mock)。

        resolve_template_path 用真实语义打桩：只有当 image / image_path 至少有一个
        非空时才解析出图片路径，否则返回空——这样「未选图片」分支才测得准。
        """
        resolver = lambda name, full: (self.png if (name or full) else "")
        ui_side = ui_error if ui_error else (lambda fn: fn())
        with mock.patch.object(tasks_mod, "resolve_template_path",
                               side_effect=resolver) as resolve, \
                mock.patch.object(overlay_actor, "show_image",
                                  return_value=(object() if show_result else None)) as show, \
                mock.patch.object(shot_actor, "ui_call", side_effect=ui_side) as ui:
            result = tasks_mod.run_float_image_step(params, self.vars, stop)
        return result, show, ui, resolve

    # ---------- 正常流程 ----------
    def test_success_reports_the_image_name(self):
        (ok, msg), show, ui, _res = self._run({"image": "pic.png"})
        self.assertTrue(ok, msg)
        self.assertIn("pic.png", msg)
        show.assert_called_once()

    def test_uses_ui_bridge_exactly_once(self):
        """建窗只能在主线程；且是异步的——只有这一次交互，不等用户关闭。"""
        _res, _show, ui, _r = self._run({"image": "pic.png"})
        self.assertEqual(ui.call_count, 1,
                         "悬浮是异步的：只应回主线程建一次窗，不应有等待用户关闭的第二次调用")

    def test_forwards_position_scale_and_click_to_close(self):
        _res, show, _ui, _r = self._run({
            "image": "pic.png", "position": "center", "scale": 150,
            "click_to_close": True})
        _args, kwargs = show.call_args
        self.assertEqual(kwargs["pos"], "center")
        self.assertEqual(kwargs["scale"], 150)
        self.assertTrue(kwargs["click_to_close"])

    def test_position_defaults_to_right_bottom(self):
        _res, show, _ui, _r = self._run({"image": "pic.png"})
        self.assertEqual(show.call_args.kwargs["pos"], "right_bottom")

    def test_bad_scale_falls_back_to_100(self):
        for bad in ("abc", None):
            _res, show, _ui, _r = self._run({"image": "pic.png", "scale": bad})
            self.assertEqual(show.call_args.kwargs["scale"], 100)

    def test_image_path_is_used_when_filename_absent(self):
        _res, _show, _ui, resolve = self._run({"image_path": self.png})
        resolve.assert_called_once_with("", self.png)

    def test_does_not_write_any_variable(self):
        """异步展示不产出变量，因此不应污染流程变量表。"""
        (ok, _msg), _show, _ui, _r = self._run({"image": "pic.png"})
        self.assertTrue(ok)
        self.assertEqual(self.vars, {})

    # ---------- 失败分支 ----------
    def test_no_image_configured_fails(self):
        (ok, msg), show, ui, _r = self._run({})
        self.assertFalse(ok)
        self.assertIn("未选择", msg)
        show.assert_not_called()
        ui.assert_not_called()

    def test_resolved_path_not_existing_fails(self):
        missing = os.path.join(self.tmp, "gone.png")
        with mock.patch.object(tasks_mod, "resolve_template_path", return_value=missing):
            ok, msg = tasks_mod.run_float_image_step({"image": "x.png"}, self.vars)
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_load_failure_fails(self):
        (ok, msg), _show, _ui, _r = self._run({"image": "pic.png"}, show_result=False)
        self.assertFalse(ok)
        self.assertIn("加载失败", msg)

    def test_ui_call_error_is_reported(self):
        (ok, msg), _show, _ui, _r = self._run(
            {"image": "pic.png"}, ui_error=RuntimeError("boom"))
        self.assertFalse(ok)
        self.assertIn("悬浮图片失败", msg)
        self.assertIn("RuntimeError", msg)

    def test_stop_before_running_fails_without_showing(self):
        stop = threading.Event()
        stop.set()
        (ok, msg), show, ui, _r = self._run({"image": "pic.png"}, stop=stop)
        self.assertFalse(ok)
        self.assertIn("停止", msg)
        show.assert_not_called()
        ui.assert_not_called()


class TestFloatImageDialog(_QtTestCase):
    """编辑对话框：表单 / 回填 / 保存三点必须闭环。

    这一段最容易「登记漏了」——表单建了但 _fill 没回填、或 apply_to 没写回，
    用户改完一保存就丢配置。所以用「建对话框 -> 回填 -> 改值 -> apply_to ->
    再建一个对话框读回」的方式整圈验证。
    """

    def _dialog(self, params):
        from app.config import FlowStep
        from app.ui.flow_dialog import StepParamsDialog
        step = FlowStep(type="float_image", params=params)
        return StepParamsDialog(step), step

    def test_form_is_filled_from_params(self):
        dlg, _step = self._dialog({
            "image": "tpl_a.png", "position": "custom", "x": 30, "y": 40,
            "scale": 150, "click_to_close": True})
        try:
            self.assertEqual(dlg.fi_pos.currentData(), "custom")
            self.assertEqual(dlg.fi_x.value(), 30)
            self.assertEqual(dlg.fi_y.value(), 40)
            self.assertEqual(dlg.fi_scale.value(), 150)
            self.assertTrue(dlg.fi_click_close.isChecked())
        finally:
            dlg.deleteLater()

    def test_custom_coords_enabled_only_for_custom_position(self):
        dlg, _ = self._dialog({"position": "right_bottom"})
        try:
            self.assertFalse(dlg._fi_xy_widget.isEnabled())
            dlg.fi_pos.setCurrentIndex(dlg.fi_pos.findData("custom"))
            self.assertTrue(dlg._fi_xy_widget.isEnabled())
        finally:
            dlg.deleteLater()

    def test_apply_to_writes_every_field_back(self):
        dlg, step = self._dialog({"image": "tpl_a.png"})
        try:
            dlg.fi_pos.setCurrentIndex(dlg.fi_pos.findData("center"))
            dlg.fi_scale.setValue(80)
            dlg.fi_click_close.setChecked(True)
            dlg.apply_to(step)
        finally:
            dlg.deleteLater()
        self.assertEqual(step.params["position"], "center")
        self.assertEqual(step.params["scale"], 80)
        self.assertTrue(step.params["click_to_close"])
        self.assertEqual(step.params["image"], "tpl_a.png")

    def test_apply_to_keeps_template_when_capture_not_redone(self):
        """没重新截图/上传时不能把已有图片写空（防呆）。"""
        dlg, step = self._dialog({"image": "tpl_a.png",
                                  "image_path": r"C:\tmp\tpl_a.png"})
        try:
            dlg.apply_to(step)
        finally:
            dlg.deleteLater()
        self.assertEqual(step.params["image"], "tpl_a.png")
        self.assertEqual(step.params["image_path"], r"C:\tmp\tpl_a.png")

    def test_accept_is_blocked_without_an_image(self):
        from app.ui import flow_dialog as fd
        dlg, _step = self._dialog({})
        try:
            with mock.patch.object(fd.QMessageBox, "warning") as warn:
                dlg.accept()
            warn.assert_called_once()
            self.assertNotEqual(dlg.result(), fd.QDialog.Accepted)
        finally:
            dlg.deleteLater()

    def test_image_edit_preview_accepts_a_real_path(self):
        """_fill 里会走 _update_preview；给了真实图片不应报错。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="float_dlg_")
        try:
            png = _make_png(os.path.join(tmp, "p.png"))
            dlg, _ = self._dialog({"image_path": png})
            try:
                self.assertEqual(dlg.fi_pos.currentData(), "right_bottom")
            finally:
                dlg.deleteLater()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 图片来源二选一 ----------
    def test_default_source_mode_is_template(self):
        dlg, _ = self._dialog({})
        try:
            self.assertTrue(dlg.fi_mode_tpl.isChecked())
            self.assertFalse(dlg.fi_mode_addr.isChecked())
            self.assertTrue(dlg._fi_form.isRowVisible(dlg._fi_tpl_rows[0]))
            self.assertFalse(dlg._fi_form.isRowVisible(dlg._fi_addr_rows[0]))
        finally:
            dlg.deleteLater()

    def test_switching_to_address_hides_the_template_row(self):
        dlg, _ = self._dialog({})
        try:
            dlg.fi_mode_addr.setChecked(True)
            self.assertFalse(dlg._fi_form.isRowVisible(dlg._fi_tpl_rows[0]))
            for row in dlg._fi_addr_rows:
                self.assertTrue(dlg._fi_form.isRowVisible(row))
            dlg.fi_mode_tpl.setChecked(True)
            self.assertTrue(dlg._fi_form.isRowVisible(dlg._fi_tpl_rows[0]))
            self.assertFalse(dlg._fi_form.isRowVisible(dlg._fi_addr_rows[0]))
        finally:
            dlg.deleteLater()

    def test_proxy_row_follows_its_checkbox(self):
        dlg, _ = self._dialog({"source_mode": "address"})
        try:
            dlg.fi_proxy_check.setChecked(False)
            self.assertFalse(dlg._fi_form.isRowVisible(dlg._fi_proxy_row))
            dlg.fi_proxy_check.setChecked(True)
            self.assertTrue(dlg._fi_form.isRowVisible(dlg._fi_proxy_row))
        finally:
            dlg.deleteLater()

    def test_address_round_trip(self):
        from app.ui.flow_dialog import StepParamsDialog
        dlg, step = self._dialog({"source_mode": "address"})
        try:
            dlg.fi_address.setText("$img_url")
            dlg.fi_timeout.setValue(25.0)
            dlg.fi_proxy_check.setChecked(False)
            dlg.fi_proxy.setText("1.2.3.4:8")
            dlg.apply_to(step)
        finally:
            dlg.deleteLater()
        self.assertEqual(step.params["source_mode"], "address")
        self.assertEqual(step.params["address"], "$img_url")
        self.assertEqual(step.params["timeout"], 25.0)
        self.assertFalse(step.params["use_proxy"])
        self.assertEqual(step.params["proxy"], "1.2.3.4:8")

        again = StepParamsDialog(step)          # 再打开应原样回填
        try:
            self.assertTrue(again.fi_mode_addr.isChecked())
            self.assertEqual(again.fi_address.text(), "$img_url")
            self.assertEqual(again.fi_timeout.value(), 25.0)
            self.assertFalse(again.fi_proxy_check.isChecked())
        finally:
            again.deleteLater()

    def test_switching_back_keeps_the_template_image(self):
        """两种来源的字段都要留着：切回模板图模式时，原来选的模板图不能丢。"""
        dlg, step = self._dialog({"image": "tpl_a.png"})
        try:
            dlg.fi_mode_addr.setChecked(True)
            dlg.fi_address.setText("https://a.com/x.png")
            dlg.apply_to(step)
        finally:
            dlg.deleteLater()
        self.assertEqual(step.params["image"], "tpl_a.png")
        self.assertEqual(step.params["address"], "https://a.com/x.png")

    def test_accept_is_blocked_without_an_address(self):
        from app.ui import flow_dialog as fd
        dlg, _ = self._dialog({"source_mode": "address"})
        try:
            with mock.patch.object(fd.QMessageBox, "warning") as warn:
                dlg.accept()
            warn.assert_called_once()
            self.assertNotEqual(dlg.result(), fd.QDialog.Accepted)
        finally:
            dlg.deleteLater()

    def test_accept_passes_with_an_address(self):
        from app.ui import flow_dialog as fd
        dlg, _ = self._dialog({"source_mode": "address",
                               "address": "https://a.com/x.png"})
        try:
            with mock.patch.object(fd.QMessageBox, "warning") as warn:
                dlg.accept()
            warn.assert_not_called()
            self.assertEqual(dlg.result(), fd.QDialog.Accepted)
        finally:
            dlg.deleteLater()

    def test_insert_variable_button_writes_a_reference(self):
        """「插入变量」应把 $变量名 插到地址光标处，然后复位下拉。"""
        from app.ui.flow_dialog import StepParamsDialog
        with mock.patch.object(StepParamsDialog, "_flow_var_names",
                               return_value=["img_url"]):
            dlg, _ = self._dialog({"source_mode": "address"})
        try:
            self.assertEqual(dlg.fi_addr_var.count(), 2)   # 占位项 + img_url
            dlg.fi_address.setText("A")
            dlg.fi_address.setCursorPosition(1)
            dlg.fi_addr_var.setCurrentIndex(1)
            self.assertEqual(dlg.fi_address.text(), "A$img_url")
            self.assertEqual(dlg.fi_addr_var.currentIndex(), 0, "插入后应复位到占位项")
        finally:
            dlg.deleteLater()

    def test_insert_variable_is_disabled_without_flow_variables(self):
        dlg, _ = self._dialog({"source_mode": "address"})
        try:
            self.assertFalse(dlg.fi_addr_var.isEnabled())
            self.assertEqual(dlg.fi_addr_var.currentData(), "")
        finally:
            dlg.deleteLater()


class TestFloatImageRegistration(unittest.TestCase):
    """配置 / 面板 / 对话框的登记点是否齐全（漏一处模块就露不出来）。"""

    def test_step_type_registered(self):
        from app.config import FLOW_STEP_TYPES
        self.assertEqual(FLOW_STEP_TYPES.get("float_image"), "图片悬浮")

    def test_default_params_are_json_safe(self):
        from app.config import default_step_params
        p = default_step_params("float_image")
        json.dumps(p)     # 不可序列化会直接抛错
        for key in ("source_mode", "image", "image_path", "address", "timeout",
                    "use_proxy", "proxy", "position", "x", "y",
                    "scale", "click_to_close"):
            self.assertIn(key, p)
        self.assertEqual(p["source_mode"], "template")
        self.assertEqual(p["position"], "right_bottom")

    def test_is_not_a_variable_producing_step(self):
        """异步展示不写变量，所以不该出现在 STEP_OUTPUT_FIELDS 里。"""
        from app.config import STEP_OUTPUT_FIELDS
        self.assertNotIn("float_image", STEP_OUTPUT_FIELDS)

    def test_summary_mentions_image_and_async(self):
        from app.config import FlowStep
        step = FlowStep(type="float_image",
                        params={"image": "tpl_x.png", "position": "center"})
        text = step.summary()
        self.assertIn("tpl_x.png", text)
        self.assertIn("异步", text)

    def test_summary_without_image_still_renders(self):
        from app.config import FlowStep
        step = FlowStep(type="float_image", params={})
        self.assertIn("图片悬浮", step.summary())

    def test_summary_shows_the_address_in_address_mode(self):
        from app.config import FlowStep
        step = FlowStep(type="float_image",
                        params={"source_mode": "address",
                                "address": "$img_url", "image": "tpl.png"})
        text = step.summary()
        self.assertIn("$img_url", text)
        self.assertNotIn("tpl.png", text, "地址模式下不该再显示模板图名")

    def test_summary_truncates_a_long_address(self):
        from app.config import FlowStep
        step = FlowStep(type="float_image",
                        params={"source_mode": "address",
                                "address": "https://example.com/" + "x" * 80 + ".png"})
        text = step.summary()
        self.assertIn("…", text)
        self.assertLess(len(text), 60)

    def test_listed_in_app_web_group(self):
        from app.ui.flow_tab import MODULE_GROUPS
        groups = {gid: types for gid, _, types in MODULE_GROUPS}
        self.assertIn("float_image", groups["app_web"])

    def test_has_a_dialog_icon(self):
        from app.ui.flow_dialog import _TYPE_ICONS
        self.assertTrue(_TYPE_ICONS.get("float_image"))

    def test_dispatched_by_the_runner(self):
        """FlowRunner 必须认这个类型，否则流程跑到这里会报未知步骤。"""
        import inspect
        import app.flows as flows_mod
        src = inspect.getsource(flows_mod)
        self.assertIn('step.type == "float_image"', src)


class TestFloatImageAddressResolve(_QtTestCase):
    """「图片地址」的本地路径解析规则。"""

    def setUp(self):
        self._paths = TempConfigPaths()
        self.tmp = self._paths.__enter__()
        os.makedirs(config_mod.TEMPLATE_DIR, exist_ok=True)

    def tearDown(self):
        self._paths.__exit__(None, None, None)

    def test_absolute_path(self):
        png = _make_png(os.path.join(self.tmp, "a.png"))
        self.assertEqual(tasks_mod._resolve_local_image_path(png), png)

    def test_missing_absolute_path_returns_blank(self):
        self.assertEqual(
            tasks_mod._resolve_local_image_path(os.path.join(self.tmp, "no.png")), "")

    def test_blank_returns_blank(self):
        for bad in ("", "   ", None):
            self.assertEqual(tasks_mod._resolve_local_image_path(bad), "")

    def test_surrounding_quotes_are_trimmed(self):
        png = _make_png(os.path.join(self.tmp, "a.png"))
        self.assertEqual(tasks_mod._resolve_local_image_path(f'"{png}"'), png)

    def test_relative_path_is_resolved_against_program_dir(self):
        png = _make_png(os.path.join(self.tmp, "sub_图.png"))
        self.assertEqual(tasks_mod._resolve_local_image_path("sub_图.png"), png)

    def test_falls_back_to_template_dir(self):
        """写「tpl_x.png」这种裸文件名时，去模板目录里找——可复用已有模板图。"""
        png = _make_png(os.path.join(config_mod.TEMPLATE_DIR, "tpl_x.png"))
        self.assertEqual(tasks_mod._resolve_local_image_path("tpl_x.png"), png)

    def test_file_url(self):
        png = _make_png(os.path.join(self.tmp, "a.png"))
        url = "file:///" + png.replace(os.sep, "/").lstrip("/")
        self.assertEqual(tasks_mod._resolve_local_image_path(url), png)

    def test_env_var_is_expanded(self):
        png = _make_png(os.path.join(self.tmp, "a.png"))
        with mock.patch.dict(os.environ, {"QF_TEST_IMG": png}):
            self.assertEqual(tasks_mod._resolve_local_image_path("%QF_TEST_IMG%"), png)

    def test_cache_dir_lives_under_template_dir(self):
        """网络图片缓存要落在 TEMPLATE_DIR 下：templates/ 已被 git 忽略，不会污染仓库。"""
        cache = tasks_mod._float_image_cache_dir()
        self.assertTrue(cache.startswith(config_mod.TEMPLATE_DIR))


class TestFloatImageNetwork(_QtTestCase):
    """网络图片：下载 → 落到按 URL 命名的稳定缓存 → 二次命中缓存不重复下载。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="float_net_")
        self.img = _make_png(os.path.join(self.tmp, "dl.png"))
        self.cache = os.path.join(self.tmp, "cache")
        patcher = mock.patch.object(tasks_mod, "_float_image_cache_dir",
                                    return_value=self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fetch(self, params=None, **req_kw):
        with mock.patch("app.http_actor.perform_request", **req_kw) as req:
            result = tasks_mod._fetch_network_image(params or {}, "https://a.com/x.png")
        return result, req

    def test_download_lands_in_cache_with_a_stable_name(self):
        (path, err), req = self._fetch(return_value={"content": self.img})
        self.assertEqual(err, "")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.dirname(path), self.cache)
        self.assertTrue(os.path.basename(path).startswith("url_"))
        self.assertTrue(os.path.basename(path).endswith(".png"))
        self.assertEqual(req.call_args.kwargs["result_type"], "image")

    def test_temp_download_file_is_moved_not_copied(self):
        """必须把带时间戳的临时文件挪走——留一份副本只会越攒越多。"""
        self._fetch(return_value={"content": self.img})
        self.assertFalse(os.path.exists(self.img))

    def test_same_url_reuses_the_cached_file(self):
        with mock.patch("app.http_actor.perform_request",
                        return_value={"content": self.img}) as req:
            first, _ = tasks_mod._fetch_network_image({}, "https://a.com/x.png")
            second, _ = tasks_mod._fetch_network_image({}, "https://a.com/x.png")
        self.assertEqual(first, second)
        self.assertEqual(req.call_count, 1, "同一网址第二次应命中缓存、不再下载")

    def test_different_urls_use_different_files(self):
        with mock.patch("app.http_actor.perform_request") as req:
            req.side_effect = [
                {"content": _make_png(os.path.join(self.tmp, "1.png"))},
                {"content": _make_png(os.path.join(self.tmp, "2.png"))}]
            first, _ = tasks_mod._fetch_network_image({}, "https://a.com/1.png")
            second, _ = tasks_mod._fetch_network_image({}, "https://a.com/2.png")
        self.assertNotEqual(first, second)
        self.assertEqual(req.call_count, 2)

    def test_http_error_is_reported(self):
        from app.http_actor import HttpError
        (path, err), _req = self._fetch(side_effect=HttpError("请求超时（10 秒）"))
        self.assertEqual(path, "")
        self.assertIn("图片下载失败", err)
        self.assertIn("超时", err)

    def test_unexpected_error_is_reported(self):
        (path, err), _req = self._fetch(side_effect=RuntimeError("boom"))
        self.assertEqual(path, "")
        self.assertIn("图片下载失败", err)
        self.assertIn("RuntimeError", err)

    def test_response_without_image_data_is_reported(self):
        (path, err), _req = self._fetch(return_value={"content": ""})
        self.assertEqual(path, "")
        self.assertIn("没取到图片数据", err)

    def test_timeout_and_proxy_are_forwarded(self):
        _res, req = self._fetch(params={"timeout": 3, "use_proxy": False,
                                        "proxy": "1.2.3.4:8"},
                                return_value={"content": self.img})
        kwargs = req.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 3.0)
        self.assertFalse(kwargs["use_proxy"])
        self.assertEqual(kwargs["proxy"], "1.2.3.4:8")

    def test_default_timeout_and_proxy(self):
        _res, req = self._fetch(return_value={"content": self.img})
        kwargs = req.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 10.0)
        self.assertTrue(kwargs["use_proxy"])
        self.assertEqual(kwargs["proxy"], "127.0.0.1:7897")

    def test_bad_timeout_falls_back_to_default(self):
        _res, req = self._fetch(params={"timeout": "abc"},
                                return_value={"content": self.img})
        self.assertEqual(req.call_args.kwargs["timeout"], 10.0)


class TestFloatImageAddressExecutor(_QtTestCase):
    """执行器在「图片地址」模式下的行为（本地 / 变量 / 网络三种来源）。"""

    def setUp(self):
        self._paths = TempConfigPaths()
        self.tmp = self._paths.__enter__()
        self.png = _make_png(os.path.join(self.tmp, "pic.png"))
        self.vars: dict = {}

    def tearDown(self):
        self._paths.__exit__(None, None, None)

    def _run(self, params, variables=None, show_result=True, stop=None):
        with mock.patch.object(overlay_actor, "show_image",
                               return_value=(object() if show_result else None)) as show, \
                mock.patch.object(shot_actor, "ui_call",
                                  side_effect=lambda fn: fn()) as ui:
            result = tasks_mod.run_float_image_step(
                params, self.vars if variables is None else variables, stop)
        return result, show, ui

    # ---------- 本地 ----------
    def test_local_path_is_displayed(self):
        (ok, msg), show, _ui = self._run({"source_mode": "address", "address": self.png})
        self.assertTrue(ok, msg)
        self.assertIn("pic.png", msg)
        self.assertEqual(show.call_args.args[0], self.png)

    def test_address_comes_from_a_variable(self):
        (ok, msg), show, _ui = self._run({"source_mode": "address", "address": "$img"},
                                         {"img": self.png})
        self.assertTrue(ok, msg)
        self.assertEqual(show.call_args.args[0], self.png)

    def test_blank_address_fails_without_showing(self):
        (ok, msg), show, ui = self._run({"source_mode": "address", "address": ""})
        self.assertFalse(ok)
        self.assertIn("地址为空", msg)
        show.assert_not_called()
        ui.assert_not_called()

    def test_whitespace_address_fails(self):
        (ok, msg), _show, _ui = self._run({"source_mode": "address", "address": "   "})
        self.assertFalse(ok)
        self.assertIn("地址为空", msg)

    def test_unassigned_variable_gets_a_clear_message(self):
        (ok, msg), show, _ui = self._run({"source_mode": "address", "address": "$img"})
        self.assertFalse(ok)
        self.assertIn("$img", msg)
        self.assertIn("未赋值", msg)
        show.assert_not_called()

    def test_missing_local_file_fails(self):
        (ok, msg), show, _ui = self._run(
            {"source_mode": "address", "address": os.path.join(self.tmp, "no.png")})
        self.assertFalse(ok)
        self.assertIn("不存在", msg)
        show.assert_not_called()

    def test_existing_but_unreadable_image_reports_load_failure(self):
        bad = os.path.join(self.tmp, "bad.png")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("not an image")
        (ok, msg), _show, _ui = self._run(
            {"source_mode": "address", "address": bad}, show_result=False)
        self.assertFalse(ok)
        self.assertIn("加载失败", msg)

    # ---------- 网络 ----------
    def test_url_is_downloaded_then_displayed(self):
        downloaded = _make_png(os.path.join(self.tmp, "d.png"))
        cache = os.path.join(self.tmp, "cache")
        with mock.patch.object(tasks_mod, "_float_image_cache_dir", return_value=cache), \
                mock.patch("app.http_actor.perform_request",
                           return_value={"content": downloaded}) as req:
            (ok, msg), show, _ui = self._run(
                {"source_mode": "address", "address": "https://a.com/x.png"})
        self.assertTrue(ok, msg)
        self.assertEqual(req.call_count, 1)
        self.assertIn("https://a.com/x.png", msg)
        shown = show.call_args.args[0]
        self.assertTrue(shown.startswith(cache))
        self.assertTrue(os.path.isfile(shown))

    def test_url_from_a_variable_is_downloaded(self):
        downloaded = _make_png(os.path.join(self.tmp, "d.png"))
        with mock.patch.object(tasks_mod, "_float_image_cache_dir",
                               return_value=os.path.join(self.tmp, "cache")), \
                mock.patch("app.http_actor.perform_request",
                           return_value={"content": downloaded}):
            (ok, msg), show, _ui = self._run(
                {"source_mode": "address", "address": "$u"},
                {"u": "https://a.com/x.png"})
        self.assertTrue(ok, msg)
        self.assertIn("https://a.com/x.png", msg)
        self.assertTrue(os.path.isfile(show.call_args.args[0]))

    def test_download_failure_is_reported(self):
        from app.http_actor import HttpError
        with mock.patch.object(tasks_mod, "_float_image_cache_dir",
                               return_value=os.path.join(self.tmp, "cache")), \
                mock.patch("app.http_actor.perform_request",
                           side_effect=HttpError("请求失败：拒绝连接")):
            (ok, msg), show, _ui = self._run(
                {"source_mode": "address", "address": "https://a.com/x.png"})
        self.assertFalse(ok)
        self.assertIn("下载失败", msg)
        show.assert_not_called()

    def test_stop_before_download(self):
        stop = threading.Event()
        stop.set()
        (ok, msg), show, _ui = self._run(
            {"source_mode": "address", "address": "https://a.com/x.png"}, stop=stop)
        self.assertFalse(ok)
        self.assertIn("停止", msg)
        show.assert_not_called()

    # ---------- 与模板图模式的关系 ----------
    def test_missing_source_mode_still_means_template(self):
        """旧流程没有 source_mode 字段，必须继续按模板图处理。"""
        with mock.patch.object(tasks_mod, "resolve_template_path", return_value=self.png):
            (ok, _msg), _show, _ui = self._run({"image": "x.png"})
        self.assertTrue(ok)

    def test_address_mode_does_not_fall_back_to_the_template_image(self):
        """两种来源互斥：地址模式没填地址时不能拿模板图顶上。"""
        (ok, _msg), show, _ui = self._run({"source_mode": "address", "image": "a.png"})
        self.assertFalse(ok)
        show.assert_not_called()

    def test_address_mode_is_still_async(self):
        (ok, _msg), _show, ui = self._run({"source_mode": "address", "address": self.png})
        self.assertTrue(ok)
        self.assertEqual(ui.call_count, 1)


if __name__ == "__main__":
    unittest.main()
