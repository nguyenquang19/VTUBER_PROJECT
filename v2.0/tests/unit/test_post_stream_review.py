from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.operations.post_stream_review import PostStreamReviewer, ReviewConfig


def _config(tmp_path: Path) -> ReviewConfig:
    return ReviewConfig(
        shutdown_snapshot=tmp_path / "shutdown.json",
        incident_log=tmp_path / "incidents.jsonl",
        operator_audit=tmp_path / "audit.jsonl",
        soak_report=tmp_path / "soak.json",
        export_dir=tmp_path / "reviews",
    )


@pytest.mark.asyncio
async def test_review_ready_exports_metadata_only_checklist(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.shutdown_snapshot.write_text(json.dumps({
        "schema_version": 1, "shutdown_errors_before_snapshot": [],
    }), encoding="utf-8")
    config.incident_log.write_text("", encoding="utf-8")
    config.operator_audit.write_text(json.dumps({"action": "pause"}) + "\n", encoding="utf-8")
    config.soak_report.write_text(json.dumps({
        "passed": True, "configured_duration_s": 7200,
    }), encoding="utf-8")

    report = await PostStreamReviewer(config).review(tmp_path / "review.json")

    assert report["ready"] is True
    exported = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert exported["privacy"] == "metadata_only_no_chat_or_prompt_content"
    assert "records" not in json.dumps(exported)


@pytest.mark.asyncio
async def test_review_flags_unresolved_incident_and_missing_soak(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.shutdown_snapshot.write_text(json.dumps({
        "schema_version": 1, "shutdown_errors_before_snapshot": [],
    }), encoding="utf-8")
    config.incident_log.write_text(json.dumps({
        "incident_id": "inc:1", "status": "open",
    }) + "\n", encoding="utf-8")

    report = await PostStreamReviewer(config).review(tmp_path / "review.json")

    assert report["ready"] is False
    assert report["checklist"]["incident_log"]["unresolved_count"] == 1
    assert report["checklist"]["soak_acceptance"]["error"] == "missing"
