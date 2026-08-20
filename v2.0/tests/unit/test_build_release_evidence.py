from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.build_release_evidence import (
    _default_output_path,
    _release_exit_code,
    _write_json_atomic,
    build_release_evidence,
)
from services.evaluation.release_gate import SourceState


REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
REVISION = "a" * 40
CURRENT = "1.4.3"
TARGET = "2.0.0"
GROUPS = ("targeted", "offline", "llm", "slow", "smoke")
COUNTERS = (
    "unauthorized_executed_actions", "unavailable_capability_executed",
    "duplicate_committed_actions", "false_committed_world_states",
    "transaction_inconsistencies",
)
PREFLIGHT_CHECKS = (
    "windows", "python", "credential_contract", "llama_binary", "llm_model",
    "tts_reference", "transactions", "decision_records", "subtitle_fallback",
    "subtitle_path", "platform", "llama_health", "youtube_video",
)


def _common(marker: str) -> dict[str, Any]:
    return {
        "schema_version": 2, "marker": marker, "sanitized": True,
        "source_revision": REVISION, "current_product_version": CURRENT,
        "target_product_version": TARGET, "generated_at_utc": NOW.isoformat(),
    }


def _verification() -> dict[str, Any]:
    return {
        **_common("mai_release_verification"),
        "runner_id": "mai-fixed-verification-v1", "clean_worktree": True,
        "test_groups": {
            name: {
                "command_id": name, "passed": 1, "failures": 0,
                "skipped": 0, "deselected": 0, "duration_seconds": 0.1,
            }
            for name in GROUPS
        },
        "correctness": {name: 0 for name in COUNTERS},
        "bounded_state_passed": True, "status": "passed",
    }


def _preflight(*, ready: bool = True) -> dict[str, Any]:
    return {
        "schema_version": 1, "marker": "mai_live_preflight", "sanitized": True,
        "product_version": CURRENT, "generated_at_utc": NOW.isoformat(),
        "ready": ready, "platform": "youtube",
        "checks": [
            {"name": name, "passed": ready, "detail": "sanitized", "blocking": True}
            for name in PREFLIGHT_CHECKS
        ],
    }


def _canary() -> dict[str, Any]:
    return {
        **_common("mai_closed_loop_canary"), "canary_id": "canary-000001",
        "started_at_utc": NOW.isoformat(),
        "action": {
            "action_id": "action-1", "proposal_id": "proposal-1",
            "action_type": "SWITCH_SCENE", "capability_id": "SWITCH_SCENE",
        },
        "pre_snapshot": {
            "world_snapshot_id": "world-before", "self_snapshot_id": "self-1",
            "capability_snapshot_id": "cap-1",
        },
        "post_snapshot": {
            "world_snapshot_id": "world-after", "self_snapshot_id": "self-1",
            "capability_snapshot_id": "cap-2",
        },
        "result": {
            "status": "success", "verified": True,
            "verification_source": "obs_current_scene", "transaction_committed": True,
            "world_projected": True, "rollback_outcome": "not_required",
        },
        "next_decision": {
            "proposal_id": "proposal-2", "action_type": "WAIT",
            "capability_rechecked": True,
        },
        "outcome": "passed", "reason_code": "closed_loop_verified", "passed": True,
    }


def _human() -> dict[str, Any]:
    return {
        **_common("mai_human_quality_evidence"), "review_digest": "b" * 64,
        "reviewed_pairs": 20,
        "previous": {
            "weighted_average": 0.60, "ai_smell_rate": 0.10,
            "character_average": 0.70,
        },
        "candidate": {
            "weighted_average": 0.61, "ai_smell_rate": 0.09,
            "character_average": 0.71,
        },
        "previous_build_delta": 0.01, "operator_approved": True, "status": "passed",
    }


def _operations(preflight_digest: str) -> dict[str, Any]:
    return {
        **_common("mai_operations_rehearsal"), "preflight_sha256": preflight_digest,
        "backup_restore_verified": True, "deny_by_default_permissions": True,
        "secrets_scan_passed": True, "pii_scan_passed": True,
        "emergency_stop_passed": True, "graceful_shutdown_passed": True,
        "rollback_rehearsal_passed": True,
        "live_canaries": {
            "platform": True, "audio": True, "avatar": True,
            "obs": True, "memory": True,
        },
        "evidence_refs": ["operator-rehearsal:2026-08-20"], "status": "passed",
    }


def _write(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy2(REPO_ROOT / "config" / "system.yaml", config / "system.yaml")
    shutil.copy2(REPO_ROOT / "config" / "operations.yaml", config / "operations.yaml")
    paths = {
        "verification": _write(tmp_path / "verification.json", _verification()),
        "preflight": _write(tmp_path / "preflight.json", _preflight()),
        "canary": _write(tmp_path / "canary.json", _canary()),
        "human": _write(tmp_path / "human.json", _human()),
    }
    digest = hashlib.sha256(paths["preflight"].read_bytes()).hexdigest()
    paths["operations"] = _write(tmp_path / "operations.json", _operations(digest))
    return tmp_path, paths


def _build(root: Path, paths: dict[str, Path] | None = None, *, clean: bool = True):
    args: dict[str, Any] = {}
    if paths:
        args = {
            "verification_report": paths["verification"],
            "preflight_report": paths["preflight"],
            "canary_report": paths["canary"], "human_report": paths["human"],
            "operations_report": paths["operations"],
        }
    return build_release_evidence(
        root, now_utc=NOW, source_state=SourceState(REVISION, clean), **args,
    )


def test_missing_evidence_fails_every_release_gate(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)
    report = _build(root)
    assert report["status"] == "not_ready"
    assert report["release_ready"] is False
    assert report["configuration_ready"] is False
    assert report["canary_passed"] is False
    assert not any(report["gates"].values())


def test_default_release_output_does_not_dirty_source_tree() -> None:
    path = _default_output_path(REPO_ROOT, TARGET)
    assert path == REPO_ROOT / "logs" / "operations" / "release_evidence_2_0_0.json"


def test_current_clean_artifacts_are_eligible_for_version_bump(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    report = _build(root, paths)
    assert report["status"] == "eligible_for_version_bump"
    assert report["eligible_for_version_bump"] is True
    assert report["release_ready"] is False
    assert report["configuration_ready"] is True
    assert report["canary_passed"] is True
    assert all(report["gates"].values())
    assert _release_exit_code(report, require_release_ready=False) == 0
    assert _release_exit_code(report, require_release_ready=True) == 1


def test_dirty_source_blocks_otherwise_valid_evidence(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    report = _build(root, paths, clean=False)
    assert report["status"] == "not_ready"
    assert "worktree_not_clean" in report["reasons"]


@pytest.mark.parametrize("artifact", ("verification", "canary", "human", "operations"))
def test_source_revision_mismatch_rejects_source_bound_artifact(
    tmp_path: Path, artifact: str,
) -> None:
    root, paths = _fixture(tmp_path)
    value = json.loads(paths[artifact].read_text(encoding="utf-8"))
    value["source_revision"] = "c" * 40
    _write(paths[artifact], value)
    report = _build(root, paths)
    key = {
        "canary": "closed_loop_canary", "human": "human_quality",
        "operations": "operations_rehearsal",
    }.get(artifact, artifact)
    assert report["artifacts"][key]["valid"] is False


def test_verification_cannot_self_declare_zero_tests_or_zero_counter(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    value = _verification()
    value["test_groups"]["targeted"]["passed"] = 0
    value["correctness"][COUNTERS[0]] = 1
    value["status"] = "failed"
    _write(paths["verification"], value)
    errors = _build(root, paths)["artifacts"]["verification"]["errors"]
    assert any("passed no checks" in item for item in errors)
    assert any(COUNTERS[0] in item for item in errors)


def test_canary_requires_world_change_and_verified_projection(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    value = _canary()
    value["post_snapshot"] = deepcopy(value["pre_snapshot"])
    value["result"]["world_projected"] = False
    value["passed"] = False
    value["outcome"] = "failed"
    _write(paths["canary"], value)
    errors = _build(root, paths)["artifacts"]["closed_loop_canary"]["errors"]
    assert any("world snapshot did not change" in item for item in errors)
    assert any("transaction gate failed" in item for item in errors)


def test_human_regression_and_tampered_preflight_digest_fail_closed(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    human = _human()
    human["candidate"]["weighted_average"] = 0.50
    human["previous_build_delta"] = -0.10
    _write(paths["human"], human)
    _write(paths["operations"], _operations("d" * 64))
    report = _build(root, paths)
    assert report["gates"]["human_like_quality"] is False
    assert report["gates"]["operations_security"] is False
    assert "operations preflight digest mismatch" in report["artifacts"]["operations_rehearsal"]["errors"]


def test_preflight_deferred_required_health_check_is_not_release_evidence(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    value = _preflight()
    health = next(item for item in value["checks"] if item["name"] == "llama_health")
    health["detail"] = "deferred: runtime may check later"
    _write(paths["preflight"], value)
    report = _build(root, paths)
    assert report["gates"]["operations_security"] is False
    assert any(
        "directly verified" in item for item in report["artifacts"]["preflight"]["errors"]
    )


def test_human_nan_does_not_bypass_quality_gate(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    value = _human()
    value["candidate"]["weighted_average"] = float("nan")
    value["previous_build_delta"] = float("nan")
    _write(paths["human"], value)
    assert _build(root, paths)["artifacts"]["human_quality"]["valid"] is False


@pytest.mark.parametrize("generated_at", (NOW - timedelta(days=2), NOW + timedelta(seconds=31)))
def test_stale_or_future_artifact_is_rejected(
    tmp_path: Path, generated_at: datetime,
) -> None:
    root, paths = _fixture(tmp_path)
    value = _verification()
    value["generated_at_utc"] = generated_at.isoformat()
    _write(paths["verification"], value)
    assert _build(root, paths)["artifacts"]["verification"]["valid"] is False


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    root, paths = _fixture(tmp_path)
    paths["verification"].write_text(
        '{"schema_version":2,"schema_version":2}', encoding="utf-8",
    )
    report = _build(root, paths)
    assert report["artifacts"]["verification"]["valid"] is False
    assert any("ValueError" in item for item in report["artifacts"]["verification"]["errors"])


def test_atomic_write_preserves_destination_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "evidence.json"
    destination.write_text("old-valid-evidence", encoding="utf-8")

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        _write_json_atomic(destination, {"release_ready": True})
    assert destination.read_text(encoding="utf-8") == "old-valid-evidence"
    assert not destination.with_suffix(".json.tmp").exists()
