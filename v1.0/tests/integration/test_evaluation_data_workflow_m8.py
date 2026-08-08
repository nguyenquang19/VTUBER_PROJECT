from __future__ import annotations

from pathlib import Path

from scripts.export_dataset import DatasetScrubber, build_dpo, build_sft
from services.evaluation.data_quality import DatasetQualityGate, load_data_contract, quality_report


ROOT = Path(__file__).resolve().parents[2]


def test_contract_gate_correction_override_and_session_split_work_together() -> None:
    gate = DatasetQualityGate(load_data_contract(ROOT / "eval" / "contracts" / "mai_agent_v1.yaml"))
    turn = {
        "schema_version": 2,
        "session_id": "session-corrected",
        "turn_id": 7,
        "persona_version": "5cacb00a34a1",
        "architecture_version": "mai-agent-v1",
        "context_schema_version": "mai-context-v1",
        "agenda_policy_version": "mai-agenda-v1",
        "level_used": 0,
        "parse_ok": True,
        "mai_text": "weak original",
        "user_text": "grounded prompt",
        "kind": "chat_reply",
        "source": "chat",
        "filter_verdict": {"passed": True},
    }
    identity = ("session-corrected", 7)
    selected, report = quality_report(
        [turn], gate, ratings={identity: "bad"}, corrections={identity},
    )
    correction = {
        "session_id": identity[0], "turn_id": identity[1],
        "original": "weak original", "corrected": "grounded correction",
    }
    scrubber = DatasetScrubber()
    sft = build_sft(
        selected, {identity: "bad"}, {identity: correction["corrected"]},
        "ref", "", scrubber,
    )
    dpo = build_dpo([], [correction], {identity: turn}, scrubber)

    assert report["eligible_turns"] == 1
    assert sft[0]["messages"][-1]["content"] == "grounded correction"
    assert dpo[0]["chosen"] == "grounded correction"
    assert dpo[0]["rejected"] == "weak original"
    sft_split = next(name for name, rows in gate.partition(sft).items() if rows)
    dpo_split = next(name for name, rows in gate.partition(dpo).items() if rows)
    assert sft_split == dpo_split
