"""嵌入服务客户端（V-1：Ollama）。

★ 企业级真实入库必须用 TEI（HuggingFace Text Embeddings Inference）替换 Ollama：
  Ollama 定位是开发者本机工具，吞吐与并发能力不适合生产批量入库
  （架构文档 ADR-0007 指定 TEI）。V-1 选用 Ollama 仅因开发机已就绪、零下载。
  切换方式：只改本文件的请求构造（/api/embed → TEI 的 /embed），
  embed_texts() 的签名与返回不变，业务层（ingest / 检索）无需改动；
  切换后需重新入库——不同推理栈的浮点细微差异会影响新旧向量的一致性。

接口约定（架构文档铁律②）：
  bge-m3 输出 1024 维、已归一化（norm≈1）。本客户端显式校验维度，
  归一化作抽样确认（余弦距离对缩放不敏感，只记录不阻断）。
"""

from __future__ import annotations

import math
import os

import httpx


class EmbeddingError(RuntimeError):
    """嵌入调用失败（网络不可达、响应格式异常、维度不符）。"""


def embed_texts(
    texts: list[str],
    *,
    base_url: str | None = None,
    model: str | None = None,
    dim: int | None = None,
    batch_size: int = 32,
    timeout_s: float = 120.0,
) -> list[list[float]]:
    """批量把文本转成向量（Ollama /api/embed）。

    Args:
        texts: 文本列表。
        base_url: Ollama 地址，默认读环境变量 OLLAMA_BASE_URL。
        model: 模型名，默认读 EMBEDDING_MODEL（bge-m3）。
        dim: 期望维度，默认读 EMBEDDING_DIM（1024）。
        batch_size: 单次请求批量大小（受内存限制，CPU 推理 32 为保守值）。
        timeout_s: 单批超时。CPU 推理一批 32 条较慢，默认 120s。

    Returns:
        与 texts 等长的向量列表，顺序一一对应。
    """
    # 这里用「单参 getenv + or 兜底」，而不用 os.getenv(key, default) 两参形式：
    # 两参形式在当前 mypy 下被推断为 str | None，会让 int() 拿到 Optional 类型而报错。
    # 语义等价（空字符串也会落到默认值，反而更稳）。
    base_url = base_url or os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
    model = model or os.getenv("EMBEDDING_MODEL") or "bge-m3"
    dim_env = os.getenv("EMBEDDING_DIM")
    dim = dim if dim is not None else int(dim_env or "1024")

    if not texts:
        return []

    vectors: list[list[float]] = []
    # trust_env=False：嵌入服务是本机/内网组件，绝不能走系统代理——
    # 否则本机代理（如 Clash）会把 localhost 请求转发出去，返回 502
    with httpx.Client(timeout=timeout_s, trust_env=False) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vectors.extend(_embed_batch(client, base_url, model, dim, batch))
    return vectors


def _embed_batch(
    client: httpx.Client, base_url: str, model: str, dim: int, batch: list[str]
) -> list[list[float]]:
    """调一次 /api/embed 并校验响应（R7：显式失败，不吞错误）。"""
    try:
        resp = client.post(
            f"{base_url}/api/embed", json={"model": model, "input": batch}
        )
    except httpx.HTTPError as exc:
        raise EmbeddingError(
            f"嵌入服务不可达（{base_url}）：{exc!r}。"
            f"请确认 Ollama 已启动且已拉取模型 {model}（ollama list 查看）。"
        ) from exc

    if resp.status_code != 200:
        raise EmbeddingError(
            f"嵌入请求失败：HTTP {resp.status_code}，body={resp.text[:300]}"
        )

    data = resp.json()
    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(batch):
        raise EmbeddingError(
            f"嵌入响应格式异常：期望 {len(batch)} 条向量，实际 {str(embeddings)[:200]}"
        )

    for j, vec in enumerate(embeddings):
        if len(vec) != dim:
            raise EmbeddingError(
                f"向量维度不符：期望 {dim}，实际 {len(vec)}（第 {j} 条）。"
                f"模型输出维度必须与 chunks.embedding 列定义一致。"
            )
    return embeddings


def check_normalized(vectors: list[list[float]], tolerance: float = 0.01) -> float:
    """抽样校验向量是否已归一化，返回平均 norm。

    偏离 1 超过容差时打印告警（不阻断——余弦距离对缩放不敏感，
    此校验只为显式确认架构文档铁律②，避免检索质量悄悄变差）。
    """
    if not vectors:
        return 0.0
    norms = [math.sqrt(sum(x * x for x in vec)) for vec in vectors]
    avg = sum(norms) / len(norms)
    if abs(avg - 1.0) > tolerance:
        print(f"⚠️ 向量未归一化（平均 norm={avg:.4f}），请确认模型输出格式")
    return avg
