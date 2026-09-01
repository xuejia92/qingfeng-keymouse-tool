"""app/tasks.py 的找图抓屏间隔测试。

grab_interval 是 CPU 占用与响应延迟之间的权衡点：调快了风扇狂转，
调慢了用户觉得「点了没反应」。把边界钉住，免得以后被随手改回固定值。
"""
from __future__ import annotations

import unittest

from app.tasks import SEARCH_GRAB_INTERVAL, _GRAB_INTERVAL_MAX, _GRAB_INTERVAL_MIN, grab_interval


class TestGrabInterval(unittest.TestCase):
    def test_tiny_region_hits_floor(self):
        self.assertAlmostEqual(grab_interval((0, 0, 100, 100)), _GRAB_INTERVAL_MIN)

    def test_1080p_is_about_10hz(self):
        iv = grab_interval((0, 0, 1920, 1080))
        self.assertGreater(iv, _GRAB_INTERVAL_MIN)
        self.assertLess(iv, _GRAB_INTERVAL_MAX)
        self.assertAlmostEqual(1.0 / iv, 10.0, delta=2.0)

    def test_4k_hits_ceiling(self):
        self.assertAlmostEqual(grab_interval((0, 0, 3840, 2160)), _GRAB_INTERVAL_MAX)

    def test_grows_with_area(self):
        small = grab_interval((0, 0, 400, 300))
        large = grab_interval((0, 0, 1600, 1200))
        self.assertLess(small, large)

    def test_always_within_bounds(self):
        for w, h in ((1, 1), (800, 600), (2560, 1440), (7680, 4320), (100, 100000)):
            iv = grab_interval((0, 0, w, h))
            self.assertGreaterEqual(iv, _GRAB_INTERVAL_MIN)
            self.assertLessEqual(iv, _GRAB_INTERVAL_MAX)

    def test_bad_region_falls_back_to_screen(self):
        """区域解析不出宽高时退回全屏面积，而不是抛异常中断找图。"""
        screen_iv = grab_interval(None)
        for bad in (("a", "b", "c", "d"), (0, 0), "x,y,w,h", 42):
            self.assertAlmostEqual(grab_interval(bad), screen_iv)

    def test_none_equals_omitted(self):
        self.assertAlmostEqual(grab_interval(), grab_interval(None))

    def test_legacy_constant_is_the_floor(self):
        """旧代码引用 SEARCH_GRAB_INTERVAL，它应该等于下限而不是被删掉。"""
        self.assertEqual(SEARCH_GRAB_INTERVAL, _GRAB_INTERVAL_MIN)


if __name__ == "__main__":
    unittest.main()
