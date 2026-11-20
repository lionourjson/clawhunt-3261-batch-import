#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_api.py -- 【S2-TAX-008】#3261 批量导入初稿字典 · 接口层测试（FastAPI + TestClient，全虚构数据）

第 2 版返工新增（第 1 轮意见【一】）：接口层测试，覆盖
  AC1 预演四类清单在 POST /admin/taxonomy/import:dry-run 上的表现（CSV multipart 与 JSON 请求体）；
  AC2 重复导入同一文件不产生重复标签（init 两连跑 applied=0、字典字节不变；proposal 复用提议）；
  AC3 parent_code 不存在的行进错误清单（接口返回 + init 后字典无该行）；
  init 接口权限：权限桩按请求调用者判定（X-User / X-User-Roles 请求头），
      非 system_admin -> 403 且不落盘，system_admin -> 200；同一进程内两个调用者分别判定，
      证明权限落在请求上而非进程环境变量；
  接口与命令行行为一致（同一文件 dry-run 报告与 CLI 报告逐字段相等）；
  父级级联校验在接口上同样生效（父行无效 + 现库无该 code -> 子行进错误清单）；
  坏表头 / 不支持扩展名 / 非 JSON 数组 / 空请求体 -> 400（fail-closed）。

运行: python -I test_api.py   （依赖: pip install -r requirements.txt）
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
from app import ADMIN_ROLE, create_app  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError as e:  # pragma: no cover
    raise SystemExit("缺少依赖 fastapi/httpx：请先 pip install -r requirements.txt（%s）" % e)

EXISTING = os.path.join(HERE, "existing_dictionary.json")
CSV_FILE = os.path.join(HERE, "import_v1.csv")
JSON_FILE = os.path.join(HERE, "import_v1.json")

ADMIN = {"X-User": "admin-001", "X-User-Roles": ADMIN_ROLE}
EDITOR = {"X-User": "editor-002", "X-User-Roles": "taxonomy_editor"}


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class ImportApiTest(unittest.TestCase):
    def setUp(self):
        os.environ["IMPORT_ASOF"] = "2026-11-18"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        existing = os.path.join(self.tmp.name, "existing.json")
        shutil.copyfile(EXISTING, existing)  # 复制一份字节相同的现库桩，测试不碰仓库文件
        self.existing = existing
        with open(existing, "rb") as f:
            self._existing_bytes_before = f.read()
        self.queue = os.path.join(self.tmp.name, "review_queue.json")
        self.log = os.path.join(self.tmp.name, "import_log.json")
        self.client = TestClient(create_app(
            existing_path=self.existing, queue_path=self.queue, log_path=self.log))

    def _post_dry_run(self, content=None, filename=None, json_body=None, headers=None):
        if json_body is not None:
            return self.client.post("/admin/taxonomy/import:dry-run", content=json_body,
                                    headers={"Content-Type": "application/json"})
        return self.client.post("/admin/taxonomy/import:dry-run",
                                files={"file": (filename, content, "text/csv")},
                                headers=headers or {})

    # ---------- AC1（接口）：预演四类清单 ----------
    def test_dry_run_csv_four_lists(self):
        r = self._post_dry_run(_read_bytes(CSV_FILE), "import_v1.csv")
        self.assertEqual(r.status_code, 200, r.text)
        rep = r.json()
        self.assertEqual(rep["counts"], {"total_rows": 14, "will_add": 3, "will_update": 2,
                                         "conflicts": 4, "errors": 4, "unchanged": 1,
                                         "ignored_duplicates": 0})
        self.assertEqual({x["code"] for x in rep["will_add"]},
                         {"IND-ENE", "IND-ENE-PV", "SVC-SPACE-COWORK"})
        self.assertEqual({x["row_no"] for x in rep["conflicts"]}, {8, 9, 10, 11})
        self.assertEqual({x["row_no"] for x in rep["errors"]}, {12, 13, 14, 15})
        self.assertEqual(rep["package"], "S2-TAX-008")

    def test_dry_run_json_body(self):
        # 原始 application/json 请求体（内容即 JSON 导入数组），与 multipart 等价
        r = self._post_dry_run(json_body=_read_bytes(JSON_FILE))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["counts"]["will_add"], 3)
        self.assertEqual(r.json()["counts"]["errors"], 4)
        self.assertEqual(r.json()["source_file"], "upload.json")

    def test_dry_run_json_multipart(self):
        # multipart 上传 .json 文件
        r = self._post_dry_run(_read_bytes(JSON_FILE), "import_v1.json")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["counts"], {"total_rows": 14, "will_add": 3, "will_update": 2,
                                              "conflicts": 4, "errors": 4, "unchanged": 1,
                                              "ignored_duplicates": 0})

    def test_dry_run_matches_cli(self):
        # 接口与命令行行为一致：同一文件（同字节）的报告逐字段相等
        r = self._post_dry_run(_read_bytes(CSV_FILE), "import_v1.csv")
        self.assertEqual(r.status_code, 200, r.text)
        existing = bi.load_existing(self.existing)
        rows, h = bi.load_rows(CSV_FILE)
        cli_report = bi.build_report(rows, existing, "import_v1.csv", h)
        self.assertEqual(r.json(), cli_report)

    # ---------- AC2（接口）：重复导入幂等 ----------
    def test_init_idempotent_no_duplicate_tags(self):
        payload = _read_bytes(CSV_FILE)
        r1 = self.client.post("/admin/taxonomy/import:init", content=payload,
                              headers={"Content-Type": "text/csv", **ADMIN})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual((r1.json()["applied_add"], r1.json()["applied_update"]), (3, 2))
        with open(self.existing, "rb") as f:
            dict_bytes_1 = f.read()  # init 直接生效 = 现库文件桩被原地更新
        r2 = self.client.post("/admin/taxonomy/import:init", content=payload,
                              headers={"Content-Type": "text/csv", **ADMIN})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual((r2.json()["applied_add"], r2.json()["applied_update"]), (0, 0),
                         "第二次导入必须 applied=0")
        with open(self.existing, "rb") as f:
            dict_bytes_2 = f.read()
        self.assertEqual(dict_bytes_1, dict_bytes_2, "第二次导入后字典文件字节不变")
        tags = json.loads(dict_bytes_1.decode("utf-8"))["tags"]
        codes = [t["code"] for t in tags]
        self.assertEqual(len(codes), len(set(codes)), "不得产生重复标签")
        self.assertEqual(len(codes), 15, "现库 12 + 新增 3")

    def test_proposal_idempotent_reuse(self):
        payload = _read_bytes(CSV_FILE)
        r1 = self.client.post("/admin/taxonomy/import:proposal", content=payload,
                              headers={"Content-Type": "text/csv"})
        r2 = self.client.post("/admin/taxonomy/import:proposal", content=payload,
                              headers={"Content-Type": "text/csv"})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r1.json()["proposal_id"].startswith("IMP-"))
        self.assertFalse(r1.json()["proposal_reused"])
        self.assertTrue(r2.json()["proposal_reused"], "同一文件第二次应复用已有提议（幂等）")
        self.assertEqual(r1.json()["proposal_id"], r2.json()["proposal_id"])
        with open(self.queue, encoding="utf-8") as f:
            q = json.load(f)
        self.assertEqual(len(q["proposals"]), 1, "复核队列不产生重复提议")
        self.assertEqual(q["proposals"][r1.json()["proposal_id"]]["status"], "pending_review")

    # ---------- AC3（接口）：parent_code 不存在 -> 错误清单 ----------
    def test_ac3_missing_parent_on_api(self):
        r = self._post_dry_run(_read_bytes(CSV_FILE), "import_v1.csv")
        self.assertEqual(r.status_code, 200, r.text)
        rep = r.json()
        err14 = [x for x in rep["errors"] if x["row_no"] == 14]
        self.assertEqual(len(err14), 1)
        self.assertEqual(err14[0]["code"], "IND-ORPHAN")
        joined = "；".join(err14[0]["reasons"])
        self.assertIn("parent_code 不存在", joined)
        self.assertIn("IND-XX9", joined)
        self.assertNotIn("IND-ORPHAN", {x["code"] for x in rep["will_add"]})
        r2 = self.client.post("/admin/taxonomy/import:init", content=_read_bytes(CSV_FILE),
                              headers={"Content-Type": "text/csv", **ADMIN})
        self.assertEqual(r2.status_code, 200, r2.text)
        with open(self.existing, encoding="utf-8") as f:
            codes = {t["code"] for t in json.load(f)["tags"]}
        self.assertNotIn("IND-ORPHAN", codes, "错误行不得被 init 写入字典")

    # ---------- init 接口权限：落在请求调用者上（权限桩） ----------
    def test_init_requires_admin_caller(self):
        payload = _read_bytes(CSV_FILE)
        # 1) 非系统管理员 -> 403，不写任何文件
        r = self.client.post("/admin/taxonomy/import:init", content=payload,
                             headers={"Content-Type": "text/csv", **EDITOR})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn(ADMIN_ROLE, r.json()["detail"])
        self.assertIn("editor-002", r.json()["detail"])
        with open(self.existing, "rb") as f:
            self.assertEqual(f.read(), self._existing_bytes_before, "403 时现库文件桩不得被改动")
        self.assertFalse(os.path.exists(self.log), "403 时不得写导入日志")
        # 2) 无角色头（匿名） -> 403
        r_anon = self.client.post("/admin/taxonomy/import:init", content=payload,
                                  headers={"Content-Type": "text/csv"})
        self.assertEqual(r_anon.status_code, 403)
        # 3) 同一进程、同一客户端，仅换请求头：系统管理员 -> 200（权限在请求上，与环境变量无关）
        r_ok = self.client.post("/admin/taxonomy/import:init", content=payload,
                                headers={"Content-Type": "text/csv", **ADMIN})
        self.assertEqual(r_ok.status_code, 200, r_ok.text)
        self.assertEqual(r_ok.json()["applied_add"], 3)
        with open(self.existing, "rb") as f:
            self.assertNotEqual(f.read(), self._existing_bytes_before, "init 生效后现库文件桩应被更新")
        # 4) 进程环境变量不起作用：不设 IMPORT_INIT_ADMIN 也能通过（按调用者判定），
        #    反之设了环境变量但调用者无角色仍被拒
        os.environ.pop("IMPORT_INIT_ADMIN", None)
        r_again = self.client.post("/admin/taxonomy/import:init", content=payload,
                                   headers={"Content-Type": "text/csv", **ADMIN})
        self.assertEqual(r_again.status_code, 200)
        r_denied = self.client.post("/admin/taxonomy/import:init", content=payload,
                                    headers={"Content-Type": "text/csv", **EDITOR})
        self.assertEqual(r_denied.status_code, 403)

    # ---------- 父级级联校验在接口上生效（第 2 版返工） ----------
    def test_parent_cascade_on_api(self):
        cascade_csv = ("code,name,parent_code,definition,aliases,sort_order\n"
                       "IND-BAD-PARENT,坏父级行,,父级行 sort_order 非法（虚构）,,abc\n"
                       "IND-CHILD-1,子级行,IND-BAD-PARENT,引用无效父级的子行（虚构）,,70\n").encode("utf-8")
        r = self._post_dry_run(cascade_csv, "cascade.csv")
        self.assertEqual(r.status_code, 200, r.text)
        rep = r.json()
        err_rows = {x["row_no"]: x for x in rep["errors"]}
        self.assertIn(3, err_rows, "子行应随无效父行进错误清单")
        reason = "；".join(err_rows[3]["reasons"])
        self.assertIn("IND-BAD-PARENT", reason)
        self.assertIn("row_no=2", reason)
        self.assertNotIn("IND-CHILD-1", {x["code"] for x in rep["will_add"]})
        r2 = self.client.post("/admin/taxonomy/import:init", content=cascade_csv,
                              headers={"Content-Type": "text/csv", **ADMIN})
        self.assertEqual(r2.status_code, 200, r2.text)
        with open(self.existing, encoding="utf-8") as f:
            codes = {t["code"] for t in json.load(f)["tags"]}
        self.assertNotIn("IND-CHILD-1", codes, "init 后字典不得出现父级无效的子行")

    # ---------- 坏输入 fail-closed -> 400 ----------
    def test_malformed_uploads_rejected_400(self):
        bad_header = b"code,name,parent_code,definition,aliases\nIND-X,X,,d,,1\n"
        r1 = self._post_dry_run(bad_header, "bad.csv")
        self.assertEqual(r1.status_code, 400, r1.text)
        self.assertIn("表头", r1.json()["detail"])
        r2 = self._post_dry_run(b"not json at all", "upload.json")
        self.assertEqual(r2.status_code, 400)
        r3 = self.client.post("/admin/taxonomy/import:dry-run",
                              files={"file": ("import.txt", b"hello", "text/plain")})
        self.assertEqual(r3.status_code, 400)
        self.assertIn("不支持的文件类型", r3.json()["detail"])
        r4 = self.client.post("/admin/taxonomy/import:dry-run",
                              content=b'{"not": "an array"}',
                              headers={"Content-Type": "application/json"})
        self.assertEqual(r4.status_code, 400)
        self.assertIn("对象数组", r4.json()["detail"])
        r5 = self.client.post("/admin/taxonomy/import:dry-run", content=b"")
        self.assertEqual(r5.status_code, 400)
        self.assertIn("请求体为空", r5.json()["detail"])

    def test_empty_rows_import_zero_counts(self):
        r = self._post_dry_run(b"code,name,parent_code,definition,aliases,sort_order\n", "empty.csv")
        self.assertEqual(r.status_code, 200, r.text)
        rep = r.json()
        self.assertEqual(rep["counts"]["total_rows"], 0)
        self.assertFalse(any(rep[k] for k in ("will_add", "will_update", "conflicts", "errors")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
