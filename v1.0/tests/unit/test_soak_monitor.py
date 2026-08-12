from __future__ import annotations

from pathlib import Path

import pytest

from services.operations.soak_monitor import ControlledSoakMonitor, SoakConfig


def _config(tmp_path: Path, **overrides) -> SoakConfig:
    values = {
        "duration_s": 0.12, "sample_interval_s": 0.01, "input_rate_hz": 200,
        "queue_capacity": 16, "progress_timeout_s": 1,
        "latency_sample_max": 100, "max_memory_growth_mb": 16,
        "max_queue_growth": 16, "max_error_rate": 0,
        "latency_p95_budget_ms": 100, "report_file": tmp_path / "soak.json",
    }
    values.update(overrides)
    return SoakConfig(**values)


@pytest.mark.asyncio
async def test_controlled_soak_passes_integrity_and_writes_report(tmp_path: Path) -> None:
    monitor = ControlledSoakMonitor(_config(tmp_path))
    report = await monitor.run()

    assert report["passed"] is True
    assert report["measurements"]["produced"] == report["measurements"]["consumed"]
    assert report["measurements"]["checksum_match"] is True
    assert (tmp_path / "soak.json").exists()
    assert monitor.snapshot()["running"] is False


@pytest.mark.asyncio
async def test_hook_errors_fail_error_rate_gate_without_data_loss(tmp_path: Path) -> None:
    async def broken(_sequence: int) -> None:
        raise RuntimeError("controlled failure")

    monitor = ControlledSoakMonitor(
        _config(tmp_path, max_error_rate=0.1), event_hook=broken,
    )
    report = await monitor.run()

    assert report["passed"] is False
    assert report["gates"]["error_rate"] is False
    assert report["gates"]["data_integrity"] is True
