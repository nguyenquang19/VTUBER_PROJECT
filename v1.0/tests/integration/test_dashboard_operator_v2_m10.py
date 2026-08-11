from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer, _build_operator_overview
from orchestrator.features import FeatureStatus
from orchestrator.metrics_collector import MetricsCollector
from interfaces.animation import MoodState


class V2Features:
    async def get_status(self, feature_id: str) -> FeatureStatus:
        assert feature_id == "operator_dashboard_v2"
        return FeatureStatus.ENABLED


def test_legacy_and_operator_routes_run_side_by_side() -> None:
    client = TestClient(DashboardServer().app)
    root = client.get("/")
    legacy = client.get("/legacy")
    operator = client.get("/operator")
    assert root.status_code == legacy.status_code == operator.status_code == 200
    assert "Mai — Dashboard" in root.text
    assert "Mai — Dashboard" in legacy.text
    assert "Mai Operator Console" in operator.text


def test_feature_flag_selects_v2_as_default_without_removing_legacy() -> None:
    client = TestClient(DashboardServer(feature_manager=V2Features()).app)
    assert "Mai Operator Console" in client.get("/").text
    assert "Mai — Dashboard" in client.get("/legacy").text


def test_operator_dashboard_has_exactly_five_primary_sections() -> None:
    html = TestClient(DashboardServer().app).get("/operator").text
    assert html.count('data-section="') == 5
    for section, label in (
        ("overview", "Tổng quan"),
        ("brain", "Brain"),
        ("conversation", "Hội thoại"),
        ("system", "Hệ thống"),
        ("evaluation", "Đánh giá"),
    ):
        assert f'data-section="{section}"' in html
        assert f'id="section-{section}"' in html
        assert label in html


def test_operator_assets_are_local_and_served() -> None:
    client = TestClient(DashboardServer().app)
    assert client.get("/static/operator_v2.css").status_code == 200
    js = client.get("/static/operator_v2.js")
    assert js.status_code == 200
    assert "https://" not in js.text
    assert ".innerHTML" not in js.text
    assert "mood_pos" in js.text
    assert "mood-column-fill" in js.text
    assert "parse_rate_percent" in js.text
    assert "parse_ok_rate_percent" not in js.text
    assert "thread.status" in js.text
    assert "thread.next_move" in js.text
    assert "thread.open_questions" in js.text


def test_dashboard_views_are_observable() -> None:
    metrics = MetricsCollector()
    client = TestClient(DashboardServer(metrics=metrics).app)
    client.get("/")
    client.get("/operator")
    client.get("/legacy")
    assert metrics.operator_dashboard_view_snapshot() == {"legacy": 2, "v2": 1}
    prometheus = metrics.prometheus_text()
    assert b'mai_operator_dashboard_views_total{version="v2"} 1.0' in prometheus


def test_overview_prioritizes_offline_emergency_incident_and_failed_decision() -> None:
    offline = _build_operator_overview({"runtime": {"online": False}})
    assert offline["overall_status"] == "critical"
    assert offline["recovery_action"] == "restart_runtime"

    emergency = _build_operator_overview({
        "runtime": {"online": True}, "emergency": {"latched": True, "reason": "operator"},
    })
    assert emergency["recovery_action"] == "resume_emergency"

    incident = _build_operator_overview({
        "runtime": {"online": True}, "incidents": {"unresolved": 2},
    })
    assert incident["unresolved_incidents"] == 2
    assert incident["recovery_action"] == "inspect_incidents"

    failed = _build_operator_overview({
        "runtime": {"online": True},
        "decisions": {"current": {
            "decision_id": "dec-1", "action": "read_chat", "reason": "top_single",
            "delivery_state": "failed", "outcome": "released", "evidence_refs": ["event-1"],
        }},
    })
    assert failed["overall_status"] == "warning"
    assert failed["recovery_action"] == "inspect_decision"
    assert failed["current_reason"] == "top_single"
    assert failed["evidence_refs"] == ["event-1"]


def test_ready_overview_exposes_active_goal_and_direct_decision_view() -> None:
    goal = {"id": "goal-1", "kind": "operator_pinned"}
    value = _build_operator_overview({
        "runtime": {"online": True, "controls_available": True},
        "goals": {"active": goal},
        "decisions": {"current": {
            "decision_id": "dec-2", "action": "self_talk", "reason": "dead_air",
            "delivery_state": "delivered", "outcome": "committed", "evidence_refs": [],
        }},
        "incidents": {"unresolved": 0},
    })
    assert value["overall_status"] == "ready"
    assert value["active_goal"] == goal
    assert value["current_action"] == "self_talk"
    assert value["current_reason"] == "dead_air"


def test_standalone_operator_page_keeps_mutations_unavailable() -> None:
    client = TestClient(DashboardServer().app)
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["operator_overview"]["controls_available"] is False
    assert client.post("/api/agent/pause", json={"reason": "test"}).status_code == 503
    assert client.post("/api/goals/pin", json={
        "reason": "x", "success_condition": "y",
    }).status_code == 503


def test_snapshot_exposes_float_mood_ticks_and_thought_state() -> None:
    class Emotion:
        def snapshot(self):
            return {
                "mood_pos": {"vui": 5.125, "buon": 4.875},
                "mood_target": {"vui": 8.0, "buon": 2.0},
                "current_mood": MoodState(vui=5, buon=5).model_dump(),
            }

        def get_metrics(self):
            return {"mood_ticks": 42}

    class ThoughtEngine:
        def snapshot(self):
            return {
                "active_thought_id": "thought-1",
                "cause": "silence",
                "intention": "notice one detail",
                "stage": "open",
                "pending_plan_id": None,
                "ledger": [],
            }

    value = TestClient(DashboardServer(
        emotion=Emotion(), self_talk_planner=ThoughtEngine(),
    ).app).get("/api/snapshot").json()
    assert value["mood"]["mood_pos"]["vui"] == 5.125
    assert value["mood"]["ticks"] == 42
    assert value["mood"]["sampled_at"]
    assert value["thought_engine"]["cause"] == "silence"
