# OPEN-QUESTIONS.md · 待确认项清单

> **所有"还没定的事"都在这里收敛**——包括文档里的 `⚙️ 待你确认`，
> 也包括**代码注释里承认、但任务清单里没有的缺口**。
> 一、二、三节手工维护；🤖 区**当前人工粘贴**（自动汇总脚本缺失，排期见第二节 #2）。

## 一、阻塞项（不定下来就没法开工）

_（暂无。自动汇总标了 5 处"可能阻塞"，见下方 🤖 区，人工判断后填入此处或降级到第二节）_

## 二、非阻塞项（有保守默认值，可以先开工）

> 处理原则：**每一条不确定，都往"少答、标注、拒答"倒。**
> 默认值必须是**安全的那一侧**，不需要任何人签字。

| # | 问题 | 出现在 | 当前按什么处理 | 改的话影响哪几份文档 | 状态 |
|---|---|---|---|---|---|
| 1 | ~~**`kb_id` 轴检索过滤未实现**~~ | `app/repos/chunk_repo.py`、`app/services/permission.py` | **已由 T17b 实现**（2026-10-15）：检索 SQL 为两条件；`kb:` 授权由 `user_accessible_kb_ids()` 解析成 kb_ids；WARN 护栏随之移除；回归 case 见 `tests/test_permission_leak.py` Case 6 | 已落地 | ✅ 已决（移入第三节 #2） |
| 2 | **`scripts/collect_open_questions.py` 自动汇总脚本缺失**：清单承诺"`⚙️` 标记由脚本汇总到 🤖 区"，但脚本从未存在过；🤖 区当前靠**人工粘贴**维护 | `OPEN-QUESTIONS.md` 文件头说明（本次提交）、`scripts/` 目录 | 🤖 区 AI 扫出来后人工粘贴；每次清单更新（比如 T17b 入账）后，🤖 区的 28 处过时标记需要**手工重新扫一遍**才能同步 —— 下次就会被遗忘 | 仅影响本清单维护流程；不影响任何功能代码 / 测试 / 验收 | ⬜ 未决 |
| 3 | **`status` 枚举有两套值**：`docs/spec.md` AC-02、`docs/api-contract.md` §5.4、`docs/data-model.md` 2.3（documents）用的是 `pending → parsing → chunking → embedding → done`；而 `docs/data-model.md` 2.6（parse_tasks）与 `docs/openapi.yaml` 的 ParseTask 只有 `pending / running / done / failed / canceled`。GET /api/v1/tasks/{id} 返回的正是 ParseTask，api-contract §5.4 示例却写 `"status": "embedding"` —— 同一份契约里出现两台状态机 | `docs/spec.md`、`docs/api-contract.md` §5.4、`docs/data-model.md` 2.3 与 2.6、`docs/openapi.yaml` | **T19 按 2.6 + openapi 执行**（4 态 + `progress` 字段表示进度）；spec 里的阶段名理解为 progress 的细分，统一文档待后续专门改 | 改的话要动 spec.md / api-contract.md / data-model.md 三处示例 | ⬜ 未决 |
| 4 | **`parse_tasks.doc_id` 的可空性**：`docs/data-model.md` 2.6 写 "doc_id: bigint FK"（读起来是"非空 + 外键"），而 `alembic/versions/0003_parse_tasks.py` 实际建的是**可空、无外键**。实现是对的（任务行先于文档存在，worker 跑完才回写 doc_id），是文档措辞没写清 | `docs/data-model.md` 2.6、`alembic/versions/0003_parse_tasks.py` | 按实现现状执行（可空、无 FK）；文档补一句说明待后续 | 改的话只影响 data-model 2.6 的措辞 | ⬜ 未决 |
| 5 | **400 级错误码只有 `QUESTION_INVALID` 一个**：T21 的知识库参数校验（非法 scope / 字段超长）也复用它，`code` 被拉伸成"任何入参不合法"。T21 的兜底是让 `RequestValidationError` 处理器**按路径分流文案**（`/api/v1/query` → 问题参数说明；其他 → 通用"字段缺失或超长"），但 `code` 仍是 `QUESTION_INVALID` | `docs/api-contract.md` §3.3 错误码清单、`app/main.py` | 沿用 T18 处理非法 scope 时的同一妥协；补一个通用入参码（如 `INVALID_PARAMETER`）需同步改 api-contract + openapi | 改的话动 api-contract.md / openapi.yaml 两处 | ⬜ 未决 |
| 6 | **上传接口未接"知识库默认 scope"**：`docs/openapi.yaml` 说上传不传 scope 时取知识库 `default_scope`，但 `app/api/v1/documents.py` 仍硬编码 `scope=internal`（其代码注释写着"T21 后再换成 kb 默认值"）。接上还要顺带校验 `kb_id` 存在；且 dev 库已有 kb_id=1/2/3 的存量文档而无对应知识库行 | `app/api/v1/documents.py`、`docs/openapi.yaml`（POST /documents） | 保持 T18 已验收行为不变；已与项目所有者确认**单独排任务**（会改 T18 行为 + 需给存量 kb 补数据行，超出 T21"只加 CRUD"范围） | 改的话动 documents.py + 可能需要数据补齐脚本 | ⬜ 未决（已确认单独排任务） |

> ~~**这条与第一条性质不同**~~（第一条已决，保留原文备查）：kb 轴是**功能缺口**（影响权限隔离的正确性），脚本缺失是**维护流程缺口**（影响清单能否持续更新）。前者必须在 T17b 补上，后者可以暂缓 —— 暂缓的代价只是下次清单更新时要记得手工同步 🤖 区，不会造成线上风险。

## 三、已决项（留痕，别删）

| # | 问题 | 结论 | 依据 | 决定日期 |
|---|---|---|---|---|
| 1 | T17（越权回归测试集）是否顺带补 kb 轴实现？ | **否**——T17 只做部门轴测试；kb 轴单独排 T17b（补两轴要改 chunk_repo 签名 + permission 数据链，超出"只加测试"范围） | T17 验收时发现缺口，与项目所有者确认 | 2026-10-15 |
| 2 | `kb_id` 轴检索过滤未实现（原第二节 #1） | **T17b 已实现**：两条件 SQL + `PermissionBundle`（scope_keys/kb_ids 两轴）+ 缓存指纹含 kb_ids；越权回归 Case 6 通过 | T17b 验收：`make test T="-k permission"` 全绿 | 2026-10-15 |
| 3 | T19 的"任务状态推进"要不要给 `ingest_document` 加阶段回调（解析/分块/向量化 → 30/60/90）？ | **不加**——保持粗粒度进度（running=0 / done=100 / 失败保留最后值），不改 T13 已完成的 `ingest_document` 签名 | 与项目所有者确认：V0.1 前端只需"状态 + 失败原因"，细粒度进度留 V1 | 2026-10-16 |
| 4 | T19 重试策略：确定性失败（非 UTF-8 / 解析后为空）要不要跳过重试？ | **不跳过**——所有失败一律重试，`retry_count < 3` 即回退 pending 重新入队，达上限转终态 failed；不做异常分类 | 与项目所有者确认：坏文件在解析阶段就中断、根本不调嵌入，3 次重试成本毫秒级；换来"嵌入服务抖动"这类瞬时故障能自愈 | 2026-10-16 |
| 5 | T19 发现的两处 docs 矛盾（`status` 枚举两套值 / `doc_id` 可空性）是否当场改文档？ | **否**——T19 按 `data-model.md` 2.6 + `openapi.yaml` 执行，矛盾记入本清单第二节 #3 / #4，待后续专门改 | 与项目所有者确认 | 2026-10-16 |
| 6 | T21 发现的三处 docs 矛盾按哪个执行？ | **按任务清单 + `openapi.yaml` 执行，spec.md / data-model.md 不改，矛盾记入本清单**：① `docs/spec.md` L343「✗ 不做知识库的在线编辑」与 api-contract / openapi / tasks 把 4 个 KB CRUD 列为 V0.1 冲突 → 以 tasks + openapi 为准（"在线编辑"理解为不做富文本/在线改文档正文，KB 元数据 CRUD 不属此列）；② `docs/data-model.md` 第四节把"删除与合规"整体标 V1/AC-19（7 步），而 openapi 的 DELETE 属 V0.1 且 V0.1 明确不做审计日志 → V0.1 删除知识库**只做前 3 步**（documents 软删 + 真删 chunks + 删对象存储），第 4—7 步留 V1；③ `docs/spec.md` Q-01「新增文档默认可见范围」仍标"待定"，而 openapi 已定 `default_scope: internal` → Q-01 结论即 `internal` | 与项目所有者确认（T21 方案阶段 AskUserQuestion 四项全选推荐项） | 2026-10-16 |
| 7 | T21 DELETE 的"级联清理"在 V0.1 做到哪一步？ | **级联三步**：真删 `chunks` → `documents` 软删（`is_active=false`，行保留）→ 删 `knowledge_bases` 行；对象存储按 `docs/{uuid}/` 前缀整段删，且**先删对象、后删库行**（反过来会留永久孤儿对象＝静默数据留存） | 同上（T21 方案阶段 AskUserQuestion 选"级联三步"） | 2026-10-16 |
| 8 | T21 知识库重名返回什么？ | **新增错误码 `DUPLICATE_KB_NAME`（409）**（已补进 `docs/api-contract.md` §3.3）；判重以数据库 UNIQUE 约束为准，不做"预检查 + 相信它" | 同上（T21 方案阶段 AskUserQuestion 选"新增 DUPLICATE_KB_NAME"） | 2026-10-16 |
| 9 | T21 `GET /knowledge-bases` 列表要不要按权限过滤？ | **否**——V0.1 列出全部；（"谁能看知识库"这一层 V0.1 未建立，实际可见性仍由文档级 `chunks.scope_key + kb_id` 两条件 SQL 决定，列表不构成越权） | 同上（T21 方案阶段 AskUserQuestion 选"列全部"） | 2026-10-16 |

## 四、这张清单怎么用

1. **开工前**：只看第一节。第一节清空了就可以开工。
2. **开工中**：第二节的每一条都要**显式标注**在产出物里（例：答案上写"数据截止 {{时间}}"），
   不能悄悄用默认值。
3. **每条都要有"该问谁"** —— 否则它会一直挂着，永远没人处理。
4. **代码注释里写"要等 TX / 待实现"的，必须同时出现在本清单或 tasks.md** ——
   注释是给读代码的人的，不是给排期的人的。

---

<!-- AUTO-COLLECT:BEGIN -->
### 🤖 自动汇总（2026-10-15 · 共 28 处）

扫描根目录：`/Users/tj/Documents/trae_projects/rag-kb`

> **先说结论**：十来个待确认项里，真正阻塞的通常只有两三个。
> 把阻塞项挑出来后可以立刻开工，其余给保守默认值即可 ——
> 保守默认值本身就是安全的那一侧，不需要任何人签字。

#### 一、可能阻塞（5 处）

| # | 问题 | 出现在 | 所在小节 | 我的建议 | 该问谁 |
|---|---|---|---|---|---|
| 1 | 3. **`> ` 标记的段落必须清空。** | `docs/00-index.md` L134 | 四、三条全局纪律 | ⚙️ 待填 | ⚙️ 待填 |
| 2 | ≤ ¥1500 / 月（含模型调用与服务器）（``） | `docs/constraints.md` L53 | 四、硬约束（最重要的一节） | ⚙️ 待填 | ⚙️ 待填 |
| 3 | [ ] ``（V2）：SQL 通道要连的业务数据源是**哪个库、哪些表**？ | `docs/glossary.md` L219 | 八、需要我确认的术语 | ⚙️ 待填 | ⚙️ 待填 |
| 4 | [ ] ``（V2）：业务口径以**谁的版本为准**？ | `docs/glossary.md` L222 | 八、需要我确认的术语 | ⚙️ 待填 | ⚙️ 待填 |
| 5 | 月度预算 ≤ ¥1500（``），不允许"先用最强模型再说" | `docs/adr/0007-模型接入与provider适配策略.md` L25 | 一、背景 | ⚙️ 待填 | ⚙️ 待填 |

#### 二、非阻塞（23 处，可先给保守默认值开工）

| # | 问题 | 出现在 | 所在小节 | 我的建议 | 该问谁 |
|---|---|---|---|---|---|
| 1 | 每份文档里用 `> ` 标出的段落，是需要你替换的地方。 | `README.md` L101 | 第 2 步：逐份把"实例内容"换成你的真实情况 | ⚙️ 待填 | ⚙️ 待填 |
| 2 | 用的是四位数字前缀，见 ``） | `REPO_CONVENTION.md` L157 | 四、目录结构（与 AGENTS.md 第三节一致 | ⚙️ 待填 | ⚙️ 待填 |
| 3 | #    换成真实数据源时，请逐条替换，并把 `` 清空。 | `config/semantic_layer.yaml` L14 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 4 | name: hr_oa_readonly            # ：数据源叫什么 | `config/semantic_layer.yaml` L20 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 5 | # ：数据更新频率（决定答案里要不要标注"数据截止 T-1"） | `config/semantic_layer.yaml` L26 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 6 | # ：dept_id 与 user_scope_grants.scope_key（如 "dept:RND"）的映射在哪 | `config/semantic_layer.yaml` L44 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 7 | # ：还有哪些指标允许 global？其余一律 own。 | `config/semantic_layer.yaml` L143 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 8 | # ：公司财年是不是自然年？若不是，需要单独的 fiscal_* 解析规则 | `config/semantic_layer.yaml` L159 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 9 | # ：是否还有"字段级权限"——同一张表里，某些列只有特定角色能查 | `config/semantic_layer.yaml` L173 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 10 | `，默认 3 年` | `docs/architecture.md` L388 | 五、数据流与存储 | ⚙️ 待填 | ⚙️ 待填 |
| 11 | > **凡是标 `` 的，都要换成你的真实情况。** | `docs/constraints.md` L7 | (文件开头) | ⚙️ 待填 | ⚙️ 待填 |
| 12 | 峰值约 15 人（``） | `docs/constraints.md` L28 | 二、使用者 | ⚙️ 待填 | ⚙️ 待填 |
| 13 | 约 3200 份文档，纯文本约 800 MB（``） | `docs/constraints.md` L40 | 三、数据情况 | ⚙️ 待填 | ⚙️ 待填 |
| 14 | 有：公开 / 内部 / 部门专属（`` 三级够不够） | `docs/constraints.md` L44 | 三、数据情况 | ⚙️ 待填 | ⚙️ 待填 |
| 15 | 一台内网服务器：8 核 CPU / 32 GB 内存 / **无 GPU**，已装 Ubuntu（`` 能否长期占用） | `docs/constraints.md` L79 | 六、我手上有什么资源 | ⚙️ 待填 | ⚙️ 待填 |
| 16 | [ ] ``：「客户案例」在你们公司是否含客户名称？含的话密级默认是什么？ | `docs/glossary.md` L213 | 八、需要我确认的术语 | ⚙️ 待填 | ⚙️ 待填 |
| 17 | [ ] ``：是否有内部代号/项目名（如"灯塔项目""X 计划"）需要写进术语表？ | `docs/glossary.md` L214 | 八、需要我确认的术语 | ⚙️ 待填 | ⚙️ 待填 |
| 18 | [ ] ``：是否有老员工习惯用的简称（如"手册"特指《员工手册》）？ | `docs/glossary.md` L215 | 八、需要我确认的术语 | ⚙️ 待填 | ⚙️ 待填 |
| 19 | [ ] ``：评估集的**标签体系**（见 `docs/spec.md` Q-06） | `docs/glossary.md` L216 | 八、需要我确认的术语 | ⚙️ 待填 | ⚙️ 待填 |
| 20 | ``，默认 3 年 | `docs/security.md` L184 | 5.3 留存 | ⚙️ 待填 | ⚙️ 待填 |
| 21 | > ``：请和法务/IT 确认三个问题 | `docs/security.md` L204 | 六、合规要点清单 | ⚙️ 待填 | ⚙️ 待填 |
| 22 | ``：请补充你所在公司真实的 5—10 条此类规则，这是 RAG 系统答对的关键。 | `docs/spec.md` L398 | 五、业务流程与规则（只有我能填的部分） | ⚙️ 待填 | ⚙️ 待填 |
| 23 | > `` | `docs/text-to-sql.md` L328 | 7.2 ★ 聚合与行级权限在数学上不可调和 | ⚙️ 待填 | ⚙️ 待填 |

#### 三、按文件分布（哪份文件欠债最多）

| 文件 | 处数 |
|---|---|
| `config/semantic_layer.yaml` | 7 |
| `docs/constraints.md` | 6 |
| `docs/glossary.md` | 6 |
| `docs/security.md` | 2 |
| `README.md` | 1 |
| `REPO_CONVENTION.md` | 1 |
| `docs/00-index.md` | 1 |
| `docs/architecture.md` | 1 |
| `docs/spec.md` | 1 |
| `docs/text-to-sql.md` | 1 |
| `docs/adr/0007-模型接入与provider适配策略.md` | 1 |

> ⚠️ 这是**机械汇总**，只按关键词猜测「阻塞 / 非阻塞」，**必须人工过一遍**。
> 处理完后，把结论填到上面第一节 / 第二节的表里，并删掉这里的对应行。
<!-- AUTO-COLLECT:END -->
