"""Composition-only bindings for the canonical live Operations Surface."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


def build_operations_surface(
    *, loader: Any, metrics: Any, snapshot_providers: dict[str, Any],
) -> Any:
    from services.operations.surface import OperationsSurface

    surface = OperationsSurface.from_loader(loader, metrics=metrics)
    for name, provider in snapshot_providers.items():
        if provider is not None:
            surface.register_snapshot_provider(name, provider)
    return surface


def bind_standard_operator_commands(
    surface: Any,
    *, feature_manager: Any, control_plane: Any,
    goal_manager: Any, relationship_manager: Any,
) -> None:
    from orchestrator.features import CoreFeatureError, FeatureStatus

    async def feature_toggle(payload: Any) -> tuple[int, dict[str, Any]]:
        feature_id = _required(payload, "feature_id")
        if feature_manager.is_core(feature_id):
            return 400, {"ok": False, "reason": f"{feature_id} là core feature, không toggle được"}
        try:
            status = await feature_manager.get_status(feature_id)
        except KeyError:
            return 404, {"ok": False, "reason": "unknown feature"}
        try:
            result = (
                await feature_manager.disable(feature_id, user="dashboard")
                if status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
                else await feature_manager.enable(feature_id, user="dashboard")
            )
        except CoreFeatureError as exc:
            return 400, {"ok": False, "reason": str(exc)}
        return 200, {
            "ok": result.ok, "status": result.status.value, "reason": result.reason,
        }

    async def agent_pause(payload: Any) -> dict[str, Any]:
        ok = await control_plane.pause(str(payload.get("reason") or "dashboard operator pause"))
        return {"ok": ok, "operations": control_plane.snapshot()}

    async def agent_resume(payload: Any) -> dict[str, Any]:
        ok = await control_plane.resume(str(payload.get("reason") or "dashboard operator resume"))
        return {"ok": ok, "operations": control_plane.snapshot()}

    def goal_pin(payload: Any) -> tuple[int, dict[str, Any]]:
        goal = goal_manager.pin_operator(
            reason=str(payload.get("reason") or "").strip(),
            success_condition=str(payload.get("success_condition") or "").strip(),
            parent_thread_id=str(payload.get("parent_thread_id") or "").strip() or None,
        )
        if goal is None:
            return 400, {"ok": False, "reason": "invalid or rejected operator goal"}
        control_plane.record_operator_action("pin_goal", goal.goal_id, "completed")
        return 200, {"ok": True, "goal": goal.to_dict()}

    def goal_terminal(payload: Any, *, cancel: bool) -> tuple[int, dict[str, Any]]:
        goal_id = _required(payload, "goal_id")
        reason = str(payload.get("reason") or (
            "operator cancel" if cancel else "operator complete"
        )).strip()
        ok = (
            goal_manager.operator_cancel(goal_id, reason=reason)
            if cancel else goal_manager.operator_complete(goal_id, reason=reason)
        )
        control_plane.record_operator_action(
            "cancel_goal" if cancel else "complete_goal",
            goal_id, "completed" if ok else "not_found",
        )
        return (200 if ok else 404), {
            "ok": ok, "goal_id": goal_id, "reason": reason if ok else "unknown goal",
        }

    def relationship_profile(payload: Any) -> tuple[int, dict[str, Any]]:
        profile = relationship_manager.update_profile(
            _required(payload, "viewer_id"),
            preferences=_string_list(payload.get("preferences")),
            boundaries=_string_list(payload.get("boundaries")),
            tone=str(payload.get("tone") or "").strip() or None,
            evidence_refs=_string_list(payload.get("evidence_refs")),
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if profile else 400), {
            "ok": profile is not None,
            "profile": profile.to_dict() if profile else None,
        }

    def relationship_note_create(payload: Any) -> tuple[int, dict[str, Any]]:
        note = relationship_manager.create_note(
            _required(payload, "viewer_id"), summary=str(payload.get("summary") or ""),
            evidence_refs=_string_list(payload.get("evidence_refs")),
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if note else 400), {
            "ok": note is not None, "note": note.to_dict() if note else None,
        }

    def relationship_note_review(payload: Any) -> tuple[int, dict[str, Any]]:
        ok = relationship_manager.review_note(
            _required(payload, "note_id"), approve=payload.get("approve") is True,
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if ok else 400), {"ok": ok}

    def relationship_note_delete(payload: Any) -> tuple[int, dict[str, Any]]:
        ok = relationship_manager.delete_note(
            _required(payload, "note_id"),
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if ok else 400), {"ok": ok}

    def relationship_narrative_create(payload: Any) -> tuple[int, dict[str, Any]]:
        item = relationship_manager.create_narrative(
            summary=str(payload.get("summary") or ""),
            event_refs=_string_list(payload.get("event_refs")),
            viewer_id=str(payload.get("viewer_id") or "").strip() or None,
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if item else 400), {
            "ok": item is not None, "narrative": item.to_dict() if item else None,
        }

    def relationship_narrative_resolve(payload: Any) -> tuple[int, dict[str, Any]]:
        ok = relationship_manager.resolve_narrative(
            _required(payload, "narrative_id"),
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if ok else 400), {"ok": ok}

    def relationship_gag_create(payload: Any) -> tuple[int, dict[str, Any]]:
        gag = relationship_manager.create_running_gag(
            _required(payload, "viewer_id"), summary=str(payload.get("summary") or ""),
            event_refs=_string_list(payload.get("event_refs")),
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if gag else 400), {
            "ok": gag is not None, "running_gag": gag.to_dict() if gag else None,
        }

    def relationship_gag_review(payload: Any) -> tuple[int, dict[str, Any]]:
        ok = relationship_manager.review_running_gag(
            _required(payload, "gag_id"), approve=payload.get("approve") is True,
            reason=str(payload.get("reason") or "").strip(),
        )
        return (200 if ok else 400), {"ok": ok}

    async def relationship_export(payload: Any) -> tuple[int, dict[str, Any]]:
        viewer_id = _required(payload, "viewer_id")
        try:
            exported = await relationship_manager.export_viewer(viewer_id)
        except (ValueError, RuntimeError) as exc:
            return 400, {"ok": False, "reason": str(exc)}
        return 200, {"ok": True, "viewer_id": viewer_id, "export": exported}

    async def relationship_delete(payload: Any) -> tuple[int, dict[str, Any]]:
        viewer_id = _required(payload, "viewer_id")
        try:
            result = await relationship_manager.delete_viewer(
                viewer_id, reason=str(payload.get("reason") or "").strip(),
            )
        except Exception as exc:
            return 500, {"ok": False, "reason": str(exc)}
        return (200 if result else 400), {"ok": result is not None, "result": result}

    handlers: dict[str, Any] = {
        "feature.toggle": feature_toggle,
    }
    if control_plane is not None:
        handlers.update({
            "agent.pause": agent_pause,
            "agent.resume": agent_resume,
        })
    if goal_manager is not None:
        handlers.update({
            "goal.pin": goal_pin,
            "goal.complete": lambda value: goal_terminal(value, cancel=False),
            "goal.cancel": lambda value: goal_terminal(value, cancel=True),
        })
    if relationship_manager is not None:
        handlers.update({
            "relationship.profile": relationship_profile,
            "relationship.note.create": relationship_note_create,
            "relationship.note.review": relationship_note_review,
            "relationship.note.delete": relationship_note_delete,
            "relationship.narrative.create": relationship_narrative_create,
            "relationship.narrative.resolve": relationship_narrative_resolve,
            "relationship.gag.create": relationship_gag_create,
            "relationship.gag.review": relationship_gag_review,
            "relationship.export": relationship_export,
            "relationship.delete": relationship_delete,
        })
    for name, handler in handlers.items():
        surface.register_command(name, handler)


def bind_emergency_commands(surface: Any, emergency_controller: Any) -> None:
    async def trigger(payload: Any) -> dict[str, Any]:
        ok = await emergency_controller.trigger(
            str(payload.get("reason") or "dashboard emergency stop"),
        )
        return {"ok": ok, "emergency": emergency_controller.snapshot()}

    async def resume(payload: Any) -> tuple[int, dict[str, Any]]:
        ok = await emergency_controller.resume(
            str(payload.get("reason") or "dashboard operator resume"),
        )
        return (200 if ok else 409), {
            "ok": ok, "emergency": emergency_controller.snapshot(),
        }

    surface.register_command("emergency.trigger", trigger)
    surface.register_command("emergency.resume", resume)


def standard_snapshot_providers(
    *,
    loader: Any,
    metrics: Any,
    feature_manager: Any,
    filter_svc: Any,
    regenerator: Any,
    tts_svc: Any,
    audio_player: Any,
    tts_pipeline: Any,
    emotion: Any,
    self_talk_planner: Any,
    agent_state: Any,
    world_model: Any,
    self_model: Any,
    capability_registry: Any,
    action_mock_loop: Any,
    director_v2_shadow: Any,
    director_v2_takeover: Any,
    goal_manager: Any,
    relationship_manager: Any,
    decision_records: Any,
    trajectory_records: Any,
    turn_journal: Any,
    control_plane: Any,
    incident_log: Any,
    runner: Any,
) -> dict[str, Any]:
    """Return presentation-safe providers without granting dashboard ownership."""

    async def gpu_metrics() -> dict[str, Any]:
        return await asyncio.to_thread(
            metrics.sample_gpu_metrics,
            command=str(loader.get(
                "system", "dashboard.gpu_metrics.command", "nvidia-smi",
            )),
            timeout_s=float(loader.get(
                "system", "dashboard.gpu_metrics.timeout_s", 1.0,
            )),
            refresh_s=float(loader.get(
                "system", "dashboard.gpu_metrics.refresh_s", 2.0,
            )),
        )

    async def features() -> list[dict[str, Any]]:
        values = await feature_manager.list_features()
        return [{
            "id": item.id,
            "status": item.current_status.value,
            "enabled": item.is_enabled,
            "category": item.category,
            "vram_cost_mb": item.vram_cost_mb,
            "is_core": feature_manager.is_core(item.id),
        } for item in values]

    def runtime() -> dict[str, Any]:
        operations = control_plane.snapshot() if control_plane is not None else {}
        available = bool(operations.get("available", False))
        return {
            "online": available,
            "mode": "embedded" if available else "embedded_starting",
            "controls_available": available,
        }

    def filter_snapshot() -> dict[str, Any]:
        value = dict(metrics.filter_snapshot())
        if regenerator is not None:
            current = regenerator.get_metrics()
            value["regen"] = {
                "attempts_total": current.get("filter_regen_attempts_total", 0),
                "recovered_total": current.get("filter_regen_recovered_total", 0),
                "exhausted_total": current.get("filter_regen_exhausted_total", 0),
            }
        if filter_svc is not None:
            current = filter_svc.get_metrics()
            value["service_fail_open_total"] = current.get("filter_fail_open_total", 0)
        return value

    def tts_snapshot() -> dict[str, Any]:
        value = dict(metrics.tts_snapshot())
        if tts_svc is not None:
            current = tts_svc.get_metrics()
            value["service"] = {
                "requests_total": current.get("tts_requests_total", 0),
                "errors_total": current.get("tts_errors_total", 0),
                "last_ttfa_ms": current.get("tts_last_ttfa_ms"),
                "last_chunks": current.get("tts_last_chunks", 0),
                "last_rtf": current.get("tts_last_rtf"),
            }
        if audio_player is not None:
            current = audio_player.get_metrics()
            value["player"] = {
                "chunks_played": current.get("audio_chunks_played", 0),
                "chunks_dropped": current.get("audio_chunks_dropped", 0),
                "queue_size": current.get("audio_queue_size", 0),
                "is_playing": current.get("audio_is_playing", False),
            }
        if tts_pipeline is not None:
            current = tts_pipeline.get_metrics()
            value["pipeline"] = {
                "sentences_total": current.get("tts_pipeline_sentences_total", 0),
            }
        return value

    def mood() -> dict[str, Any]:
        value = dict(emotion.snapshot())
        value["sampled_at"] = datetime.now(timezone.utc).isoformat()
        value["ticks"] = emotion.get_metrics().get("mood_ticks")
        return value

    def data_label() -> dict[str, Any]:
        session_id = getattr(runner, "session_id", None)
        turn_id = getattr(runner, "last_turn_id", 0)
        if not isinstance(session_id, str) or not session_id or not turn_id:
            return {"latest_turn": None}
        return {
            "latest_turn": {
                "session_id": session_id,
                "turn_id": int(turn_id),
            },
        }

    providers: dict[str, Any] = {
        "runtime": runtime,
        "metrics": gpu_metrics,
        "llm": metrics.llm_snapshot,
        "features": features,
        "vram": lambda: {
            "used_mb": feature_manager.used_vram_mb(),
            "budget_mb": feature_manager._vram_budget_mb,
        },
        "filter": filter_snapshot,
        "tts": tts_snapshot,
        "mood": mood,
        "operations": control_plane.snapshot if control_plane is not None else lambda: {},
        "incidents": incident_log.snapshot if incident_log is not None else lambda: {},
        "decisions": decision_records.snapshot,
        "trajectories": trajectory_records.snapshot,
        "turn_journal": turn_journal.snapshot,
        "data_label": data_label,
    }
    optional = {
        "thought_engine": _snapshot_provider(self_talk_planner),
        "agent": _typed_snapshot_provider(agent_state),
        "agent_metrics": metrics.agent_snapshot,
        "world": _snapshot_and_metrics_provider(world_model),
        "self": _snapshot_and_metrics_provider(self_model),
        "capabilities": _snapshot_and_metrics_provider(capability_registry),
        "action_mock": _snapshot_and_metrics_provider(action_mock_loop),
        "director_v2_shadow": _snapshot_and_metrics_provider(director_v2_shadow),
        "director_v2_takeover": _snapshot_and_metrics_provider(director_v2_takeover),
        "goals": _typed_snapshot_provider(goal_manager),
        "goal_metrics": _metrics_provider(goal_manager),
        "relationships": _typed_snapshot_provider(relationship_manager),
        "relationship_metrics": _metrics_provider(relationship_manager),
    }
    providers.update({key: value for key, value in optional.items() if value is not None})
    return providers


def _required(payload: Any, key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _snapshot_provider(service: Any) -> Any:
    return service.snapshot if service is not None and hasattr(service, "snapshot") else None


def _typed_snapshot_provider(service: Any) -> Any:
    if service is None or not hasattr(service, "snapshot"):
        return None

    def provider() -> Any:
        value = service.snapshot()
        return value.to_dict() if hasattr(value, "to_dict") else value

    return provider


def _metrics_provider(service: Any) -> Any:
    return service.get_metrics if service is not None and hasattr(service, "get_metrics") else None


def _snapshot_and_metrics_provider(service: Any) -> Any:
    if service is None or not hasattr(service, "snapshot"):
        return None

    def provider() -> dict[str, Any]:
        value = service.snapshot()
        snapshot = value.to_dict() if hasattr(value, "to_dict") else value
        return {
            "snapshot": snapshot,
            "metrics": service.get_metrics() if hasattr(service, "get_metrics") else {},
        }

    return provider
