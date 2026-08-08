from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from services.relationship.manager import RelationshipLimits, RelationshipManager
from services.relationship.store import RelationshipStore


def _manager() -> tuple[RelationshipManager, str]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "005_add_relationship_tables.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    manager = RelationshipManager(
        RelationshipStore(conn=conn), RelationshipLimits(),
        clock=lambda: datetime(2026, 8, 8, tzinfo=timezone.utc),
        evidence_exists=lambda value: value == "agent:chat:e1",
    )
    profile = manager.observe_interaction(
        raw_viewer_id="raw", event_id="e1",
        occurred_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    assert profile is not None
    return manager, profile.viewer_id


def test_dashboard_profile_note_review_delete_flow() -> None:
    manager, viewer_id = _manager()
    client = TestClient(DashboardServer(relationship_manager=manager).app)
    profile = client.post(f"/api/relationships/{viewer_id}/profile", json={
        "preferences": ["cats"], "boundaries": ["no roast"], "tone": "gentle",
        "evidence_refs": ["agent:chat:e1"], "reason": "operator",
    })
    assert profile.status_code == 200 and profile.json()["ok"] is True
    created = client.post(f"/api/relationships/{viewer_id}/notes", json={
        "summary": "likes cat stories", "evidence_refs": ["agent:chat:e1"],
        "reason": "operator",
    })
    note_id = created.json()["note"]["note_id"]
    reviewed = client.post(f"/api/relationships/notes/{note_id}/review", json={
        "approve": True, "reason": "verified",
    })
    assert reviewed.json()["ok"] is True
    snapshot = client.get("/api/relationships").json()
    assert snapshot["profiles"][0]["viewer_id"].startswith("v_")
    assert snapshot["notes"][0]["status"] == "approved"
    deleted = client.request(
        "DELETE", f"/api/relationships/notes/{note_id}", json={"reason": "viewer request"},
    )
    assert deleted.json()["ok"] is True


def test_dashboard_rejects_note_with_invented_evidence() -> None:
    manager, viewer_id = _manager()
    client = TestClient(DashboardServer(relationship_manager=manager).app)
    response = client.post(f"/api/relationships/{viewer_id}/notes", json={
        "summary": "invented lore", "evidence_refs": ["fake"], "reason": "operator",
    })
    assert response.status_code == 400
