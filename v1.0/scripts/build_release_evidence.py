"""Build a sanitized, deterministic M10 release-readiness report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def build_release_evidence(
    repo_root: Path = REPO_ROOT,
    *,
    preflight_report: Path | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve()
    loader = ConfigLoader(root / "config")
    loader.load_all()
    baseline = root / "docs" / "baselines"
    text_acceptance = _read_json(baseline / "m10_text_acceptance.json")
    smoke = _read_json(baseline / "m10_mood_hybrid_smoke_final.json")
    cutover = _read_json(baseline / "m10_hybrid_cutover.json")
    delivery = _read_json(baseline / "m10_tts_delivery_acceptance.json")
    verification = _read_json(baseline / "m10_full_live_verification.json")
    verification = _read_json(baseline / "m10_full_live_verification.json")

    gates = {
        "text_acceptance": text_acceptance.get("status") == "passed",
        "hybrid_smoke": smoke.get("status") == "passed",
        "hybrid_operator_cutover": (
            cutover.get("decision") == "approved" and cutover.get("cutover_enabled") is True
        ),
        "hybrid_feature_enabled": bool(loader.get(
            "features", "features.mood_v2_prompt.enabled", False,
        )),
        "legacy_mood_rollback_retained": (
            (root / "config" / "mood_engine.yaml").is_file()
            and (root / "services" / "emotion" / "modifiers.py").is_file()
        ),
        "operator_dashboard_v2_enabled": bool(loader.get(
            "features", "features.operator_dashboard_v2.enabled", False,
        )),
        "action_transactions_enabled": bool(loader.get(
            "features", "features.action_transactions.enabled", False,
        )),
        "decision_records_enabled": bool(loader.get(
            "features", "features.decision_records.enabled", False,
        )),
        "tts_delivery_contract": delivery.get("status") == "passed",
        "subtitle_delivery_required": (
            bool(loader.get("models", "tts_fallback.enabled", False))
            and bool(loader.get("models", "tts_fallback.require_delivery", False))
        ),
        "backup_tool": (root / "scripts" / "backup_data.py").is_file(),
        "restore_tool": (root / "scripts" / "restore_data.py").is_file(),
        "preflight_tool": (root / "scripts" / "live_preflight.py").is_file(),
        "one_command_launcher": (root / "scripts" / "start_live.ps1").is_file(),
        "offline_regression": (
            verification.get("status") == "passed"
            and verification.get("offline_regression", {}).get("failures") == 0
        ),
        "offline_regression": (
            verification.get("status") == "passed"
            and verification.get("offline_regression", {}).get("failures") == 0
        ),
    }
    software_ready = all(gates.values())

    platform_preflight: dict[str, Any] | None = None
    platform_ready: bool | None = None
    if preflight_report is not None:
        platform_preflight = _read_json(preflight_report)
        platform_ready = bool(platform_preflight.get("ready"))

    if not software_ready:
        status = "not_ready"
    elif platform_ready is False:
        status = "software_ready_platform_blocked"
    elif platform_ready is True:
        status = "ready_to_start_live"
    else:
        status = "software_ready_platform_preflight_pending"

    return {
        "schema_version": 1,
        "milestone": "M10.8",
        "marker": "full_live_release_evidence",
        "sanitized": True,
        "raw_transcript_included": False,
        "software_ready": software_ready,
        "platform_ready": platform_ready,
        "status": status,
        "gates": gates,
        "verification": verification,
        "verification": verification,
        "platform_preflight": platform_preflight,
        "rollback": {
            "mood": "disable features.mood_v2_prompt.enabled",
            "dashboard": "disable features.operator_dashboard_v2.enabled or open /legacy",
            "data": "verify with restore_data.py before using --apply",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build M10 full-live release evidence")
    parser.add_argument("--preflight")
    parser.add_argument(
        "--output", default=str(REPO_ROOT / "docs" / "baselines" / "m10_release_evidence.json"),
    )
    args = parser.parse_args()
    report = build_release_evidence(
        preflight_report=Path(args.preflight) if args.preflight else None,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["software_ready"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
