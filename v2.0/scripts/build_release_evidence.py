"""Build sanitized release-readiness evidence for the current Mai version."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402


REQUIRED_TEST_GROUPS = ("targeted", "offline", "llm", "slow", "smoke")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_untrusted_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _load_untrusted_json(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        return _read_untrusted_json(path), []
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, [f"{label} could not be loaded: {type(exc).__name__}"]


def _validate_preflight(
    report: dict[str, Any],
    expected_version: str,
    *,
    now_utc: datetime,
    max_age_s: float,
    max_future_skew_s: float,
) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != 1:
        errors.append("preflight schema_version must be 1")
    if report.get("marker") != "mai_live_preflight":
        errors.append("preflight marker is invalid")
    if report.get("sanitized") is not True:
        errors.append("preflight must declare sanitized=true")
    if report.get("product_version") != expected_version:
        errors.append("preflight product_version does not match the current release")
    generated_at = _parse_utc_timestamp(report.get("generated_at_utc"))
    if generated_at is None:
        errors.append("preflight generated_at_utc must be a timezone-aware UTC timestamp")
    elif generated_at > now_utc + timedelta(seconds=max_future_skew_s):
        errors.append("preflight generated_at_utc exceeds allowed future clock skew")
    elif now_utc - generated_at > timedelta(seconds=max_age_s):
        errors.append("preflight report is stale")
    if report.get("platform") not in {"youtube", "discord"}:
        errors.append("preflight platform must be youtube or discord")
    if type(report.get("ready")) is not bool:
        errors.append("preflight ready must be a boolean")

    checks = report.get("checks")
    blocking_results: list[bool] = []
    names: set[str] = set()
    if not isinstance(checks, list) or not checks:
        errors.append("preflight checks must be a non-empty list")
    else:
        for index, check in enumerate(checks):
            prefix = f"preflight check {index}"
            if not isinstance(check, dict):
                errors.append(f"{prefix} must be an object")
                continue
            name = check.get("name")
            passed = check.get("passed")
            blocking = check.get("blocking")
            detail = check.get("detail")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{prefix} name must be a non-empty string")
            elif name in names:
                errors.append("preflight check names must be unique")
            else:
                names.add(name)
            if type(passed) is not bool:
                errors.append(f"{prefix} passed must be a boolean")
            if type(blocking) is not bool:
                errors.append(f"{prefix} blocking must be a boolean")
            if not isinstance(detail, str):
                errors.append(f"{prefix} detail must be a string")
            if type(passed) is bool and blocking is True:
                blocking_results.append(passed)

    ready = report.get("ready")
    if type(ready) is bool and isinstance(checks, list) and checks:
        if ready != all(blocking_results):
            errors.append("preflight ready does not match its blocking checks")
    return errors


def _validate_verification(report: dict[str, Any], expected_version: str) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != 1:
        errors.append("verification schema_version must be 1")
    if report.get("marker") != "mai_release_verification":
        errors.append("verification marker is invalid")
    if report.get("sanitized") is not True:
        errors.append("verification must declare sanitized=true")
    if report.get("product_version") != expected_version:
        errors.append("verification product_version does not match the current release")
    if report.get("status") != "passed":
        errors.append("verification status must be passed")

    groups = report.get("test_groups")
    if not isinstance(groups, dict):
        errors.append("verification test_groups must be an object")
        return errors
    for name in REQUIRED_TEST_GROUPS:
        group = groups.get(name)
        if not isinstance(group, dict):
            errors.append(f"verification test group is missing: {name}")
            continue
        passed = group.get("passed")
        failures = group.get("failures")
        if type(passed) is not int or passed <= 0:
            errors.append(f"verification {name}.passed must be a positive integer")
        if type(failures) is not int or failures != 0:
            errors.append(f"verification {name}.failures must be zero")
        for optional_count in ("deselected", "skipped"):
            value = group.get(optional_count)
            if value is not None and (type(value) is not int or value < 0):
                errors.append(
                    f"verification {name}.{optional_count} must be a non-negative integer"
                )
    return errors


def _versioned_path(directory: Path, prefix: str, version: str) -> Path:
    return directory / f"{prefix}_{version.replace('.', '_')}.json"


def build_release_evidence(
    repo_root: Path = REPO_ROOT,
    *,
    preflight_report: Path | None = None,
    verification_report: Path | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve()
    loader = ConfigLoader(root / "config")
    loader.load_all()
    product_version = str(loader.get("system", "app.version", ""))
    current_time = now_utc or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)
    max_age_s = float(loader.get("operations", "live_preflight.max_age_s", 300.0))
    max_future_skew_s = float(loader.get(
        "operations", "live_preflight.max_future_skew_s", 30.0,
    ))
    if max_age_s <= 0 or max_future_skew_s < 0:
        raise ValueError("live preflight freshness configuration is invalid")
    baseline = root / "docs" / "baselines"

    text_acceptance = _read_json(baseline / "m10_text_acceptance.json")
    smoke = _read_json(baseline / "m10_mood_hybrid_smoke_final.json")
    cutover = _read_json(baseline / "m10_hybrid_cutover.json")
    delivery = _read_json(baseline / "m10_tts_delivery_acceptance.json")

    verification_path = verification_report or _versioned_path(
        baseline, "release_verification", product_version,
    )
    verification, verification_errors = _load_untrusted_json(
        verification_path, label="verification",
    )
    if verification is not None:
        verification_errors.extend(_validate_verification(verification, product_version))
    verification_valid = verification is not None and not verification_errors

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
        "current_release_verification": verification_valid,
    }
    software_ready = all(gates.values())

    platform_preflight: dict[str, Any] | None = None
    platform_errors: list[str] = []
    platform_valid: bool | None = None
    platform_ready: bool | None = None
    if preflight_report is not None:
        loaded_preflight, platform_errors = _load_untrusted_json(
            preflight_report, label="preflight",
        )
        if loaded_preflight is not None:
            platform_errors.extend(_validate_preflight(
                loaded_preflight,
                product_version,
                now_utc=current_time,
                max_age_s=max_age_s,
                max_future_skew_s=max_future_skew_s,
            ))
        platform_valid = loaded_preflight is not None and not platform_errors
        if platform_valid and loaded_preflight is not None:
            platform_preflight = loaded_preflight
            platform_ready = loaded_preflight["ready"]
        else:
            platform_ready = False

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
        "marker": "mai_release_evidence",
        "product_version": product_version,
        "generated_at_utc": current_time.isoformat(),
        "historical_capability_baseline": "M10.8",
        "sanitized": True,
        "raw_transcript_included": False,
        "software_ready": software_ready,
        "platform_ready": platform_ready,
        "status": status,
        "gates": gates,
        "verification": verification if verification_valid else None,
        "verification_validation": {
            "valid": verification_valid,
            "errors": verification_errors,
        },
        "platform_preflight": platform_preflight,
        "platform_preflight_validation": {
            "valid": platform_valid,
            "errors": platform_errors,
        },
        "rollback": {
            "mood": "disable features.mood_v2_prompt.enabled",
            "dashboard": "disable features.operator_dashboard_v2.enabled or open /legacy",
            "data": "verify with restore_data.py before using --apply",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build current Mai release evidence")
    parser.add_argument("--preflight")
    parser.add_argument("--verification")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = build_release_evidence(
        preflight_report=Path(args.preflight) if args.preflight else None,
        verification_report=Path(args.verification) if args.verification else None,
    )
    output = Path(args.output) if args.output else _versioned_path(
        REPO_ROOT / "docs" / "baselines", "release_evidence", report["product_version"],
    )
    _write_json_atomic(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _release_exit_code(report, preflight_requested=args.preflight is not None)


def _release_exit_code(report: dict[str, Any], *, preflight_requested: bool) -> int:
    if not report.get("software_ready"):
        return 1
    if preflight_requested and report.get("platform_ready") is not True:
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
