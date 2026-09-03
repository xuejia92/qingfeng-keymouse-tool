"""目标检测步骤（yolo_detect）的测试。

覆盖：config 默认参数与摘要；yolo_actor.detect 的前置校验（路径/依赖/设备/
区域越界）与结果过滤（类别/置信度排序/全局坐标偏移）；tasks.run_yolo_detect_step
的分支（无变量/停止/检测失败/未检出/附加动作/效果预览/变量解析）；
步骤编辑对话框表单（回填/默认值/确定前校验/模型浏览）。
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app import finder, input_actors, yolo_actor
from app.config import FlowStep, default_step_params
from app.tasks import run_yolo_detect_step

PARAMS = {"model_path": "model.pt", "region": "", "classes": "",
          "confidence": 0.95, "device": "cuda", "action": "none",
          "preview": False, "preview_duration": 1.0, "variable": "det"}


def _img(w=100, h=80):
    return np.zeros((h, w, 3), dtype=np.uint8)


class _FakeModel:
    """假模型：predict 返回预设框（子图坐标），记录收到的图与阈值。"""

    def __init__(self, boxes):
        self.boxes = boxes
        self.seen = []

    def predict(self, img, conf):
        self.seen.append((img.shape[1], img.shape[0], conf))
        return self.boxes


# ---------- config ----------

class TestYoloConfig(unittest.TestCase):
    def test_registered(self):
        from app.config import FLOW_STEP_TYPES
        self.assertEqual(FLOW_STEP_TYPES.get("yolo_detect"), "目标检测")

    def test_in_perceive_group(self):
        from app.ui.flow_tab import MODULE_GROUPS
        groups = {gid: types for gid, _, types in MODULE_GROUPS}
        self.assertIn("yolo_detect", groups["perceive"])

    def test_default_params(self):
        p = default_step_params("yolo_detect")
        self.assertEqual(set(p), {"model_path", "region", "classes", "confidence",
                                  "device", "action", "preview", "preview_duration",
                                  "variable"})
        self.assertEqual(p["model_path"], "")
        self.assertEqual(p["region"], "")
        self.assertEqual(p["classes"], "")
        self.assertEqual(p["confidence"], 0.5)
        self.assertEqual(p["device"], "cuda")
        self.assertEqual(p["action"], "none")
        self.assertFalse(p["preview"])
        self.assertEqual(p["preview_duration"], 1.0)
        self.assertEqual(p["variable"], "")

    def test_summary(self):
        s = FlowStep(type="yolo_detect",
                     params={"model_path": "yolov5s.pt", "variable": "det"})
        self.assertIn("目标检测", s.summary())
        self.assertIn("yolov5s.pt", s.summary())
        self.assertIn("det", s.summary())
        s2 = FlowStep(type="yolo_detect")
        self.assertIn("未设模型", s2.summary())
        self.assertIn("未指定变量", s2.summary())


# ---------- yolo_actor 前置校验 ----------

class TestYoloActorPrecheck(unittest.TestCase):
    def setUp(self):
        yolo_actor.clear_model_cache()

    def test_empty_model_path(self):
        with self.assertRaises(yolo_actor.YoloError) as ctx:
            yolo_actor.detect("", confidence=0.9)
        self.assertIn("未设置模型路径", str(ctx.exception))

    def test_model_file_not_exists(self):
        with self.assertRaises(yolo_actor.YoloError) as ctx:
            yolo_actor.detect("no/such/model.pt")
        self.assertIn("模型文件不存在", str(ctx.exception))

    def test_missing_torch(self):
        """有效路径但 import torch 失败：给出明确缺依赖提示。"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
        try:
            with mock.patch.object(yolo_actor, "_import_torch",
                                   side_effect=yolo_actor.YoloError("缺少依赖 torch")):
                with self.assertRaises(yolo_actor.YoloError) as ctx:
                    yolo_actor.detect(path)
            self.assertIn("缺少依赖", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_invalid_device(self):
        fake_torch = mock.Mock()
        with self.assertRaises(yolo_actor.YoloError) as ctx:
            yolo_actor._check_device(fake_torch, "tpu")
        self.assertIn("推理设备无效", str(ctx.exception))

    def test_cuda_unavailable(self):
        fake_torch = mock.Mock()
        fake_torch.cuda.is_available.return_value = False
        with self.assertRaises(yolo_actor.YoloError) as ctx:
            yolo_actor._check_device(fake_torch, "cuda")
        self.assertIn("CUDA 不可用", str(ctx.exception))

    def test_cpu_device_ok(self):
        fake_torch = mock.Mock()
        fake_torch.cuda.is_available.return_value = False
        self.assertEqual(yolo_actor._check_device(fake_torch, "cpu"), "cpu")

    def test_model_cached_per_path_device(self):
        """同一 (路径, 设备) 只加载一次。"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
        try:
            fake_torch = mock.Mock()
            fake_torch.cuda.is_available.return_value = True
            with mock.patch.object(yolo_actor, "_import_torch", return_value=fake_torch), \
                 mock.patch.object(yolo_actor, "_UltralyticsModel") as UM:
                UM.return_value = _FakeModel([])
                m1 = yolo_actor._load_model(path, "cuda")
                m2 = yolo_actor._load_model(path, "cuda")
            self.assertIs(m1, m2)
            UM.assert_called_once()
        finally:
            os.unlink(path)


# ---------- yolo_actor 后端兜底（旧版 yolov5 模型兼容） ----------

class TestYoloBackendFallback(unittest.TestCase):
    def setUp(self):
        yolo_actor.clear_model_cache()

    def _tmp_model(self):
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".pt", delete=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _fake_torch(self):
        t = mock.Mock()
        t.cuda.is_available.return_value = True
        return t

    def test_fallback_to_hub_when_ultralytics_rejects_legacy_model(self):
        """ultralytics 报旧模型不兼容(TypeError)时，自动兜底 yolov5 仓库后端。"""
        path = self._tmp_model()
        hub_model = _FakeModel([])
        with mock.patch.object(yolo_actor, "_import_torch", return_value=self._fake_torch()), \
             mock.patch.object(yolo_actor, "_UltralyticsModel",
                               side_effect=TypeError("NOT forwards compatible")) as UM, \
             mock.patch.object(yolo_actor, "_HubModel", return_value=hub_model) as HM:
            m = yolo_actor._load_model(path, "cuda")
        self.assertIs(m, hub_model)
        UM.assert_called_once()
        HM.assert_called_once()

    def test_fallback_to_hub_when_ultralytics_not_installed(self):
        """ultralytics 未安装(ImportError)同样兜底。"""
        path = self._tmp_model()
        with mock.patch.object(yolo_actor, "_import_torch", return_value=self._fake_torch()), \
             mock.patch.object(yolo_actor, "_UltralyticsModel",
                               side_effect=ImportError("No module named 'ultralytics'")), \
             mock.patch.object(yolo_actor, "_HubModel", return_value=_FakeModel([])) as HM:
            m = yolo_actor._load_model(path, "cpu")
        self.assertIsNotNone(m)
        HM.assert_called_once()

    def test_both_backends_fail_combined_error(self):
        """两种后端都失败：合并报错，包含两边的原因与修复提示。"""
        path = self._tmp_model()
        with mock.patch.object(yolo_actor, "_import_torch", return_value=self._fake_torch()), \
             mock.patch.object(yolo_actor, "_UltralyticsModel",
                               side_effect=TypeError("NOT forwards compatible")), \
             mock.patch.object(yolo_actor, "_HubModel",
                               side_effect=RuntimeError("network unreachable")):
            with self.assertRaises(yolo_actor.YoloError) as ctx:
                yolo_actor._load_model(path, "cuda")
        msg = str(ctx.exception)
        self.assertIn("两种后端", msg)
        self.assertIn("NOT forwards compatible", msg)
        self.assertIn("network unreachable", msg)
        self.assertIn("hubconf.py", msg)

    def test_find_local_yolov5_repo(self):
        """模型上级目录含 hubconf.py + models/ + utils/ 时识别为本地仓库。"""
        import shutil
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        repo = os.path.join(root, "v5-7.0")
        os.makedirs(os.path.join(repo, "models"))
        os.makedirs(os.path.join(repo, "utils"))
        os.makedirs(os.path.join(repo, "weights"))
        open(os.path.join(repo, "hubconf.py"), "w").close()
        model = os.path.join(repo, "weights", "yolov5s_best.pt")
        open(model, "w").close()
        self.assertEqual(yolo_actor._find_local_yolov5_repo(model), repo)

    def test_find_local_yolov5_repo_none_when_not_repo(self):
        """普通目录（无 hubconf.py）返回 None。"""
        path = self._tmp_model()
        self.assertIsNone(yolo_actor._find_local_yolov5_repo(path))

    def test_hub_model_prefers_local_repo(self):
        """找到本地仓库：走本地加载，不调用 torch.hub.load。"""
        fake_torch = self._fake_torch()
        with mock.patch.object(yolo_actor, "_find_local_yolov5_repo",
                               return_value="C:/repo"), \
             mock.patch.object(yolo_actor._HubModel, "_load_from_local_repo",
                               return_value="local_model") as local:
            m = yolo_actor._HubModel(fake_torch, "C:/repo/m.pt", "cuda")
        self.assertEqual(m._m, "local_model")
        local.assert_called_once_with(fake_torch, "C:/repo", "C:/repo/m.pt", "cuda")
        fake_torch.hub.load.assert_not_called()

    def test_hub_model_github_when_no_local_repo(self):
        """无本地仓库：torch.hub 联网拉取 ultralytics/yolov5。"""
        fake_torch = self._fake_torch()
        with mock.patch.object(yolo_actor, "_find_local_yolov5_repo", return_value=None):
            yolo_actor._HubModel(fake_torch, "C:/m.pt", "cpu")
        args, kwargs = fake_torch.hub.load.call_args
        self.assertEqual(args[:2], ("ultralytics/yolov5", "custom"))
        self.assertEqual(kwargs["path"], "C:/m.pt")

    def test_local_repo_load_weights_only_compat(self):
        """本地仓库加载：torch.load 临时回退 weights_only=False（torch>=2.6 兼容），
        加载完恢复原 torch.load；模型用 AutoShape 包装。"""
        import shutil
        import sys
        import tempfile
        import types
        repo = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        calls = {}
        fake_torch = mock.Mock()

        def fake_load(*a, **kw):
            calls.update(kw)
            return "ckpt"

        fake_torch.load = fake_load
        models_pkg = types.ModuleType("models")
        models_common = types.ModuleType("models.common")
        models_exp = types.ModuleType("models.experimental")
        models_common.AutoShape = lambda m: ("autoshape", m)

        def attempt_load(path, device=None):
            fake_torch.load(path, map_location="cpu")
            return "raw_model"

        models_exp.attempt_load = attempt_load
        with mock.patch.dict(sys.modules, {"models": models_pkg,
                                           "models.common": models_common,
                                           "models.experimental": models_exp}):
            m = yolo_actor._HubModel._load_from_local_repo(
                fake_torch, repo, os.path.join(repo, "m.pt"), "cpu")
        self.assertEqual(m, ("autoshape", "raw_model"))
        self.assertEqual(calls.get("weights_only"), False)
        self.assertIs(fake_torch.load, fake_load)   # 已恢复

    def test_pt_failure_hints_sibling_onnx(self):
        """.pt 两后端都失败且同目录有同名 .onnx：错误里给出改用 onnx 的提示。"""
        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        pt = os.path.join(d, "best.pt")
        open(pt, "w").close()
        open(os.path.join(d, "best.onnx"), "w").close()
        with mock.patch.object(yolo_actor, "_import_torch", return_value=self._fake_torch()), \
             mock.patch.object(yolo_actor, "_UltralyticsModel",
                               side_effect=TypeError("NOT forwards compatible")), \
             mock.patch.object(yolo_actor, "_HubModel",
                               side_effect=RuntimeError("network unreachable")):
            with self.assertRaises(yolo_actor.YoloError) as ctx:
                yolo_actor._load_model(pt, "cuda")
        self.assertIn("best.onnx", str(ctx.exception))
        self.assertIn("onnxruntime", str(ctx.exception))


# ---------- yolo_actor onnxruntime 后端 ----------

class TestYoloOnnxBackend(unittest.TestCase):
    def setUp(self):
        yolo_actor.clear_model_cache()

    def _tmp_onnx(self, with_classes=False):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "best.onnx")
        open(path, "w").close()
        if with_classes:
            with open(os.path.join(d, "classes.txt"), "w", encoding="utf-8") as f:
                f.write("dadishu\nother\n")
        return path

    def test_onnx_routes_without_torch(self):
        """.onnx 直接走 onnxruntime 后端，不 import torch。"""
        path = self._tmp_onnx()
        with mock.patch.object(yolo_actor, "_import_torch",
                               side_effect=AssertionError("不应 import torch")), \
             mock.patch.object(yolo_actor, "_OnnxModel", return_value=_FakeModel([])) as OM:
            m = yolo_actor._load_model(path, "cpu")
        self.assertIsNotNone(m)
        OM.assert_called_once_with(path, "cpu")

    def test_onnx_invalid_device(self):
        path = self._tmp_onnx()
        with self.assertRaises(yolo_actor.YoloError) as ctx:
            yolo_actor._load_model(path, "tpu")
        self.assertIn("推理设备无效", str(ctx.exception))

    def test_onnx_missing_onnxruntime(self):
        """未安装 onnxruntime：明确缺依赖提示。"""
        import sys
        path = self._tmp_onnx()
        with mock.patch.dict(sys.modules, {"onnxruntime": None}):
            with self.assertRaises(yolo_actor.YoloError) as ctx:
                yolo_actor._OnnxModel(path, "cpu")
        self.assertIn("onnxruntime", str(ctx.exception))

    def _make_onnx_model(self, pred, names=(), iw=640, ih=640):
        """绕过 __init__ 构造 _OnnxModel：假 session 返回预设输出。"""
        m = yolo_actor._OnnxModel.__new__(yolo_actor._OnnxModel)
        sess = mock.Mock()
        sess.run.return_value = [np.array(pred, dtype=np.float32)]
        m._sess = sess
        m._input_name = "images"
        m._iw, m._ih = iw, ih
        m._names = list(names)
        return m

    def test_onnx_predict_maps_letterbox_coords(self):
        """letterbox 坐标正确映射回原图；类别名来自 classes.txt。"""
        # 原图 100x80，输入 640x640：r=6.4，nw=640，nh=512，dw=0，dh=64
        # letterbox 框 (288,288)-(352,352) -> 原图 (45,35)-(55,45)
        pred = [[320, 320, 64, 64, 1.0, 0.1, 0.9]]   # cls1，conf=0.9
        m = self._make_onnx_model(pred, names=["a", "b"])
        out = m.predict(_img(100, 80), 0.5)
        self.assertEqual(len(out), 1)
        x1, y1, x2, y2, conf, label = out[0]
        self.assertAlmostEqual(x1, 45.0, places=1)
        self.assertAlmostEqual(y1, 35.0, places=1)
        self.assertAlmostEqual(x2, 55.0, places=1)
        self.assertAlmostEqual(y2, 45.0, places=1)
        self.assertAlmostEqual(conf, 0.9, places=3)
        self.assertEqual(label, "b")

    def test_onnx_predict_conf_filter_and_empty(self):
        """低于阈值过滤；全过滤返回 []。"""
        pred = [[320, 320, 64, 64, 1.0, 0.3, 0.2]]   # 最高 conf=0.3
        m = self._make_onnx_model(pred, names=["a", "b"])
        self.assertEqual(m.predict(_img(100, 80), 0.5), [])

    def test_onnx_predict_default_class_names(self):
        """无 classes.txt：类别名用 classN 占位。"""
        pred = [[320, 320, 64, 64, 1.0, 0.1, 0.9]]
        m = self._make_onnx_model(pred, names=[])
        out = m.predict(_img(100, 80), 0.5)
        self.assertEqual(out[0][5], "class1")

    def test_onnx_bad_output_shape(self):
        """输出不是 (N, 5+nc)：明确格式异常报错。"""
        m = self._make_onnx_model(np.zeros((1, 4), dtype=np.float32))
        with self.assertRaises(yolo_actor.YoloError) as ctx:
            m.predict(_img(100, 80), 0.5)
        self.assertIn("输出格式异常", str(ctx.exception))

    def test_onnx_class_names_loaded_from_sibling(self):
        """模型同目录 classes.txt 被读取。"""
        path = self._tmp_onnx(with_classes=True)
        names = yolo_actor._load_onnx_class_names(path)
        self.assertEqual(names, ["dadishu", "other"])



# ---------- yolo_actor.detect 过滤与坐标 ----------

class TestYoloDetect(unittest.TestCase):
    def setUp(self):
        yolo_actor.clear_model_cache()

    def _detect(self, boxes, **kw):
        model = _FakeModel(boxes)
        with mock.patch.object(yolo_actor, "_load_model", return_value=model), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img()):
            return yolo_actor.detect("model.pt", **kw), model

    def test_fullscreen_results(self):
        """全屏检测：结果字典含 class/confidence/region，按置信度降序。"""
        boxes = [(10, 10, 30, 40, 0.80, "dog"),
                 (50, 20, 90, 60, 0.97, "person")]
        results, _ = self._detect(boxes)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["class"], "person")   # 高置信度在前
        self.assertEqual(results[0]["confidence"], 0.97)
        self.assertEqual(results[0]["region"], "50,20,90,60")
        self.assertEqual(results[1]["region"], "10,10,30,40")
        for d in results:
            self.assertEqual(set(d), {"class", "confidence", "region"})

    def test_region_offset_applied(self):
        """区域检测：返回坐标 = 子图坐标 + 区域偏移（全局虚拟桌面坐标）。"""
        boxes = [(5, 5, 15, 20, 0.99, "cat")]
        results, model = self._detect(boxes, region="10,20,50,40")
        self.assertEqual(results[0]["region"], "15,25,25,40")
        # 子图大小 = 区域宽高
        self.assertEqual(model.seen[0][:2], (50, 40))

    def test_region_partially_out_of_screen_clamped(self):
        """区域部分越界：自动裁剪到屏幕内（屏 100x80）。"""
        results, model = self._detect([], region="60,40,100,100")
        self.assertEqual(results, [])
        self.assertEqual(model.seen[0][:2], (40, 40))     # 100-60, 80-40

    def test_region_fully_out_of_screen(self):
        """区域完全在屏幕外：明确报错。"""
        with mock.patch.object(yolo_actor, "_load_model", return_value=_FakeModel([])), \
             mock.patch.object(finder, "grab_full_screen", return_value=_img()):
            with self.assertRaises(yolo_actor.YoloError) as ctx:
                yolo_actor.detect("model.pt", region="500,600,100,100")
        self.assertIn("超出屏幕范围", str(ctx.exception))

    def test_class_filter(self):
        """指定类别：只返回匹配类别；空=全部。"""
        boxes = [(0, 0, 9, 9, 0.9, "person"),
                 (10, 10, 19, 19, 0.8, "dog"),
                 (20, 20, 29, 29, 0.7, "person")]
        results, _ = self._detect(boxes, classes="person")
        self.assertEqual([d["class"] for d in results], ["person", "person"])
        results_all, _ = self._detect(boxes, classes="")
        self.assertEqual(len(results_all), 3)

    def test_class_filter_chinese_comma_and_unknown(self):
        """中文逗号可用；模型里没有的类别名只是匹配不到，不报错。"""
        boxes = [(0, 0, 9, 9, 0.9, "person"), (10, 10, 19, 19, 0.8, "dog")]
        results, _ = self._detect(boxes, classes="dog，cat")
        self.assertEqual([d["class"] for d in results], ["dog"])

    def test_confidence_passed_to_model(self):
        """置信度阈值原样传给模型推理。"""
        _, model = self._detect([], confidence=0.66)
        self.assertEqual(model.seen[0][2], 0.66)


# ---------- tasks.run_yolo_detect_step ----------

class TestRunYoloDetectStep(unittest.TestCase):
    HITS = [{"class": "person", "confidence": 0.97, "region": "50,20,90,60"},
            {"class": "dog", "confidence": 0.80, "region": "10,10,30,40"}]

    def _run(self, params, variables=None, detect_ret=None, detect_exc=None):
        variables = variables if variables is not None else {}
        m = mock.patch.object(yolo_actor, "detect")
        with m as det:
            if detect_exc is not None:
                det.side_effect = detect_exc
            else:
                det.return_value = detect_ret if detect_ret is not None else []
            ok, why = run_yolo_detect_step(params, variables)
        return ok, why, variables, det

    def test_stop_set(self):
        ok, why = run_yolo_detect_step(dict(PARAMS), {}, threading.Event())
        # 未 set 的 stop 不应拦截；这里单独测 set 过的
        stop = threading.Event()
        stop.set()
        ok, why = run_yolo_detect_step(dict(PARAMS), {}, stop=stop)
        self.assertFalse(ok)
        self.assertEqual(why, "已手动停止")

    def test_no_variable(self):
        ok, why = run_yolo_detect_step(dict(PARAMS, variable=""), {})
        self.assertFalse(ok)
        self.assertIn("未指定结果变量", why)

    def test_yolo_error(self):
        ok, why, _, _ = self._run(dict(PARAMS),
                                  detect_exc=yolo_actor.YoloError("模型文件不存在：x.pt"))
        self.assertFalse(ok)
        self.assertIn("模型文件不存在", why)

    def test_generic_error(self):
        ok, why, _, _ = self._run(dict(PARAMS), detect_exc=RuntimeError("boom"))
        self.assertFalse(ok)
        self.assertIn("目标检测失败", why)
        self.assertIn("boom", why)

    def test_no_detection_writes_empty_list(self):
        """未检测到：写空列表 []，步骤不失败（供分支判断）。"""
        ok, why, variables, _ = self._run(dict(PARAMS), detect_ret=[])
        self.assertTrue(ok)
        self.assertEqual(variables["det"], [])
        self.assertIn("未检测到目标", why)

    def test_detected_writes_results(self):
        ok, why, variables, det = self._run(dict(PARAMS), detect_ret=list(self.HITS))
        self.assertTrue(ok)
        self.assertEqual(variables["det"], self.HITS)
        self.assertIn("检测到 2 个目标", why)
        det.assert_called_once_with(model_path="model.pt", region="", classes="",
                                    confidence=0.95, device="cuda")

    def test_variable_type_recorded(self):
        variables, types = {}, {}
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)):
            ok, _ = run_yolo_detect_step(dict(PARAMS), variables, types)
        self.assertTrue(ok)
        self.assertEqual(types["det"], "list")

    def test_classes_variable_reference(self):
        """检测类别支持 $变量名 引用。"""
        with mock.patch.object(yolo_actor, "detect", return_value=[]) as det:
            run_yolo_detect_step(dict(PARAMS, classes="$want"),
                                 {"want": "person,car"})
        self.assertEqual(det.call_args.kwargs["classes"], "person,car")

    def test_action_none_no_click(self):
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)), \
             mock.patch.object(input_actors, "click") as clk:
            ok, _ = run_yolo_detect_step(dict(PARAMS, action="none"), {})
        self.assertTrue(ok)
        clk.assert_not_called()

    def test_action_left_clicks_top_center(self):
        """左键单击：点最高置信度目标中心（50,20,90,60 -> 70,40）。"""
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)), \
             mock.patch.object(input_actors, "click") as clk:
            ok, _ = run_yolo_detect_step(dict(PARAMS, action="left"), {})
        self.assertTrue(ok)
        clk.assert_called_once_with("left", 1, 70, 40)

    def test_action_right(self):
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)), \
             mock.patch.object(input_actors, "click") as clk:
            run_yolo_detect_step(dict(PARAMS, action="right"), {})
        clk.assert_called_once_with("right", 1, 70, 40)

    def test_action_double(self):
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)), \
             mock.patch.object(input_actors, "click") as clk:
            run_yolo_detect_step(dict(PARAMS, action="double"), {})
        clk.assert_called_once_with("left", 2, 70, 40)

    def test_preview_called_with_labeled_boxes(self):
        """勾选预览：红框列表 = (矩形, 类别, 置信度两位小数)。"""
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)), \
             mock.patch("app.find_preview.show_boxes_highlight") as show:
            ok, _ = run_yolo_detect_step(dict(PARAMS, preview=True,
                                              preview_duration=2.0), {})
        self.assertTrue(ok)
        boxes, duration = show.call_args.args
        self.assertEqual(duration, 2.0)
        self.assertEqual(boxes[0], ((50, 20, 90, 60), "person", "0.97"))
        self.assertEqual(boxes[1], ((10, 10, 30, 40), "dog", "0.80"))

    def test_preview_failure_swallowed(self):
        """预览异常不影响检测结果。"""
        with mock.patch.object(yolo_actor, "detect", return_value=list(self.HITS)), \
             mock.patch("app.find_preview.show_boxes_highlight",
                        side_effect=RuntimeError("no qt")):
            ok, why = run_yolo_detect_step(dict(PARAMS, preview=True), {})
        self.assertTrue(ok)
        self.assertIn("检测到 2 个目标", why)


# ---------- flows.py 分发 ----------

class TestYoloDispatch(unittest.TestCase):
    def test_dispatched_in_exec_step(self):
        """_exec_step 能把 yolo_detect 分发到 run_yolo_detect_step。"""
        import inspect
        from app import flows
        src = inspect.getsource(flows.FlowRunner._exec_step)
        self.assertIn("yolo_detect", src)
        self.assertIn("run_yolo_detect_step", src)


# ---------- 对话框 ----------

class TestYoloDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _open(self, params: dict):
        from app.ui.flow_dialog import StepParamsDialog
        return StepParamsDialog(FlowStep(type="yolo_detect", params=params))

    def test_form_defaults(self):
        """默认：置信度 0.5、cuda、无操作、预览关（spin 禁用）、全屏。"""
        params = dict(PARAMS)
        params.pop("confidence", None)   # 未提供置信度 → 回填默认 0.5
        dlg = self._open(dict(params, model_path="", variable=""))
        self.assertEqual(dlg.confidence.value(), 0.5)
        self.assertEqual(dlg.device_combo.currentData(), "cuda")
        self.assertEqual(dlg.action_combo.currentData(), "none")
        self.assertFalse(dlg.preview_check.isChecked())
        self.assertFalse(dlg.preview_spin.isEnabled())
        self.assertEqual(dlg.preview_spin.value(), 1.0)
        self.assertEqual(dlg.region_edit.text(), "全屏（整个虚拟桌面）")

    def test_form_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
        try:
            dlg = self._open({"model_path": path, "region": "10,20,100,50",
                              "classes": "person,car", "confidence": 0.9,
                              "device": "cpu", "action": "double",
                              "preview": True, "preview_duration": 2.5,
                              "variable": "det"})
            self.assertEqual(dlg.model_path_edit.text(), path)
            self.assertEqual(dlg.region_edit.text(), "10, 20, 100 x 50")
            self.assertEqual(dlg.classes_edit.text(), "person,car")
            self.assertEqual(dlg.confidence.value(), 0.9)
            self.assertEqual(dlg.device_combo.currentData(), "cpu")
            self.assertEqual(dlg.action_combo.currentData(), "double")
            self.assertTrue(dlg.preview_check.isChecked())
            self.assertTrue(dlg.preview_spin.isEnabled())
            step = FlowStep(type="yolo_detect")
            dlg.apply_to(step)
            self.assertEqual(step.params["model_path"], path)
            self.assertEqual(step.params["region"], "10,20,100,50")
            self.assertEqual(step.params["classes"], "person,car")
            self.assertEqual(step.params["confidence"], 0.9)
            self.assertEqual(step.params["device"], "cpu")
            self.assertEqual(step.params["action"], "double")
            self.assertEqual(step.params["variable"], "det")
            self.assertTrue(step.params["preview"])
            self.assertEqual(step.params["preview_duration"], 2.5)
        finally:
            os.unlink(path)

    def test_apply_manual_region(self):
        """手动输入「左上x,左上y,右下x,右下y」转成 x,y,w,h。"""
        dlg = self._open(PARAMS)
        dlg.manual_edit.setText("100,200,400,500")
        dlg._apply_manual_region()
        self.assertEqual(dlg._region, "100,200,300,300")
        self.assertEqual(dlg.region_edit.text(), "100, 200, 300 x 300")

    def test_pick_model_file(self):
        dlg = self._open(PARAMS)
        with mock.patch("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        return_value=("D:/models/yolov5s.pt", "")):
            dlg._pick_model_file()
        self.assertEqual(dlg.model_path_edit.text(), "D:/models/yolov5s.pt")

    def test_check_model_path_warns(self):
        """手动输入不存在的路径，失焦即提示。"""
        dlg = self._open(PARAMS)
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.model_path_edit.setText("no/such/model.pt")
            dlg._check_model_path()
        warn.assert_called_once()

    def test_check_model_path_ok_silent(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
        try:
            dlg = self._open(PARAMS)
            with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
                dlg.model_path_edit.setText(path)
                dlg._check_model_path()
            warn.assert_not_called()
        finally:
            os.unlink(path)

    def test_accept_requires_model_path(self):
        dlg = self._open(dict(PARAMS, model_path=""))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()

    def test_accept_rejects_nonexistent_model(self):
        dlg = self._open(dict(PARAMS, model_path="no/such/model.pt"))
        with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
            dlg.accept()
        warn.assert_called_once()

    def test_accept_requires_variable(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
        try:
            dlg = self._open(dict(PARAMS, model_path=path, variable=""))
            with mock.patch("app.ui.flow_dialog.QMessageBox.warning") as warn:
                dlg.accept()
            warn.assert_called_once()
        finally:
            os.unlink(path)


# ---------- _BoxesOverlay：多框标注绘制 ----------

class TestBoxesOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_paint_draws_boxes_and_badges(self):
        """两个框：drawRect 2 次（红框）+ fillRect 2 次（类别徽标）+ 2 次（置信度徽标）。"""
        from PySide6.QtGui import QPainter, QPaintEvent
        from app.find_preview import _BoxesOverlay
        boxes = [((10, 10, 100, 80), "person", "0.97"),
                 ((200, 150, 300, 260), "dog", "0.80")]
        w = _BoxesOverlay(boxes, 60000)
        try:
            with mock.patch.object(QPainter, "drawRect") as draw, \
                 mock.patch.object(QPainter, "fillRect") as fill:
                w.paintEvent(QPaintEvent(w.rect()))
            self.assertEqual(draw.call_count, 2)
            self.assertEqual(fill.call_count, 4)
        finally:
            w.close()

    def test_empty_label_skips_badge(self):
        """标签为空时不画徽标（与找图单框预览一致的纯红框）。"""
        from PySide6.QtGui import QPainter, QPaintEvent
        from app.find_preview import _BoxesOverlay
        w = _BoxesOverlay([((10, 10, 100, 80), "", "")], 60000)
        try:
            with mock.patch.object(QPainter, "drawRect") as draw, \
                 mock.patch.object(QPainter, "fillRect") as fill:
                w.paintEvent(QPaintEvent(w.rect()))
            self.assertEqual(draw.call_count, 1)
            self.assertEqual(fill.call_count, 0)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
