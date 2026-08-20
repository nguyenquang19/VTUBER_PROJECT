"""Aggregate strict, hashed Phase 15 operations rehearsal evidence."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from scripts.build_release_evidence import (  # noqa: E402
    _parse_utc,
    _read_untrusted_json,
    _sha256,
    _write_json_atomic,
)
from services.evaluation.release_gate import (  # noqa: E402
    ReleaseReadinessConfig,
    SourceState,
    inspect_source_state,
)


CHECKS = {
    "backup_restore": "backup_restore_verified",
    "deny_by_default_permissions": "deny_by_default_permissions",
    "secrets_scan": "secrets_scan_passed",
    "pii_scan": "pii_scan_passed",
    "emergency_stop": "emergency_stop_passed",
    "graceful_shutdown": "graceful_shutdown_passed",
    "rollback_rehearsal": "rollback_rehearsal_passed",
}
LIVE_CANARIES = {
    "live_canary_platform": "platform",
    "live_canary_audio": "audio",
    "live_canary_avatar": "avatar",
    "live_canary_obs": "obs",
    "live_canary_memory": "memory",
}
_EVIDENCE_KEYS = {
    "schema_version", "marker", "sanitized", "check_id",
    "generated_at_utc", "status", "evidence_refs",
}


def _validate_check(
    path: Path, check_id: str, *, config: ReleaseReadinessConfig, now_utc: datetime,
) -> str:
    value = _read_untrusted_json(path)
    if set(value) != _EVIDENCE_KEYS:
        raise ValueError(f"operation check keys are invalid: {check_id}")
    if (
        value.get("schema_version") != 1
        or value.get("marker") != "mai_operation_check"
        or value.get("sanitized") is not True
        or value.get("check_id") != check_id
        or value.get("status") != "passed"
    ):
        raise ValueError(f"operation check contract failed: {check_id}")
    generated = _parse_utc(value.get("generated_at_utc"))
    if generated is None:
        raise ValueError(f"operation check timestamp is invalid: {check_id}")
    if generated > now_utc + timedelta(seconds=config.max_future_skew_s):
        raise ValueError(f"operation check timestamp is in the future: {check_id}")
    if now_utc - generated > timedelta(seconds=config.artifact_max_age_s):
        raise ValueError(f"operation check is stale: {check_id}")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not refs or any(
        not isinstance(ref, str) or not ref or ref != ref.strip()
        or len(ref) > config.max_label_chars for ref in refs
    ):
        raise ValueError(f"operation check evidence refs are invalid: {check_id}")
    if len(path.name) > config.max_label_chars:
        raise ValueError(f"operation check file name is too long: {check_id}")
    reference = f"{check_id}:sha256:{_sha256(path)}"
    if len(reference) > config.max_label_chars:
        raise ValueError(f"operation check digest reference is too long: {check_id}")
    return reference


def build_operations_rehearsal(
    preflight_report: Path,
    evidence_reports: Mapping[str, Path],
    *,
    repo_root: Path = REPO_ROOT,
    source_state: SourceState | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve()
    loader = ConfigLoader(root / "config")
    loader.load_all()
    config = ReleaseReadinessConfig.from_loader(loader)
    source = source_state or inspect_source_state(root)
    if not source.clean:
        raise RuntimeError("operations rehearsal evidence requires a clean worktree")
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("operations rehearsal clock must be timezone-aware")
    now = now.astimezone(timezone.utc)
    expected = set(CHECKS) | set(LIVE_CANARIES)
    if set(evidence_reports) != expected:
        raise ValueError("operations rehearsal evidence set is incomplete or unknown")
    preflight = _read_untrusted_json(preflight_report)
    if (
        preflight.get("marker") != "mai_live_preflight"
        or preflight.get("sanitized") is not True
        or preflight.get("ready") is not True
    ):
        raise ValueError("operations rehearsal requires a passed live preflight")
    references = [
        _validate_check(evidence_reports[check_id], check_id, config=config, now_utc=now)
        for check_id in sorted(expected)
    ]
    output: dict[str, Any] = {
        "schema_version": config.schema_version,
        "marker": "mai_operations_rehearsal",
        "sanitized": True,
        "source_revision": source.revision,
        "current_product_version": str(loader.get("system", "app.version", "")),
        "target_product_version": config.target_version,
        "generated_at_utc": now.isoformat(),
        "preflight_sha256": _sha256(preflight_report),
        **{field: True for field in CHECKS.values()},
        "live_canaries": {name: True for name in LIVE_CANARIES.values()},
        "evidence_refs": references,
        "status": "passed",
    }
    return output


def _evidence_argument(value: str) -> tuple[str, Path]:
    check_id, separator, raw_path = value.partition("=")
    if not separator or not check_id or not raw_path:
        raise argparse.ArgumentTypeError("evidence must use CHECK_ID=PATH")
    return check_id, Path(raw_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--evidence", type=_evidence_argument, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = dict(args.evidence)
    if len(evidence) != len(args.evidence):
        raise ValueError("operations rehearsal evidence ids must be unique")
    report = build_operations_rehearsal(args.preflight, evidence)
    _write_json_atomic(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
