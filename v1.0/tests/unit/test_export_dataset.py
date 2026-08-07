"""Test T5 — export dataset (SFT + DPO) (Phase 8 data pipeline)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "export_dataset", REPO / "scripts" / "export_dataset.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def _turn(tid, level=0, parse_ok=True, mai="câu Mai", kind="chat_reply",
          user="hỏi gì đó", verdict=None, ctx="[Context] mood"):
    return {"turn_id": tid, "level_used": level, "parse_ok": parse_ok,
            "mai_text": mai, "kind": kind, "user_text": user,
            "filter_verdict": verdict, "context_block": ctx,
            "persona_version": "v1"}


class TestBuildSFT:
    def test_filters_canned_and_bad(self) -> None:
        turns = [
            _turn(1), _turn(2),                    # 2 good/neutral → giữ
            _turn(3, level=1),                     # canned → bỏ
            _turn(4),                              # bad (rating) → bỏ
        ]
        ratings = {4: "bad"}
        sft = export.build_sft(turns, ratings, {}, "ref", "")
        assert len(sft) == 2
        ids = {s["meta"]["turn_id"] for s in sft}
        assert ids == {1, 2}

    def test_messages_shape(self) -> None:
        sft = export.build_sft([_turn(1)], {}, {}, "ref", "")
        msgs = sft[0]["messages"]
        assert msgs[0]["role"] == "system" and "persona:v1" in msgs[0]["content"]
        assert any(m["role"] == "user" for m in msgs)
        assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == "câu Mai"

    def test_correction_overrides_target(self) -> None:
        # T7: turn có correction → target = câu sửa (kể cả rating bad)
        sft = export.build_sft([_turn(1, mai="câu gốc dở")], {1: "bad"},
                               {1: "câu sửa hay"}, "ref", "")
        assert len(sft) == 1
        assert sft[0]["messages"][-1]["content"] == "câu sửa hay"

    def test_blocked_unregenerated_dropped(self) -> None:
        turns = [_turn(1, verdict={"passed": False, "regen": False})]
        assert export.build_sft(turns, {}, {}, "ref", "") == []

    def test_ambient_gets_selftalk_user(self) -> None:
        sft = export.build_sft([_turn(1, kind="ambient", user=None)], {}, {}, "ref", "")
        users = [m for m in sft[0]["messages"] if m["role"] == "user"]
        assert users and "tự lên tiếng" in users[0]["content"]


class TestBuildDPO:
    def test_pref_pairs_to_dpo(self) -> None:
        pref = [{"prompt_ref": {"context_block": "[Context]", "user_text": "x"},
                 "chosen": "hay", "rejected": "dở", "reason": "filter:troll"}]
        dpo = export.build_dpo(pref, [], {})
        assert len(dpo) == 1
        assert dpo[0]["chosen"] == "hay" and dpo[0]["rejected"] == "dở"
        assert dpo[0]["source"] == "filter:troll"

    def test_correction_to_dpo(self) -> None:
        corr = [{"turn_id": 1, "original": "gốc", "corrected": "sửa"}]
        dpo = export.build_dpo([], corr, {1: _turn(1)})
        assert len(dpo) == 1
        assert dpo[0]["chosen"] == "sửa" and dpo[0]["rejected"] == "gốc"
        assert dpo[0]["source"] == "correction"

    def test_identical_correction_skipped(self) -> None:
        corr = [{"turn_id": 1, "original": "same", "corrected": "same"}]
        assert export.build_dpo([], corr, {}) == []


class TestDoD:
    def test_5_turns_2_sft_1_dpo(self) -> None:
        # DoD: 2 good, 1 canned, 1 bad + 1 regen(pref) → SFT 2, DPO 1
        turns = [_turn(1), _turn(2), _turn(3, level=1), _turn(4)]
        ratings = {4: "bad"}
        pref = [{"prompt_ref": {}, "chosen": "c", "rejected": "r", "reason": "dedup:ambient"}]
        sft = export.build_sft(turns, ratings, {}, "ref", "")
        dpo = export.build_dpo(pref, [], {})
        assert len(sft) == 2 and len(dpo) == 1
