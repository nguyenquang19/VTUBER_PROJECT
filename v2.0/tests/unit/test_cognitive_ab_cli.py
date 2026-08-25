"""CLI persistence boundary for MCB-4 offline review artifacts."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.run_cognitive_ab_replay import _source_summary, main
from services.evaluation.human_like import DIMENSIONS
from tests.unit.test_cognitive_ab_evaluation import _complete, _evaluation, _source


def test_cli_builds_atomic_blind_files_then_finalizes_without_auto_go(
    tmp_path: Path, capsys,
) -> None:
    evaluation = _evaluation()
    input_path = tmp_path / "source.json"
    private_path = tmp_path / "private.json"
    review_path = tmp_path / "review.json"
    manifest_path = tmp_path / "manifest.json"
    final_path = tmp_path / "final.json"
    input_path.write_text(json.dumps(_source(evaluation)), encoding="utf-8")
    assert main([
        "--input", str(input_path),
        "--private", str(private_path),
        "--review", str(review_path),
        "--manifest", str(manifest_path),
    ]) == 0
    capsys.readouterr()
    review = json.loads(review_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(review["rows"]) == 30
    assert all("Tập:" in row["context_summary"] for row in review["rows"])
    assert not list(tmp_path.glob("*.tmp"))
    review_path.write_text(
        json.dumps(_complete(review, manifest), ensure_ascii=False), encoding="utf-8",
    )
    assert main([
        "--finalize", str(review_path),
        "--manifest", str(manifest_path),
        "--private", str(private_path),
        "--output", str(final_path),
    ]) == 0
    capsys.readouterr()
    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert final["owner_go_no_go_required"] is True
    assert final["automatic_release_decision"] is False
    assert set(final["human_like"]["summaries"]) == {"previous", "candidate"}
    assert set(DIMENSIONS) == set(
        final["human_like"]["summaries"]["candidate"]["dimension_averages"],
    )


def test_source_summary_keeps_failures_and_both_wait_in_denominator() -> None:
    evaluation = _evaluation()
    source = _source(evaluation)
    summary = _source_summary(source)
    assert summary["cases"] == 40
    assert summary["informative_pairs"] == 38
    assert summary["mode_matrix"]["WAIT->WAIT"] == 2
