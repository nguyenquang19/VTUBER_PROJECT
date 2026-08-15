from __future__ import annotations

import json
from pathlib import Path

from orchestrator.metrics_collector import MetricsCollector
from services.operations.control_plane import RuntimeControlPlane


async def test_pause_resume_are_idempotent_audited_and_observable(tmp_path: Path) -> None:
    actions: list[str] = []

    async def pause() -> None:
        actions.append("pause")

    async def resume() -> None:
        actions.append("resume")

    metrics = MetricsCollector()
    audit = tmp_path / "operator_audit.jsonl"
    control = RuntimeControlPlane(
        pause_action=pause, resume_action=resume,
        queue_provider=lambda: [{"kind": "goal", "id": "goal:1"}],
        audit_path=audit, metrics=metrics,
    )
    await control.start()
    assert await control.pause("operator requested") is True
    assert await control.pause("again") is True
    assert await control.resume("continue") is True
    assert actions == ["pause", "resume"]
    assert control.snapshot()["paused"] is False
    assert control.snapshot()["action_queue"][0]["id"] == "goal:1"
    assert metrics.operator_control_snapshot()["pause:completed"] == 1
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 3


async def test_operator_audit_masks_pii_before_dashboard_projection(tmp_path: Path) -> None:
    async def noop() -> None:
        return None

    control = RuntimeControlPlane(
        pause_action=noop, resume_action=noop, queue_provider=list,
        audit_path=tmp_path / "audit.jsonl",
    )
    control.record_operator_action("note", "private@example.com", "completed")
    encoded = json.dumps(control.snapshot(), ensure_ascii=False)
    assert "private@example.com" not in encoded
    assert "[PII]" in encoded
