"""Orchestrator entry point (ARCHITECTURE 8.1, 13.2).

Phase 0: bootstrap config + logger + feature manager + state machine +
trigger manager + metrics + emergency stop + dashboard. CHƯA có LLM/TTS thật
(Phase 1+). Chạy được: dashboard mở ở localhost, toggle/metric/state/emergency.

Chạy:  python -m orchestrator.main   (Windows: nên Run as Administrator cho hotkey)
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import uvicorn

from dashboard.dashboard_server import DashboardServer
from orchestrator.config_loader import ConfigLoader
from orchestrator.emergency_stop import EmergencyStop
from orchestrator.event_bus import EventBus
from orchestrator.features import FeatureManager
from orchestrator.health_monitor import HealthMonitor
from orchestrator.logger import get_logger, setup_from_config
from orchestrator.metrics_collector import MetricsCollector
from orchestrator.migration_runner import MigrationRunner
from orchestrator.state_machine import ConversationStateMachine
from orchestrator.trigger_manager import TriggerManager

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


class Orchestrator:
    """Giữ mọi component + vòng đời (start/stop)."""

    def __init__(self, config_dir: Path = CONFIG_DIR) -> None:
        self.loader = ConfigLoader(config_dir)
        self.loader.load_all()
        setup_from_config(self.loader)
        self.log = get_logger("orchestrator")

        # migration (fail-safe: log nhưng không chặn dashboard mở)
        try:
            MigrationRunner.from_config(self.loader).initialize()
        except Exception as e:
            self.log.error("migration_init_failed", error=str(e))

        self.event_bus = EventBus.from_config(self.loader)
        self.metrics = MetricsCollector()
        self.features = FeatureManager.from_config(self.loader)
        self.state_machine = ConversationStateMachine.from_config(
            self.loader, event_bus=self.event_bus
        )
        self.triggers = TriggerManager.from_config(self.loader, event_bus=self.event_bus)

        # health monitor: poll các Service định kỳ (13.6)
        self.health = HealthMonitor.from_config(self.loader, event_bus=self.event_bus)
        self.health.register_service(self.triggers)

        # emergency stop → state machine.emergency_stop()
        async def _do_stop() -> None:
            with contextlib.suppress(Exception):
                await self.state_machine.emergency_stop()

        self.emergency = EmergencyStop.from_config(self.loader, callback=_do_stop)

        # metric hook: mỗi state_change tăng counter
        self._wire_metrics()

        self.dashboard = DashboardServer(
            feature_manager=self.features,
            state_machine=self.state_machine,
            trigger_manager=self.triggers,
            metrics=self.metrics,
            emergency_stop=self.emergency,
            health_monitor=self.health,
            push_interval_s=self.loader.get("system", "dashboard.push_interval_s", 1.0),
        )

    def _wire_metrics(self) -> None:
        original = self.state_machine._on_state_change

        async def wrapped(event):
            await original(event)
            last = self.state_machine.history(limit=1)
            if last:
                h = last[0]
                self.metrics.record_state_transition(h.from_state, h.to_state)

        self.state_machine._on_state_change = wrapped  # type: ignore[method-assign]

    async def serve(self) -> None:
        host = self.loader.get("system", "dashboard.host", "127.0.0.1")
        port = int(self.loader.get("system", "dashboard.port", 7860))

        self.loader.start_watching()
        self.emergency.bind()  # fail → chỉ log, vẫn chạy (không admin)
        self.health.start()
        self.dashboard.start_push_loop()

        self.log.info("orchestrator_ready", host=host, port=port,
                      hotkey_bound=self.emergency.is_bound)

        config = uvicorn.Config(self.dashboard.app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.log.info("orchestrator_shutdown")
        self.loader.stop_watching()
        self.emergency.unbind()
        await self.health.stop()
        await self.dashboard.stop_push_loop()
        await self.state_machine.shutdown()
        await self.event_bus.close()


def main() -> None:
    orch = Orchestrator()
    try:
        asyncio.run(orch.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
