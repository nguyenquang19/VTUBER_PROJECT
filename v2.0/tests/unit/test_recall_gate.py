from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from interfaces.memory import MemoryEntry
from services.memory.config import MemoryRuntimeConfig
from services.memory.recall_gate import RecallGate


NOW = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)


def _config(**updates: object) -> MemoryRuntimeConfig:
    base = MemoryRuntimeConfig(
        working_maxlen=20,
        semantic_max_entries=100,
        query_timeout_s=0.15,
        latency_sample_max=32,
        default_top_k=3,
        max_query_top_k=20,
        content_max_chars=4000,
        metadata_max_items=24,
        metadata_text_max_chars=512,
        tags_max=12,
        tag_max_chars=64,
        extractor_min_chars=15,
        extractor_promote_intensity=7,
        pending_writes_max=32,
    )
    return replace(base, **updates)


def _entry(
    entry_id: str, *, importance: float = 0.9, kind: str = "EPISODIC",
) -> MemoryEntry:
    return MemoryEntry(
        entry_id=entry_id,
        content=f"raw private memory {entry_id}",
        timestamp=NOW,
        importance=importance,
        metadata={"cognitive_kind": kind},
    )


def test_decision_is_deterministic_and_hint_never_contains_raw_memory() -> None:
    entry = _entry("m1")
    first = RecallGate(_config()).evaluate((entry,), now=NOW)
    replay = RecallGate(_config()).evaluate((entry,), now=NOW)
    assert replay == first
    assert first[0].surface is True
    assert entry.content not in (first[0].latent_hint or "")
    assert first[0].salience == 0.9


def test_salience_cooldown_frequency_and_window_are_enforced() -> None:
    gate = RecallGate(_config())
    low = gate.evaluate((_entry("low", importance=0.59),), now=NOW)
    first = gate.evaluate((_entry("m1"),), now=NOW)
    cooldown = gate.evaluate((_entry("m1"),), now=NOW + timedelta(seconds=1))
    second = gate.evaluate((_entry("m2"),), now=NOW + timedelta(seconds=1))
    capped = gate.evaluate((_entry("m3"),), now=NOW + timedelta(seconds=2))
    reopened = gate.evaluate((_entry("m3"),), now=NOW + timedelta(seconds=121))
    assert low[0].reason_code == "salience"
    assert first[0].surface is True
    assert cooldown[0].reason_code == "cooldown"
    assert second[0].surface is True
    assert capped[0].reason_code == "frequency_cap"
    assert reopened[0].surface is True


def test_context_cap_and_entry_history_are_bounded() -> None:
    gate = RecallGate(_config(
        recall_cooldown_s=1000.0,
        recall_frequency_window_s=1.0,
        recall_frequency_cap=2,
        recall_max_hints=1,
        recall_entry_history_max=2,
    ))
    decisions = gate.evaluate((_entry("m1"), _entry("m2")), now=NOW)
    assert decisions[0].surface is True
    assert decisions[1].reason_code == "context_cap"
    gate.evaluate((_entry("m2"),), now=NOW + timedelta(seconds=2))
    gate.evaluate((_entry("m3"),), now=NOW + timedelta(seconds=4))
    metrics = gate.get_metrics()
    assert metrics["recall_gate_retained_entries"] == 2
    assert metrics["recall_gate_retained_surfaces"] <= 2
    assert metrics["recall_gate_history_evictions_total"] == 1


def test_flag_off_is_non_leaking_and_toggle_clears_cooldown_state() -> None:
    gate = RecallGate(_config())
    entry = _entry("m1")
    assert gate.evaluate((entry,), now=NOW)[0].surface is True
    gate.set_enabled(False)
    disabled = gate.evaluate((entry,), now=NOW + timedelta(seconds=1))[0]
    assert disabled.surface is False
    assert disabled.latent_hint is None
    assert disabled.reason_code == "disabled"
    gate.set_enabled(True)
    assert gate.evaluate((entry,), now=NOW + timedelta(seconds=2))[0].surface is True
