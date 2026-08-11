from __future__ import annotations

import json
import shutil
from pathlib import Path

from scripts.build_release_evidence import build_release_evidence


REPO_ROOT = Path(__file__).resolve().parents[2]


def _release_fixture(tmp_path: Path) -> Path:
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
        "m10_full_live_verification.json",
        "m10_full_live_verification.json",
    ):
        shutil.copy2(REPO_ROOT / "docs" / "baselines" / name, tmp_path / "docs" / "baselines" / name)
    for name in ("backup_data.py", "restore_data.py", "live_preflight.py", "start_live.ps1"):
        (tmp_path / "scripts" / name).write_text("present", encoding="utf-8")
    return tmp_path


def test_release_evidence_distinguishes_software_from_platform(tmp_path: Path) -> None:
    report = build_release_evidence(_release_fixture(tmp_path))
    assert report["software_ready"] is True
    assert report["platform_ready"] is None
    assert report["status"] == "software_ready_platform_preflight_pending"


def test_release_evidence_includes_failed_platform_preflight(tmp_path: Path) -> None:
    root = _release_fixture(tmp_path)
    preflight = tmp_path / "preflight.json"
    preflight.write_text(json.dumps({"schema_version": 1, "ready": False}), encoding="utf-8")
    report = build_release_evidence(root, preflight_report=preflight)
    assert report["software_ready"] is True
    assert report["platform_ready"] is False
    assert report["status"] == "software_ready_platform_blocked"
