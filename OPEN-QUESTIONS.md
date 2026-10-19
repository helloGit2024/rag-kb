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
| 6 | **上传接口未接"知识库默认 scope"**：`docs/openapi.yaml` 说上传不传 scope 时取知识库 `default_scope`，但 `app/api/v1/documents.py` 仍硬编码 `scope=internal`（其代码注释写着"T21 后再换成 kb 默认值"）。接上还要顺带校验 `kb_id` 存在；且 dev 库已有 kb_id=1/2/3 的存量文档而无对应知识库行 | `app/api/v1/documents.py`、`docs/openapi.yaml`（POST /documents） | 保持 T18 已验收行为不变；已与项目所有者确认**单独排任务**（会改 T18 行为 + 需给存量 kb 补数据行，超出 T21"只加 CRUD"范围） | 改的话动 documents.py + 可能需要数据补齐脚本 | ⬜ 未决（**已排任务 T18b**，见 `docs/tasks.md` 2.3） |
| 7 | **T22 未打通 `uploaded_by` 与自定义 `title`**：① documents 行由 worker **异步**创建，上传接口并不知道将来是谁建的（`perm.user_id` 从未进过 worker），故列表 `uploaded_by` 恒为 `null`（契约里是 string）；② 上传不接受契约里的 `title` 表单字段——现有 `title` 由 filename 推得，且 `validate_metadata` 还从 filename 推 `source_type`，要支持自定义 title 必须改 `ingest_document` / worker 签名（T13/T18 已验收链路） | `app/api/v1/documents.py`、`app/services/ingest.py`、`app/workers/ingest_worker.py`、`docs/openapi.yaml` | 与项目所有者确认**合并为同一张后续任务**（两者都要动入库链路，一起改只付一次代价） | 3—4 个文件 + payload 增字段（向后兼容） | ⬜ 未决（**已排任务 T22c**，见 `docs/tasks.md` 2.4） |
| 8 | **文档列表信封不含 `page` / `page_size`**：契约如此（`docs/openapi.yaml` L263—274，与知识库列表 L185—190 不同），T22 按契约实现，前端分页组件需自己保留页码状态 | `docs/openapi.yaml`、`app/schemas/documents.py` | 按契约实现，**仅备案**；若前端需要回显页码，需先改契约 | 改的话动 openapi + schema 两处 | ⬜ 未决（仅备案） |
| 9 | **`GET /tasks/{task_id}` 的 404/401 契约没写**：openapi 该端点只定义了 200 响应，404（任务不存在）与 401（无身份）未定义；T23 按项目惯例实现（404 `NOT_FOUND`、401 `UNAUTHENTICATED`，错误信封一致） | `docs/openapi.yaml` L329—343、`app/api/v1/tasks.py` | 按惯例实现，**仅备案**；下次改 openapi 时补这两个响应定义 | 改的话动 openapi + api-contract 两处 | ⬜ 未决（仅备案） |
| 10 | **任务状态查询只校身份、不校归属**：`GET /tasks/{task_id}` 挂身份依赖（401），但任何登录用户可查任意 task_id 的进度与失败原因。根因同第二节 #7：`parse_tasks` 无 user 字段，上传时 `perm.user_id` 没进 payload，worker 无从回填归属 | `app/api/v1/tasks.py`、`docs/tasks.md` T23 | **仅备案**：task_id 可枚举但只暴露进度与失败原因，不泄露文档内容；V0.1 内网固定测试账号，风险可接受。**待 #7 那张任务打通 `uploaded_by` 时一并补归属校验**（404 口径，与删除一致） | 改的话动 tasks.py + task payload + openapi 契约 | ⬜ 未决（仅备案，随 #7 复审） |
| 11 | **T24 脱敏范围的两处已知缺口**：① 银行卡暂缓——16—19 位裸数字正则会误伤语料里真实存在的合同号/编号（要避免只能加 Luhn 校验，复杂度不值当，V0.1 不做）；② 18 位纯数字业务编号会被身份证正则掩掉（16 位及以下有数字边界保护、安全；constraints.md 说语料以金额/编号为主，18 位编号概率低但非零） | `app/services/pii.py`（docstring 已写明）、`docs/security.md` 4.3、`docs/constraints.md` L43 | **仅备案**：语料本身不含银行卡（constraints.md L43）；18 位编号误伤的后果是"编号被掩"（不泄露、可用性轻微受损），方向安全。V1 语料扩大时复审（银行卡加 Luhn、编号改白名单） | 改的话只动 pii.py 正则与测试 | ⬜ 未决（仅备案，V1 复审） |
| 12 | **同文件重复上传存在竞态窗口，可能入库两行**：`POST /documents` 的重名判重（409）查的是"该 KB 下已有同名有效文档"，但 documents 行由 worker **异步**创建——第一遍上传后 worker 尚未建行时再传一次，两次都判为"不重名"、都 202、最终入库两行。T26 浏览器 e2e 实测踩到（同一 `t26-ok.md` 连点两次 → documents 出现两条 done 行） | `app/api/v1/documents.py`（上传端查重）、`app/services/ingest.py`、`app/workers/ingest_worker.py` | **仅备案，V0.1 未修**：触发需要"worker 建行前极短时间内重复提交"，正常点击间隔（秒级，worker 通常已完成入库）碰不到；影响是同一文档重复入库、检索时该文档被重复召回，不越权、不丢数据。**修复方向**：worker 侧 ingest 落 documents 行前按 `(kb_id, title, is_active)` 二次查重，命中则复用/跳过；或上传端对 `(kb_id, file_md5)` 建唯一约束（R8：先加字段后约束，已排为 **T22b**） | 改的话动 documents.py + ingest.py + worker（+ 可能加 DB 唯一约束与迁移，属高风险改动需先切分支） | ⬜ 未决（**已排任务 T22b**，见 `docs/tasks.md` 2.4） |

> 原第二节 #8（DELETE 文档校范围 vs DELETE 知识库不校范围）已于 2026-10-16 裁决并移入第三节 #15。

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
| 10 | T22 `GET /documents` 列表要不要按权限过滤？ | **是**——可见性（`scope_keys` + `kb_ids` 两轴）下沉到 SQL WHERE（R2）。文档标题常含敏感信息（如"XX 部门降薪方案"），列全部等于泄露；与知识库列表不同，此处不做"列全部"的妥协 | 与项目所有者确认（T22 方案阶段 AskUserQuestion 选"按权限过滤"） | 2026-10-16 |
| 11 | T22 `DELETE /documents/{id}` 要不要校可见范围？ | **是**——不在范围内返回 **404**（不是 403：403 等于告诉调用方"该 id 确实存在"）。过滤条件与列表一致、同在 SQL WHERE（R2） | 与项目所有者确认（删除不可逆，按项目规则先问你；选"校可见范围"） | 2026-10-16 |
| 12 | T22 列表的 `status` 查询参数按什么校验？ | **严格按 `openapi.yaml` 六态**（pending/parsing/chunking/embedding/done/failed），非法值 400、空白串视同不传；不顺从实现的真实分布（实际只会出现 `done`，其余五态为 V1 预留） | 与项目所有者确认（T22 方案阶段 AskUserQuestion 选"严格按 openapi 六态"） | 2026-10-16 |
| 13 | T22 `uploaded_by` 要不要本任务一并打通？ | **否**——列表暂返回 `null`；（documents 行由 worker 异步创建，上传接口不知道将来是谁建的）。与自定义 `title` 合并为同一张后续任务（见第二节 #7） | 与项目所有者确认（T22 方案阶段 AskUserQuestion 选"暂返回 null，另排任务"） | 2026-10-16 |
| 14 | T22 删除文档的"级联清理"做到哪一步？ | **级联三步**：删对象存储 → 真删 `chunks` → `documents` 软删（行保留）；缓存失效与审计日志留 V1（与 T21 知识库删除同口径） | 与项目所有者确认（T22 方案阶段 AskUserQuestion 选"级联三步"） | 2026-10-16 |
| 15 | 原 #8：`DELETE /knowledge-bases/{id}`（T21 只校身份）与 `DELETE /documents/{id}`（T22 校可见范围）删除口径不一致，如何统一？ | **统一为"校可见范围"**：DELETE 知识库按 `kb.default_scope ∈ scope_keys` 且（`kb_ids` 为空 或 `kb.id ∈ kb_ids`）两轴校验——与文档可见性同构（default_scope 就是"这个 KB 默认给谁看"）；不在范围内返回 **404**（不是 403）。保守方向：KB 里可能存在你看不见的文档，看不见就不该删。**PATCH / GET 单个不连带统一**（列表已决"列全部"，见三节 #9），后续再议 | 与项目所有者确认（AskUserQuestion：两轴同构 / 只动 DELETE / fix(kb)）；验证：`make test` 184 passed（新增越权 404 × 2、kb 轴 404+对照组、空 scope ValueError、API 层 404 共 5 条负例/对照）+ 真机 e2e（dept: KB 被 internal 用户删 → 404；internal KB → 204 → 重复 404） | 2026-10-16 |
| 16 | T23 任务状态查询的鉴权做到哪一层？ | **只校身份（401），不校任务归属**——parse_tasks 无 user 字段，归属模型要动 T18 上传链路（正是第二节 #7 那张任务）；task_id 只泄露进度不泄露内容，V0.1 内网固定测试账号风险可接受。备案见第二节 #10，待 #7 打通 uploaded_by 时一并补归属校验（404 口径） | 与项目所有者确认（T23 方案阶段 AskUserQuestion 选"只校身份"） | 2026-10-19 |
| 17 | openapi 未定义 `GET /tasks/{task_id}` 的 404/401，怎么处理？ | **按项目惯例实现 + 备案**——404 `NOT_FOUND`、401 `UNAUTHENTICATED`，错误信封一致；不先改契约（改 openapi 需另行确认），备案见第二节 #9 | 与项目所有者确认（T23 方案阶段 AskUserQuestion 选"按惯例实现+备案"） | 2026-10-19 |
| 18 | T24 脱敏范围做到哪几类？ | **手机号 + 身份证**（保长掩码 `138****5678` / `1101**********7758`）；银行卡暂缓（误伤合同编号，见第二节 #11）。**双点脱敏**：进模型前（`_build_context`，constraints.md 数据合规"这条最硬"）+ 出系统前（答案 + snippet 兜底）；答案脱敏放在 R6 数字校验**之前**（掩码头尾数字是原文子串，`_unverifiable_numbers` 不误判）。入库不脱敏（security.md 4.3：保检索质量） | 与项目所有者确认（T24 方案阶段 AskUserQuestion 选"手机号+身份证"）；验证：`make test` 204 passed（+15）+ 真机 e2e（含 13812345678 的文档 → 答案/snippet 零完整号码、掩码出现、refused=false） | 2026-10-19 |
| 19 | T26 前端三处工程决策 | **① 脚手架算进 T26**（web/ 目录此前不存在，脚手架无独立任务、无独立验收标准，随 T26 一起交付；生成物 26 文件超 8 文件规则，已特批——均为 `npx create-next-app` / `shadcn` 样板，非手写逻辑，拆无可拆）；**② 身份用页面顶部切换器**（存 localStorage + 广播事件），不用环境变量——V0.1 无登录，验收越权场景要能随时换账号，改 env 重启等于没法验收；**③ 前端进 docker-compose**（`make dev` 一键拉起全部，符合"我能验收"承诺），代价是首次拉 node 镜像约 1GB | 与项目所有者确认（T26 方案阶段 AskUserQuestion 三项全选推荐项） | 2026-10-19 |
| 20 | 前端容器化踩到的三个环境坑及处置 | **① shadcn CLI 报 TLS 证书链错误**：本地 npm 走公司代理（配了 `cafile`），Node 自带 fetch 不读该配置 → 装组件时临时 `NODE_TLS_REJECT_UNAUTHORIZED=0`（仅该步，事后即恢复）；**② Google Fonts 拉不到致 build 失败**：`create-next-app` 默认用 `next/font/google` 的 Geist，内网环境本就拉不到 → 改用系统字体栈（内网部署本不该依赖外网字体）；**③ 前端容器 SIGBUS 崩溃**：Next 的 SWC 原生二进制是 glibc 构建——(a) alpine(musl) 镜像下启动即崩，故用 `node:22` Debian 版；(b) 宿主 macOS 的 `node_modules`（darwin 二进制）被挂载进 linux 容器同样崩，故用匿名卷隔离 `node_modules`、绝不让宿主目录覆盖。**副作用**：宿主 `web/` 不再有 node_modules（`npm run build` 在宿主不可用），类型检查/lint 一律在容器内跑（`docker exec ragkb-frontend npx tsc --noEmit`） | T26 实施过程实测，处置已写进 `docker-compose.dev.yml` 注释与 `web/next.config.ts` 注释 | 2026-10-19 |
| 21 | T26 e2e 发现的"同文件重复上传竞态"（第二节 #12）怎么处置？ | **单独排任务 `T22b`**（编号带 b 沿用 T17b 先例，避免打乱既有编号；不塞进 T26——T26 是前端页面，竞态根因在上传+worker 链路，混在一起会让"页面做完"与"后端缺陷修完"两件事互相拖累） | 与项目所有者确认（T26 收尾时选"单独排任务"）；任务行见 `docs/tasks.md` 2.4 | 2026-10-19 |
| 22 | `web/README.md`（`create-next-app` 默认模板说明）留不留？ | **删**——内容是 Next.js 官方模板介绍，与本项目无关；仓库根目录已有项目 `README.md`，留着只是噪音 | 与项目所有者确认（T26 收尾时选"删"） | 2026-10-19 |
| 23 | 第二条 #6 / #7 两张"已确认单独排任务"的缺口何时落到任务行？ | **一并排 T18b（→ 2.3 节）与 T22c（→ 2.4 节）**——两者都已确认要修但一直没编号，留在 OPEN-QUESTIONS 里等于"永远排不上"。编号带字母后缀沿用 T17b 先例，缺陷修复不新占号、不打乱既有编号。T18b 归 2.3（异步任务/上传链路），T22c 归 2.4（接口层） | 与项目所有者确认（T26 收尾时选"一并排 T18b 和 T22c"）；任务行见 `docs/tasks.md` 2.3 / 2.4，两份背景说明紧随其后 | 2026-10-19 |

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
