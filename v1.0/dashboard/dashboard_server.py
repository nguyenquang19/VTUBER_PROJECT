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

from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
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
        watchdog: Any = None,
        filter_svc: Any = None,
        regenerator: Any = None,
        tts_service: Any = None,
        audio_player: Any = None,
        tts_pipeline: Any = None,
        emotion: Any = None,
        runner: Any = None,           # T3/T7: LLMTurnRunner (last_turn_id) — data label
        data_dir: str = "logs",       # nơi ghi ratings/corrections
        push_interval_s: float = 1.0,
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
        self._data_dir = Path(data_dir)
        self._ratings_writer = None       # lazy JsonlWriter
        self._corrections_writer = None
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
                snap["mood"] = self.emotion.snapshot()

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

        # ── T3: operator chấm điểm turn gần nhất ──
        @app.post("/api/rate")
        async def api_rate(request: Request) -> JSONResponse:
            body = await _json(request)
            rating = str(body.get("rating", "")).strip()
            if rating not in ("good", "bad", "flag"):
                return JSONResponse({"ok": False, "reason": "rating không hợp lệ"}, status_code=400)
            # turn_id cụ thể (bấm trên 1 item Review) hoặc turn cuối (chấm live)
            turn_id = body.get("turn_id")
            if turn_id is None:
                turn_id = self._last_turn_id()
            else:
                try:
                    turn_id = int(turn_id)
                except (TypeError, ValueError):
                    turn_id = None
            if turn_id is None:
                return JSONResponse({"ok": False, "reason": "chưa có turn"}, status_code=400)
            self._write_rating({"turn_id": turn_id, "rating": rating,
                                "ts": _now_iso()})
            return JSONResponse({"ok": True, "turn_id": turn_id, "rating": rating})

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
            corrected = str(body.get("corrected_text", "")).strip()
            if not corrected:
                return JSONResponse({"ok": False, "reason": "corrected_text rỗng"}, status_code=400)
            original = self._original_of(turn_id)
            self._write_correction({"turn_id": turn_id, "original": original,
                                    "corrected": corrected, "ts": _now_iso()})
            return JSONResponse({"ok": True, "turn_id": turn_id})

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            self._ws_clients.add(websocket)
            try:
                await websocket.send_json(await self.build_snapshot())
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

        return app

    # ---------- T3/T7 data label helpers ----------

    def _last_turn_id(self) -> int | None:
        tid = getattr(self.runner, "last_turn_id", 0) if self.runner else 0
        return tid if tid else None

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

    def _recent_turns(self, n: int) -> list[dict]:
        """Tail turns.jsonl → N turn gần nhất (turn_id, kind, user_text, mai_text)."""
        recs = _tail_jsonl(self._data_dir / "turns.jsonl", n)
        return [{"turn_id": r.get("turn_id"), "kind": r.get("kind"),
                 "user_text": r.get("user_text"), "mai_text": r.get("mai_text")}
                for r in recs]

    def _original_of(self, turn_id: int) -> str | None:
        for r in _tail_jsonl(self._data_dir / "turns.jsonl", 200):
            if r.get("turn_id") == turn_id:
                return r.get("mai_text")
        return None

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
