---
description: 评估链路专项规则：评估集、评估运行、归因、BadCase。修改评估相关代码或 evals/ 下文件时自动生效。
globs: "evals/**, backend/app/eval/**, app/eval/**, backend/app/services/evaluator.py, app/services/evaluator.py, backend/app/api/v1/eval.py, app/api/v1/eval.py, backend/app/repos/eval_repo.py, app/repos/eval_repo.py, backend/scripts/eval*, scripts/eval*, backend/app/api/v1/badcases.py, app/api/v1/badcases.py"
alwaysApply: false
---

# 评估链路专项规则

> 本文件只在这几类文件被打开/引用时自动生效。
> 这些规则的共同点：**违反了不会报错，只会让你得出错误结论**——
> 而"得出错误结论"比"程序报错"危险得多，因为你会照着它去改一个没坏的地方。

## 一、评估集（题目本体）

- 题目本体是**文件**（`evals/*.yaml|jsonl|csv`），**不是数据库表**。
  数据库只存元数据与 checksum（`docs/data-model.md` 2.11）。
  **禁止**把题目 CRUD 做成"写数据库"，那会让标准答案的修改失去逐行 diff 与人工 review。
- 标准答案**必须经人确认**。AI 只能生成候选题目、按口述整理格式。
  `eval_datasets.unconfirmed_count > 0` 时**禁止**置为 `active`（数据库 CHECK 约束兜底）。
- 三件事绝对不可动，除非单独提交并写明理由：
  **删题、改标准答案、改 `should_refuse` 标记**。
- **禁止为了让评估通过而改题**。如果某条题一直过不了，
  先判断是"题目出错了"还是"系统真的不行"——后者要保留，它正在告诉你真实存在的问题。

## 二、评估运行（可复现性）

- 每次运行必须记录 `git_commit` 与 `config_snapshot`
  （提示词版本 / 分块参数 / 检索参数 / 重排开关 / 模型组合）。
  **换了其中任何一项却没记录，这次运行的数字就是不可解读的。**
- `config_snapshot` 变了，**禁止**把两次运行直接对比并宣称"进步了"。
  要么结论写明"同时改了 A 和 B，无法归因"，要么拆开重跑。
- 跑评估前校验 `eval_datasets.checksum` 与实际文件一致，不一致**拒绝开始**，
  而不是打个警告继续跑（H21）。
- 同一数据集下只允许一个基线（`uq_run_baseline`）。
- 逐条结果必须落 `eval_run_items`，**含 `answer` 全文**。
  只存汇总指标的评估等于没评估——无法回答"哪几条从通过变失败"。

## 三、归因（最容易写错的部分）

- **归因只到"候选"为止。** 机器产出 `attribution_candidate`，
  结论 `attribution_confirmed` **只能由人工写入**。
  **禁止**让模型或规则直接写入 `attribution_confirmed`。
- **允许 `unclassified` 大量存在。** 不要为了"好看"给每条都贴一个原因。
- 写入 `attribution_confirmed` 时**必须同时有依据**（`retrieval_hit` 的值
  或引用核对结果）。数据库 CHECK 约束会拒绝"只有结论没有依据"的行。
- 判定方法用**对照实验法**（`evals/attribution.md` 第二节）：
  同一题分别"只跑检索"和"跑完整链路"，依据是 `retrieval_hit` 这个**布尔值**。
  **禁止**用"LLM 自动判定失败类型"替代它。
- 统计只统计已确认的。报告里必须显式显示"未确认 N 条"。

## 四、口径（数字不可混用）

- **评估口径 ≠ 线上口径**，两者必须分开算、分开显示，禁止相加或平均：
  - 评估口径：离线单条，无缓存命中，`eval_runs.metrics.latency_eval` / `cost_eval`
  - 线上口径：含缓存命中后的真实平均，取自 `query_logs`
- `Recall@5`（检索层，标准来源是否出现在 Top-5）与 RAGAS 的
  `context_recall`（生成层，答案依据的上下文覆盖了标准答案的哪些事实点）
  **不是同一个指标，数字也不同**。同时展示时必须标明各自的口径与含义。
- `Precision@K` **不作为门禁指标**（RAG 场景下它天然很低但不影响体感，
  盯它会把你引向不影响用户体验的优化）。

## 五、BadCase 闭环

- `feedback`（用户 👎）是**候选**，`badcases` 是**结论**。禁止把两者合并成一张表。
- `badcases.attribution` 必须配 `attribution_basis`（判定依据），禁止只填结论。
- **promote 到评估集只能生成"候选题目"**（写入 MinIO 的 `evals/pending/`），
  **禁止直接追加进 `evals/golden_set.yaml`**。
  合入必须由人工确认标准答案后进行——这是"标准答案必须人确认"的唯一入口保障。
- `data_gap`（知识库里本来就没有）与系统答错必须分开归档：
  前者要补文档，后者要改代码。

## 六、报告与导出

- 导出优先 **CSV / XLSX**（简单、可靠、可二次分析）。
  PDF **不做服务端渲染**，用前端浏览器"打印成 PDF"替代。
- 报告里每个指标旁边必须能看出它的**版本上下文**：
  哪次运行、哪个数据集版本、哪个 `git_commit`、与哪个基线对比。
  **一个没有版本上下文的数字，比没有数字更危险。**
