# REPO_CONVENTION.md — 仓库约定

> 这份文件解决一个具体问题：**AI 每次提交的代码风格都不一样。**
> 它写变量用 camelCase 还是 snake_case？提交信息写中文还是英文？
> 分支怎么命名？这些看起来是小事，但混乱累积三周后，你会看不懂自己的仓库。
>
> 约定一旦写下，AI 就会遵守；不写，它每次都会"重新选择"一遍。

---

## 一、命名约定

| 对象 | 约定 | 示例 |
|------|------|------|
| Python 变量/函数 | snake_case | `chunk_size`, `resolve_scope_keys()` |
| Python 类 | PascalCase | `ChunkStrategy`, `LlmGateway` |
| Python 常量 | UPPER_SNAKE | `MAX_QUERY_LENGTH = 2000` |
| TypeScript 变量/函数 | camelCase | `topK`, `fetchAnswer()` |
| TypeScript 组件 | PascalCase，文件名同组件名 | `CitationCard.tsx` |
| 数据库表 | snake_case，复数 | `documents`, `chunks` |
| 数据库字段 | snake_case | `created_at`, `scope_snapshot` |
| 环境变量 | UPPER_SNAKE，前缀 `KB_` | `KB_DATABASE_URL` |
| API 路径 | 小写 + 连字符，版本前缀 | `/api/v1/knowledge-bases` |
| JSON 字段 | snake_case（与 Python 一致） | `{"doc_id": "...", "refused": false}` |

**为什么 JSON 用 snake_case 而不是前端习惯的 camelCase**：
前后端字段名不一致是"后端改字段忘了通知前端"这类事故的根源。
统一成 snake_case，Python 侧可以零转换直接序列化，
前端在 `lib/api-client.ts` 里做一次性类型映射即可。

---

## 二、分支策略（单人开发的最简正确做法）

```
main          ← 永远可运行、可部署。不直接往上提交。
 └─ t05-混合检索   ← 一个任务一个分支，做完合回 main
```

- 分支名格式：`t<任务号>-<简短英文描述>`，如 `t05-hybrid-search`
- 从 `main` 切出，做完直接快进合并回 `main`（单人不需要 PR 流程，
  但**合并前必须 `make test && make eval` 全绿**）
- 分支存活不超过 3 天；超过说明任务拆得太大

**为什么不直接在 main 上改**：因为 AI 经常一改就改坏。
有分支，你就有一个"随时能扔掉的实验区"。
单人项目里，这条纪律的价值不是协作，是**止损**。

---

## 三、提交信息约定（Conventional Commits）

```
<类型>(<范围>): <一句话说明>
```

| 类型 | 什么时候用 |
|------|-----------|
| `feat` | 新增功能（必须对应 `docs/tasks.md` 里的任务） |
| `fix` | 修 bug |
| `test` | 只加/改测试 |
| `refactor` | 重构，行为不变 |
| `prompt` | **只改提示词**（单独类型，便于回溯"哪版提示词最好"） |
| `eval` | 改评估集或评估脚本（必须说明理由） |
| `semantic` | **只改语义层**（`config/semantic_layer.yaml`，即指标口径/白名单） |
| `docs` | 只改文档 |
| `chore` | 配置、依赖、脚手架 |
| `perf` | 性能优化 |

**示例：**

```
feat(retrieval): T05 实现 BM25 + 向量混合检索与 RRF 融合
test(retrieval): 补混合检索的固定语料单测，Recall@5 达 0.86
prompt(answer): v3 强制引用编号 + 固定拒答句式，忠实度 0.861→0.931
eval(golden): 新增 6 条线上失败案例（含 2 条间接注入），说明见提交正文
fix(security): R2 权限过滤下沉 SQL，修复缓存 key 未含权限范围的越权
feat(nl2sql): T73 SQL 五道闸门，注入用例 11—16 全部拦下（V2）
semantic(metrics): 培训支出口径改为按 settle_date，EX 0.71→0.93（V2）
```

**关于 `semantic` 这个类型，有一条专属纪律：**

**语义层改动的提交，正文里必须写"这个改动的口径依据是什么"**——
比如"财务口径确认：培训支出按 `settle_date` 结算日统计，不含 VOID 状态"。
原因：`semantic_layer.yaml` 改一行，所有相关题的数字都会变，
半年后你看 `git log` 时，如果只看到 `semantic(metrics): fix 培训支出`，
你无法判断当时是**改对了**还是**改错了**。

**规矩：**
- 一次提交只做一件事。改了检索又顺手改了前端样式 → 拆成两次提交。
- 提交正文里，涉及指标变化的必须贴数字（"忠实度 0.861 → 0.931"）。
- **数据库迁移文件单独提交**，提交信息里注明"是否可回滚"。

---

## 四、目录结构（与 AGENTS.md 第三节一致）

```
rag-kb/
├── AGENTS.md
├── REPO_CONVENTION.md
├── TRAE-启动手册.md
├── Makefile
├── docker-compose.dev.yml · Dockerfile
├── requirements.txt · ruff.toml · alembic.ini
├── .env.example                （`.env` 只在本机，进 `.gitignore`）
├── .github/workflows/ci.yml
├── config/                 ★ 语义层（SQL 通道的唯一对象来源，V2；走 Git 不进数据库）
│   └── semantic_layer.yaml
├── app/                    ← **实测结构：后端在仓库根，不套一层 `backend/`**（T01 定下的，别搬）
│   ├── api/v1/             路由
│   ├── services/           业务编排（chunker / generator / qa）
│   │   └── nl2sql/         ★ SQL 通道：路由、生成、五道闸门（V2）
│   │                       （是"唯一允许拼接动态 SQL"的例外，但只在语义层声明过的对象内）
│   ├── repos/              数据访问（唯一写 SQL 的地方）
│   ├── schemas/            Pydantic 模型
│   ├── prompts/            提示词模板（`.md`，版本化，如 `answer_v1.md`）
│   │   └── nl2sql/         ★ SQL 生成的提示词与 few-shot（V2）
│   ├── workers/            离线任务（V0.1 起）
│   ├── eval/               评估编排（数据集校验、逐条执行、对照检索、候选归类、报告）
│   └── core/               配置/日志/鉴权/LLM 网关
│       └── providers/      各厂商模型适配器（chat/embed/rerank 三层薄抽象）
├── alembic/versions/       数据库迁移
├── tests/                  单元 + 集成测试
├── scripts/                `ingest.py` / `ask.py` 等离线脚本
├── frontend/               Next.js 15（V0.1 起）
├── docs/
├── evals/
│   ├── golden_set.yaml     题目本体（唯一事实来源，走 Git）
│   ├── README.md           指标与及格线
│   ├── attribution.md      归因手册
│   ├── judge-prompt.md     裁判提示词
│   ├── sql/                ★ SQL 通道题库（V2）
│   │   ├── golden_set_sql.yaml   含 meta.frozen_now（冻结"今天"）
│   │   ├── seed.sql              固定种子数据集（临时库，跑完销毁）
│   │   ├── expected/             人工核算的标准结果集（S001.json 等）
│   │   └── README.md
│   ├── pending/            ★ promote 生成的**候选题目**（待人工确认，确认后才并入 golden_set.yaml）
│   ├── report/             每次评估的报告文件（与数据库 eval_runs 互为备份）
│   └── failures/           失败案例归档
└── deploy/
```

> **`config/` 为什么单独放在仓库根目录，而不是塞进 `backend/`**：
> 它是**业务口径**，不是代码——写它的人是懂业务的人，不是跑代码的人。
> 放根目录，谁都容易找到、容易改、容易单独 review；
> 塞进 `backend/app/`，它会变成"程序员才敢碰的文件"，
> 而口径恰恰最不该由程序员单方面决定。

> **`evals/pending/` 是目录，不是数据库表**，因为它的内容需要人工逐条 review 后才合入
> `golden_set.yaml`。**注意 `pending/` 下的文件不是题库的一部分** ——
> 它们不参与任何评估，只是"待办草稿"。理由见 `docs/adr/0008` 与 `spec.md` AC-25。

**迁移文件命名**：`alembic/versions/20261120_1430_add_scope_snapshot_to_query_logs.py`
（日期时间前缀，保证顺序可读 —— 注：现有 `0001_initial` / `0002_users_and_scope_grants`
用的是四位数字前缀，见 `⚙️ 待你确认`）

---

## 五、代码风格（由工具强制，不靠人记）

| 工具 | 作用 | 违反时 |
|------|------|--------|
| `ruff format` | Python 格式化 | 提交前自动跑 |
| `ruff check` | Python 静态检查 | CI 拦截 |
| `mypy --strict` | Python 类型检查 | CI 拦截 |
| `eslint` + `prettier` | 前端规范 | CI 拦截 |
| `pre-commit` | 本地提交前自动跑上面这些 | 本地拦截 |

**纪律：不要和工具争论。** 如果 lint 规则让你难受，
改配置要单独提交并说明理由，不要用 `# noqa` 绕过。

---

## 六、什么必须写进版本库，什么绝对不能

| 必须进 | 绝对不能进 |
|--------|-----------|
| `AGENTS.md`、`docs/`、`evals/`（含 `attribution.md`） | `.env`（只提交 `.env.example`） |
| 提示词模板 `app/prompts/*.md` | 任何 API Key、密码、内网 IP |
| **语义层 `config/semantic_layer.yaml`**（V2） | **SQL 通道的 DSN**（只写环境变量名 `SQL_CHANNEL_DSN`） |
| 数据库迁移文件 | 模型权重文件（写进 `.gitignore` + 文档说明下载地址） |
| `docker-compose.yml` | 用户上传的原始文档、向量数据、日志 |
| 测试与评估结果摘要 | 大于 10 MB 的任何二进制文件 |
| `evals/pending/` 下的候选题目 | `eval_run_items` 的数据库导出（含系统原答案，属内网数据） |
| `evals/sql/seed.sql` 与 `expected/`（V2） | 业务库的任何真实数据导出（种子库是**人造**的，真库永远不进仓库） |

> **（V2）语义层进 Git、但不进数据库**——这是个刻意的选择（见 `docs/adr/0009`）：
> 口径变更需要被**逐行 diff**、被 review、被回滚，这正是 Git 擅长的。
> 数据库里只存**生效的版本号**（`semantic_layer_versions` 表），
> 用于回答"某次问答当时用的是哪版口径"。
> **不要把 YAML 正文也塞进数据库**，那样你就失去了 diff 能力。

**上线前必须跑 `make scan`**，确认没有密钥误提交。
密钥一旦提交进 Git 历史，即使后续删除也仍然存在于历史中——必须立刻轮换密钥。

> **关于评估结果的双份存储**：数据库 `eval_runs` / `eval_run_items` 是主存，
> `evals/report/*.json` 是便于离线查看的副本。
> **两者口径必须一致**，出现分歧时**以数据库为准**（文件是人可能手改的，库里的带 `git_commit`）。
> 另外 `eval_run_items.answer` 含系统原答案，**导出时注意它属于内网数据**。

---

## 七、AI 协作的三个固定动作

1. **开任务前**：`请读 AGENTS.md，然后说明你打算怎么实现 T05，先不要写代码。`
2. **做完一个任务**：`make test && make lint && make eval` 三条都绿，才算完成。
3. **提交前**：让 AI 生成提交信息，你检查类型和范围是否用对。

---

## 八、与「用户级规则」的关系（冲突时一律以本项目为准）

Trae 的**用户级规则**（`~/.trae-cn/user_rules/`，全局，对所有项目生效）里有一套通用开发流程。
本项目另有一套**更具体**的规则：本文件 + `.trae/rules/project_rules.md` + `AGENTS.md`。

**两者冲突时，一律以本项目规则为准。** 理由很简单：项目规则是针对这套技术栈、
这套约束（单人、预算极低、要过质量门禁）写的；用户级规则要适配"所有项目"，所以只能说平均话。

> **✅ 已同步（2026-10-13）**：用户级文件已按下表调整完毕——5 处冲突项在用户级文件里
> 全部改写成「**以项目规定为准**」的默认值写法（数值型硬阈值换成**语义触发**），
> 并新增第 0 节（读法）+ 第 12 节（有争议时怎么办）。
> **下表保留，作为「为什么这么规定」的记录**，不必再逐条比对。

**5 处已裁决的差异：**

| 事项 | 用户级规则说 | **本项目规定（以此为准）** | 为什么 |
|------|-------------|------------------------|--------|
| 版本号 / Tag | 第一版 `v0.1`、第二版 `v0.2`… | 版本名用 `V-1` / `V0.1` / `V1` / `V2`（见 `docs/releases.md`）；**Tag 名 = 项目版本名小写**：`v0.1`、`v1`、`v2`。**V-1 不打 Tag**（它只是骨架，不对外） | 两套编号并存，三周后会出现"V1 阶段打了 v0.3 标签"这种对不上账的情况 |
| 分支名 | `feature/xxx` | `t<任务号>-<简短英文描述>`，如 `t05-hybrid-search`（见第二节） | 分支名带任务号，`git branch` 一眼看出做到哪一步、还剩哪些 |
| Commit 类型 | 6 种（`feat`/`fix`/`docs`/`refactor`/`test`/`chore`） | **10 种**，多出 `prompt` / `eval` / `semantic` / `perf`（见第三节） | 提示词、评估集、语义层、性能必须能单独回溯。少了 `prompt` 就查不出"哪版提示词最好"，少了 `semantic` 就查不出"口径什么时候改的" |
| 大改动阈值 | `> 3 个文件` 先建检查点 commit 或分支 | `> **8** 个文件` 说明**任务拆得太大**，应该先拆任务 | 3 个太紧，会制造一堆无意义的检查点提交；真正的问题不是"缺检查点"，而是"任务切得太大导致你审不了" |
| 文档维护 | 每个模块完成后更新 **README 的功能列表** | README 是**导航文档**，不放功能清单；进度只记在 `docs/tasks.md` | README 一旦兼做进度表，就会和 `docs/tasks.md` 互相打架，而 AI 会挑一个它喜欢的执行 |

**其余部分两边一致、不冲突，照做即可**：
安全守则（`.env` + 不硬编码密钥）、分步提交（一次提交只做一件事）、
依赖管理（先说明理由、锁版本、保持极简）、测试验证（做完即测）、
MVP 策略（先跑通最核心链路再迭代）—— 这五条本项目也就这么规定的。

> **一条兜底纪律**：如果某件事**本文件没写、用户级规则也没写**，
> **先问我，不要自己选一个看起来合理的做法**（`project_rules.md` 第 0 节第 3 条：
> "猜错比不写更糟"）。这条比上面任何一条裁决都重要。
