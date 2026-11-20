#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【S2-TAX-008】#3261 批量导入初稿字典 · 交付自检脚本（全虚构数据，第 2 版）

跑通即证明：
  AC1 预演输出新增、更新、冲突、错误四类清单（CLI 真实运行 + 与 README 手工判定表逐行对照）
  AC2 重复导入同一文件不产生重复标签（init 链条两连跑：第二次 applied=0、字典字节不变）
  AC3 parent_code 不存在的行被列入错误清单（IND-ORPHAN 落错误清单、不落将新增、不进字典）
  【第 2 版返工】接口层（app.py / FastAPI）：三个接口 dry-run / proposal / init 真实调用，
      与 CLI 行为逐字段一致；init 权限落在请求调用者上（X-User-Roles 桩，非管理员 403 且不落盘）；
      父级级联校验在接口上生效（父行无效 + 现库无该 code -> 子行进错误清单、不进字典）；
      接口坏输入 400（fail-closed）
  【第 2 版返工】父级级联：父行在错误/冲突清单且现库无该 code 时子行连锁不导入（CLI 与接口双覆盖）
  CSV/JSON 双格式一致、proposal 队列幂等、init 管理员门禁、冲突/错误行不导入、
  坏表头 fail-closed、IMPORT_ASOF 环境变量生效、输出确定性（重跑 sha256 一致）、
  单元测试与接口测试全过、README 验收对照与虚构声明在文、全包敏感扫描零命中。

运行：pip install -r requirements.txt 后执行 python -I verify_3261.py   （EXIT=0 即自检通过）
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import batch_import as bi  # noqa: E402

EXISTING = os.path.join(HERE, "existing_dictionary.json")
CSV_FILE = os.path.join(HERE, "import_v1.csv")
JSON_FILE = os.path.join(HERE, "import_v1.json")
README = os.path.join(HERE, "README.md")
SAMPLE_OUT = os.path.join(HERE, "sample_output")
PY = sys.executable

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if not cond:
        print("FAIL %s %s" % (name, detail))
        raise SystemExit("SELF-CHECK FAILED at %s %s" % (name, detail))
    PASS += 1
    print(("PASS %s %s" % (name, detail)).rstrip())


def run_py(args, env=None, use_admin=False):
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUTF8"] = "1"
    e.pop("IMPORT_INIT_ADMIN", None)
    if use_admin:
        e["IMPORT_INIT_ADMIN"] = "1"
    if env:
        e.update(env)
    p = subprocess.run([PY, "-I"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=HERE, env=e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 手工判定表（与 README 第 4 节、test_batch_import 期望一致）
EXPECT_COUNTS = {"total_rows": 14, "will_add": 3, "will_update": 2,
                 "conflicts": 4, "errors": 4, "unchanged": 1, "ignored_duplicates": 0}
EXPECT_ADD = {"IND-ENE", "IND-ENE-PV", "SVC-SPACE-COWORK"}
EXPECT_UPDATE = {"IND-FIN", "SVC-EVENT"}
EXPECT_CONFLICT_ROWS = {8, 9, 10, 11}
EXPECT_ERROR_ROWS = {12, 13, 14, 15}

SENSITIVE_PATTERNS = [
    "clawhunt" + "_key",
    r"api[_-]?key\s*[:=]",
    r"sk-[A-Za-z0-9]{8,}",
    r"Bearer\s+[A-Za-z0-9_\-]{16,}",
    r"[CD]:\\+Users", r"[CD]:/Users",
    r"[CD]:\\+clawhunt", r"[CD]:/clawhunt",
    r"(?<!\d)(138|139|186)\d{8}(?!\d)",
]


def main():
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.environ["IMPORT_ASOF"] = "2026-11-18"  # 输出确定性：自检全程固定业务日期
    tmp = tempfile.mkdtemp(prefix="verify3261_")
    try:
        # ---------- V1 CLI 真实运行 + 四类计数与手工判定一致 ----------
        rep_csv = os.path.join(tmp, "report_csv.json")
        code, out = run_py(["batch_import.py", "dry-run", "--existing", EXISTING,
                            "--file", CSV_FILE, "--report", rep_csv])
        check("V1a dry-run CLI 真实运行", code == 0, "EXIT=%d" % code)
        check("V1b 报告落盘且 JSON 可解析", os.path.isfile(rep_csv))
        rep = read_json(rep_csv)
        check("V1c 计数与手工判定表一致", rep["counts"] == EXPECT_COUNTS, str(rep["counts"]))
        check("V1d CLI 摘要输出含四类计数",
              all(s in out for s in ("将新增 3", "将更新 2", "冲突 4", "错误 4")))

        # ---------- V2 AC1 四类清单齐备且逐行命中 ----------
        check("V2 AC1 将新增 3 行: %s" % sorted(x["code"] for x in rep["will_add"]),
              {x["code"] for x in rep["will_add"]} == EXPECT_ADD)
        check("V2 AC1 将更新 2 行含逐字段 old/new",
              {x["code"] for x in rep["will_update"]} == EXPECT_UPDATE
              and rep["will_update"][0]["changes"]["definition"]["new"].endswith("定义 v2）")
              and any(x["code"] == "SVC-EVENT" and x["changes"]["sort_order"] == {"old": 42, "new": 45}
                      for x in rep["will_update"]))
        check("V2 AC1 冲突 4 行（行 8/9/10/11: 文件内重复×2/撞名/软删除）",
              {x["row_no"] for x in rep["conflicts"]} == EXPECT_CONFLICT_ROWS)
        check("V2 AC1 错误 4 行（行 12/13/14/15: 缺code/非法sort/父不存在/自指）",
              {x["row_no"] for x in rep["errors"]} == EXPECT_ERROR_ROWS)
        check("V2 AC1 一致行 1 行（IND-IT）不算变更",
              {x["code"] for x in rep["unchanged"]} == {"IND-IT"})

        # ---------- V3 AC3 parent_code 不存在 -> 错误清单 ----------
        err14 = [x for x in rep["errors"] if x["code"] == "IND-ORPHAN"]
        check("V3a AC3 IND-ORPHAN（parent_code=IND-XX9）在错误清单", len(err14) == 1
              and any("parent_code 不存在" in r and "IND-XX9" in r for r in err14[0]["reasons"]))
        check("V3b AC3 该行不在将新增清单", "IND-ORPHAN" not in {x["code"] for x in rep["will_add"]})
        d_after = os.path.join(tmp, "dict_after.json")
        code, out = run_py(["batch_import.py", "apply", "--mode", "init", "--existing", EXISTING,
                            "--file", CSV_FILE, "--out-dict", d_after,
                            "--queue", os.path.join(tmp, "log.json")], use_admin=True)
        check("V3c AC3 init 导入后字典无该错误行", code == 0
              and "IND-ORPHAN" not in {t["code"] for t in read_json(d_after)["tags"]})

        # ---------- V4 CSV/JSON 双格式一致 ----------
        rep_json = os.path.join(tmp, "report_json.json")
        code, out = run_py(["batch_import.py", "dry-run", "--existing", EXISTING,
                            "--file", JSON_FILE, "--report", rep_json])
        rj = read_json(rep_json)
        strip = lambda r: {k: v for k, v in r.items() if k not in ("source_file", "source_hash")}
        check("V4 CSV 与 JSON 同内容分类完全一致（仅 source_file/source_hash 不同）",
              code == 0 and strip(rep) == strip(rj))

        # ---------- V5 AC2 幂等：init 两连跑 applied=0 且字典字节不变 ----------
        d1 = os.path.join(tmp, "d.json")
        l1 = os.path.join(tmp, "l.json")
        code, out = run_py(["batch_import.py", "apply", "--mode", "init", "--existing", EXISTING,
                            "--file", CSV_FILE, "--out-dict", d1, "--queue", l1], use_admin=True)
        check("V5a init 第 1 次导入 EXIT=0", code == 0)
        log1 = read_json(l1)["logs"]
        h1 = sha256_file(d1)
        entry = list(log1.values())[0]
        check("V5b 第 1 次导入 applied_add=3 / applied_update=2",
              entry["applied_add"] == 3 and entry["applied_update"] == 2, str(entry))
        code, out = run_py(["batch_import.py", "apply", "--mode", "init", "--existing", d1,
                            "--file", CSV_FILE, "--out-dict", d1, "--queue", l1], use_admin=True)
        log2 = read_json(l1)["logs"]
        h2 = sha256_file(d1)
        tags = read_json(d1)["tags"]
        codes = [t["code"] for t in tags]
        check("V5c AC2 第 2 次导入 applied=0 且不产生重复标签",
              code == 0 and list(log2.values())[0]["applied_add"] == 0
              and list(log2.values())[0]["applied_update"] == 0
              and len(codes) == len(set(codes)) and len(codes) == 15,
              "第二次 applied_add=%d" % list(log2.values())[0]["applied_add"])
        check("V5d AC2 第 2 次导入后字典 sha256 不变", h1 == h2, h1[:16] + "…")
        check("V5e 导入日志同一文件键控去重（1 条，不随重导增长）", len(log2) == 1)
        check("V5f 冲突与错误行一律不导入（错误码缺失/软删除未恢复/撞名未写入）",
              not ({"IND-ORPHAN", "IND-BAD-SORT", "SVC-LOOP", "SVC-DESIGN-PRO", ""} & set(codes))
              and [t for t in tags if t["code"] == "IND-IT-SOFT"][0]["name"] == "软件服务"
              and [t for t in tags if t["code"] == "IND-MANU-OLD"][0]["deleted_at"] == "2026-09-30"
              and [t for t in tags if t["code"] == "IND-FIN"][0]["definition"].endswith("定义 v2）"))

        # ---------- V6 proposal 模式：复核队列幂等 ----------
        q = os.path.join(tmp, "proposals.json")
        run_py(["batch_import.py", "apply", "--mode", "proposal", "--existing", EXISTING,
                "--file", JSON_FILE, "--queue", q])
        code, out = run_py(["batch_import.py", "apply", "--mode", "proposal", "--existing", EXISTING,
                            "--file", JSON_FILE, "--queue", q])
        qd = read_json(q)
        pids = list(qd["proposals"])
        check("V6a AC2 提议模式同一文件两连跑只产生 1 条提议（幂等）",
              code == 0 and len(pids) == 1 and pids[0].startswith("IMP-"), "提议 %s" % pids)
        check("V6b 提议状态 pending_review 且计数齐备",
              qd["proposals"][pids[0]]["status"] == "pending_review"
              and qd["proposals"][pids[0]]["counts"]["will_add"] == 3
              and "复用已有提议" in out)

        # ---------- V7 init 模式管理员门禁（fail-closed 负例 1） ----------
        neg_d = os.path.join(tmp, "neg_d.json")
        code, out = run_py(["batch_import.py", "apply", "--mode", "init", "--existing", EXISTING,
                            "--file", CSV_FILE, "--out-dict", neg_d, "--queue", os.path.join(tmp, "n.json")])
        check("V7 负例: 无 IMPORT_INIT_ADMIN=1 时 init 被拒绝 EXIT=1 且不落盘",
              code == 1 and "仅系统管理员可用" in out and not os.path.exists(neg_d))

        # ---------- V8 坏表头 fail-closed（负例 2） ----------
        bad = os.path.join(tmp, "bad.csv")
        with open(bad, "w", encoding="utf-8", newline="") as f:
            f.write("code,name,parent_code,definition,aliases\nIND-X,X,,d,,\n")
        neg_rep = os.path.join(tmp, "neg_report.json")
        code, out = run_py(["batch_import.py", "dry-run", "--existing", EXISTING,
                            "--file", bad, "--report", neg_rep])
        check("V8 负例: CSV 缺列被拒绝 EXIT=1 且不落盘",
              code == 1 and "表头" in out and not os.path.exists(neg_rep))

        # ---------- V9 单元测试与接口测试真实运行 ----------
        def _ran(out):
            import re as _re
            m = _re.search(r"Ran (\d+) test", out)
            return m.group(1) if m else "?"
        code, out = run_py(["test_batch_import.py"])
        check("V9a 单元测试全过（含 AC1/AC2/AC3 与父级级联）", code == 0 and "OK" in out,
              "ran=%s" % _ran(out))
        code, out = run_py(["test_api.py"])
        check("V9b 接口层测试全过（AC 在接口上的表现 + init 权限 + 级联）",
              code == 0 and "OK" in out, "ran=%s" % _ran(out))

        # ---------- V10 IMPORT_ASOF 环境变量生效 ----------
        rep_asof = os.path.join(tmp, "report_asof.json")
        code, out = run_py(["batch_import.py", "dry-run", "--existing", EXISTING,
                            "--file", CSV_FILE, "--report", rep_asof],
                           env={"IMPORT_ASOF": "2026-11-01"})
        check("V10 IMPORT_ASOF=2026-11-01 生效（报告 asof 变化，缺省 2026-11-18）",
              code == 0 and read_json(rep_asof)["asof"] == "2026-11-01"
              and rep["asof"] == "2026-11-18")

        # ---------- V11 输出确定性：同输入重跑 sha256 一致 ----------
        rep_again = os.path.join(tmp, "report_again.json")
        run_py(["batch_import.py", "dry-run", "--existing", EXISTING,
                "--file", CSV_FILE, "--report", rep_again])
        check("V11 同输入重跑报告 sha256 一致（确定性序列化，不含运行时刻）",
              sha256_file(rep_again) == sha256_file(rep_csv))

        # ---------- V12 接口层（FastAPI app.py）：三接口真实调用（第 2 版返工） ----------
        try:
            from fastapi.testclient import TestClient
        except ImportError as e:
            raise SystemExit("SELF-CHECK FAILED: 接口层依赖缺失 %s —— 先执行 pip install -r requirements.txt" % e)
        import app as api_app
        api_existing = os.path.join(tmp, "api_existing.json")
        shutil.copyfile(EXISTING, api_existing)
        api_queue = os.path.join(tmp, "api_review_queue.json")
        api_log = os.path.join(tmp, "api_import_log.json")
        client = TestClient(api_app.create_app(existing_path=api_existing,
                                               queue_path=api_queue, log_path=api_log))
        csv_bytes = open(CSV_FILE, "rb").read()
        json_bytes = open(JSON_FILE, "rb").read()
        ADMIN_H = {"X-User": "admin-001", "X-User-Roles": "system_admin"}
        EDITOR_H = {"X-User": "editor-002", "X-User-Roles": "taxonomy_editor"}
        r_api = client.post("/admin/taxonomy/import:dry-run",
                            files={"file": ("import_v1.csv", csv_bytes, "text/csv")})
        check("V12a 接口 dry-run(multipart CSV) 200 且计数与手工判定表一致",
              r_api.status_code == 200 and r_api.json()["counts"] == EXPECT_COUNTS,
              str(r_api.status_code))
        rj_api = client.post("/admin/taxonomy/import:dry-run", content=json_bytes,
                             headers={"Content-Type": "application/json"})
        check("V12b 接口 dry-run(JSON 请求体) 200 且同计数（CSV 与 JSON 都支持）",
              rj_api.status_code == 200 and rj_api.json()["counts"] == EXPECT_COUNTS)
        _ex = bi.load_existing(api_existing)
        _rows, _h = bi.load_rows(CSV_FILE)
        _cli = bi.build_report(_rows, _ex, "import_v1.csv", _h)
        check("V12c 接口报告与 CLI dry-run 报告逐字段相等（行为与命令行一致）",
              r_api.json() == _cli)
        r403 = client.post("/admin/taxonomy/import:init", content=csv_bytes,
                           headers={"Content-Type": "text/csv", **EDITOR_H})
        check("V12d 返工权限桩: 接口 init 非 system_admin（taxonomy_editor）403 且现库桩未动、日志未写",
              r403.status_code == 403 and "system_admin" in r403.json()["detail"]
              and sha256_file(api_existing) == sha256_file(EXISTING)
              and not os.path.exists(api_log))
        r403b = client.post("/admin/taxonomy/import:init", content=csv_bytes,
                            headers={"Content-Type": "text/csv"})
        check("V12e 返工权限桩: 匿名调用（无 X-User-Roles）同样 403", r403b.status_code == 403)
        r_init = client.post("/admin/taxonomy/import:init", content=csv_bytes,
                             headers={"Content-Type": "text/csv", **ADMIN_H})
        check("V12f 返工权限桩: 同一进程换请求头 system_admin -> 200 且 applied_add=3 / applied_update=2",
              r_init.status_code == 200 and r_init.json()["applied_add"] == 3
              and r_init.json()["applied_update"] == 2, str(r_init.status_code))
        api_dict_1 = sha256_file(api_existing)
        r_init2 = client.post("/admin/taxonomy/import:init", content=csv_bytes,
                              headers={"Content-Type": "text/csv", **ADMIN_H})
        _codes = [t["code"] for t in read_json(api_existing)["tags"]]
        check("V12g AC2 接口重复导入同一文件 applied=0、现库桩字节不变、15 个标签无重复",
              r_init2.status_code == 200 and r_init2.json()["applied_add"] == 0
              and sha256_file(api_existing) == api_dict_1
              and len(_codes) == 15 and len(set(_codes)) == 15)
        check("V12h AC3 接口导入后字典无错误行（IND-ORPHAN / IND-BAD-SORT / SVC-LOOP）",
              not ({"IND-ORPHAN", "IND-BAD-SORT", "SVC-LOOP", ""} & set(_codes)))
        q1 = client.post("/admin/taxonomy/import:proposal", content=csv_bytes,
                         headers={"Content-Type": "text/csv"})
        q2 = client.post("/admin/taxonomy/import:proposal", content=csv_bytes,
                         headers={"Content-Type": "text/csv"})
        check("V12i AC2 接口 proposal 两连跑复用同一提议（队列 1 条，幂等）",
              q1.status_code == 200 and q2.status_code == 200
              and q1.json()["proposal_id"] == q2.json()["proposal_id"]
              and q2.json()["proposal_reused"] is True and q2.json()["review_queue_size"] == 1)
        cascade_csv = ("code,name,parent_code,definition,aliases,sort_order\n"
                       "IND-BAD-PARENT,坏父级行,,父级行 sort_order 非法（虚构）,,abc\n"
                       "IND-CHILD-1,子级行,IND-BAD-PARENT,引用无效父级的子行（虚构）,,70\n").encode("utf-8")
        rc = client.post("/admin/taxonomy/import:dry-run",
                         files={"file": ("cascade.csv", cascade_csv, "text/csv")})
        _err3 = [x for x in rc.json()["errors"] if x["row_no"] == 3]
        check("V12j 返工级联(接口): 父行无效且现库无该 code -> 子行进错误清单（点名父行 code 与 row_no）、不在将新增",
              rc.status_code == 200 and _err3
              and "IND-BAD-PARENT" in "；".join(_err3[0]["reasons"])
              and "row_no=2" in "；".join(_err3[0]["reasons"])
              and "IND-CHILD-1" not in {x["code"] for x in rc.json()["will_add"]})
        rc2 = client.post("/admin/taxonomy/import:init", content=cascade_csv,
                          headers={"Content-Type": "text/csv", **ADMIN_H})
        check("V12k 返工级联(接口): init 后现库桩无该子行",
              rc2.status_code == 200
              and "IND-CHILD-1" not in {t["code"] for t in read_json(api_existing)["tags"]})
        r400a = client.post("/admin/taxonomy/import:dry-run",
                            files={"file": ("bad.csv", b"code,name,parent_code,definition,aliases\nIND-X,X,,d,,1\n",
                                            "text/csv")})
        r400b = client.post("/admin/taxonomy/import:dry-run", content=b'{"not":"array"}',
                            headers={"Content-Type": "application/json"})
        r400c = client.post("/admin/taxonomy/import:dry-run",
                            files={"file": ("import.txt", b"hello", "text/plain")})
        r400d = client.post("/admin/taxonomy/import:dry-run", content=b"")
        check("V12l 负例: 接口坏输入 400（坏表头/非 JSON 数组/不支持扩展名/空请求体），fail-closed",
              r400a.status_code == 400 and r400b.status_code == 400
              and r400c.status_code == 400 and r400d.status_code == 400,
              "%d/%d/%d/%d" % (r400a.status_code, r400b.status_code, r400c.status_code, r400d.status_code))

        # ---------- V13 产出样例结果 sample_output/（确定性，可复核） ----------
        if os.path.exists(SAMPLE_OUT):
            shutil.rmtree(SAMPLE_OUT)
        os.makedirs(SAMPLE_OUT)
        seq = [
            ["batch_import.py", "dry-run", "--existing", EXISTING, "--file", CSV_FILE,
             "--report", os.path.join(SAMPLE_OUT, "report_v1_csv.json")],
            ["batch_import.py", "dry-run", "--existing", EXISTING, "--file", JSON_FILE,
             "--report", os.path.join(SAMPLE_OUT, "report_v1_json.json")],
            ["batch_import.py", "apply", "--mode", "proposal", "--existing", EXISTING,
             "--file", CSV_FILE, "--queue", os.path.join(SAMPLE_OUT, "proposals_queue.json")],
            ["batch_import.py", "apply", "--mode", "proposal", "--existing", EXISTING,
             "--file", CSV_FILE, "--queue", os.path.join(SAMPLE_OUT, "proposals_queue.json")],
            ["batch_import.py", "apply", "--mode", "init", "--existing", EXISTING,
             "--file", CSV_FILE, "--out-dict", os.path.join(SAMPLE_OUT, "dictionary_after_init.json"),
             "--queue", os.path.join(SAMPLE_OUT, "import_log.json")],
        ]
        seq_ok = True
        for args in seq:
            c, o = run_py(args, use_admin="--mode init" in " ".join(args))
            seq_ok = seq_ok and c == 0
        # 接口响应样例（与 CLI 报告同口径；确定性序列化，不含运行时刻）
        with open(os.path.join(SAMPLE_OUT, "api_dry_run_response.json"), "wb") as f:
            f.write(r_api.content)
        with open(os.path.join(SAMPLE_OUT, "api_proposal_response.json"), "wb") as f:
            f.write(q1.content)
        with open(os.path.join(SAMPLE_OUT, "api_init_response.json"), "wb") as f:
            f.write(r_init.content)
        kept = sorted(os.listdir(SAMPLE_OUT))
        check("V13a sample_output 8 文件齐备（CLI 5 + 接口响应 3）且全链 EXIT=0",
              seq_ok and kept == ["api_dry_run_response.json", "api_init_response.json",
                                  "api_proposal_response.json", "dictionary_after_init.json",
                                  "import_log.json", "proposals_queue.json",
                                  "report_v1_csv.json", "report_v1_json.json"],
              str(kept))
        check("V13b 提议队列 1 条（两连跑幂等）",
              len(read_json(os.path.join(SAMPLE_OUT, "proposals_queue.json"))["proposals"]) == 1)
        check("V13c 导入日志 applied_add=3（首导留痕）",
              list(read_json(os.path.join(SAMPLE_OUT, "import_log.json"))["logs"].values())[0]
              ["applied_add"] == 3)
        check("V13d 导入后字典与 V5 幂等链结果字节一致",
              sha256_file(os.path.join(SAMPLE_OUT, "dictionary_after_init.json")) == h1)
        check("V13e 接口响应样例与 CLI 报告计数一致（四类清单同口径）",
              read_json(os.path.join(SAMPLE_OUT, "api_dry_run_response.json"))["counts"] == EXPECT_COUNTS
              and read_json(os.path.join(SAMPLE_OUT, "api_init_response.json"))["applied_add"] == 3)

        # ---------- V14 全包敏感模式扫描零命中（含样例输出） ----------
        scan_files = ["batch_import.py", "app.py", "test_batch_import.py", "test_api.py",
                      "verify_3261.py", "requirements.txt", "README.md",
                      "existing_dictionary.json", "import_v1.csv", "import_v1.json"]
        scan_files += ["sample_output/" + n for n in kept]
        hits = []
        for rel in scan_files:
            with open(os.path.join(HERE, rel), encoding="utf-8") as f:
                text = f.read()
            for pat in SENSITIVE_PATTERNS:
                m = __import__("re").search(pat, text)
                if m:
                    hits.append((rel, pat, m.group(0)))
        check("V14 全包敏感模式扫描零命中（%d 个文件）" % len(scan_files), not hits, str(hits))

        # ---------- V15 README 验收对照、接口章节与虚构声明 ----------
        with open(README, encoding="utf-8") as f:
            rd = f.read()
        ac_lines = ["预演输出新增、更新、冲突、错误四类清单",
                    "重复导入同一文件不产生重复标签",
                    "parent_code 不存在的行被列入错误清单"]
        check("V15a README 验收标准 3 条原句逐条在文",
              all(a in rd for a in ac_lines))
        check("V15b README 含虚构数据声明与 mock 对接点",
              "虚构" in rd and "taxonomy_tags" in rd and "pending_review" in rd)
        check("V15c README 分类口径与实现一致（四类判定规则表 + 级联口径）",
              all(s in rd for s in ("将新增 will_add", "将更新 will_update",
                                    "冲突 conflicts", "错误 errors", "父行无效")))
        check("V15d README 接口章节与实现一致（三接口路径、权限桩请求头、requirements 安装）",
              all(s in rd for s in ("/admin/taxonomy/import:dry-run", "/admin/taxonomy/import:proposal",
                                    "/admin/taxonomy/import:init", "X-User-Roles", "system_admin",
                                    "pip install -r requirements.txt", "uvicorn")))

        print("")
        print("SELF-CHECK PASS (V1-V15, %d checks)" % PASS)
        print("EXIT=0")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
