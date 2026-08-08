from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def test_sanitized_regular_viewer_is_grounded_without_fake_lore() -> None:
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "relationship_sanitized.json").read_text(encoding="utf-8")
    )
    conn = sqlite3.connect(":memory:")
    for name in ("005_add_relationship_tables.sql", "006_add_relationship_positive_events.sql"):
        conn.executescript((ROOT / "migrations" / name).read_text(encoding="utf-8"))
    event_refs = {f"agent:chat:{item['event_id']}" for item in fixture["events"]}
    clock = [NOW]
    manager = RelationshipManager(
        RelationshipStore(conn=conn),
        RelationshipLimits(positive_interactions_required=3, gag_reference_cooldown_s=1800),
        clock=lambda: clock[0], evidence_exists=lambda value: value in event_refs,
    )
    profile = None
    for index, event in enumerate(fixture["events"]):
        profile = manager.observe_interaction(
            raw_viewer_id="synthetic-regular", event_id=event["event_id"],
            occurred_at=NOW + timedelta(seconds=index),
            emotion_category=event["category"],
        )
    assert profile is not None and profile.viewer_id.startswith("v_")
    first_ref = sorted(event_refs)[0]
    manager.update_profile(
        profile.viewer_id, preferences=[fixture["confirmed_preference"]], boundaries=[],
        tone="friendly", evidence_refs=[first_ref], reason="operator fixture review",
    )
    note = manager.create_note(
        profile.viewer_id, summary=fixture["approved_note"], evidence_refs=[first_ref],
        reason="operator fixture review",
    )
    assert note is not None
    manager.review_note(note.note_id, approve=True, reason="verified")
    narrative = manager.create_narrative(
        summary=fixture["narrative"], event_refs=[first_ref], reason="verified",
        viewer_id=profile.viewer_id,
    )
    assert narrative is not None
    gag = manager.create_running_gag(
        profile.viewer_id, summary=fixture["running_gag"],
        event_refs=sorted(event_refs), reason="operator proposal",
    )
    assert gag is not None
    manager.review_running_gag(gag.gag_id, approve=True, reason="verified")
    context = manager.render_context("synthetic-regular")
    assert fixture["confirmed_preference"] in context
    assert fixture["approved_note"] in context
    assert any(ref in context for ref in event_refs)
    assert "private address" not in context and "invented birthday" not in context
    assert "synthetic-regular" not in context
    # Conservative injection cooldown: the gag is absent from the immediate next render.
    assert fixture["running_gag"] not in manager.render_context("synthetic-regular")


def test_baseline_records_all_m7_grounding_checks() -> None:
    baseline = json.loads(
        (ROOT / "docs" / "baselines" / "m7_relationship_eval.json").read_text(encoding="utf-8")
    )
    assert baseline["passed"] == baseline["total"] == 5
    assert all(baseline["checks"].values())
    assert "viewer_id" not in baseline
