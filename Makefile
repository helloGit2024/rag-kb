# Makefile — 统一命令入口（V-1 骨架版）
#
# 为什么需要这个：AI 和你用同一套命令，避免"你敲的和他写的不一样"。
# 每个命令都用大白话说清楚它干什么。
# V-1 只保留当前阶段需要的命令；migrate / eval 等在后续版本加入。

.PHONY: help dev down logs test lint scan clean migrate migrate-down eval

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
# 本机跑测试：从 .env 读连接串，并把 compose 服务名换成 localhost
# （postgres / silo / redis **三个都要换**；与 scripts/ingest.py 同一套约定；
#  凭证只存在于被 .gitignore 忽略的 .env，Makefile 里只出现变量名，符合 R1）
# pytest / ruff / mypy / gitleaks 从哪来：优先用项目自带的 .venv（存在即用），
# 否则回落到 PATH（CI 容器内通常是全局安装）。目的是不让 `make test` /
# `make lint` / `make scan` 因为"忘了 source activate"只丢一句
# `command not found` 就停住——门禁命令必须敲下去就能见结果。
# 需要临时改用别的解释器时：make lint RUFF=/path/to/ruff MYPY=/path/to/mypy
PYTEST    ?= $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,pytest)
RUFF      ?= $(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff)
MYPY      ?= $(if $(wildcard .venv/bin/mypy),.venv/bin/mypy,mypy)
GITLEAKS  ?= $(if $(wildcard .venv/bin/gitleaks),.venv/bin/gitleaks,gitleaks)

test:  ## 跑全部测试（透传 pytest 参数：make test T="-k permission"）
	@set -a; . ./.env; set +a; \
	DATABASE_URL=$$(printf '%s' "$$DATABASE_URL" | sed 's/@postgres:/@localhost:/'); \
	export DATABASE_URL; \
	MINIO_ENDPOINT=$$(printf '%s' "$$MINIO_ENDPOINT" | sed 's|://silo:|://localhost:|'); \
	export MINIO_ENDPOINT; \
	REDIS_URL=$$(printf '%s' "$$REDIS_URL" | sed 's|//redis:|//localhost:|'); \
	export REDIS_URL; \
	PYTHONPATH=. $(PYTEST) -q $(T)

lint:  ## 检查代码规范与类型错误
	$(RUFF) check .
	$(MYPY) app

scan:  ## 扫描误提交的密钥（上线前必须跑）
	$(GITLEAKS) detect --no-banner --redact

# ── 评估 ────────────────────────────────────────────────
PYTHON    ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

eval:  ## 跑评估集（V0.1 31 题三层指标，结果写 eval_runs / eval_run_items）
	@set -a; . ./.env; set +a; \
	DATABASE_URL=$$(printf '%s' "$$DATABASE_URL" | sed 's/@postgres:/@localhost:/'); \
	export DATABASE_URL; \
	PYTHONPATH=. $(PYTHON) -m app.eval.runner $(ARGS)

eval-compare:  ## T31 对比两次 run。用法：make eval-compare BASELINE=97 TARGET=149 或 make eval-compare AUTO=1
	@set -a; . ./.env; set +a; \
	DATABASE_URL=$$(printf '%s' "$$DATABASE_URL" | sed 's/@postgres:/@localhost:/'); \
	export DATABASE_URL; \
	$(if $(AUTO), \
		PYTHONPATH=. $(PYTHON) -m app.eval.compare --auto, \
		PYTHONPATH=. $(PYTHON) -m app.eval.compare --baseline $(BASELINE) --target $(TARGET))

eval-attribute:  ## 归因候选回填（只跑检索，不调生成；绝不写结论，R15。用法：make eval-attribute ARGS="--run-id 87"）
	@set -a; . ./.env; set +a; \
	DATABASE_URL=$$(printf '%s' "$$DATABASE_URL" | sed 's/@postgres:/@localhost:/'); \
	export DATABASE_URL; \
	PYTHONPATH=. $(PYTHON) -m app.eval.attribution $(ARGS)

# ── 清理 ────────────────────────────────────────────────
clean:  ## 清理构建产物与临时文件（不动数据卷）
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache
