from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from services.operations.standalone_snapshot import StandaloneSnapshotProvider


def test_dashboard_runs_without_runtime_and_disables_mutating_controls(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "agent": {
            "active_goal_ref": "goal:last",
            "open_threads": [{"thread_id": "thread:last", "kind": "promise"}],
            "environment_summary": {"game": "offline"},
        },
        "goals": {"active": None, "candidates": [], "suspended": []},
    }), encoding="utf-8")
    provider = StandaloneSnapshotProvider(
        snapshot_path=snapshot, audit_path=tmp_path / "audit.jsonl",
    )
    client = TestClient(DashboardServer(snapshot_provider=provider).app)

    value = client.get("/api/snapshot").json()
    assert value["runtime"]["online"] is False
    assert value["agent"]["active_goal_ref"] == "goal:last"
    pause = client.post("/api/agent/pause", json={"reason": "test"})
    pin = client.post("/api/goals/pin", json={
        "reason": "x", "success_condition": "y",
    })
    assert pause.status_code == 503 and pause.json()["reason"] == "runtime_offline"
    assert pin.status_code == 503


def test_agent_tab_contains_runtime_thread_queue_and_audit_panels() -> None:
    html = TestClient(DashboardServer().app).get("/").text
    for element_id in (
        "agent-runtime", "btn-agent-pause", "btn-agent-resume", "agent-threads",
        "agent-environment", "agent-action-queue", "agent-audit",
        "agent-incidents", "agent-incident-count",
    ):
        assert f'id="{element_id}"' in html
