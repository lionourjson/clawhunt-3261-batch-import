#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py -- 【S2-TAX-008】#3261 会员空间平台 · 字典批量导入接口（FastAPI）

把 batch_import.py 的 build_report / apply_proposal / apply_init 包成三个 HTTP 接口，
行为与命令行一一对应（同一套解析与分类代码，同一个 fail-closed 口径）：

  POST /admin/taxonomy/import:dry-run    预演：返回将新增/将更新/冲突/错误四类清单（不改任何数据）
  POST /admin/taxonomy/import:proposal   提议：结果进入复核队列文件桩（pending_review），不改字典
  POST /admin/taxonomy/import:init       初始化：直接生效；仅系统管理员可用（按请求调用者判定）

请求体两种形式（都支持 CSV 与 JSON，按文件名扩展名分派，与命令行一致）：
  1) multipart/form-data，字段 file=<导入文件>（推荐，文件名保留扩展名）；
  2) 原始请求体：Content-Type: application/json（内容即 JSON 导入数组）或 text/csv（内容即 CSV 文本）。

「仅系统管理员可用初始化模式」落在请求的调用者上（第 2 版返工要求）：
  【权限桩】get_caller 从请求头取调用者身份与角色——X-User: 调用者标识；
  X-User-Roles: 逗号分隔角色列表（含 system_admin 才算系统管理员）。
  非 system_admin 调用 import:init 返回 403，且不写任何文件。
  桩与进程环境变量无关：同一进程内不同请求可以带不同调用者。
  真实接入时把 get_caller 替换为会话/JWT 解析依赖（对接 S2 权限工单），见 README 第 6 节对接点。

文件桩（mock，第 2 版返工确认可用）：现库字典与复核队列仍是 JSON 文件，路径只从环境变量读：
  TAXO_IMPORT_EXISTING（缺省 <仓库>/existing_dictionary.json）现库字典
      （init 直接生效 = 原地更新这个文件桩，等价于 CLI 里 --out-dict 指向现库文件的语义；
        因此重复导入同一文件第二次 applied=0，天然幂等）
  TAXO_IMPORT_QUEUE   （缺省 <仓库>/review_queue.json）      复核队列
  TAXO_IMPORT_LOG     （缺省 <仓库>/import_log.json）        init 导入日志
  也可在 create_app(...) 里显式传路径（测试用）。

本地启动：
  pip install -r requirements.txt
  python -m uvicorn app:app --port 8321
接口返回均为 JSON；预演/提议/初始化的 report 部分与命令行 dry-run 报告字段一致。
全部数据为自拟虚构数据（虚构演示），不含任何真实个人信息或企业信息，不出现客户名称。
"""
import json
import os

from fastapi import Depends, FastAPI, HTTPException, Request

import batch_import as bi

HERE = os.path.dirname(os.path.abspath(__file__))

ENV_EXISTING = "TAXO_IMPORT_EXISTING"
ENV_QUEUE = "TAXO_IMPORT_QUEUE"
ENV_LOG = "TAXO_IMPORT_LOG"

ADMIN_ROLE = "system_admin"

# 导入被拒绝的异常族：batch_import 的校验用 AssertionError（fail-closed），
# 解析层会抛 json.JSONDecodeError（ValueError 子类）与 UnicodeDecodeError
_REJECT_EXC = (AssertionError, ValueError, UnicodeDecodeError)


class Caller:
    """调用者身份（权限桩的数据载体；真实接入时由会话/JWT 依赖构造同一结构）。"""

    def __init__(self, user_id, roles):
        self.user_id = user_id
        self.roles = roles


async def get_caller(request: Request) -> Caller:
    """【权限桩】从请求头取调用者身份与角色。
    真实接入点：把本函数换成会话/JWT 解析依赖（对接 S2 权限工单的鉴权中间件），
    返回的 Caller 结构（user_id + roles 集合）保持不变即可，下游无需改动。"""
    user = (request.headers.get("x-user") or "").strip() or "anonymous"
    roles_raw = request.headers.get("x-user-roles") or ""
    roles = {r.strip() for r in roles_raw.split(",") if r.strip()}
    return Caller(user, roles)


def require_init_admin(caller: Caller = Depends(get_caller)) -> Caller:
    """init 接口专用依赖：调用者必须带 system_admin 角色，否则 403（fail-closed）。"""
    if ADMIN_ROLE not in caller.roles:
        raise HTTPException(
            status_code=403,
            detail="init 模式仅系统管理员可用: 调用者 %s 缺少 %s 角色" % (caller.user_id, ADMIN_ROLE))
    return caller


async def _read_upload(request: Request):
    """读出 (内容字节, 文件名)。multipart 取上传文件；原始请求体按 Content-Type 推断文件名。"""
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        up = form.get("file")
        if up is None or isinstance(up, str):
            raise HTTPException(status_code=400, detail="multipart 请求需包含文件字段 file")
        content = await up.read()
        filename = up.filename or "upload.csv"
        if not content:
            raise HTTPException(status_code=400, detail="上传文件为空（%s）" % filename)
        return content, filename
    body = await request.body()
    if not body:
        raise HTTPException(
            status_code=400,
            detail="请求体为空: 请用 multipart 上传导入文件（字段 file），"
                   "或以 application/json（导入数组）/ text/csv（CSV 文本）直接传内容")
    if "json" in ctype:
        return body, "upload.json"
    return body, "upload.csv"


def create_app(existing_path=None, queue_path=None, log_path=None):
    """构建 FastAPI 应用。路径参数缺省时从环境变量读（配置只从环境变量读），再缺省用仓库内文件桩。"""
    paths = {
        "existing": existing_path or os.environ.get(ENV_EXISTING) or os.path.join(HERE, "existing_dictionary.json"),
        "queue": queue_path or os.environ.get(ENV_QUEUE) or os.path.join(HERE, "review_queue.json"),
        "log": log_path or os.environ.get(ENV_LOG) or os.path.join(HERE, "import_log.json"),
    }
    app = FastAPI(
        title="会员空间平台 · 字典批量导入接口（S2-TAX-008，虚构演示）",
        version="2",
        description="包住 batch_import.py 的 build_report / apply_proposal / apply_init；"
                    "权限桩按请求调用者判定，文件桩路径从环境变量读。")

    def _existing_or_500():
        try:
            return bi.load_existing(paths["existing"])
        except FileNotFoundError as e:
            raise HTTPException(status_code=500,
                                detail="现库字典文件桩不存在: %s（环境变量 %s）" % (paths["existing"], ENV_EXISTING)) from e

    @app.post("/admin/taxonomy/import:dry-run",
              summary="预演：四类清单（将新增/将更新/冲突/错误），不改任何数据")
    async def import_dry_run(request: Request, caller: Caller = Depends(get_caller)):
        content, filename = await _read_upload(request)
        try:
            rows, h = bi.load_rows_bytes(content, filename)
            report = bi.build_report(rows, _existing_or_500(), filename, h)
        except _REJECT_EXC as e:
            raise HTTPException(status_code=400, detail="导入被拒绝（fail-closed）: %s" % e) from e
        return report

    @app.post("/admin/taxonomy/import:proposal",
              summary="提议：结果进入复核队列（pending_review），确认后才正式导入；同一文件幂等")
    async def import_proposal(request: Request, caller: Caller = Depends(get_caller)):
        content, filename = await _read_upload(request)
        try:
            report, proposal_id, reused = bi.apply_proposal(
                paths["existing"], None, paths["queue"], content=content, source_name=filename)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500,
                                detail="现库字典文件桩不存在: %s（环境变量 %s）" % (paths["existing"], ENV_EXISTING)) from e
        except _REJECT_EXC as e:
            raise HTTPException(status_code=400, detail="导入被拒绝（fail-closed）: %s" % e) from e
        resp = dict(report)
        resp["proposal_id"] = proposal_id
        resp["proposal_reused"] = reused
        with open(paths["queue"], encoding="utf-8") as f:
            resp["review_queue_size"] = len(json.load(f).get("proposals", {}))
        return resp

    @app.post("/admin/taxonomy/import:init",
              summary="初始化：直接生效（原地更新现库文件桩），仅系统管理员（调用者需 system_admin 角色）；同一文件幂等")
    async def import_init(request: Request, caller: Caller = Depends(require_init_admin)):
        content, filename = await _read_upload(request)
        try:
            # 直接生效 = 原地更新现库文件桩（apply_init 先读后写同一文件），
            # 等价于 CLI「--out-dict 指向现库文件」的语义；重复导入同一文件第二次 applied=0。
            report, n_add, n_upd = bi.apply_init(
                paths["existing"], None, paths["existing"], paths["log"],
                content=content, source_name=filename, admin_confirmed=True)
        except FileNotFoundError as e:
            raise HTTPException(status_code=500,
                                detail="现库字典文件桩不存在: %s（环境变量 %s）" % (paths["existing"], ENV_EXISTING)) from e
        except _REJECT_EXC as e:
            raise HTTPException(status_code=400, detail="导入被拒绝（fail-closed）: %s" % e) from e
        resp = dict(report)
        resp["applied_add"] = n_add
        resp["applied_update"] = n_upd
        return resp

    return app


app = create_app()
