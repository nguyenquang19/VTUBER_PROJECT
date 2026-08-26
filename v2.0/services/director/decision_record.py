"""Bounded in-memory Director decision record store (M10.3)."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any
from datetime import datetime, timezone

from interfaces.base import HealthStatus
from interfaces.decision_record import (
    DecisionCandidateSummary,
    DecisionRecord,
    DecisionRecordService,
)
from interfaces.operations import TurnJournalEvent, TurnJournalStage
from services.operations.turn_journal import TurnJournal, TurnJournalConfig


class DecisionRecordManager(DecisionRecordService):
    service_id = "decision_records"

    def __init__(
        self,
        *,
        max_recent: int = 256,
        max_evidence_refs: int = 8,
        max_label_chars: int = 120,
        hard_rejection_reasons: tuple[str, ...] = (),
        clock: Callable[[], float] | None = None,
        metrics: Any = None,
        turn_journal: Any = None,
        enabled: bool = True,
    ) -> None:
        if max_recent <= 0 or max_evidence_refs <= 0 or max_label_chars <= 0:
            raise ValueError("decision record bounds must be positive")
        self.max_recent = int(max_recent)
        self.max_evidence_refs = int(max_evidence_refs)
        self.max_label_chars = int(max_label_chars)
        self._hard_rejections = frozenset(
            self._clean(item) for item in hard_rejection_reasons if str(item).strip()
        )
        self._clock = clock or time.time
        self._metrics = metrics
        self._turn_journal = turn_journal or TurnJournal(TurnJournalConfig(
            max_lineages=max_recent,
            max_events_per_lineage=16,
            max_reason_codes=max_evidence_refs,
            max_evidence_refs=max_evidence_refs,
            max_label_chars=max_label_chars,
            max_projection_bytes=16384,
        ))
        self.enabled = bool(enabled)
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, turn_journal: Any = None,
        enabled: bool = True,
    ) -> "DecisionRecordManager":
        base = "director.decision_records"
        return cls(
            max_recent=int(loader.get("director", f"{base}.max_recent", 256)),
            max_evidence_refs=int(loader.get(
                "director", f"{base}.max_evidence_refs", 8,
            )),
            max_label_chars=int(loader.get(
                "director", f"{base}.max_label_chars", 120,
            )),
            hard_rejection_reasons=tuple(loader.get(
                "director", f"{base}.hard_rejection_reasons", [],
            ) or ()),
            metrics=metrics,
            turn_journal=turn_journal,
            enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, retained=len(self._records()),
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def record_decision(
        self,
        *,
        created_at: float,
        action: str,
        reason: str,
        segment: str,
        evidence_refs: tuple[str, ...],
        candidate_summary: DecisionCandidateSummary,
        hard_rejection_reason: str = "",
    ) -> DecisionRecord | None:
        if not self.enabled:
            return None
        now = float(created_at)
        rejection = self._clean(hard_rejection_reason)
        outcome = "hard_rejected" if rejection else (
            "waiting" if str(action) == "wait" else "selected"
        )
        record = DecisionRecord(
            decision_id=f"dec_{uuid.uuid4().hex}",
            created_at=now,
            updated_at=now,
            action=self._clean(action),
            reason=self._clean(reason),
            segment=self._clean(segment),
            evidence_refs=self._evidence(evidence_refs),
            candidate_summary=self._summary(candidate_summary),
            hard_rejection_reason=rejection,
            outcome=outcome,
        )
        self._append_journal(
            record,
            TurnJournalStage.DECISION_RECORDED,
            verified=None,
        )
        self._record_metric(record.action, outcome)
        return record

    def update_transaction(
        self,
        decision_id: str,
        *,
        transaction_id: str,
        transaction_state: str,
        delivery_state: str,
        outcome: str,
    ) -> DecisionRecord | None:
        if not self.enabled:
            return None
        record = self._get_record(decision_id)
        if record is None:
            return None
        updated = record.model_copy(update={
            "updated_at": self._clock(),
            "transaction_id": self._clean(transaction_id),
            "transaction_state": self._clean(transaction_state),
            "delivery_state": self._clean(delivery_state),
            "outcome": self._clean(outcome),
        })
        self._record_metric(updated.action, updated.outcome)
        if updated.transaction_id:
            stage = {
                "reserved": TurnJournalStage.DELIVERY_RESERVED,
                "committed": TurnJournalStage.OUTCOME_COMMITTED,
                "duplicate_committed": TurnJournalStage.OUTCOME_COMMITTED,
                "released": TurnJournalStage.OUTCOME_RELEASED,
            }.get(updated.outcome)
            if stage is not None:
                self._append_journal(
                    updated,
                    stage,
                    verified=(
                        updated.delivery_state == "delivered"
                        if stage in {
                            TurnJournalStage.OUTCOME_COMMITTED,
                            TurnJournalStage.OUTCOME_RELEASED,
                        }
                        else None
                    ),
                )
        return updated

    def update_outcome(
        self, decision_id: str, *, delivery_state: str, outcome: str,
    ) -> DecisionRecord | None:
        if not self.enabled:
            return None
        record = self._get_record(decision_id)
        if record is None:
            return None
        updated = record.model_copy(update={
            "updated_at": self._clock(),
            "delivery_state": self._clean(delivery_state),
            "outcome": self._clean(outcome),
        })
        self._record_metric(updated.action, updated.outcome)
        stage = (
            TurnJournalStage.OUTCOME_COMMITTED
            if updated.delivery_state == "delivered"
            else TurnJournalStage.OUTCOME_RELEASED
        )
        self._append_journal(
            updated, stage, verified=updated.delivery_state == "delivered",
        )
        return updated

    def _append_journal(
        self,
        record: DecisionRecord,
        stage: TurnJournalStage,
        *,
        verified: bool | None,
    ) -> None:
        if self._turn_journal is None:
            return
        try:
            self._turn_journal.append(TurnJournalEvent(
                schema_version=1,
                lineage_id=record.decision_id,
                stage=stage,
                occurred_at=datetime.fromtimestamp(record.updated_at, timezone.utc),
                decision_id=record.decision_id,
                transaction_id=record.transaction_id,
                mode=record.action,
                terminal_state=record.outcome,
                verified=verified,
                reason_codes=((record.hard_rejection_reason,) if record.hard_rejection_reason else ()),
                evidence_refs=record.evidence_refs,
                projection_kind="decision_record",
                projection_json=record.model_dump_json(),
            ))
        except Exception:
            return

    def classify_hard_rejection(self, action: str, reason: str) -> str:
        value = self._clean(reason)
        return value if str(action) == "wait" and value in self._hard_rejections else ""

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"decision_records_{outcome}_total": count
            for outcome, count in sorted(self._counts.items())
        }

    def snapshot(self) -> dict[str, Any]:
        recent = self._records()[-20:]
        return {
            "schema_version": 1,
            "enabled": self.enabled,
            "counts": dict(sorted(self._counts.items())),
            "current": recent[-1].model_dump(mode="json") if recent else None,
            "recent": [item.model_dump(mode="json") for item in reversed(recent)],
        }

    def _get_record(self, decision_id: str) -> DecisionRecord | None:
        value = self._turn_journal.projection(str(decision_id), "decision_record")
        return DecisionRecord.model_validate_json(value) if value is not None else None

    def _records(self) -> list[DecisionRecord]:
        values: list[DecisionRecord] = []
        for lineage in self._turn_journal.recent():
            value = self._turn_journal.projection(
                lineage.lineage_id, "decision_record",
            )
            if value is None:
                continue
            values.append(DecisionRecord.model_validate_json(value))
        return values[-self.max_recent:]

    def _clean(self, value: Any) -> str:
        return str(value).strip()[:self.max_label_chars]

    def _evidence(self, values: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            clean = self._clean(value)
            if clean and clean not in result:
                result.append(clean)
            if len(result) >= self.max_evidence_refs:
                break
        return tuple(result)

    def _summary(self, value: DecisionCandidateSummary) -> DecisionCandidateSummary:
        return DecisionCandidateSummary(
            candidate_count=max(0, int(value.candidate_count)),
            pool_size=max(0, int(value.pool_size)),
            pulse_state=self._clean(value.pulse_state),
            active_goal_id=(
                self._clean(value.active_goal_id) if value.active_goal_id else None
            ),
            safety_hold=bool(value.safety_hold),
            candidate_kinds=tuple(
                self._clean(item)
                for item in value.candidate_kinds[:self.max_evidence_refs]
                if self._clean(item)
            ),
            top_score=value.top_score,
        )

    def _record_metric(self, action: str, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(
            self._metrics, "record_director_decision_record",
        ):
            try:
                self._metrics.record_director_decision_record(action, outcome)
            except Exception:
                pass
