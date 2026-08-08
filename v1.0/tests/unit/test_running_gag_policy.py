from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


def _manager(clock: list[datetime]) -> RelationshipManager:
    root = Path(__file__).resolve().parents[2]
    conn = sqlite3.connect(":memory:")
    conn.executescript((root / "migrations" / "005_add_relationship_tables.sql").read_text(encoding="utf-8"))
    conn.executescript((root / "migrations" / "006_add_relationship_positive_events.sql").read_text(encoding="utf-8"))
    return RelationshipManager(
        RelationshipStore(conn=conn),
        RelationshipLimits(
            positive_interactions_required=3, gag_reference_cooldown_s=1800,
        ),
        clock=lambda: clock[0],
    )


def _positive_interactions(manager: RelationshipManager, now: datetime) -> str:
    viewer_id = ""
    for index in range(1, 4):
        profile = manager.observe_interaction(
            raw_viewer_id="raw", event_id=f"e{index}",
            occurred_at=now + timedelta(seconds=index),
            emotion_category="chat_compliment",
        )
        assert profile is not None
        viewer_id = profile.viewer_id
    return viewer_id


def test_running_gag_needs_repeated_positive_evidence_and_operator_review() -> None:
    clock = [datetime(2026, 8, 8, tzinfo=timezone.utc)]
    manager = _manager(clock)
    first = manager.observe_interaction(
        raw_viewer_id="raw", event_id="e1", occurred_at=clock[0],
        emotion_category="chat_compliment",
    )
    assert first is not None
    assert manager.create_running_gag(
        first.viewer_id, summary="cat greeting", event_refs=["agent:chat:e1"],
        reason="operator",
    ) is None

    manager = _manager(clock)
    viewer_id = _positive_interactions(manager, clock[0])
    refs = ["agent:chat:e1", "agent:chat:e2", "agent:chat:e3"]
    gag = manager.create_running_gag(
        viewer_id, summary="the cat greeting", event_refs=refs, reason="operator proposal",
    )
    assert gag is not None
    assert "cat greeting" not in manager.render_context("raw")
    assert manager.review_running_gag(gag.gag_id, approve=True, reason="operator approved")
    assert "cat greeting" in manager.render_context("raw")


def test_running_gag_reference_is_rate_limited() -> None:
    clock = [datetime(2026, 8, 8, tzinfo=timezone.utc)]
    manager = _manager(clock)
    viewer_id = _positive_interactions(manager, clock[0])
    gag = manager.create_running_gag(
        viewer_id, summary="the cat greeting",
        event_refs=["agent:chat:e1", "agent:chat:e2", "agent:chat:e3"],
        reason="operator",
    )
    assert gag is not None
    manager.review_running_gag(gag.gag_id, approve=True, reason="approved")
    assert "cat greeting" in manager.render_context("raw")
    assert "cat greeting" not in manager.render_context("raw")
    clock[0] += timedelta(seconds=1801)
    assert "cat greeting" in manager.render_context("raw")


def test_non_positive_categories_do_not_qualify() -> None:
    clock = [datetime(2026, 8, 8, tzinfo=timezone.utc)]
    manager = _manager(clock)
    viewer_id = ""
    for index in range(3):
        profile = manager.observe_interaction(
            raw_viewer_id="raw", event_id=f"e{index}", occurred_at=clock[0],
            emotion_category="chat_neutral",
        )
        assert profile is not None
        viewer_id = profile.viewer_id
    assert manager.create_running_gag(
        viewer_id, summary="fake gag",
        event_refs=["agent:chat:e0", "agent:chat:e1", "agent:chat:e2"],
        reason="operator",
    ) is None
