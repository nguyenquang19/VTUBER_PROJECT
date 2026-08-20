"""Integration test dashboard (ARCHITECTURE 6, DoD Phase 0).

DoD kiểm ở đây:
- Dashboard mở ở localhost, toggle giả bật/tắt được
- Metric NVIDIA có trong snapshot (không phát sinh số giả)
- Emergency stop → PAUSED từ mọi state
- Resume → IDLE
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from orchestrator.config_loader import ConfigLoader
from orchestrator.emergency_stop import EmergencyStop
from orchestrator.event_bus import EventBus
from orchestrator.features import FeatureManager
from orchestrator.metrics_collector import MetricsCollector
from orchestrator.state_machine import ConversationState, ConversationStateMachine
from orchestrator.trigger_manager import TriggerManager
from prometheus_client import CollectorRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_TOKEN = "test-dashboard-control-token-123456"
CONTROL_HEADERS = {"X-Mai-Operator-Token": CONTROL_TOKEN}


def build_server() -> DashboardServer:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    bus = EventBus.from_config(loader)
    sm = ConversationStateMachine.from_config(loader, event_bus=bus, auto_cooldown=False)
    features = FeatureManager.from_config(loader)
    triggers = TriggerManager.from_config(loader, event_bus=bus)
    metrics = MetricsCollector(registry=CollectorRegistry())
    emergency = EmergencyStop(callback=sm.emergency_stop)
    return DashboardServer(
        feature_manager=features,
        state_machine=sm,
        trigger_manager=triggers,
        metrics=metrics,
        emergency_stop=emergency,
        control_token=CONTROL_TOKEN,
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_server().app, headers=CONTROL_HEADERS)


class TestServesUI:
    def test_index_served(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "Mai" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert CONTROL_TOKEN not in r.text

    def test_static_css_served(self, client: TestClient) -> None:
        r = client.get("/static/dashboard.css")
        assert r.status_code == 200

    def test_static_js_served(self, client: TestClient) -> None:
        r = client.get("/static/dashboard.js")
        assert r.status_code == 200


class TestSnapshot:
    def test_snapshot_has_all_sections(self, client: TestClient) -> None:
        snap = client.get("/api/snapshot").json()
        assert "state" in snap
        assert "metrics" in snap
        assert "triggers" in snap
        assert "features" in snap
        assert "vram" in snap

    def test_metrics_present(self, client: TestClient) -> None:
        """GPU/VRAM fields always expose availability instead of fake values."""
        m = client.get("/api/snapshot").json()["metrics"]
        assert "gpu_util_percent" in m
        assert "vram_mb" in m
        assert "gpu_metrics_available" in m
        assert m["source"] == "nvidia-smi"

    def test_prometheus_endpoint(self, client: TestClient) -> None:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "mai_" in r.text


class TestToggle:
    """DoD: toggle giả bật/tắt được."""

    def test_toggle_enable_then_disable(self, client: TestClient) -> None:
        # filter_ai default OFF → enable → disable
        r1 = client.post("/api/features/filter_ai/toggle").json()
        assert r1["ok"] is True
        assert r1["status"] == "enabled"
        r2 = client.post("/api/features/filter_ai/toggle").json()
        assert r2["ok"] is True
        assert r2["status"] == "disabled"

    def test_toggle_reflected_in_snapshot(self, client: TestClient) -> None:
        client.post("/api/features/filter_ai/toggle")
        feats = {f["id"]: f for f in client.get("/api/snapshot").json()["features"]}
        assert feats["filter_ai"]["enabled"] is True

    def test_toggle_unknown_feature_404(self, client: TestClient) -> None:
        r = client.post("/api/features/nonexistent/toggle")
        assert r.status_code == 404

    def test_toggle_core_feature_rejected(self, client: TestClient) -> None:
        # llm_main là core → không toggle được
        r = client.post("/api/features/llm_main/toggle")
        assert r.status_code == 400
        assert r.json()["ok"] is False

    def test_toggle_dependency_blocked(self, client: TestClient) -> None:
        # animation_micro cần animation_smooth; nếu smooth đang ON thì micro enable OK.
        # Ngược lại: memory_hierarchical cần memory_semantic (OFF) → enable fail.
        r = client.post("/api/features/memory_hierarchical/toggle").json()
        assert r["ok"] is False
        assert "dependency" in r["reason"]


class TestEmergencyStop:
    """DoD: Emergency stop → PAUSED từ mọi state; Resume → IDLE."""

    def test_emergency_from_idle(self, client: TestClient) -> None:
        r = client.post("/api/emergency_stop").json()
        assert r["ok"] is True
        assert r["state"] == "PAUSED"

    def test_resume_from_paused(self, client: TestClient) -> None:
        client.post("/api/emergency_stop")
        r = client.post("/api/resume").json()
        assert r["ok"] is True
        assert r["state"] == "IDLE"

    def test_resume_when_not_paused_fails(self, client: TestClient) -> None:
        r = client.post("/api/resume")
        assert r.status_code == 400

    def test_emergency_reflected_in_snapshot(self, client: TestClient) -> None:
        client.post("/api/emergency_stop")
        assert client.get("/api/snapshot").json()["state"]["current"] == "PAUSED"


class TestWebSocket:
    def test_ws_sends_initial_snapshot(self, client: TestClient) -> None:
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            assert "state" in data
            assert "metrics" in data

    def test_ws_reflects_state_after_estop(self, client: TestClient) -> None:
        client.post("/api/emergency_stop")
        with client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            assert data["state"]["current"] == "PAUSED"


class TestDegradedServer:
    """Server phải chạy kể cả khi thiếu component (fail-safe)."""

    def test_empty_server_serves_index(self) -> None:
        client = TestClient(DashboardServer().app)
        assert client.get("/").status_code == 200

    def test_empty_server_snapshot_ok(self) -> None:
        client = TestClient(DashboardServer().app)
        assert client.get("/api/snapshot").status_code == 200

    def test_toggle_without_manager_503(self) -> None:
        client = TestClient(
            DashboardServer(control_token=CONTROL_TOKEN).app,
            headers=CONTROL_HEADERS,
        )
        assert client.post("/api/features/x/toggle").status_code == 503


class TestDashboardSecurity:
    def test_mutation_requires_exact_operator_token(self) -> None:
        server = DashboardServer(control_token=CONTROL_TOKEN)
        client = TestClient(server.app)
        assert client.post("/api/emergency_stop").status_code == 403
        assert client.post(
            "/api/emergency_stop",
            headers={"X-Mai-Operator-Token": "wrong-dashboard-control-token"},
        ).status_code == 403
        assert client.post(
            "/api/emergency_stop", headers=CONTROL_HEADERS,
        ).status_code == 503

    def test_non_loopback_bind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="loopback"):
            DashboardServer(host="0.0.0.0", control_token=CONTROL_TOKEN)

    def test_untrusted_host_header_is_rejected(self) -> None:
        client = TestClient(
            DashboardServer(control_token=CONTROL_TOKEN).app,
            base_url="http://attacker.example",
        )
        assert client.get("/api/snapshot").status_code == 400
