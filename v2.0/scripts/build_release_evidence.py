"""Build strict Phase 15 release evidence for the current Git revision."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.closed_loop_canary import ClosedLoopCanaryConfig  # noqa: E402
from services.evaluation.release_gate import (  # noqa: E402
    ReleaseReadinessConfig,
    SourceState,
    inspect_source_state,
)


_COMMON_KEYS = {
    "schema_version", "marker", "sanitized", "source_revision",
    "current_product_version", "target_product_version", "generated_at_utc",
}
_GROUP_KEYS = {
    "command_id", "passed", "failures", "skipped", "deselected",
    "duration_seconds",
}


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_untrusted_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError("artifact must be a JSON object")
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: Any) -> datetime | None:
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


def _finite_number(value: Any, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _valid_label(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str) and bool(value) and value == value.strip()
        and len(value) <= limit
    )


def _validate_common(
    report: Mapping[str, Any],
    *,
    marker: str,
    expected_keys: set[str],
    current_version: str,
    config: ReleaseReadinessConfig,
    source: SourceState,
    now_utc: datetime,
) -> list[str]:
    errors: list[str] = []
    if set(report) != expected_keys:
        errors.append(f"{marker} keys are invalid")
    if report.get("schema_version") != config.schema_version:
        errors.append(f"{marker} schema_version is invalid")
    if report.get("marker") != marker:
        errors.append(f"{marker} marker is invalid")
    if report.get("sanitized") is not True:
        errors.append(f"{marker} must declare sanitized=true")
    if report.get("source_revision") != source.revision:
        errors.append(f"{marker} source_revision does not match HEAD")
    if report.get("current_product_version") != current_version:
        errors.append(f"{marker} current product version is stale")
    if report.get("target_product_version") != config.target_version:
        errors.append(f"{marker} target product version is invalid")
    generated = _parse_utc(report.get("generated_at_utc"))
    if generated is None:
        errors.append(f"{marker} generated_at_utc is invalid")
    elif generated > now_utc + timedelta(seconds=config.max_future_skew_s):
        errors.append(f"{marker} timestamp exceeds future clock skew")
    elif now_utc - generated > timedelta(seconds=config.artifact_max_age_s):
        errors.append(f"{marker} artifact is stale")
    return errors


def _validate_verification(
    report: Mapping[str, Any], **common: Any,
) -> tuple[list[str], bool, bool]:
    config: ReleaseReadinessConfig = common["config"]
    expected = _COMMON_KEYS | {
        "runner_id", "clean_worktree", "test_groups", "correctness",
        "bounded_state_passed", "status",
    }
    errors = _validate_common(
        report, marker="mai_release_verification", expected_keys=expected, **common,
    )
    if report.get("runner_id") != "mai-fixed-verification-v1":
        errors.append("verification runner_id is invalid")
    if report.get("clean_worktree") is not True:
        errors.append("verification must be generated from a clean worktree")
    groups = report.get("test_groups")
    regression_passed = True
    if not isinstance(groups, Mapping) or set(groups) != set(config.required_test_groups):
        errors.append("verification test_groups are invalid")
        regression_passed = False
    else:
        for name in config.required_test_groups:
            group = groups[name]
            if not isinstance(group, Mapping) or set(group) != _GROUP_KEYS:
                errors.append(f"verification group {name} keys are invalid")
                regression_passed = False
                continue
            if group.get("command_id") != name:
                errors.append(f"verification group {name} command_id is invalid")
                regression_passed = False
            for count in ("passed", "failures", "skipped", "deselected"):
                value = group.get(count)
                if type(value) is not int or value < 0:
                    errors.append(f"verification group {name}.{count} is invalid")
                    regression_passed = False
            if type(group.get("passed")) is int and group["passed"] <= 0:
                errors.append(f"verification group {name} passed no checks")
                regression_passed = False
            if group.get("failures") != 0:
                errors.append(f"verification group {name} has failures")
                regression_passed = False
            duration = group.get("duration_seconds")
            if (
                isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration)) or float(duration) < 0
            ):
                errors.append(f"verification group {name} duration is invalid")
                regression_passed = False
    correctness = report.get("correctness")
    correctness_passed = True
    if not isinstance(correctness, Mapping) or set(correctness) != set(
        config.correctness_zero_counters
    ):
        errors.append("verification correctness counters are invalid")
        correctness_passed = False
    else:
        for name in config.correctness_zero_counters:
            if type(correctness[name]) is not int or correctness[name] != 0:
                errors.append(f"correctness counter must be zero: {name}")
                correctness_passed = False
    if report.get("bounded_state_passed") is not True:
        errors.append("bounded state gate did not pass")
        correctness_passed = False
    expected_status = "passed" if regression_passed and correctness_passed else "failed"
    if report.get("status") != expected_status:
        errors.append("verification status disagrees with gate results")
    return errors, correctness_passed, regression_passed


def _validate_preflight(
    report: Mapping[str, Any],
    *,
    current_version: str,
    config: ReleaseReadinessConfig,
    now_utc: datetime,
) -> list[str]:
    errors: list[str] = []
    expected = {
        "schema_version", "marker", "sanitized", "product_version",
        "generated_at_utc", "ready", "platform", "checks",
    }
    if set(report) != expected:
        errors.append("preflight keys are invalid")
    if report.get("schema_version") != 1 or report.get("marker") != "mai_live_preflight":
        errors.append("preflight contract marker is invalid")
    if report.get("sanitized") is not True:
        errors.append("preflight must declare sanitized=true")
    if report.get("product_version") != current_version:
        errors.append("preflight product version is stale")
    generated = _parse_utc(report.get("generated_at_utc"))
    if generated is None:
        errors.append("preflight generated_at_utc is invalid")
    elif generated > now_utc + timedelta(seconds=config.max_future_skew_s):
        errors.append("preflight timestamp exceeds future clock skew")
    elif now_utc - generated > timedelta(seconds=config.artifact_max_age_s):
        errors.append("preflight is stale")
    platform = report.get("platform")
    if platform not in {"youtube", "discord"}:
        errors.append("preflight platform is invalid")
    checks = report.get("checks")
    names: set[str] = set()
    checks_by_name: dict[str, Mapping[str, Any]] = {}
    blocking: list[bool] = []
    if not isinstance(checks, list):
        errors.append("preflight checks must be a list")
    else:
        for item in checks:
            if not isinstance(item, Mapping) or set(item) != {
                "name", "passed", "detail", "blocking",
            }:
                errors.append("preflight check keys are invalid")
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name or name != name.strip() or name in names:
                errors.append("preflight check name is invalid or duplicate")
            else:
                names.add(name)
                checks_by_name[name] = item
            if type(item.get("passed")) is not bool or type(item.get("blocking")) is not bool:
                errors.append("preflight check booleans are invalid")
            if not isinstance(item.get("detail"), str):
                errors.append("preflight check detail is invalid")
            if item.get("blocking") is True and type(item.get("passed")) is bool:
                blocking.append(item["passed"])
    required = set(config.required_preflight_checks)
    required.add("youtube_video" if platform == "youtube" else "discord_token")
    if not required.issubset(names):
        errors.append("preflight required checks are incomplete")
    elif any(
        checks_by_name[name].get("passed") is not True
        or checks_by_name[name].get("blocking") is not True
        or str(checks_by_name[name].get("detail", "")).lower().startswith("deferred:")
        for name in required
    ):
        errors.append("preflight required checks must be blocking and directly verified")
    calculated = bool(blocking) and all(blocking)
    if type(report.get("ready")) is not bool or report.get("ready") != calculated:
        errors.append("preflight ready disagrees with blocking checks")
    if report.get("ready") is not True:
        errors.append("preflight is not ready")
    return errors


def _validate_canary(
    report: Mapping[str, Any], *, canary_config: ClosedLoopCanaryConfig, **common: Any,
) -> list[str]:
    expected = _COMMON_KEYS | {
        "canary_id", "started_at_utc", "action", "pre_snapshot",
        "post_snapshot", "result", "next_decision", "outcome", "reason_code",
        "passed",
    }
    errors = _validate_common(
        report, marker="mai_closed_loop_canary", expected_keys=expected, **common,
    )
    action = report.get("action")
    if not isinstance(action, Mapping) or set(action) != {
        "action_id", "proposal_id", "action_type", "capability_id",
    }:
        errors.append("canary action projection is invalid")
    elif (
        any(not _valid_label(value, canary_config.max_label_chars) for value in action.values())
        or action.get("action_type") not in canary_config.allowed_actions
        or action.get("capability_id") != action.get("action_type")
    ):
        errors.append("canary action identity is invalid")
    for key in ("pre_snapshot", "post_snapshot"):
        value = report.get(key)
        if not isinstance(value, Mapping) or set(value) != {
            "world_snapshot_id", "self_snapshot_id", "capability_snapshot_id",
        } or any(not isinstance(item, str) or not item for item in value.values()):
            errors.append(f"canary {key} is invalid")
    pre = report.get("pre_snapshot")
    post = report.get("post_snapshot")
    if isinstance(pre, Mapping) and isinstance(post, Mapping) and (
        pre.get("world_snapshot_id") == post.get("world_snapshot_id")
    ):
        errors.append("canary world snapshot did not change")
    result = report.get("result")
    if not isinstance(result, Mapping) or set(result) != {
        "status", "verified", "verification_source", "transaction_committed",
        "world_projected", "rollback_outcome",
    }:
        errors.append("canary result projection is invalid")
    elif not (
        result.get("status") == "success"
        and result.get("verified") is True
        and result.get("transaction_committed") is True
        and result.get("world_projected") is True
        and _valid_label(result.get("verification_source"), canary_config.max_label_chars)
        and _valid_label(result.get("rollback_outcome"), canary_config.max_label_chars)
    ):
        errors.append("canary verified transaction gate failed")
    next_decision = report.get("next_decision")
    if not isinstance(next_decision, Mapping) or set(next_decision) != {
        "proposal_id", "action_type", "capability_rechecked",
    } or next_decision.get("capability_rechecked") is not True or not _valid_label(
        next_decision.get("proposal_id"), canary_config.max_label_chars,
    ) or not _valid_label(
        next_decision.get("action_type"), canary_config.max_label_chars,
    ) or (
        isinstance(action, Mapping)
        and next_decision.get("proposal_id") == action.get("proposal_id")
    ):
        errors.append("canary next decision gate failed")
    started = _parse_utc(report.get("started_at_utc"))
    completed = _parse_utc(report.get("generated_at_utc"))
    if started is None or completed is None or started > completed:
        errors.append("canary execution timestamps are invalid")
    if report.get("outcome") != "passed" or report.get("passed") is not True:
        errors.append("canary did not pass")
    return errors


def _validate_human(report: Mapping[str, Any], **common: Any) -> list[str]:
    config: ReleaseReadinessConfig = common["config"]
    expected = _COMMON_KEYS | {
        "review_digest", "reviewed_pairs", "previous", "candidate",
        "previous_build_delta", "operator_approved",
        "status",
    }
    errors = _validate_common(
        report, marker="mai_human_quality_evidence", expected_keys=expected, **common,
    )
    pairs = report.get("reviewed_pairs")
    if type(pairs) is not int or pairs < config.human_min_pairs:
        errors.append("human quality pair count is insufficient")
    digest = report.get("review_digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        errors.append("human quality review digest is invalid")
    previous = report.get("previous")
    candidate = report.get("candidate")
    required = {"weighted_average", "ai_smell_rate", "character_average"}
    if not isinstance(previous, Mapping) or set(previous) != required:
        errors.append("human previous summary is invalid")
    if not isinstance(candidate, Mapping) or set(candidate) != required:
        errors.append("human candidate summary is invalid")
    if isinstance(previous, Mapping) and isinstance(candidate, Mapping):
        previous_values = {name: _finite_number(previous.get(name), minimum=0.0) for name in required}
        candidate_values = {name: _finite_number(candidate.get(name), minimum=0.0) for name in required}
        bounded = (
            ("weighted_average", 5.0), ("ai_smell_rate", 1.0),
            ("character_average", 5.0),
        )
        if any(
            previous_values[name] is None or candidate_values[name] is None
            or previous_values[name] > maximum or candidate_values[name] > maximum
            for name, maximum in bounded
        ):
            errors.append("human quality summary values are invalid")
        else:
            delta = candidate_values["weighted_average"] - previous_values["weighted_average"]
            smell = candidate_values["ai_smell_rate"] - previous_values["ai_smell_rate"]
            character = candidate_values["character_average"] - previous_values["character_average"]
            if delta < config.minimum_previous_build_delta:
                errors.append("human quality aggregate did not improve")
            if smell > config.max_ai_smell_rate_increase:
                errors.append("human quality AI Smell regressed")
            if character < config.minimum_character_delta:
                errors.append("human quality Character regressed")
            claimed = report.get("previous_build_delta")
            claimed_number = _finite_number(claimed)
            if claimed_number is None or abs(claimed_number - delta) > 1e-6:
                errors.append("human quality delta is inconsistent")
    if report.get("operator_approved") is not True or report.get("status") != "passed":
        errors.append("human quality operator approval is missing")
    return errors


def _validate_operations(report: Mapping[str, Any], **common: Any) -> list[str]:
    expected = _COMMON_KEYS | {
        "preflight_sha256", "backup_restore_verified", "deny_by_default_permissions",
        "secrets_scan_passed", "pii_scan_passed", "emergency_stop_passed",
        "graceful_shutdown_passed", "rollback_rehearsal_passed", "live_canaries",
        "evidence_refs", "status",
    }
    errors = _validate_common(
        report, marker="mai_operations_rehearsal", expected_keys=expected, **common,
    )
    for name in (
        "backup_restore_verified", "deny_by_default_permissions", "secrets_scan_passed",
        "pii_scan_passed", "emergency_stop_passed", "graceful_shutdown_passed",
        "rollback_rehearsal_passed",
    ):
        if report.get(name) is not True:
            errors.append(f"operations gate failed: {name}")
    canaries = report.get("live_canaries")
    if not isinstance(canaries, Mapping) or set(canaries) != {
        "platform", "audio", "avatar", "obs", "memory",
    } or any(value is not True for value in canaries.values()):
        errors.append("operations live canary set is incomplete")
    digest = report.get("preflight_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        errors.append("operations preflight digest is invalid")
    refs = report.get("evidence_refs")
    if not isinstance(refs, list) or not refs or any(
        not _valid_label(item, common["config"].max_label_chars) for item in refs
    ):
        errors.append("operations evidence_refs are invalid")
    if report.get("status") != "passed":
        errors.append("operations rehearsal status is not passed")
    return errors


def _load_artifact(
    path: Path | None,
    *,
    label: str,
    validator: Callable[[Mapping[str, Any]], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any], Any]:
    if path is None:
        return None, {
            "present": False, "valid": False, "sha256": None,
            "file": None, "errors": [f"missing artifact: {label}"],
        }, None
    try:
        report = _read_untrusted_json(path)
        digest = _sha256(path)
        validated = validator(report)
        errors = validated[0] if isinstance(validated, tuple) else validated
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, {
            "present": True, "valid": False, "sha256": None,
            "file": path.name, "errors": [f"{label} load failed: {type(exc).__name__}"],
        }, None
    return report, {
        "present": True, "valid": not errors, "sha256": digest,
        "file": path.name, "errors": list(errors),
    }, validated


def build_release_evidence(
    repo_root: Path = REPO_ROOT,
    *,
    preflight_report: Path | None = None,
    verification_report: Path | None = None,
    canary_report: Path | None = None,
    human_report: Path | None = None,
    operations_report: Path | None = None,
    now_utc: datetime | None = None,
    source_state: SourceState | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve()
    loader = ConfigLoader(root / "config")
    loader.load_all()
    config = ReleaseReadinessConfig.from_loader(loader)
    canary_config = ClosedLoopCanaryConfig.from_loader(loader)
    current_version = str(loader.get("system", "app.version", ""))
    current_time = now_utc or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)
    source = source_state or inspect_source_state(root)
    common = {
        "current_version": current_version,
        "config": config,
        "source": source,
        "now_utc": current_time,
    }

    verification, verification_meta, verification_result = _load_artifact(
        verification_report, label="verification",
        validator=lambda value: _validate_verification(value, **common),
    )
    _, preflight_meta, _ = _load_artifact(
        preflight_report, label="preflight",
        validator=lambda value: _validate_preflight(
            value, current_version=current_version, config=config, now_utc=current_time,
        ),
    )
    _, canary_meta, _ = _load_artifact(
        canary_report, label="closed_loop_canary",
        validator=lambda value: _validate_canary(
            value, canary_config=canary_config, **common,
        ),
    )
    _, human_meta, _ = _load_artifact(
        human_report, label="human_quality",
        validator=lambda value: _validate_human(value, **common),
    )
    operations, operations_meta, _ = _load_artifact(
        operations_report, label="operations_rehearsal",
        validator=lambda value: _validate_operations(value, **common),
    )
    if operations is not None and preflight_meta["sha256"] is not None and (
        operations.get("preflight_sha256") != preflight_meta["sha256"]
    ):
        operations_meta["valid"] = False
        operations_meta["errors"].append("operations preflight digest mismatch")

    correctness = False
    regression = False
    if verification is not None and isinstance(verification_result, tuple):
        correctness = not verification_meta["errors"] and verification_result[1]
        regression = not verification_meta["errors"] and verification_result[2]
    gates = {
        "correctness": correctness,
        "closed_loop_agency": canary_meta["valid"],
        "v1_regression": regression,
        "human_like_quality": human_meta["valid"],
        "operations_security": preflight_meta["valid"] and operations_meta["valid"],
    }
    artifacts = {
        "verification": verification_meta,
        "preflight": preflight_meta,
        "closed_loop_canary": canary_meta,
        "human_quality": human_meta,
        "operations_rehearsal": operations_meta,
    }
    if len(artifacts) > config.max_artifacts:
        raise ValueError("release artifact inventory exceeds configured maximum")
    all_gates = all(gates.values()) and source.clean
    eligible = all_gates and current_version != config.target_version
    release_ready = all_gates and current_version == config.target_version
    reasons = []
    if not source.clean:
        reasons.append("worktree_not_clean")
    for name, passed in gates.items():
        if not passed:
            reasons.append(f"gate_failed:{name}")
    if release_ready:
        status = "release_ready"
    elif eligible:
        status = "eligible_for_version_bump"
    else:
        status = "not_ready"
    return {
        "schema_version": config.schema_version,
        "marker": "mai_release_evidence",
        "sanitized": True,
        "current_product_version": current_version,
        "target_product_version": config.target_version,
        "source_revision": source.revision,
        "clean_worktree": source.clean,
        "generated_at_utc": current_time.isoformat(),
        "status": status,
        "configuration_ready": preflight_meta["valid"],
        "canary_passed": canary_meta["valid"],
        "eligible_for_version_bump": eligible,
        "release_ready": release_ready,
        "gates": gates,
        "artifacts": artifacts,
        "reasons": reasons,
    }


def _versioned_path(directory: Path, target_version: str) -> Path:
    return directory / f"release_evidence_{target_version.replace('.', '_')}.json"


def _default_output_path(repo_root: Path, target_version: str) -> Path:
    return _versioned_path(repo_root / "logs" / "operations", target_version)


def _release_exit_code(report: Mapping[str, Any], *, require_release_ready: bool) -> int:
    if require_release_ready:
        return 0 if report.get("release_ready") is True else 1
    return 0 if report.get("eligible_for_version_bump") is True else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--canary", type=Path)
    parser.add_argument("--human", type=Path)
    parser.add_argument("--operations", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args(argv)
    report = build_release_evidence(
        preflight_report=args.preflight,
        verification_report=args.verification,
        canary_report=args.canary,
        human_report=args.human,
        operations_report=args.operations,
    )
    output = args.output or _default_output_path(
        REPO_ROOT, report["target_product_version"],
    )
    _write_json_atomic(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _release_exit_code(report, require_release_ready=args.require_release_ready)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
