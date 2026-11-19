#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_batch_import.py -- 【S2-TAX-008】#3261 批量导入初稿字典 · 单元测试（全虚构数据）

覆盖工单三条验收标准：
  AC1 预演输出新增、更新、冲突、错误四类清单（测试通过）
  AC2 重复导入同一文件不产生重复标签（测试通过）
  AC3 parent_code 不存在的行被列入错误清单（测试通过）
以及：CSV/JSON 双格式一致、冲突三类（文件内重复/撞名/软删除）、提议队列幂等、
init 模式管理员门禁、冲突与错误行一律不导入、will_update 逐字段差异、坏表头 fail-closed。

运行: python -I test_batch_import.py   （或在包目录: python -I -m unittest test_batch_import -v）
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import batch_import as bi  # noqa: E402


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

EXISTING = os.path.join(HERE, "existing_dictionary.json")
CSV_FILE = os.path.join(HERE, "import_v1.csv")
JSON_FILE = os.path.join(HERE, "import_v1.json")

# 手工判定表（与 README.md 第 3 节一致，逐行核对过的期望分类）
EXPECTED_ADD = {"IND-ENE", "IND-ENE-PV", "SVC-SPACE-COWORK"}
EXPECTED_UPDATE = {"IND-FIN", "SVC-EVENT"}
EXPECTED_CONFLICT_ROWS = {8, 9, 10, 11}
EXPECTED_ERROR_ROWS = {12, 13, 14, 15}
EXPECTED_UNCHANGED = {"IND-IT"}


def make_report(path):
    existing = bi.load_existing(EXISTING)
    rows, h = bi.load_rows(path)
    return bi.build_report(rows, existing, path, h)


class BatchImportTest(unittest.TestCase):
    def setUp(self):
        os.environ["IMPORT_ASOF"] = "2026-11-18"

    # ---------- AC1 预演四类清单 ----------
    def test_ac1_four_lists(self):
        rep = make_report(CSV_FILE)
        c = rep["counts"]
        self.assertEqual(c["total_rows"], 14)
        self.assertEqual(c["will_add"], 3)
        self.assertEqual(c["will_update"], 2)
        self.assertEqual(c["conflicts"], 4)
        self.assertEqual(c["errors"], 4)
        self.assertEqual(c["unchanged"], 1)
        self.assertEqual({x["code"] for x in rep["will_add"]}, EXPECTED_ADD)
        self.assertEqual({x["code"] for x in rep["will_update"]}, EXPECTED_UPDATE)
        self.assertEqual({x["row_no"] for x in rep["conflicts"]}, EXPECTED_CONFLICT_ROWS)
        self.assertEqual({x["row_no"] for x in rep["errors"]}, EXPECTED_ERROR_ROWS)
        self.assertEqual({x["code"] for x in rep["unchanged"]}, EXPECTED_UNCHANGED)

    # ---------- AC2 重复导入同一文件不产生重复标签 ----------
    def test_ac2_idempotent_reimport(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dict = os.path.join(tmp, "dictionary_after.json")
            log = os.path.join(tmp, "import_log.json")
            os.environ["IMPORT_INIT_ADMIN"] = "1"
            try:
                rep1, a1, u1 = bi.apply_init(EXISTING, CSV_FILE, out_dict, log)
                d1 = _load(out_dict)
                # 第二次导入：以上一次导出的字典作为现库（真实使用顺序）
                rep2, a2, u2 = bi.apply_init(out_dict, CSV_FILE, out_dict, log)
                d2 = _load(out_dict)
            finally:
                os.environ.pop("IMPORT_INIT_ADMIN", None)
            self.assertEqual((a1, u1), (3, 2))
            self.assertEqual((a2, u2), (0, 0), "第二次导入必须 applied=0")
            codes1 = [t["code"] for t in d1["tags"]]
            codes2 = [t["code"] for t in d2["tags"]]
            self.assertEqual(len(codes1), len(set(codes1)), "不得产生重复标签")
            self.assertEqual(codes1, codes2, "第二次导入后字典不得变化")
            self.assertEqual(len(codes1), 12 + 3, "现库 12 + 新增 3")
            self.assertEqual(rep2["counts"]["will_add"], 0)

    # ---------- AC3 parent_code 不存在的行进错误清单 ----------
    def test_ac3_missing_parent_in_errors(self):
        rep = make_report(CSV_FILE)
        err_rows = {x["row_no"]: x for x in rep["errors"]}
        self.assertIn(14, err_rows)
        e = err_rows[14]
        self.assertEqual(e["code"], "IND-ORPHAN")
        joined = "；".join(e["reasons"])
        self.assertIn("parent_code 不存在", joined)
        self.assertIn("IND-XX9", joined)
        add_codes = {x["code"] for x in rep["will_add"]}
        self.assertNotIn("IND-ORPHAN", add_codes)
        os.environ["IMPORT_INIT_ADMIN"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_dict = os.path.join(tmp, "d.json")
                bi.apply_init(EXISTING, CSV_FILE, out_dict, os.path.join(tmp, "l.json"))
                d = _load(out_dict)
        finally:
            os.environ.pop("IMPORT_INIT_ADMIN", None)
        codes = {t["code"] for t in d["tags"]}
        self.assertNotIn("IND-ORPHAN", codes, "错误行不得被导入")

    # ---------- 双格式一致 ----------
    def test_csv_json_equivalence(self):
        rep_csv = make_report(CSV_FILE)
        rep_json = make_report(JSON_FILE)
        self.assertEqual(rep_csv["counts"], rep_json["counts"])
        strip = lambda r: {k: v for k, v in r.items()
                           if k not in ("source_file", "source_hash")}
        self.assertEqual(strip(rep_csv), strip(rep_json),
                         "同一内容的 CSV 与 JSON 导入文件，分类结果必须完全一致")

    # ---------- 冲突三类 ----------
    def test_conflicts_three_kinds(self):
        rep = make_report(CSV_FILE)
        reasons = " | ".join(x["reason"] for x in rep["conflicts"])
        self.assertIn("文件内同 code 多行且内容不一致", reasons)
        self.assertIn("name 撞名", reasons)
        self.assertIn("SVC-DESIGN", reasons)
        self.assertIn("已软删除标签", reasons)
        codes = {x["code"] for x in rep["conflicts"]}
        self.assertEqual(codes, {"IND-IT-SOFT", "SVC-DESIGN-PRO", "IND-MANU-OLD"})

    # ---------- 冲突与错误行一律不导入 ----------
    def test_errors_and_conflicts_never_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dict = os.path.join(tmp, "d.json")
            os.environ["IMPORT_INIT_ADMIN"] = "1"
            try:
                bi.apply_init(EXISTING, CSV_FILE, out_dict, os.path.join(tmp, "l.json"))
            finally:
                os.environ.pop("IMPORT_INIT_ADMIN", None)
            d = _load(out_dict)
            tags = {t["code"]: t for t in d["tags"]}
            for bad in ("IND-ORPHAN", "IND-BAD-SORT", "SVC-LOOP", "SVC-DESIGN-PRO"):
                self.assertNotIn(bad, tags)
            self.assertNotIn("", tags)
            self.assertEqual(tags["IND-IT-SOFT"]["name"], "软件服务",
                             "冲突行内容不得写入现库")
            self.assertTrue(tags["IND-MANU-OLD"]["deleted_at"], "软删除标签不得被恢复")

    # ---------- 提议模式幂等 ----------
    def test_proposal_queue_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = os.path.join(tmp, "proposals.json")
            rep1, pid, reused1 = bi.apply_proposal(EXISTING, CSV_FILE, queue)
            rep2, pid2, reused2 = bi.apply_proposal(EXISTING, CSV_FILE, queue)
            q = _load(queue)
            self.assertEqual(len(q["proposals"]), 1, "同一文件重复导入不得产生重复提议")
            self.assertFalse(reused1)
            self.assertTrue(reused2)
            self.assertEqual(pid, pid2)
            self.assertTrue(pid.startswith("IMP-"))
            self.assertEqual(q["proposals"][pid]["status"], "pending_review")
            self.assertEqual(q["proposals"][pid]["counts"]["will_add"], 3)

    # ---------- init 模式管理员门禁 ----------
    def test_init_requires_admin(self):
        os.environ.pop("IMPORT_INIT_ADMIN", None)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AssertionError):
                bi.apply_init(EXISTING, CSV_FILE, os.path.join(tmp, "d.json"),
                              os.path.join(tmp, "l.json"))

    # ---------- will_update 逐字段差异 ----------
    def test_will_update_changes_recorded(self):
        rep = make_report(CSV_FILE)
        by_code = {x["code"]: x for x in rep["will_update"]}
        self.assertEqual(by_code["IND-FIN"]["changes"]["definition"]["old"],
                         "银行、证券、保险等金融服务会员企业（虚构）")
        self.assertEqual(by_code["IND-FIN"]["changes"]["definition"]["new"],
                         "银行、证券、保险等金融服务会员企业（虚构，定义 v2）")
        self.assertEqual(by_code["SVC-EVENT"]["changes"]["sort_order"], {"old": 42, "new": 45})

    # ---------- 坏表头 fail-closed ----------
    def test_malformed_csv_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "bad.csv")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write("code,name,parent_code,definition,aliases\nIND-X,X,,d,,1\n")
            with self.assertRaises(AssertionError):
                bi.load_rows(p)

    # ---------- 确定性序列化（幂等口径） ----------
    def test_report_serialization_deterministic(self):
        r1 = bi.dumps(make_report(CSV_FILE))
        r2 = bi.dumps(make_report(CSV_FILE))
        self.assertEqual(r1, r2)
        self.assertNotIn("2026-11-18T", r1)  # 不含运行时刻


if __name__ == "__main__":
    unittest.main(verbosity=2)
