"""评估编排包（V0.1 · T28）。

职责边界（目录约定）：数据集校验、对照检索、候选归类、报告。
  - 不写 SQL（数据访问走 app/repos/eval_repo）；
  - 不写归因**结论**——attribution_confirmed 只能人工写入（R15）；
  - 题目本体是 evals/golden_set.yaml（文件），本包只读不写（R16）。

模块：loader（加载/校验/注册）→ runner（跑链/指标/报告/写库）。
"""
