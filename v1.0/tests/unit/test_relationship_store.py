from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from services.relationship.store import RelationshipStore


def _store() -> RelationshipStore:
    conn = sqlite3.connect(":memory:")
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "005_add_relationship_tables.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    return RelationshipStore(conn=conn)


def test_profile_insert_and_event_dedup() -> None:
    store = _store()
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    first, inserted = store.observe_profile(
        viewer_id="v_123", event_id="chat-1", occurred_at=now, expires_at=now,
    )
    second, duplicated = store.observe_profile(
        viewer_id="v_123", event_id="chat-1", occurred_at=now, expires_at=now,
    )
    assert inserted is True and duplicated is False
    assert first.interaction_count == second.interaction_count == 1


def test_profile_never_contains_raw_identity_fields() -> None:
    store = _store()
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    profile, _ = store.observe_profile(
        viewer_id="v_safe", event_id="chat-1", occurred_at=now, expires_at=now,
    )
    assert set(profile.to_dict()) == {
        "viewer_id", "interaction_count", "first_seen", "last_seen", "expires_at",
        "confirmed_preferences", "boundaries", "tone",
    }

