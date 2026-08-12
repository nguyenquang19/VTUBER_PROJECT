from __future__ import annotations

from pathlib import Path

from services.evaluation.data_quality import (
    DatasetQualityGate,
    index_delivery_outcomes,
    load_data_contract,
    quality_report,
)


ROOT = Path(__file__).resolve().parents[2]


def _gate() -> DatasetQualityGate:
    return DatasetQualityGate(load_data_contract(ROOT / "eval" / "contracts" / "mai_agent_v1.yaml"))


def _turn(session: str = "session-a", turn_id: int = 1) -> dict:
    return {
        "schema_version": 3,
        "session_id": session,
        "turn_id": turn_id,
        "request_id": f"request-{turn_id}",
        "persona_version": "a755c6d68383",
        "architecture_version": "mai-agent-v1",
        "context_schema_version": "mai-context-v1",
        "agenda_policy_version": "mai-agenda-v1",
        "level_used": 0,
        "parse_ok": True,
        "mai_text": "sanitized target",
        "kind": "chat_reply",
        "mood_dominant": "neutral",
        "source": "chat",
        "filter_verdict": {"passed": True, "categories": []},
    }


def _outcome(turn: dict, delivered: bool = True) -> dict:
    return {
        "schema_version": 1,
        "session_id": turn["session_id"],
        "request_id": turn["request_id"],
        "turn_id": turn["turn_id"],
        "delivered": delivered,
    }


def test_contract_quarantines_legacy_or_mismatched_architecture() -> None:
    gate = _gate()
    legacy = _turn("legacy:turns")
    legacy.pop("architecture_version")
    decision = gate.assess_turn(legacy)
    assert decision.eligible is False
    assert "incompatible:legacy_session" in decision.reasons
    assert "missing:architecture_version" in decision.reasons


def test_primary_passed_turn_is_eligible() -> None:
    assert _gate().assess_turn(_turn()).eligible is True


def test_delivery_gate_rejects_missing_and_failed_outcomes() -> None:
    gate = _gate()
    assert gate.assess_delivery(None).eligible is False
    assert "delivery:missing_outcome" in gate.assess_delivery(None).reasons
    assert gate.assess_delivery(_outcome(_turn(), False)).eligible is False
    assert "delivery:not_delivered" in gate.assess_delivery(_outcome(_turn(), False)).reasons


def test_session_split_is_stable_and_never_splits_one_session() -> None:
    gate = _gate()
    records = [_turn("same-session", index) for index in range(1, 5)]
    partition = gate.partition(records)
    populated = [name for name, rows in partition.items() if rows]
    assert len(populated) == 1
    assert gate.split_for_session("same-session") == populated[0]


def test_distribution_and_quarantine_report_contains_no_raw_session() -> None:
    gate = _gate()
    good = _turn("secret-session", 1)
    bad = _turn("legacy:turns", 2)
    selected, report = quality_report(
        [good, bad], gate,
        ratings={("secret-session", 1): "good"},
        corrections={("secret-session", 1)},
        delivery_outcomes=index_delivery_outcomes([_outcome(good), _outcome(bad)]),
    )
    assert selected == [good]
    assert report["eligible_turns"] == 1
    assert report["quarantined_turns"] == 1
    assert report["distribution"]["operator_rating"] == {"good": 1}
    assert "legacy:turns" not in str(report["quarantine"])


def test_quality_report_only_selects_explicitly_delivered_turns() -> None:
    gate = _gate()
    delivered = _turn("session-a", 1)
    failed = _turn("session-a", 2)
    pending = _turn("session-a", 3)
    selected, report = quality_report(
        [delivered, failed, pending], gate, ratings={}, corrections=set(),
        delivery_outcomes=index_delivery_outcomes([
            _outcome(delivered, True), _outcome(failed, False),
        ]),
    )
    assert selected == [delivered]
    assert report["quarantine_reason_counts"]["delivery:not_delivered"] == 1
    assert report["quarantine_reason_counts"]["delivery:missing_outcome"] == 1
