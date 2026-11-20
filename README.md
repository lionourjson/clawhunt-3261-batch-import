# 【S2-TAX-008】会员空间平台 · 批量导入初稿字典（CSV 与 JSON）（任务号 3261，第 2 版）

> 平台名：会员空间平台（虚构演示）。本仓库全部标签、企业、人名、数值均为自拟虚构数据，
> 不含任何真实个人信息或企业信息，不出现客户名称。

字典与标签中心（Stage ii 数据与权限底座）的初稿字典批量导入：**命令与接口都交**——
**先预演、确认后生效、错误行一律拦下、父级无效连锁拦下、重复导入幂等**。
命令行入口 `batch_import.py`（仅标准库），HTTP 接口入口 `app.py`（FastAPI，包住同一套核心函数，行为与命令行一致）。

## 1. 验收标准逐条对照

| 验收标准（工单原句） | 怎么满足 | 怎么验证 |
| --- | --- | --- |
| 1. 预演输出新增、更新、冲突、错误四类清单（测试通过） | `dry-run` 命令与 `POST /admin/taxonomy/import:dry-run` 都输出 `will_add / will_update / conflicts / errors` 四类清单（外加 `unchanged` 与 `ignored_duplicates` 两个辅助口径）；14 行样例四类齐备（3 新增 / 2 更新 / 4 冲突 / 4 错误），逐行判定见第 5 节手工判定表 | `test_batch_import.py` 的 `test_ac1_four_lists`；接口上 `test_api.py` 的 `test_dry_run_csv_four_lists` / `test_dry_run_matches_cli`；`verify_3261.py` V1/V2/V12a–V12c |
| 2. 重复导入同一文件不产生重复标签（测试通过） | 导入按文件内容 sha256 键控：init 模式第二次导入分类结果全为 `unchanged`，`applied=0`，字典字节不变；接口 init 直接更新现库文件桩，重复调用同样 `applied=0`；proposal 模式同一文件复用已有提议（`IMP-<hash8>`），不产生重复提议 | `test_ac2_idempotent_reimport`、`test_proposal_queue_idempotent`；接口上 `test_init_idempotent_no_duplicate_tags`、`test_proposal_idempotent_reuse`；`verify_3261.py` V5/V6/V12g/V12i |
| 3. parent_code 不存在的行被列入错误清单（测试通过） | 校验器对每个非空 `parent_code` 检查：不在现库、也不在本文件中 → 记入 `errors`（`parent_code 不存在: IND-XX9`）；等于自身、指向已软删除标签、或指向本文件内不会被导入的父行（级联，见第 5 节）同样记入错误 | `test_ac3_missing_parent_in_errors`；接口上 `test_ac3_missing_parent_on_api`；`verify_3261.py` V3/V12h |

## 2. 交付物清单

| 文件 | 说明 |
| --- | --- |
| `batch_import.py` | 导入命令与核心函数：CLI `dry-run` 与 `apply --mode proposal/init` 两个子命令；函数级接口 `load_existing / load_rows / load_rows_bytes / build_report / apply_proposal / apply_init`，命令行、接口、测试三方共用同一套解析与分类（含第 2 版新增的父级级联校验） |
| `app.py` | HTTP 接口（FastAPI）：`POST /admin/taxonomy/import:dry-run`、`import:proposal`、`import:init` 三个路由包住上面三个函数；multipart 文件或 JSON 请求体；权限桩按请求调用者判定初始化权限 |
| `existing_dictionary.json` | 现库字典初始状态（12 个虚构标签，含 1 个软删除标签 IND-MANU-OLD） |
| `import_v1.csv` / `import_v1.json` | 同一内容的两种格式样例导入文件（14 行，覆盖四类清单全部场景） |
| `test_batch_import.py` | 单元测试 20 条（AC1/AC2/AC3、冲突三类、双格式一致、提议幂等、管理员门禁、fail-closed、父级级联五类场景） |
| `test_api.py` | 接口层测试 12 条（三条验收标准在接口上的表现、init 权限桩、接口与 CLI 逐字段一致、级联在接口生效、坏输入 400、现库桩缺失 500 fail-closed） |
| `verify_3261.py` | 自检脚本：CLI 真实运行 + 接口真实调用 + 手工判定逐行对照 + 幂等 + 负例 + 敏感扫描，`EXIT=0` 即通过 |
| `requirements.txt` | 接口层依赖（fastapi、httpx、uvicorn、python-multipart）；命令行本身仅用标准库 |
| `README.md` | 本文件 |
| `sample_output/` | 自检产出的样例结果：预演报告（CSV/JSON 各一份）、复核队列、init 导入后的字典、导入日志、接口响应样例 3 份 |

## 3. 命令用法（在仓库根目录执行）

```bash
# 预演（不改任何数据）：输出将新增/将更新/冲突/错误四类清单
python -I batch_import.py dry-run \
  --existing existing_dictionary.json \
  --file import_v1.csv \
  --report sample_output/report_v1_csv.json

# 提议模式：导入结果以「提议」进入复核队列（pending_review），确认后才正式导入
python -I batch_import.py apply --mode proposal \
  --existing existing_dictionary.json \
  --file import_v1.json \
  --queue sample_output/proposals_queue.json

# 初始化模式：直接生效，仅系统管理员可用（环境变量 IMPORT_INIT_ADMIN=1 表示管理员身份）
IMPORT_INIT_ADMIN=1 python -I batch_import.py apply --mode init \
  --existing existing_dictionary.json \
  --file import_v1.csv \
  --out-dict sample_output/dictionary_after_init.json \
  --queue sample_output/import_log.json
```

CSV 约定：UTF-8，表头必须包含六列 `code, name, parent_code, definition, aliases, sort_order`（列序不限）；
`aliases` 用 `;` 分隔；JSON 为对象数组，`aliases` 可为数组，缺失字段按错误行处理。

## 4. 接口用法（FastAPI，第 2 版新增）

```bash
pip install -r requirements.txt
python -m uvicorn app:app --port 8321
```

| 接口 | 行为（与命令行一一对应） | 权限 |
| --- | --- | --- |
| `POST /admin/taxonomy/import:dry-run` | 预演：返回四类清单报告（JSON），不改任何数据 | 无特殊要求 |
| `POST /admin/taxonomy/import:proposal` | 提议：写入复核队列文件桩（`pending_review`），返回 `proposal_id` / `proposal_reused` / `review_queue_size`；同一文件幂等 | 无特殊要求 |
| `POST /admin/taxonomy/import:init` | 初始化：直接生效（原地更新现库文件桩），返回 `applied_add` / `applied_update`；同一文件幂等（第二次 `applied=0`） | 仅系统管理员：调用者需 `system_admin` 角色，否则 `403`，不写任何文件 |

请求体两种形式（CSV 与 JSON 都支持，按文件名扩展名分派，与命令行同一套解析）：

```bash
# 1) multipart 文件上传（推荐，文件名保留扩展名）
curl -s -X POST http://127.0.0.1:8321/admin/taxonomy/import:dry-run \
  -F "file=@import_v1.csv"

# 2) 原始请求体：application/json（内容即 JSON 导入数组）或 text/csv（内容即 CSV 文本）
curl -s -X POST http://127.0.0.1:8321/admin/taxonomy/import:dry-run \
  -H "Content-Type: application/json" --data-binary @import_v1.json

# 初始化：系统管理员通过请求头表明身份（权限桩），非管理员返回 403
curl -s -X POST http://127.0.0.1:8321/admin/taxonomy/import:init \
  -H "X-User: admin-001" -H "X-User-Roles: system_admin" \
  -F "file=@import_v1.csv"
```

- 预演/初始化返回的报告字段与命令行 `--report` 文件完全一致（`test_dry_run_matches_cli` 逐字段断言）；
- 坏表头、非 JSON 数组、不支持的扩展名、空请求体一律 `400`（fail-closed，不落盘）；
- 非系统管理员调 `import:init` 返回 `403`，现库文件桩与导入日志都不被改动；
- 现库字典文件桩缺失时返回 `500` 并点名环境变量 `TAXO_IMPORT_EXISTING`，不写队列与日志（`test_missing_existing_stub_500`）；
- `403` 与进程环境变量无关：同一进程内不同请求带不同 `X-User` / `X-User-Roles` 即得不同结果（`test_init_requires_admin_caller`）。

## 5. 分类口径与样例手工判定表

| 判定 | 规则 |
| --- | --- |
| 将新增 will_add | code 不在现库，且 name 不与现库撞名、父级级联校验通过 |
| 将更新 will_update | code 在现库且内容有差异（报告逐字段记录 old/new），且父级级联校验通过；完全一致记 `unchanged` 不导入 |
| 冲突 conflicts | ① 文件内同 code 多行且内容不一致；② code 对应已软删除标签；③ name 与现库其他 code 撞名。冲突行一律不导入，需人工处理后重导 |
| 错误 errors | 缺 code 或 name / code 格式非法（要求大写字母/数字/连字符）/ sort_order 非整数 / parent_code 等于自身 / parent_code 不存在（不在现库、也不在本文件）/ parent_code 指向已软删除标签 / **parent_code 指向本文件内不会被导入的父行（第 2 版新增）**。错误行一律不导入（fail-closed） |

**父级级联校验（第 2 版返工新增）**：「将新增」的每一行，其 `parent_code` 必须落在「导入后确实存在」的集合里——现库有效标签，或本轮同样被接受的将新增行。父行在本文件里但自身进了错误清单或冲突清单、且现库无该 code 时，引用它的子行进错误清单，原因**点名父行 code 与文件行号**（如 `父行无效, 本行随之不导入: parent_code=IND-BAD-PARENT（文件内 row_no=2 的父行进入错误清单，现库无该 code）`）；其下各级随之连锁不导入（多级同理，不动点迭代直到不再有新拒绝，A→B→A 环同样全部拒绝）。将更新行的新 `parent_code` 不可解析时该行整体不导入，现库保持原值。父行在本文件里且有效时，子行照常级联新增（样例 IND-ENE 与 IND-ENE-PV 行为不变）。

样例 `import_v1.csv` 14 行的手工判定（与测试期望、verify 自检逐行对照一致，第 2 版判定结果与第 1 版相同）：

| CSV 行 | code | 判定 | 原因 |
| --- | --- | --- | --- |
| 2 | IND-ENE | 将新增 | 新 code（新能源产业，虚构） |
| 3 | IND-ENE-PV | 将新增 | 新 code，父 IND-ENE 在本文件（级联新增） |
| 4 | SVC-SPACE-COWORK | 将新增 | 新 code，父 SVC-SPACE 在现库 |
| 5 | IND-FIN | 将更新 | definition 变更（v2） |
| 6 | SVC-EVENT | 将更新 | sort_order 42 -> 45 |
| 7 | IND-IT | 一致 | 与现库完全一致，不导入 |
| 8 | IND-IT-SOFT | 冲突 | 文件内同 code 两行且内容不一致 |
| 9 | IND-IT-SOFT | 冲突 | 同上（两行都进冲突清单） |
| 10 | SVC-DESIGN-PRO | 冲突 | name「设计服务」与现库 SVC-DESIGN 撞名 |
| 11 | IND-MANU-OLD | 冲突 | code 对应已软删除标签（deleted_at=2026-09-30） |
| 12 | （空） | 错误 | 缺少 code（必填） |
| 13 | IND-BAD-SORT | 错误 | sort_order 非整数: "abc" |
| 14 | IND-ORPHAN | 错误 | parent_code 不存在: IND-XX9（验收标准 3） |
| 15 | SVC-LOOP | 错误 | parent_code 等于自身 code |

## 6. 权限桩与 mock 对接点

**权限（落在请求的调用者上）**：`app.py` 的 `get_caller` 是权限桩——从请求头取调用者身份与角色
（`X-User`: 调用者标识；`X-User-Roles`: 逗号分隔角色列表），`require_init_admin` 依赖要求 `system_admin`
角色，否则 `403`。真实接入时把 `get_caller` 替换为会话/JWT 解析依赖（对接 S2 权限工单的鉴权中间件），
`Caller` 结构（user_id + roles 集合）保持不变即可，下游 `require_init_admin` 与路由无需改动。
CLI 的 `IMPORT_INIT_ADMIN` 环境变量仅是命令行的进程级开关；接口权限只看请求调用者，不依赖环境变量。

**数据桩（mock）**：现库字典（`TAXO_IMPORT_EXISTING`，缺省 `existing_dictionary.json`）、复核队列
（`TAXO_IMPORT_QUEUE`）、导入日志（`TAXO_IMPORT_LOG`）目前用 JSON 文件桩，正式集成时对接：
S2-CRM「字典与标签中心」的 `taxonomy_tags` 标签表（统一字段 id(UUID)/org_id/site_id/created_at/created_by/updated_at/deleted_at，
软删除口径与本仓库 `deleted_at` 一致）与复核队列表；三个接口与本仓库三个核心函数一一对应：
`import:dry-run → build_report`、`import:proposal → apply_proposal`、`import:init → apply_init`，
届时把 `load_existing / _write` 换成对应的 SQLAlchemy 读写即可，分类与级联逻辑不变。

## 7. 环境变量（配置只从环境变量读）

| 环境变量 | 缺省 | 说明 |
| --- | --- | --- |
| `IMPORT_ASOF` | `2026-11-18` | 业务日期（写入报告与导入日志；输出为确定性序列化，不含运行时刻，同输入重跑字节一致） |
| `IMPORT_INIT_ADMIN` | 未设置 | CLI init 模式管理员开关：必须为 `1` 才允许初始化模式直接生效，否则拒绝执行（EXIT=1）。仅命令行用；接口权限按请求调用者判定，不读这个变量 |
| `TAXO_IMPORT_EXISTING` | `<仓库>/existing_dictionary.json` | 接口层现库字典文件桩路径（init 直接生效=原地更新该文件） |
| `TAXO_IMPORT_QUEUE` | `<仓库>/review_queue.json` | 接口层复核队列文件桩路径 |
| `TAXO_IMPORT_LOG` | `<仓库>/import_log.json` | 接口层导入日志文件桩路径 |

## 8. 复跑方式（陌生人照抄可复现）

环境要求：Python 3.11+；接口层需要 `pip install -r requirements.txt`（命令行本身仅标准库）。
本版实测环境：Windows 11，Python 3.14.3，fastapi 0.165.0，httpx 0.28.1，uvicorn 0.54.0，python-multipart 0.0.34。

```bash
pip install -r requirements.txt
python -I verify_3261.py        # 自检 EXIT=0（CLI + 接口 + 幂等 + 负例 + 敏感扫描；同时重新生成 sample_output/）
python -I test_batch_import.py  # 单元测试 20 条 OK
python -I test_api.py           # 接口层测试 12 条 OK
```

用 pytest 跑也行：`python -m pytest -q` → 32 passed（20 + 12）。
有一条与本仓库代码无关的环境警告：`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated`
来自当前环境的 starlette 1.10.0 提示装 httpx2，不影响任何测试结果与接口行为。

## 9. 虚构数据声明

全部标签编码、名称、定义、别名、日期均为自拟虚构数据（虚构演示），不含任何真实个人信息或企业信息，
不出现客户名称；不含密钥与绝对路径；金额口径不涉及（本包无金额字段）。
