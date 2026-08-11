from __future__ import annotations

from pathlib import Path

from services.evaluation.data_quality import DatasetQualityGate, load_data_contract, quality_report


ROOT = Path(__file__).resolve().parents[2]


def _gate() -> DatasetQualityGate:
    return DatasetQualityGate(load_data_contract(ROOT / "eval" / "contracts" / "mai_agent_v1.yaml"))


def _turn(session: str = "session-a", turn_id: int = 1) -> dict:
    return {
        "schema_version": 2,
        "session_id": session,
        "turn_id": turn_id,
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
    )
    assert selected == [good]
    assert report["eligible_turns"] == 1
    assert report["quarantined_turns"] == 1
    assert report["distribution"]["operator_rating"] == {"good": 1}
    assert "legacy:turns" not in str(report["quarantine"])
