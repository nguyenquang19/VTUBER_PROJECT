"""Dashboard server: FastAPI + WebSocket (ARCHITECTURE 6, Phase 0 task 11).

Tab (Phase 0): toggle giả, metric giả, state machine, triggers.
Frontend: HTML + Vanilla JS + Chart.js (6.1). Alpine.js để Phase 6.

Dependency-injected (FeatureManager, state machine, trigger manager, metrics,
emergency stop) để test bằng FastAPI TestClient không cần chạy thật.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.logger import get_logger

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES = _DASHBOARD_DIR / "templates"
_STATIC = _DASHBOARD_DIR / "static"


class DashboardServer:
    def __init__(
        self,
        feature_manager: Any = None,
        state_machine: Any = None,
        trigger_manager: Any = None,
        metrics: Any = None,
        emergency_stop: Any = None,
        health_monitor: Any = None,
        push_interval_s: float = 1.0,
    ) -> None:
        self.features = feature_manager
        self.sm = state_machine
        self.triggers = trigger_manager
        self.metrics = metrics
        self.emergency = emergency_stop
        self.health = health_monitor
        self.push_interval_s = push_interval_s
        self._log = get_logger("dashboard")
        self._ws_clients: set[WebSocket] = set()
        self._push_task: asyncio.Task[None] | None = None
        self.app = self._build_app()

    # ---------- snapshot ----------

    async def build_snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {}

        if self.sm is not None:
            snap["state"] = {
                "current": self.sm.state,
                "time_in_state_ms": self.sm.time_in_state_ms(),
                "last_turn_interrupted": self.sm.last_turn_interrupted,
                "history": [h.to_log_dict() for h in self.sm.history(limit=10)],
            }

        if self.metrics is not None:
            snap["metrics"] = self.metrics.tick_fake_metrics()

        if self.triggers is not None:
            stats = await self.triggers.get_queue_stats()
            snap["triggers"] = stats.model_dump()

        if self.features is not None:
            feats = await self.features.list_features()
            snap["features"] = [
                {
                    "id": f.id,
                    "status": f.current_status.value,
                    "enabled": f.is_enabled,
                    "category": f.category,
                    "vram_cost_mb": f.vram_cost_mb,
                    "is_core": self.features.is_core(f.id),
                }
                for f in feats
            ]
            snap["vram"] = {
                "used_mb": self.features.used_vram_mb(),
                "budget_mb": self.features._vram_budget_mb,
            }

        if self.health is not None:
            snap["health"] = self.health.snapshot()
        return snap

    # ---------- app ----------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Mai Dashboard")

        if _STATIC.exists():
            app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            index_file = _TEMPLATES / "index.html"
            if index_file.exists():
                return index_file.read_text(encoding="utf-8")
            return "<h1>Mai Dashboard</h1><p>template chưa có</p>"

        @app.get("/api/snapshot")
        async def api_snapshot() -> JSONResponse:
            return JSONResponse(await self.build_snapshot())

        @app.get("/api/features")
        async def api_features() -> JSONResponse:
            snap = await self.build_snapshot()
            return JSONResponse(snap.get("features", []))

        @app.post("/api/features/{feature_id}/toggle")
        async def api_toggle(feature_id: str) -> JSONResponse:
            if self.features is None:
                return JSONResponse({"ok": False, "reason": "no feature manager"}, status_code=503)
            from orchestrator.features import CoreFeatureError, FeatureStatus

            # Core feature không nằm trong registry — chặn trước khi get_status raise
            if self.features.is_core(feature_id):
                return JSONResponse(
                    {"ok": False, "reason": f"{feature_id} là core feature, không toggle được"},
                    status_code=400,
                )
            try:
                status = await self.features.get_status(feature_id)
            except KeyError:
                return JSONResponse({"ok": False, "reason": "unknown feature"}, status_code=404)

            enabled = status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
            try:
                if enabled:
                    result = await self.features.disable(feature_id, user="dashboard")
                else:
                    result = await self.features.enable(feature_id, user="dashboard")
            except CoreFeatureError as e:
                return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)
            return JSONResponse(
                {"ok": result.ok, "status": result.status.value, "reason": result.reason}
            )

        @app.post("/api/emergency_stop")
        async def api_emergency_stop() -> JSONResponse:
            if self.emergency is not None:
                await self.emergency.trigger()
            elif self.sm is not None:
                await self.sm.emergency_stop()
            else:
                return JSONResponse({"ok": False, "reason": "no handler"}, status_code=503)
            return JSONResponse({"ok": True, "state": self.sm.state if self.sm else None})

        @app.post("/api/resume")
        async def api_resume() -> JSONResponse:
            if self.sm is None:
                return JSONResponse({"ok": False, "reason": "no state machine"}, status_code=503)
            from transitions.core import MachineError

            try:
                await self.sm.resume()
            except MachineError as e:
                return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)
            return JSONResponse({"ok": True, "state": self.sm.state})

        @app.get("/metrics", response_class=PlainTextResponse)
        async def metrics_endpoint() -> bytes:
            if self.metrics is None:
                return b""
            return self.metrics.prometheus_text()

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            self._ws_clients.add(websocket)
            try:
                await websocket.send_json(await self.build_snapshot())
                while True:
                    # giữ kết nối; client không cần gửi gì, nhưng đọc để phát hiện disconnect
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                self._ws_clients.discard(websocket)

        return app

    # ---------- push loop ----------

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(self.push_interval_s)
            if not self._ws_clients:
                continue
            snapshot = await self.build_snapshot()
            dead: list[WebSocket] = []
            for client in list(self._ws_clients):
                try:
                    await client.send_json(snapshot)
                except Exception:
                    dead.append(client)
            for d in dead:
                self._ws_clients.discard(d)

    def start_push_loop(self) -> None:
        if self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop())

    async def stop_push_loop(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None
