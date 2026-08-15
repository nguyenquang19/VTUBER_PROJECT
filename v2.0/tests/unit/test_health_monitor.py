"""Test HealthMonitor (ARCHITECTURE 13.2, 13.6)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from interfaces.base import HealthState, HealthStatus
from orchestrator.config_loader import ConfigLoader
from orchestrator.event_bus import EventBus
from orchestrator.health_monitor import HealthMonitor

REPO_ROOT = Path(__file__).resolve().parents[2]


def healthy_check(service_id: str):
    async def _c() -> HealthStatus:
        return HealthStatus.healthy(service_id)
    return _c


def unhealthy_check(service_id: str, msg: str = "down"):
    async def _c() -> HealthStatus:
        return HealthStatus.unhealthy(service_id, msg)
    return _c


class FakeService:
    def __init__(self, service_id: str, healthy: bool = True) -> None:
        self.service_id = service_id
        self._healthy = healthy

    async def health_check(self) -> HealthStatus:
        if self._healthy:
            return HealthStatus.healthy(self.service_id)
        return HealthStatus.unhealthy(self.service_id, "broken")


class TestRegistration:
    def test_register_and_unregister(self) -> None:
        hm = HealthMonitor()
        hm.register("a", healthy_check("a"))
        assert "a" in hm._checks
        hm.unregister("a")
        assert "a" not in hm._checks

    def test_register_service_object(self) -> None:
        hm = HealthMonitor()
        hm.register_service(FakeService("trigger_manager"))
        assert "trigger_manager" in hm._checks


class TestCheckOnce:
    async def test_all_healthy(self) -> None:
        hm = HealthMonitor()
        hm.register("a", healthy_check("a"))
        hm.register("b", healthy_check("b"))
        results = await hm.check_once()
        assert results["a"].state is HealthState.HEALTHY
        assert results["b"].state is HealthState.HEALTHY
        assert hm.is_all_healthy() is True

    async def test_unhealthy_detected(self) -> None:
        hm = HealthMonitor()
        hm.register("a", unhealthy_check("a"))
        results = await hm.check_once()
        assert results["a"].state is HealthState.UNHEALTHY
        assert hm.is_all_healthy() is False

    async def test_check_exception_becomes_unhealthy(self) -> None:
        hm = HealthMonitor()

        async def boom() -> HealthStatus:
            raise RuntimeError("probe crashed")

        hm.register("a", boom)
        results = await hm.check_once()
        assert results["a"].state is HealthState.UNHEALTHY
        assert "raised" in results["a"].message

    async def test_check_timeout_becomes_unhealthy(self) -> None:
        hm = HealthMonitor(check_timeout_s=0.05)

        async def slow() -> HealthStatus:
            await asyncio.sleep(1.0)
            return HealthStatus.healthy("a")

        hm.register("a", slow)
        results = await hm.check_once()
        assert results["a"].state is HealthState.UNHEALTHY
        assert "timeout" in results["a"].message

    async def test_one_bad_check_does_not_block_others(self) -> None:
        hm = HealthMonitor()

        async def boom() -> HealthStatus:
            raise RuntimeError("x")

        hm.register("bad", boom)
        hm.register("good", healthy_check("good"))
        results = await hm.check_once()
        assert results["bad"].state is HealthState.UNHEALTHY
        assert results["good"].state is HealthState.HEALTHY

    async def test_rounds_counter(self) -> None:
        hm = HealthMonitor()
        hm.register("a", healthy_check("a"))
        await hm.check_once()
        await hm.check_once()
        assert hm.rounds_completed == 2


class TestChangeDetection:
    async def test_publishes_only_on_change(self) -> None:
        bus = EventBus()
        sub = bus.subscribe("health_status")
        svc = FakeService("a", healthy=True)
        hm = HealthMonitor(event_bus=bus)
        hm.register_service(svc)

        await hm.check_once()  # HEALTHY (first time — no previous → emit? prev None → emit)
        # first observation: prev is None → considered change → emitted
        first = await sub.get()
        assert first.payload["state"] == "healthy"

        await hm.check_once()  # vẫn HEALTHY → không emit
        svc._healthy = False
        await hm.check_once()  # đổi sang UNHEALTHY → emit
        change = await sub.get()
        assert change.payload["state"] == "unhealthy"

    async def test_snapshot_reflects_latest(self) -> None:
        svc = FakeService("a", healthy=True)
        hm = HealthMonitor()
        hm.register_service(svc)
        await hm.check_once()
        assert hm.snapshot()["a"]["state"] == "healthy"
        svc._healthy = False
        await hm.check_once()
        assert hm.snapshot()["a"]["state"] == "unhealthy"


class TestLoop:
    async def test_start_stop_loop_runs_checks(self) -> None:
        hm = HealthMonitor(interval_s=0.02)
        hm.register("a", healthy_check("a"))
        hm.start()
        await asyncio.sleep(0.1)
        await hm.stop()
        assert hm.rounds_completed >= 2

    async def test_stop_without_start_is_safe(self) -> None:
        hm = HealthMonitor()
        await hm.stop()

    async def test_loop_survives_check_exception(self) -> None:
        hm = HealthMonitor(interval_s=0.02)

        async def boom() -> HealthStatus:
            raise RuntimeError("x")

        hm.register("a", boom)
        hm.start()
        await asyncio.sleep(0.1)
        await hm.stop()
        assert hm.rounds_completed >= 2  # vẫn chạy dù check luôn raise


class TestFromConfig:
    def test_reads_health_config(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        hm = HealthMonitor.from_config(loader)
        assert hm.interval_s == 10.0
        assert hm.check_timeout_s == 5.0
