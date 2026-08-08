"""Read-only dashboard snapshot provider used when StreamRuntime is offline."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from interfaces.base import HealthStatus
from interfaces.operations import OperationsSnapshotService


class StandaloneSnapshotProvider(OperationsSnapshotService):
    service_id = "standalone_snapshot_provider"

    def __init__(
        self,
        *,
        snapshot_path: str | Path,
        audit_path: str | Path,
        audit_limit: int = 50,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.audit_path = Path(audit_path)
        self.audit_limit = max(1, int(audit_limit))
        self._running = False

    @classmethod
    def from_loader(cls, loader: Any) -> "StandaloneSnapshotProvider":
        return cls(
            snapshot_path=loader.get(
                "operations", "shutdown.snapshot_file",
                "logs/operations/last_runtime_snapshot.json",
            ),
            audit_path=loader.get(
                "operations", "dashboard_standalone.operator_audit_file",
                "logs/operations/operator_audit.jsonl",
            ),
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, snapshot_exists=self.snapshot_path.exists(),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {"standalone_snapshot_exists": self.snapshot_path.exists()}

    async def snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_snapshot)

    def _read_snapshot(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        try:
            loaded = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, ValueError, TypeError):
            value = {}
        audit = _tail_jsonl(self.audit_path, self.audit_limit)
        operations = dict(value.get("operations") or {})
        operations.update({
            "available": False,
            "paused": True,
            "pause_reason": "runtime_offline",
            "action_queue": [],
            "audit": audit,
        })
        value["operations"] = operations
        value["runtime"] = {
            **dict(value.get("runtime") or {}),
            "online": False,
            "mode": "standalone",
            "controls_available": False,
        }
        return value


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    output: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            output.append(value)
    return output
