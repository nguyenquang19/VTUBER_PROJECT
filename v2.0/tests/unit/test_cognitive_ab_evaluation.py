"""MCB-4 source-bound comparison, selection and sealed-review behavior."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.evaluation.cognitive_ab import (
    CognitiveABConfig,
    CognitiveABCorpus,
    CognitiveABEvaluation,
)
from services.evaluation.human_like import DIMENSIONS, HumanLikeCalibration


ROOT = Path(__file__).resolve().parents[2]
HEX = {
    "config_digest": "1" * 64,
    "model_digest": "2" * 64,
    "persona_digest": "3" * 64,
    "compatibility_prompt_digest": "4" * 64,
    "brain_prompt_digest": "5" * 64,
}


def _loader() -> ConfigLoader:
    loader = ConfigLoader(ROOT / "config")
    loader.load_all()
    return loader


def _evaluation(*, metrics: MetricsCollector | None = None) -> CognitiveABEvaluation:
    loader = _loader()
    config = CognitiveABConfig.from_loader(loader)
    corpus = CognitiveABCorpus.load(ROOT / config.corpus_file, config)
    return CognitiveABEvaluation(
        config,
        corpus,
        HumanLikeCalibration.from_loader(loader, metrics=metrics, enabled=True),
        metrics=metrics,
    )


def _source(evaluation: CognitiveABEvaluation) -> dict:
    identities = {**HEX, "corpus_digest": evaluation.corpus.digest}
    rows = []
    for index, case in enumerate(evaluation.corpus.cases):
        both_wait = index < 2
        brain_wait = both_wait or index % 7 == 0
        compatibility_wait = both_wait

        def candidate(role: str, wait: bool) -> dict:
            prompt = (
                identities["compatibility_prompt_digest"]
                if role == "compatibility" else identities["brain_prompt_digest"]
            )
            return {
                "mode": "WAIT" if wait else "SPEAK",
                "action_label": "wait" if wait else "read_chat",
                "output": None if wait else f"{role} output {case.case_id}",
                "outcome": "COMPLETED",
                "prompt_ref": prompt,
                "latency_ms": 25.0 + index,
                "input_tokens": 100 + index,
                "output_tokens": 12,
            }

        rows.append({
            "case_id": case.case_id,
            "context_id": "ctx:" + f"{index:064x}",
            "context_summary": case.review_context,
            "same_input_context": True,
            "profile_ref": identities["persona_digest"],
            "model_ref": identities["model_digest"],
            "seed": evaluation.config.seed + index,
            "max_tokens": evaluation.config.generation_max_tokens,
            "temperature": evaluation.config.generation_temperature,
            "hard_flags": [],
            "compatibility": candidate("compatibility", compatibility_wait),
            "brain": candidate("brain", brain_wait),
        })
    return {
        "schema_version": 1,
        "marker": "mai_cognitive_ab_source",
        "source_revision": "a" * 40,
        "source_clean": True,
        "product_version": "1.4.3",
        "evidence_identity": identities,
        "rows": rows,
    }


def _complete(review: dict, manifest: dict) -> dict:
    review["human_review"] = {"reviewer_role": "owner", "complete": True}
    sealed = {row["pair_ref"]: row for row in manifest["rows"]}
    for row in review["rows"]:
        identity = sealed[row["pair_ref"]]
        for key in ("a", "b"):
            candidate = identity[key]["role"] == "candidate"
            score = 4 if candidate else 3
            row["review"][key] = {
                "dimensions": {name: score for name in DIMENSIONS},
                "ai_smell": not candidate,
                "ai_smell_tags": [] if candidate else ["assistant_register"],
                "liveness": score,
                "action_coherence": score,
                "note": "bounded human discovery note",
            }
        row["review"]["preferred"] = (
            "A" if identity["a"]["role"] == "candidate" else "B"
        )
    return review


def test_config_corpus_and_source_contract_are_strict() -> None:
    evaluation = _evaluation()
    assert len(evaluation.corpus.cases) == 40
    assert set(case.stratum for case in evaluation.corpus.cases) == set(
        evaluation.config.required_strata,
    )
    assert set(case.arc_id for case in evaluation.corpus.cases) == set(
        evaluation.config.required_arcs,
    )
    for arc in evaluation.config.required_arcs:
        turns = sorted(
            case.turn_index for case in evaluation.corpus.cases if case.arc_id == arc
        )
        assert turns == [1, 2, 3, 4, 5]
    source = _source(evaluation)
    source["rows"][0]["same_input_context"] = False
    with pytest.raises(ValueError, match="same input"):
        evaluation.build(source)


def test_config_rejects_unknown_key_bool_coercion_and_pair_bound_drift() -> None:
    loader = _loader()
    raw = dict(loader.get("evaluation", "evaluation.cognitive_ab"))
    with pytest.raises(ValueError, match="keys"):
        CognitiveABConfig.from_mapping({**raw, "extra": 1})
    with pytest.raises(ValueError, match="integer"):
        CognitiveABConfig.from_mapping({**raw, "seed": True})
    with pytest.raises(ValueError, match="cover"):
        CognitiveABConfig.from_mapping({**raw, "minimum_cases": 29})
    duplicated_arcs = deepcopy(raw)
    duplicated_arcs["required_arcs"][-1] = duplicated_arcs["required_arcs"][0]
    with pytest.raises(ValueError, match="required_arcs must be unique"):
        CognitiveABConfig.from_mapping(duplicated_arcs)


def test_story_corpus_rejects_turn_gap_and_first_beat_history(tmp_path: Path) -> None:
    evaluation = _evaluation()
    source = ROOT / evaluation.config.corpus_file
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["cases"][1]["turn_index"] = 3
    target = tmp_path / "gap.yaml"
    target.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="turn order"):
        CognitiveABCorpus.load(target, evaluation.config)

    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["cases"][0]["prior_turns"] = [{"role": "mai", "text": "future leak"}]
    target.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="first story beat"):
        CognitiveABCorpus.load(target, evaluation.config)


def test_build_is_deterministic_stratified_and_hides_role_identity() -> None:
    metrics = MetricsCollector()
    first_service = _evaluation(metrics=metrics)
    first = first_service.build(_source(first_service))
    second_service = _evaluation()
    second = second_service.build(_source(second_service))
    assert first == second
    private, review, manifest = first
    assert private["summary"]["total_cases"] == 40
    assert private["summary"]["both_wait"] == 2
    assert private["summary"]["selected_pairs"] == 30
    assert private["source_gate_eligible"] is True
    assert set(private["summary"]["selected_per_stratum"]) == set(
        first_service.config.required_strata,
    )
    assert all(
        count >= first_service.config.minimum_selected_per_arc
        for count in private["summary"]["selected_per_arc"].values()
    )
    selected = [
        next(case for case in first_service.corpus.cases if case.case_id == case_id)
        for case_id in private["selected_pair_refs"]
    ]
    arc_order = {
        arc: index for index, arc in enumerate(first_service.config.required_arcs)
    }
    assert selected == sorted(
        selected, key=lambda case: (arc_order[case.arc_id], case.turn_index),
    )
    assert "Tập:" in review["rows"][0]["context_summary"]
    rendered = json.dumps(review, ensure_ascii=False)
    assert "compatibility_prompt_digest" not in rendered
    assert "a" * 40 not in rendered
    assert review["status"] == "pending_human_review"
    assert manifest["commitment"] == review["commitment"]
    snapshot = metrics.cognition_ab_snapshot()
    assert snapshot["pairs"] == {"built": 1}
    assert sum(snapshot["cases"].values()) == 40


@pytest.mark.asyncio
async def test_finalize_reveals_sealed_modes_only_after_persisted_review(
    tmp_path: Path,
) -> None:
    evaluation = _evaluation()
    private, review, manifest = evaluation.build(_source(evaluation))
    path = tmp_path / "review.json"
    path.write_text(json.dumps(_complete(review, manifest)), encoding="utf-8")
    final = await evaluation.finalize(path, manifest, private)
    assert final["marker"] == "mai_cognitive_ab_finalized_review"
    assert final["automatic_release_decision"] is False
    assert final["owner_go_no_go_required"] is True
    revealed = final["human_like"]["rows"][0]["revealed"]
    metadata = next(iter(revealed.values()))["sealed_metadata"]
    assert metadata["mode"] in {"WAIT", "SPEAK"}
    assert metadata["source_digest"] == private["source_digest"]


def test_failure_and_both_wait_remain_in_denominator_and_cannot_be_padded() -> None:
    evaluation = _evaluation()
    source = _source(evaluation)
    for row in source["rows"][2:11]:
        row["brain"].update({
            "mode": "WAIT", "action_label": "wait", "output": None,
            "outcome": "SCHEMA_REJECTED", "latency_ms": None,
            "input_tokens": None, "output_tokens": None,
        })
    with pytest.raises(ValueError, match="fewer than 30|insufficient informative"):
        evaluation.build(source)


def test_identity_and_candidate_output_fail_closed() -> None:
    evaluation = _evaluation()
    source = _source(evaluation)
    source["rows"][0]["brain"]["prompt_ref"] = "6" * 64
    with pytest.raises(ValueError, match="prompt identity"):
        evaluation.build(source)
    source = _source(evaluation)
    source["rows"][2]["brain"]["mode"] = "WAIT"
    with pytest.raises(ValueError, match="must not contain output"):
        evaluation.build(source)
