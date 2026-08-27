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
    assert loader.get("features", "features.memory_semantic.enabled") is True


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
