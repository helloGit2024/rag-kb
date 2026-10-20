# 数据模型（data-model.md）

> **这是原需求完全缺失、但必须在写代码前定下来的一份文档。**
>
> 原需求写了"知识库级权限""文档级权限""细粒度行级权限"，
> 但没有回答最关键的一个问题：**权限具体绑定在哪张表的哪个字段上？**
>
> 这个问题不定，AI 会用最自然（也最危险）的方式实现：
> "先查出 Top-20，再在 Python 里过滤掉无权访问的"——
> 而这会同时造成**越权泄露**和**召回质量断崖**两个问题。
>
> `AGENTS.md` 的 R2 硬规则就是从这份文档推导出来的。

---

## 一、实体关系总览

```
users ──┬──< user_scope_grants >──┐
        │                          │
        │                    (scope_key)
        │                          │
knowledge_bases ──< documents ──< chunks
        │                │            │
        │                │      (scope_key 冗余存储)
        │                │            │
        │           parse_tasks       │
        │                             │
        │                             ▼
        │                    model_configs（V1）—— llm_gateway 按 model_type 读取
        │                    embedding 类的 embedding_dim 约束 chunks.embedding 的维度
        │
   (V1) audit_logs / feedback
                          │  👍👎 只是"候选"
                          ▼
                    badcases（V1）—— 人工确认后的"结论"，含归因与依据
                          │  promote（强制人工确认）
                          ▼
   eval_datasets ───< eval_runs ───< eval_run_items
   （题目本体在 MinIO+Git，    （一次评估，含配置快照）  （逐条结果，支撑
     表里只存元数据与校验和）                             「哪几条从通过变失败」）

   query_logs ──1:1──< nl2sql_logs（V2）
   （一问一行，          （生成 SQL / 校验结果 / 结果摘要 /
     V2 加 channel 字段）  命中指标 / 口径 / 权限快照）
```

**三条"不是表"的东西**（形态选择都有理由，不要"顺手"把它做成表）

| 对象 | 形态 | 为什么不是表 | 什么时候才该变成表 |
|------|------|------------|-----------------|
| RAG 策略参数 | `config/rag_strategy.yaml` | V1 策略固化，没有 CRUD 与版本管理需求 | V2 做"可视化策略配置平台"时 |
| **语义层（V2）** | `config/semantic_layer.yaml` | 口径改动需要**逐行 diff 与人工 review**；存进表会让"改一个口径"变成一次 API 调用，而口径改动恰恰最需要留痕 | V3，当业务方要自己改口径时 |
| 提示词模板 | `app/prompts/*.md` | 需要版本化与 diff（R4） | 永远不是表 |

> **语义层的版本怎么落进数据库**：表里只存 `nl2sql_logs.semantic_layer_version`
> （文件 checksum 前 12 位）。**版本号是引用，不是内容。**
> 这样"数字为什么变了"可以靠 `git diff` 精确回答 —— 这比把口径存进表里更有力。
>
> 与 `eval_datasets.checksum` 是同一种设计：**内容走 Git，数据库只记"当时用的是哪一版"。**

**两条贯穿全表的形态选择**（不是随心所欲，都有理由）

| 对象 | 存哪儿 | 为什么 |
|------|-------|-------|
| 文档原文 / 评估题目 / 提示词 | 文件（MinIO + Git） | 它们是**内容**：需要逐行 diff、需要人工逐字 review。存进表里会让"改一个字"变成一次 API 调用，而这类修改恰恰最需要留痕 |
| 元数据 / 评估结果 / 审计日志 | 数据库 | 它们是**机器产出或需要关联查询**：量大、要跨 run 对比、要按维度聚合 |

**一句话核心设计**：
**`chunks` 表上冗余存一个 `scope_key` 字段，权限过滤直接写在 chunk 检索的 SQL 里。**
这样"过滤"和"检索"是同一条 SQL 的两个条件，数据库层面就保证不会越权。

> **注意：RAG 策略配置不是数据库表。**
> 分块 / 检索 / 重排参数存于配置文件（`config/rag_strategy.yaml`），
> 对应 `docs/releases.md` 的"V1 策略固化在代码 + 配置文件，不做可视化界面"。
> 所以 ER 图里**不出现** `rag_strategies` —— 它曾经出现在这里，是残留，已移除。
> 什么时候它才会变成表？V2 做"可视化策略配置平台"时（那时才有 CRUD 与版本管理的需求）。

---

## 二、核心表设计

### 2.1 users

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| username | text UNIQUE | 登录名 |
| display_name | text | 显示名 |
| dept | text | 所属部门（用于部门级权限判定） |
| role | text | `admin` / `kb_admin` / `user` / `viewer`（V1 启用） |
| status | text | `active` / `disabled` |
| created_at | timestamptz | |

### 2.2 knowledge_bases

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| name | text UNIQUE | |
| description | text | |
| default_scope | text | **新文档默认可见范围**（对应 `spec.md` Q-01，默认 `internal`） |
| strategy_id | bigint FK | 绑定的 RAG 策略（V1） |
| created_by | bigint FK users | |
| created_at | timestamptz | |

### 2.3 documents

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| kb_id | bigint FK | |
| title | text | |
| source_type | text | `txt` / `markdown`（V2 扩展 pdf/docx） |
| storage_key | text | MinIO 对象 key（原文） |
| parsed_key | text | MinIO 对象 key（解析后文本） |
| file_md5 | text | **用于去重**，同 kb 内 md5 相同则拒绝重复入库（上传端 409；另由下方局部唯一索引兜底） |
| file_size | bigint | |
| status | text | `pending`/`parsing`/`chunking`/`embedding`/`done`/`failed` |
| error_reason | text | 失败原因（**必须是人话**，见 AC-02） |
| scope | text | 可见范围：`public` / `internal` / `dept:研发部` |
| uploaded_by | bigint FK users | |
| version | int | 同名文档的第几版 |
| is_active | boolean | 软删除标记（真正的清理由清理任务完成） |
| created_at / updated_at | timestamptz | |

**局部唯一索引（T22b：重复上传竞态兜底）**

```sql
CREATE UNIQUE INDEX uq_documents_kb_md5_active
  ON documents (kb_id, file_md5)
  WHERE is_active = true;
```

> **为什么需要它**：`file_md5` 的去重判断在**上传接口**（`ensure_not_duplicate` → 409）完成，
> 但 documents 行由 **worker 异步创建** —— worker 还没建行时连传两次，两次都判为"不重名"，
> 会入库两行。此索引把"同一 kb 内存在两行相同 md5 的活跃文档"变成**数据库层面的不可能**。
> **`WHERE is_active = true`**：软删行不参与约束，故删除某文档后重传同一文件不会被拦
> （软删行保留用于追溯，见第四节"删除与合规"）。
> worker 侧命中该约束时抛 `DuplicateIngestError`，把第二个任务标 `done` 并复用已有 `doc_id`（不重试、不标 failed）。

### 2.4 chunks（★ 最关键的表）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| doc_id | bigint FK documents | |
| kb_id | bigint | **冗余**，避免检索时 JOIN |
| chunk_index | int | 在文档内的顺序 |
| content | text | 块文本 |
| token_count | int | 用于上下文预算控制 |
| char_start / char_end | int | 在原文中的字符位置（引用定位用） |
| embedding | vector(1024) | bge-m3 向量，HNSW 索引 |
| **scope_key** | **text** | ★ **权限过滤字段**，值由 doc.scope 与 kb 可见性推导，形如 `dept:销售部` / `internal` / `public` |
| tsv | tsvector | 全文检索字段（V1），`GENERATED ALWAYS` |
| created_at | timestamptz | |

**索引**

```sql
-- 向量检索（HNSW，余弦距离）
CREATE INDEX idx_chunks_embedding ON chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);

-- 全文检索（V1）
CREATE INDEX idx_chunks_tsv ON chunks USING gin (tsv);

-- ★ 权限过滤 + 检索的核心索引（复合索引，让过滤和检索同一次扫描完成）
CREATE INDEX idx_chunks_scope_kb ON chunks (scope_key, kb_id);

-- 去重与清理
CREATE UNIQUE INDEX idx_chunks_doc_idx ON chunks (doc_id, chunk_index);
```

### 2.5 user_scope_grants

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| user_id | bigint FK users | |
| scope_key | text | 授予的可见范围，如 `dept:销售部`、`internal`、`kb:3` |
| granted_by | bigint | |
| granted_at | timestamptz | |

> **设计取舍说明**：这里用"授权表"而不是"用户表上放一个部门字段"，
> 是因为一个人可能同时属于多个范围（本部门 + 跨部门项目组 + 公开）。
> 用 `scope_key` 字符串做统一抽象，比堆多个 bool 字段更容易扩展。

### 2.6 parse_tasks

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | 对外的 task_id |
| doc_id | bigint FK | |
| task_type | text | `parse` / `embed` / `eval` |
| status | text | `pending`/`running`/`done`/`failed`/`canceled` |
| progress | int | 0—100 |
| retry_count | int | 最多 3 |
| error_reason | text | |
| payload | jsonb | 任务参数快照（**便于重放**） |
| created_at / started_at / finished_at | timestamptz | |

### 2.7 audit_logs（V1，合规用）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| actor | text | 谁 |
| action | text | 做了什么：`doc.upload` / `doc.delete` / `config.update` / `scope.grant` / `attack_attempt` |
| target | text | 对象：`doc:123` / `kb:3` |
| result | text | `success` / `denied` / `failed` |
| detail | jsonb | 附加信息（**不得包含文档正文**） |
| created_at | timestamptz | |

**不可修改的实现方式**（这是审计日志和操作日志的本质区别）：

```sql
-- 应用使用的数据库账号，只授予 INSERT 和 SELECT
GRANT INSERT, SELECT ON audit_logs TO kb_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM kb_app;

-- 自动阻止 UPDATE/DELETE 的触发器（双保险）
CREATE OR REPLACE FUNCTION deny_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_logs is append-only'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION deny_mutation();
```

> 这一段是 `spec.md` AC-18 的实现依据。
> **为什么要双保险**：只靠权限，某次部署用错账号就失效了；
> 只靠触发器，超级用户仍能绕过。两条都要有。

### 2.8 feedback（V1）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| query_log_id | bigint | 关联那次问答 |
| rating | text | `up` / `down` |
| reason | text | `hallucination` / `missing_recall` / `off_topic` / `wrong_number` / `permission_error` / `other` |
| comment | text | |
| added_to_golden | boolean | 是否已沉淀进评估集（**这是闭环的关键字段**） |
| created_at | timestamptz | |

### 2.9 query_logs（V0.1 就要有）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| user_id / kb_id | bigint | |
| question | text | |
| scope_snapshot | jsonb | ★ 当时生效的权限范围（**排查越权问题的唯一手段**） |
| retrieved_chunk_ids | bigint[] | 检索命中的块 |
| answer | text | |
| refused | boolean | |
| citations | jsonb | |
| latency_ms / retrieve_ms / generate_ms / rerank_ms | int | 分段耗时 |
| cost_cny | numeric(10,4) | |
| model_used | text | 路由到了哪个模型 |
| trace_id | text | 关联 Langfuse |
| created_at | timestamptz | |

> `scope_snapshot` 这个字段看起来不起眼，但它解决了排查越权问题时最痛的一件事：
> 用户权限是会变的，事后复现时你无法知道"当时他到底能看到什么"。
> **没有这个字段，越权问题基本无法定位。**

### 2.10 model_configs（V1，模型配置）

> **为什么这张表必须有**：`spec.md` 的 US-09 要求"换模型不用改代码"。
> 如果模型配置写死在 `.env` 或代码常量里，这条需求根本无法满足——
> 换一次模型要改代码、重新发布，而且没有人能回答"线上现在到底在跑哪个模型"。
>
> **它同时解决第二个问题**：`chunks.embedding` 是固定维度的向量列，
> 而"当前用哪个嵌入模型"决定了这个维度。两者必须有一处显式绑定，
> 否则换嵌入模型时会静默写进错误维度的向量。绑定点就是这张表的 `embedding_dim`。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| name | text | 人类可读名，如 `chat-deepseek-v3`、`embed-bge-m3-local`、`rerank-bge-v2-m3` |
| model_type | text | `chat` / `embedding` / `rerank`（**与 `model-test` 接口的枚举逐字一致**） |
| provider | text | 适配器名，见 `architecture.md` 的 provider 适配矩阵 |
| base_url | text | 完整 base，如 `https://api.deepseek.com/v1`；本地为 `http://tei:8080` |
| model_name | text | 厂商侧的模型 ID，如 `deepseek-chat`、`bge-m3`、`BAAI/bge-reranker-v2-m3` |
| api_key_ref | text | ★ **只存"去哪儿取"，不存明文**：环境变量名（如 `KB_LLM_KEY_DEEPSEEK`）或密钥服务路径 |
| params | jsonb | 非敏感调用参数：`temperature` / `max_tokens` / `top_p` / `timeout_s` 等 |
| extra_headers | jsonb | 少数厂商需要的自定义头（如 Azure 的 `api-version`） |
| embedding_dim | int | **仅 embedding 类型**：向量维度，必须与 `chunks.embedding` 的维度一致 |
| batch_limit | int | **仅 embedding / rerank 类型**：单次批量上限（各厂商差异极大） |
| max_input_tokens | int | 单条输入上限；超长必须在调用前切分，不能等报错 |
| is_default | boolean | 该类型下的默认模型（每个 `model_type` 最多一个为 true） |
| fallback_of | bigint | 备用关系：指向另一个 `model_configs.id`（主模型故障时切到它） |
| enabled | boolean | 停用后网关直接跳过，连尝试都不尝试 |
| health_status | text | `unknown` / `ok` / `degraded` / `down`（由 `model-test` 或定时健康检查更新） |
| last_checked_at | timestamptz | 上次连通性检查时间 |
| created_at / updated_at | timestamptz | |

**约束与索引**

```sql
ALTER TABLE model_configs ADD CONSTRAINT uq_model_name UNIQUE (model_type, name);

-- 每类模型最多一个默认
CREATE UNIQUE INDEX uq_model_default ON model_configs (model_type) WHERE is_default;

-- embedding 类必须有维度；其他类型不该有
ALTER TABLE model_configs ADD CONSTRAINT ck_embedding_dim CHECK (
  (model_type = 'embedding' AND embedding_dim IS NOT NULL) OR
  (model_type <> 'embedding')
);
```

**三条硬约束（写进代码审查清单）**

1. **`api_key_ref` 不得存明文**（`AGENTS.md` R1）。API Key 本体只放在环境变量或密钥服务里，
   这张表只记"去哪儿取"。因此 `POST /api/v1/admin/model-test` 收到的 `api_key`
   属于**一次性输入**，测试完即丢，绝不落库。
2. **删除一个仍在被引用的模型配置时返回 409**，不要级联删除——
   它可能正被 `kb.model_config_id` 或另一条记录的 `fallback_of` 指向。
3. **不允许把 `embedding` 类模型改成不同维度后直接启用**。维度变更 = `chunks` 全量重建，
   必须走"新建一条配置 → 重建 → 切换默认 → 停用旧配置"的流程（与 R8 同源：
   代码能回滚，数据不能）。

---

### 2.11 eval_datasets（V0.1 建表，V1 接界面）

> **为什么评估集需要一张表**：原设计里评估集就是 `evals/golden_set.yaml` 一个文件。
> 只有一份、30 条的时候没问题。但评估集一定会**长出多份**——
> V0.1 的 30 条起步集、V1 扩到 100 条、一份只含权限题的专项集、
> 一次想试新分块策略时的临时集。而每次评估都要能回答"这次用的是哪一份、哪一版"。
>
> **题目本体仍然不进表**，以文件形态存在于 MinIO（归档快照）与 Git（人改的那份）。
> 表里只存元数据与校验和 —— 这也是 2.13 里 `item_id` 不是外键的原因。
> 理由见本节开头"两条贯穿全表的形态选择"。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| name | text | 如 `golden-v1-30`、`golden-v1-100`、`perm-only` |
| version | text | 语义化版本，如 `1.0.0`。**改题必须升版本** |
| repo_path | text | Git 内路径，如 `evals/golden_set.yaml`（人改的是这个） |
| file_path | text | MinIO 对象路径（归档快照，跑评估时读它，保证可复现） |
| file_format | text | `yaml` / `jsonl` / `csv` |
| checksum | text | 文件 sha256。★ **用来判断"这次跑的题，和我看过的那份是不是同一份"** |
| item_count | int | 条目总数 |
| distribution | jsonb | 四类分布 `{"normal":18,"refuse":6,"permission":3,"edge":3}` |
| tag_stats | jsonb | 自由标签统计 `{"year-leave":3,"reimburse":4}` |
| state | text | `draft`（含未确认新题）/ `active`（可用）/ `archived` |
| unconfirmed_count | int | ★ **标准答案尚未经我确认的条目数。非 0 时禁止设为 `active`** |
| source | text | `manual` / `online_badcase` / `eval_failure` |
| created_at / updated_at | timestamptz | |

**约束**

```sql
ALTER TABLE eval_datasets ADD CONSTRAINT uq_dataset_name_ver UNIQUE (name, version);

-- 只要还有未确认条目，就不允许启用
ALTER TABLE eval_datasets ADD CONSTRAINT ck_active_requires_confirmed CHECK (
  state <> 'active' OR unconfirmed_count = 0
);
```

> `unconfirmed_count > 0` 时不允许 `active` —— 这不是形式主义。
> 它把"标准答案必须人工确认"这条铁律**从口头约定变成数据库约束**。
> AI 帮你生成 40 条候选题目，只要你不逐条确认，这份数据集就永远进不了 `active`，
> 也就永远不会被拿去跑门禁。**是约束在替你守住纪律，而不是你的自制力。**

---

### 2.12 eval_runs（V0.1 建表）

> **为什么这张表是"评估体系的地基"**：没有它，评估结果只能躺在
> `evals/report/*.json` 一堆散文件里，而"这次是跟哪个基线比"这个问题的答案
> **只存在于你的记忆里**。跑到第 4、5 次评估时你必然会遇到：
> *"上周那个 0.891 是哪个版本跑出来的？"*
>
> **它还解决一个更隐蔽的问题**：两次评估之间你往往改了好几样东西
> （提示词、分块、模型）。只有把配置快照存下来，你才能回答
> "到底哪个变化带来了这 3 个点" —— 或者诚实地写下"同时改了 A 和 B，无法归因"。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| dataset_id | bigint FK | 用哪份数据集 |
| git_commit | text | ★ 跑评估时的代码版本。**没有它，两次评估的数字不可比** |
| config_snapshot | jsonb | ★ 提示词版本 / 分块参数 / 检索参数 / 重排开关 / 模型组合 |
| model_config_ids | bigint[] | 本次用到的模型配置（chat / embedding / rerank） |
| status | text | `pending` / `running` / `done` / `failed` / `cancelled` |
| metrics | jsonb | 汇总指标（结构见下） |
| attribution_counts | jsonb | 归因**候选**分布 `{"retrieval_suspect":9,"generation_suspect":5,"unclassified":16}` |
| cost_cny | numeric(10,4) | ★ 本次评估**自身的**花费（评估也要花钱，这条常被忘记） |
| total_tokens_in / total_tokens_out | bigint | |
| wall_time_s | int | 墙钟耗时 |
| report_path | text | 报告文件（json + csv/xlsx）在 MinIO 的位置 |
| is_baseline | boolean | 是否被标为对比基线。**同一数据集下只允许一个** |
| baseline_of | bigint | 本次是在与哪个 run 对比（可空） |
| triggered_by | text | `manual` / `ci` |
| error_reason | text | 失败原因（写给人看的话，不是堆栈） |
| started_at / finished_at | timestamptz | |

`metrics` 结构（**所有指标都在这一处，并显式标注口径**）：

```json
{
  "retrieval":  { "recall@5": 0.891, "mrr": 0.735, "context_precision": 0.742 },
  "generation": { "faithfulness": 0.902, "answer_relevancy": 0.864, "citation_accuracy": 0.928 },
  "e2e":        { "task_completion": 0.933, "refusal_correct": 0.933, "false_refusal": 0.031 },
  "latency_eval": { "p50_ms": 2140, "p95_ms": 3620, "retrieve_ms": 410, "generate_ms": 2680 },
  "cost_eval":    { "per_query_cny": 0.021, "avg_tokens_in": 3480, "avg_tokens_out": 296 },
  "metric_scope": "latency_eval / cost_eval 为离线单条口径，与线上口径不同，见 evals/README.md 第八节"
}
```

**约束**

```sql
-- 同一数据集下只允许一个基线
CREATE UNIQUE INDEX uq_run_baseline ON eval_runs (dataset_id) WHERE is_baseline;
```

> **一条必须写下来的纪律**：`config_snapshot` 里任何一项变了，
> 就**不允许**把两次运行直接对比并宣称"进步了"。
> 要么结论里写明"同时改了 A 和 B，无法归因"，要么回去拆开重跑。
> 这与 `docs/tasks.md` 里"T42 重排 → T43 查询改写 顺序不能颠倒"是同一条纪律。

---

### 2.13 eval_run_items（V0.1 建表）

> **没有逐条结果，就无法回答"哪几条从通过变失败"。**
> 而这恰恰是回归测试的全部价值 —— 汇总指标从 0.891 掉到 0.862，
> 可能是 3 条题崩了，也可能是 30 条均匀掉一点，**两者的修法完全不同**。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| run_id | bigint FK | |
| item_id | text | 题目在数据集文件里的 id（如 `Q001`）。**不是外键** —— 题目本体在文件里 |
| category | text | `normal` / `refuse` / `permission` / `edge`（冗余存储，便于按类聚合而不回读文件） |
| question | text | 冗余存一份，让报告可以独立阅读 |
| status | text | `pass` / `fail` / `error` |
| scores | jsonb | 逐维度得分 `{"faithfulness":0.0,"answer_relevancy":0.5,"citation_accuracy":0.0}` |
| verdict | text | 裁判判定：`correct` / `hallucination` / `off_topic` / `wrong_number` / `over_refusal` / `under_refusal` |
| retrieval_hit | boolean | ★ **只跑检索时，标准来源是否出现在 Top-5**。归因的关键字段 |
| retrieved_chunk_ids | bigint[] | |
| answer | text | ★ 系统实际输出。**必须存，否则无法复盘** |
| refused / degraded | boolean | |
| latency_ms / retrieve_ms / generate_ms | int | |
| cost_cny | numeric(10,4) | |
| attribution_candidate | text | ★ 归因**候选**（机器判定）：`retrieval_suspect` / `generation_suspect` / `unclassified` |
| attribution_confirmed | text | ★ 归因**结论**（人工确认，可空）。`retrieval` / `generation` / `permission` / `data_gap` / `undetermined`。**只有这一栏非空才计入统计** |
| confirmed_by / confirmed_at | text / timestamptz | |
| notes | text | |

**索引与约束**

```sql
CREATE UNIQUE INDEX uq_run_item ON eval_run_items (run_id, item_id);

-- 归因结论必须与依据并存：不能只有结论、没有依据
ALTER TABLE eval_run_items ADD CONSTRAINT ck_attribution CHECK (
  attribution_confirmed IS NULL
  OR (attribution_candidate IS NOT NULL AND confirmed_by IS NOT NULL)
);
```

> **`retrieval_hit` 这个字段值得单独说**：它把"这条题失败到底是检索的锅
> 还是生成的锅"变成一个**布尔值**，而不是一个需要模型打分的概率值。
> 判定方式是"对照跑一次只检索的链路"—— 准确率接近 100%，单条成本几厘钱。
> 完整方法见 `evals/attribution.md`。

---

### 2.14 badcases（V1）

> **它和 `feedback` 的区别必须分清**：
> `feedback` 是"用户点了 👎" —— **候选**，含误报；
> `badcases` 是"确认过这确实是个问题" —— **结论**，含归因与判定依据。
>
> 不做这个区分，你的"问题统计"里会混进一半情绪化反馈，
> 然后你会开始怀疑这份数据，最后不再看它。
> **这就是"候选"和"结论"必须分成两个实体的全部理由。**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| source | text | `feedback` / `eval_run` / `manual` |
| source_ref | text | 来源 id（`feedback.id` 或 `eval_run_items.id`） |
| kb_id | bigint | |
| question / answer | text | 快照（源记录可能被清理，这里必须自包含） |
| evidence | jsonb | 检索结果 + 引用 + 权限快照 —— 复盘时的全部依据 |
| category | text | `hallucination` / `missing_recall` / `off_topic` / `wrong_number` / `over_refusal` / `permission_error` / `other` |
| attribution | text | `retrieval` / `generation` / `permission` / `data_gap` / `undetermined` |
| attribution_basis | text | ★ **判定依据**（对照跑的 `retrieval_hit` 值、引用核对结果）。不允许只填结论不留依据 |
| severity | text | `P0` / `P1` / `P2` |
| status | text | `new` / `confirmed` / `fixing` / `fixed` / `wontfix` / `data_gap_wontfix` |
| dismissed_reason | text | 判定为误报时记录理由（避免同类反馈反复出现、反复讨论） |
| promoted_dataset_id | bigint | 已沉淀进哪份数据集（可空） |
| candidate_item_path | text | ★ promote 生成的**候选题目**文件路径（在 MinIO）。**需人工确认后才合入 `golden_set.yaml`** |
| created_at / updated_at | timestamptz | |

**`feedback` 表在 V1 需要补两个字段**（迁移）：`badcase_id bigint`（确认后回填关联）、
`dismissed_reason text`（判定为误报时记录理由）。
原有 `added_to_golden` 保留，闭环完成时置 `true`。

> **`status` 里为什么要单列一个 `data_gap_wontfix`**：
> "知识库里没有这个信息"和"系统答错了"是两件完全不同的事，
> 前者要补文档、后者要改代码。混在一起会让你的待办清单永远理不清
> （见 `docs/glossary.md` 对"缺口"的定义）。

### 2.15 nl2sql_logs（V2，Text-to-SQL 通道的排查与审计依据）

> **为什么这张表是必须的，而不是"顺手记个日志"**：
> SQL 通道的失败是**静默**的 —— 答案看起来完全正常，
> 你唯一能事后判断"它当时到底干了什么"的地方，就是这里。
> 没有它，出了错你只能猜：是路由错了、语义层缺字段、还是口径写错了？
> 这三种错要改三个完全不同的地方。
>
> 与 `query_logs.scope_snapshot` 同理：**没有任何一条错误数字能靠"复现"来排查**，
> 因为数据已经变了、口径版本已经变了。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | bigserial PK | |
| query_log_id | bigint FK | 关联到 `query_logs`（一问一行） |
| question | text | 原始问题快照 |
| channel | text | 实际走的通道：`doc` / `sql` / `hybrid` |
| route_confidence | numeric(4,3) | 路由给出的置信度（低于阈值会回落 doc） |
| channel_attempts | text[] | 尝试过的通道顺序（SQL 失败回落 doc 时会有两个值） |
| semantic_layer_version | text | ★ 当时生效的语义层版本。**没有它，无法解释"同一个问题为什么数字变了"** |
| candidate_metrics | jsonb | 语义层召回命中的指标名与相似度（可解释性：它以为你要问什么） |
| generated_sql | text | ★ 生成的 SQL **原文**（审计与复盘的第一手材料） |
| normalized_sql | text | 归一化后的 SQL（去空白/大小写/别名），用于"同类查询"聚类 |
| sql_params | jsonb | 参数绑定值（**不拼进 SQL 字符串**） |
| valid | boolean | 是否通过五道闸门的校验 |
| reject_reason | text | 未通过时的原因：`unknown_object` / `denied_column` / `not_select` / `multi_statement` / `comment_detected` / `system_table` / `syntax_error` / `over_row_limit` |
| executed | boolean | 是否真的执行了（`valid=false` 时必须为 `false`） |
| row_count | int | 返回行数 |
| truncated | boolean | 是否被 `max_rows` 截断 |
| result_digest | text | 结果集摘要 hash（判断"同一问题是否得到同一结果"，以及缓存命中） |
| scope_snapshot | jsonb | ★ 与 `query_logs.scope_snapshot` **存同一个值**（权限同源，`docs/text-to-sql.md` 第七节） |
| scope_mode_used | text | 本次指标用的口径：`own` / `global`。★ 决定答案里有没有范围标注 |
| exec_ms / generate_ms / route_ms | int | 分段耗时（执行的瓶颈通常在生成，不在执行） |
| llm_calls | int | 模型调用次数（通常 2 次：生成 SQL + 复述结果） |
| cost_cny | numeric(10,4) | 单独记账，**不与文档通道混算** |
| trace_id | text | 关联 Langfuse |
| created_at | timestamptz | |

**三条必须写进数据库约束的规则**

```sql
-- ① key 在 SQL 里：未通过校验的行不允许标记为"已执行"
ALTER TABLE nl2sql_logs ADD CONSTRAINT ck_nl2sql_exec_valid
  CHECK (NOT executed OR valid);

-- ② 被拒的行必须有原因（否则排查时不知道是"哪道闸门"拦的）
ALTER TABLE nl2sql_logs ADD CONSTRAINT ck_nl2sql_reject_reason
  CHECK (valid OR reject_reason IS NOT NULL);

-- ③ 跨范围聚合的答案必须留下口径痕迹
ALTER TABLE nl2sql_logs ADD CONSTRAINT ck_nl2sql_scope_mode
  CHECK (channel <> 'sql' OR scope_mode_used IN ('own','global'));
```

> **`ck_nl2sql_exec_valid` 是最有价值的一条**：它把"未通过校验却执行了"
> 这个**唯一不可接受的组合**变成了数据库层面的不可能。
> 这类约束的价值在于：**它是唯一不依赖"我以后还记得"的保障。**

**`query_logs` 需要补两个字段**（迁移，V2）：

| 字段 | 类型 | 说明 |
|------|------|------|
| channel | text | `doc`（默认，历史行为不变）/ `sql` / `hybrid`。**加字段而非新建表**，理由是绝大多数问答仍是文档通道，分开建表会让"按用户看全部提问"变成 UNION |
| route_confidence | numeric(4,3) | 路由置信度（`channel=doc` 且未经过路由时为空） |

> **这次是"加字段"而不是"改字段"，是 R8 的正面示例**：
> `channel` 可空 + 默认 `doc`，代码同时兼容"读得到"与"读不到"两种情况，
> 所以即使新代码回滚，旧代码也不受影响。**代码能回滚，数据不能。**

---

## 三、权限过滤的正确写法（对照示例）

### ❌ 错误写法：应用层过滤（AI 最容易写出来的版本）

```python
# 危险！不要这样写
hits = await vector_search(q, limit=20)          # 先查出 20 条（可能含越权内容）
visible = [h for h in hits if can_access(user, h)]  # 再筛掉无权的
return visible[:5]                                # 可能只剩 2 条 → 召回崩了
```

**两个后果**：
① 越权内容已进入应用内存、日志、缓存 —— 泄露风险；
② 过滤后候选太少 —— 用户会觉得"库里明明有就是搜不出来"。

### ✅ 正确写法：过滤下沉到 SQL

```sql
-- 检索与权限过滤在同一条 SQL 里完成，数据库保证不会捞出无权内容
SELECT c.id, c.content, c.doc_id, c.char_start, c.char_end,
       1 - (c.embedding <=> :query_vec) AS score
FROM chunks c
WHERE c.scope_key = ANY(:scope_keys)          -- ★ 权限条件
  AND c.kb_id = ANY(:accessible_kb_ids)       -- ★ 知识库条件
ORDER BY c.embedding <=> :query_vec           -- 向量距离排序
LIMIT :top_k;
```

对应 Python 层（`app/repos/chunk_repo.py`，唯一允许写 SQL 的地方）：

```python
async def search_chunks(
    session, query_vec: list[float], scope_keys: list[str],
    kb_ids: list[int], top_k: int,
) -> list[ChunkHit]:
    """★ 权限过滤必须在 SQL 里，禁止在应用层过滤（AGENTS.md R2）。"""
    sql = text("""SELECT ... WHERE c.scope_key = ANY(:scope_keys)
                  AND c.kb_id = ANY(:kb_ids) ORDER BY ... LIMIT :k""")
    rows = await session.execute(sql, {
        "scope_keys": scope_keys, "kb_ids": kb_ids,
        "query_vec": query_vec, "k": top_k,
    })
    return [ChunkHit(**r._mapping) for r in rows]
```

### 缓存也必须带权限指纹

```python
def cache_key(question: str, scope_keys: list[str]) -> str:
    """★ key 必须含权限指纹，否则 A 部门会命中 B 部门的答案（AGENTS.md R3）。"""
    scope_fp = hashlib.sha256("|".join(sorted(scope_keys)).encode()).hexdigest()[:12]
    q_fp = hashlib.sha256(normalize(question).encode()).hexdigest()[:16]
    return f"qa:{scope_fp}:{q_fp}"
```

---

## 四、删除与合规（V1，对应 AC-19）

一次"删除文档"必须触发下列**全部**动作，缺一不可：

| 步骤 | 操作 | 验证方式 |
|------|------|---------|
| 1 | `documents.is_active = false`（软删，立即可见性消失） | 列表页不再出现 |
| 2 | 删除 `chunks` 中该 doc 的所有行（**含向量**） | `SELECT count(*) FROM chunks WHERE doc_id=?` = 0 |
| 3 | 删除 MinIO 中的原文与解析文本对象 | 对象存储查不到 |
| 4 | 失效相关缓存（按 `scope_key` 前缀批量清理或用版本号失效） | 再次提问不得出现旧内容 |
| 5 | 标记 `feedback` / 评估集中引用该文档的题为 `stale` | `evals/` 中相关题目标记待重标 |
| 6 | 写审计日志 `doc.delete` | 审计表新增记录 |
| 7 | （保留）`documents` 行与审计日志保留，用于追溯"什么时候谁删了什么" | 合规需要 |

> **为什么第 2 步必须真删向量而不是只标记**：
> 只标记的话，向量仍在库里，一旦有代码路径漏了 `is_active` 条件就会重新检索出来。
> 这是最典型的"以为删干净了其实没有"的坑。

---

## 五、备份与恢复（V1）

| 对象 | 方式 | 频率 | 恢复演练 |
|------|------|------|---------|
| PostgreSQL | `pg_dump` 全量 + WAL 归档 | 每日全量 | 每月一次恢复到临时库，**必须真的做** |
| MinIO | `mc mirror` 到备用目录 | 每日 | 每季度 |
| Redis | 不做备份 | — | 缓存丢了只是变慢，可接受 |
| 配置与提示词 | Git | 每次改动 | — |

**RPO / RTO 目标**：RPO ≤ 24 小时（最多丢一天数据），RTO ≤ 2 小时（2 小时内恢复服务）。

> **注意**：备份没有做过恢复演练，就等于没有备份。
> 这是运维里最常见的自欺。恢复演练要写进 `docs/ops-runbook.md` 的月度检查项。
