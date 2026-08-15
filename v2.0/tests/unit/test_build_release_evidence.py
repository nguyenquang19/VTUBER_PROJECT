from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.build_release_evidence import (
    _release_exit_code,
    _write_json_atomic,
    build_release_evidence,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_VERSION = "1.4.3"
NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _verification() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "marker": "mai_release_verification",
        "sanitized": True,
        "product_version": PRODUCT_VERSION,
        "status": "passed",
        "test_groups": {
            name: {"passed": 1, "failures": 0}
            for name in ("targeted", "offline", "llm", "slow", "smoke")
        },
    }


def _preflight(
    *,
    ready: bool = True,
    generated_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "marker": "mai_live_preflight",
        "sanitized": True,
        "product_version": PRODUCT_VERSION,
        "generated_at_utc": generated_at.isoformat(),
        "ready": ready,
        "platform": "youtube",
        "checks": [{
            "name": "platform",
            "passed": ready,
            "detail": "sanitized platform result",
            "blocking": True,
        }],
    }


def _release_fixture(tmp_path: Path) -> tuple[Path, Path]:
    for relative in (
        "config", "docs/baselines", "scripts", "services/emotion",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    for name in (
        "features.yaml", "models.yaml", "system.yaml", "mood_engine.yaml", "operations.yaml",
    ):
        shutil.copy2(REPO_ROOT / "config" / name, tmp_path / "config" / name)
    shutil.copy2(
        REPO_ROOT / "services" / "emotion" / "modifiers.py",
        tmp_path / "services" / "emotion" / "modifiers.py",
    )
    for name in (
        "m10_text_acceptance.json", "m10_mood_hybrid_smoke_final.json",
        "m10_hybrid_cutover.json", "m10_tts_delivery_acceptance.json",
    ):
        shutil.copy2(
            REPO_ROOT / "docs" / "baselines" / name,
            tmp_path / "docs" / "baselines" / name,
        )
    for name in ("backup_data.py", "restore_data.py", "live_preflight.py", "start_live.ps1"):
        (tmp_path / "scripts" / name).write_text("present", encoding="utf-8")
    verification = _write_json(tmp_path / "verification.json", _verification())
    return tmp_path, verification


def _build(root: Path, **kwargs: Any) -> dict[str, Any]:
    return build_release_evidence(root, now_utc=NOW, **kwargs)


def test_release_evidence_distinguishes_software_from_platform(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    report = _build(root, verification_report=verification)
    assert report["software_ready"] is True
    assert report["platform_ready"] is None
    assert report["status"] == "software_ready_platform_preflight_pending"
    assert report["verification_validation"] == {"valid": True, "errors": []}
    assert report["generated_at_utc"] == NOW.isoformat()


def test_release_evidence_includes_valid_failed_platform_preflight(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = _write_json(tmp_path / "preflight.json", _preflight(ready=False))
    report = _build(root, preflight_report=preflight, verification_report=verification)
    assert report["software_ready"] is True
    assert report["platform_ready"] is False
    assert report["status"] == "software_ready_platform_blocked"
    assert report["platform_preflight_validation"] == {"valid": True, "errors": []}


def test_release_evidence_rejects_tampered_preflight_ready_flag(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _preflight(ready=False)
    value["ready"] = True
    preflight = _write_json(tmp_path / "preflight.json", value)
    report = _build(root, preflight_report=preflight, verification_report=verification)
    assert report["platform_ready"] is False
    assert report["platform_preflight"] is None
    assert any("does not match" in error
               for error in report["platform_preflight_validation"]["errors"])


def test_release_evidence_rejects_stale_version_or_duplicate_check(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _preflight()
    value["product_version"] = "1.4.2"
    value["checks"].append(deepcopy(value["checks"][0]))
    preflight = _write_json(tmp_path / "preflight.json", value)
    report = _build(root, preflight_report=preflight, verification_report=verification)
    errors = report["platform_preflight_validation"]["errors"]
    assert report["platform_ready"] is False
    assert any("product_version" in error for error in errors)
    assert any("unique" in error for error in errors)


def test_release_evidence_fails_closed_for_malformed_preflight(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = tmp_path / "preflight.json"
    preflight.write_text("{not json", encoding="utf-8")
    report = _build(root, preflight_report=preflight, verification_report=verification)
    assert report["platform_ready"] is False
    assert report["platform_preflight_validation"]["valid"] is False
    assert report["platform_preflight"] is None


def test_release_evidence_rejects_preflight_without_contract_marker(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = _write_json(tmp_path / "preflight.json", {"ready": True})
    report = _build(root, preflight_report=preflight, verification_report=verification)
    assert report["platform_ready"] is False
    assert report["platform_preflight_validation"]["valid"] is False


def test_release_evidence_rejects_stale_verification(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _verification()
    value["product_version"] = "1.4.2"
    _write_json(verification, value)
    report = _build(root, verification_report=verification)
    assert report["software_ready"] is False
    assert report["gates"]["current_release_verification"] is False
    assert report["status"] == "not_ready"
    assert report["verification"] is None


def test_release_evidence_rejects_missing_test_group(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _verification()
    del value["test_groups"]["smoke"]
    _write_json(verification, value)
    report = _build(root, verification_report=verification)
    assert report["software_ready"] is False
    assert any("smoke" in error for error in report["verification_validation"]["errors"])


def test_release_evidence_rejects_zero_pass_or_failed_test_group(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _verification()
    value["test_groups"]["targeted"] = {"passed": 0, "failures": 0}
    value["test_groups"]["offline"] = {"passed": 1, "failures": 1}
    _write_json(verification, value)
    report = _build(root, verification_report=verification)
    errors = report["verification_validation"]["errors"]
    assert report["software_ready"] is False
    assert any("targeted.passed" in error for error in errors)
    assert any("offline.failures" in error for error in errors)


def test_release_evidence_fails_closed_for_malformed_verification(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    verification.write_text("[] trailing", encoding="utf-8")
    report = _build(root, verification_report=verification)
    assert report["software_ready"] is False
    assert report["verification_validation"]["valid"] is False
    assert report["verification"] is None


def test_release_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    verification.write_text(
        '{"schema_version": 1, "schema_version": 1}', encoding="utf-8",
    )
    report = _build(root, verification_report=verification)
    assert report["software_ready"] is False
    assert report["verification_validation"]["valid"] is False
    assert any("ValueError" in error for error in report["verification_validation"]["errors"])


@pytest.mark.parametrize(
    ("generated_at", "expected_error"),
    (
        (NOW - timedelta(seconds=301), "stale"),
        (NOW + timedelta(seconds=31), "future clock skew"),
    ),
)
def test_release_evidence_rejects_preflight_outside_freshness_window(
    tmp_path: Path,
    generated_at: datetime,
    expected_error: str,
) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = _write_json(tmp_path / "preflight.json", _preflight(generated_at=generated_at))
    report = _build(root, preflight_report=preflight, verification_report=verification)
    assert report["platform_ready"] is False
    assert any(expected_error in error
               for error in report["platform_preflight_validation"]["errors"])


@pytest.mark.parametrize("timestamp", ("not-a-date", "2026-08-14T08:00:00"))
def test_release_evidence_rejects_invalid_or_naive_preflight_timestamp(
    tmp_path: Path,
    timestamp: str,
) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _preflight()
    value["generated_at_utc"] = timestamp
    preflight = _write_json(tmp_path / "preflight.json", value)
    report = _build(root, preflight_report=preflight, verification_report=verification)
    assert report["platform_ready"] is False
    assert any("timezone-aware UTC" in error
               for error in report["platform_preflight_validation"]["errors"])


def test_release_evidence_accepts_fresh_preflight_and_cli_exit_semantics(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    fresh = _write_json(tmp_path / "fresh.json", _preflight())
    ready_report = _build(root, preflight_report=fresh, verification_report=verification)
    assert ready_report["platform_ready"] is True
    assert ready_report["status"] == "ready_to_start_live"
    assert _release_exit_code(ready_report, preflight_requested=True) == 0

    blocked = deepcopy(ready_report)
    blocked["platform_ready"] = False
    assert _release_exit_code(blocked, preflight_requested=True) == 1
    assert _release_exit_code(blocked, preflight_requested=False) == 0


def test_release_evidence_atomic_write_preserves_existing_destination_on_replace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "evidence.json"
    destination.write_text("old-valid-evidence", encoding="utf-8")

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        _write_json_atomic(destination, {"software_ready": True})

    assert destination.read_text(encoding="utf-8") == "old-valid-evidence"
    assert not destination.with_suffix(".json.tmp").exists()


def test_release_evidence_cli_exits_nonzero_for_blocked_requested_preflight(
    tmp_path: Path,
) -> None:
    verification = _write_json(tmp_path / "verification.json", _verification())
    preflight = _write_json(
        tmp_path / "preflight.json",
        _preflight(ready=False, generated_at=datetime.now(timezone.utc)),
    )
    output = tmp_path / "evidence.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "build_release_evidence.py"),
            "--preflight", str(preflight),
            "--verification", str(verification),
            "--output", str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["software_ready"] is True
    assert report["platform_ready"] is False
    assert report["status"] == "software_ready_platform_blocked"
