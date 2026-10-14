# 文档索引（docs/00-index.md）

> 这是整套文档的地图。**先看这张表，再决定读哪个文件**，不要按文件夹顺序读。

---

## 一、阅读路线

### 如果你是第一次读（约 40 分钟）

```
README.md  →  docs/00-index.md（本文件）  →  docs/constraints.md  →  docs/spec.md
```

读完后你应该能回答：**这个系统给谁用、解决什么问题、什么算成功、什么明确不做。**

### 如果你要开始写代码（约 2 小时）

```
docs/releases.md  →  docs/architecture.md  →  docs/data-model.md
   →  docs/adr/0001~0009  →  docs/tasks.md
```

读完后你应该能回答：**V0.1 到底做哪几件事、技术栈是什么、权限怎么保证、从哪个任务开始。**

### 如果你要调优 RAG 效果

```
evals/README.md  →  evals/golden_set.yaml  →  docs/experiments/README.md
   →  docs/experiments/2026-11-20-分块大小512vs256.md（示例）
```

### 如果你要回答"这次改动到底是进步还是退步"

```
evals/attribution.md  →  docs/adr/0008（评估结果持久化与归因方式）
   →  docs/data-model.md 2.11—2.14（eval_datasets / eval_runs / eval_run_items / badcases）
```

读完后你应该能回答：**指标掉了改哪里、哪几条从通过变失败、线上失败怎么回灌成考题。**
> 注意：这里的"归因"**不是自动的**。系统只给候选，结论必须你确认 ——
> 这是刻意的设计，理由在 `docs/adr/0008`。

### 如果你要上线

```
docs/quality-gates.md  →  docs/security.md  →  docs/ops-runbook.md
```

### 如果你要做数据查询（V2 · Text-to-SQL）

```
docs/text-to-sql.md  →  config/semantic_layer.yaml  →  docs/adr/0009
   →  evals/sql/README.md  →  evals/sql/golden_set_sql.yaml
```

读完后你应该能回答：**什么样的题该走 SQL、什么样的题必须走文档、
"上个月培训支出"到底按哪个口径、权限不足时系统该怎么答。**

> ⚠️ **这一版有前置条件。** `docs/text-to-sql.md` 第零节列了 5 条，
> 不满足就别开工——它不是"排到就做"的功能（`docs/releases.md` V2-6）。
> 最关键的一条：**路由比生成重要**。SQL 写错是可见失败（报错/0 行），
> 路由错是静默失败（数字真实、答非所问），所以门禁里路由准确率的权重更高。

---

## 二、文件清单与依赖关系

```
constraints.md  ──┬──> spec.md  ──> releases.md ──> tasks.md
                  │        │
                  │        └──> evals/golden_set.yaml   （验收标准 → 考试题）
                  │        │            │
                  │        │            └──> evals/README.md（及格线）
                  │        │                 └──> evals/attribution.md（指标掉了改哪里）
                  │        │
                  │        └──> evals/sql/golden_set_sql.yaml  （V2：数据查询考题）
                  │                     └──> config/semantic_layer.yaml（V2：口径唯一来源）
                  │
                  ├──> glossary.md
                  │
                  └──> architecture.md ──┬──> data-model.md
                                         ├──> api-contract.md (+ openapi.yaml)
                                         ├──> security.md
                                         ├──> ops-runbook.md
                                         ├──> text-to-sql.md   （V2）
                                         └──> adr/0001~0009
```

**箭头方向 = "必须先有左边，才能写右边"。**
比如没有 `spec.md` 的验收标准，`tasks.md` 里的任务就无法定义"做完的标准"。

> **V2 多加了一条依赖**：`text-to-sql.md` 依赖 `semantic_layer.yaml`（口径来源），
> 而 `golden_set_sql.yaml` 又依赖前者——**语义层没定，题库里的标准答案就没法算**。
> 这条依赖是单向的、不可跳过的，所以任务顺序是 T71（语义层）→ T72（路由）→ …（`docs/tasks.md` 4.1）。

---

## 三、每个文件回答什么问题

| 文件 | 回答的问题 | 谁主导填写 |
|------|-----------|-----------|
| `constraints.md` | 我有什么约束？预算多少？数据能不能出内网？我懂什么？ | AI 问，你答 |
| `spec.md` | 系统要做什么？什么算做对了？什么明确不做？ | AI 问，你答 |
| `releases.md` | V0.1 / V1 / V2 分别做什么？每版上线的门禁是什么？ | AI 起草，你裁剪 |
| `glossary.md` | 这些词在我们这儿是什么意思？AI 容易误解成什么？ | AI 起草，你纠正 |
| `architecture.md` | 系统由哪几块组成？技术栈是什么？瓶颈在哪？ | AI 起草并解释，你确认 |
| `data-model.md` | 数据怎么存？权限怎么绑定到文档块上？评估结果存哪？ | AI 起草，你确认权限模型 |
| `api-contract.md` + `openapi.yaml` | 前端要什么、后端给什么？字段叫什么？ | AI 起草，你冻结 |
| `text-to-sql.md`（V2） | 什么样的题该走 SQL？口径从哪来？权限不足怎么答？ | AI 起草，**你定口径** |
| `config/semantic_layer.yaml`（V2） | 有哪些指标？每个怎么算？哪些字段不许查？ | **你主导**（业务口径） |
| `quality-gates.md` | 什么情况禁止合并？什么情况需要人工判断？ | AI 起草，你定阈值 |
| `security.md` | 审计日志留多久？PII 怎么处理？文档删除要怎么删干净？ | 你主导（合规判断） |
| `ops-runbook.md` | 出问题怎么办？告警阈值多少？怎么回滚？ | AI 起草，你演练确认 |
| `tasks.md` | 下一步做什么？做完怎么验？ | AI 拆，你检查可验收性 |
| `adr/` | 为什么选它？放弃了什么？什么时候该重新考虑？ | AI 起草，你确认"代价" |
| `experiments/` | 这次优化到底有没有用？数据在哪？ | AI 记录，你判断保留还是回滚 |
| `evals/` | 怎么衡量质量？多少分算及格？ | **你主导**，AI 只出候选 |
| `evals/attribution.md` | 指标掉了，改哪里？是检索的锅还是生成的锅？ | AI 起草，**结论永远由你确认** |
| `evals/sql/`（V2） | 数据查询这条路，路由判对了没？结果算对了没？ | AI 起草题库，**你核算标准答案** |

---

## 四、三条全局纪律

1. **每份文档定稿前，问 AI 三个问题**（不需要技术知识也能问）：
   - 请把你在这份文档里**替我做过的假设**全部列出来
   - 这份文档**还缺什么**？每个缺失项会导致后面什么困难？
   - 如果这个项目失败了，**最可能是这里的哪个决定**导致的？

2. **改了行为就要改文档。** 代码和文档不一致时，AI 会以文档为准去做错误的判断。
   它不是"文档滞后一点没关系"的那种项目。

3. **`> ⚙️ 待你确认` 标记的段落必须清空。**
   我在这套实例里用这个标记标出了所有需要你替换成真实情况的地方。
