"""Phase 14 blind HLC scoring and replay-safe trajectory records."""
from __future__ import annotations

import hashlib
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.evaluation import HumanLikeCalibrationService, TrajectoryRecorderService
from services.data.sanitize import mask_pii

_DIMENSIONS = ("language", "presence", "context", "character", "timing", "spontaneity")


@dataclass(frozen=True)
class HumanLikeCalibrationConfig:
    max_artifact_rows: int
    max_candidate_chars: int
    max_note_chars: int
    max_smell_tags: int
    max_trajectory_records: int
    max_reason_codes: int
    weights: Mapping[str, float]

    @classmethod
    def from_loader(cls, loader: Any) -> "HumanLikeCalibrationConfig":
        raw = loader.get("evaluation", "fine_tune_gate.human_like_calibration", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("human_like_calibration config must be a mapping")
        weights = raw.get("weights", {})
        if not isinstance(weights, Mapping):
            raise ValueError("human_like_calibration.weights must be a mapping")
        value = cls(
            max_artifact_rows=int(raw.get("max_artifact_rows", 0)),
            max_candidate_chars=int(raw.get("max_candidate_chars", 0)),
            max_note_chars=int(raw.get("max_note_chars", 0)),
            max_smell_tags=int(raw.get("max_smell_tags", 0)),
            max_trajectory_records=int(raw.get("max_trajectory_records", 0)),
            max_reason_codes=int(raw.get("max_reason_codes", 0)),
            weights={key: float(weights.get(key, 0.0)) for key in _DIMENSIONS},
        )
        if min(value.max_artifact_rows, value.max_candidate_chars, value.max_note_chars, value.max_smell_tags, value.max_trajectory_records, value.max_reason_codes) <= 0:
            raise ValueError("human-like calibration bounds must be positive")
        if any(weight < 0 for weight in value.weights.values()) or round(sum(value.weights.values()), 8) != 1.0:
            raise ValueError("human-like calibration weights must sum to 1")
        return value


class HumanLikeCalibration(HumanLikeCalibrationService):
    service_id = "human_like_calibration"

    def __init__(self, config: HumanLikeCalibrationConfig, *, metrics: Any = None, enabled: bool = False) -> None:
        self._config = config
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._internals: dict[str, dict[str, object]] = {}
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None, enabled: bool = False) -> "HumanLikeCalibration":
        return cls(HumanLikeCalibrationConfig.from_loader(loader), metrics=metrics, enabled=enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, enabled=self.enabled, artifacts=len(self._internals))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def build(self, comparisons: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("human-like calibration feature disabled")
        if not comparisons or len(comparisons) > self._config.max_artifact_rows:
            raise ValueError("calibration comparison count is invalid")
        artifact_id = "hlc_" + uuid.uuid4().hex
        rows: list[dict[str, object]] = []
        hidden: dict[str, object] = {}
        for index, raw in enumerate(comparisons):
            ref = _label(raw.get("turn_ref") or f"turn-{index + 1}", 120)
            candidate = _clean_text(raw.get("candidate_a", raw.get("candidate")), self._config.max_candidate_chars)
            alternative = _clean_text(raw.get("candidate_b"), self._config.max_candidate_chars)
            if not candidate:
                raise ValueError("calibration candidate is required")
            blind_ref = "review:" + hashlib.sha256(f"{artifact_id}:{ref}".encode()).hexdigest()[:16]
            row: dict[str, object] = {"review_ref": blind_ref, "review": _empty_review()}
            if alternative:
                swapped = bool(hashlib.sha256(ref.encode()).digest()[0] & 1)
                row.update({
                    "candidate_a": alternative if swapped else candidate,
                    "candidate_b": candidate if swapped else alternative,
                    "paired_blind_review": True,
                })
            else:
                row["candidate"] = candidate
            rows.append(row)
            hidden[blind_ref] = {
                "build_label": _label(raw.get("build_label"), 120),
                "director_score": _number(raw.get("director_score")),
                "trajectory_ref": _label(raw.get("trajectory_ref"), 120),
            }
        self._internals[artifact_id] = hidden
        artifact = {
            "schema_version": 1, "marker": "human_like_calibration", "artifact_id": artifact_id,
            "sanitized": True, "blind": True, "build_identity_included": False,
            "director_score_included": False, "prompt_included": False, "memory_internals_included": False,
            "status": "pending_human_review", "rows": rows,
            "previous_build_score": None, "human_review": {"reviewer_role": "", "complete": False},
        }
        self._record("built")
        return artifact

    def finalize(self, artifact: dict[str, Any]) -> dict[str, Any]:
        if artifact.get("marker") != "human_like_calibration" or artifact.get("sanitized") is not True:
            raise ValueError("invalid HLC artifact")
        reviewer = _label((artifact.get("human_review") or {}).get("reviewer_role"), 80)
        if not reviewer:
            raise ValueError("human reviewer role is required")
        rows = [dict(item) for item in artifact.get("rows") or []]
        if not rows:
            raise ValueError("HLC artifact has no rows")
        summaries: list[dict[str, object]] = []
        for row in rows:
            review = dict(row.get("review") or {})
            dimensions = {name: _score(review.get(name), name) for name in _DIMENSIONS}
            smell = review.get("ai_smell")
            if not isinstance(smell, bool):
                raise ValueError("ai_smell must be boolean")
            tags = _tags(review.get("ai_smell_tags"), self._config.max_smell_tags)
            if smell and not tags:
                raise ValueError("AI smell requires at least one tag")
            if not smell and tags:
                raise ValueError("AI smell tags require ai_smell=true")
            liveness = _score(review.get("liveness"), "liveness")
            coherence = _score(review.get("action_coherence"), "action_coherence")
            note = _clean_text(review.get("note"), self._config.max_note_chars)
            if not note:
                raise ValueError("HLC review note is required")
            aggregate = sum(dimensions[name] * self._config.weights[name] for name in _DIMENSIONS)
            weakest = min(_DIMENSIONS, key=lambda name: (dimensions[name], name))
            row["review"] = {**dimensions, "ai_smell": smell, "ai_smell_tags": tags, "liveness": liveness, "action_coherence": coherence, "preferred": str(review.get("preferred") or "").upper() if row.get("paired_blind_review") else None, "note": note}
            summaries.append({"aggregate": aggregate, "weakest_dimension": weakest, "ai_smell": smell, "liveness": liveness, "action_coherence": coherence})
        aggregate = sum(float(item["aggregate"]) for item in summaries) / len(summaries)
        previous = artifact.get("previous_build_score")
        if previous is not None and (isinstance(previous, bool) or not isinstance(previous, (int, float)) or not 1 <= float(previous) <= 5):
            raise ValueError("previous_build_score must be within 1..5")
        weakest = min(
            _DIMENSIONS,
            key=lambda name: (sum(int(row["review"][name]) for row in rows) / len(rows), name),
        )
        output = dict(artifact)
        output["rows"] = rows
        output["status"] = "finalized"
        output["human_review"] = {"reviewer_role": reviewer, "complete": True, "aggregate": round(aggregate, 3), "previous_build_delta": None if previous is None else round(aggregate - float(previous), 3), "weakest_dimension": weakest, "ai_smell_ratio": round(sum(bool(item["ai_smell"]) for item in summaries) / len(summaries), 3), "liveness_average": round(sum(int(item["liveness"]) for item in summaries) / len(summaries), 3), "action_coherence_average": round(sum(int(item["action_coherence"]) for item in summaries) / len(summaries), 3)}
        self._record("finalized")
        return output

    def reveal_internals(self, artifact: dict[str, Any]) -> dict[str, Any]:
        if artifact.get("status") != "finalized" or not (artifact.get("human_review") or {}).get("complete"):
            raise ValueError("internals are available only after finalized human score")
        artifact_id = str(artifact.get("artifact_id") or "")
        values = self._internals.get(artifact_id)
        if values is None:
            raise KeyError("HLC artifact internals not available")
        self._record("revealed")
        return {"artifact_id": artifact_id, "rows": dict(values)}

    def get_metrics(self) -> dict[str, object]:
        return {f"human_like_calibration_{key}_total": value for key, value in sorted(self._counts.items())}

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        callback = getattr(self._metrics, "record_human_like_calibration", None)
        if callable(callback):
            callback(outcome)


class TrajectoryRecorder(TrajectoryRecorderService):
    service_id = "trajectory_recorder"

    def __init__(self, *, max_recent: int, max_reason_codes: int, metrics: Any = None, enabled: bool = False, clock: Callable[[], float] | None = None) -> None:
        if min(max_recent, max_reason_codes) <= 0:
            raise ValueError("trajectory limits must be positive")
        self._max_reason_codes = int(max_reason_codes)
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._clock = clock or time.time
        self._running = False
        self._items: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._max_recent = int(max_recent)
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None, enabled: bool = False) -> "TrajectoryRecorder":
        config = HumanLikeCalibrationConfig.from_loader(loader)
        return cls(max_recent=config.max_trajectory_records, max_reason_codes=config.max_reason_codes, metrics=metrics, enabled=enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, enabled=self.enabled, retained=len(self._items))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def record_decision(self, *, start_snapshots: Mapping[str, Any], candidate_summary: Mapping[str, Any], selected_action: str, reason_codes: tuple[str, ...] = (), action_request: ActionRequest | None = None) -> str | None:
        if not self.enabled:
            return None
        if _contains_forbidden({"start_snapshots": start_snapshots, "candidate_summary": candidate_summary, "reason_codes": reason_codes}):
            self._record("rejected")
            return None
        trajectory_id = "traj_" + uuid.uuid4().hex
        record: dict[str, object] = {"trajectory_id": trajectory_id, "created_at": self._clock(), "start": _snapshots(start_snapshots), "decision": {"candidate_summary": _summary(candidate_summary), "selected_action": _label(selected_action, 120), "reason_codes": _codes(reason_codes, self._max_reason_codes)}, "action_request": _action_request(action_request), "result": None, "next": None}
        self._items[trajectory_id] = record
        self._trim()
        self._record("recorded")
        return trajectory_id

    def update_result(self, trajectory_id: str, *, result: ActionResult | None, verification_outcome: str, next_snapshots: Mapping[str, Any]) -> bool:
        record = self._items.get(str(trajectory_id))
        if not self.enabled or record is None or _contains_forbidden({"next_snapshots": next_snapshots, "verification_outcome": verification_outcome}):
            self._record("rejected")
            return False
        record["result"] = _action_result(result, verification_outcome)
        record["next"] = _snapshots(next_snapshots)
        self._items.move_to_end(str(trajectory_id))
        self._record("updated")
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "counts": dict(sorted(self._counts.items())), "recent": list(reversed(list(self._items.values())[-20:]))}

    def get_metrics(self) -> dict[str, object]:
        return {f"trajectory_{key}_total": value for key, value in sorted(self._counts.items())}

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        callback = getattr(self._metrics, "record_trajectory", None)
        if callable(callback):
            callback(outcome)

    def _trim(self) -> None:
        while len(self._items) > self._max_recent:
            self._items.popitem(last=False)


def _empty_review() -> dict[str, object]:
    return {**{name: None for name in _DIMENSIONS}, "ai_smell": None, "ai_smell_tags": [], "liveness": None, "action_coherence": None, "preferred": None, "note": ""}


def _clean_text(value: Any, limit: int) -> str:
    return " ".join((mask_pii(str(value or "")) or "").split())[:limit]


def _label(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _score(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError(f"{name} score must be an integer within 1..5")
    return value


def _tags(value: Any, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError("AI smell tags are invalid")
    tags = tuple(_label(item, 60) for item in value if _label(item, 60))
    if len(tags) != len(set(tags)):
        raise ValueError("AI smell tags must be unique")
    return tags


def _contains_forbidden(value: Any) -> bool:
    forbidden = ("prompt", "memory", "context", "chain_of_thought", "raw_text", "transcript")
    if isinstance(value, Mapping):
        return any(str(key).casefold() in forbidden or _contains_forbidden(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_contains_forbidden(item) for item in value)
    return False


def _snapshots(value: Mapping[str, Any]) -> dict[str, str]:
    allowed = ("world_snapshot_id", "self_snapshot_id", "capability_snapshot_id")
    return {key: _label(value.get(key), 120) for key in allowed if _label(value.get(key), 120)}


def _summary(value: Mapping[str, Any]) -> dict[str, object]:
    allowed = ("candidate_count", "pool_size", "candidate_kinds", "top_score")
    return {key: value[key] for key in allowed if key in value}


def _codes(value: tuple[str, ...], limit: int) -> tuple[str, ...]:
    return tuple(_label(item, 120) for item in value if _label(item, 120))[:limit]


def _action_request(value: ActionRequest | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"action_id": value.action_id, "capability_id": value.capability_id, "action_type": value.action_type, "intention_id": value.intention_id, "evidence_refs": tuple(value.evidence_refs), "transaction_policy": value.transaction_policy}


def _action_result(value: ActionResult | None, verification_outcome: str) -> dict[str, object]:
    if value is None:
        return {"verification_outcome": _label(verification_outcome, 120)}
    return {"action_id": value.action_id, "status": value.status.value, "verified": value.verified, "error_code": value.error_code, "verification_outcome": _label(verification_outcome, 120)}