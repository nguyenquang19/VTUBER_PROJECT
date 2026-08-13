"""Construction helpers for optional runtime operations services."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any


def build_control_plane(
    *,
    enabled: bool,
    director_loop: Any,
    goal_manager: Any,
    pool: Any,
    loader: Any,
    metrics: Any,
) -> Any:
    if not enabled:
        return None

    from services.operations.control_plane import RuntimeControlPlane

    async def _pause_agent_actions() -> None:
        await director_loop.stop()

    async def _resume_agent_actions() -> None:
        await director_loop.start()

    def _action_queue() -> list[dict[str, Any]]:
        snapshot = goal_manager.snapshot()
        queue: list[dict[str, Any]] = []
        for goal in (
            *((snapshot.active,) if snapshot.active else ()),
            *snapshot.candidates,
            *snapshot.suspended,
        ):
            queue.append({
                "kind": "goal",
                "id": goal.goal_id,
                "status": goal.status.value,
                "priority": goal.priority,
                "reason": goal.reason,
            })
        queue.append({
            "kind": "chat_pool",
            "pending_count": len(getattr(pool, "_items", {})),
        })
        return queue

    return RuntimeControlPlane(
        pause_action=_pause_agent_actions,
        resume_action=_resume_agent_actions,
        queue_provider=_action_queue,
        audit_path=loader.get(
            "operations", "dashboard_standalone.operator_audit_file",
            "logs/operations/operator_audit.jsonl",
        ),
        metrics=metrics,
    )


def build_incident_log(*, enabled: bool, loader: Any, metrics: Any) -> Any:
    if not enabled:
        return None
    from services.operations.incident_log import IncidentLog
    return IncidentLog.from_loader(loader, metrics=metrics)


def start_dashboard(
    *,
    enabled: bool,
    loader: Any,
    feature_manager: Any,
    metrics: Any,
    filter_svc: Any,
    regenerator: Any,
    emotion: Any,
    runner: Any,
    agent_state: Any,
    goal_manager: Any,
    relationship_manager: Any,
    decision_records: Any,
    self_talk_planner: Any,
    control_plane: Any,
    incident_log: Any,
) -> tuple[asyncio.Task[Any] | None, dict[str, Any] | None, Any]:
    if not enabled:
        return None, None, None

    from dashboard.dashboard_server import DashboardServer
    server = DashboardServer(
        feature_manager=feature_manager, metrics=metrics,
        filter_svc=filter_svc, regenerator=regenerator,
        emotion=emotion, runner=runner,
        agent_state=agent_state,
        goal_manager=goal_manager,
        relationship_manager=relationship_manager,
        decision_records=decision_records,
        self_talk_planner=self_talk_planner,
        control_plane=control_plane,
        incident_log=incident_log,
        data_dir=loader.get("logging", "jsonl.dir", "logs"),
        host=loader.get("system", "dashboard.host", "127.0.0.1"),
        port=int(loader.get("system", "dashboard.port", 7860)),
        push_interval_s=float(loader.get(
            "system", "dashboard.push_interval_s", 1.0,
        )),
        gpu_metrics_command=str(loader.get(
            "system", "dashboard.gpu_metrics.command", "nvidia-smi",
        )),
        gpu_metrics_timeout_s=float(loader.get(
            "system", "dashboard.gpu_metrics.timeout_s", 1.0,
        )),
        gpu_metrics_refresh_s=float(loader.get(
            "system", "dashboard.gpu_metrics.refresh_s", 2.0,
        )),
    )
    task = asyncio.create_task(server.serve(), name="dashboard")
    return task, {"task": task, "server": server}, server


def build_health_supervisor(
    *,
    enabled: bool,
    loader: Any,
    metrics: Any,
    incident_log: Any,
    turn_lock: asyncio.Lock,
    llm_svc: Any,
    llama_process_manager: Any,
    router: Any,
    tts_svc: Any,
    dashboard_ref: dict[str, Any] | None,
    dashboard_server: Any,
) -> Any:
    if not enabled or not bool(loader.get(
        "operations", "health_supervisor.enabled", True,
    )):
        return None

    from services.operations.health_supervisor import HealthSupervisor

    def _record_recovery_incident(component: str, action: str, summary: str) -> None:
        incident_log.record_incident(
            severity="critical" if action in {"circuit_open", "restart_failed"} else "warning",
            component=component, summary=summary, action=action,
        )

    supervisor = HealthSupervisor.from_loader(
        loader, metrics=metrics, incident_sink=_record_recovery_incident,
    )

    async def _restart_llm() -> None:
        async with turn_lock:
            await llm_svc.stop()
            if llama_process_manager is not None:
                await llama_process_manager.restart()
            await llm_svc.start()

    async def _restart_input() -> None:
        await router.stop()
        await router.start()

    supervisor.register_target("llm_main", llm_svc.health_check, _restart_llm)
    supervisor.register_target("input_router", router.health_check, _restart_input)

    if tts_svc is not None:
        async def _restart_tts() -> None:
            await tts_svc.stop()
            await tts_svc.start()

        supervisor.register_target("tts", tts_svc.health_check, _restart_tts)

    if dashboard_ref is not None:
        async def _dashboard_health() -> Any:
            from interfaces.base import HealthStatus
            task = dashboard_ref.get("task")
            if task is None or task.done():
                return HealthStatus.unhealthy("dashboard", "serve task stopped")
            return HealthStatus.healthy("dashboard")

        async def _restart_dashboard() -> None:
            task = dashboard_ref.get("task")
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            dashboard_ref["task"] = asyncio.create_task(
                dashboard_ref["server"].serve(), name="dashboard",
            )

        supervisor.register_target(
            "dashboard", _dashboard_health, _restart_dashboard,
        )
        dashboard_server.health_supervisor = supervisor
    return supervisor


def build_emergency_controller(
    *,
    enabled: bool,
    loader: Any,
    metrics: Any,
    control_plane: Any,
    director_loop: Any,
    tts_pipeline: Any,
    audio_player: Any,
    goal_manager: Any,
    health_supervisor: Any,
    emergency_ref: dict[str, Any],
    dashboard_server: Any,
) -> Any:
    if not enabled:
        return None

    from services.operations.emergency_control import EmergencyController

    async def _emergency_pause_actions() -> None:
        if control_plane is not None:
            await control_plane.pause("emergency stop")
        else:
            await director_loop.stop()

    async def _emergency_resume_actions() -> None:
        if control_plane is not None:
            await control_plane.resume("emergency resume")
        else:
            await director_loop.start()

    async def _cancel_speech_now() -> None:
        if tts_pipeline is not None:
            await tts_pipeline.cancel_all()
        elif audio_player is not None:
            await audio_player.cancel_all()

    async def _prune_expired_goals() -> None:
        goal_manager.snapshot()

    controller = EmergencyController(
        pause_actions=_emergency_pause_actions,
        resume_actions=_emergency_resume_actions,
        cancel_speech=_cancel_speech_now,
        prune_stale_work=_prune_expired_goals,
        pause_recovery=(
            health_supervisor.pause_recovery if health_supervisor is not None else None
        ),
        resume_recovery=(
            health_supervisor.resume_recovery if health_supervisor is not None else None
        ),
        audit=(
            control_plane.record_operator_action if control_plane is not None else None
        ),
        metrics=metrics,
        reason_max_chars=int(loader.get(
            "operations", "emergency_stop.reason_max_chars", 240,
        )),
    )
    emergency_ref["controller"] = controller
    if dashboard_server is not None:
        dashboard_server.emergency_controller = controller
    return controller


def configure_shutdown_coordinator(
    *,
    enabled: bool,
    loader: Any,
    runtime: Any,
    animation: Any,
    metrics: Any,
) -> None:
    """Attach the operations shutdown coordinator after runtime construction."""
    if not enabled:
        return

    from orchestrator.logger import flush_logging
    from services.operations.shutdown_coordinator import ShutdownCoordinator

    coordinator = ShutdownCoordinator.from_loader(
        loader,
        snapshot_provider=runtime.operations_snapshot,
        flush_callback=flush_logging,
        metrics=metrics,
    )
    for name, callback in runtime.shutdown_steps():
        coordinator.register_step(name, callback)
    coordinator.register_step("animation", animation.stop)
    runtime.set_shutdown_coordinator(coordinator)
