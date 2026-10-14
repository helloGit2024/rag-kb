---
description: 接口层与数据层专项规则：接口契约、错误码、数据库迁移、权限数据模型。
globs: "backend/app/api/**, app/api/**, backend/app/schemas/**, app/schemas/**, backend/app/repos/**, app/repos/**, backend/alembic/**, alembic/**, docs/openapi.yaml, docs/data-model.md"
alwaysApply: false
---

# 接口与数据层专项规则

## 一、接口契约（`docs/openapi.yaml` 是唯一事实来源）

- 字段名、类型、层级**一律以 `docs/openapi.yaml` 为准**。你需要新字段时：
  先改 `openapi.yaml` → 同步改 `docs/api-contract.md` → 再写代码。**不许"先在代码里加、文档回头再补"。**
- 路径为小写 + 连字符 + `/api/v1` 前缀；JSON 字段用 `snake_case`（与 Python 一致，前端做一次性映射）。
- **不要自己发明新的响应包装格式**。成功直接返回资源对象，错误统一：
  ```json
  { "error": { "code": "DOCUMENT_NOT_FOUND", "message": "文档不存在", "detail": {} } }
  ```
- 错误码必须来自 `openapi.yaml` 里的枚举，**不允许临时造一个字符串**。前端要按 code 分支处理。

## 二、必须区分的三种"非成功但非错误"状态（易被写成 500 或 4xx）

| 状态 | HTTP | 结构 | 为什么不能当错误 |
|---|---|---|---|
| 拒答 | **200** | `{"refused": true, "reason": "no_evidence", ...}` | 这是正确业务行为；返回 4xx 会让前端把它当异常，也会污染错误率监控 |
| 降级 | **200** | `{"degraded": true, "degrade_reason": "primary_model_timeout", ...}` | 服务仍在正常工作，只是质量降级；必须可见但不能计入错误率 |
| 异步受理 | **202** | `{"task_id": "...", "status": "pending"}` | 未完成不等于失败；用轮询/SSE 推进状态 |

> 这三条是本项目最容易写错的地方。任何把拒答实现成 4xx/5xx 的代码都要打回。

## 三、鉴权与权限

- 鉴权在路由层统一用依赖注入（FastAPI `Depends`）完成，**不允许每个接口自己解析 token**。
- 从 token 解析出的 `user_scope` 必须**显式向下传递**到 service 与 repo 层（R2、R3）。
- 权限不足时的响应：**不要用 404 掩盖 403**。本项目明确返回 `403 FORBIDDEN_SCOPE`，并在审计日志中记录（掩盖会让用户以为文档不存在，也会让排障困难）。
- 缓存 key 生成函数**必须**接收 `user_scope`（R3），且使用 `scope` 的哈希而不是明文。

## 四、数据库与迁移（Alembic）

- 每一次迁移必须**成对提供 upgrade / downgrade**，且 `make migrate-down` 必须真的能用（H11 会验证）。
- 禁止在迁移里做数据回填 + 结构变更的混合作业。结构变更与数据回填**分两个迁移文件**。
- 字段类型：金额用 `NUMERIC`（不用 float）、时间统一 `TIMESTAMPTZ`（不用 naive datetime）、主键用 `UUID`。
- `chunks` 表必须冗余 `scope_key` 列并建索引——这是权限过滤能走索引的前提（见 `docs/data-model.md`）。**不要改成 join 权限表**，那会让每次召回都退化成全表扫描。
- 向量列固定维度，与嵌入模型绑定。**换嵌入模型 = 换向量维度 = 必须全量重建**，不是改个配置就行。
- 删除文档必须走"全链路清理"，顺序见 `docs/data-model.md` 第 7 步。任何"只删主表"的实现都算未完成。

## 五、日志与可观测性

- 结构化日志（JSON），必须包含 `request_id`、`user_id`、`scope_hash`、`route`、`latency_ms`。
- **禁止把全文文档内容、完整提示词、密钥写入日志**。检索日志只记 chunk_id 与分数，不记 chunk 文本。
- 审计日志表只允许 INSERT：用数据库层面的约束保证不可 UPDATE/DELETE（V1 生效，见 `docs/security.md`）。
