"""Test T5 — export dataset (SFT + DPO) (Phase 8 data pipeline)."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "export_dataset", REPO / "scripts" / "export_dataset.py")
export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export)


def _turn(tid, level=0, parse_ok=True, mai="câu Mai", kind="chat_reply",
          user="hỏi gì đó", verdict=None, ctx="[Context] mood", session="session-a"):
    return {"session_id": session, "turn_id": tid,
            "level_used": level, "parse_ok": parse_ok,
            "mai_text": mai, "kind": kind, "user_text": user,
            "filter_verdict": verdict, "context_block": ctx,
            "persona_version": "v1"}


def _key(session="session-a", turn_id=1):
    return session, turn_id


def _delivered_ids(turns):
    return {
        (str(turn.get("session_id") or "legacy:turns"), int(turn["turn_id"]))
        for turn in turns
    }


def _build_sft(turns, ratings=None, corrections=None):
    return export.build_sft(
        turns,
        turns,
        _delivered_ids(turns),
        ratings or {},
        corrections or {},
        "ref",
        "",
    )


def _build_dpo(pref_pairs, corrections, turns_by_identity=None):
    indexed = turns_by_identity or {}
    turns = list(indexed.values())
    return export.build_dpo(
        pref_pairs,
        corrections,
        indexed,
        turns,
        _delivered_ids(turns),
        "ref",
        "",
    )


class TestBuildSFT:
    def test_filters_canned_and_bad(self) -> None:
        turns = [
            _turn(1), _turn(2),                    # 2 good/neutral → giữ
            _turn(3, level=1),                     # canned → bỏ
            _turn(4),                              # bad (rating) → bỏ
        ]
        ratings = {_key(turn_id=4): "bad"}
        sft = _build_sft(turns, ratings)
        assert len(sft) == 2
        ids = {s["meta"]["turn_id"] for s in sft}
        assert ids == {1, 2}

    def test_messages_shape(self) -> None:
        sft = _build_sft([_turn(1)])
        msgs = sft[0]["messages"]
        assert msgs[0]["role"] == "system" and "persona:v1" in msgs[0]["content"]
        assert any(m["role"] == "user" for m in msgs)
        assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == "câu Mai"
        assert sft[0]["schema_version"] == 2
        assert sft[0]["source"] == "sft:chat_reply"
        assert sft[0]["session_id"] == "session-a"
        assert sft[0]["timestamp"].endswith("+00:00")

    def test_correction_overrides_target(self) -> None:
        # T7: turn có correction → target = câu sửa (kể cả rating bad)
        sft = _build_sft(
            [_turn(1, mai="câu gốc dở")], {_key(): "bad"},
            {_key(): "câu sửa hay"},
        )
        assert len(sft) == 1
        assert sft[0]["messages"][-1]["content"] == "câu sửa hay"

    def test_blocked_unregenerated_dropped(self) -> None:
        turns = [_turn(1, verdict={"passed": False, "regen": False})]
        assert _build_sft(turns) == []

    def test_ambient_gets_selftalk_user(self) -> None:
        sft = _build_sft([_turn(1, kind="ambient", user=None)])
        users = [m for m in sft[0]["messages"] if m["role"] == "user"]
        assert users and "tự lên tiếng" in users[0]["content"]

    def test_same_turn_id_maps_rating_and_correction_by_session(self) -> None:
        turns = [
            _turn(1, mai="gốc A", session="session-a"),
            _turn(1, mai="gốc B", session="session-b"),
        ]
        ratings = {_key("session-a", 1): "bad", _key("session-b", 1): "good"}
        corrections = {_key("session-a", 1): "sửa A"}
        sft = _build_sft(turns, ratings, corrections)

        by_session = {item["meta"]["session_id"]: item for item in sft}
        assert by_session["session-a"]["messages"][-1]["content"] == "sửa A"
        assert by_session["session-b"]["messages"][-1]["content"] == "gốc B"

    def test_legacy_sources_are_isolated_instead_of_guessed(self) -> None:
        turn = _turn(1)
        turn.pop("session_id")
        legacy_rating = {("legacy:ratings", 1): "bad"}
        legacy_correction = {("legacy:corrections", 1): "sửa nhầm"}

        sft = _build_sft([turn], legacy_rating, legacy_correction)
        assert len(sft) == 1
        assert sft[0]["meta"]["session_id"] == "legacy:turns"
        assert sft[0]["meta"]["rating"] is None
        assert sft[0]["messages"][-1]["content"] == "câu Mai"

    def test_scrubs_pii_without_breaking_correction_join(self) -> None:
        turn = _turn(
            1,
            user="tên tôi là Nguyễn Văn An, nhắn @real_handle",
            ctx="callback https://example.test/cb?token=raw-secret",
            mai="mail abc@example.com",
        )
        corrections = {_key(): "gọi 0912345678 để sửa"}
        sft = _build_sft([turn], corrections=corrections)
        encoded = json.dumps(sft, ensure_ascii=False)
        for raw in ("Nguyễn Văn An", "@real_handle", "raw-secret", "0912345678"):
            assert raw not in encoded
        assert sft[0]["meta"]["corrected"] is True
        assert "[PII]" in encoded


class TestBuildDPO:
    def test_pref_pairs_to_dpo(self) -> None:
        pref = [{"prompt_ref": {"context_block": "[Context]", "user_text": "x"},
                 "chosen": "hay", "rejected": "dở", "reason": "filter:troll"}]
        dpo = _build_dpo(pref, [])
        assert len(dpo) == 1
        assert dpo[0]["chosen"] == "hay" and dpo[0]["rejected"] == "dở"
        assert dpo[0]["source"] == "filter:troll"

    def test_correction_to_dpo(self) -> None:
        corr = [{"session_id": "session-a", "turn_id": 1,
                 "original": "gốc", "corrected": "sửa"}]
        dpo = _build_dpo([], corr, {_key(): _turn(1)})
        assert len(dpo) == 1
        assert dpo[0]["chosen"] == "sửa" and dpo[0]["rejected"] == "gốc"
        assert dpo[0]["source"] == "correction"
        assert dpo[0]["session_id"] == "session-a"
        assert dpo[0]["timestamp"].endswith("+00:00")

    def test_identical_correction_skipped(self) -> None:
        corr = [{"session_id": "session-a", "turn_id": 1,
                 "original": "same", "corrected": "same"}]
        assert _build_dpo([], corr) == []


class TestDoD:
    def test_5_turns_2_sft_1_dpo(self) -> None:
        # DoD: 2 good, 1 canned, 1 bad + 1 regen(pref) → SFT 2, DPO 1
        turns = [_turn(1), _turn(2), _turn(3, level=1), _turn(4)]
        ratings = {_key(turn_id=4): "bad"}
        pref = [{"prompt_ref": {}, "chosen": "c", "rejected": "r", "reason": "dedup:ambient"}]
        sft = _build_sft(turns, ratings)
        dpo = _build_dpo(pref, [])
        assert len(sft) == 2 and len(dpo) == 1

    def test_dry_run_writes_no_dataset(self, tmp_path: Path, monkeypatch, capsys) -> None:
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "turns.jsonl").write_text(
            json.dumps(_turn(1), ensure_ascii=False) + "\nnot-json\n", encoding="utf-8",
        )
        output = tmp_path / "datasets"
        monkeypatch.setattr(export.sys, "argv", [
            "export_dataset.py", "--in-dir", str(logs), "--out-dir", str(output),
            "--dry-run",
        ])

        export.main()

        captured = capsys.readouterr().out
        assert "dry-run" in captured
        assert "invalid records:  1" in captured
        assert not output.exists()

    def test_bundle_is_immutable_and_has_source_checksums(self, tmp_path: Path) -> None:
        source = tmp_path / "turns.jsonl"
        source.write_text('{"one":1}\n', encoding="utf-8")
        contract = export.load_data_contract(REPO / "eval" / "contracts" / "mai_agent_v1.yaml")
        now = datetime(2026, 8, 12, 1, 2, 3, tzinfo=timezone.utc)

        bundle = export.write_dataset_bundle(
            out_dir=tmp_path / "datasets",
            contract=contract,
            source_paths=[source],
            canonical_turns=[{"schema_version": 1, "session_id": "s"}],
            sft_splits={"train": [], "validation": [], "holdout": []},
            dpo_splits={"train": [], "validation": [], "holdout": []},
            quality={"quarantined_turns": 0},
            now=now,
        )
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["contract_id"] == "mai-agent-v1"
        assert manifest["sources"][0]["sha256"] == hashlib.sha256(
            source.read_bytes(),
        ).hexdigest()
        assert (bundle / "canonical" / "turns.jsonl").exists()

        with pytest.raises(FileExistsError):
            export.write_dataset_bundle(
                out_dir=tmp_path / "datasets", contract=contract,
                source_paths=[source], canonical_turns=[],
                sft_splits={"train": [], "validation": [], "holdout": []},
                dpo_splits={"train": [], "validation": [], "holdout": []},
                quality={}, now=now,
            )
