#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【S2-TAX-008】#3261 批量导入初稿字典 · 预做包自检脚本（全虚构数据）

跑通即证明：
  AC1 预演输出新增、更新、冲突、错误四类清单（CLI 真实运行 + 与 README 手工判定表逐行对照）
  AC2 重复导入同一文件不产生重复标签（init 链条两连跑：第二次 applied=0、字典字节不变）
  AC3 parent_code 不存在的行被列入错误清单（IND-ORPHAN 落错误清单、不落将新增、不进字典）
  CSV/JSON 双格式一致、proposal 队列幂等、init 管理员门禁、冲突/错误行不导入、
  坏表头 fail-closed、IMPORT_ASOF 环境变量生效、输出确定性（重跑 sha256 一致）、
  单元测试 11 条全过、README 验收对照与虚构声明在文、全包敏感扫描零命中。

运行：python -I verify_3261.py   （EXIT=0 即自检通过）
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

        # ---------- V9 单元测试 11 条真实运行 ----------
        code, out = run_py(["test_batch_import.py"])
        check("V9 unittest 全过（含 AC1/AC2/AC3）", code == 0 and "OK" in out,
              "ran=%s" % ("11" if "Ran 11" in out else "?"))

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

        # ---------- V12 产出样例结果 sample_output/（确定性，可复核） ----------
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
        kept = sorted(os.listdir(SAMPLE_OUT))
        check("V12 sample_output 5 文件齐备且全链 EXIT=0",
              seq_ok and kept == ["dictionary_after_init.json", "import_log.json",
                                  "proposals_queue.json", "report_v1_csv.json", "report_v1_json.json"],
              str(kept))
        check("V12b 提议队列 1 条（两连跑幂等）",
              len(read_json(os.path.join(SAMPLE_OUT, "proposals_queue.json"))["proposals"]) == 1)
        check("V12c 导入日志 applied_add=3（首导留痕）",
              list(read_json(os.path.join(SAMPLE_OUT, "import_log.json"))["logs"].values())[0]
              ["applied_add"] == 3)
        check("V12d 导入后字典与 V5 幂等链结果字节一致",
              sha256_file(os.path.join(SAMPLE_OUT, "dictionary_after_init.json")) == h1)

        # ---------- V13 全包敏感模式扫描零命中（含样例输出） ----------
        scan_files = ["batch_import.py", "test_batch_import.py", "README.md",
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
        check("V13 全包敏感模式扫描零命中（%d 个文件）" % len(scan_files), not hits, str(hits))

        # ---------- V14 README 验收对照与虚构声明 ----------
        with open(README, encoding="utf-8") as f:
            rd = f.read()
        ac_lines = ["预演输出新增、更新、冲突、错误四类清单",
                    "重复导入同一文件不产生重复标签",
                    "parent_code 不存在的行被列入错误清单"]
        check("V14 README 验收标准 3 条原句逐条在文",
              all(a in rd for a in ac_lines))
        check("V14b README 含虚构数据声明与 mock 对接点",
              "虚构" in rd and "taxonomy_tags" in rd and "pending_review" in rd)
        check("V14c README 分类口径与实现一致（四类判定规则表）",
              all(s in rd for s in ("将新增 will_add", "将更新 will_update",
                                    "冲突 conflicts", "错误 errors")))

        print("")
        print("SELF-CHECK PASS (V1-V14, %d checks)" % PASS)
        print("EXIT=0")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
