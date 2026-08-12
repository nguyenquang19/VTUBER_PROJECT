from __future__ import annotations

from dashboard.dashboard_server import DashboardServer


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.code = None

    async def close(self, code: int, reason: str) -> None:
        self.closed = True
        self.code = code


class FakeUvicorn:
    should_exit = False


async def test_dashboard_shutdown_closes_websockets_and_requests_clean_exit() -> None:
    server = DashboardServer()
    websocket = FakeWebSocket()
    uvicorn = FakeUvicorn()
    server._ws_clients.add(websocket)  # noqa: SLF001
    server._uvicorn_server = uvicorn  # noqa: SLF001

    await server.shutdown()

    assert websocket.closed is True
    assert websocket.code == 1001
    assert server._ws_clients == set()  # noqa: SLF001
    assert uvicorn.should_exit is True
