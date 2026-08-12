from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orchestrator.metrics_collector import MetricsCollector
from services.operations.shutdown_coordinator import ShutdownCoordinator


async def test_shutdown_is_ordered_atomic_flushed_and_idempotent(tmp_path: Path) -> None:
    order: list[str] = []
    flushed = 0

    def step(name: str):
        async def run() -> None:
            order.append(name)
        return run

    def flush() -> None:
        nonlocal flushed
        flushed += 1

    path = tmp_path / "operations" / "snapshot.json"
    coordinator = ShutdownCoordinator(
        timeout_s=0.2, snapshot_path=path,
        snapshot_provider=lambda: {"agent": {"active_goal_ref": "goal:1"}},
        flush_callback=flush,
    )
    coordinator.register_step("input", step("input"))
    coordinator.register_step("speech", step("speech"))
    first = await coordinator.shutdown()
    second = await coordinator.shutdown()

    assert order == ["input", "speech"]
    assert flushed == 1
    assert first == second
    assert first["status"] == "completed"
    assert not path.with_suffix(".json.tmp").exists()
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["agent"]["active_goal_ref"] == "goal:1"


async def test_step_failure_and_timeout_do_not_skip_later_cleanup(tmp_path: Path) -> None:
    completed = False
    metrics = MetricsCollector()

    async def fail() -> None:
        raise RuntimeError("broken")

    async def hang() -> None:
        await asyncio.sleep(1)

    async def final() -> None:
        nonlocal completed
        completed = True

    coordinator = ShutdownCoordinator(
        timeout_s=0.01, snapshot_path=tmp_path / "snapshot.json",
        snapshot_provider=lambda: {}, metrics=metrics,
    )
    coordinator.register_step("fail", fail)
    coordinator.register_step("hang", hang)
    coordinator.register_step("final", final)
    report = await coordinator.shutdown()

    assert completed is True
    assert report["status"] == "completed_with_errors"
    assert report["errors"] == [
        {"step": "fail", "error": "RuntimeError"},
        {"step": "hang", "error": "timeout"},
    ]
    assert metrics.shutdown_snapshot()["final:completed"] == 1


async def test_health_transitions_to_stopped_after_shutdown(tmp_path: Path) -> None:
    coordinator = ShutdownCoordinator(
        timeout_s=0.1, snapshot_path=tmp_path / "snapshot.json",
        snapshot_provider=lambda: {},
    )
    await coordinator.start()
    assert (await coordinator.health_check()).is_ok
    await coordinator.stop()
    assert not (await coordinator.health_check()).is_ok
