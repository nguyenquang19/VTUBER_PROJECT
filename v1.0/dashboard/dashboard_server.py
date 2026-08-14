"""Dashboard server: FastAPI + WebSocket operator surface.

System GPU/VRAM values come from bounded nvidia-smi sampling.
Frontend: HTML + Vanilla JS + Chart.js (6.1). Alpine.js để Phase 6.

Dependency-injected (FeatureManager, state machine, trigger manager, metrics,
emergency stop) để test bằng FastAPI TestClient không cần chạy thật.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.logger import get_logger

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES = _DASHBOARD_DIR / "templates"
_STATIC = _DASHBOARD_DIR / "static"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _build_operator_overview(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build one action-first view so the browser never reconstructs Brain state."""
    runtime = dict(snapshot.get("runtime") or {})
    operations = dict(snapshot.get("operations") or {})
    emergency = dict(snapshot.get("emergency") or {})
    incidents = dict(snapshot.get("incidents") or {})
    decisions = dict(snapshot.get("decisions") or {})
    current = dict(decisions.get("current") or {})
    goals = dict(snapshot.get("goals") or {})
    health = dict(snapshot.get("health_supervisor") or {})
    targets = dict(health.get("targets") or {})
    unhealthy = [
        service_id for service_id, value in targets.items()
        if str((value or {}).get("health", "unknown")) not in {"healthy", "unknown"}
        or bool((value or {}).get("circuit_open"))
    ]

    status = "ready"
    headline = "Hệ thống sẵn sàng"
    action_required = "Không có việc khẩn cấp."
    recovery_action = None
    if not runtime.get("online", False):
        status = "critical"
        headline = "Runtime đang offline"
        action_required = "Khởi động hoặc kết nối lại runtime trước khi điều khiển Mai."
        recovery_action = "restart_runtime"
    elif emergency.get("latched", False):
        status = "critical"
        headline = "Emergency stop đang khóa output"
        action_required = str(emergency.get("reason") or "Xác minh an toàn trước khi resume.")
        recovery_action = "resume_emergency"
    elif int(incidents.get("unresolved") or 0) > 0:
        status = "critical"
        headline = "Có incident chưa xử lý"
        action_required = "Mở danh sách incident và xử lý nguyên nhân trước khi tiếp tục live."
        recovery_action = "inspect_incidents"
    elif unhealthy:
        status = "warning"
        headline = "Có service cần chú ý"
        action_required = "Kiểm tra recovery/circuit của: " + ", ".join(unhealthy)
        recovery_action = "inspect_health"
    elif operations.get("paused", False):
        status = "warning"
        headline = "Agent đang tạm dừng"
        action_required = str(operations.get("pause_reason") or "Resume khi đã sẵn sàng.")
        recovery_action = "resume_agent"
    elif current.get("hard_rejection_reason") or current.get("delivery_state") == "failed":
        status = "warning"
        headline = "Quyết định gần nhất cần kiểm tra"
        action_required = str(
            current.get("hard_rejection_reason") or current.get("outcome") or "delivery_failed"
        )
        recovery_action = "inspect_decision"

    return {
        "schema_version": 1,
        "overall_status": status,
        "headline": headline,
        "action_required": action_required,
        "recovery_action": recovery_action,
        "runtime_online": bool(runtime.get("online", False)),
        "controls_available": bool(runtime.get("controls_available", False)),
        "unresolved_incidents": int(incidents.get("unresolved") or 0),
        "unhealthy_services": unhealthy,
        "current_action": current.get("action"),
        "current_reason": current.get("reason"),
        "current_delivery_state": current.get("delivery_state"),
        "current_outcome": current.get("outcome"),
        "decision_id": current.get("decision_id"),
        "evidence_refs": list(current.get("evidence_refs") or []),
        "active_goal": goals.get("active"),
    }


class DashboardServer:
    def __init__(
        self,
        feature_manager: Any = None,
        state_machine: Any = None,
        trigger_manager: Any = None,
        metrics: Any = None,
        emergency_stop: Any = None,
        health_monitor: Any = None,
        watchdog: Any = None,
        filter_svc: Any = None,
        regenerator: Any = None,
        tts_service: Any = None,
        audio_player: Any = None,
        tts_pipeline: Any = None,
        emotion: Any = None,
        runner: Any = None,           # T3/T7: LLMTurnRunner (last_turn_id) — data label
        agent_state: Any = None,      # M1: shared grounded state, read-only snapshot
        goal_manager: Any = None,     # M2: read + audited operator controls
        relationship_manager: Any = None,  # M7: audited social record controls
        decision_records: Any = None,      # M10.3: versioned Director decision view
        self_talk_planner: Any = None,     # cause-first thought state, read-only
        control_plane: Any = None,          # M9: pause/resume/action queue/audit
        snapshot_provider: Any = None,      # M9: standalone read-only provider
        health_supervisor: Any = None,      # M9: bounded recovery snapshot
        emergency_controller: Any = None,   # M9: fail-closed output latch
        incident_log: Any = None,            # M9: versioned incident ledger
        data_dir: str = "logs",       # nơi ghi ratings/corrections
        push_interval_s: float = 1.0,
        gpu_metrics_command: str = "nvidia-smi",
        gpu_metrics_timeout_s: float = 1.0,
        gpu_metrics_refresh_s: float = 2.0,
        host: str = "127.0.0.1",
        port: int = 7860,
    ) -> None:
        self.features = feature_manager
        self.sm = state_machine
        self.triggers = trigger_manager
        self.metrics = metrics
        self.emergency = emergency_stop
        self.health = health_monitor
        self.watchdog = watchdog
        self.filter = filter_svc
        self.regenerator = regenerator
        self.tts_service = tts_service
        self.audio_player = audio_player
        self.tts_pipeline = tts_pipeline
        self.emotion = emotion
        self.runner = runner
        self.agent_state = agent_state
        self.goal_manager = goal_manager
        self.relationship_manager = relationship_manager
        self.decision_records = decision_records
        self.self_talk_planner = self_talk_planner
        self.control_plane = control_plane
        self.snapshot_provider = snapshot_provider
        self.health_supervisor = health_supervisor
        self.emergency_controller = emergency_controller
        self.incident_log = incident_log
        self._data_dir = Path(data_dir)
        self._ratings_writer = None       # lazy JsonlWriter
        self._corrections_writer = None
        self.push_interval_s = push_interval_s
        self.gpu_metrics_command = gpu_metrics_command
        self.gpu_metrics_timeout_s = float(gpu_metrics_timeout_s)
        self.gpu_metrics_refresh_s = float(gpu_metrics_refresh_s)
        self.host = host
        self.port = int(port)
        self._log = get_logger("dashboard")
        self._ws_clients: set[WebSocket] = set()
        self._ws_source_modes: dict[WebSocket, str] = {}
        self._push_task: asyncio.Task[None] | None = None
        self._uvicorn_server: Any = None
        self.app = self._build_app()

    async def serve(self) -> None:
        """Serve as a managed task so M9 supervisor can restart the dashboard."""
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
        """Close clients and request uvicorn exit without ASGI cancellation noise."""
        clients = list(self._ws_clients)
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close(code=1001, reason="runtime shutdown")
        self._ws_clients.clear()
        self._ws_source_modes.clear()
        await self.stop_push_loop()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    # ---------- snapshot ----------

    async def build_snapshot(self, source_mode: str = "auto") -> dict[str, Any]:
        snap: dict[str, Any] = {}
        if self.snapshot_provider is not None:
            with contextlib.suppress(Exception):
                if hasattr(self.snapshot_provider, "snapshot_for"):
                    snap.update(await self.snapshot_provider.snapshot_for(source_mode))
                else:
                    snap.update(await self.snapshot_provider.snapshot())

        if self.sm is not None:
            snap["state"] = {
                "current": self.sm.state,
                "time_in_state_ms": self.sm.time_in_state_ms(),
                "last_turn_interrupted": self.sm.last_turn_interrupted,
                "history": [h.to_log_dict() for h in self.sm.history(limit=10)],
            }

        if self.metrics is not None:
            snap["metrics"] = await asyncio.to_thread(
                self.metrics.sample_gpu_metrics,
                command=self.gpu_metrics_command,
                timeout_s=self.gpu_metrics_timeout_s,
                refresh_s=self.gpu_metrics_refresh_s,
            )
            if hasattr(self.metrics, "llm_snapshot"):
                snap["llm"] = self.metrics.llm_snapshot()

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

        if self.watchdog is not None:
            snap["watchdog"] = self.watchdog.snapshot()

        # 3.C: filter panel — merge check-level metrics + regen metrics + fail_open
        # từ filter service (nếu có).
        if self.metrics is not None and hasattr(self.metrics, "filter_snapshot"):
            fsnap = self.metrics.filter_snapshot()
            if self.regenerator is not None:
                rm = self.regenerator.get_metrics()
                fsnap["regen"] = {
                    "attempts_total": rm.get("filter_regen_attempts_total", 0),
                    "recovered_total": rm.get("filter_regen_recovered_total", 0),
                    "exhausted_total": rm.get("filter_regen_exhausted_total", 0),
                }
            if self.filter is not None and hasattr(self.filter, "get_metrics"):
                fm = self.filter.get_metrics()
                fsnap["service_fail_open_total"] = fm.get("filter_fail_open_total", 0)
            snap["filter"] = fsnap

        # 4.E: TTS panel — merge check-level metrics + service/player/pipeline
        if self.metrics is not None and hasattr(self.metrics, "tts_snapshot"):
            tsnap = self.metrics.tts_snapshot()
            if self.tts_service is not None and hasattr(self.tts_service, "get_metrics"):
                sm = self.tts_service.get_metrics()
                tsnap["service"] = {
                    "requests_total": sm.get("tts_requests_total", 0),
                    "errors_total": sm.get("tts_errors_total", 0),
                    "last_ttfa_ms": sm.get("tts_last_ttfa_ms"),
                    "last_chunks": sm.get("tts_last_chunks", 0),
                    "last_rtf": sm.get("tts_last_rtf"),
                }
            if self.audio_player is not None and hasattr(self.audio_player, "get_metrics"):
                pm = self.audio_player.get_metrics()
                tsnap["player"] = {
                    "chunks_played": pm.get("audio_chunks_played", 0),
                    "chunks_dropped": pm.get("audio_chunks_dropped", 0),
                    "queue_size": pm.get("audio_queue_size", 0),
                    "is_playing": pm.get("audio_is_playing", False),
                }
            if self.tts_pipeline is not None and hasattr(self.tts_pipeline, "get_metrics"):
                pp = self.tts_pipeline.get_metrics()
                tsnap["pipeline"] = {
                    "sentences_total": pp.get("tts_pipeline_sentences_total", 0),
                }
            snap["tts"] = tsnap

        # Mood panel — current_mood (pos) + target + active flags (A1: mood engine
        # là ground-truth duy nhất sau khi bỏ LLM self-report).
        if self.emotion is not None and hasattr(self.emotion, "snapshot"):
            with contextlib.suppress(Exception):
                mood = self.emotion.snapshot()
                mood["sampled_at"] = datetime.now(timezone.utc).isoformat()
                if hasattr(self.emotion, "get_metrics"):
                    mood["ticks"] = self.emotion.get_metrics().get("mood_ticks")
                snap["mood"] = mood
        if self.self_talk_planner is not None:
            with contextlib.suppress(Exception):
                snap["thought_engine"] = self.self_talk_planner.snapshot()

        if self.health is not None:
            snap["health"] = self.health.snapshot()
        if self.agent_state is not None:
            with contextlib.suppress(Exception):
                snap["agent"] = self.agent_state.snapshot().to_dict()
                if self.metrics is not None and hasattr(self.metrics, "agent_snapshot"):
                    snap["agent_metrics"] = self.metrics.agent_snapshot()
        if self.goal_manager is not None:
            with contextlib.suppress(Exception):
                snap["goals"] = self.goal_manager.snapshot().to_dict()
                snap["goal_metrics"] = self.goal_manager.get_metrics()
        if self.relationship_manager is not None:
            with contextlib.suppress(Exception):
                snap["relationships"] = self.relationship_manager.snapshot().to_dict()
                snap["relationship_metrics"] = self.relationship_manager.get_metrics()
        if self.decision_records is not None:
            with contextlib.suppress(Exception):
                snap["decisions"] = self.decision_records.snapshot()
        if self.health_supervisor is not None:
            with contextlib.suppress(Exception):
                snap["health_supervisor"] = self.health_supervisor.snapshot()
        if self.emergency_controller is not None:
            with contextlib.suppress(Exception):
                snap["emergency"] = self.emergency_controller.snapshot()
        if self.incident_log is not None:
            with contextlib.suppress(Exception):
                snap["incidents"] = self.incident_log.snapshot()
        if self.control_plane is not None:
            with contextlib.suppress(Exception):
                snap["operations"] = self.control_plane.snapshot()
            controls_available = bool(
                (snap.get("operations") or {}).get("available", False)
            )
            snap["runtime"] = {
                **dict(snap.get("runtime") or {}),
                "online": controls_available,
                "mode": "embedded" if controls_available else "embedded_starting",
                "controls_available": controls_available,
            }
        else:
            snap.setdefault("runtime", {
                "online": False,
                "mode": "standalone",
                "controls_available": False,
            })
        snap["operator_overview"] = _build_operator_overview(snap)
        if self.snapshot_provider is not None and hasattr(
            self.snapshot_provider, "get_metrics",
        ):
            with contextlib.suppress(Exception):
                snap["dashboard_source_metrics"] = self.snapshot_provider.get_metrics()
        return snap

    # ---------- app ----------

    async def _operator_v2_enabled(self) -> bool:
        if self.snapshot_provider is not None:
            return True
        if self.features is None:
            return False
        try:
            from orchestrator.features import FeatureStatus
            status = await self.features.get_status("operator_dashboard_v2")
            return status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
        except Exception:
            return False

    def _record_dashboard_view(self, version: str) -> None:
        if self.metrics is not None and hasattr(
            self.metrics, "record_operator_dashboard_view",
        ):
            with contextlib.suppress(Exception):
                self.metrics.record_operator_dashboard_view(version)

    @staticmethod
    def _read_template(name: str) -> str:
        path = _TEMPLATES / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return "<h1>Mai Dashboard</h1><p>template chưa có</p>"

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Mai Dashboard")

        if _STATIC.exists():
            app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            template = "operator_v2.html" if await self._operator_v2_enabled() else "index.html"
            self._record_dashboard_view("v2" if template.startswith("operator") else "legacy")
            return self._read_template(template)

        @app.get("/operator", response_class=HTMLResponse)
        async def operator_dashboard() -> str:
            self._record_dashboard_view("v2")
            return self._read_template("operator_v2.html")

        @app.get("/legacy", response_class=HTMLResponse)
        async def legacy_dashboard() -> str:
            self._record_dashboard_view("legacy")
            return self._read_template("index.html")

        @app.get("/api/snapshot")
        async def api_snapshot(source: str = "auto") -> JSONResponse:
            return JSONResponse(await self.build_snapshot(source))

        @app.get("/api/history/turns")
        async def api_history_turns(
            session_id: str | None = None,
            started_at: str | None = None,
            ended_at: str | None = None,
            kind: str | None = None,
            delivered: bool | None = None,
            limit: int | None = None,
        ) -> JSONResponse:
            if self.snapshot_provider is None or not hasattr(
                self.snapshot_provider, "query_history",
            ):
                return JSONResponse(
                    {"ok": False, "reason": "history_source_unavailable"}, status_code=503,
                )
            value = await self.snapshot_provider.query_history(
                session_id=session_id, started_at=started_at, ended_at=ended_at,
                kind=kind, delivered=delivered, limit=limit,
            )
            return JSONResponse(value)

        @app.get("/api/features")
        async def api_features() -> JSONResponse:
            snap = await self.build_snapshot()
            return JSONResponse(snap.get("features", []))

        @app.post("/api/features/{feature_id}/toggle")
        async def api_toggle(feature_id: str) -> JSONResponse:
            if self.features is None:
                return await self._forward_or_unavailable(
                    f"/api/features/{feature_id}/toggle", {}, "no feature manager",
                )
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
            if self.emergency_controller is not None:
                ok = await self.emergency_controller.trigger("dashboard emergency stop")
                return JSONResponse({"ok": ok, "emergency": self.emergency_controller.snapshot()})
            if self.emergency is not None:
                await self.emergency.trigger()
            elif self.sm is not None:
                await self.sm.emergency_stop()
            else:
                return await self._forward_or_unavailable(
                    "/api/emergency_stop", {}, "no handler",
                )
            return JSONResponse({"ok": True, "state": self.sm.state if self.sm else None})

        @app.post("/api/resume")
        async def api_resume() -> JSONResponse:
            if self.emergency_controller is not None:
                ok = await self.emergency_controller.resume("dashboard operator resume")
                return JSONResponse(
                    {"ok": ok, "emergency": self.emergency_controller.snapshot()},
                    status_code=200 if ok else 409,
                )
            if self.sm is None:
                return await self._forward_or_unavailable(
                    "/api/resume", {}, "no state machine",
                )
            from transitions.core import MachineError

            try:
                await self.sm.resume()
            except MachineError as e:
                return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)
            return JSONResponse({"ok": True, "state": self.sm.state})

        @app.post("/api/goals/pin")
        async def api_goal_pin(request: Request) -> JSONResponse:
            if self.goal_manager is None:
                return await self._forward_or_unavailable(
                    "/api/goals/pin", await _json(request), "no goal manager",
                )
            body = await _json(request)
            reason = str(body.get("reason") or "").strip()
            success = str(body.get("success_condition") or "").strip()
            parent = str(body.get("parent_thread_id") or "").strip() or None
            goal = self.goal_manager.pin_operator(
                reason=reason, success_condition=success, parent_thread_id=parent,
            )
            if goal is None:
                return JSONResponse(
                    {"ok": False, "reason": "invalid or rejected operator goal"},
                    status_code=400,
                )
            self._audit_control("pin_goal", goal.goal_id, "completed")
            return JSONResponse({"ok": True, "goal": goal.to_dict()})

        @app.post("/api/goals/{goal_id}/complete")
        async def api_goal_complete(goal_id: str, request: Request) -> JSONResponse:
            if self.goal_manager is None:
                return await self._forward_or_unavailable(
                    f"/api/goals/{goal_id}/complete", await _json(request), "no goal manager",
                )
            body = await _json(request)
            reason = str(body.get("reason") or "operator complete").strip()
            ok = self.goal_manager.operator_complete(goal_id, reason=reason)
            self._audit_control("complete_goal", goal_id, "completed" if ok else "not_found")
            return JSONResponse(
                {"ok": ok, "goal_id": goal_id, "reason": reason if ok else "unknown goal"},
                status_code=200 if ok else 404,
            )

        @app.post("/api/goals/{goal_id}/cancel")
        async def api_goal_cancel(goal_id: str, request: Request) -> JSONResponse:
            if self.goal_manager is None:
                return await self._forward_or_unavailable(
                    f"/api/goals/{goal_id}/cancel", await _json(request), "no goal manager",
                )
            body = await _json(request)
            reason = str(body.get("reason") or "operator cancel").strip()
            ok = self.goal_manager.operator_cancel(goal_id, reason=reason)
            self._audit_control("cancel_goal", goal_id, "completed" if ok else "not_found")
            return JSONResponse(
                {"ok": ok, "goal_id": goal_id, "reason": reason if ok else "unknown goal"},
                status_code=200 if ok else 404,
            )

        @app.post("/api/agent/pause")
        async def api_agent_pause(request: Request) -> JSONResponse:
            if self.control_plane is None:
                return await self._forward_or_unavailable(
                    "/api/agent/pause", await _json(request), "runtime_offline",
                )
            body = await _json(request)
            ok = await self.control_plane.pause(
                str(body.get("reason") or "dashboard operator pause"),
            )
            return JSONResponse({"ok": ok, "operations": self.control_plane.snapshot()})

        @app.post("/api/agent/resume")
        async def api_agent_resume(request: Request) -> JSONResponse:
            if self.control_plane is None:
                return await self._forward_or_unavailable(
                    "/api/agent/resume", await _json(request), "runtime_offline",
                )
            body = await _json(request)
            ok = await self.control_plane.resume(
                str(body.get("reason") or "dashboard operator resume"),
            )
            return JSONResponse({"ok": ok, "operations": self.control_plane.snapshot()})

        @app.get("/api/relationships")
        async def api_relationships() -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            return JSONResponse({"ok": True, **self.relationship_manager.snapshot().to_dict()})

        @app.post("/api/relationships/{viewer_id}/profile")
        async def api_relationship_profile(viewer_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            profile = self.relationship_manager.update_profile(
                viewer_id,
                preferences=_string_list(body.get("preferences")),
                boundaries=_string_list(body.get("boundaries")),
                tone=str(body.get("tone") or "").strip() or None,
                evidence_refs=_string_list(body.get("evidence_refs")),
                reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse(
                {"ok": profile is not None, "profile": profile.to_dict() if profile else None},
                status_code=200 if profile else 400,
            )

        @app.post("/api/relationships/{viewer_id}/notes")
        async def api_relationship_note(viewer_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            note = self.relationship_manager.create_note(
                viewer_id, summary=str(body.get("summary") or ""),
                evidence_refs=_string_list(body.get("evidence_refs")),
                reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse(
                {"ok": note is not None, "note": note.to_dict() if note else None},
                status_code=200 if note else 400,
            )

        @app.post("/api/relationships/notes/{note_id}/review")
        async def api_relationship_note_review(note_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            approve = body.get("approve") is True
            ok = self.relationship_manager.review_note(
                note_id, approve=approve, reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse({"ok": ok}, status_code=200 if ok else 400)

        @app.delete("/api/relationships/notes/{note_id}")
        async def api_relationship_note_delete(note_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            ok = self.relationship_manager.delete_note(
                note_id, reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse({"ok": ok}, status_code=200 if ok else 400)

        @app.post("/api/relationships/narratives")
        async def api_relationship_narrative(request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            item = self.relationship_manager.create_narrative(
                summary=str(body.get("summary") or ""),
                event_refs=_string_list(body.get("event_refs")),
                viewer_id=str(body.get("viewer_id") or "").strip() or None,
                reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse(
                {"ok": item is not None, "narrative": item.to_dict() if item else None},
                status_code=200 if item else 400,
            )

        @app.post("/api/relationships/narratives/{narrative_id}/resolve")
        async def api_relationship_narrative_resolve(
            narrative_id: str, request: Request,
        ) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            ok = self.relationship_manager.resolve_narrative(
                narrative_id, reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse({"ok": ok}, status_code=200 if ok else 400)

        @app.post("/api/relationships/{viewer_id}/running-gags")
        async def api_relationship_running_gag(viewer_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            gag = self.relationship_manager.create_running_gag(
                viewer_id, summary=str(body.get("summary") or ""),
                event_refs=_string_list(body.get("event_refs")),
                reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse(
                {"ok": gag is not None, "running_gag": gag.to_dict() if gag else None},
                status_code=200 if gag else 400,
            )

        @app.post("/api/relationships/running-gags/{gag_id}/review")
        async def api_relationship_running_gag_review(gag_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            ok = self.relationship_manager.review_running_gag(
                gag_id, approve=body.get("approve") is True,
                reason=str(body.get("reason") or "").strip(),
            )
            return JSONResponse({"ok": ok}, status_code=200 if ok else 400)

        @app.get("/api/relationships/{viewer_id}/export")
        async def api_relationship_export(viewer_id: str) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            try:
                exported = await self.relationship_manager.export_viewer(viewer_id)
            except (ValueError, RuntimeError) as exc:
                return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
            return JSONResponse({"ok": True, "viewer_id": viewer_id, "export": exported})

        @app.delete("/api/relationships/{viewer_id}")
        async def api_relationship_delete(viewer_id: str, request: Request) -> JSONResponse:
            if self.relationship_manager is None:
                return JSONResponse({"ok": False, "reason": "no relationship manager"}, status_code=503)
            body = await _json(request)
            try:
                result = await self.relationship_manager.delete_viewer(
                    viewer_id, reason=str(body.get("reason") or "").strip(),
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "reason": str(exc)}, status_code=500)
            return JSONResponse(
                {"ok": result is not None, "result": result},
                status_code=200 if result else 400,
            )

        @app.get("/metrics", response_class=PlainTextResponse)
        async def metrics_endpoint() -> bytes:
            if self.metrics is None:
                return b""
            return self.metrics.prometheus_text()

        # ── T3: operator chấm điểm turn gần nhất ──
        @app.post("/api/rate")
        async def api_rate(request: Request) -> JSONResponse:
            body = await _json(request)
            rating = str(body.get("rating", "")).strip()
            if rating not in ("good", "bad", "flag"):
                return JSONResponse({"ok": False, "reason": "rating không hợp lệ"}, status_code=400)
            # Identity cụ thể (Review) hoặc identity của turn cuối (chấm live).
            turn_id = body.get("turn_id")
            session_id = body.get("session_id")
            if turn_id is None:
                if session_id is not None:
                    return JSONResponse(
                        {"ok": False, "reason": "thiếu turn_id"}, status_code=400,
                    )
                identity = self._last_turn_identity()
                if identity is not None:
                    session_id, turn_id = identity
            else:
                if not isinstance(session_id, str) or not session_id.strip():
                    return JSONResponse(
                        {"ok": False, "reason": "thiếu session_id"}, status_code=400,
                    )
                session_id = session_id.strip()
                try:
                    turn_id = int(turn_id)
                except (TypeError, ValueError):
                    turn_id = None
            if turn_id is None or session_id is None:
                return JSONResponse({"ok": False, "reason": "chưa có turn"}, status_code=400)
            self._write_rating({"session_id": session_id, "turn_id": turn_id, "rating": rating,
                                "ts": _now_iso()})
            return JSONResponse({"ok": True, "session_id": session_id,
                                 "turn_id": turn_id, "rating": rating})

        # ── T7: operator sửa trực tiếp câu Mai (data vàng nhất) ──
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
            corrected = mask_pii(corrected) or ""
            self._write_correction({"session_id": session_id, "turn_id": turn_id,
                                    "original": original,
                                    "corrected": corrected, "ts": _now_iso()})
            return JSONResponse({"ok": True, "session_id": session_id, "turn_id": turn_id})

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            self._ws_clients.add(websocket)
            source_mode = str(websocket.query_params.get("source") or "auto").lower()
            if source_mode not in {"auto", "live", "history"}:
                source_mode = "auto"
            self._ws_source_modes[websocket] = source_mode
            try:
                await websocket.send_json(await self.build_snapshot(source_mode))
                while True:
                    # giữ kết nối; client không cần gửi gì, nhưng đọc để phát hiện disconnect
                    await websocket.receive_text()
            except (WebSocketDisconnect, asyncio.CancelledError):
                # disconnect bình thường HOẶC server shutdown (cancel) → im lặng,
                # không đổ traceback ASGI lúc quit.
                pass
            except Exception:
                pass   # kết nối lỗi bất kỳ → dọn client, không giết server
            finally:
                self._ws_clients.discard(websocket)
                self._ws_source_modes.pop(websocket, None)

        return app

    # ---------- T3/T7 data label helpers ----------

    def _last_turn_identity(self) -> tuple[str, int] | None:
        session_id = getattr(self.runner, "session_id", None) if self.runner else None
        tid = getattr(self.runner, "last_turn_id", 0) if self.runner else 0
        if not isinstance(session_id, str) or not session_id or not tid:
            return None
        return session_id, int(tid)

    def _ratings(self):
        if self._ratings_writer is None:
            from orchestrator.logger import JsonlWriter
            self._ratings_writer = JsonlWriter(self._data_dir / "ratings.jsonl")
        return self._ratings_writer

    def _corrections(self):
        if self._corrections_writer is None:
            from orchestrator.logger import JsonlWriter
            self._corrections_writer = JsonlWriter(self._data_dir / "corrections.jsonl")
        return self._corrections_writer

    def _write_rating(self, rec: dict) -> None:
        try:
            self._ratings().write(rec)
        except Exception as e:
            self._log.warning("rating_write_failed", error=str(e))

    def _write_correction(self, rec: dict) -> None:
        try:
            self._corrections().write(rec)
        except Exception as e:
            self._log.warning("correction_write_failed", error=str(e))

    def _audit_control(self, action: str, target: str, outcome: str) -> None:
        if self.control_plane is not None:
            with contextlib.suppress(Exception):
                self.control_plane.record_operator_action(action, target, outcome)

    async def _forward_or_unavailable(
        self, path: str, payload: dict[str, Any], reason: str,
    ) -> JSONResponse:
        if self.snapshot_provider is None or not hasattr(
            self.snapshot_provider, "forward_command",
        ):
            return JSONResponse({"ok": False, "reason": reason}, status_code=503)
        status, value = await self.snapshot_provider.forward_command(path, payload)
        return JSONResponse(value, status_code=status)

    def _recent_turns(self, n: int) -> list[dict]:
        """Tail turns.jsonl → N turn gần nhất với composite identity."""
        recs = _tail_jsonl(self._data_dir / "turns.jsonl", n)
        return [{"session_id": r.get("session_id"), "turn_id": r.get("turn_id"),
                 "kind": r.get("kind"),
                 "user_text": r.get("user_text"), "mai_text": r.get("mai_text")}
                for r in recs]

    def _original_of(self, session_id: str, turn_id: int) -> str | None:
        for r in _tail_jsonl(self._data_dir / "turns.jsonl", 200):
            if r.get("session_id") == session_id and r.get("turn_id") == turn_id:
                return r.get("mai_text")
        return None

    # ---------- push loop ----------

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(self.push_interval_s)
            if not self._ws_clients:
                continue
            dead: list[WebSocket] = []
            snapshots: dict[str, dict[str, Any]] = {}
            for client in list(self._ws_clients):
                try:
                    source_mode = self._ws_source_modes.get(client, "auto")
                    if source_mode not in snapshots:
                        snapshots[source_mode] = await self.build_snapshot(source_mode)
                    await client.send_json(snapshots[source_mode])
                except Exception:
                    dead.append(client)
            for d in dead:
                self._ws_clients.discard(d)
                self._ws_source_modes.pop(d, None)

    def start_push_loop(self) -> None:
        if self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop())

    async def stop_push_loop(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._push_task
            self._push_task = None


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail_jsonl(path: Path, n: int) -> list[dict]:
    """Đọc N dòng cuối JSONL → list dict (mới nhất cuối). Lỗi/thiếu file → []."""
    import json
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out
    except Exception:
        return []
