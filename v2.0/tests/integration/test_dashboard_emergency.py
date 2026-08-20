from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer

CONTROL_TOKEN = "test-dashboard-control-token-123456"
CONTROL_HEADERS = {"X-Mai-Operator-Token": CONTROL_TOKEN}


class FakeEmergencyController:
    def __init__(self) -> None:
        self.latched = False

    async def trigger(self, reason: str) -> bool:
        assert reason
        self.latched = True
        return True

    async def resume(self, reason: str) -> bool:
        assert reason
        self.latched = False
        return True

    def snapshot(self) -> dict:
        return {"available": True, "latched": self.latched}


def test_dashboard_emergency_routes_use_fail_closed_controller() -> None:
    controller = FakeEmergencyController()
    client = TestClient(
        DashboardServer(
            emergency_controller=controller, control_token=CONTROL_TOKEN,
        ).app,
        headers=CONTROL_HEADERS,
    )

    stopped = client.post("/api/emergency_stop")
    snapshot = client.get("/api/snapshot")
    resumed = client.post("/api/resume")

    assert stopped.json() == {"ok": True, "emergency": {"available": True, "latched": True}}
    assert snapshot.json()["emergency"]["latched"] is True
    assert resumed.json()["emergency"]["latched"] is False
