from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from services.operations.surface import OperationsSurface, OperationsSurfaceConfig


def _surface() -> OperationsSurface:
    return OperationsSurface(OperationsSurfaceConfig(
        max_snapshot_sections=8,
        max_commands=8,
        max_label_chars=80,
        max_payload_bytes=1024,
    ))


async def test_dashboard_reads_and_controls_only_through_operations_surface() -> None:
    surface = _surface()
    surface.register_snapshot_provider("runtime", lambda: {
        "online": True, "mode": "embedded", "controls_available": True,
    })
    surface.register_snapshot_provider("features", lambda: [{
        "id": "filter_rule", "enabled": True,
    }])
    surface.register_snapshot_provider("operations", lambda: {"available": True})
    surface.register_snapshot_provider("data_label", lambda: {
        "latest_turn": {"session_id": "session-live", "turn_id": 7},
    })
    surface.register_command("feature.toggle", lambda payload: {
        "ok": True, "status": "disabled", "feature_id": payload["feature_id"],
    })
    await surface.start()
    server = DashboardServer(
        operations_surface=surface,
        control_token="test-dashboard-control-token-123456",
    )
    with TestClient(server.app) as client:
        snapshot = client.get("/api/snapshot").json()
        assert snapshot["runtime"]["online"] is True
        assert snapshot["operator_overview"]["overall_status"] == "ready"
        assert client.get("/api/features").json()[0]["id"] == "filter_rule"
        response = client.post(
            "/api/features/filter_rule/toggle",
            headers={"X-Mai-Operator-Token": "test-dashboard-control-token-123456"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "status": "disabled",
            "feature_id": "filter_rule",
        }
        rating = client.post(
            "/api/rate",
            json={"rating": "good"},
            headers={"X-Mai-Operator-Token": "test-dashboard-control-token-123456"},
        )
        assert rating.status_code == 200
        assert rating.json()["session_id"] == "session-live"
        assert rating.json()["turn_id"] == 7
