from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.build_release_evidence import build_release_evidence


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_VERSION = "1.4.2"


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


def _preflight(*, ready: bool = True) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "marker": "mai_live_preflight",
        "sanitized": True,
        "product_version": PRODUCT_VERSION,
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
    for name in ("features.yaml", "models.yaml", "system.yaml", "mood_engine.yaml"):
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


def test_release_evidence_distinguishes_software_from_platform(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    report = build_release_evidence(root, verification_report=verification)
    assert report["software_ready"] is True
    assert report["platform_ready"] is None
    assert report["status"] == "software_ready_platform_preflight_pending"
    assert report["verification_validation"] == {"valid": True, "errors": []}


def test_release_evidence_includes_valid_failed_platform_preflight(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = _write_json(tmp_path / "preflight.json", _preflight(ready=False))
    report = build_release_evidence(
        root, preflight_report=preflight, verification_report=verification,
    )
    assert report["software_ready"] is True
    assert report["platform_ready"] is False
    assert report["status"] == "software_ready_platform_blocked"
    assert report["platform_preflight_validation"] == {"valid": True, "errors": []}


def test_release_evidence_rejects_tampered_preflight_ready_flag(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _preflight(ready=False)
    value["ready"] = True
    preflight = _write_json(tmp_path / "preflight.json", value)
    report = build_release_evidence(
        root, preflight_report=preflight, verification_report=verification,
    )
    assert report["platform_ready"] is False
    assert report["platform_preflight"] is None
    assert report["platform_preflight_validation"]["valid"] is False
    assert any("does not match" in error
               for error in report["platform_preflight_validation"]["errors"])


def test_release_evidence_rejects_stale_or_duplicate_preflight(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _preflight()
    value["product_version"] = "1.4.1"
    value["checks"].append(deepcopy(value["checks"][0]))
    preflight = _write_json(tmp_path / "preflight.json", value)
    report = build_release_evidence(
        root, preflight_report=preflight, verification_report=verification,
    )
    errors = report["platform_preflight_validation"]["errors"]
    assert report["platform_ready"] is False
    assert any("product_version" in error for error in errors)
    assert any("unique" in error for error in errors)


def test_release_evidence_fails_closed_for_malformed_preflight(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = tmp_path / "preflight.json"
    preflight.write_text("{not json", encoding="utf-8")
    report = build_release_evidence(
        root, preflight_report=preflight, verification_report=verification,
    )
    assert report["platform_ready"] is False
    assert report["platform_preflight_validation"]["valid"] is False
    assert report["platform_preflight"] is None


def test_release_evidence_rejects_preflight_without_contract_marker(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    preflight = _write_json(tmp_path / "preflight.json", {"ready": True})
    report = build_release_evidence(
        root, preflight_report=preflight, verification_report=verification,
    )
    assert report["platform_ready"] is False
    assert report["platform_preflight_validation"]["valid"] is False


def test_release_evidence_rejects_stale_verification(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _verification()
    value["product_version"] = "1.4.1"
    _write_json(verification, value)
    report = build_release_evidence(root, verification_report=verification)
    assert report["software_ready"] is False
    assert report["gates"]["current_release_verification"] is False
    assert report["status"] == "not_ready"
    assert report["verification"] is None


def test_release_evidence_rejects_missing_test_group(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _verification()
    del value["test_groups"]["smoke"]
    _write_json(verification, value)
    report = build_release_evidence(root, verification_report=verification)
    assert report["software_ready"] is False
    assert any("smoke" in error for error in report["verification_validation"]["errors"])


def test_release_evidence_rejects_zero_pass_or_failed_test_group(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    value = _verification()
    value["test_groups"]["targeted"] = {"passed": 0, "failures": 0}
    value["test_groups"]["offline"] = {"passed": 1, "failures": 1}
    _write_json(verification, value)
    report = build_release_evidence(root, verification_report=verification)
    errors = report["verification_validation"]["errors"]
    assert report["software_ready"] is False
    assert any("targeted.passed" in error for error in errors)
    assert any("offline.failures" in error for error in errors)


def test_release_evidence_fails_closed_for_malformed_verification(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    verification.write_text("[] trailing", encoding="utf-8")
    report = build_release_evidence(root, verification_report=verification)
    assert report["software_ready"] is False
    assert report["verification_validation"]["valid"] is False
    assert report["verification"] is None


def test_release_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root, verification = _release_fixture(tmp_path)
    verification.write_text(
        '{"schema_version": 1, "schema_version": 1}', encoding="utf-8",
    )
    report = build_release_evidence(root, verification_report=verification)
    assert report["software_ready"] is False
    assert report["verification_validation"]["valid"] is False
    assert any("ValueError" in error for error in report["verification_validation"]["errors"])
