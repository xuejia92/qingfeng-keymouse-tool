"""`app.imgio`（Unicode 安全图片读写）的测试。

**这是「手动截图提示图片写入失败」那个 bug 的回归测试**，所以必须真跑 cv2、
真落盘，不能 mock：bug 的成因就是 cv2 在中文路径下的真实失败行为。

OpenCV 的 imread/imwrite 在 Windows 上用窄字符 fopen，路径含非 ASCII 就打不开，
而且是静默失败（imwrite 返回 False、imread 返回 None）。本程序目录叫
「清风自动化键鼠工具」，于是所有走磁盘的图片读写都会中招。

覆盖点：
- 中文目录 + 中文文件名：真实写盘 + 读回像素一致；
- 纯 ASCII 路径：走 cv2 原生快路，同样成功（不能为了中文修好就把 ASCII 弄坏）；
- 失败分支：目录不存在 -> False（且 write_failure_reason 指出目录问题）；
- 非图片文件 / 不存在的文件 -> imread 返回 None；
- 扩展名与编码参数透传（jpg 质量参数生效，落盘是 JPEG 头）；
- 契约：app/ 下除 imgio.py 外不许再直接调 cv2.imread / cv2.imwrite；
- 回归：finder.load_template 能读中文路径下的模板。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import numpy as np

from app import finder, imgio

CJK_DIR_NAME = "清风截图测试"
CJK_FILE_NAME = "手动截图_20260917_133428.png"


def _img(h=9, w=7) -> np.ndarray:
    """造一张有内容的图（纯色会被压缩成一样的字节，不方便验证读回）。"""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = np.arange(w, dtype=np.uint8) * 3      # B
    img[:, :, 1] = np.arange(h, dtype=np.uint8).reshape(h, 1) * 5   # G
    img[:, :, 2] = 200                                   # R
    return img


def _ascii_tmpdir() -> str:
    """要一个纯 ASCII 的临时目录（tempfile 默认在 C:\\Users\\<中文名>\\... 下）。

    优先用 ASCII 的 TEMP/TMP；都不行就在系统盘根下建一个（建不了就跳过测试）。
    """
    for var in ("TEMP", "TMP"):
        cand = os.environ.get(var, "")
        if cand and cand.isascii() and os.path.isdir(cand):
            return tempfile.mkdtemp(prefix="imgio_ascii_", dir=cand)
    drive = os.path.splitdrive(os.path.abspath(os.sep))[0] or "C:"
    root = os.path.join(drive + os.sep, "_imgio_ascii_test")
    os.makedirs(root, exist_ok=True)        # 建不了会抛 OSError -> 调用方 skip
    return tempfile.mkdtemp(prefix="case_", dir=root)


class TestImwriteCjkPath(unittest.TestCase):
    """本 bug 的主场景：中文路径必须能写、能读。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="imgio_cjk_")
        self.dir = os.path.join(self.tmp, CJK_DIR_NAME)
        os.makedirs(self.dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_read_back(self):
        path = os.path.join(self.dir, CJK_FILE_NAME)
        self.assertTrue(imgio.imwrite(path, _img()), "中文路径写盘失败")
        self.assertTrue(os.path.isfile(path), "imwrite 说成功但文件不存在")
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n", "落盘的不是 PNG")

        back = imgio.imread(path)
        self.assertIsNotNone(back, "写进去的图读不回来")
        self.assertTrue(np.array_equal(back, _img()), "读回的像素与写入的不一致")

    def test_ascii_filename_in_cjk_dir(self):
        """目录含中文、文件名纯 ASCII：同样要能写（用户自选路径常见）。"""
        path = os.path.join(self.dir, "shot.png")
        self.assertTrue(imgio.imwrite(path, _img()))
        self.assertTrue(os.path.isfile(path))

    def test_overwrite_existing_file(self):
        """同名文件已存在时直接覆盖（另存为对话框确认覆盖后走的就是这条路）。"""
        path = os.path.join(self.dir, CJK_FILE_NAME)
        imgio.imwrite(path, np.zeros((4, 4, 3), dtype=np.uint8))
        self.assertTrue(imgio.imwrite(path, _img()))
        self.assertTrue(np.array_equal(imgio.imread(path), _img()))

    def test_jpeg_ext_and_params_are_honored(self):
        """扩展名与编码参数要透传到 imencode（jpg 质量参数生效）。"""
        path = os.path.join(self.dir, "带参数的图.jpg")
        self.assertTrue(imgio.imwrite(path, _img(), [int(__import__("cv2").IMWRITE_JPEG_QUALITY), 95]))
        with open(path, "rb") as f:
            self.assertEqual(f.read(2), b"\xff\xd8", "jpg 落盘没有 JPEG 头")

    def test_auto_created_dir_is_not_required(self):
        """imgio 不负责建目录：目录不存在就老实返回 False，由调用方决定提示。"""
        path = os.path.join(self.dir, "没有这个子目录", "x.png")
        self.assertFalse(imgio.imwrite(path, _img()))


class TestImwriteAsciiPath(unittest.TestCase):
    """ASCII 路径走 cv2 原生快路，不能因为修中文而回归。"""

    def setUp(self):
        try:
            self.tmp = _ascii_tmpdir()
        except OSError as e:                     # 建不了 ASCII 目录就跳过
            self.skipTest(f"无法创建纯 ASCII 临时目录：{e}")
        if not self.tmp.isascii():
            self.skipTest("拿不到纯 ASCII 临时目录")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        # 壳目录（用例自己建的才可能删掉，删不掉=不是我们建的）
        parent = os.path.dirname(self.tmp)
        if os.path.basename(parent) == "_imgio_ascii_test":
            try:
                os.rmdir(parent)
            except OSError:
                pass

    def test_write_and_read_back(self):
        path = os.path.join(self.tmp, "ascii_shot.png")
        self.assertTrue(imgio.imwrite(path, _img()))
        self.assertTrue(np.array_equal(imgio.imread(path), _img()))


class TestImread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="imgio_read_")
        os.makedirs(os.path.join(self.tmp, CJK_DIR_NAME), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_none(self):
        self.assertIsNone(imgio.imread(os.path.join(self.tmp, CJK_DIR_NAME, "没有.png")))

    def test_not_an_image_returns_none(self):
        path = os.path.join(self.tmp, CJK_DIR_NAME, "其实是文本.png")
        with open(path, "w", encoding="utf-8") as f:
            f.write("这不是图片")
        self.assertIsNone(imgio.imread(path))

    def test_empty_path_and_none(self):
        self.assertIsNone(imgio.imread(""))
        self.assertIsNone(imgio.imread(None))

    def test_grayscale_flag(self):
        """flags 要透传（找图用 IMREAD_COLOR，别的调用方可能用灰度）。"""
        import cv2
        path = os.path.join(self.tmp, CJK_DIR_NAME, "灰.png")
        imgio.imwrite(path, _img())
        gray = imgio.imread(path, cv2.IMREAD_GRAYSCALE)
        self.assertEqual(gray.shape, (_img().shape[0], _img().shape[1]))


class TestWriteFailureReason(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="imgio_reason_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_dir(self):
        why = imgio.write_failure_reason(os.path.join(self.tmp, "不存在", "x.png"))
        self.assertIn("目录不存在", why)

    def test_empty_path(self):
        self.assertIn("未指定", imgio.write_failure_reason(""))

    def test_writable_dir_falls_back_to_generic_hint(self):
        """目录正常却写失败（磁盘满/被占用/权限）：给个通用但不空的话。"""
        why = imgio.write_failure_reason(os.path.join(self.tmp, "x.png"))
        self.assertTrue(why.strip())


class TestLoadTemplateUsesImgio(unittest.TestCase):
    """找图模板读盘回归：模板目录在中文程序目录下，cv2.imread 读不出来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="imgio_tpl_")
        self.dir = os.path.join(self.tmp, CJK_DIR_NAME)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "模板.png")
        imgio.imwrite(self.path, _img())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_template_from_cjk_path(self):
        tpl = finder.load_template(self.path)
        self.assertIsNotNone(tpl, "中文路径下的模板读不出来")
        self.assertTrue(np.array_equal(tpl, _img()))

    def test_load_template_missing_returns_none(self):
        self.assertIsNone(finder.load_template(os.path.join(self.dir, "没有.png")))


class TestNoDirectCv2DiskIO(unittest.TestCase):
    """契约测试：磁盘图片读写必须走 imgio。

    直接在别处调 cv2.imread/imwrite 会在这个中文目录里静默失败，
    而且失败得悄无声息（imwrite 返回 False 没人看、imread 返回 None 当没找到）。
    """

    APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    ALLOWED = {"imgio.py"}

    def test_no_direct_cv2_file_io_in_app(self):
        import re
        pat = re.compile(r"cv2\s*\.\s*(imread|imwrite)\s*\(")
        bad = []
        for name in sorted(os.listdir(self.APP_DIR)):
            if not name.endswith(".py") or name in self.ALLOWED:
                continue
            path = os.path.join(self.APP_DIR, name)
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    code = line.split("#", 1)[0]        # 注释里提到不算
                    if pat.search(code):
                        bad.append(f"{name}:{i}: {line.strip()[:80]}")
        self.assertEqual(bad, [], "这些地方直接用了 cv2 的磁盘读写，请改走 imgio：\n"
                                  + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
