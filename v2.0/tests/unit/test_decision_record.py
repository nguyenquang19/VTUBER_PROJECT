from __future__ import annotations

import pytest
from pydantic import ValidationError

from interfaces.decision_record import DecisionCandidateSummary
from services.operations.metrics import MetricsCollector
from services.director.decision_record import DecisionRecordManager


def _summary() -> DecisionCandidateSummary:
    return DecisionCandidateSummary(
        candidate_count=2,
        pool_size=5,
        pulse_state="lively",
        active_goal_id="goal-1",
        candidate_kinds=("mention", "question"),
        top_score=35.5,
    )


def _record(manager: DecisionRecordManager, index: int = 1):
    return manager.record_decision(
        created_at=float(index),
        action="read_chat",
        reason="top_single",
        segment="main",
        evidence_refs=(f"event-{index}",),
        candidate_summary=_summary(),
    )


def test_record_schema_is_versioned_frozen_and_privacy_safe() -> None:
    manager = DecisionRecordManager()
    record = _record(manager)
    assert record is not None
    assert record.schema_version == 1
    assert record.evidence_refs == ("event-1",)
    with pytest.raises(ValidationError):
        record.action = "changed"  # type: ignore[misc]
    exported = str(manager.snapshot())
    assert "raw chat secret" not in exported
    assert "viewer_name" not in exported


def test_retention_and_evidence_are_bounded_and_deduplicated() -> None:
    manager = DecisionRecordManager(max_recent=2, max_evidence_refs=2)
    first = manager.record_decision(
        created_at=1,
        action="read_chat",
        reason="top_single",
        segment="main",
        evidence_refs=("e1", "e1", "e2", "e3"),
        candidate_summary=_summary(),
    )
    assert first is not None and first.evidence_refs == ("e1", "e2")
    _record(manager, 2)
    _record(manager, 3)
    assert len(manager.snapshot()["recent"]) == 2


def test_configured_wait_reason_becomes_hard_rejection() -> None:
    manager = DecisionRecordManager(hard_rejection_reasons=("safety_hold",))
    rejection = manager.classify_hard_rejection("wait", "safety_hold")
    record = manager.record_decision(
        created_at=1,
        action="wait",
        reason="safety_hold",
        segment="main",
        evidence_refs=(),
        candidate_summary=DecisionCandidateSummary(safety_hold=True),
        hard_rejection_reason=rejection,
    )
    assert record is not None
    assert record.outcome == "hard_rejected"
    assert record.hard_rejection_reason == "safety_hold"


def test_transaction_update_is_linked_and_observable() -> None:
    metrics = MetricsCollector()
    manager = DecisionRecordManager(metrics=metrics, clock=lambda: 3.0)
    record = _record(manager)
    assert record is not None
    updated = manager.update_transaction(
        record.decision_id,
        transaction_id="act-1",
        transaction_state="committed",
        delivery_state="delivered",
        outcome="committed",
    )
    assert updated is not None
    assert updated.transaction_id == "act-1"
    assert updated.outcome == "committed"
    assert metrics.director_decision_record_snapshot() == {
        "read_chat:committed": 1,
        "read_chat:selected": 1,
    }
    assert (
        b'mai_director_decision_records_total{action="read_chat",outcome="committed"} 1.0'
        in metrics.prometheus_text()
    )


def test_disabled_store_does_not_change_calling_behavior() -> None:
    manager = DecisionRecordManager(enabled=False)
    assert _record(manager) is None
    assert manager.snapshot()["recent"] == []


def test_metrics_failure_does_not_drop_decision_record() -> None:
    class BrokenMetrics:
        def record_director_decision_record(self, action: str, outcome: str) -> None:
            raise RuntimeError("metrics unavailable")

    manager = DecisionRecordManager(metrics=BrokenMetrics())
    assert _record(manager) is not None
    assert len(manager.snapshot()["recent"]) == 1
