from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.memory.config import MemoryRuntimeConfig


REPO_ROOT = Path(__file__).resolve().parents[2]


def _raw() -> dict[str, object]:
    return {
        "working_maxlen": 20,
        "semantic_max_entries": 10000,
        "query_timeout_s": 0.15,
        "latency_sample_max": 256,
        "default_top_k": 3,
        "max_query_top_k": 20,
        "content_max_chars": 4000,
        "metadata_max_items": 24,
        "metadata_text_max_chars": 512,
        "tags_max": 12,
        "tag_max_chars": 64,
        "extractor_min_chars": 15,
        "extractor_promote_intensity": 7,
        "pending_writes_max": 64,
        "summary_every_turns": 6,
        "max_summaries": 8,
        "session_ttl_s": 21600,
        "summary_input_max_chars": 6000,
        "summary_max_chars": 600,
        "summary_max_tokens": 160,
        "summary_timeout_s": 10.0,
        "summary_pending_max": 1,
        "summary_seed": 42,
        "recency_weight": 0.4,
        "salience_weight": 0.6,
    }


class Loader:
    def __init__(self, raw: object) -> None:
        self.raw = raw

    def get(self, file_name: str, path: str, default=None):
        assert file_name == "system" and path == "memory"
        return self.raw


def test_canonical_memory_yaml_loads_strictly() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    config = MemoryRuntimeConfig.from_loader(loader)
    assert config.working_maxlen == 20
    assert config.semantic_max_entries == 10000
    assert config.query_timeout_s == 0.15
    assert config.latency_sample_max == 256
    assert config.pending_writes_max == 64
    assert config.summary_every_turns == 6
    assert config.max_summaries == 8
    assert config.recency_weight + config.salience_weight == 1.0
    assert loader.get("features", "features.memory_semantic.enabled") is True
    assert loader.get("features", "features.episodic_memory.enabled") is True
    assert loader.get("features", "features.episodic_memory.depends_on") == ["memory_semantic"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("working_maxlen", True),
        ("semantic_max_entries", 0),
        ("query_timeout_s", "0.15"),
        ("latency_sample_max", 0),
        ("default_top_k", 3.0),
        ("extractor_promote_intensity", 11),
        ("pending_writes_max", 0),
        ("summary_every_turns", 0),
        ("session_ttl_s", "21600"),
        ("summary_seed", -1),
        ("summary_pending_max", 2),
        ("recency_weight", 1.1),
    ],
)
def test_memory_config_rejects_coercion_and_invalid_ranges(field: str, value: object) -> None:
    raw = _raw()
    raw[field] = value
    with pytest.raises(ValueError):
        MemoryRuntimeConfig.from_loader(Loader(raw))


def test_memory_config_rejects_missing_and_unknown_keys() -> None:
    missing = _raw()
    missing.pop("tags_max")
    with pytest.raises(ValueError, match="missing"):
        MemoryRuntimeConfig.from_loader(Loader(missing))
    unknown = _raw()
    unknown["surprise"] = 1
    with pytest.raises(ValueError, match="unknown"):
        MemoryRuntimeConfig.from_loader(Loader(unknown))
