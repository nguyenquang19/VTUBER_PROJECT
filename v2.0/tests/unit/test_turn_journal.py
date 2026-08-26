from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from interfaces.operations import TurnJournalEvent, TurnJournalStage
from services.operations.turn_journal import TurnJournal, TurnJournalConfig


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _journal(*, lineages: int = 2, events: int = 8) -> TurnJournal:
    return TurnJournal(TurnJournalConfig(
        max_lineages=lineages,
        max_events_per_lineage=events,
        max_reason_codes=4,
        max_evidence_refs=4,
        max_label_chars=80,
        max_projection_bytes=4096,
    ))


def _event(stage: TurnJournalStage, **values: object) -> TurnJournalEvent:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "lineage_id": "decision-1",
        "stage": stage,
        "occurred_at": NOW,
        "session_id": "session-1",
        "decision_id": "decision-1",
    }
    defaults.update(values)
    return TurnJournalEvent(**defaults)  # type: ignore[arg-type]


def test_lineage_is_bounded_idempotent_and_privacy_safe() -> None:
    journal = _journal(events=3)
    first = _event(TurnJournalStage.DECISION_RECORDED)
    assert journal.append(first) == journal.append(first)
    journal.append(_event(
        TurnJournalStage.DELIVERY_RESERVED,
        occurred_at=NOW + timedelta(seconds=1),
        transaction_id="transaction-1",
    ))
    journal.append(_event(
        TurnJournalStage.GENERATION_STARTED,
        occurred_at=NOW + timedelta(seconds=2),
        transaction_id="transaction-1",
        request_id="request-1",
    ))
    journal.append(_event(
        TurnJournalStage.GENERATION_COMPLETED,
        occurred_at=NOW + timedelta(seconds=3),
        transaction_id="transaction-1",
        request_id="request-1",
        turn_id="turn-1",
    ))
    record = journal.get("decision-1")
    assert record is not None
    assert len(record.events) == 3
    exported = str(journal.snapshot())
    assert "prompt" not in exported
    assert "chain_of_thought" not in exported
    assert journal.get_metrics()["turn_journal_duplicate_total"] == 1
    assert journal.get_metrics()["turn_journal_event_evicted_total"] == 1


def test_terminal_and_stable_identity_conflicts_fail_closed() -> None:
    journal = _journal()
    journal.append(_event(
        TurnJournalStage.DELIVERY_RESERVED, transaction_id="transaction-1",
    ))
    with pytest.raises(ValueError, match="transaction_id conflicts"):
        journal.append(_event(
            TurnJournalStage.DELIVERY_STARTED, transaction_id="transaction-2",
        ))
    journal.append(_event(
        TurnJournalStage.OUTCOME_RELEASED,
        transaction_id="transaction-1",
        verified=False,
    ))
    with pytest.raises(ValueError, match="released lineage"):
        journal.append(_event(
            TurnJournalStage.CONTINUITY_COMMITTED,
            transaction_id="transaction-1",
            continuity_id="continuity-1",
        ))


def test_committed_lineage_accepts_only_continuity_and_retention_is_bounded() -> None:
    journal = _journal(lineages=1)
    journal.append(_event(
        TurnJournalStage.OUTCOME_COMMITTED,
        transaction_id="transaction-1",
        outcome_ref="outcome-1",
        verified=True,
    ))
    journal.append(_event(
        TurnJournalStage.CONTINUITY_COMMITTED,
        occurred_at=NOW + timedelta(seconds=1),
        transaction_id="transaction-1",
        outcome_ref="outcome-1",
        continuity_id="continuity-1",
        verified=True,
    ))
    with pytest.raises(ValueError, match="only accepts continuity"):
        journal.append(_event(
            TurnJournalStage.DELIVERY_FINISHED,
            transaction_id="transaction-1",
        ))
    journal.append(_event(
        TurnJournalStage.DECISION_RECORDED,
        lineage_id="decision-2",
        decision_id="decision-2",
        session_id="session-1",
    ))
    assert journal.get("decision-1") is None
    assert journal.get("decision-2") is not None
