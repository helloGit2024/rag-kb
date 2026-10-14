# 基础镜像按 digest 锁定（用户级规则：锁定依赖版本，避免漂移）。
# 当前 = python:3.12.15-slim-trixie（2026-10-06 构建）。
# 升级方法：docker buildx imagetools inspect python:3.12-slim  查新 digest 替换。
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic.ini alembic.ini
COPY alembic ./alembic

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
