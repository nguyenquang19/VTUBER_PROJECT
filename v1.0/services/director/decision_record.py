"""Bounded in-memory Director decision record store (M10.3)."""
from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from interfaces.base import HealthStatus
from interfaces.decision_record import (
    DecisionCandidateSummary,
    DecisionRecord,
    DecisionRecordService,
)


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
        self.enabled = bool(enabled)
        self._running = False
        self._items: OrderedDict[str, DecisionRecord] = OrderedDict()
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool = True,
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
            self.service_id, enabled=self.enabled, retained=len(self._items),
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
        self._items[record.decision_id] = record
        self._trim()
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
        record = self._items.get(str(decision_id))
        if record is None:
            return None
        updated = record.model_copy(update={
            "updated_at": self._clock(),
            "transaction_id": self._clean(transaction_id),
            "transaction_state": self._clean(transaction_state),
            "delivery_state": self._clean(delivery_state),
            "outcome": self._clean(outcome),
        })
        self._items[updated.decision_id] = updated
        self._items.move_to_end(updated.decision_id)
        self._record_metric(updated.action, updated.outcome)
        return updated

    def update_outcome(
        self, decision_id: str, *, delivery_state: str, outcome: str,
    ) -> DecisionRecord | None:
        if not self.enabled:
            return None
        record = self._items.get(str(decision_id))
        if record is None:
            return None
        updated = record.model_copy(update={
            "updated_at": self._clock(),
            "delivery_state": self._clean(delivery_state),
            "outcome": self._clean(outcome),
        })
        self._items[updated.decision_id] = updated
        self._items.move_to_end(updated.decision_id)
        self._record_metric(updated.action, updated.outcome)
        return updated

    def classify_hard_rejection(self, action: str, reason: str) -> str:
        value = self._clean(reason)
        return value if str(action) == "wait" and value in self._hard_rejections else ""

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"decision_records_{outcome}_total": count
            for outcome, count in sorted(self._counts.items())
        }

    def snapshot(self) -> dict[str, Any]:
        recent = list(self._items.values())[-20:]
        return {
            "schema_version": 1,
            "enabled": self.enabled,
            "counts": dict(sorted(self._counts.items())),
            "current": recent[-1].model_dump(mode="json") if recent else None,
            "recent": [item.model_dump(mode="json") for item in reversed(recent)],
        }

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

    def _trim(self) -> None:
        while len(self._items) > self.max_recent:
            self._items.popitem(last=False)
