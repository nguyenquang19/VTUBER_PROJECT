from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from dashboard.dashboard_server import DashboardServer
from services.operations.metrics import MetricsCollector
from services.state.agent import AgentState, AgentStateLimits, AgentStateReducer
from services.state.event_ledger import EventLedger
from services.agent.goal_manager import GoalLimits, GoalManager
from interfaces.state import AgentEventKind
from orchestrator.runtime_operations_surface import bind_standard_operator_commands
from services.operations.surface import OperationsSurface, OperationsSurfaceConfig

CONTROL_TOKEN = "test-dashboard-control-token-123456"
CONTROL_HEADERS = {"X-Mai-Operator-Token": CONTROL_TOKEN}


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
    surface = OperationsSurface(OperationsSurfaceConfig(16, 24, 120, 4096), metrics=metrics)
    surface.register_snapshot_provider("goals", lambda: goals.snapshot().to_dict())
    surface.register_snapshot_provider("goal_metrics", goals.get_metrics)
    surface.register_snapshot_provider("agent", lambda: state.snapshot().to_dict())
    control = type("Control", (), {
        "record_operator_action": lambda self, *args: None,
    })()
    bind_standard_operator_commands(
        surface, feature_manager=object(), control_plane=control,
        goal_manager=goals, relationship_manager=None,
    )
    asyncio.run(surface.start())
    server = DashboardServer(
        operations_surface=surface, metrics=metrics,
        control_token=CONTROL_TOKEN,
    )
    return TestClient(server.app, headers=CONTROL_HEADERS), goals, state, metrics


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
    assert snap["goals"]["current_intention"]["goal_id"] == goals.snapshot().active.goal_id  # type: ignore[union-attr]
    assert snap["goals"]["current_intention"]["status"] == "active"
    assert snap["goal_metrics"]["goal_operator_override_total"] == 1
