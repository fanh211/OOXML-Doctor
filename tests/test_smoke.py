"""ooxml-doctor 冒烟测试（只用合成夹具，不依赖任何第三方课件）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

import synth  # noqa: E402
from ooxml_doctor.cli import do_scan  # noqa: E402
from ooxml_doctor.oracle import verify  # noqa: E402
from ooxml_doctor.repair import apply_repair, build_plan  # noqa: E402


class TestScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.broken = synth.build(os.path.join(cls.tmp.name, "broken.pptx"))
        cls.clean = synth.build_clean(os.path.join(cls.tmp.name, "clean.pptx"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_finds_injected_defects(self):
        bag, _pkg, _parts = do_scan(self.broken)
        codes = [d.code for d in bag.items]
        self.assertIn("M1.20", codes, "应检出元素顺序违规")
        self.assertIn("P4-ASC", codes, "应检出空关系引用/媒体重复")
        self.assertIn("APP-REL-PLACEHOLDER", codes, "应检出 Target=NULL")
        self.assertIn("APP-MIME-REG", codes, "应检出未注册媒体类型")
        self.assertTrue(bag.will_repair, "应判定为会被 Office 要求修复")

    def test_null_target_is_classified_once(self):
        """Target="NULL" 只能被报成"占位符"，不能被同时报成"本机绝对路径"。"""
        bag, _pkg, _parts = do_scan(self.broken)
        placeholder = [d for d in bag.items if d.code == "APP-REL-PLACEHOLDER"]
        localpath = [d for d in bag.items if d.code == "APP-REL-LOCALPATH"]
        self.assertEqual(len(placeholder), 1, "应恰好报一次占位符目标")
        self.assertEqual(len(localpath), 0,
                         f"NULL 不该被当成本机绝对路径：{[d.message for d in localpath]}")

    def test_order_violation_is_the_repair_trigger(self):
        bag, _pkg, _parts = do_scan(self.broken)
        triggers = {d.code for d in bag.repair_triggers}
        self.assertEqual(triggers, {"M1.20"})

    def test_no_false_positive_on_clean_package(self):
        bag, _pkg, _parts = do_scan(self.clean)
        self.assertFalse(bag.will_repair,
                         "干净包不应被判为需要修复；实际触发：" +
                         str([(d.code, d.message) for d in bag.repair_triggers]))
        self.assertEqual(bag.n_error, 0,
                         "干净包不应有 error：" + str(
                             [(d.code, d.message) for d in bag.items
                              if d.severity.name == "ERROR"]))

    def test_value_violation_is_warning_not_repair_trigger(self):
        """XSD 取值类违规（pattern/enumeration）必须报 warning 且不算修复触发项。

        校准依据：PowerPoint 官方修复版仍保留 <a:buSzPct val="80000"/>。
        """
        path = synth.build_pattern_violation(
            os.path.join(self.tmp.name, "pattern.pptx"))
        bag, _pkg, _parts = do_scan(path)
        m120 = [d for d in bag.items if d.code == "M1.20"]
        self.assertTrue(m120, "应检出 XSD 取值违规")
        for d in m120:
            self.assertFalse(d.repair_trigger,
                             f"取值违规不应算作修复触发项：{d.message}")
            self.assertEqual(d.severity.name, "WARNING",
                             f"取值违规应为 warning：{d.message}")
        self.assertFalse(bag.will_repair)

    def test_every_diagnostic_has_code_and_severity(self):
        bag, _pkg, _parts = do_scan(self.broken)
        for d in bag.items:
            self.assertTrue(d.code)
            self.assertIn(d.severity.name, ("NOTE", "WARNING", "ERROR"))
            if d.repair_trigger:
                self.assertIsNotNone(d.ref, f"{d.code} 声称会触发修复，必须挂规范依据")


class TestLicensing(unittest.TestCase):
    """合规回归：许可证与第三方声明必须存在，且 NOTICE 覆盖 ISO/IEC schema。"""

    ROOT = os.path.dirname(HERE)

    def test_files_exist(self):
        for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
            p = os.path.join(self.ROOT, name)
            self.assertTrue(os.path.isfile(p), f"缺少 {name}")
            self.assertGreater(os.path.getsize(p), 200, f"{name} 太空")

    def test_notice_embedded_in_package(self):
        notice = os.path.join(self.ROOT, "src", "ooxml_doctor", "NOTICE.txt")
        self.assertTrue(os.path.isfile(notice), "NOTICE.txt 必须随包内嵌")
        text = open(notice, encoding="utf-8").read()
        for needle in ("MIT License", "ISO/IEC", "Ecma International",
                       "--no-schemas"):
            self.assertIn(needle, text, f"NOTICE.txt 缺关键内容：{needle}")

    def test_licenses_command_works(self):
        from ooxml_doctor.cli import main
        self.assertEqual(main(["licenses"]), 0)


class TestRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.broken = synth.build(os.path.join(cls.tmp.name, "broken.pptx"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _repair(self, level: str, name: str):
        out = os.path.join(self.tmp.name, name)
        bag, pkg, parts = do_scan(self.broken)
        plan = build_plan(pkg, parts, bag, level)
        apply_repair(self.broken, out, pkg, parts, bag, plan)
        return out, bag, plan

    def test_low_fixes_schema_order_and_keeps_equivalence(self):
        out, before, plan = self._repair("low", "low.pptx")
        self.assertTrue(any(e.code == "M1.20" for e in plan.edits))
        after, _pkg, _parts = do_scan(out)
        self.assertFalse(after.will_repair,
                         "low 修复后不应再触发 Office 修复：" +
                         str([(d.code, d.message) for d in after.repair_triggers]))
        self.assertLess(after.n_error, before.n_error)
        eq = verify(self.broken, out)
        self.assertTrue(eq["checks"]["text"]["ok"], "文本必须完全等价")
        self.assertTrue(eq["checks"]["media"]["ok"], "媒体必须完全等价")
        self.assertTrue(eq["checks"]["rel_graph"]["ok"], "low 不应改动关系图")

    def test_all_planned_edits_actually_land(self):
        """回归测试：计划里每一处编辑都必须体现在产物字节里。

        曾经出现过 [Content_Types].xml 的修复被静默丢弃
        （apply_repair 遍历 pkg.parts，而它按 OPC 语义排除了内容类型流）。
        """
        import zipfile
        for level in ("low", "high"):
            out = os.path.join(self.tmp.name, f"landed-{level}.pptx")
            bag, pkg, parts = do_scan(self.broken)
            plan = build_plan(pkg, parts, bag, level)
            manifest = apply_repair(self.broken, out, pkg, parts, bag, plan)
            self.assertTrue(manifest["postcondition_ok"],
                            f"{level}: 有编辑没落地 -> {manifest['unverified_edits']}")
            self.assertEqual(manifest["unverified_edits"], [])
            # 内容类型修复必须真的写进产物
            with zipfile.ZipFile(out) as z:
                ct = z.read("[Content_Types].xml").decode()
            self.assertNotIn("image/.jpg", ct)
            self.assertNotIn('ContentType="audio/mp3"', ct)

    def test_low_does_not_touch_external_links(self):
        out, _before, plan = self._repair("low", "low2.pptx")
        self.assertFalse(any(e.level == "high" for e in plan.edits),
                         "low 计划里不应出现 high 级改动")

    def test_high_fixes_more_and_stays_equivalent(self):
        low_out, before, _ = self._repair("low", "l3.pptx")
        high_out, _b, plan = self._repair("high", "h3.pptx")
        self.assertTrue(any(e.level == "high" for e in plan.edits))
        low_bag, _p, _q = do_scan(low_out)
        high_bag, _p, _q = do_scan(high_out)
        self.assertLess(high_bag.n_error, low_bag.n_error,
                        "high 应比 low 修掉更多 error")
        eq = verify(self.broken, high_out, allow_external_rel_drop=True)
        self.assertTrue(eq["ok"], f"high 后等价性应通过：{eq['checks']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
