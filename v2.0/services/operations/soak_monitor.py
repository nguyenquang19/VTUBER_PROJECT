"""Controlled async soak workload with resource and integrity gates."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import tracemalloc
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from interfaces.base import HealthStatus
from interfaces.operations import SoakMonitorService


EventHook = Callable[[int], Awaitable[None]]
_STOP = object()


@dataclass(frozen=True)
class SoakConfig:
    duration_s: float
    sample_interval_s: float
    input_rate_hz: float
    queue_capacity: int
    progress_timeout_s: float
    latency_sample_max: int
    max_memory_growth_mb: float
    max_queue_growth: int
    max_error_rate: float
    latency_p95_budget_ms: float
    report_file: Path

    @classmethod
    def from_loader(cls, loader: Any) -> "SoakConfig":
        prefix = "soak."
        return cls(
            duration_s=float(loader.get("operations", prefix + "duration_s", 7200)),
            sample_interval_s=float(loader.get("operations", prefix + "sample_interval_s", 5)),
            input_rate_hz=float(loader.get("operations", prefix + "input_rate_hz", 20)),
            queue_capacity=int(loader.get("operations", prefix + "queue_capacity", 256)),
            progress_timeout_s=float(loader.get("operations", prefix + "progress_timeout_s", 30)),
            latency_sample_max=int(loader.get("operations", prefix + "latency_sample_max", 20000)),
            max_memory_growth_mb=float(loader.get("operations", prefix + "max_memory_growth_mb", 128)),
            max_queue_growth=int(loader.get("operations", prefix + "max_queue_growth", 20)),
            max_error_rate=float(loader.get("operations", prefix + "max_error_rate", 0.01)),
            latency_p95_budget_ms=float(loader.get(
                "operations", prefix + "latency_p95_budget_ms", 1500,
            )),
            report_file=Path(loader.get(
                "operations", prefix + "report_file",
                "docs/baselines/m9_live_operations.json",
            )),
        )


class ControlledSoakMonitor(SoakMonitorService):
    service_id = "controlled_soak_monitor"

    def __init__(
        self, config: SoakConfig, *, event_hook: EventHook | None = None,
    ) -> None:
        self.config = config
        self._event_hook = event_hook
        self._running = False
        self._snapshot: dict[str, Any] = {
            "running": False, "produced": 0, "consumed": 0,
            "queue_size": 0, "queue_high_water": 0, "errors": 0,
        }

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, **self._snapshot)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "soak_running": self._running,
            "soak_produced_total": self._snapshot.get("produced", 0),
            "soak_consumed_total": self._snapshot.get("consumed", 0),
            "soak_errors_total": self._snapshot.get("errors", 0),
            "soak_queue_high_water": self._snapshot.get("queue_high_water", 0),
        }

    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)

    async def run(self, duration_s: float | None = None) -> dict[str, Any]:
        duration = self.config.duration_s if duration_s is None else float(duration_s)
        if duration <= 0:
            raise ValueError("duration_s must be positive")
        await self.start()
        started_wall = datetime.now(timezone.utc)
        started_mono = time.perf_counter()
        deadline = started_mono + duration
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(1, self.config.queue_capacity))
        latencies: deque[float] = deque(maxlen=max(1, self.config.latency_sample_max))
        produced = consumed = errors = 0
        produced_sum = consumed_sum = 0
        queue_high_water = 0
        last_progress = started_mono
        stalled = False
        baseline_queue = queue.qsize()
        owns_tracing = not tracemalloc.is_tracing()
        if owns_tracing:
            tracemalloc.start()
        baseline_memory, _ = tracemalloc.get_traced_memory()
        peak_memory = baseline_memory

        async def producer() -> None:
            nonlocal produced, produced_sum, queue_high_water, last_progress
            interval = 1.0 / max(0.1, self.config.input_rate_hz)
            next_at = time.perf_counter()
            sequence = 0
            while time.perf_counter() < deadline:
                sequence += 1
                created = time.perf_counter()
                await queue.put((sequence, created))
                produced += 1
                produced_sum = (produced_sum + sequence) & 0xFFFFFFFFFFFFFFFF
                queue_high_water = max(queue_high_water, queue.qsize())
                last_progress = time.perf_counter()
                next_at += interval
                await asyncio.sleep(max(0.0, next_at - time.perf_counter()))
            await queue.put(_STOP)

        async def consumer() -> None:
            nonlocal consumed, consumed_sum, errors, last_progress
            while True:
                item = await queue.get()
                try:
                    if item is _STOP:
                        return
                    sequence, created = item
                    try:
                        if self._event_hook is not None:
                            await self._event_hook(sequence)
                    except Exception:
                        errors += 1
                    consumed += 1
                    consumed_sum = (consumed_sum + sequence) & 0xFFFFFFFFFFFFFFFF
                    latencies.append((time.perf_counter() - created) * 1000.0)
                    last_progress = time.perf_counter()
                finally:
                    queue.task_done()

        async def sampler() -> None:
            nonlocal peak_memory, stalled
            while self._running:
                await asyncio.sleep(max(0.01, self.config.sample_interval_s))
                current, peak = tracemalloc.get_traced_memory()
                peak_memory = max(peak_memory, peak, current)
                elapsed = time.perf_counter() - started_mono
                self._snapshot = {
                    "running": True, "elapsed_s": round(elapsed, 3),
                    "produced": produced, "consumed": consumed,
                    "queue_size": queue.qsize(), "queue_high_water": queue_high_water,
                    "errors": errors,
                    "memory_current_mb": round(current / 1024 / 1024, 3),
                }
                if time.perf_counter() - last_progress > self.config.progress_timeout_s:
                    stalled = True

        sampler_task = asyncio.create_task(sampler(), name="soak_sampler")
        runtime_error: str | None = None
        try:
            await asyncio.wait_for(
                asyncio.gather(producer(), consumer()),
                timeout=duration + self.config.progress_timeout_s,
            )
        except Exception as exc:
            runtime_error = type(exc).__name__
            errors += 1
        finally:
            self._running = False
            sampler_task.cancel()
            try:
                await sampler_task
            except (asyncio.CancelledError, TimeoutError):
                pass
            current_memory, measured_peak = tracemalloc.get_traced_memory()
            peak_memory = max(peak_memory, measured_peak, current_memory)
            if owns_tracing:
                tracemalloc.stop()

        ended_wall = datetime.now(timezone.utc)
        if runtime_error is None and stalled:
            runtime_error = "ProgressTimeout"
        elapsed_s = time.perf_counter() - started_mono
        ordered_latencies = sorted(latencies)
        p95_index = max(0, math.ceil(len(ordered_latencies) * 0.95) - 1)
        latency_p95 = ordered_latencies[p95_index] if ordered_latencies else 0.0
        memory_growth_mb = max(0.0, (current_memory - baseline_memory) / 1024 / 1024)
        queue_growth = max(0, queue_high_water - baseline_queue)
        error_rate = errors / max(1, consumed)
        data_loss = max(0, produced - consumed)
        checksum_match = produced_sum == consumed_sum
        gates = {
            "duration_reached": elapsed_s >= duration,
            "no_deadlock": runtime_error is None,
            "memory_growth": memory_growth_mb <= self.config.max_memory_growth_mb,
            "queue_growth": queue_growth <= self.config.max_queue_growth,
            "latency_p95": latency_p95 <= self.config.latency_p95_budget_ms,
            "error_rate": error_rate <= self.config.max_error_rate,
            "data_integrity": data_loss == 0 and checksum_match,
        }
        report = {
            "schema_version": 1,
            "workload": "controlled_async_input",
            "started_at": started_wall.isoformat(), "ended_at": ended_wall.isoformat(),
            "configured_duration_s": duration, "elapsed_s": round(elapsed_s, 3),
            "measurements": {
                "produced": produced, "consumed": consumed, "data_loss": data_loss,
                "checksum_match": checksum_match, "errors": errors,
                "error_rate": error_rate, "queue_high_water": queue_high_water,
                "queue_growth": queue_growth, "latency_p95_ms": round(latency_p95, 3),
                "latency_samples": len(latencies),
                "memory_growth_mb": round(memory_growth_mb, 3),
                "memory_peak_mb": round(peak_memory / 1024 / 1024, 3),
                "runtime_error": runtime_error,
            },
            "limits": {
                "max_memory_growth_mb": self.config.max_memory_growth_mb,
                "max_queue_growth": self.config.max_queue_growth,
                "max_error_rate": self.config.max_error_rate,
                "latency_p95_budget_ms": self.config.latency_p95_budget_ms,
            },
            "gates": gates, "passed": all(gates.values()),
        }
        self._snapshot = {"running": False, **report["measurements"], "passed": report["passed"]}
        await asyncio.to_thread(self._write_report, report)
        return report

    def _write_report(self, report: dict[str, Any]) -> None:
        path = self.config.report_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
