# 接口契约（api-contract.md）

> 原需求写了"前后端接口对齐"，但只有这句话——**对齐什么、用什么对齐、谁改谁负责**都没说。
> 这一份补上，配套的机读版本是 `docs/openapi.yaml`。

---

## 一、为什么必须先冻结接口

你现在不懂前端后端，最容易出的问题不是"写不出来"，而是：

```
第 3 周：AI 写的后端接口返回 {"docId": 123, "docName": "手册"}
第 4 周：AI 写的前端读取 data.document_id 和 data.title
        → 页面空白，报错 undefined
        → 你贴报错给 AI，它改后端字段名
        → 三处测试和缓存 key 一起挂掉
        → 花两天修一个本可以不存在的问题
```

**先冻结接口，一次消除三类问题：**

| 好处 | 具体表现 |
|------|---------|
| 前后端可以并行 | 前端用 Mock 数据先画页面，不必等后端 |
| AI 有明确靶子 | 告诉它"严格按 schema 实现"，它就不会自创字段名 |
| 自动生成类型与文档 | 一处修改，前端类型、后端校验、文档同步更新 |

---

## 二、契约的三条纪律

1. **`openapi.yaml` 是唯一事实来源。**
   接口字段以它为准。`docs/api-contract.md`（本文件）只是解释，不能和它冲突。

2. **改接口必须先改 `openapi.yaml`，再改代码。**
   顺序反了就会出现"代码是真的、文档是假的"，而 AI 会按文档写错。

3. **不兼容的改动要升版本号**（`/api/v1` → `/api/v2`），并保留旧版一段时间。
   单人项目里，破坏性改动只允许发生在 V-1 和 V0.1 阶段；V1 上线后必须走版本升级。

---

## 三、通用约定

### 3.1 路径与命名

- 统一前缀 `/api/v1`
- 资源名用复数：`/knowledge-bases`、`/documents`、`/tasks`
- 字段名统一 `snake_case`（与 Python 一致，前端做一次映射）
- 时间统一 ISO 8601 带时区：`2026-11-20T14:30:00+08:00`

### 3.2 认证（V0.1 临时方案）

```
X-KB-User: zhangsan      # V0.1：直接传用户名，仅用于演示
Authorization: Bearer <jwt>   # V1：标准 JWT
```

> **V0.1 用 header 传用户名这件事必须写清楚、且必须在 V1 前删掉。**
> 不写清楚，AI 可能会把它当成正式方案一直用下去。

### 3.3 统一错误格式

所有非 2xx 响应都必须返回同样的结构：

```json
{
  "error": {
    "code": "QUESTION_INVALID",
    "message": "问题长度超过上限 2000 字",
    "detail": { "max_length": 2000, "actual": 3210 }
  }
}
```

**错误码清单（必须全部实现，AI 不允许自创）**

| code | HTTP | 含义 |
|------|------|------|
| `UNAUTHENTICATED` | 401 | 未提供身份 |
| `PERMISSION_DENIED` | 403 | 无权访问该资源 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `QUESTION_INVALID` | 400 | 问题为空/纯符号/超长 |
| `FILE_TOO_LARGE` | 413 | 文件超 20 MB |
| `FILE_TYPE_UNSUPPORTED` | 415 | 文件类型不支持 |
| `DUPLICATE_FILE` | 409 | md5 重复 |
| `RATE_LIMITED` | 429 | 触发限流 |
| `MODEL_UNAVAILABLE` | 503 | 所有模型均不可用 |
| `MODEL_TEST_FAILED` | 422 | 模型连通性测试未通过，配置**未落库/未生效** |
| `DUPLICATE_MODEL_NAME` | 409 | 同 `model_type` 下已存在同名模型配置 |
| `DIMENSION_CHANGE_REQUIRES_REBUILD` | 409 | 试图修改 embedding 类的向量维度（需全量重建） |
| `MODEL_IN_USE` | 409 | 该模型配置仍被知识库或备用关系引用，不可硬删 |
| `BUDGET_EXCEEDED` | 200（degraded） | 预算超限，已降级为仅检索 |
| `SQL_CHANNEL_UNAVAILABLE` | 503 | ★ V2 数据源未启用/连不上/只读账号鉴权失败。**不与 `MODEL_UNAVAILABLE` 合并**（排查动作不同） |
| `SQL_TIMEOUT` | 504 | ★ V2 查询超 `statement_timeout`。**不重试**（重试会放大数据库负载） |
| `SQL_REJECTED` | 422 | ★ V2 外部调用执行接口时被闸门拦下。`/query` 内部走该通道时一律 200 + `refused=true` |
| `INTERNAL_ERROR` | 500 | 其他 |

> **注意 `BUDGET_EXCEEDED` 返回 200 而不是 4xx**：
> 它不是错误，是"降级成功"。前端要正常展示检索片段，只是标记 `degraded=true`。
> 这类"看起来像错误其实不是"的语义，必须在契约里写清楚，否则 AI 会实现成 500。
>
> **反过来，`MODEL_TEST_FAILED` 必须是 422 而不是 200**：
> 它是真的失败了，配置**没有**保存成功。如果实现成 200，前端会以为保存成功，
> 而实际上你点"保存"之后什么都没变——这类静默失败最难排查。

### 3.4 分页约定

```
GET /api/v1/documents?kb_id=3&page=1&page_size=20
→ { "items": [...], "total": 137, "page": 1, "page_size": 20 }
```

`page_size` 上限 100，超过则返回 400。

---

## 四、接口清单（V0.1 / V1 / V2）

| # | 方法 | 路径 | 说明 | 版本 |
|---|------|------|------|------|
| 1 | GET | `/healthz` | 健康检查（含数据库、Redis、模型连通性） | V-1 |
| 2 | POST | `/api/v1/query` | ★ 核心问答接口 | V-1 |
| 3 | GET | `/api/v1/query/stream` | 流式问答（SSE） | V1 |
| 4 | POST | `/api/v1/feedback` | 提交 👍👎 反馈 | V1 |
| 5 | GET | `/api/v1/knowledge-bases` | 知识库列表 | V0.1 |
| 6 | POST | `/api/v1/knowledge-bases` | 创建知识库 | V0.1 |
| 7 | PATCH | `/api/v1/knowledge-bases/{id}` | 修改知识库 | V0.1 |
| 8 | DELETE | `/api/v1/knowledge-bases/{id}` | 删除知识库 | V0.1 |
| 9 | GET | `/api/v1/documents` | 文档列表（含状态、失败原因） | V0.1 |
| 10 | POST | `/api/v1/documents` | 上传文档（异步，立即返回 task_id） | V0.1 |
| 11 | DELETE | `/api/v1/documents/{id}` | 删除文档（触发全链路清理） | V0.1 |
| 12 | GET | `/api/v1/tasks/{id}` | 查询任务状态与失败原因 | V0.1 |
| 13 | POST | `/api/v1/admin/model-test` | 模型连通性测试（`api_key` 一次性输入，不落库） | V1 |
| 14 | GET | `/api/v1/admin/models` | 模型配置列表（**密钥只返回引用，不返回明文**） | V1 |
| 15 | POST | `/api/v1/admin/models` | 新建模型配置（**保存前强制连通性测试，不通过不落库**） | V1 |
| 16 | PATCH | `/api/v1/admin/models/{id}` | 修改模型配置（改后需重测；维度变更被拒） | V1 |
| 17 | DELETE | `/api/v1/admin/models/{id}` | 停用模型配置（默认软停用；被引用时拒绝硬删） | V1 |
| 18 | GET | `/api/v1/admin/stats` | 指标统计（QPS、延迟、成本、拒答率） | V1 |
| 19 | GET | `/api/v1/admin/gaps` | 答不出来的问题排行 | V1 |
| 20 | GET | `/api/v1/admin/audit-logs` | 审计日志查询 | V1 |
| 21 | POST | `/api/v1/admin/degrade` | ★ 全局降级开关（一键关闭 LLM） | V1 |
| 22 | GET | `/api/v1/eval/datasets` | 评估数据集列表（含分布、标签统计、未确认条目数） | V1 |
| 23 | POST | `/api/v1/eval/datasets` | 注册评估数据集（上传 jsonl/csv；含未确认题目则以 `draft` 落库） | V1 |
| 24 | GET | `/api/v1/eval/datasets/{id}` | 数据集详情（含前 20 条预览与 checksum） | V1 |
| 25 | GET | `/api/v1/eval/datasets/{id}/items` | 条目列表（按类别/标签/确认状态筛选、预览） | V1 |
| 26 | GET | `/api/v1/eval/runs` | 评估运行历史（含配置快照摘要、是否基线） | V1 |
| 27 | POST | `/api/v1/eval/runs` | 新建评估任务（异步；**跑前校验 checksum**） | V1 |
| 28 | GET | `/api/v1/eval/runs/{id}` | 运行详情（指标面板数据 + 归因候选分布 + 与基线差值） | V1 |
| 29 | GET | `/api/v1/eval/runs/{id}/items` | 逐条结果（`?changed_only=true` 只看相对基线变化的） | V1 |
| 30 | GET | `/api/v1/eval/runs/{id}/export` | 导出 CSV / XLSX（**不提供服务端 PDF**） | V1 |
| 31 | POST | `/api/v1/eval/runs/{id}/baseline` | 将该次运行设为该数据集下的基线 | V1 |
| 32 | GET | `/api/v1/badcases` | BadCase 列表（**已确认的问题**，非用户反馈流） | V1 |
| 33 | GET | `/api/v1/badcases/{id}` | BadCase 详情（含 evidence） | V1 |
| 34 | PATCH | `/api/v1/badcases/{id}` | 人工标注（**唯一允许写入归因结论的入口**） | V1 |
| 35 | POST | `/api/v1/badcases/{id}/promote` | 沉淀为评估集题目（只生成**候选**，不直接改 `golden_set.yaml`） | V1 |
| 36 | POST | `/api/v1/admin/nl2sql/preview` | ★ 预览生成的 SQL（**默认不执行**；把"生成"这个黑箱打开） | V2 |
| 37 | GET | `/api/v1/admin/semantic-layer` | 查看语义层版本与指标目录（本体是文件，走 Git） | V2 |
| 38 | GET | `/api/v1/admin/nl2sql-logs` | 查询 NL2SQL 日志（**含被校验拦下的 SQL**） | V2 |

**总数：38 个接口**（V-1: 2 / V0.1: 10 / V1: 23 / V2: 3）。
新增的评估接口共 **14 个**，全部属于 V1，设计说明见下方 5.6 与 `evals/attribution.md`。
新增的 SQL 通道接口共 **3 个**，全部属于 V2，设计说明见下方 5.7 与 `docs/text-to-sql.md`。

> **注意：SQL 通道的"业务入口"不是新接口。**
> 用户提问仍然打 `POST /api/v1/query`，只是响应里多了 `channel` 与 `sql` 两个字段
> （见 5.1）。**这是刻意的**：给同一条业务动作开两个外部入口，
> 就等于维护两套鉴权、两套限流、两套权限判定路径 —— 而其中一套迟早会被漏掉。
> 下面 3 个接口全部是**管理员的调试与查看**类，不是业务入口。

---

## 五、核心接口详解

### 5.1 `POST /api/v1/query` — 问答

**请求**

```json
{
  "question": "年假怎么算",
  "kb_ids": [3],
  "top_k": 5,
  "filters": { "doc_type": "policy" },
  "channel": "auto"
}
```

| 字段 | 必填 | 约束 | 说明 |
|------|------|------|------|
| question | 是 | 1—2000 字 | 超长返回 `QUESTION_INVALID`，**且不产生模型调用**（AC-11） |
| kb_ids | 否 | 不传则搜全部有权知识库 | |
| top_k | 否 | 默认 5，最大 20 | |
| filters | 否 | 元数据过滤 | |
| `channel` | 否 | 默认 `auto` | ★ V2。`auto` 走内部路由 / `doc` 强制文档 / `sql` 强制数据。传 `sql` 但数据源未启用 → 503，**不静默改走 doc** |

**响应 200**

```json
{
  "answer": "入职满 1 年可享 5 天年假，满 3 年 10 天，满 5 年 15 天。[1]",
  "refused": false,
  "degraded": false,
  "citations": [
    {
      "index": 1,
      "doc_id": 12,
      "doc_title": "员工手册.txt",
      "chunk_id": 8842,
      "char_start": 1024,
      "char_end": 1180,
      "snippet": "入职满 1 年可享受年假 5 天……",
      "score": 0.83
    }
  ],
  "latency_ms": 3620,
  "retrieve_ms": 180,
  "generate_ms": 3100,
  "cost_cny": 0.021,
  "model_used": "small-model-a",
  "trace_id": "lf-8f2c11"
}
```

| 字段 | 说明 |
|------|------|
| `refused` | **true 表示知识库无相关内容，已拒答**。此时 `answer` 必须恰好是固定句式 |
| `degraded` | true 表示处于降级模式（预算超限或模型故障），`answer` 可能是检索片段拼接而非模型生成 |
| `citations[].index` | 与答案里的 `[1][2]` 编号严格对应，前端据此渲染引用卡片 |
| `citations[].char_start/char_end` | 用于前端滚到原文对应位置高亮 |
| `latency_ms` 等分段耗时 | 出问题时能立刻看出是检索慢还是生成慢 |

**响应 200（V2 · 走 SQL 通道时）**

```json
{
  "answer": "上个月（2026-09，按结算日）已通过的培训费共 47 笔，合计 ¥151,270.00。",
  "refused": false,
  "degraded": false,
  "channel": "sql",
  "route_confidence": 0.91,
  "channel_attempts": ["sql"],
  "refused_reason": null,
  "citations": [],
  "sql": {
    "text": "SELECT SUM(amount) AS total, COUNT(*) AS cnt FROM expense_claims\n WHERE category = 'TRAIN' AND status = 'APPROVED'\n   AND settle_date >= DATE '2026-09-01' AND settle_date < DATE '2026-10-01'\n   AND dept_id = ANY($1)",
    "params": ["{101}"],
    "rows": [{ "total": 151270.0, "cnt": 47 }],
    "columns": ["total", "cnt"],
    "truncated": false,
    "semantic_layer_version": "2026-12-01.1",
    "scope_mode_used": "own",
    "matched_metrics": ["培训支出"],
    "exec_ms": 38,
    "generate_ms": 1420,
    "route_ms": 95
  },
  "latency_ms": 1780,
  "cost_cny": 0.041,
  "model_used": "small-model-a",
  "trace_id": "lf-9a71b3"
}
```

| 字段 | 说明 |
|------|------|
| `channel` | `doc`（默认）/ `sql` / `hybrid`。**V1 之前的历史记录一律视为 doc**（可空即兼容，R8） |
| `route_confidence` | 路由置信度。**<0.75 时已回落 doc**，该值仍会返回，便于观察路由质量 |
| `channel_attempts` | 尝试过的通道顺序。SQL 校验失败回落 doc 时形如 `["sql","doc"]` |
| `refused_reason` | 拒答的结构化原因：`doc_no_match` / `schema_no_match` / `scope_denied` / `unsafe_sql`。**拒答句式与文档通道完全相同**（R5），原因只在结构化字段里区分 |
| `sql.text` | ★ **必须返回**。用户要能看到数字是怎么算出来的（可审计 + 口径错时用户会比你先发现） |
| `sql.params` | 参数**绑定**值。SQL 里只出现占位符，禁止把值拼进字符串 |
| `sql.scope_mode_used` | `own` / `global`。★ `own` 时**答案文案必须标注范围**（"基于你有权访问的 N 条记录"）—— 这是用户唯一能察觉"这不是全公司数字"的线索（AC-30） |
| `sql.semantic_layer_version` | 当时生效的语义层版本。**没有它就无法解释"同一个问题为什么数字变了"** |

> **`channel=hybrid` 时的形状**：`citations` 与 `sql` **同时非空**，
> 答案分两块并列展示，并明确标注来源（"以下两部分分别来自制度文档与业务数据"）。
> **不做融合** —— 融合需要模型在两组异质信息间推理，而推理错误的形态又是静默的。

**响应 403（越权）**

```json
{ "error": { "code": "PERMISSION_DENIED", "message": "无权访问所选知识库" } }
```

> ⚠️ **关键约束**：403 响应体、日志、缓存中**都不得出现该知识库的任何文档片段**（AC-05）。
> 这是最容易实现错的地方——AI 常常"很贴心地"把检索到的内容一起返回。

### 5.2 `GET /api/v1/query/stream` — 流式问答（V1）

使用 **SSE**（Server-Sent Events），不用 WebSocket。

> **为什么优先 SSE 而不是 WebSocket**：企业网络里 WebSocket 经常被代理拦截，
> 而 SSE 就是普通 HTTP 长连接，穿透性更好，实现也更简单（前端 `EventSource` 即可）。
> 代价是不能双向通信——但问答本来也不需要。

事件序列：

```
event: meta      data: {"trace_id":"lf-8f2c11","retrieve_ms":180}
event: citation  data: {"index":1,"doc_id":12,"doc_title":"员工手册.txt",...}
event: delta     data: {"text":"入职满"}
event: delta     data: {"text":" 1 年可享"}
event: done      data: {"refused":false,"latency_ms":3620,"cost_cny":0.021}
```

**设计要点**
- `citation` 在**生成开始之前**就推送，用户能尽早看到"找到哪些资料"
- `delta` 只推文本增量，前端追加渲染
- `done` 必推，前端据此关闭连接、解除 loading

**降级路径**：若 `EventSource` 连接失败或 5 秒内无任何事件，
前端自动回退到 `POST /api/v1/query`（非流式），并在 UI 上不提示用户。

### 5.3 `POST /api/v1/documents` — 上传

**请求**：`multipart/form-data`，字段 `file`、`kb_id`、可选 `scope`、`title`

**响应 202**

```json
{ "task_id": 9001, "doc_id": 137, "status": "pending" }
```

**必须在 500ms 内返回**（AC-02）——解析和向量化一律异步。
同步处理会导致：用户上传一份 300 页文档时，全站问答被阻塞（`AGENTS.md` R10）。

### 5.4 `GET /api/v1/tasks/{id}` — 任务状态

```json
{
  "task_id": 9001,
  "doc_id": 137,
  "status": "embedding",
  "progress": 62,
  "retry_count": 0,
  "error_reason": null
}
```

失败时 `error_reason` **必须是给人看的话**，例如：

```
"PDF 第 14 页含加密内容，无法提取文本。建议：另存为 txt 后重新上传。"
```

而不是 `"error: PyPDF2.utils.PdfReadError"`。

> 这不是"体验问题"而是"可用性问题"：
> 3000 份文档必然有几十份解析失败，如果失败原因看不懂，
> 你就只能一份份手动打开排查，这件事会吃掉整个项目的时间。

### 5.5 `GET|POST|PATCH|DELETE /api/v1/admin/models` — 模型配置（V1）

> 这一组接口存在的唯一目的：**让"换模型"不需要改代码、不需要重新发布**（`spec.md` US-09）。
> 设计细节与"为什么"见 `docs/adr/0007`。

**`GET /api/v1/admin/models` 响应示例**

```json
[
  {
    "id": 1,
    "name": "embed-bge-m3-local",
    "model_type": "embedding",
    "provider": "tei",
    "base_url": "http://tei:8080",
    "model_name": "BAAI/bge-m3",
    "api_key_ref": null,
    "embedding_dim": 1024,
    "batch_limit": 64,
    "is_default": true,
    "enabled": true,
    "health_status": "ok",
    "last_checked_at": "2026-11-24T09:12:00Z"
  },
  {
    "id": 2,
    "name": "chat-deepseek-v3",
    "model_type": "chat",
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "model_name": "deepseek-chat",
    "api_key_ref": "KB_LLM_KEY_DEEPSEEK",
    "params": { "temperature": 0.1, "max_tokens": 1024, "timeout_s": 15 },
    "is_default": true,
    "fallback_of": null,
    "enabled": true,
    "health_status": "ok"
  }
]
```

**四条与普通 CRUD 不同的规则**（这几条是本组接口的全部价值所在）：

| # | 规则 | 背后的原因 |
|---|------|-----------|
| 1 | `POST` 与 `PATCH` **保存前必须自动跑一次连通性测试**，失败则返回 422 且**不落库**（`MODEL_TEST_FAILED`） | 存在数据库里的配置没有编译期检查。不强制测试，脏配置会一直躺在库里，直到某次真实调用才炸 |
| 2 | 响应里**永远没有 `api_key` 字段**，只有 `api_key_ref`（环境变量名或密钥服务路径） | `AGENTS.md` R1。任何一次数据库导出都不得等于密钥泄露 |
| 3 | `PATCH` 改 `embedding_dim` → 返回 409 `DIMENSION_CHANGE_REQUIRES_REBUILD` | 维度变更 = `chunks` 全量重建。必须走"新建配置 → 重建 → 切默认 → 停旧配置"，不允许原地改（`docs/data-model.md` 2.10） |
| 4 | `DELETE` 默认**软停用**（`enabled=false`）；`?hard=true` 且无引用时才物理删除，否则 409 `MODEL_IN_USE` | 配置被删掉后，历史 `query_logs.model_used` 就失去了可解释性——你会无法回答"三个月前那些答案是哪个模型生成的" |

**`POST /api/v1/admin/models` 的一个完整请求体**

```json
{
  "name": "chat-deepseek-v3",
  "model_type": "chat",
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "model_name": "deepseek-chat",
  "api_key_ref": "KB_LLM_KEY_DEEPSEEK",
  "params": { "temperature": 0.1, "max_tokens": 1024, "timeout_s": 15 },
  "is_default": true,
  "enabled": true
}
```

> 注意 `api_key_ref` 填的是**环境变量名**，不是密钥本体。
> 如果你不小心把 `sk-xxxxx` 直接填进 `api_key_ref`，服务端应当**拒绝**（防止密钥落库）。
> 这条校验要写进测试用例。

---

### 5.6 评估平台接口（V1）— 四条关键设计

**① 题目不进接口做 CRUD，只做元数据与预览。**

`POST /api/v1/eval/datasets` 上传的是**整份文件**；
`GET .../items` 是**只读**的列表与预览。
没有"新增一道题""改一道题"的写接口 —— 这不是没做完。

> 标准答案的修改是本项目里**最需要留痕、最需要人工 review** 的动作。
> 做成 API 调用，它就变成了"点一下就改了"，失去逐行 diff。
> 要改题 → 改 `evals/golden_set.yaml` → 走 Git 提交 → 重新注册数据集（版本号 +1、checksum 变化）。
> 这条链路慢，**慢是刻意保留的**。

**② 跑评估前校验 checksum，不一致直接拒绝。**

```
POST /api/v1/eval/runs { "dataset_id": 3 }
→ 409 { "code": "STALE_DATASET",
        "message": "数据集文件已变更（注册时 sha256=ab12…，当前=cd34…），请先重新注册数据集" }
```

> 存在的意义：防止"表里说跑的是 A 版、实际跑的是 B 版"。
> 这种不一致**不会报错**，只会让你拿到一个无法解释的数字，然后花半天找原因。
> 同理，`dataset.state != active` 或 `unconfirmed_count > 0` 也拒绝（`DATASET_NOT_ACTIVE`）。

**③ 归因结论只能由 `PATCH /api/v1/badcases/{id}` 写入，且必须带依据。**

```json
{
  "attribution": "retrieval",
  "attribution_basis": "对照跑 retrieval_hit=false；库里确有《财务报销制度》第 3 节，属召回失败",
  "severity": "P1",
  "status": "confirmed"
}
```

`attribution` 与 `attribution_basis` **必须成对提供**，否则 422 `ATTRIBUTION_BASIS_REQUIRED`。
判为误报（`status=wontfix`）时必须填 `dismissed_reason`，否则 422 `DISMISS_REASON_REQUIRED`。

> 为什么对字段这么较真：归因结论是**你下一步优化方向的唯一依据**。
> 一条"结论 == 检索问题、依据 == （空）"的记录，
> 三个月后你自己也无法判断当初为什么这么定。

**④ 导出只做 CSV / XLSX，不做服务端 PDF。**

`GET /api/v1/eval/runs/{id}/export?format=xlsx&scope=both`
- `scope=summary`：指标汇总（含 `metric_scope` 口径说明列）
- `scope=items`：逐条结果（含 `answer` 全文、`retrieval_hit`、归因两栏）
- `scope=both`：两个 sheet

> **PDF 用前端浏览器"打印成 PDF"**（零后端成本）。
> 服务端渲染中文 PDF 是三个坑的叠加：字体嵌入、表格换行、图表栅格化，
> 而且对单人项目几乎没有决策价值 —— 报告主要是给你自己看的。
> 详细理由见 `docs/adr/0008` 第六节第 5 条（含"什么条件下应该回头做服务端 PDF"）。

**一次完整的评估时序（便于对齐前后端行为）**

```
POST /eval/datasets                    → 201  { id:3, state:"draft", unconfirmed_count:40 }
#  你花 6—10 小时逐条确认标准答案，改文件 → 重新注册（version 1.1.0）
POST /eval/datasets                    → 201  { id:3, state:"active", unconfirmed_count:0 }
POST /eval/runs { dataset_id:3 }       → 202  { run_id:17, status:"pending" }
GET  /eval/runs/17                     → running → done（含 metrics / attribution_counts）
POST /eval/runs/17/baseline            → 200  { baseline_run_id:17 }
#  改一处参数（如加入 BM25）后重跑
POST /eval/runs { dataset_id:3, baseline_of:17 } → 202 { run_id:18 }
GET  /eval/runs/18                     → metrics_delta + comparable（★ 配置快照不同则为 false）
GET  /eval/runs/18/items?changed_only=true       → 只看哪几条从 pass 变 fail
#  按 attribution_candidate 分组，逐条人工确认
PATCH /badcases/{id}                   → 200
POST /badcases/{id}/promote            → 201  { candidate_item_path: "evals/pending/Q031.yaml" }
```

> 注意最后一行：promote **只生成候选题目**，落在 `evals/pending/`。
> 把它合入 `evals/golden_set.yaml` 必须由你人工确认标准答案后进行。
> 这是"标准答案必须人确认"这条铁律的**唯一入口保障** ——
> 如果 promote 能直接写进题库，这条铁律就等于没有。

**本节新增的错误码**

| code | HTTP | 含义 |
|------|------|------|
| `DATASET_INVALID` | 422 | 数据集文件格式错误 / 条目缺 id / 类别不在枚举内 |
| `DATASET_NOT_ACTIVE` | 409 | 数据集仍是 `draft`（含未确认题目），不能用于评估 |
| `STALE_DATASET` | 409 | 文件 checksum 与注册时不一致 |
| `RUN_NOT_DONE` | 409 | 对未成功完成的运行执行操作（如设基线） |
| `ATTRIBUTION_BASIS_REQUIRED` | 422 | 写了归因结论但没写判定依据 |
| `DISMISS_REASON_REQUIRED` | 422 | 判为误报但没写理由 |

### 5.7 SQL 数据查询通道（V2）— 四条必须写进契约的约定

完整设计见 `docs/text-to-sql.md`，决策与代价见 `docs/adr/0009`。
这里只写**接口层面必须冻结的四件事** —— 它们都是"不写清就会被实现错"的语义。

**① SQL 通道没有独立业务入口。**
用户提问仍然打 `POST /api/v1/query`，通过 `channel` 字段区分。
新增的 3 个接口全部是管理员调试/查看类。
**理由**：两个外部入口 = 两套鉴权与权限判定路径，其中一套迟早被漏掉。

**② `refused` 与 `channel=sql` 的组合语义**

| 情况 | `refused` | `channel` | `sql` | 说明 |
|------|-----------|-----------|-------|------|
| 正常走数据 | false | sql | 有 | |
| 该走文档的题走了数据但查不到数据 | **true** | sql | 有（`executed` 可能为 false） | 拒答原因 `schema_no_match` |
| SQL 校验被拦 | **true** | sql | **有**（含被拒原因） | 原因 `unsafe_sql`；**必须能让人看到"它本来想跑什么"** |
| 混合问题 | false | hybrid | 有 | `citations` 同时非空 |

> **为什么被校验拦下的 SQL 也要返回**：
> 它是排查"语义层该补什么"的第一手材料。藏起来会让"为什么它答不出来"
> 变成一个只能翻日志的问题 —— 而翻日志的成本足以让你不再愿意排查。

**③ `channel=sql` 但数据源不可用 → 503 `SQL_CHANNEL_UNAVAILABLE`**
**不静默改走 doc**。理由：用户明确要的是数据，悄悄给他一段制度原文，
他会以为是数据 —— **这比报错更糟**。（与降级场景不同：降级时前端会显示 `degraded=true`。）

**④ 缓存 key 必须含 `semantic_layer_version`**
除权限指纹（R3）之外再加这一项。否则改了口径之后，
用户还在拿按旧口径算出来的数字 —— 而这类错误**看起来和正常答案一模一样**。

```json
// POST /api/v1/admin/nl2sql/preview  （dry_run=true，默认）
{ "question": "上个月培训支出是多少", "dry_run": true }
// →
{
  "question": "上个月培训支出是多少",
  "channel": "sql",
  "route_confidence": 0.91,
  "candidate_metrics": ["培训支出"],
  "valid": true,
  "reject_reason": null,
  "reject_detail": null,
  "semantic_layer_version": "2026-12-01.1",
  "sql": {
    "text": "SELECT SUM(amount) ... WHERE category = 'TRAIN' AND status='APPROVED' AND settle_date >= ... AND dept_id = ANY($1)",
    "params": ["{101}"],
    "rows": [],
    "columns": [],
    "truncated": false,
    "scope_mode_used": "own",
    "matched_metrics": ["培训支出"]
  }
}

// 语义层里没有的指标 → valid=false，但仍是 200（不是 4xx）
{ "question": "平均工龄是多少", "dry_run": true }
// →
{
  "channel": "sql",
  "valid": false,
  "reject_reason": "unknown_object",
  "reject_detail": "引用了未声明的指标/字段：avg_tenure",
  "sql": { "text": "SELECT AVG(...) FROM employees ...", "rows": [], "columns": [] }
}
```

> **注意最后这个例子：校验失败返回 200，不是 422 也不是 500。**
> 它**不是错误**，而是"被闸门正确地拦下了"——
> 与 `refused`/`degraded`/`BUDGET_EXCEEDED` 返回 200 是同一类设计。
> **必须区分"看起来像错误但其实不是"和"真的失败了"**，
> 否则前端会把"语义层缺一个字段"渲染成"服务器故障"。

**本节新增的错误码**

| code | HTTP | 含义 |
|------|------|------|
| `SQL_CHANNEL_UNAVAILABLE` | 503 | 数据源未启用 / 连不上 / 只读账号鉴权失败。**不与 `MODEL_UNAVAILABLE` 合并**——排查动作完全不同 |
| `SQL_TIMEOUT` | 504 | 查询超过 `statement_timeout`。**不重试**（重试会放大数据库负载） |
| `SQL_REJECTED` | 422 | 仅用于**外部直接调用**执行接口时被闸门拦下。`/query` 内部走该通道时一律返回 200 + `refused=true` |

**为什么要有 `SQL_REJECTED` 这个码，而不是全部走 200 + refused**：
外部调用（管理接口、脚本）需要能区分"被拦"和"通过但没结果"；
而面向用户的 `/query` 必须是 200 + `refused`，因为它对用户而言就是一次正常问答。
**同一个内部事件，面对用户与面对程序，契约不同 —— 这是刻意的。**

---

## 六、接口冻结检查清单

写代码前，逐条确认：

- [ ] 每个接口的请求/响应字段都在 `openapi.yaml` 里有定义
- [ ] 所有必填字段已标注 `required`
- [ ] 所有枚举值已穷举（`status`、`reason`、`role` 等）
- [ ] 错误码清单已冻结，前端已按 code 做分支（而不是按 message 文本）
- [ ] `refused` / `degraded` 两种"非错误特殊状态"的语义已和前端对齐
- [ ] 分页字段统一
- [ ] 时间格式统一
- [ ] 我能否回答这个问题：**"如果后端改了字段名，谁会先出错？"**
      （答案必须是：CI，而不是用户）

**V2 追加（涉及 SQL 通道时）**

- [ ] `channel=sql` 时 `sql` 对象**一定**返回（含 SQL 原文与 `scope_mode_used`）
- [ ] `scope_mode=own` 的答案文案里**一定**含聚合范围标注
- [ ] `refused_reason` 枚举已冻结（`doc_no_match` / `schema_no_match` / `scope_denied` / `unsafe_sql`）
- [ ] 被校验拦下的 SQL **也返回给用户**（含被拒原因），不藏进日志
- [ ] `channel=sql` 而数据源不可用 → **503，不静默改走 doc**
- [ ] SQL 通道的缓存 key 含 `semantic_layer_version`
- [ ] 前端已按 `channel` 分支渲染（`doc` 显示引用卡片 / `sql` 显示可折叠 SQL / `hybrid` 显示两块并列）
