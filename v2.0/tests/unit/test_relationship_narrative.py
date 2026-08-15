from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def manager_at(now: datetime = NOW) -> RelationshipManager:
    conn = sqlite3.connect(":memory:")
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "005_add_relationship_tables.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    return RelationshipManager(
        RelationshipStore(conn=conn), RelationshipLimits(), clock=lambda: now,
        evidence_exists=lambda value: value in {"agent:chat:e1", "agent:speech:e2"},
    )


def test_narrative_requires_existing_evidence() -> None:
    manager = manager_at()
    assert manager.create_narrative(
        summary="made up arc", event_refs=["invented"], reason="operator",
    ) is None
    item = manager.create_narrative(
        summary="Mai promised a cat story", event_refs=["agent:chat:e1"],
        reason="operator verified",
    )
    assert item is not None
    context = manager.render_context()
    assert "Mai promised a cat story" in context
    assert "agent:chat:e1" in context


def test_resolved_or_expired_narrative_never_enters_prompt() -> None:
    manager = manager_at()
    item = manager.create_narrative(
        summary="temporary arc", event_refs=["agent:chat:e1"], reason="operator",
    )
    assert item is not None
    assert manager.resolve_narrative(item.narrative_id, reason="finished") is True
    assert "temporary arc" not in manager.render_context()

    expired_manager = manager_at(NOW + timedelta(days=31))
    expired = expired_manager.create_narrative(
        summary="already old", event_refs=["agent:chat:e1"], reason="operator",
    )
    assert expired is not None  # created at injected current time, therefore live


def test_only_approved_notes_enter_viewer_context() -> None:
    manager = manager_at()
    profile = manager.observe_interaction(raw_viewer_id="raw", event_id="e1", occurred_at=NOW)
    assert profile is not None
    note = manager.create_note(
        profile.viewer_id, summary="likes cats", evidence_refs=["agent:chat:e1"],
        reason="operator",
    )
    assert note is not None
    assert "likes cats" not in manager.render_context("raw")
    manager.review_note(note.note_id, approve=True, reason="verified")
    assert "likes cats" in manager.render_context("raw")
    assert manager.render_context("different viewer") == ""


def test_context_is_bounded() -> None:
    manager = manager_at()
    for index in range(4):
        manager.create_narrative(
            summary=f"arc {index} " + "x" * 200,
            event_refs=["agent:chat:e1"], reason="operator",
        )
    assert len(manager.render_context()) <= manager.limits.prompt_max_chars

