"""Canonical FastAPI operator console over operations boundaries."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
from typing import Any
import uuid

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from orchestrator.credential_contract import validate_dashboard_control_token
from orchestrator.logger import get_logger

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES = _DASHBOARD_DIR / "templates"
_STATIC = _DASHBOARD_DIR / "static"
_OPERATOR_TOKEN_HEADER = "X-Mai-Operator-Token"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_dashboard_host(value: object) -> str:
    if not isinstance(value, str) or value not in _LOOPBACK_HOSTS:
        raise ValueError("dashboard host must be an explicit loopback address")
    return value


def _build_operator_overview(snapshot: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(snapshot.get("runtime") or {})
    operations = dict(snapshot.get("operations") or {})
    emergency = dict(snapshot.get("emergency") or {})
    incidents = dict(snapshot.get("incidents") or {})
    current = dict((snapshot.get("decisions") or {}).get("current") or {})
    goals = dict(snapshot.get("goals") or {})
    targets = dict((snapshot.get("health_supervisor") or {}).get("targets") or {})
    unhealthy = [
        name for name, value in targets.items()
        if str((value or {}).get("health", "unknown")) not in {"healthy", "unknown"}
        or bool((value or {}).get("circuit_open"))
    ]
    status, headline = "ready", "Hệ thống sẵn sàng"
    action_required, recovery_action = "Không có việc khẩn cấp.", None
    if not runtime.get("online", False):
        status, headline = "critical", "Runtime đang offline"
        action_required = "Khởi động hoặc kết nối lại runtime trước khi điều khiển Mai."
        recovery_action = "restart_runtime"
    elif emergency.get("latched", False):
        status, headline = "critical", "Emergency stop đang khóa output"
        action_required = str(emergency.get("reason") or "Xác minh an toàn trước khi resume.")
        recovery_action = "resume_emergency"
    elif int(incidents.get("unresolved") or 0) > 0:
        status, headline = "critical", "Có incident chưa xử lý"
        action_required = "Mở danh sách incident và xử lý nguyên nhân trước khi tiếp tục live."
        recovery_action = "inspect_incidents"
    elif unhealthy:
        status, headline = "warning", "Có service cần chú ý"
        action_required = "Kiểm tra recovery/circuit của: " + ", ".join(unhealthy)
        recovery_action = "inspect_health"
    elif operations.get("paused", False):
        status, headline = "warning", "Agent đang tạm dừng"
        action_required = str(operations.get("pause_reason") or "Resume khi đã sẵn sàng.")
        recovery_action = "resume_agent"
    elif current.get("hard_rejection_reason") or current.get("delivery_state") == "failed":
        status, headline = "warning", "Quyết định gần nhất cần kiểm tra"
        action_required = str(
            current.get("hard_rejection_reason") or current.get("outcome") or "delivery_failed"
        )
        recovery_action = "inspect_decision"
    return {
        "schema_version": 1, "overall_status": status, "headline": headline,
        "action_required": action_required, "recovery_action": recovery_action,
        "runtime_online": bool(runtime.get("online", False)),
        "controls_available": bool(runtime.get("controls_available", False)),
        "unresolved_incidents": int(incidents.get("unresolved") or 0),
        "unhealthy_services": unhealthy, "current_action": current.get("action"),
        "current_reason": current.get("reason"),
        "current_delivery_state": current.get("delivery_state"),
        "current_outcome": current.get("outcome"),
        "decision_id": current.get("decision_id"),
        "evidence_refs": list(current.get("evidence_refs") or []),
        "active_goal": goals.get("active"),
    }


class DashboardServer:
    """Presentation adapter; never owns or mutates a domain service directly."""

    def __init__(
        self, *, operations_surface: Any = None, snapshot_provider: Any = None,
        metrics: Any = None, data_dir: str = "logs", push_interval_s: float = 1.0,
        host: str = "127.0.0.1", port: int = 7860,
        control_token: str | None = None,
    ) -> None:
        self.operations_surface = operations_surface
        self.snapshot_provider = snapshot_provider
        self.metrics = metrics
        self._data_dir = Path(data_dir)
        self._ratings_writer: Any = None
        self._corrections_writer: Any = None
        self.push_interval_s = float(push_interval_s)
        self.host = validate_dashboard_host(host)
        self.port = int(port)
        candidate = secrets.token_urlsafe(32) if control_token is None else control_token
        self._control_token = validate_dashboard_control_token(
            candidate, source="dashboard_control_token",
        )
        self._log = get_logger("dashboard")
        self._ws_clients: set[WebSocket] = set()
        self._ws_source_modes: dict[WebSocket, str] = {}
        self._push_task: asyncio.Task[None] | None = None
        self._uvicorn_server: Any = None
        self.app = self._build_app()

    async def serve(self) -> None:
        import uvicorn
        self.start_push_loop()
        self._uvicorn_server = uvicorn.Server(uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="warning",
        ))
        try:
            await self._uvicorn_server.serve()
        finally:
            await self.stop_push_loop()
            self._uvicorn_server = None

    async def shutdown(self) -> None:
        for client in list(self._ws_clients):
            with contextlib.suppress(Exception):
                await client.close(code=1001, reason="runtime shutdown")
        self._ws_clients.clear()
        self._ws_source_modes.clear()
        await self.stop_push_loop()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    async def build_snapshot(self, source_mode: str = "auto") -> dict[str, Any]:
        if self.operations_surface is not None:
            try:
                snapshot = (await self.operations_surface.snapshot()).to_dict()
            except Exception as exc:
                snapshot = {"operations_degraded": {
                    "failed_sections": {"operations_surface": type(exc).__name__},
                }}
        elif self.snapshot_provider is not None:
            try:
                value = (
                    await self.snapshot_provider.snapshot_for(source_mode)
                    if hasattr(self.snapshot_provider, "snapshot_for")
                    else await self.snapshot_provider.snapshot()
                )
                snapshot = dict(value)
            except Exception as exc:
                snapshot = {"operations_degraded": {
                    "failed_sections": {"snapshot_provider": type(exc).__name__},
                }}
        else:
            snapshot = {}
        snapshot.setdefault("runtime", {
            "online": False, "mode": "standalone", "controls_available": False,
        })
        snapshot["operator_overview"] = _build_operator_overview(snapshot)
        if self.snapshot_provider is not None and hasattr(self.snapshot_provider, "get_metrics"):
            with contextlib.suppress(Exception):
                snapshot["dashboard_source_metrics"] = self.snapshot_provider.get_metrics()
        return snapshot

    def _record_dashboard_view(self) -> None:
        if self.metrics is not None and hasattr(self.metrics, "record_operator_dashboard_view"):
            with contextlib.suppress(Exception):
                self.metrics.record_operator_dashboard_view("v2")

    def _render_operator(self) -> str:
        path = _TEMPLATES / "operator_v2.html"
        return path.read_text(encoding="utf-8") if path.exists() else "<h1>Mai Operator Console</h1>"

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Mai Dashboard")
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
            "127.0.0.1", "localhost", "[::1]", "testserver",
        ])

        @app.middleware("http")
        async def require_operator_token(request: Request, call_next: Any) -> Any:
            if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get(_OPERATOR_TOKEN_HEADER, "")
                if not secrets.compare_digest(supplied, self._control_token):
                    return JSONResponse(
                        {"ok": False, "reason": "operator_auth_required"}, status_code=403,
                    )
            return await call_next(request)

        if _STATIC.exists():
            app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

        @app.get("/", response_class=HTMLResponse)
        @app.get("/operator", response_class=HTMLResponse)
        async def operator_dashboard() -> HTMLResponse:
            self._record_dashboard_view()
            return HTMLResponse(self._render_operator(), headers={
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            })

        @app.get("/api/snapshot")
        async def api_snapshot(source: str = "auto") -> JSONResponse:
            return JSONResponse(await self.build_snapshot(source))

        @app.get("/api/history/turns")
        async def api_history_turns(
            session_id: str | None = None, started_at: str | None = None,
            ended_at: str | None = None, kind: str | None = None,
            delivered: bool | None = None, limit: int | None = None,
        ) -> JSONResponse:
            if self.snapshot_provider is None or not hasattr(self.snapshot_provider, "query_history"):
                return JSONResponse(
                    {"ok": False, "reason": "history_source_unavailable"}, status_code=503,
                )
            return JSONResponse(await self.snapshot_provider.query_history(
                session_id=session_id, started_at=started_at, ended_at=ended_at,
                kind=kind, delivered=delivered, limit=limit,
            ))

        @app.get("/api/features")
        async def api_features() -> JSONResponse:
            return JSONResponse((await self.build_snapshot()).get("features", []))

        @app.post("/api/features/{feature_id}/toggle")
        async def api_toggle(feature_id: str) -> JSONResponse:
            return await self._command(
                "feature.toggle", f"/api/features/{feature_id}/toggle", {"feature_id": feature_id},
            )

        @app.post("/api/emergency_stop")
        async def api_emergency_stop() -> JSONResponse:
            return await self._command("emergency.trigger", "/api/emergency_stop", {
                "reason": "dashboard emergency stop",
            })

        @app.post("/api/resume")
        async def api_resume() -> JSONResponse:
            return await self._command("emergency.resume", "/api/resume", {
                "reason": "dashboard operator resume",
            })

        @app.post("/api/goals/pin")
        async def api_goal_pin(request: Request) -> JSONResponse:
            return await self._command("goal.pin", "/api/goals/pin", await _json(request))

        @app.post("/api/goals/{goal_id}/complete")
        async def api_goal_complete(goal_id: str, request: Request) -> JSONResponse:
            body = {**await _json(request), "goal_id": goal_id}
            return await self._command("goal.complete", f"/api/goals/{goal_id}/complete", body)

        @app.post("/api/goals/{goal_id}/cancel")
        async def api_goal_cancel(goal_id: str, request: Request) -> JSONResponse:
            body = {**await _json(request), "goal_id": goal_id}
            return await self._command("goal.cancel", f"/api/goals/{goal_id}/cancel", body)

        @app.post("/api/agent/pause")
        async def api_agent_pause(request: Request) -> JSONResponse:
            return await self._command("agent.pause", "/api/agent/pause", await _json(request))

        @app.post("/api/agent/resume")
        async def api_agent_resume(request: Request) -> JSONResponse:
            return await self._command("agent.resume", "/api/agent/resume", await _json(request))

        @app.get("/api/relationships")
        async def api_relationships() -> JSONResponse:
            value = (await self.build_snapshot()).get("relationships")
            if not isinstance(value, dict):
                return JSONResponse({"ok": False, "reason": "no relationship provider"}, status_code=503)
            return JSONResponse({"ok": True, **value})

        @app.post("/api/relationships/{viewer_id}/profile")
        async def api_relationship_profile(viewer_id: str, request: Request) -> JSONResponse:
            return await self._command("relationship.profile", f"/api/relationships/{viewer_id}/profile", {
                **await _json(request), "viewer_id": viewer_id,
            })

        @app.post("/api/relationships/{viewer_id}/notes")
        async def api_relationship_note(viewer_id: str, request: Request) -> JSONResponse:
            return await self._command("relationship.note.create", f"/api/relationships/{viewer_id}/notes", {
                **await _json(request), "viewer_id": viewer_id,
            })

        @app.post("/api/relationships/notes/{note_id}/review")
        async def api_relationship_note_review(note_id: str, request: Request) -> JSONResponse:
            return await self._command("relationship.note.review", f"/api/relationships/notes/{note_id}/review", {
                **await _json(request), "note_id": note_id,
            })

        @app.delete("/api/relationships/notes/{note_id}")
        async def api_relationship_note_delete(note_id: str, request: Request) -> JSONResponse:
            return await self._command("relationship.note.delete", f"/api/relationships/notes/{note_id}", {
                **await _json(request), "note_id": note_id,
            })

        @app.post("/api/relationships/narratives")
        async def api_relationship_narrative(request: Request) -> JSONResponse:
            return await self._command(
                "relationship.narrative.create", "/api/relationships/narratives", await _json(request),
            )

        @app.post("/api/relationships/narratives/{narrative_id}/resolve")
        async def api_relationship_narrative_resolve(narrative_id: str, request: Request) -> JSONResponse:
            return await self._command(
                "relationship.narrative.resolve", f"/api/relationships/narratives/{narrative_id}/resolve",
                {**await _json(request), "narrative_id": narrative_id},
            )

        @app.post("/api/relationships/{viewer_id}/running-gags")
        async def api_relationship_gag(viewer_id: str, request: Request) -> JSONResponse:
            return await self._command(
                "relationship.gag.create", f"/api/relationships/{viewer_id}/running-gags",
                {**await _json(request), "viewer_id": viewer_id},
            )

        @app.post("/api/relationships/running-gags/{gag_id}/review")
        async def api_relationship_gag_review(gag_id: str, request: Request) -> JSONResponse:
            return await self._command(
                "relationship.gag.review", f"/api/relationships/running-gags/{gag_id}/review",
                {**await _json(request), "gag_id": gag_id},
            )

        @app.delete("/api/relationships/{viewer_id}")
        async def api_relationship_delete(viewer_id: str, request: Request) -> JSONResponse:
            return await self._command("relationship.delete", f"/api/relationships/{viewer_id}", {
                **await _json(request), "viewer_id": viewer_id,
            })

        @app.get("/api/relationships/{viewer_id}/export")
        async def api_relationship_export(viewer_id: str) -> JSONResponse:
            return await self._command(
                "relationship.export", f"/api/relationships/{viewer_id}/export",
                {"viewer_id": viewer_id},
            )

        @app.get("/metrics", response_class=PlainTextResponse)
        async def metrics_endpoint() -> bytes:
            if self.operations_surface is not None:
                return self.operations_surface.prometheus_text()
            if self.metrics is not None and hasattr(self.metrics, "prometheus_text"):
                return self.metrics.prometheus_text()
            return b""

        @app.post("/api/rate")
        async def api_rate(request: Request) -> JSONResponse:
            body = await _json(request)
            rating = str(body.get("rating", "")).strip()
            if rating not in {"good", "bad", "flag"}:
                return JSONResponse({"ok": False, "reason": "rating không hợp lệ"}, status_code=400)
            identity = await self._rating_identity(body)
            if identity is None:
                return JSONResponse({"ok": False, "reason": "chưa có turn"}, status_code=400)
            session_id, turn_id = identity
            self._write_rating({
                "session_id": session_id, "turn_id": turn_id,
                "rating": rating, "ts": _now_iso(),
            })
            return JSONResponse({
                "ok": True, "session_id": session_id, "turn_id": turn_id, "rating": rating,
            })

        @app.get("/api/recent_turns")
        async def api_recent_turns(n: int = 20) -> JSONResponse:
            return JSONResponse({"turns": self._recent_turns(min(max(1, n), 100))})

        @app.post("/api/correct")
        async def api_correct(request: Request) -> JSONResponse:
            body = await _json(request)
            try:
                turn_id = int(body.get("turn_id"))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "reason": "turn_id sai"}, status_code=400)
            session_id = body.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                return JSONResponse({"ok": False, "reason": "thiếu session_id"}, status_code=400)
            session_id = session_id.strip()
            corrected = str(body.get("corrected_text", "")).strip()
            if not corrected:
                return JSONResponse({"ok": False, "reason": "corrected_text rỗng"}, status_code=400)
            original = self._original_of(session_id, turn_id)
            if original is None:
                return JSONResponse({"ok": False, "reason": "không tìm thấy turn"}, status_code=404)
            from services.data.sanitize import mask_pii
            self._write_correction({
                "session_id": session_id, "turn_id": turn_id, "original": original,
                "corrected": mask_pii(corrected) or "", "ts": _now_iso(),
            })
            return JSONResponse({"ok": True, "session_id": session_id, "turn_id": turn_id})

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            self._ws_clients.add(websocket)
            source = str(websocket.query_params.get("source") or "auto").lower()
            self._ws_source_modes[websocket] = source if source in {"auto", "live", "history"} else "auto"
            try:
                await websocket.send_json(await self.build_snapshot(self._ws_source_modes[websocket]))
                while True:
                    await websocket.receive_text()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception:
                pass
            finally:
                self._ws_clients.discard(websocket)
                self._ws_source_modes.pop(websocket, None)
        return app

    async def _command(self, name: str, path: str, payload: dict[str, Any]) -> JSONResponse:
        if self.operations_surface is not None:
            from interfaces.operations import OperationsCommand
            result = await self.operations_surface.execute(OperationsCommand(
                command_id=f"dashboard:{uuid.uuid4().hex}", name=name,
                issued_at=datetime.now(timezone.utc), payload=payload,
            ))
            return JSONResponse(dict(result.payload), status_code=result.status_code)
        if self.snapshot_provider is not None and hasattr(self.snapshot_provider, "forward_command"):
            status, value = await self.snapshot_provider.forward_command(path, payload)
            return JSONResponse(value, status_code=status)
        return JSONResponse({"ok": False, "reason": "runtime_offline"}, status_code=503)

    async def _rating_identity(self, body: dict[str, Any]) -> tuple[str, int] | None:
        turn_id, session_id = body.get("turn_id"), body.get("session_id")
        if turn_id is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                return None
            try:
                return session_id.strip(), int(turn_id)
            except (TypeError, ValueError):
                return None
        if session_id is not None or self.operations_surface is None:
            return None
        try:
            value = await self.operations_surface.snapshot_section("data_label")
            latest = value.get("latest_turn") if isinstance(value, dict) else None
            current_session = latest.get("session_id") if isinstance(latest, dict) else None
            current_turn = latest.get("turn_id") if isinstance(latest, dict) else None
            if isinstance(current_session, str) and current_session and isinstance(current_turn, int) and current_turn:
                return current_session, current_turn
        except Exception:
            return None
        return None

    def _ratings(self) -> Any:
        if self._ratings_writer is None:
            from orchestrator.logger import JsonlWriter
            self._ratings_writer = JsonlWriter(self._data_dir / "ratings.jsonl")
        return self._ratings_writer

    def _corrections(self) -> Any:
        if self._corrections_writer is None:
            from orchestrator.logger import JsonlWriter
            self._corrections_writer = JsonlWriter(self._data_dir / "corrections.jsonl")
        return self._corrections_writer

    def _write_rating(self, record: dict[str, Any]) -> None:
        try:
            self._ratings().write(record)
        except Exception as exc:
            self._log.warning("rating_write_failed", error=str(exc))

    def _write_correction(self, record: dict[str, Any]) -> None:
        try:
            self._corrections().write(record)
        except Exception as exc:
            self._log.warning("correction_write_failed", error=str(exc))

    def _recent_turns(self, n: int) -> list[dict[str, Any]]:
        return [{
            "session_id": item.get("session_id"), "turn_id": item.get("turn_id"),
            "kind": item.get("kind"), "user_text": item.get("user_text"),
            "mai_text": item.get("mai_text"),
        } for item in _tail_jsonl(self._data_dir / "turns.jsonl", n)]

    def _original_of(self, session_id: str, turn_id: int) -> str | None:
        for item in _tail_jsonl(self._data_dir / "turns.jsonl", 200):
            if item.get("session_id") == session_id and item.get("turn_id") == turn_id:
                value = item.get("mai_text")
                return value if isinstance(value, str) else None
        return None

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(self.push_interval_s)
            dead: list[WebSocket] = []
            snapshots: dict[str, dict[str, Any]] = {}
            for client in list(self._ws_clients):
                try:
                    source = self._ws_source_modes.get(client, "auto")
                    if source not in snapshots:
                        snapshots[source] = await self.build_snapshot(source)
                    await client.send_json(snapshots[source])
                except Exception:
                    dead.append(client)
            for client in dead:
                self._ws_clients.discard(client)
                self._ws_source_modes.pop(client, None)

    def start_push_loop(self) -> None:
        if self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop())

    async def stop_push_loop(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None


async def _json(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail_jsonl(path: Path, n: int) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records
    except OSError:
        return []
