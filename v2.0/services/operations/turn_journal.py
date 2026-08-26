"""Canonical bounded, privacy-safe turn lineage journal for live operations."""
from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.base import HealthStatus
from interfaces.operations import (
    TurnJournalEvent,
    TurnJournalService,
    TurnJournalStage,
    TurnLineageRecord,
)


@dataclass(frozen=True)
class TurnJournalConfig:
    max_lineages: int
    max_events_per_lineage: int
    max_reason_codes: int
    max_evidence_refs: int
    max_label_chars: int
    max_projection_bytes: int

    @classmethod
    def from_loader(cls, loader: Any) -> "TurnJournalConfig":
        base = "turn_journal"
        return cls(
            max_lineages=_positive_int(
                loader.get("operations", f"{base}.max_lineages", 512),
                "turn_journal.max_lineages",
            ),
            max_events_per_lineage=_positive_int(
                loader.get("operations", f"{base}.max_events_per_lineage", 32),
                "turn_journal.max_events_per_lineage",
            ),
            max_reason_codes=_positive_int(
                loader.get("operations", f"{base}.max_reason_codes", 8),
                "turn_journal.max_reason_codes",
            ),
            max_evidence_refs=_positive_int(
                loader.get("operations", f"{base}.max_evidence_refs", 16),
                "turn_journal.max_evidence_refs",
            ),
            max_label_chars=_positive_int(
                loader.get("operations", f"{base}.max_label_chars", 160),
                "turn_journal.max_label_chars",
            ),
            max_projection_bytes=_positive_int(
                loader.get("operations", f"{base}.max_projection_bytes", 16384),
                "turn_journal.max_projection_bytes",
            ),
        )


_ID_FIELDS = (
    "session_id", "event_id", "opportunity_id", "decision_id", "attempt_id",
    "turn_id", "request_id", "transaction_id", "outcome_ref", "continuity_id",
    "action_id",
)
_STABLE_ID_FIELDS = (
    "session_id", "event_id", "opportunity_id", "decision_id",
    "transaction_id", "outcome_ref", "continuity_id",
)
_TERMINAL = {TurnJournalStage.OUTCOME_COMMITTED, TurnJournalStage.OUTCOME_RELEASED}


class TurnJournal(TurnJournalService):
    """Materialize sanitized lineage only; never declares delivery or state success."""

    service_id = "turn_journal"

    def __init__(
        self,
        config: TurnJournalConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, TurnJournalConfig):
            raise ValueError("config must be TurnJournalConfig")
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: OrderedDict[str, TurnLineageRecord] = OrderedDict()
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "TurnJournal":
        return cls(TurnJournalConfig.from_loader(loader), **kwargs)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            retained=len(self._records),
            inconsistencies=self._counts.get("inconsistent", 0),
        )

    def append(self, event: TurnJournalEvent) -> TurnLineageRecord:
        if not isinstance(event, TurnJournalEvent):
            raise ValueError("event must be TurnJournalEvent")
        self._validate_bounds(event)
        existing = self._records.get(event.lineage_id)
        if existing is not None and event in existing.events:
            self._record("duplicate")
            return existing
        try:
            self._validate_consistency(existing, event)
        except ValueError:
            self._record("inconsistent")
            raise

        events = (() if existing is None else existing.events) + (event,)
        if len(events) > self.config.max_events_per_lineage:
            events = events[-self.config.max_events_per_lineage:]
            self._record("event_evicted")
        created = event.occurred_at if existing is None else existing.created_at
        record = TurnLineageRecord(
            schema_version=1,
            lineage_id=event.lineage_id,
            created_at=created,
            updated_at=max(item.occurred_at for item in events),
            events=events,
        )
        self._records[event.lineage_id] = record
        self._records.move_to_end(event.lineage_id)
        while len(self._records) > self.config.max_lineages:
            self._records.popitem(last=False)
            self._record("lineage_evicted")
        self._record("appended")
        self._record(f"stage:{event.stage.value.lower()}")
        return record

    def get(self, lineage_id: str) -> TurnLineageRecord | None:
        key = self._label(lineage_id, "lineage_id")
        return self._records.get(key)

    def recent(self, limit: int | None = None) -> tuple[TurnLineageRecord, ...]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("turn journal recent limit must be a positive integer")
        values = tuple(self._records.values())
        return values if limit is None else values[-limit:]

    def projection(self, lineage_id: str, projection_kind: str) -> str | None:
        record = self.get(lineage_id)
        kind = self._label(projection_kind, "projection_kind")
        if record is None:
            return None
        for event in reversed(record.events):
            if event.projection_kind == kind and event.projection_json is not None:
                return event.projection_json
        return None

    def snapshot(self) -> dict[str, Any]:
        recent = list(self._records.values())
        value = {
            "schema_version": 1,
            "running": self._running,
            "retained": len(recent),
            "counts": dict(sorted(self._counts.items())),
            "recent": [item.to_dict() for item in reversed(recent)],
        }
        return json.loads(json.dumps(value, ensure_ascii=False))

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"turn_journal_{name.replace(':', '_')}_total": count
            for name, count in sorted(self._counts.items())
        }

    def _validate_bounds(self, event: TurnJournalEvent) -> None:
        self._label(event.lineage_id, "lineage_id")
        for name in (
            *_ID_FIELDS, "owner", "mode", "terminal_state", "projection_kind",
        ):
            value = getattr(event, name)
            if value is not None:
                self._label(value, name)
        if len(event.reason_codes) > self.config.max_reason_codes:
            raise ValueError("turn journal reason_codes exceed configured bound")
        if len(event.evidence_refs) > self.config.max_evidence_refs:
            raise ValueError("turn journal evidence_refs exceed configured bound")
        for value in (*event.reason_codes, *event.evidence_refs):
            self._label(value, "reason/evidence")
        if event.projection_json is not None:
            rendered = event.projection_json.encode("utf-8")
            if len(rendered) > self.config.max_projection_bytes:
                raise ValueError("turn journal projection exceeds configured bound")
            lowered = event.projection_json.casefold()
            forbidden = (
                "chain_of_thought", "raw_prompt", "raw_memory", "secret",
                "viewer_name", "user_name",
            )
            if any(token in lowered for token in forbidden):
                raise ValueError("turn journal projection contains forbidden data")

    def _validate_consistency(
        self,
        existing: TurnLineageRecord | None,
        event: TurnJournalEvent,
    ) -> None:
        if existing is None:
            return
        known: dict[str, str] = {}
        terminal: TurnJournalStage | None = None
        for prior in existing.events:
            for name in _STABLE_ID_FIELDS:
                value = getattr(prior, name)
                if value is not None:
                    known.setdefault(name, value)
            if prior.stage in _TERMINAL:
                terminal = prior.stage
        for name, expected in known.items():
            actual = getattr(event, name)
            if actual is not None and actual != expected:
                raise ValueError(f"turn journal {name} conflicts with existing lineage")
        if terminal is TurnJournalStage.OUTCOME_RELEASED and event.stage is not terminal:
            raise ValueError("released lineage cannot accept another stage")
        if terminal is TurnJournalStage.OUTCOME_COMMITTED and event.stage not in {
            TurnJournalStage.OUTCOME_COMMITTED,
            TurnJournalStage.CONTINUITY_COMMITTED,
        }:
            raise ValueError("committed lineage only accepts continuity projection")

    def _label(self, value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"turn journal {name} must be non-empty")
        text = value.strip()
        if len(text) > self.config.max_label_chars:
            raise ValueError(f"turn journal {name} exceeds configured bound")
        return text

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
