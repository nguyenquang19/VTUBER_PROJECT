from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.data.sanitize import hash_viewer_id
from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


def _manager(now: datetime) -> RelationshipManager:
    conn = sqlite3.connect(":memory:")
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "005_add_relationship_tables.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    return RelationshipManager(
        RelationshipStore(conn=conn), RelationshipLimits(profile_ttl_days=30), clock=lambda: now,
    )


def test_observe_hashes_identity_and_deduplicates_event() -> None:
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    manager = _manager(now)
    first = manager.observe_interaction(raw_viewer_id="raw-platform-id", event_id="e1", occurred_at=now)
    second = manager.observe_interaction(raw_viewer_id="raw-platform-id", event_id="e1", occurred_at=now)
    assert first is not None and second is not None
    assert first.viewer_id == hash_viewer_id("raw-platform-id")
    assert first.viewer_id != "raw-platform-id"
    assert second.interaction_count == 1
    assert manager.get_metrics()["relationship_duplicates_total"] == 1


def test_expired_profile_is_not_returned_or_snapshotted() -> None:
    observed = datetime(2026, 7, 1, tzinfo=timezone.utc)
    manager = _manager(observed + timedelta(days=31))
    profile = manager.observe_interaction(
        raw_viewer_id="raw", event_id="e1", occurred_at=observed,
    )
    assert profile is not None
    assert manager.get_profile(profile.viewer_id) is None
    assert manager.snapshot().profiles == ()


def test_missing_identity_is_fail_safe() -> None:
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    manager = _manager(now)
    assert manager.observe_interaction(raw_viewer_id=None, event_id="e1", occurred_at=now) is None

