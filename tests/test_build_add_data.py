# -*- coding: utf-8 -*-
"""build.py 收集 rapidocr 资源（--add-data）的测试。

背景（2026-09-26 现场故障）：打包后的程序报
`ModuleNotFoundError: No module named 'ch_ppocr_v3_det'`。原因是 rapidocr 用
**动态导入**加载三个实现子包：

    rapid_ocr_api.py:  sys.path.append(包目录)
                       importlib.import_module("ch_ppocr_v3_det")   # 名字来自 config.yaml

PyInstaller 静态分析扫不到这些名字，必须把整个目录当**数据**带进包里。
同一处还发现旧代码把模型文件名硬编码成 v4（本机实际只有 v3）→ 模型也会静默漏收，
所以现在改成扫描 models/ 目录并把所有 .onnx 都带上。
"""
from __future__ import annotations

import os
import unittest

import build

try:
    import rapidocr_onnxruntime
    HAS_RAPIDOCR = True
except ImportError:                       # 打包机没装依赖时跳过（build.py 会打警告）
    HAS_RAPIDOCR = False


@unittest.skipUnless(HAS_RAPIDOCR, "本机没装 rapidocr_onnxruntime")
class TestRapidocrAddData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.args = build._add_data_args()
        cls.pkg = os.path.dirname(os.path.abspath(rapidocr_onnxruntime.__file__))

    def _pairs(self):
        return [a.partition(os.pathsep) for a in self.args]

    def _targets(self):
        return [dst.replace("/", os.sep) for _src, _s, dst in self._pairs()]

    def test_dynamic_subpackages_are_collected(self):
        """三个动态导入的子包都必须作为数据带进包 —— 缺了就是本次报错。"""
        for sub in build.RAPIDOCR_SUBPACKAGES:
            want = f"rapidocr_onnxruntime{os.sep}{sub}"
            self.assertIn(want, self._targets(), f"子包 {sub} 没被收集")

    def test_subpackage_source_is_a_real_dir_with_init(self):
        for src, _s, dst in self._pairs():
            if dst.replace("/", os.sep).startswith(f"rapidocr_onnxruntime{os.sep}ch_ppocr"):
                self.assertTrue(os.path.isdir(src), src)
                self.assertTrue(os.path.isfile(os.path.join(src, "__init__.py")), src)

    def test_config_yaml_is_collected(self):
        hit = any(src.endswith("config.yaml") and dst == "rapidocr_onnxruntime"
                  for src, _s, dst in self._pairs())
        self.assertTrue(hit, "config.yaml 没被收集（模型清单丢了就没法初始化）")

    def test_all_models_are_collected(self):
        """models/ 下的 .onnx 一个都不能漏（曾经硬编码 v4 名字导致静默漏收）。"""
        model_dir = os.path.join(self.pkg, build.RAPIDOCR_MODEL_DIR)
        wanted = {f for f in os.listdir(model_dir)
                  if f.lower().endswith(build.RAPIDOCR_MODEL_EXT)}
        self.assertTrue(wanted, "本机 rapidocr 没找到任何模型文件")
        got = {os.path.basename(src) for src, _s, _dst in self._pairs()
               if src.lower().endswith(build.RAPIDOCR_MODEL_EXT)}
        self.assertEqual(got, wanted)

    def test_models_go_into_models_subdir(self):
        for src, _s, dst in self._pairs():
            if src.lower().endswith(build.RAPIDOCR_MODEL_EXT):
                self.assertEqual(dst, f"rapidocr_onnxruntime/{build.RAPIDOCR_MODEL_DIR}")

    def test_no_hardcoded_model_file_names_in_source(self):
        """源码里不能再出现写死的模型文件名（换版本就会漏）。"""
        with open(build.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for stale in ("ch_PP-OCRv3", "ch_PP-OCRv4"):
            self.assertNotIn(stale, src, f"build.py 里还写着模型文件名 {stale}")


if __name__ == "__main__":
    unittest.main()
