from __future__ import annotations

import json
from pathlib import Path

from scripts.run_human_like_review import main
from services.evaluation.human_like import DIMENSIONS


def _input() -> dict:
    return {"comparisons": [{
        "pair_ref": f"pair-{index}",
        "context_summary": "sanitized stream situation",
        "previous": {
            "build_identity": "build-old",
            "output": f"old output {index}",
            "director_score": 1.0,
            "prompt_ref": "prompt-old",
            "memory_refs": ["memory-old"],
        },
        "candidate": {
            "build_identity": "build-new",
            "output": f"new output {index}",
            "director_score": 2.0,
            "prompt_ref": "prompt-new",
            "memory_refs": ["memory-new"],
        },
    } for index in range(20)]}


def test_cli_builds_pending_blind_artifact_then_finalizes_persisted_scores(
    tmp_path: Path, capsys,
) -> None:
    source = tmp_path / "input.json"
    review_path = tmp_path / "review.json"
    manifest_path = tmp_path / "sealed.json"
    final_path = tmp_path / "final.json"
    source.write_text(json.dumps(_input()), encoding="utf-8")
    assert main([
        "--input", str(source),
        "--review", str(review_path),
        "--manifest", str(manifest_path),
    ]) == 0
    capsys.readouterr()
    review_text = review_path.read_text(encoding="utf-8")
    assert "build-old" not in review_text
    assert "prompt-old" not in review_text

    artifact = json.loads(review_text)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact["human_review"] = {"reviewer_role": "operator", "complete": True}
    sealed = {row["pair_ref"]: row for row in manifest["rows"]}
    for row in artifact["rows"]:
        identity = sealed[row["pair_ref"]]
        for key in ("a", "b"):
            candidate = identity[key]["role"] == "candidate"
            score = 5 if candidate else 4
            row["review"][key] = {
                "dimensions": {name: score for name in DIMENSIONS},
                "ai_smell": False,
                "ai_smell_tags": [],
                "liveness": score,
                "action_coherence": score,
                "note": "human evidence",
            }
        row["review"]["preferred"] = (
            "A" if identity["a"]["role"] == "candidate" else "B"
        )
    review_path.write_text(json.dumps(artifact), encoding="utf-8")

    assert main([
        "--finalize", str(review_path),
        "--manifest", str(manifest_path),
        "--output", str(final_path),
    ]) == 0
    capsys.readouterr()
    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert final["status"] == "review_complete"
    assert final["previous_build_delta"] == 1.0
    assert final["automatic_release_decision"] is False
