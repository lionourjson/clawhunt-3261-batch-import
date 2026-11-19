# 【S2-TAX-008】会员空间平台 · 批量导入初稿字典（CSV 与 JSON）（任务号 3261）

> 平台名：会员空间平台（虚构演示）。本仓库全部标签、企业、人名、数值均为自拟虚构数据，
> 不含任何真实个人信息或企业信息，不出现客户名称。

字典与标签中心（Stage ii 数据与权限底座）的初稿字典批量导入：**先预演、确认后生效、错误行一律拦下、重复导入幂等**。

## 1. 验收标准逐条对照

| 验收标准（工单原句） | 怎么满足 | 怎么验证 |
| --- | --- | --- |
| 1. 预演输出新增、更新、冲突、错误四类清单（测试通过） | `dry-run` 命令输出 `will_add / will_update / conflicts / errors` 四类清单（外加 `unchanged` 与 `ignored_duplicates` 两个辅助口径）；14 行样例四类齐备（3 新增 / 2 更新 / 4 冲突 / 4 错误），逐行判定见第 4 节手工判定表 | `python -I test_batch_import.py` 中 `test_ac1_four_lists`；`verify_3261.py` V1/V2 |
| 2. 重复导入同一文件不产生重复标签（测试通过） | 导入按文件内容 sha256 键控：init 模式第二次导入分类结果全为 `unchanged`，`applied=0`，字典字节不变；proposal 模式同一文件复用已有提议（`IMP-<hash8>`），不产生重复提议 | `test_ac2_idempotent_reimport`、`test_proposal_queue_idempotent`；`verify_3261.py` V5/V6 |
| 3. parent_code 不存在的行被列入错误清单（测试通过） | 校验器对每个非空 `parent_code` 检查：不在现库有效标签、也不在本文件中 → 记入 `errors`（`parent_code 不存在: IND-XX9`）；等于自身或指向已软删除标签同样记入错误 | `test_ac3_missing_parent_in_errors`；`verify_3261.py` V3 |

## 2. 交付物清单

| 文件 | 说明 |
| --- | --- |
| `batch_import.py` | 导入命令与接口代码：`dry-run`（预演四类清单）与 `apply --mode proposal/init` 两个子命令；函数级接口 `load_existing / load_rows / build_report / apply_proposal / apply_init` 可被测试与上层接口壳直接调用 |
| `existing_dictionary.json` | 现库字典初始状态（12 个虚构标签，含 1 个软删除标签 IND-MANU-OLD） |
| `import_v1.csv` / `import_v1.json` | 同一内容的两种格式样例导入文件（14 行，覆盖四类清单全部场景） |
| `test_batch_import.py` | 单元测试 11 条（覆盖 AC1/AC2/AC3 及冲突三类、双格式一致、提议幂等、管理员门禁、fail-closed） |
| `verify_3261.py` | 自检脚本：CLI 真实运行 + 手工判定逐行对照 + 幂等 + 负例 + 敏感扫描，`EXIT=0` 即通过 |
| `README.md` | 本文件 |
| `sample_output/` | 自检产出的样例结果：预演报告（CSV/JSON 各一份）、复核队列、init 导入后的字典、导入日志 |

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

## 4. 分类口径与样例手工判定表

| 判定 | 规则 |
| --- | --- |
| 将新增 will_add | code 不在现库，且 name 不与现库撞名、parent_code 可解析 |
| 将更新 will_update | code 在现库且内容有差异（报告逐字段记录 old/new）；完全一致记 `unchanged` 不导入 |
| 冲突 conflicts | ① 文件内同 code 多行且内容不一致；② code 对应已软删除标签；③ name 与现库其他 code 撞名。冲突行一律不导入，需人工处理后重导 |
| 错误 errors | 缺 code 或 name / code 格式非法（要求大写字母/数字/连字符）/ sort_order 非整数 / parent_code 等于自身 / parent_code 不存在（不在现库有效标签、也不在本文件）/ parent_code 指向已软删除标签。错误行一律不导入（fail-closed） |

样例 `import_v1.csv` 14 行的手工判定（与测试期望、verify 自检逐行对照一致）：

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

## 5. 环境变量（配置只从环境变量读）

| 环境变量 | 缺省 | 说明 |
| --- | --- | --- |
| `IMPORT_ASOF` | `2026-11-18` | 业务日期（写入报告与导入日志；输出为确定性序列化，不含运行时刻，同输入重跑字节一致） |
| `IMPORT_INIT_ADMIN` | 未设置 | init 模式管理员开关：必须为 `1` 才允许初始化模式直接生效，否则拒绝执行（EXIT=1） |

## 6. mock 对接点

现库字典（`existing_dictionary.json`）与复核队列（`proposals.json`）目前用 JSON 文件桩，正式集成时对接：
S2-CRM「字典与标签中心」的 `taxonomy_tags` 标签表（统一字段 id(UUID)/org_id/site_id/created_at/created_by/updated_at/deleted_at，
软删除口径与本仓库 `deleted_at` 一致）与复核队列表；函数级接口 `build_report / apply_proposal / apply_init`
与 `POST /admin/taxonomy/import:dry-run`、`POST /admin/taxonomy/import:proposal`、`POST /admin/taxonomy/import:init`
三个接口一一对应（本仓库以文件桩先行，接口壳由前端/后端工单实现）。

## 7. 复跑方式（陌生人一条命令可复现）

环境要求：Python 3.11+（仅标准库，无需安装任何第三方包）。

```bash
python -I verify_3261.py        # 自检 EXIT=0（含测试、幂等、负例；同时重新生成 sample_output/）
python -I test_batch_import.py  # 单元测试 11 条 OK
```

## 8. 虚构数据声明

全部标签编码、名称、定义、别名、日期均为自拟虚构数据（虚构演示），不含任何真实个人信息或企业信息，
不出现客户名称；不含密钥与绝对路径；金额口径不涉及（本包无金额字段）。
