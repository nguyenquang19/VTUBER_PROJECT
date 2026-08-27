from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from interfaces.memory import RecallDecision
from interfaces.relationship import RelationshipHintKind
from orchestrator.config_loader import ConfigLoader
from services.memory.config import MemoryRuntimeConfig
from services.memory.recall_gate import RecallGate
from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 27, 10, 0, 10, tzinfo=timezone.utc)
RAW_VIEWER = "platform-user-very-secret"


def _memory_config() -> MemoryRuntimeConfig:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return MemoryRuntimeConfig.from_loader(loader)


def _recall_gate() -> RecallGate:
    return RecallGate(_memory_config(), enabled=True)


def _manager(
    clock: list[datetime], *, context_enabled: bool = True,
    gate: RecallGate | None = None, limits: RelationshipLimits | None = None,
) -> RelationshipManager:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        (ROOT / "migrations" / "005_add_relationship_tables.sql").read_text(
            encoding="utf-8",
        ),
    )
    conn.executescript(
        (ROOT / "migrations" / "006_add_relationship_positive_events.sql").read_text(
            encoding="utf-8",
        ),
    )
    return RelationshipManager(
        RelationshipStore(conn=conn),
        limits or RelationshipLimits(),
        clock=lambda: clock[0],
        context_enabled=context_enabled,
        recall_gate=gate or _recall_gate(),
        evidence_exists=lambda ref: ref in {
            "agent:chat:e1", "agent:chat:e2", "agent:chat:e3",
        },
    )


def _regular(
    manager: RelationshipManager, *, positive: bool = False,
) -> str:
    viewer_id = ""
    for index in range(1, 4):
        profile = manager.observe_interaction(
            raw_viewer_id=RAW_VIEWER,
            event_id=f"e{index}",
            occurred_at=NOW - timedelta(seconds=4 - index),
            emotion_category="chat_compliment" if positive else "chat_neutral",
        )
        assert profile is not None
        viewer_id = profile.viewer_id
    return viewer_id


def test_only_recent_regular_gets_fixed_warm_tone() -> None:
    clock = [NOW]
    manager = _manager(clock)
    assert manager.context_hints(raw_viewer_id=RAW_VIEWER) == ()
    manager.observe_interaction(
        raw_viewer_id=RAW_VIEWER, event_id="e1",
        occurred_at=NOW - timedelta(seconds=3),
    )
    manager.observe_interaction(
        raw_viewer_id=RAW_VIEWER, event_id="e2",
        occurred_at=NOW - timedelta(seconds=2),
    )
    assert manager.context_hints(raw_viewer_id=RAW_VIEWER) == ()
    profile = manager.observe_interaction(
        raw_viewer_id=RAW_VIEWER, event_id="e3",
        occurred_at=NOW - timedelta(seconds=1),
    )
    assert profile is not None

    hints = manager.context_hints(raw_viewer_id=RAW_VIEWER)
    assert len(hints) == 1
    assert hints[0].kind is RelationshipHintKind.TONE
    assert hints[0].viewer_ref == profile.viewer_id
    assert "returning regular" in hints[0].instruction
    assert RAW_VIEWER not in hints[0].instruction

    clock[0] += timedelta(days=15)
    assert manager.context_hints(raw_viewer_id=RAW_VIEWER) == ()


def test_fact_hint_uses_event_lineage_and_never_stored_wording_or_pii() -> None:
    clock = [NOW]
    manager = _manager(clock)
    viewer_id = _regular(manager)
    raw_fact = "mê truyện mèo sapphire; email alice@example.com"
    profile = manager.update_profile(
        viewer_id,
        preferences=[raw_fact],
        boundaries=["không nhắc tên thật"],
        tone="gentle",
        evidence_refs=["agent:chat:e1"],
        reason="operator confirmed",
    )
    assert profile is not None

    hints = manager.context_hints(event_ref="agent:chat:e3")
    assert [hint.kind for hint in hints] == [
        RelationshipHintKind.TONE, RelationshipHintKind.FACT,
    ]
    rendered = "\n".join(hint.instruction for hint in hints)
    assert "known preference" in rendered
    assert raw_fact not in rendered
    assert "sapphire" not in rendered
    assert "alice@example.com" not in rendered
    assert RAW_VIEWER not in rendered
    assert all(hint.viewer_ref == viewer_id for hint in hints)
    assert manager.context_hints(event_ref="agent:chat:missing") == ()


def test_callback_requires_review_and_reopens_after_yaml_budgets() -> None:
    clock = [NOW]
    manager = _manager(clock)
    viewer_id = _regular(manager, positive=True)
    raw_callback = "the secret sapphire cat greeting"
    gag = manager.create_running_gag(
        viewer_id,
        summary=raw_callback,
        event_refs=["agent:chat:e1", "agent:chat:e2", "agent:chat:e3"],
        reason="operator proposal",
    )
    assert gag is not None
    assert [hint.kind for hint in manager.context_hints(raw_viewer_id=RAW_VIEWER)] == [
        RelationshipHintKind.TONE,
    ]
    assert manager.review_running_gag(
        gag.gag_id, approve=True, reason="operator approved",
    )

    hints = manager.context_hints(raw_viewer_id=RAW_VIEWER)
    assert [hint.kind for hint in hints] == [
        RelationshipHintKind.TONE, RelationshipHintKind.CALLBACK,
    ]
    assert raw_callback not in "\n".join(hint.instruction for hint in hints)
    assert manager.context_hints(raw_viewer_id=RAW_VIEWER)[0].kind is RelationshipHintKind.TONE
    assert len(manager.context_hints(raw_viewer_id=RAW_VIEWER)) == 1
    assert manager.get_metrics()["relationship_context_callback_suppressed_total"] >= 1

    clock[0] += timedelta(seconds=3601)
    reopened = manager.context_hints(raw_viewer_id=RAW_VIEWER)
    assert [hint.kind for hint in reopened] == [
        RelationshipHintKind.TONE, RelationshipHintKind.CALLBACK,
    ]


def test_gate_failure_is_tone_only_and_counted() -> None:
    class BrokenGate(RecallGate):
        def evaluate(self, entries, *, now):  # type: ignore[no-untyped-def]
            raise RuntimeError("gate failed")

    clock = [NOW]
    gate = BrokenGate(_memory_config(), enabled=True)
    manager = _manager(clock, gate=gate)
    viewer_id = _regular(manager)
    assert manager.update_profile(
        viewer_id,
        preferences=["mê truyện mèo"],
        boundaries=[],
        tone=None,
        evidence_refs=["agent:chat:e1"],
        reason="operator confirmed",
    ) is not None

    hints = manager.context_hints(raw_viewer_id=RAW_VIEWER)
    assert [hint.kind for hint in hints] == [RelationshipHintKind.TONE]
    assert manager.get_metrics()["relationship_context_gate_error_total"] == 1


def test_invalid_gate_batch_has_no_partial_callback_side_effect() -> None:
    class InvalidBatchGate(RecallGate):
        def evaluate(self, entries, *, now):  # type: ignore[no-untyped-def]
            return (
                RecallDecision(
                    memory_ref=entries[0].entry_id,
                    surface=True,
                    salience=0.9,
                    latent_hint="fixed safe callback hint",
                    reason_code="surfaced",
                ),
                object(),
            )

    clock = [NOW]
    manager = _manager(clock, gate=InvalidBatchGate(_memory_config(), enabled=True))
    viewer_id = _regular(manager, positive=True)
    assert manager.update_profile(
        viewer_id,
        preferences=["mê truyện mèo"], boundaries=[], tone=None,
        evidence_refs=["agent:chat:e1"], reason="operator confirmed",
    ) is not None
    gag = manager.create_running_gag(
        viewer_id,
        summary="the secret sapphire cat greeting",
        event_refs=["agent:chat:e1", "agent:chat:e2", "agent:chat:e3"],
        reason="operator proposal",
    )
    assert gag is not None
    assert manager.review_running_gag(gag.gag_id, approve=True, reason="approved")

    hints = manager.context_hints(raw_viewer_id=RAW_VIEWER)
    assert [hint.kind for hint in hints] == [RelationshipHintKind.TONE]
    stored_gag = next(item for item in manager.snapshot().running_gags if item.gag_id == gag.gag_id)
    assert stored_gag.last_referenced_at is None


def test_fact_candidate_count_is_bounded_before_recall_gate() -> None:
    class SpyGate(RecallGate):
        seen_count = 0

        def evaluate(self, entries, *, now):  # type: ignore[no-untyped-def]
            self.seen_count = len(entries)
            return super().evaluate(entries, now=now)

    clock = [NOW]
    config = replace(
        _memory_config(),
        recall_frequency_cap=3,
        recall_max_hints=3,
        recall_entry_history_max=3,
    )
    gate = SpyGate(config, enabled=True)
    manager = _manager(clock, gate=gate)
    viewer_id = _regular(manager)
    assert manager.update_profile(
        viewer_id,
        preferences=["mê truyện mèo"], boundaries=[], tone=None,
        evidence_refs=["agent:chat:e1"], reason="operator confirmed",
    ) is not None
    for index in range(2):
        note = manager.create_note(
            viewer_id,
            summary=f"approved detail {index}",
            evidence_refs=[f"agent:chat:e{index + 1}"],
            reason="operator observed",
        )
        assert note is not None
        assert manager.review_note(note.note_id, approve=True, reason="verified")

    manager.context_hints(raw_viewer_id=RAW_VIEWER)
    assert gate.seen_count == manager.limits.context_fact_slots_max == 2


def test_flag_off_restores_bounded_m7_renderer() -> None:
    clock = [NOW]
    manager = _manager(clock, context_enabled=False)
    viewer_id = _regular(manager)
    assert manager.update_profile(
        viewer_id,
        preferences=["likes sapphire cats"], boundaries=[], tone="gentle",
        evidence_refs=["agent:chat:e1"], reason="operator confirmed",
    ) is not None
    rendered = manager.render_context(RAW_VIEWER)
    assert "likes sapphire cats" in rendered
    assert manager.context_hints(raw_viewer_id=RAW_VIEWER) == ()


def test_canonical_config_declares_a4_bounds_and_dependency() -> None:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    limits = RelationshipLimits.from_loader(loader)
    assert limits.regular_min_interactions == 3
    assert limits.regular_last_seen_days == 14
    assert limits.context_fact_slots_max == 2
    assert limits.callback_frequency_window_s == 3600
    assert limits.callback_frequency_cap == 1
    feature = yaml.safe_load(
        (ROOT / "config" / "features.yaml").read_text(encoding="utf-8"),
    )["features"]["relationship_context"]
    assert feature["enabled"] is True
    assert feature["depends_on"] == ["recall_gate"]
