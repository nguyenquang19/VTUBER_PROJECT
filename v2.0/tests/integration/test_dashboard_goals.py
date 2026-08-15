from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from dashboard.dashboard_server import DashboardServer
from orchestrator.metrics_collector import MetricsCollector
from services.agent.agent_state import AgentState, AgentStateLimits, AgentStateReducer
from services.agent.event_ledger import EventLedger
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.types import AgentEventKind


def _stack() -> tuple[TestClient, GoalManager, AgentState, MetricsCollector]:
    now = datetime(2026, 8, 8, 11, 0, tzinfo=timezone.utc)
    clock = lambda: now
    metrics = MetricsCollector(registry=CollectorRegistry())
    state = AgentState(
        AgentStateReducer(AgentStateLimits(32, 600, 4, 300, 240)),
        EventLedger(32, 600, 600, clock=clock, metrics=metrics),
        clock=clock,
    )
    goals = GoalManager(
        GoalLimits(8, 4, 16, 240, 90, 3600), clock=clock, metrics=metrics,
        on_active_changed=state.set_active_goal_ref, audit_sink=state.record,
    )
    server = DashboardServer(agent_state=state, goal_manager=goals, metrics=metrics)
    return TestClient(server.app), goals, state, metrics


def test_dashboard_can_only_pin_operator_goal_and_audits_it() -> None:
    client, goals, state, _metrics = _stack()
    response = client.post("/api/goals/pin", json={
        "reason": "Kết thúc phần cà phê",
        "success_condition": "operator complete",
    })
    assert response.status_code == 200
    goal = response.json()["goal"]
    assert goal["kind"] == "operator_pinned"
    assert goal["source"] == "operator"
    assert goals.snapshot().active.goal_id == goal["id"]  # type: ignore[union-attr]
    assert state.snapshot().active_goal_ref == goal["id"]
    audits = [e for e in state.snapshot().recent_events if e.kind is AgentEventKind.GOAL_AUDIT]
    assert audits[-1].payload["action"] == "pin"


def test_dashboard_complete_and_cancel_any_goal_with_audit() -> None:
    client, goals, state, _metrics = _stack()
    first = client.post("/api/goals/pin", json={
        "reason": "goal one", "success_condition": "done",
    }).json()["goal"]["id"]
    assert client.post(f"/api/goals/{first}/complete", json={"reason": "done live"}).status_code == 200
    second = client.post("/api/goals/pin", json={
        "reason": "goal two", "success_condition": "done",
    }).json()["goal"]["id"]
    assert client.post(f"/api/goals/{second}/cancel", json={"reason": "changed plan"}).status_code == 200
    actions = [
        e.payload["action"] for e in state.snapshot().recent_events
        if e.kind is AgentEventKind.GOAL_AUDIT
    ]
    assert actions == ["pin", "complete", "pin", "cancel"]
    assert goals.snapshot().active is None


def test_invalid_pin_and_unknown_goal_are_rejected() -> None:
    client, _goals, _state, _metrics = _stack()
    assert client.post("/api/goals/pin", json={"reason": "", "success_condition": ""}).status_code == 400
    assert client.post("/api/goals/nope/cancel", json={}).status_code == 404


def test_snapshot_is_detached_and_exposes_operator_metric() -> None:
    client, goals, _state, _metrics = _stack()
    client.post("/api/goals/pin", json={"reason": "grounded", "success_condition": "done"})
    snap = client.get("/api/snapshot").json()
    snap["goals"]["active"]["reason"] = "fake"
    assert goals.snapshot().active.reason == "grounded"  # type: ignore[union-attr]
    assert snap["goal_metrics"]["goal_operator_override_total"] == 1
