"""T20：不抢占在线算力——嵌入批间让出（pace_s）+ worker 并发信号量。

均不依赖外部服务（HTTP / DB 全 mock）：
- pacing：`embed_texts` 在 pace_s>0 时"批与批之间让出、最后一批后不让出"；
  pace_s=0（在线路径默认值）完全不让出。
- 接线：`ingest_document` 调 `embed_texts` 必须带 pace_s——
  忘了传参不会报错，功能静默失效，必须有测试钉住。
- 信号量：`process_one` 全程受 `_INGEST_SEMAPHORE` 保护，并发上限 = 许可数（R10）。
"""

from __future__ import annotations

import threading
import time
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.core import embedding as embedding_mod
from app.core.embedding import embed_texts
from app.services import ingest as ingest_mod
from app.workers import ingest_worker as worker_mod


def _fake_vectors(n: int, dim: int = 1024) -> list[list[float]]:
    return [[0.0] * dim for _ in range(n)]


class TestEmbedPacing:
    """embed_texts 的 pace_s 行为（mock _embed_batch，不发 HTTP）。"""

    def _patch_batch(
        self, monkeypatch: pytest.MonkeyPatch, batch_sizes: list[int]
    ) -> None:
        def fake_batch(client, base_url, model, dim, batch):  # noqa: ANN001, ANN202
            batch_sizes.append(len(batch))
            return _fake_vectors(len(batch), dim)

        monkeypatch.setattr(embedding_mod, "_embed_batch", fake_batch)

    def _patch_sleep(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        sleeps: list[float] = []
        monkeypatch.setattr(embedding_mod.time, "sleep", sleeps.append)
        return sleeps

    def test_no_pace_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """在线路径默认 pace_s=0：多批也绝不让出（行为与 T20 之前一致）。"""
        self._patch_batch(monkeypatch, [])
        sleeps = self._patch_sleep(monkeypatch)
        out = embed_texts(["x"] * 65)  # 32+32+1 = 3 批
        assert len(out) == 65
        assert sleeps == []

    def test_pace_between_batches_not_after_last(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 批 → 恰好 2 个让出窗口；最后一批之后不让出（窗口只为"两批之间"服务）。"""
        batch_sizes: list[int] = []
        self._patch_batch(monkeypatch, batch_sizes)
        sleeps = self._patch_sleep(monkeypatch)
        out = embed_texts(["x"] * 65, pace_s=0.2)
        assert len(out) == 65
        assert batch_sizes == [32, 32, 1]
        assert sleeps == [0.2, 0.2]

    def test_single_batch_never_paces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有一批时没有任何让出——小文件入库不多花一分钱时间。"""
        self._patch_batch(monkeypatch, [])
        sleeps = self._patch_sleep(monkeypatch)
        embed_texts(["x"] * 5, pace_s=0.2)
        assert sleeps == []


class TestIngestPassesPace:
    """接线：入库链路必须真的把 pace_s 传给 embed_texts。"""

    def test_ingest_calls_embed_with_pace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_embed(texts, **kwargs):  # noqa: ANN001, ANN002, ANN202
            captured.update(kwargs)
            return _fake_vectors(len(texts))

        # 全链路其他环节全部 stub：本测试只关心"嵌入调用带没带 pace_s"
        monkeypatch.setattr(ingest_mod, "embed_texts", fake_embed)
        monkeypatch.setattr(
            ingest_mod, "find_active_by_md5", lambda *a, **k: None
        )
        monkeypatch.setattr(ingest_mod, "ensure_bucket", lambda: None)
        monkeypatch.setattr(ingest_mod, "put_text", lambda *a, **k: None)
        monkeypatch.setattr(ingest_mod, "insert_document", lambda *a, **k: 1)
        monkeypatch.setattr(ingest_mod, "insert_chunks", lambda *a, **k: None)

        class _FakeSession:
            def commit(self) -> None:
                pass

            def rollback(self) -> None:
                pass

        result = ingest_mod.ingest_document(
            cast(Session, _FakeSession()),
            kb_id=1,
            filename="t.md",
            data="# 标题\n\n正文内容，足够分出一个块。".encode(),
            scope="internal",
        )
        assert result.chunk_count == result.vector_count
        assert captured.get("pace_s") == ingest_mod._INGEST_EMBED_PACE_S
        # 默认值必须 > 0：0 等于没做 T20
        assert ingest_mod._INGEST_EMBED_PACE_S > 0


class TestWorkerSemaphore:
    """process_one 的并发上限（R10：离线任务并发数用信号量限制）。"""

    def test_process_one_respects_permit_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 许可时，4 个线程同时调 process_one，任意时刻最多 1 个在执行。"""
        # 换成全新的 1 许可信号量，不受模块级信号量当前持有状态影响
        monkeypatch.setattr(
            worker_mod, "_INGEST_SEMAPHORE", threading.BoundedSemaphore(1)
        )

        peak = 0
        current = 0
        lock = threading.Lock()

        def fake_guarded(session, task_id) -> None:  # noqa: ANN001
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.02)  # 模拟任务执行耗时，给并发冲突留窗口
            with lock:
                current -= 1

        monkeypatch.setattr(worker_mod, "_process_one_guarded", fake_guarded)

        threads = [
            threading.Thread(target=worker_mod.process_one, args=(None, i))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak == 1

    def test_default_permit_is_one(self) -> None:
        """默认 INGEST_WORKER_CONCURRENCY=1：拿到第一个许可后第二个拿不到。"""
        sem = worker_mod._INGEST_SEMAPHORE
        assert sem.acquire(blocking=False) is True
        try:
            assert sem.acquire(blocking=False) is False
        finally:
            sem.release()
