"""app/config.py 的纯函数与配置兼容迁移测试。

迁移逻辑是这里最脆的地方：它要同时处理「全新安装」「旧版内嵌流程」
「旧版热键字段」「配置损坏」四种输入，而且只跑一次就改写磁盘，
回归风险高，所以优先补测试。
"""
from __future__ import annotations

import json
import os
import unittest

from app import config
from app.config import (
    AppConfig,
    FindTask,
    Flow,
    FlowStep,
    _atomic_write_json,
    _is_flow_id,
    assign_missing_flow_seqs,
    assign_missing_group_seqs,
    clamp,
    flow_from_dict,
    flow_to_dict,
    load_flow_files,
    parse_region_str,
    repair_web_pairs,
    safe_filename,
    save_flows_dir,
)
from tests._env import TempConfigPaths, write_json


class TestRegionStr(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_region_str("10, 20, 300, 400"), (10, 20, 300, 400))

    def test_empty_is_fullscreen(self):
        self.assertIsNone(parse_region_str(""))
        self.assertIsNone(parse_region_str(None))

    def test_invalid(self):
        self.assertIsNone(parse_region_str("1,2,3"))          # 段数不对
        self.assertIsNone(parse_region_str("a,b,c,d"))         # 非数字
        self.assertIsNone(parse_region_str("0,0,0,100"))       # 宽为 0
        self.assertIsNone(parse_region_str("0,0,100,-5"))      # 高为负


class TestClamp(unittest.TestCase):
    def test_in_range(self):
        self.assertEqual(clamp(5, 1, 10), 5)

    def test_out_of_range(self):
        self.assertEqual(clamp(-1, 1, 10), 1)
        self.assertEqual(clamp(99, 1, 10), 10)

    def test_junk_falls_back_to_low(self):
        self.assertEqual(clamp("abc", 20, 100), 20)
        self.assertEqual(clamp(None, 20, 100), 20)

    def test_keeps_type(self):
        self.assertIsInstance(clamp("5", 1, 10), int)
        self.assertIsInstance(clamp("0.5", 0.0, 1.0), float)


class TestSafeFilename(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(safe_filename('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i")

    def test_strip_dots_and_spaces(self):
        self.assertEqual(safe_filename("  ...  "), "流程")
        self.assertEqual(safe_filename(" 我的流程 "), "我的流程")

    def test_windows_reserved(self):
        self.assertEqual(safe_filename("con"), "_con")
        self.assertEqual(safe_filename("NUL"), "_NUL")
        self.assertEqual(safe_filename("com1"), "_com1")

    def test_normal_name_untouched(self):
        self.assertEqual(safe_filename("找图登录"), "找图登录")


class TestFlowFromDict(unittest.TestCase):
    def _flow_dict(self, **kw):
        base = {"id": "abc", "name": "F", "steps": [{"type": "wait", "params": {"seconds": 2}}]}
        base.update(kw)
        return base

    def test_roundtrip(self):
        f = flow_from_dict(self._flow_dict())
        self.assertIsNotNone(f)
        self.assertEqual(f.id, "abc")
        self.assertEqual(len(f.steps), 1)
        self.assertEqual(f.steps[0].params["seconds"], 2)

    def test_missing_steps_is_not_a_flow(self):
        """没有 steps 的 json 不能当成空流程——否则 flows/ 里随便一个 json 都会被吞掉。"""
        self.assertIsNone(flow_from_dict({"name": "x"}))
        self.assertIsNone(flow_from_dict(None))

    def test_bad_step_skipped_others_kept(self):
        data = self._flow_dict(steps=[
            {"type": "nonexistent"},
            {"type": "wait", "params": {"seconds": 1}},
        ])
        f = flow_from_dict(data)
        self.assertIsNotNone(f)
        self.assertEqual(len(f.steps), 1)

    def test_loops_clamped(self):
        self.assertEqual(flow_from_dict(self._flow_dict(loops=-3)).loops, 0)
        self.assertEqual(flow_from_dict(self._flow_dict(loops=99999)).loops, 9999)

    def test_created_seq_roundtrip_and_default(self):
        """created_seq 随流程文件存下来；旧文件缺字段时回退 0（后续由迁移补发）。"""
        self.assertEqual(flow_from_dict(self._flow_dict(created_seq=7)).created_seq, 7)
        self.assertEqual(flow_from_dict(self._flow_dict()).created_seq, 0)
        self.assertEqual(flow_from_dict(self._flow_dict(created_seq=-5)).created_seq, 0)
        back = flow_from_dict(flow_to_dict(Flow(name="F", created_seq=9)))
        self.assertEqual(back.created_seq, 9)

    def test_unknown_step_type_raises(self):
        with self.assertRaises(ValueError):
            FlowStep(type="teleport")


class TestWebPair(unittest.TestCase):
    """网页步骤配对：打开网址 + 关闭浏览器 成对出现、删除联动、编辑破坏后解除。"""

    @staticmethod
    def _pair_steps():
        """构造一对共享 pair_id 的网页步骤（打开 + 关闭）。"""
        pid = "pair-001"
        open_s = FlowStep(type="web", params={"action": "open", "url": "https://x.com"})
        close_s = FlowStep(type="web", params={"action": "close_browser"})
        open_s.pair_id = close_s.pair_id = pid
        return open_s, close_s

    def test_pair_id_roundtrip(self):
        """pair_id 要能随流程文件存下来，重载后配对关系还在。"""
        open_s, close_s = self._pair_steps()
        flow = Flow(name="网页流程", steps=[open_s, close_s])
        back = flow_from_dict(flow_to_dict(flow))
        self.assertIsNotNone(back)
        self.assertEqual(back.steps[0].pair_id, back.steps[1].pair_id)
        self.assertTrue(back.steps[0].pair_id)

    def test_legacy_flow_without_pair_id_loads(self):
        """旧流程没有 pair_id 字段，加载不能崩，且 pair_id 为空。"""
        data = {"id": "abc", "name": "F", "steps": [
            {"type": "web", "params": {"action": "open", "url": "https://x.com"}},
        ]}
        flow = flow_from_dict(data)
        self.assertIsNotNone(flow)
        self.assertEqual(flow.steps[0].pair_id, "")

    def test_valid_pair_kept(self):
        """合法配对（open + close_browser）在修复后 pair_id 保留。"""
        open_s, close_s = self._pair_steps()
        self.assertFalse(repair_web_pairs([open_s, close_s]))
        self.assertTrue(open_s.pair_id and close_s.pair_id)

    def test_pair_broken_unpaired(self):
        """把配对里的「打开网址」改成别的动作 → 两个步骤都解除配对。"""
        open_s, close_s = self._pair_steps()
        open_s.params["action"] = "close_tab"      # 用户改成关闭标签页
        self.assertTrue(repair_web_pairs([open_s, close_s]))
        self.assertEqual(open_s.pair_id, "")
        self.assertEqual(close_s.pair_id, "")

    def test_pair_both_open_unpaired(self):
        """配对的两个步骤都变成「打开网址」→ 解除配对。"""
        open_s, close_s = self._pair_steps()
        close_s.params["action"] = "open"
        self.assertTrue(repair_web_pairs([open_s, close_s]))
        self.assertEqual(open_s.pair_id, "")
        self.assertEqual(close_s.pair_id, "")

    def test_single_lonely_pair_unpaired(self):
        """只有一半的配对（比如手动编辑 json 残留）→ 解除配对。"""
        open_s, _ = self._pair_steps()
        self.assertTrue(repair_web_pairs([open_s]))
        self.assertEqual(open_s.pair_id, "")

    def test_three_steps_same_pair_unpaired(self):
        """同一 pair_id 出现 3 个步骤（数据异常）→ 全部解除配对。"""
        open_s, close_s = self._pair_steps()
        extra = FlowStep(type="web", params={"action": "open"})
        extra.pair_id = open_s.pair_id
        self.assertTrue(repair_web_pairs([open_s, close_s, extra]))
        self.assertEqual([s.pair_id for s in (open_s, close_s, extra)], ["", "", ""])

    def test_unpaired_steps_untouched(self):
        """没有 pair_id 的步骤不受配对修复影响。"""
        wait = FlowStep(type="wait")
        self.assertFalse(repair_web_pairs([wait]))
        self.assertEqual(wait.pair_id, "")


class TestFlowIdCompat(unittest.TestCase):
    def test_12_hex(self):
        self.assertTrue(_is_flow_id("0123456789ab"))
        self.assertTrue(_is_flow_id("ABCDEFABCDEF"))

    def test_rejects_other(self):
        self.assertFalse(_is_flow_id("流程名"))
        self.assertFalse(_is_flow_id("0123456789a"))       # 11 位
        self.assertFalse(_is_flow_id("0123456789abc"))     # 13 位
        self.assertFalse(_is_flow_id("0123456789zz"))      # 非十六进制


class TestAtomicWrite(unittest.TestCase):
    def test_writes_and_leaves_no_tmp(self):
        with TempConfigPaths() as tmp:
            p = os.path.join(tmp, "x.json")
            self.assertTrue(_atomic_write_json(p, {"a": 1}))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 1})
            self.assertFalse(os.path.exists(p + ".tmp"))   # 临时文件必须被 replace 掉

    def test_overwrites_existing(self):
        with TempConfigPaths() as tmp:
            p = os.path.join(tmp, "x.json")
            _atomic_write_json(p, {"a": 1})
            _atomic_write_json(p, {"a": 2})
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 2})


class TestFlowDirRoundtrip(unittest.TestCase):
    def test_save_then_load(self):
        with TempConfigPaths():
            flow = Flow(name="测试流程", steps=[FlowStep(type="wait", params={"seconds": 1.5})])
            save_flows_dir([flow])
            loaded = load_flow_files()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].name, "测试流程")
            self.assertEqual(loaded[0].steps[0].params["seconds"], 1.5)

    def test_duplicate_names_do_not_overwrite(self):
        with TempConfigPaths():
            save_flows_dir([Flow(name="同名"), Flow(name="同名")])
            self.assertEqual(len(os.listdir(config.FLOWS_DIR)), 2)
            self.assertEqual(len(load_flow_files()), 2)

    def test_invalid_json_skipped(self):
        with TempConfigPaths():
            os.makedirs(config.FLOWS_DIR, exist_ok=True)
            with open(os.path.join(config.FLOWS_DIR, "bad.json"), "w", encoding="utf-8") as f:
                f.write("{ 这不是 json")
            self.assertEqual(load_flow_files(), [])


class TestAppConfigMigration(unittest.TestCase):
    """旧配置升级路径——这些分支一旦退化，用户升级后热键就会莫名失效。"""

    def test_fresh_install_gets_defaults(self):
        with TempConfigPaths():
            cfg = AppConfig.load()
            self.assertEqual(cfg.show_hide_hotkey, "shift+f1")
            self.assertEqual(cfg.stop_all_hotkey, "shift+f2")
            self.assertTrue(os.path.isfile(config.CONFIG_PATH))   # 默认值要落盘

    def test_legacy_show_hotkey_migrated(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {
                "show_hotkey": "ctrl+f9",          # 旧版：显示键
                "stop_all_hotkey": "shift+f2",
            })
            self.assertEqual(AppConfig.load().show_hide_hotkey, "ctrl+f9")

    def test_legacy_default_show_hotkey_is_upgraded(self):
        """旧默认值恰好等于 shift+f1 时也要落到新默认，而不是沿用旧字段。"""
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {"show_hotkey": "shift+f1"})
            self.assertEqual(AppConfig.load().show_hide_hotkey, "shift+f1")

    def test_legacy_stop_hotkey_upgraded(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {"stop_all_hotkey": "ctrl+alt+x"})
            self.assertEqual(AppConfig.load().stop_all_hotkey, "shift+f2")

    def test_custom_stop_hotkey_preserved(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {"stop_all_hotkey": "ctrl+shift+q"})
            self.assertEqual(AppConfig.load().stop_all_hotkey, "ctrl+shift+q")

    def test_corrupted_config_falls_back_to_defaults(self):
        with TempConfigPaths():
            with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write("{ 坏掉的 json")
            self.assertEqual(AppConfig.load().show_hide_hotkey, "shift+f1")

    def test_legacy_embedded_flows_migrated_to_dir(self):
        """旧版把流程存在 config.json 里，升级后必须落进 flows/ 目录。"""
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {
                "flows": [{"id": "aaaabbbbcccc", "name": "老流程",
                           "steps": [{"type": "wait", "params": {"seconds": 3}}]}],
            })
            cfg = AppConfig.load()
            self.assertEqual([f.name for f in cfg.flows], ["老流程"])
            self.assertEqual(os.listdir(config.FLOWS_DIR), ["老流程.json"])
            self.assertEqual(len(AppConfig.load().flows), 1)   # 再加载不应重复迁移

    def test_out_of_range_values_clamped(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {
                "clicker": {"interval_ms": 1, "count": -5},
                "capture_interval_sec": 99999,
            })
            cfg = AppConfig.load()
            self.assertEqual(cfg.clicker.interval_ms, 20)      # 下限
            self.assertEqual(cfg.clicker.count, 0)
            self.assertEqual(cfg.capture_interval_sec, 3600)   # 上限

    def test_bad_region_dropped(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {
                "find_tasks": [{"id": "t1", "name": "T", "region": "乱写的"}],
            })
            self.assertEqual(AppConfig.load().find_tasks[0].region, "")

    def test_valid_region_kept(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {
                "find_tasks": [{"id": "t1", "name": "T", "region": "0,0,800,600"}],
            })
            self.assertEqual(AppConfig.load().find_tasks[0].region_tuple(), (0, 0, 800, 600))

    def test_save_does_not_embed_flows(self):
        """save() 必须把 flows 从 config.json 里剔除，否则又退回旧版单文件存储。"""
        with TempConfigPaths():
            cfg = AppConfig()
            cfg.flows = [Flow(name="F", steps=[FlowStep(type="wait", params={"seconds": 1})])]
            cfg.save()
            with open(config.CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.assertNotIn("flows", data)
            self.assertEqual(os.listdir(config.FLOWS_DIR), ["F.json"])


class TestCreationSeq(unittest.TestCase):
    """流程/分组创建序号的补发与迁移。"""

    def test_assign_missing_flow_seqs(self):
        flows = [Flow(name="A"), Flow(name="B"), Flow(name="C")]
        self.assertTrue(assign_missing_flow_seqs(flows))
        self.assertEqual([f.created_seq for f in flows], [1, 2, 3])

    def test_assign_flow_seqs_keeps_existing(self):
        flows = [Flow(name="A", created_seq=5), Flow(name="B", created_seq=2)]
        self.assertFalse(assign_missing_flow_seqs(flows))
        self.assertEqual([f.created_seq for f in flows], [5, 2])

    def test_assign_missing_flow_seqs_mixed(self):
        """已有部分序号时，缺失者从当前最大序号之后接续补发。"""
        flows = [Flow(name="A"), Flow(name="B", created_seq=5), Flow(name="C")]
        assign_missing_flow_seqs(flows)
        self.assertEqual([f.created_seq for f in flows], [6, 5, 7])

    def test_assign_missing_group_seqs(self):
        seqs = {"办公": 3}
        self.assertTrue(assign_missing_group_seqs(["办公", "游戏", "学习"], seqs))
        self.assertEqual(seqs, {"办公": 3, "游戏": 4, "学习": 5})

    def test_assign_group_seqs_noop_when_all_set(self):
        seqs = {"办公": 1, "游戏": 2}
        self.assertFalse(assign_missing_group_seqs(["办公", "游戏"], seqs))

    def test_load_assigns_group_seqs_for_legacy_config(self):
        with TempConfigPaths():
            write_json(config.CONFIG_PATH, {"flow_groups": ["办公", "游戏"]})
            cfg = AppConfig.load()
            self.assertEqual(cfg.flow_group_seqs, {"办公": 1, "游戏": 2})

    def test_load_assigns_flow_created_seqs(self):
        with TempConfigPaths():
            os.makedirs(config.FLOWS_DIR, exist_ok=True)
            save_flows_dir([Flow(name="A"), Flow(name="B")])   # created_seq=0 落盘
            cfg = AppConfig.load()
            self.assertEqual([f.created_seq for f in cfg.flows], [1, 2])


class TestFindTask(unittest.TestCase):
    def test_auto_id(self):
        self.assertEqual(len(FindTask().id), 12)

    def test_explicit_id_kept(self):
        self.assertEqual(FindTask(id="fixed").id, "fixed")


if __name__ == "__main__":
    unittest.main()
