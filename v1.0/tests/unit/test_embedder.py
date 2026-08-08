"""Test BgeM3Embedder với FakeModel (không tải bge-m3 thật) — Phase 7.C."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from services.memory.embedder import BgeM3Embedder, EmbedderError

REPO_ROOT = Path(__file__).resolve().parents[2]

DIM = 1024


class FakeModel:
    """Model giả — không cần sentence-transformers/HF, encode ổn định giữa process."""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.encode_calls = 0
        self.last_kwargs: dict = {}

    def encode(self, texts, **kw):
        self.encode_calls += 1
        self.last_kwargs = kw
        if isinstance(texts, str):
            return self._one(texts)
        arrs = np.stack([self._one(t) for t in texts])
        return arrs

    def _one(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = np.frombuffer(digest, dtype=np.uint8).astype(np.float32) / 255.0
        return np.resize(values, self.dim).astype(np.float32)


def make(model: FakeModel | None = None, **over) -> BgeM3Embedder:
    kw = dict(model=model or FakeModel())
    kw.update(over)
    return BgeM3Embedder(**kw)


class TestLifecycle:
    def test_inject_model_no_load_needed(self) -> None:
        e = make()
        assert e.is_loaded() is True

    def test_embed_before_load_raises(self) -> None:
        e = BgeM3Embedder()  # no injected model
        assert e.is_loaded() is False
        with pytest.raises(EmbedderError, match="chưa load"):
            e.embed("hi")

    def test_metrics_default(self) -> None:
        e = make()
        m = e.get_metrics()
        assert m["embedder_calls_total"] == 0
        assert m["embedder_cache_hits"] == 0
        assert m["embedder_cache_size"] == 0


class TestEmbedOne:
    def test_returns_list_of_correct_dim(self) -> None:
        e = make()
        v = e.embed("Mai thích cà phê")
        assert isinstance(v, list)
        assert len(v) == DIM
        assert all(isinstance(x, float) for x in v[:5])

    def test_empty_text_raises(self) -> None:
        e = make()
        with pytest.raises(EmbedderError, match="text rỗng"):
            e.embed("   ")

    def test_dim_mismatch_raises(self) -> None:
        e = make(model=FakeModel(dim=512), dim=1024)
        with pytest.raises(EmbedderError, match="dim mismatch"):
            e.embed("hi")

    def test_deterministic(self) -> None:
        e = make()
        v1 = e.embed("Xin chào")
        v2 = e.embed("Xin chào")
        assert v1 == v2

    def test_different_text_different_vec(self) -> None:
        e = make()
        v1 = e.embed("Xin chào")
        v2 = e.embed("Tạm biệt")
        assert v1 != v2


class TestCache:
    def test_hit_avoids_reencode(self) -> None:
        model = FakeModel()
        e = make(model=model)
        e.embed("A")
        assert model.encode_calls == 1
        e.embed("A")  # cache hit
        assert model.encode_calls == 1
        m = e.get_metrics()
        assert m["embedder_cache_hits"] == 1
        assert m["embedder_calls_total"] == 2   # call vẫn tăng

    def test_evict_lru_when_full(self) -> None:
        e = make(cache_size=3)
        e.embed("A"); e.embed("B"); e.embed("C")
        assert e.get_metrics()["embedder_cache_size"] == 3
        e.embed("D")  # evict A (oldest)
        assert e.get_metrics()["embedder_cache_size"] == 3
        # A không còn cache → re-encode
        model_calls_before = e._model.encode_calls
        e.embed("A")
        assert e._model.encode_calls == model_calls_before + 1

    def test_move_to_end_on_hit(self) -> None:
        e = make(cache_size=3)
        e.embed("A"); e.embed("B"); e.embed("C")
        e.embed("A")  # A hit → move to end, giờ oldest là B
        e.embed("D")  # evict B
        # C, A, D còn; B bị evict
        keys = list(e._cache.keys())
        assert "B" not in keys
        assert set(keys) == {"C", "A", "D"}

    def test_clear_cache(self) -> None:
        e = make()
        e.embed("A"); e.embed("B")
        e.clear_cache()
        assert e.get_metrics()["embedder_cache_size"] == 0


class TestEmbedBatch:
    def test_returns_list_of_vecs(self) -> None:
        e = make()
        vecs = e.embed_batch(["a", "b", "c"])
        assert len(vecs) == 3
        assert all(len(v) == DIM for v in vecs)

    def test_empty_batch(self) -> None:
        e = make()
        assert e.embed_batch([]) == []

    def test_batch_does_not_use_cache(self) -> None:
        """Batch API tối ưu native — bypass cache."""
        model = FakeModel()
        e = make(model=model)
        e.embed_batch(["a", "b"])
        assert model.encode_calls == 1  # 1 batch call
        assert e.get_metrics()["embedder_cache_size"] == 0

    def test_normalize_flag_passed(self) -> None:
        model = FakeModel()
        e = make(model=model, normalize=True)
        e.embed_batch(["a"])
        assert model.last_kwargs.get("normalize_embeddings") is True


class TestFromLoader:
    def test_reads_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        e = BgeM3Embedder.from_loader(loader, model=FakeModel())
        assert e.model_name == "BAAI/bge-m3"
        assert e.device == "cpu"
        assert e.dim == 1024
        assert e.cache_size == 1000
        assert e.normalize is True
