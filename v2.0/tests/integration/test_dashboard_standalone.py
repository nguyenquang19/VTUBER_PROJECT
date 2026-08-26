from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from services.operations.standalone_snapshot import StandaloneSnapshotProvider
from services.operations.dashboard_data_source import DashboardDataSource

CONTROL_TOKEN = "test-dashboard-control-token-123456"
CONTROL_HEADERS = {"X-Mai-Operator-Token": CONTROL_TOKEN}


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
    client = TestClient(
        DashboardServer(
            snapshot_provider=provider, control_token=CONTROL_TOKEN,
        ).app,
        headers=CONTROL_HEADERS,
    )

    assert "Mai Operator Console" in client.get("/").text

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
        "overview-runtime", "operator-pause", "operator-resume", "overview-threads",
        "conversation-environment", "overview-actions", "overview-incident-list",
    ):
        assert f'id="{element_id}"' in html


def test_independent_dashboard_exposes_source_history_and_proxy(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    turns = tmp_path / "turns.jsonl"
    turns.write_text(json.dumps({
        "session_id": "session-1", "request_id": "request-1", "turn_id": 1,
        "kind": "chat", "timestamp": "2026-08-13T12:00:00+00:00",
    }), encoding="utf-8")
    source = DashboardDataSource(
        offline_provider=StandaloneSnapshotProvider(
            snapshot_path=snapshot, audit_path=tmp_path / "audit.jsonl",
        ),
        live_base_url="http://127.0.0.1:7860",
        turns_path=turns,
        delivery_path=tmp_path / "delivery.jsonl",
        request_timeout_s=0.1, max_files=2, max_records=20,
        default_limit=10, max_limit=20,
        control_token=CONTROL_TOKEN,
    )
    monkeypatch.setattr(source, "_request_json", lambda method, path, payload: (
        {"runtime": {"online": True, "controls_available": True}}
        if method == "GET" else {"ok": True, "proxied": path}
    ))
    client = TestClient(
        DashboardServer(
            snapshot_provider=source, control_token=CONTROL_TOKEN,
        ).app,
        headers=CONTROL_HEADERS,
    )

    live = client.get("/api/snapshot?source=live").json()
    history = client.get("/api/history/turns?session_id=session-1&limit=5").json()
    pause = client.post("/api/agent/pause", json={"reason": "operator"})

    assert live["dashboard_source"]["actual"] == "live"
    assert history["total_matched"] == 1
    assert pause.status_code == 200
    assert pause.json() == {"ok": True, "proxied": "/api/agent/pause"}
