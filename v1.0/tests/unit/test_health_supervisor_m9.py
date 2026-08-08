from __future__ import annotations

from interfaces.base import HealthStatus
from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.operations.health_supervisor import HealthSupervisor, SupervisorPolicy


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _policy(**changes) -> SupervisorPolicy:
    values = {
        "interval_s": 0.01,
        "check_timeout_s": 0.1,
        "restart_timeout_s": 0.1,
        "unhealthy_threshold": 2,
        "max_restart_attempts": 3,
        "restart_window_s": 60,
        "backoff_initial_s": 2,
        "backoff_max_s": 8,
        "recovery_mode": "bounded_auto_restart",
    }
    values.update(changes)
    return SupervisorPolicy(**values)


async def test_restarts_only_after_configured_failure_threshold() -> None:
    restarts = 0

    async def unhealthy() -> HealthStatus:
        return HealthStatus.unhealthy("llm_main", "down")

    async def restart() -> None:
        nonlocal restarts
        restarts += 1

    supervisor = HealthSupervisor(_policy())
    supervisor.register_target("llm_main", unhealthy, restart)
    await supervisor.check_once()
    assert restarts == 0
    await supervisor.check_once()
    assert restarts == 1
    assert supervisor.snapshot()["targets"]["llm_main"]["last_action"] == "restarted"


async def test_backoff_and_circuit_breaker_prevent_infinite_restart_loop() -> None:
    clock = Clock()
    restarts = 0

    async def unhealthy() -> HealthStatus:
        return HealthStatus.unhealthy("tts", "down")

    async def restart() -> None:
        nonlocal restarts
        restarts += 1

    supervisor = HealthSupervisor(
        _policy(unhealthy_threshold=1, max_restart_attempts=2), clock=clock,
    )
    supervisor.register_target("tts", unhealthy, restart)
    await supervisor.check_once()
    assert restarts == 1
    await supervisor.check_once()
    assert restarts == 1
    assert supervisor.snapshot()["targets"]["tts"]["last_action"] == "backoff"
    clock.value = 2
    await supervisor.check_once()
    assert restarts == 2
    clock.value = 6
    await supervisor.check_once()
    target = supervisor.snapshot()["targets"]["tts"]
    assert restarts == 2
    assert target["circuit_open"] is True
    assert target["last_action"] == "circuit_open"


async def test_alert_only_never_calls_restart() -> None:
    called = False

    async def unhealthy() -> HealthStatus:
        return HealthStatus.unhealthy("input", "down")

    async def restart() -> None:
        nonlocal called
        called = True

    supervisor = HealthSupervisor(_policy(recovery_mode="alert_only", unhealthy_threshold=1))
    supervisor.register_target("input", unhealthy, restart)
    await supervisor.check_once()
    assert called is False
    assert supervisor.snapshot()["targets"]["input"]["last_action"] == "operator_alert"


async def test_pause_blocks_recovery_and_resume_does_not_reset_circuit() -> None:
    restarts = 0

    async def unhealthy() -> HealthStatus:
        return HealthStatus.unhealthy("dashboard", "down")

    async def restart() -> None:
        nonlocal restarts
        restarts += 1

    supervisor = HealthSupervisor(_policy(unhealthy_threshold=1))
    supervisor.register_target("dashboard", unhealthy, restart)
    supervisor.pause_recovery("emergency_stop")
    await supervisor.check_once()
    assert restarts == 0
    supervisor.resume_recovery()
    await supervisor.check_once()
    assert restarts == 1


async def test_probe_timeout_isolated_and_metrics_recorded() -> None:
    metrics = MetricsCollector()

    async def stuck() -> HealthStatus:
        import asyncio
        await asyncio.sleep(1)
        return HealthStatus.healthy("stuck")

    async def healthy() -> HealthStatus:
        return HealthStatus.healthy("good")

    supervisor = HealthSupervisor(
        _policy(check_timeout_s=0.01, unhealthy_threshold=1, recovery_mode="alert_only"),
        metrics=metrics,
    )
    supervisor.register_target("stuck", stuck)
    supervisor.register_target("good", healthy)
    results = await supervisor.check_once()
    assert results["stuck"].is_ok is False
    assert results["good"].is_ok is True
    assert metrics.health_supervisor_snapshot()["stuck:unhealthy"] == 1


async def test_service_lifecycle_is_idempotent() -> None:
    supervisor = HealthSupervisor(_policy())
    await supervisor.start()
    await supervisor.start()
    assert (await supervisor.health_check()).is_ok
    await supervisor.stop()
    await supervisor.stop()
    assert not (await supervisor.health_check()).is_ok


def test_real_config_selects_bounded_auto_restart() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    policy = SupervisorPolicy.from_loader(loader)
    assert policy.recovery_mode == "bounded_auto_restart"
    assert policy.max_restart_attempts == 3
