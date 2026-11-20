#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_import.py -- 【S2-TAX-008】#3261 会员空间平台 · 批量导入初稿字典（CSV 与 JSON）

交付第 2 版（虚构演示，全部数据为自拟虚构）。实现字典批量导入命令与接口的共用核心：
  支持 CSV 与 JSON 两种格式，列为 code、name、parent_code、definition、aliases、sort_order；
  先预演（dry-run）输出 将新增 / 将更新 / 冲突 / 错误 四类清单；
  确认后正式导入：导入结果以「提议」进入复核队列（proposal 模式），
  或在初始化模式下直接生效（init 模式；CLI 仅系统管理员可用，用环境变量 IMPORT_INIT_ADMIN=1 表示；
  HTTP 接口层见 app.py，权限按请求的调用者判定，不依赖进程环境变量）。
  重复导入同一文件是幂等的（按文件内容 sha256 键控：第二次导入 applied=0，不产生重复标签、不产生重复提议）。

用法:
  预演:   python -I batch_import.py dry-run --existing existing_dictionary.json --file import_v1.csv --report report.json
  提议:   python -I batch_import.py apply --mode proposal --existing existing_dictionary.json --file import_v1.csv --queue proposals.json
  直生效: python -I batch_import.py apply --mode init --existing existing_dictionary.json --file import_v1.csv \
              --out-dict dictionary_after.json --queue import_log.json
          （init 模式要求环境变量 IMPORT_INIT_ADMIN=1，否则拒绝执行）
  接口:   pip install -r requirements.txt && python -m uvicorn app:app --port 8321
          （POST /admin/taxonomy/import:dry-run / import:proposal / import:init，见 app.py 与 README）

分类口径（详见 README.md）:
  将新增 will_add    : code 不在现库，且父级级联校验通过（见下）
  将更新 will_update : code 在现库且字段内容有差异（逐字段记录 old/new），且父级级联校验通过
  冲突   conflicts   : 文件内同 code 多行且内容不一致 / code 对应已软删除标签 / name 与现库其他 code 撞名
  错误   errors      : 缺 code 或 name / code 格式非法 / sort_order 非整数 /
                       parent_code 等于自身 / parent_code 不存在（不在现库、也不在本文件）/
                       parent_code 指向已软删除标签 /
                       parent_code 指向本文件内不会被导入的父行（父行在错误或冲突清单，
                       或父行自身因父级无效连带不导入，且现库无该 code；多级连锁一并拦下，
                       原因点名父行 code 与文件行号）
  unchanged          : 与现库完全一致（不算变更，不导入）
  ignored_duplicates : 文件内完全重复的行（保留第一行，其余忽略）
冲突与错误的行一律不导入（fail-closed）。

对接点（mock，写进 README）：现库字典与复核队列目前用 JSON 文件桩，
正式对接 S2-CRM「字典与标签中心」标签表（taxonomy_tags / review_queue）。
配置从环境变量读：IMPORT_ASOF（业务日期，缺省 2026-11-18）、IMPORT_INIT_ADMIN（CLI 初始化模式管理员开关）、
TAXO_IMPORT_EXISTING / TAXO_IMPORT_QUEUE / TAXO_IMPORT_DICT_OUT / TAXO_IMPORT_LOG（接口层文件桩路径，见 app.py）。
输出为确定性序列化（不含运行时刻），同输入重复运行字节一致。
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys

COLUMNS = ["code", "name", "parent_code", "definition", "aliases", "sort_order"]
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*$")
ALIAS_SEP = ";"          # CSV 中别名分隔符
DEFAULT_ASOF = "2026-11-18"
ENV_INIT_ADMIN = "IMPORT_INIT_ADMIN"
ENV_ASOF = "IMPORT_ASOF"
PLATFORM = "会员空间平台（虚构演示）"
PACKAGE_ID = "S2-TAX-008"
PROBLEM_ID = 3261

MSGS = []


def ok(msg):
    MSGS.append(msg)


def asof():
    return os.environ.get(ENV_ASOF, DEFAULT_ASOF)


def dumps(obj):
    """确定性序列化：sort_keys + 固定缩进，不含运行时刻，保证幂等。"""
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- loading
def load_existing(path):
    """现库字典 JSON 桩: {"tags":[{code,name,parent_code,definition,aliases,sort_order,status,deleted_at}]}"""
    with open(path, "rb") as f:
        raw = f.read()
    data = json.loads(raw.decode("utf-8-sig"))
    tags = data.get("tags")
    assert isinstance(tags, list) and tags, "现库字典格式非法: %s（应为 {\"tags\":[...]}）" % path
    d = {}
    for t in tags:
        code = t.get("code", "")
        assert code, "现库字典存在无 code 的标签"
        assert code not in d, "现库字典 code 重复: %s" % code
        d[code] = {
            "code": code,
            "name": t.get("name", ""),
            "parent_code": t.get("parent_code"),
            "definition": t.get("definition", ""),
            "aliases": list(t.get("aliases") or []),
            "sort_order": int(t.get("sort_order", 0)),
            "status": t.get("status", "active"),
            "deleted_at": t.get("deleted_at"),
        }
    return d


def load_rows(path):
    """读取导入文件（按扩展名分派 csv/json），返回 (rows, source_hash)。
    rows: [{row_no, values:{六列原始值或 None}, structural_errors:[...]}]"""
    with open(path, "rb") as f:
        raw = f.read()
    return load_rows_bytes(raw, os.path.basename(path))


def load_rows_bytes(raw, source_name):
    """从内存字节读取导入文件（按 source_name 扩展名分派 csv/json），返回 (rows, source_hash)。
    接口层（app.py）上传内容用；与 load_rows 同一套解析，保证接口与命令行行为一致。"""
    h = sha256_bytes(raw)
    ext = os.path.splitext(source_name or "")[1].lower()
    if ext == ".csv":
        rows = _load_csv(raw)
    elif ext == ".json":
        rows = _load_json(raw)
    else:
        raise AssertionError("不支持的文件类型 %r（仅 .csv / .json）" % (ext or source_name or "(无文件名)"))
    return rows, h


def _load_csv(raw):
    text = raw.decode("utf-8-sig")
    reader = csv.reader(text.splitlines())
    it = iter(reader)
    try:
        header = [c.strip() for c in next(it)]
    except StopIteration:
        raise AssertionError("CSV 文件为空，缺表头")
    if sorted(header) != sorted(COLUMNS):
        raise AssertionError("CSV 表头必须包含列 %s（实际 %s）" % (COLUMNS, header))
    idx = {c: header.index(c) for c in COLUMNS}
    rows = []
    for i, line in enumerate(it):
        if not any(c.strip() for c in line):
            continue  # 跳过整行为空的行
        vals = {}
        for c in COLUMNS:
            vals[c] = line[idx[c]].strip() if idx[c] < len(line) else ""
        rows.append({"row_no": i + 2, "values": vals, "structural_errors": []})
    return rows


def _load_json(raw):
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, list):
        raise AssertionError("JSON 导入文件应为对象数组（实际 %s）" % type(data).__name__)
    rows = []
    for i, item in enumerate(data):
        errs = []
        if not isinstance(item, dict):
            errs.append("JSON 第 %d 项不是对象" % (i + 1))
            item = {}
        vals = {}
        for c in COLUMNS:
            if c not in item:
                errs.append("JSON 第 %d 项缺字段 %s" % (i + 1, c))
                vals[c] = ""
            else:
                v = item[c]
                if c == "aliases" and isinstance(v, list):
                    v = ALIAS_SEP.join(str(x).strip() for x in v)
                vals[c] = "" if v is None else str(v).strip()
        rows.append({"row_no": i + 2, "values": vals, "structural_errors": errs})
    return rows


def _split_aliases(raw_val):
    return [a.strip() for a in raw_val.split(ALIAS_SEP) if a.strip()]


def _norm(row):
    """行标准化（尽力而为，不抛错），返回六列标准化值。"""
    v = row["values"]
    code = v["code"].upper()
    so_raw = v["sort_order"]
    so = None
    if so_raw != "":
        try:
            so = int(so_raw)
        except ValueError:
            so = None
    return {
        "code": code,
        "name": v["name"],
        "parent_code": v["parent_code"].upper() or None,
        "definition": v["definition"],
        "aliases": _split_aliases(v["aliases"]),
        "sort_order": so,
    }


# ---------------------------------------------------------------- validation & classification
def _signature(norm):
    return (norm["name"], norm["parent_code"] or "", norm["definition"],
            tuple(norm["aliases"]), norm["sort_order"])


def build_report(rows, existing, source_name, source_hash):
    """校验 + 分类，产出 dry-run 报告（内存结构，四类清单齐备）。"""
    norm_rows = []
    for r in rows:
        norm_rows.append({"row_no": r["row_no"], "n": _norm(r),
                          "raw_sort_order": r["values"]["sort_order"],
                          "structural_errors": list(r["structural_errors"])})

    # 文件内出现过的 code（仅作「存在性」检查：父 code 在本文件出现过即可通过第 1 步；
    # 父行自身是否会被导入由第 4 步级联校验判定，避免「父行在文件里但自身无效」漏判）
    file_codes = {rn["n"]["code"] for rn in norm_rows if rn["n"]["code"]}
    active_existing = {c: t for c, t in existing.items() if not t["deleted_at"]}

    # 第 1 步：逐行校验（errors）
    errors = []
    valid_rows = []
    for rn in norm_rows:
        n = rn["n"]
        reasons = list(rn["structural_errors"])
        if not n["code"]:
            reasons.append("缺少 code（必填）")
        elif not CODE_RE.match(n["code"]):
            reasons.append("code 格式非法: %r（要求大写字母/数字/连字符，如 IND-IT-SOFT）" % n["code"])
        if not n["name"]:
            reasons.append("缺少 name（必填）")
        if rn["n"]["sort_order"] is None:
            reasons.append("sort_order 非整数或缺省: %r（必填整数）" % rn["raw_sort_order"])
        pc = n["parent_code"]
        if pc is not None:
            if pc == n["code"]:
                reasons.append("parent_code 等于自身 code（%s）" % pc)
            elif pc not in file_codes and pc not in existing:
                reasons.append("parent_code 不存在: %s（既不在现库有效标签，也不在本文件中）" % pc)
            elif pc in existing and existing[pc]["deleted_at"]:
                reasons.append("parent_code 指向已软删除标签: %s" % pc)
        if reasons:
            errors.append({"row_no": rn["row_no"], "code": n["code"] or "-",
                           "name": n["name"] or "-", "reasons": reasons})
        else:
            valid_rows.append(rn)

    # 第 2 步：文件内同 code 多行检查（differ -> conflict；identical -> 保留首行）
    conflicts = []
    ignored_duplicates = []
    by_code = {}
    for rn in valid_rows:
        by_code.setdefault(rn["n"]["code"], []).append(rn)
    unique_rows = []
    for code, grp in by_code.items():
        sigs = {_signature(rn["n"]) for rn in grp}
        if len(sigs) > 1:
            for rn in grp:
                conflicts.append({"row_no": rn["row_no"], "code": code, "name": rn["n"]["name"],
                                  "reason": "文件内同 code 多行且内容不一致（与其他行冲突）"})
        else:
            unique_rows.append(grp[0])
            for rn in grp[1:]:
                ignored_duplicates.append({"row_no": rn["row_no"], "code": code,
                                           "reason": "文件内完全重复行（保留首行 row_no=%d）" % grp[0]["row_no"]})
    unique_rows.sort(key=lambda rn: rn["row_no"])

    # 第 3 步：与现库比对分类（冲突之外得到「将新增候选」与「将更新候选」，
    #          是否成立由第 4 步父级级联校验决定）
    add_candidates, will_update, unchanged = [], [], []
    upd_parent = {}   # code -> 将更新行的新 parent_code（第 4 步校验用）
    upd_meta = {}     # code -> (行号, name)（级联拒绝写错误清单用）
    taken_names = {t["name"]: c for c, t in active_existing.items()}
    for rn in unique_rows:
        n = rn["n"]
        code = n["code"]
        if code in existing:
            old = existing[code]
            if old["deleted_at"]:
                conflicts.append({"row_no": rn["row_no"], "code": code, "name": n["name"],
                                  "reason": "code 对应已软删除标签（deleted_at=%s），需先人工恢复" % old["deleted_at"]})
                continue
            changes = {}
            for k in ("name", "parent_code", "definition", "sort_order"):
                if old[k] != n[k]:
                    changes[k] = {"old": old[k], "new": n[k]}
            if old["aliases"] != n["aliases"]:
                changes["aliases"] = {"old": old["aliases"], "new": n["aliases"]}
            if changes:
                will_update.append({"row_no": rn["row_no"], "code": code, "changes": changes})
                upd_parent[code] = n["parent_code"]
                upd_meta[code] = (rn["row_no"], n["name"])
            else:
                unchanged.append({"row_no": rn["row_no"], "code": code, "note": "与现库完全一致"})
        else:
            if n["name"] in taken_names:
                conflicts.append({"row_no": rn["row_no"], "code": code, "name": n["name"],
                                  "reason": "name 撞名: 与现库 code=%s 的名称重复（name 必须唯一）" % taken_names[n["name"]]})
            else:
                add_candidates.append({"row_no": rn["row_no"], "code": code, "name": n["name"],
                                       "parent_code": n["parent_code"], "definition": n["definition"],
                                       "aliases": n["aliases"], "sort_order": n["sort_order"]})

    # 第 4 步：父级级联校验（不动点迭代，fail-closed）。
    # 「将新增候选」的每一行，parent_code 必须落在「导入后确实存在」的集合里：
    #   现库有效标签，或本轮同样被接受的将新增行。父行在本文件里但自身进了
    #   错误清单或冲突清单、且现库无该 code 时，引用它的子行进错误清单，
    #   原因点名父行 code 与文件行号；其下各级随之连锁不导入（多级同理，
    #   不动点迭代直到不再有新拒绝，A→B→A 环同样全部拒绝）。
    # 将更新行的 code 在现库已存在，不影响其子行的判定；只校验它自己的新 parent_code，
    # 新父级不可解析时该行整体不导入（fail-closed），现库保持原值。
    code_rows_map = {}   # 文件内 code -> 出现过的全部行号（点名父行用）
    for rn in norm_rows:
        c = rn["n"]["code"]
        if c:
            code_rows_map.setdefault(c, []).append(rn["row_no"])
    for v in code_rows_map.values():
        v.sort()

    accepted_codes = set()
    pending = add_candidates
    while True:
        rest = [c for c in pending
                if not (c["parent_code"] is None or c["parent_code"] in active_existing
                        or c["parent_code"] in accepted_codes)]
        if len(rest) == len(pending):
            break
        for c in pending:
            pc = c["parent_code"]
            if pc is None or pc in active_existing or pc in accepted_codes:
                accepted_codes.add(c["code"])
        pending = rest
    rejected_adds = pending
    will_add = [c for c in add_candidates if c["code"] in accepted_codes]

    # 级联拒绝的原因：点名父行 code 与文件行号（错误清单 / 冲突清单 / 连带不导入）
    err_codes = {e["code"] for e in errors if e["code"] != "-"}
    conflict_codes = {c["code"] for c in conflicts}

    def _parent_fate(pc):
        in_err, in_cfl = pc in err_codes, pc in conflict_codes
        if in_err and in_cfl:
            fate = "进入错误与冲突清单"
        elif in_err:
            fate = "进入错误清单"
        elif in_cfl:
            fate = "进入冲突清单"
        else:
            fate = "因父级无效连带不导入"
        rows_txt = "、".join(str(x) for x in code_rows_map.get(pc, ["?"]))
        return ("父行无效, 本行随之不导入: parent_code=%s（文件内 row_no=%s 的父行%s，现库无该 code）"
                % (pc, rows_txt, fate))

    for cand in rejected_adds:
        errors.append({"row_no": cand["row_no"], "code": cand["code"], "name": cand["name"],
                       "reasons": [_parent_fate(cand["parent_code"])]})
    kept_updates = []
    for upd in will_update:
        pc = upd_parent[upd["code"]]
        if pc is not None and pc not in active_existing and pc not in accepted_codes:
            row_no, name = upd_meta[upd["code"]]
            errors.append({"row_no": row_no, "code": upd["code"], "name": name,
                           "reasons": [_parent_fate(pc)]})
        else:
            kept_updates.append(upd)
    will_update = kept_updates

    will_update.sort(key=lambda x: x["row_no"])
    conflicts.sort(key=lambda x: x["row_no"])
    errors.sort(key=lambda x: x["row_no"])
    return {
        "package": PACKAGE_ID,
        "problem_id": PROBLEM_ID,
        "platform": PLATFORM,
        "data_statement": "全部数据为自拟虚构数据（虚构演示），不含任何真实个人信息或企业信息",
        "source_file": os.path.basename(source_name),
        "source_hash": source_hash,
        "asof": asof(),
        "counts": {
            "total_rows": len(rows),
            "will_add": len(will_add),
            "will_update": len(will_update),
            "conflicts": len(conflicts),
            "errors": len(errors),
            "unchanged": len(unchanged),
            "ignored_duplicates": len(ignored_duplicates),
        },
        "will_add": will_add,
        "will_update": will_update,
        "conflicts": conflicts,
        "errors": errors,
        "unchanged": unchanged,
        "ignored_duplicates": ignored_duplicates,
    }


def dry_run(existing_path, file_path, report_path=None):
    existing = load_existing(existing_path)
    rows, h = load_rows(file_path)
    report = build_report(rows, existing, file_path, h)
    if report_path:
        _write(report_path, report)
    c = report["counts"]
    ok("DRY-RUN %s: 总行数 %d | 将新增 %d | 将更新 %d | 冲突 %d | 错误 %d | 一致 %d | 重复忽略 %d"
       % (report["source_file"], c["total_rows"], c["will_add"], c["will_update"],
          c["conflicts"], c["errors"], c["unchanged"], c["ignored_duplicates"]))
    if report_path:
        ok("报告已写出 -> %s" % report_path)
    return report


# ---------------------------------------------------------------- apply
def _write(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(obj))


def apply_proposal(existing_path, file_path, queue_path, report_path=None, content=None, source_name=None):
    """proposal 模式：导入结果以「提议」进入复核队列（不直接改字典）。
    幂等：同一文件（内容 sha256 相同）重复导入复用已有提议，不产生重复提议。
    content 不为 None 时从内存字节读取导入文件（接口层用），source_name 指定文件名。"""
    existing = load_existing(existing_path)
    if content is not None:
        rows, h = load_rows_bytes(content, source_name or file_path or "upload")
    else:
        rows, h = load_rows(file_path)
    report = build_report(rows, existing, source_name or file_path, h)
    proposal_id = "IMP-" + h[:8]
    queue = {"proposals": {}}
    if os.path.exists(queue_path):
        with open(queue_path, encoding="utf-8") as f:
            queue = json.load(f)
    props = queue.get("proposals", {})
    reused = proposal_id in props
    if not reused:
        props[proposal_id] = {
            "proposal_id": proposal_id,
            "source_hash": h,
            "source_file": report["source_file"],
            "asof": asof(),
            "status": "pending_review",
            "counts": report["counts"],
            "will_add": report["will_add"],
            "will_update": report["will_update"],
            "conflicts": report["conflicts"],
            "errors": report["errors"],
        }
    queue["proposals"] = props
    queue["platform"] = PLATFORM
    _write(queue_path, queue)
    if report_path:
        _write(report_path, report)
    ok("PROPOSAL %s: 提议 %s %s（复核队列 %d 条，状态 pending_review，等待确认后正式导入）"
       % (report["source_file"], proposal_id, "复用已有提议（幂等）" if reused else "新建",
          len(props)))
    return report, proposal_id, reused


def apply_init(existing_path, file_path, out_dict_path, log_path, report_path=None,
               content=None, source_name=None, admin_confirmed=False):
    """init 模式：将新增/将更新直接生效（冲突与错误一律不导入）。
    仅系统管理员可用：CLI 要求环境变量 IMPORT_INIT_ADMIN=1（进程级开关）；
    HTTP 接口层先按请求调用者完成权限判定后传 admin_confirmed=True（见 app.py），
    二者必居其一，缺省两者皆无时拒绝执行（fail-closed）。
    content 不为 None 时从内存字节读取导入文件（接口层用），source_name 指定文件名。
    幂等：同一文件第二次导入 applied=0，字典文件字节不变。"""
    if not admin_confirmed and os.environ.get(ENV_INIT_ADMIN, "") != "1":
        raise AssertionError(
            "init 模式仅系统管理员可用：请设置环境变量 %s=1（当前未设置或值不为 1）" % ENV_INIT_ADMIN)
    existing = load_existing(existing_path)
    if content is not None:
        rows, h = load_rows_bytes(content, source_name or file_path or "upload")
    else:
        rows, h = load_rows(file_path)
    report = build_report(rows, existing, source_name or file_path, h)
    d = asof()
    n_add = n_upd = 0
    for it in report["will_add"]:
        existing[it["code"]] = {
            "code": it["code"], "name": it["name"], "parent_code": it["parent_code"],
            "definition": it["definition"], "aliases": list(it["aliases"]),
            "sort_order": it["sort_order"], "status": "active", "deleted_at": None,
        }
        n_add += 1
    for it in report["will_update"]:
        tag = existing[it["code"]]
        for k, ch in it["changes"].items():
            tag[k] = ch["new"]
        n_upd += 1
    tags = [existing[c] for c in sorted(existing)]
    _write(out_dict_path, {"tags": tags, "platform": PLATFORM, "asof": d})
    # 导入日志：同一文件（按 source_hash 键控）重导覆盖原条目 -> 日志字节幂等
    log = {"logs": {}, "platform": PLATFORM}
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            log = json.load(f)
    logs = log.get("logs", {})
    logs[h] = {
        "source_hash": h,
        "source_file": report["source_file"],
        "mode": "init",
        "asof": d,
        "applied_add": n_add,
        "applied_update": n_upd,
        "skipped_conflicts": report["counts"]["conflicts"],
        "skipped_errors": report["counts"]["errors"],
        "note": "冲突与错误的行一律不导入；重复导入同一文件 applied=0（幂等）",
    }
    log["logs"] = logs
    _write(log_path, log)
    if report_path:
        _write(report_path, report)
    ok("INIT %s: 管理员初始化导入生效 | 新增 %d | 更新 %d | 冲突跳过 %d | 错误跳过 %d | 字典 -> %s"
       % (report["source_file"], n_add, n_upd, report["counts"]["conflicts"],
          report["counts"]["errors"], out_dict_path))
    return report, n_add, n_upd


# ---------------------------------------------------------------- CLI
def _setup_stdio():
    """输出到管道/重定向时统一 UTF-8（python -I 的 -E 会忽略 PYTHONIOENCODING，
    Windows 管道缺省按 GBK 编码导致父进程按 UTF-8 解码乱码）；TTY 直连时不改，保持控制台正常显示。"""
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main(argv):
    try:
        _setup_stdio()
        ap = argparse.ArgumentParser(prog="batch_import.py", description="字典批量导入（dry-run / apply）")
        sub = ap.add_subparsers(dest="cmd", required=True)
        p1 = sub.add_parser("dry-run", help="预演：输出将新增/将更新/冲突/错误四类清单")
        p1.add_argument("--existing", required=True)
        p1.add_argument("--file", required=True)
        p1.add_argument("--report")
        p2 = sub.add_parser("apply", help="正式导入：proposal（提议入复核队列）或 init（管理员直接生效）")
        p2.add_argument("--mode", required=True, choices=["proposal", "init"])
        p2.add_argument("--existing", required=True)
        p2.add_argument("--file", required=True)
        p2.add_argument("--queue")
        p2.add_argument("--out-dict")
        p2.add_argument("--report")
        args = ap.parse_args(argv)
        if args.cmd == "dry-run":
            dry_run(args.existing, args.file, args.report)
        else:
            if args.mode == "proposal":
                if not args.queue:
                    raise AssertionError("proposal 模式需要 --queue（复核队列文件路径）")
                apply_proposal(args.existing, args.file, args.queue, args.report)
            else:
                if not (args.queue and args.out_dict):
                    raise AssertionError("init 模式需要 --queue（导入日志路径）与 --out-dict（新字典输出路径）")
                apply_init(args.existing, args.file, args.out_dict, args.queue, args.report)
        for m in MSGS:
            print(m)
        print("EXIT=0")
        return 0
    except AssertionError as e:
        print("IMPORT REJECTED: %s" % e)
        print("EXIT=1")
        return 1
    except (OSError, json.JSONDecodeError) as e:
        print("IMPORT REJECTED: %s" % e)
        print("EXIT=1")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
