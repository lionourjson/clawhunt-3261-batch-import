#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_batch_import.py -- 【S2-TAX-008】#3261 批量导入初稿字典 · 单元测试（全虚构数据）

覆盖工单三条验收标准：
  AC1 预演输出新增、更新、冲突、错误四类清单（测试通过）
  AC2 重复导入同一文件不产生重复标签（测试通过）
  AC3 parent_code 不存在的行被列入错误清单（测试通过）
以及：CSV/JSON 双格式一致、冲突三类（文件内重复/撞名/软删除）、提议队列幂等、
init 模式管理员门禁、冲突与错误行一律不导入、will_update 逐字段差异、坏表头 fail-closed、
父级级联校验（第 2 版返工新增）：父行在错误/冲突清单且现库无该 code 时，引用它的子行
进错误清单（原因点名父行 code 与行号）、不进将新增、init 后不进字典；多级连锁与
A→B→A 环一并拦下；将更新行新父级不可解析同样不导入；父行在本文件里且有效时子行照常新增。

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

    # ---------- 以下为第 2 版返工新增：父级级联校验（父行自身无效的连锁） ----------

    @staticmethod
    def _write_csv(tmp, name, text):
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return p

    def _tmp_existing_copy(self, tmp):
        p = os.path.join(tmp, "existing_copy.json")
        shutil.copyfile(EXISTING, p)
        return p

    def _init_dict(self, tmp, file_path):
        """init 导入并返回 (字典 codes 集合, 报告)。"""
        d = os.path.join(tmp, "after.json")
        os.environ["IMPORT_INIT_ADMIN"] = "1"
        try:
            rep, _, _ = bi.apply_init(EXISTING, file_path, d, os.path.join(tmp, "log.json"))
        finally:
            os.environ.pop("IMPORT_INIT_ADMIN", None)
        return {t["code"] for t in _load(d)["tags"]}, rep

    def test_invalid_parent_row_cascades(self):
        # 父行在本文件里但自身进错误清单（sort_order 非法），现库无该 code：
        # 引用它的子行必须进错误清单（原因点名父行 code 与行号），不进将新增，init 后不进字典
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "cascade_err.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-BAD-PARENT,坏父级行,,父级行 sort_order 非法（虚构）,,abc\n"
                                "IND-CHILD-1,子级行,IND-BAD-PARENT,引用无效父级的子行（虚构）,,70\n")
            rep = make_report(p)
            err_by_row = {x["row_no"]: x for x in rep["errors"]}
            self.assertIn(2, err_by_row, "父行（row_no=2）应在错误清单")
            self.assertIn(3, err_by_row, "子行（row_no=3）应随父行进错误清单")
            reason = "；".join(err_by_row[3]["reasons"])
            self.assertIn("父行无效", reason)
            self.assertIn("IND-BAD-PARENT", reason, "原因要点名父行 code")
            self.assertIn("row_no=2", reason, "原因要点名父行文件行号")
            self.assertIn("进入错误清单", reason)
            self.assertEqual(rep["will_add"], [], "子行不得出现在将新增")
            codes, _ = self._init_dict(tmp, p)
            self.assertNotIn("IND-CHILD-1", codes, "init 后字典不得出现该子行")
            self.assertNotIn("IND-BAD-PARENT", codes)

    def test_conflicted_parent_row_cascades(self):
        # 父行在本文件里但自身进冲突清单（同 code 两行内容不一致），现库无该 code：子行连锁不导入
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "cascade_cfl.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-CFL-PARENT,冲突父级行A,,父级行版本A（虚构）,,10\n"
                                "IND-CFL-PARENT,冲突父级行B,,父级行版本B（虚构）,,11\n"
                                "IND-CHILD-2,子级行,IND-CFL-PARENT,引用冲突父级的子行（虚构）,,70\n")
            rep = make_report(p)
            err_by_row = {x["row_no"]: x for x in rep["errors"]}
            self.assertIn(4, err_by_row, "子行（row_no=4）应随冲突父行进错误清单")
            reason = "；".join(err_by_row[4]["reasons"])
            self.assertIn("IND-CFL-PARENT", reason)
            self.assertIn("进入冲突清单", reason)
            self.assertNotIn("IND-CHILD-2", {x["code"] for x in rep["will_add"]})
            codes, _ = self._init_dict(tmp, p)
            self.assertNotIn("IND-CHILD-2", codes)

    def test_three_level_chain_cascades(self):
        # 三级链 A（自身非法）← B ← C：B、C 全部连锁进错误清单，原因各点名自己的父行
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "chain3.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-CHAIN-A,链头行,,链头 sort_order 非法（虚构）,,abc\n"
                                "IND-CHAIN-B,链中行,IND-CHAIN-A,引用无效链头（虚构）,,80\n"
                                "IND-CHAIN-C,链尾行,IND-CHAIN-B,引用无效链中（虚构）,,81\n")
            rep = make_report(p)
            err_by_row = {x["row_no"]: x for x in rep["errors"]}
            for row in (2, 3, 4):
                self.assertIn(row, err_by_row, "row_no=%d 应在错误清单（三级连锁）" % row)
            rb = "；".join(err_by_row[3]["reasons"])
            rc = "；".join(err_by_row[4]["reasons"])
            self.assertIn("IND-CHAIN-A", rb)
            self.assertIn("row_no=2", rb)
            self.assertIn("IND-CHAIN-B", rc)
            self.assertIn("row_no=3", rc)
            self.assertIn("因父级无效连带不导入", rc, "链中行自身是级联拒绝，父行去向应写连带不导入")
            self.assertEqual(rep["will_add"], [], "三级链任何一级都不得进将新增")
            codes, _ = self._init_dict(tmp, p)
            self.assertFalse({"IND-CHAIN-B", "IND-CHAIN-C"} & codes, "init 后三级链全部不得进字典")

    def test_parent_cycle_rejected(self):
        # A→B→A 环：两行本地字段都合法但谁也无法先成立，不动点迭代后全部拒绝
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "cycle.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-CY-A,环A,IND-CY-B,父级互相引用演示A（虚构）,,10\n"
                                "IND-CY-B,环B,IND-CY-A,父级互相引用演示B（虚构）,,11\n")
            rep = make_report(p)
            err_rows = {x["row_no"] for x in rep["errors"]}
            self.assertEqual(err_rows, {2, 3}, "环上两行都应进错误清单")
            for x in rep["errors"]:
                self.assertIn("因父级无效连带不导入", "；".join(x["reasons"]))
            self.assertEqual(rep["will_add"], [])
            codes, _ = self._init_dict(tmp, p)
            self.assertFalse({"IND-CY-A", "IND-CY-B"} & codes)

    def test_update_row_invalid_parent_rejected(self):
        # 将更新行把 parent_code 改为指向「本文件里但自身无效、现库也没有」的 code：
        # 整行不导入（fail-closed），现库保持原值
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "upd_bad_parent.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-GHOST,幽灵父行,,幽灵行 sort_order 非法（虚构）,,abc\n"
                                "IND-FIN,金融服务,IND-GHOST,银行、证券、保险等金融服务会员企业（虚构，定义 v3）,金融,30\n")
            rep = make_report(p)
            self.assertEqual(rep["will_update"], [], "新父级不可解析的更新行不得进将更新")
            fin_err = [x for x in rep["errors"] if x["code"] == "IND-FIN"]
            self.assertTrue(fin_err, "该更新行应进错误清单")
            reason = "；".join(fin_err[0]["reasons"])
            self.assertIn("IND-GHOST", reason)
            self.assertIn("row_no=2", reason)
            with tempfile.TemporaryDirectory() as tmp2:
                d = os.path.join(tmp2, "after.json")
                os.environ["IMPORT_INIT_ADMIN"] = "1"
                try:
                    bi.apply_init(EXISTING, p, d, os.path.join(tmp2, "log.json"))
                finally:
                    os.environ.pop("IMPORT_INIT_ADMIN", None)
                tags = {t["code"]: t for t in _load(d)["tags"]}
                self.assertEqual(tags["IND-FIN"]["definition"],
                                 "银行、证券、保险等金融服务会员企业（虚构）",
                                 "更新被拒后现库定义必须保持原值")
                self.assertEqual(tags["IND-FIN"]["parent_code"], None)

    def test_valid_parent_chain_still_adds(self):
        # 父行在本文件里且有效：子行、孙行照常级联新增（IND-ENE/IND-ENE-PV 口径的独立用例）
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "valid_chain.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-ROOT-X,根级行,,根级行（虚构）,,90\n"
                                "IND-MID-X,中级行,IND-ROOT-X,中级行（虚构）,,91\n"
                                "IND-LEAF-X,叶级行,IND-MID-X,叶级行（虚构）,,92\n")
            rep = make_report(p)
            self.assertEqual({x["code"] for x in rep["will_add"]},
                             {"IND-ROOT-X", "IND-MID-X", "IND-LEAF-X"}, "有效链三级全部进将新增")
            self.assertEqual(rep["errors"], [])
            codes, _ = self._init_dict(tmp, p)
            self.assertTrue({"IND-ROOT-X", "IND-MID-X", "IND-LEAF-X"} <= codes)

    def test_existing_parent_not_invalidated_by_file_conflict(self):
        # 父 code 在现库有效标签里：即使该 code 的文件行进了冲突清单，引用它的子行照常新增
        # （IND-IT 在现库 active；文件里给它两行冲突更新，子行引用 IND-IT 不受影响）
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "cfl_existing_parent.csv",
                                "code,name,parent_code,definition,aliases,sort_order\n"
                                "IND-IT,信息技术A,,信息技术版本A（虚构）,,20\n"
                                "IND-IT,信息技术B,,信息技术版本B（虚构）,,21\n"
                                "IND-CHILD-3,子级行,IND-IT,引用现库父级的子行（虚构）,,70\n")
            rep = make_report(p)
            self.assertIn("IND-CHILD-3", {x["code"] for x in rep["will_add"]},
                          "父级在现库存在时子行不受文件冲突影响")
            self.assertEqual({x["code"] for x in rep["conflicts"]}, {"IND-IT"})

    def test_empty_import_file_zero_counts(self):
        # 边界：只有表头的 CSV 与空数组 JSON 都产出全零报告（不报错、不导入）
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_csv(tmp, "empty.csv", "code,name,parent_code,definition,aliases,sort_order\n")
            rep = make_report(p)
            self.assertEqual(rep["counts"]["total_rows"], 0)
            self.assertFalse(any(rep[k] for k in ("will_add", "will_update", "conflicts", "errors")))
            pj = os.path.join(tmp, "empty.json")
            with open(pj, "w", encoding="utf-8") as f:
                f.write("[]")
            rep_j = make_report(pj)
            self.assertEqual(rep_j["counts"]["total_rows"], 0)
            self.assertFalse(rep_j["will_add"] or rep_j["errors"])

    def test_missing_existing_stub_fails_closed(self):
        # 边界：现库文件桩缺失时 CLI 拒绝执行（EXIT=1），不静默导入
        with tempfile.TemporaryDirectory() as tmp:
            rc = bi.main(["dry-run", "--existing", os.path.join(tmp, "nope.json"),
                          "--file", CSV_FILE])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
