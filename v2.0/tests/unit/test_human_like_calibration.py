from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.metrics_collector import MetricsCollector
from services.evaluation.human_like import (
    DIMENSIONS,
    HumanLikeCalibration,
    HumanLikeConfig,
)


def _config() -> HumanLikeConfig:
    return HumanLikeConfig(
        schema_version=1,
        seed=17,
        min_pairs=1,
        max_pairs=4,
        max_output_chars=200,
        max_context_chars=100,
        max_note_chars=120,
        max_ref_chars=80,
        dimensions=(
            ("language", 0.20),
            ("presence", 0.25),
            ("context", 0.15),
            ("character", 0.15),
            ("timing", 0.15),
            ("spontaneity", 0.10),
        ),
        ai_smell_tags=("formulaic_opener", "character_drift"),
    )


def _comparisons(count: int = 2) -> tuple[dict, ...]:
    return tuple({
        "pair_ref": f"private-pair-{index}",
        "context_summary": f"public summary {index}",
        "previous": {
            "build_identity": "secret-build-old",
            "output": f"old output {index}",
            "director_score": 12.5,
            "prompt_ref": "secret-prompt-old",
            "memory_refs": ["secret-memory-old"],
        },
        "candidate": {
            "build_identity": "secret-build-new",
            "output": f"new output {index}",
            "director_score": 18.0,
            "prompt_ref": "secret-prompt-new",
            "memory_refs": ["secret-memory-new"],
        },
    } for index in range(count))


def _complete(artifact: dict, manifest: dict) -> dict:
    artifact["human_review"] = {"reviewer_role": "independent_operator", "complete": True}
    sealed = {row["pair_ref"]: row for row in manifest["rows"]}
    for row in artifact["rows"]:
        identity = sealed[row["pair_ref"]]
        for key in ("a", "b"):
            is_candidate = identity[key]["role"] == "candidate"
            value = 5 if is_candidate else 3
            dimensions = {name: value for name in DIMENSIONS}
            dimensions["timing"] = value - 1
            row["review"][key] = {
                "dimensions": dimensions,
                "ai_smell": not is_candidate,
                "ai_smell_tags": [] if is_candidate else ["formulaic_opener"],
                "liveness": value,
                "action_coherence": value,
                "note": f"grounded evidence for {key}",
            }
        row["review"]["preferred"] = (
            "A" if identity["a"]["role"] == "candidate" else "B"
        )
    return artifact


def test_blind_artifact_is_deterministic_and_hides_all_internals() -> None:
    first, first_manifest = HumanLikeCalibration(_config()).build(_comparisons())
    second, second_manifest = HumanLikeCalibration(_config()).build(_comparisons())
    assert first == second
    assert first_manifest == second_manifest
    rendered = json.dumps(first, ensure_ascii=False)
    for secret in (
        "secret-build", "secret-prompt", "secret-memory", "12.5", "18.0",
    ):
        assert secret not in rendered
    assert first["status"] == "pending_human_review"
    assert first["build_identity_hidden"] is True
    assert first["dimensions"] == dict(_config().dimensions)


@pytest.mark.asyncio
async def test_finalize_requires_persisted_score_then_reveals_bounded_metadata(
    tmp_path: Path,
) -> None:
    service = HumanLikeCalibration(_config())
    artifact, manifest = service.build(_comparisons())
    with pytest.raises(ValueError, match="persisted"):
        await service.finalize(tmp_path / "missing.json", manifest)
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(_complete(artifact, manifest), ensure_ascii=False), encoding="utf-8",
    )
    final = await service.finalize(path, manifest)
    assert final["status"] == "review_complete"
    assert final["automatic_release_decision"] is False
    assert final["previous_build_delta"] > 0
    assert final["summaries"]["candidate"]["weakest_dimension"] == "timing"
    assert final["summaries"]["previous"]["ai_smell_rate"] == 1.0
    assert "secret-build-new" in json.dumps(final, ensure_ascii=False)


@pytest.mark.asyncio
async def test_commitment_tamper_and_invalid_human_fields_fail_closed(tmp_path: Path) -> None:
    service = HumanLikeCalibration(_config())
    artifact, manifest = service.build(_comparisons(1))
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(_complete(artifact, manifest), ensure_ascii=False), encoding="utf-8",
    )
    broken_manifest = json.loads(json.dumps(manifest))
    broken_manifest["rows"][0]["a"]["build_identity"] = "tampered"
    with pytest.raises(ValueError, match="commitment"):
        await service.finalize(path, broken_manifest)

    artifact, manifest = service.build(_comparisons(1))
    reviewed = _complete(artifact, manifest)
    reviewed["rows"][0]["candidate_a"] = "replacement output"
    path.write_text(json.dumps(reviewed), encoding="utf-8")
    with pytest.raises(ValueError, match="content was modified"):
        await service.finalize(path, manifest)

    artifact, manifest = service.build(_comparisons(1))
    reviewed = _complete(artifact, manifest)
    reviewed["rows"][0]["review"]["a"]["ai_smell"] = False
    reviewed["rows"][0]["review"]["a"]["ai_smell_tags"] = ["formulaic_opener"]
    path.write_text(json.dumps(reviewed), encoding="utf-8")
    with pytest.raises(ValueError, match="must agree"):
        await service.finalize(path, manifest)


@pytest.mark.asyncio
async def test_review_notes_are_sanitized_and_metrics_are_fail_isolated(
    tmp_path: Path,
) -> None:
    metrics = MetricsCollector()
    service = HumanLikeCalibration(_config(), metrics=metrics)
    await service.start()
    artifact, manifest = service.build(_comparisons(1))
    reviewed = _complete(artifact, manifest)
    reviewed["rows"][0]["review"]["a"]["note"] = "contact private@example.com"
    path = tmp_path / "review.json"
    path.write_text(json.dumps(reviewed), encoding="utf-8")
    final = await service.finalize(path, manifest)
    assert "private@example.com" not in json.dumps(final)
    assert metrics.human_like_review_snapshot() == {"built": 1, "finalized": 1}
    assert (await service.health_check()).is_ok
    await service.stop()


def test_strict_config_rejects_extra_keys_coercion_and_weight_drift() -> None:
    raw = {
        "schema_version": 1,
        "seed": 17,
        "min_pairs": 1,
        "max_pairs": 2,
        "max_output_chars": 100,
        "max_context_chars": 100,
        "max_note_chars": 100,
        "max_ref_chars": 80,
        "dimensions": dict(_config().dimensions),
        "ai_smell_tags": ["formulaic_opener"],
    }

    class Loader:
        def __init__(self, value: dict) -> None:
            self.value = value

        def get(self, *_args, **_kwargs):
            return self.value

    assert HumanLikeConfig.from_loader(Loader(raw)).schema_version == 1
    with pytest.raises(ValueError, match="keys"):
        HumanLikeConfig.from_loader(Loader({**raw, "extra": 1}))
    with pytest.raises(ValueError, match="integer"):
        HumanLikeConfig.from_loader(Loader({**raw, "min_pairs": "1"}))
    drifted = {**raw, "dimensions": {**raw["dimensions"], "language": 0.21}}
    with pytest.raises(ValueError, match="sum"):
        HumanLikeConfig.from_loader(Loader(drifted))
