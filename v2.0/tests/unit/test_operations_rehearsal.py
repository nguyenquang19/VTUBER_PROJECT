from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.build_operations_rehearsal import (
    CHECKS,
    LIVE_CANARIES,
    build_operations_rehearsal,
)
from services.evaluation.release_gate import SourceState


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
REVISION = "a" * 40


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _fixture(tmp_path: Path):
    config = tmp_path / "config"
    config.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "config" / "system.yaml", config / "system.yaml")
    shutil.copy2(REPO_ROOT / "config" / "operations.yaml", config / "operations.yaml")
    shutil.copy2(REPO_ROOT / "config" / "evaluation.yaml", config / "evaluation.yaml")
    preflight = _write(tmp_path / "preflight.json", {
        "marker": "mai_live_preflight", "sanitized": True, "ready": True,
    })
    reports = {}
    for check_id in sorted(set(CHECKS) | set(LIVE_CANARIES)):
        reports[check_id] = _write(tmp_path / f"{check_id}.json", {
            "schema_version": 1,
            "marker": "mai_operation_check",
            "sanitized": True,
            "check_id": check_id,
            "generated_at_utc": NOW.isoformat(),
            "status": "passed",
            "evidence_refs": [f"test:{check_id}"],
        })
    return preflight, reports


def test_operations_rehearsal_hashes_every_required_check(tmp_path: Path) -> None:
    preflight, reports = _fixture(tmp_path)
    output = build_operations_rehearsal(
        preflight, reports, repo_root=tmp_path,
        source_state=SourceState(REVISION, True), now_utc=NOW,
    )
    assert output["marker"] == "mai_operations_rehearsal"
    assert output["status"] == "passed"
    assert len(output["evidence_refs"]) == len(CHECKS) + len(LIVE_CANARIES)
    assert all(":sha256:" in item for item in output["evidence_refs"])
    assert all(output[field] is True for field in CHECKS.values())
    assert set(output["live_canaries"]) == set(LIVE_CANARIES.values())


def test_operations_rehearsal_rejects_missing_or_dirty_evidence(tmp_path: Path) -> None:
    preflight, reports = _fixture(tmp_path)
    reports.pop("pii_scan")
    with pytest.raises(ValueError, match="incomplete"):
        build_operations_rehearsal(
            preflight, reports, repo_root=tmp_path,
            source_state=SourceState(REVISION, True), now_utc=NOW,
        )
    _, reports = _fixture(tmp_path / "dirty")
    with pytest.raises(RuntimeError, match="clean worktree"):
        build_operations_rehearsal(
            tmp_path / "dirty" / "preflight.json", reports, repo_root=tmp_path / "dirty",
            source_state=SourceState(REVISION, False), now_utc=NOW,
        )


def test_operations_rehearsal_rejects_stale_or_failed_check(tmp_path: Path) -> None:
    preflight, reports = _fixture(tmp_path)
    value = json.loads(reports["secrets_scan"].read_text(encoding="utf-8"))
    value["generated_at_utc"] = (NOW - timedelta(days=2)).isoformat()
    value["status"] = "failed"
    _write(reports["secrets_scan"], value)
    with pytest.raises(ValueError, match="contract failed|stale"):
        build_operations_rehearsal(
            preflight, reports, repo_root=tmp_path,
            source_state=SourceState(REVISION, True), now_utc=NOW,
        )


def test_operations_rehearsal_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    preflight, reports = _fixture(tmp_path)
    reports["backup_restore"].write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        build_operations_rehearsal(
            preflight, reports, repo_root=tmp_path,
            source_state=SourceState(REVISION, True), now_utc=NOW,
        )
