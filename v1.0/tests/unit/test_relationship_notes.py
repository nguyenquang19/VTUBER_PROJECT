from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore
from services.relationship.types import ReviewStatus


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def make_manager() -> tuple[RelationshipManager, str]:
    conn = sqlite3.connect(":memory:")
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "005_add_relationship_tables.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    manager = RelationshipManager(
        RelationshipStore(conn=conn), RelationshipLimits(), clock=lambda: NOW,
        evidence_exists=lambda event_id: event_id in {"agent:chat:e1", "agent:chat:e2"},
    )
    profile = manager.observe_interaction(
        raw_viewer_id="raw-viewer", event_id="e1", occurred_at=NOW,
    )
    assert profile is not None
    return manager, profile.viewer_id


def test_profile_attributes_require_real_evidence_and_are_sanitized() -> None:
    manager, viewer_id = make_manager()
    assert manager.update_profile(
        viewer_id, preferences=["likes cats"], boundaries=[], tone="gentle",
        evidence_refs=["invented"], reason="operator",
    ) is None
    profile = manager.update_profile(
        viewer_id, preferences=["likes cats", "email me at x@example.com"],
        boundaries=["no teasing"], tone="gentle",
        evidence_refs=["agent:chat:e1"], reason="operator confirmation",
    )
    assert profile is not None
    assert profile.confirmed_preferences == ("likes cats", "email me at [PII]")
    assert profile.boundaries == ("no teasing",)
    assert profile.tone == "gentle"


def test_note_requires_evidence_then_review_and_delete() -> None:
    manager, viewer_id = make_manager()
    assert manager.create_note(
        viewer_id, summary="regular viewer", evidence_refs=["fake"], reason="operator",
    ) is None
    note = manager.create_note(
        viewer_id, summary="enjoys cat stories", evidence_refs=["agent:chat:e1"],
        reason="operator observed chat",
    )
    assert note is not None and note.status is ReviewStatus.PENDING
    assert manager.review_note(note.note_id, approve=True, reason="verified") is True
    reviewed = manager.snapshot().notes[0]
    assert reviewed.status is ReviewStatus.APPROVED
    assert manager.delete_note(note.note_id, reason="viewer request") is True
    assert manager.snapshot().notes == ()

