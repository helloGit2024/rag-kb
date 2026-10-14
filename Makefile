# Makefile — 统一命令入口（V-1 骨架版）
#
# 为什么需要这个：AI 和你用同一套命令，避免"你敲的和他写的不一样"。
# 每个命令都用大白话说清楚它干什么。
# V-1 只保留当前阶段需要的命令；migrate / eval 等在后续版本加入。

.PHONY: help dev down logs test lint scan clean migrate migrate-down

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ── 开发环境 ────────────────────────────────────────────
dev:  ## 启动本地开发环境（PG + Redis + backend）
	docker compose -f docker-compose.dev.yml up -d --build
	@echo "服务已启动，健康检查：curl -s http://localhost:8000/healthz"

down:  ## 停止全部服务（保留数据卷）
	docker compose -f docker-compose.dev.yml down

logs:  ## 查看全部服务日志
	docker compose -f docker-compose.dev.yml logs -f --tail=100

# ── 数据库迁移 ──────────────────────────────────────────
migrate:  ## 执行数据库迁移（升级到最新版本）
	docker compose -f docker-compose.dev.yml exec backend alembic upgrade head

migrate-down:  ## 回滚上一个迁移
	docker compose -f docker-compose.dev.yml exec backend alembic downgrade -1

# ── 质量检查 ────────────────────────────────────────────
# 本机跑测试：从 .env 读连接串，并把 compose 服务名 postgres 换成 localhost
# （与 scripts/ingest.py 同一套约定；凭证只存在于被 .gitignore 忽略的 .env，
#   Makefile 里只出现变量名，符合 R1）
test:  ## 跑全部测试
	@set -a; . ./.env; set +a; \
	DATABASE_URL=$$(printf '%s' "$$DATABASE_URL" | sed 's/@postgres:/@localhost:/'); \
	export DATABASE_URL; \
	PYTHONPATH=. pytest -q

lint:  ## 检查代码规范与类型错误
	ruff check .
	mypy app

scan:  ## 扫描误提交的密钥（上线前必须跑）
	gitleaks detect --no-banner --redact

# ── 清理 ────────────────────────────────────────────────
clean:  ## 清理构建产物与临时文件（不动数据卷）
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache
