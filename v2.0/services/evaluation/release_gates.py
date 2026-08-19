"""Fail-closed evidence evaluation for the Mai 2.0.0 product release gates."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.evaluation import ProductReleaseGateService

_CORRECTNESS = (
    "unauthorized_executed_actions",
    "unavailable_capability_executed_actions",
    "duplicate_committed_actions",
    "false_committed_world_states",
    "transaction_inconsistencies",
    "unbounded_state_or_queue_growth",
)
_CLOSED_LOOP = (
    "world",
    "capability_availability",
    "decision",
    "execute",
    "verify",
    "commit",
    "world_changed",
    "capability_changed",
    "next_decision_sees_change",
)
_V1_REGRESSION = (
    "persona",
    "conversation_continuity",
    "relationship",
    "delivery_reliability",
    "safety",
    "dedup_repetition",
    "self_talk_transaction",
    "thread_commit_semantics",
    "graceful_shutdown",
    "recovery",
)
_OPERATIONS = (
    "preflight_ready",
    "release_evidence_sanitized",
    "backup_restore_verified",
    "permissions_deny_by_default",
    "no_secret_or_pii_leak",
    "emergency_stop_passed",
    "graceful_shutdown_passed",
    "rollback_rehearsed",
)


@dataclass(frozen=True)
class ProductReleaseGatePolicy:
    target_product_version: str
    max_trace_refs: int
    min_completed_reviews: int
    min_aggregate_improvement: float
    max_ai_smell_increase: float
    max_blind_ab_regressions: int
    correctness_maximums: Mapping[str, int]

    @classmethod
    def from_loader(cls, loader: Any) -> "ProductReleaseGatePolicy":
        raw = loader.get("evaluation", "fine_tune_gate.release_gates", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("release_gates config must be a mapping")
        limits = raw.get("correctness_maximums", {})
        if not isinstance(limits, Mapping):
            raise ValueError("release_gates.correctness_maximums must be a mapping")
        value = cls(
            target_product_version=_text(raw.get("target_product_version"), 32),
            max_trace_refs=int(raw.get("max_trace_refs", 0)),
            min_completed_reviews=int(raw.get("min_completed_reviews", 0)),
            min_aggregate_improvement=float(raw.get("min_aggregate_improvement", -1)),
            max_ai_smell_increase=float(raw.get("max_ai_smell_increase", -1)),
            max_blind_ab_regressions=int(raw.get("max_blind_ab_regressions", -1)),
            correctness_maximums={name: _integer(limits.get(name), name) for name in _CORRECTNESS},
        )
        if not value.target_product_version or value.max_trace_refs <= 0 or value.min_completed_reviews <= 0:
            raise ValueError("release gate bounds must be positive")
        if value.min_aggregate_improvement < 0 or value.max_ai_smell_increase < 0:
            raise ValueError("release gate quality thresholds must not be negative")
        if value.max_blind_ab_regressions < 0 or any(limit < 0 for limit in value.correctness_maximums.values()):
            raise ValueError("release gate maximums must not be negative")
        return value


class ProductReleaseGateEvaluator(ProductReleaseGateService):
    """Evaluate external aggregate evidence without ever authorizing a deployment."""

    service_id = "product_release_gates"

    def __init__(self, policy: ProductReleaseGatePolicy, *, metrics: Any = None, enabled: bool = False) -> None:
        self._policy = policy
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None, enabled: bool = False) -> "ProductReleaseGateEvaluator":
        return cls(ProductReleaseGatePolicy.from_loader(loader), metrics=metrics, enabled=enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, enabled=self.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def evaluate(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("product release gate evaluation feature disabled")
        try:
            report = self._evaluate(evidence)
        except (TypeError, ValueError) as exc:
            self._record("invalid")
            return self._invalid_report(str(exc))
        self._record("eligible" if report["release_eligible"] else "blocked")
        return report

    def get_metrics(self) -> dict[str, int]:
        return {f"release_gate_{name}_total": value for name, value in sorted(self._counts.items())}

    def _evaluate(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(evidence, Mapping):
            raise ValueError("release evidence must be an object")
        _exact_keys(evidence, {
            "schema_version", "marker", "sanitized", "target_product_version", "correctness",
            "closed_loop", "v1_regression", "human_like", "operations",
        }, "release evidence")
        if evidence.get("schema_version") != 1 or evidence.get("marker") != "mai_product_release_evidence":
            raise ValueError("release evidence schema or marker is invalid")
        if evidence.get("sanitized") is not True:
            raise ValueError("release evidence must declare sanitized=true")
        if evidence.get("target_product_version") != self._policy.target_product_version:
            raise ValueError("release evidence target product version does not match policy")

        gates = {
            "A_correctness": self._correctness(evidence.get("correctness")),
            "B_closed_loop": self._closed_loop(evidence.get("closed_loop")),
            "C_v1_regression": self._regression(evidence.get("v1_regression")),
            "D_human_like_quality": self._quality(evidence.get("human_like")),
            "E_operations_security": self._operations(evidence.get("operations")),
        }
        failed = [name for name, gate in gates.items() if gate["status"] != "passed"]
        eligible = not failed
        return {
            "schema_version": 1,
            "marker": "mai_product_release_gate_report",
            "sanitized": True,
            "target_product_version": self._policy.target_product_version,
            "release_eligible": eligible,
            "legacy_retirement_allowed": eligible,
            "release_authorized": False,
            "gates": gates,
            "failed_gates": failed,
            "required_owner_actions": [] if eligible else ["collect_or_correct_real_evidence"],
            "release_mutation_required": eligible,
        }

    def _correctness(self, value: Any) -> dict[str, Any]:
        data = _mapping(value, "correctness")
        _exact_keys(data, set(_CORRECTNESS), "correctness")
        checks: dict[str, bool] = {}
        observed: dict[str, int] = {}
        for name in _CORRECTNESS:
            observed[name] = _integer(data[name], name)
            checks[name] = observed[name] <= self._policy.correctness_maximums[name]
        return _gate(checks, observed)

    def _closed_loop(self, value: Any) -> dict[str, Any]:
        data = _mapping(value, "closed_loop")
        _exact_keys(data, {"stages", "trace_refs"}, "closed_loop")
        stages = data.get("stages")
        refs = data.get("trace_refs")
        if not isinstance(stages, (list, tuple)) or tuple(stages) != _CLOSED_LOOP:
            raise ValueError("closed_loop stages must match the required ordered path")
        if not isinstance(refs, (list, tuple)) or not refs or len(refs) > self._policy.max_trace_refs:
            raise ValueError("closed_loop trace refs are invalid")
        clean_refs = tuple(_text(item, 120) for item in refs)
        if not all(clean_refs) or len(clean_refs) != len(set(clean_refs)):
            raise ValueError("closed_loop trace refs must be unique non-empty references")
        return _gate({"ordered_path": True, "trace_refs_present": True}, {"trace_ref_count": len(clean_refs)})

    def _regression(self, value: Any) -> dict[str, Any]:
        data = _mapping(value, "v1_regression")
        _exact_keys(data, set(_V1_REGRESSION), "v1_regression")
        checks = {name: _boolean(data[name], name) for name in _V1_REGRESSION}
        return _gate(checks, {})

    def _quality(self, value: Any) -> dict[str, Any]:
        data = _mapping(value, "human_like")
        fields = {
            "completed_reviews", "baseline_aggregate", "candidate_aggregate", "baseline_ai_smell_ratio",
            "candidate_ai_smell_ratio", "core_metrics_preserved", "character_preserved", "blind_ab_regressions",
        }
        _exact_keys(data, fields, "human_like")
        reviews = _integer(data["completed_reviews"], "completed_reviews")
        baseline = _score(data["baseline_aggregate"], "baseline_aggregate")
        candidate = _score(data["candidate_aggregate"], "candidate_aggregate")
        baseline_smell = _ratio(data["baseline_ai_smell_ratio"], "baseline_ai_smell_ratio")
        candidate_smell = _ratio(data["candidate_ai_smell_ratio"], "candidate_ai_smell_ratio")
        regressions = _integer(data["blind_ab_regressions"], "blind_ab_regressions")
        improvement = candidate - baseline
        smell_delta = candidate_smell - baseline_smell
        checks = {
            "completed_reviews": reviews >= self._policy.min_completed_reviews,
            "aggregate_improved": improvement >= self._policy.min_aggregate_improvement,
            "ai_smell_not_regressed": smell_delta <= self._policy.max_ai_smell_increase,
            "core_metrics_preserved": _boolean(data["core_metrics_preserved"], "core_metrics_preserved"),
            "character_preserved": _boolean(data["character_preserved"], "character_preserved"),
            "blind_ab_not_regressed": regressions <= self._policy.max_blind_ab_regressions,
        }
        return _gate(checks, {"aggregate_improvement": round(improvement, 3), "ai_smell_delta": round(smell_delta, 3), "completed_reviews": reviews, "blind_ab_regressions": regressions})

    def _operations(self, value: Any) -> dict[str, Any]:
        data = _mapping(value, "operations")
        _exact_keys(data, {"preflight_product_version", *_OPERATIONS}, "operations")
        checks = {name: _boolean(data[name], name) for name in _OPERATIONS}
        checks["preflight_product_version"] = data["preflight_product_version"] == self._policy.target_product_version
        return _gate(checks, {})

    def _invalid_report(self, error: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "marker": "mai_product_release_gate_report",
            "sanitized": True,
            "target_product_version": self._policy.target_product_version,
            "release_eligible": False,
            "legacy_retirement_allowed": False,
            "release_authorized": False,
            "gates": {},
            "failed_gates": ["invalid_evidence"],
            "required_owner_actions": ["collect_or_correct_real_evidence"],
            "release_mutation_required": False,
            "validation_error": _text(error, 240),
        }

    def _record(self, outcome: str) -> None:
        self._counts["evaluated"] = self._counts.get("evaluated", 0) + 1
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        callback = getattr(self._metrics, "record_release_gate", None)
        if callable(callback):
            callback(outcome)


def _gate(checks: Mapping[str, bool], values: Mapping[str, Any]) -> dict[str, Any]:
    failed = [name for name, passed in checks.items() if not passed]
    return {"status": "passed" if not failed else "blocked", "checks": dict(checks), "failed_checks": failed, "values": dict(values)}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields are invalid")


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _score(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= float(value) <= 5:
        raise ValueError(f"{label} must be within 1..5")
    return float(value)


def _ratio(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise ValueError(f"{label} must be within 0..1")
    return float(value)


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]