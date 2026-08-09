from __future__ import annotations

import json
from pathlib import Path

from services.operations.standalone_snapshot import StandaloneSnapshotProvider


async def test_standalone_provider_reads_last_snapshot_and_forces_controls_offline(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot.json"
    audit = tmp_path / "audit.jsonl"
    snapshot.write_text(json.dumps({
        "agent": {"active_goal_ref": "goal:1", "open_threads": []},
        "runtime": {"running": False},
    }), encoding="utf-8")
    audit.write_text(json.dumps({
        "action": "pause", "target": "agent", "outcome": "completed",
    }) + "\n", encoding="utf-8")
    incidents = tmp_path / "incidents.jsonl"
    incidents.write_text(json.dumps({
        "incident_id": "inc:1", "status": "open", "severity": "warning",
    }) + "\n", encoding="utf-8")
    provider = StandaloneSnapshotProvider(
        snapshot_path=snapshot, audit_path=audit, incident_path=incidents,
    )
    await provider.start()
    value = await provider.snapshot()
    assert value["agent"]["active_goal_ref"] == "goal:1"
    assert value["runtime"]["online"] is False
    assert value["runtime"]["mode"] == "standalone"
    assert value["runtime"]["controls_available"] is False
    assert value["operations"]["audit"][0]["action"] == "pause"
    assert value["incidents"]["unresolved"] == 1
    assert (await provider.health_check()).is_ok


async def test_missing_or_invalid_snapshot_is_safe(tmp_path: Path) -> None:
    snapshot = tmp_path / "bad.json"
    snapshot.write_text("not-json", encoding="utf-8")
    provider = StandaloneSnapshotProvider(
        snapshot_path=snapshot, audit_path=tmp_path / "missing.jsonl",
    )
    value = await provider.snapshot()
    assert value["runtime"]["online"] is False
    assert value.get("agent") is None
