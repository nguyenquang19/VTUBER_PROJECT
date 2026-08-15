from __future__ import annotations

import json
from pathlib import Path

from orchestrator.metrics_collector import MetricsCollector
from services.operations.incident_log import IncidentLog


def test_incident_log_masks_pii_and_resolves_append_only(tmp_path: Path) -> None:
    path = tmp_path / "incidents.jsonl"
    log = IncidentLog(path, recent_limit=10)

    incident_id = log.record_incident(
        severity="critical", component="tts",
        summary="email user@example.com failed", action="operator alert",
        evidence_refs=["log:42"],
    )
    assert log.snapshot()["unresolved"] == 1
    assert log.resolve(incident_id, "device reset") is True
    assert log.resolve(incident_id, "again") is False
    assert log.snapshot()["unresolved"] == 0

    text = path.read_text(encoding="utf-8")
    assert "user@example.com" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert [row["event"] for row in rows] == ["incident_opened", "incident_resolved"]
    assert all(row["schema_version"] == 1 for row in rows)


def test_incident_log_reloads_recent_state(tmp_path: Path) -> None:
    path = tmp_path / "incidents.jsonl"
    first = IncidentLog(path)
    incident_id = first.record_incident(
        severity="warning", component="dashboard", summary="offline", action="restart",
    )
    second = IncidentLog(path)
    assert second.snapshot()["unresolved"] == 1
    assert second.resolve(incident_id, "restored") is True


def test_incident_metric_is_observable(tmp_path: Path) -> None:
    metrics = MetricsCollector()
    log = IncidentLog(tmp_path / "incidents.jsonl", metrics=metrics)
    log.record_incident(
        severity="warning", component="input", summary="offline", action="alert",
    )
    assert metrics.incident_snapshot() == {"warning:open": 1}
