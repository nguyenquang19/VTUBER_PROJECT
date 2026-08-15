"""BgeM3Embedder — sentence-transformers BAAI/bge-m3 CPU (Phase 7.C).

Encode text VN → float32 vec 1024-dim. CPU only (không đụng VRAM llama+VieNeu).
LRU cache text→vec để tránh re-encode câu lặp trong session (common case:
persona prefix, mood keywords).

Sync API. SemanticMemoryService (7.D) wrap trong asyncio.to_thread.

Test không tải model: inject `_model` giả qua constructor. Live test tải bge-m3
thật có marker "memory_live".
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from orchestrator.logger import get_logger


class EmbedderError(Exception):
    pass


class BgeM3Embedder:
    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        device: str = "cpu",
        dim: int = 1024,
        cache_size: int = 1000,
        normalize: bool = True,
        model: Any = None,   # inject cho test
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.dim = dim
        self.cache_size = cache_size
        self.normalize = normalize

        self._model = model
        self._cache: "OrderedDict[str, list[float]]" = OrderedDict()
        self._log = get_logger("embedder")

        self._loads_total = 0        # số lần load model (chỉ 1 khi warmup)
        self._embed_total = 0        # số call embed()
        self._cache_hits = 0

    @classmethod
    def from_loader(cls, loader, model: Any = None) -> "BgeM3Embedder":
        get = lambda k, d=None: loader.get("models", f"embedding.{k}", d)  # noqa: E731
        return cls(
            model_name=str(get("model", "BAAI/bge-m3")),
            device=str(get("device", "cpu")),
            dim=int(get("dim", 1024)),
            cache_size=int(get("cache_size", 1000)),
            normalize=bool(get("normalize", True)),
            model=model,
        )

    # ---------- lifecycle ----------

    def load(self) -> None:
        """Load model lần đầu — chậm (tải HF nếu chưa cache). Idempotent."""
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise EmbedderError(f"sentence-transformers chưa cài: {e}") from e
        self._log.info("embedder_loading", model=self.model_name, device=self.device)
        self._model = SentenceTransformer(self.model_name, device=self.device)
        self._loads_total += 1
        self._log.info("embedder_loaded")

    def is_loaded(self) -> bool:
        return self._model is not None

    def get_metrics(self) -> dict[str, Any]:
        return {
            "embedder_loads_total": self._loads_total,
            "embedder_calls_total": self._embed_total,
            "embedder_cache_hits": self._cache_hits,
            "embedder_cache_size": len(self._cache),
        }

    # ---------- embed ----------

    def embed(self, text: str) -> list[float]:
        """Encode 1 câu → vec (list float, len=dim). Hit cache trước."""
        if self._model is None:
            raise EmbedderError("chưa load() model")
        self._embed_total += 1
        key = text.strip()
        if not key:
            raise EmbedderError("text rỗng")

        if key in self._cache:
            # LRU: move to end
            self._cache.move_to_end(key)
            self._cache_hits += 1
            return self._cache[key]

        arr = self._encode_one(key)
        vec = arr.tolist()
        self._cache[key] = vec
        # Evict oldest nếu quá cache_size
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Encode batch — không dùng cache (batch API tối ưu native)."""
        if self._model is None:
            raise EmbedderError("chưa load() model")
        if not texts:
            return []
        self._embed_total += len(texts)
        arrs = self._encode_batch([t.strip() for t in texts])
        return [a.tolist() for a in arrs]

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------- internals ----------

    def _encode_one(self, text: str) -> np.ndarray:
        arr = self._model.encode(
            text, normalize_embeddings=self.normalize, convert_to_numpy=True,
        )
        arr = np.asarray(arr, dtype=np.float32).squeeze()
        self._validate_dim(arr)
        return arr

    def _encode_batch(self, texts: list[str]) -> list[np.ndarray]:
        arrs = self._model.encode(
            texts, normalize_embeddings=self.normalize, convert_to_numpy=True,
            batch_size=min(32, len(texts)),
        )
        arrs = np.asarray(arrs, dtype=np.float32)
        if arrs.ndim == 1:  # single text edge case
            arrs = arrs[np.newaxis, :]
        for a in arrs:
            self._validate_dim(a)
        return list(arrs)

    def _validate_dim(self, arr: np.ndarray) -> None:
        if arr.shape[-1] != self.dim:
            raise EmbedderError(
                f"embedding dim mismatch: got {arr.shape[-1]}, expect {self.dim}"
            )
