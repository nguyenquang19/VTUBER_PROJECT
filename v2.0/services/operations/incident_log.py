"""Privacy-safe append-only incident ledger for live operations."""
from __future__ import annotations

import json
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interfaces.base import HealthStatus
from interfaces.operations import IncidentLogService
from orchestrator.logger import JsonlWriter
from services.data.sanitize import mask_pii


_SEVERITIES = {"info", "warning", "critical"}
_STATUSES = {"open", "monitoring", "resolved"}


class IncidentLog(IncidentLogService):
    service_id = "incident_log"

    def __init__(
        self, path: str | Path, *, recent_limit: int = 100,
        text_max_chars: int = 320, metrics: Any = None,
    ) -> None:
        self.path = Path(path)
        self._writer = JsonlWriter(self.path, source="live_incident")
        self._recent = deque(maxlen=max(1, int(recent_limit)))
        self._text_max_chars = max(1, int(text_max_chars))
        self._metrics = metrics
        self._running = False
        self._load_tail()

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None) -> "IncidentLog":
        return cls(
            loader.get("operations", "incident_log.file", "logs/operations/incidents.jsonl"),
            recent_limit=int(loader.get("operations", "incident_log.recent_limit", 100)),
            text_max_chars=int(loader.get("operations", "incident_log.text_max_chars", 320)),
            metrics=metrics,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, recent=len(self._recent))

    def get_metrics(self) -> dict[str, Any]:
        return {
            "incidents_recent": len(self._recent),
            "incidents_unresolved": sum(
                1 for item in self._latest_by_id().values()
                if item.get("status") != "resolved"
            ),
        }

    def record_incident(
        self, *, severity: str, component: str, summary: str,
        action: str, status: str = "open", evidence_refs: list[str] | None = None,
    ) -> str:
        clean_severity = severity if severity in _SEVERITIES else "warning"
        clean_status = status if status in _STATUSES else "open"
        incident_id = f"inc:{uuid.uuid4().hex}"
        record = {
            "schema_version": 1, "event": "incident_opened",
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": clean_severity,
            "component": self._clean(component), "summary": self._clean(summary),
            "action": self._clean(action), "status": clean_status,
            "evidence_refs": [self._clean(value) for value in (evidence_refs or [])[:10]],
        }
        self._append(record)
        return incident_id

    def resolve(self, incident_id: str, resolution: str) -> bool:
        latest = self._latest_by_id().get(str(incident_id))
        if latest is None or latest.get("status") == "resolved":
            return False
        self._append({
            "schema_version": 1, "event": "incident_resolved",
            "incident_id": str(incident_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": latest.get("severity", "warning"),
            "component": latest.get("component", "unknown"),
            "summary": "", "action": self._clean(resolution),
            "status": "resolved", "evidence_refs": [],
        })
        return True

    def snapshot(self) -> dict[str, Any]:
        latest = self._latest_by_id()
        return {
            "schema_version": 1,
            "unresolved": sum(1 for item in latest.values() if item.get("status") != "resolved"),
            "recent": list(self._recent),
        }

    def _append(self, record: dict[str, Any]) -> None:
        self._writer.write(record)
        self._recent.append(record)
        if self._metrics is not None and hasattr(self._metrics, "record_incident"):
            self._metrics.record_incident(
                str(record.get("severity", "warning")), str(record.get("status", "open")),
            )

    def _clean(self, value: Any) -> str:
        return mask_pii(" ".join(str(value).split())[:self._text_max_chars]) or ""

    def _load_tail(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-self._recent.maxlen:]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("incident_id"):
                self._recent.append(value)

    def _latest_by_id(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for item in self._recent:
            latest[str(item.get("incident_id", ""))] = item
        return latest
