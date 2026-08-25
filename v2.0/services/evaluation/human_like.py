"""MAI-HLC scoring and persist-before-reveal blind review workflow."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from interfaces.base import HealthStatus
from interfaces.human_like import HumanLikeCalibrationService
from services.data.sanitize import mask_pii


DIMENSIONS = (
    "language", "presence", "context", "character", "timing", "spontaneity",
)
_CONFIG_KEYS = {
    "schema_version", "seed", "min_pairs", "max_pairs", "max_output_chars",
    "max_context_chars", "max_note_chars", "max_ref_chars", "dimensions",
    "ai_smell_tags",
}


@dataclass(frozen=True)
class HumanLikeConfig:
    schema_version: int
    seed: int
    min_pairs: int
    max_pairs: int
    max_output_chars: int
    max_context_chars: int
    max_note_chars: int
    max_ref_chars: int
    dimensions: tuple[tuple[str, float], ...]
    ai_smell_tags: tuple[str, ...]

    @classmethod
    def from_loader(cls, loader: Any) -> "HumanLikeConfig":
        raw = loader.get("evaluation", "evaluation.human_like", None)
        if not isinstance(raw, dict) or set(raw) != _CONFIG_KEYS:
            raise ValueError("evaluation.human_like keys are invalid")
        dimensions = raw["dimensions"]
        if not isinstance(dimensions, dict) or tuple(dimensions) != DIMENSIONS:
            raise ValueError("human-like dimensions must use canonical order")
        weights = tuple(
            (name, _finite_weight(dimensions[name], name)) for name in DIMENSIONS
        )
        if not math.isclose(sum(weight for _, weight in weights), 1.0, abs_tol=1e-9):
            raise ValueError("human-like dimension weights must sum to 1")
        tags_raw = raw["ai_smell_tags"]
        if not isinstance(tags_raw, list) or not tags_raw:
            raise ValueError("ai_smell_tags must be a non-empty list")
        tags = tuple(_strict_label(item, "ai_smell_tag", 60) for item in tags_raw)
        if len(tags) != len(set(tags)):
            raise ValueError("ai_smell_tags must be unique")
        config = cls(
            schema_version=_strict_int(raw["schema_version"], "schema_version", minimum=1),
            seed=_strict_int(raw["seed"], "seed", minimum=0),
            min_pairs=_strict_int(raw["min_pairs"], "min_pairs", minimum=1),
            max_pairs=_strict_int(raw["max_pairs"], "max_pairs", minimum=1),
            max_output_chars=_strict_int(
                raw["max_output_chars"], "max_output_chars", minimum=1,
            ),
            max_context_chars=_strict_int(
                raw["max_context_chars"], "max_context_chars", minimum=1,
            ),
            max_note_chars=_strict_int(
                raw["max_note_chars"], "max_note_chars", minimum=1,
            ),
            max_ref_chars=_strict_int(raw["max_ref_chars"], "max_ref_chars", minimum=1),
            dimensions=weights,
            ai_smell_tags=tags,
        )
        if config.min_pairs > config.max_pairs:
            raise ValueError("human-like pair bounds are invalid")
        return config


class HumanLikeCalibration(HumanLikeCalibrationService):
    service_id = "human_like_calibration"

    def __init__(
        self, config: HumanLikeConfig, *, metrics: Any = None, enabled: bool = True,
    ) -> None:
        if not isinstance(config, HumanLikeConfig):
            raise ValueError("config must be HumanLikeConfig")
        self.config = config
        self.enabled = bool(enabled)
        self._metrics = metrics
        self._running = False
        self._built = 0
        self._finalized = 0
        self._failed = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool = True,
    ) -> "HumanLikeCalibration":
        return cls(HumanLikeConfig.from_loader(loader), metrics=metrics, enabled=enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, built=self._built,
            finalized=self._finalized, failed=self._failed,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def build(
        self, comparisons: tuple[Mapping[str, Any], ...],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.enabled:
            raise RuntimeError("human-like calibration is disabled")
        if not isinstance(comparisons, tuple):
            raise ValueError("comparisons must be a tuple")
        if not self.config.min_pairs <= len(comparisons) <= self.config.max_pairs:
            raise ValueError("human-like comparison count is outside configured bounds")
        review_rows: list[dict[str, Any]] = []
        sealed_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(comparisons):
            if not isinstance(raw, Mapping):
                raise ValueError("human-like comparison must be a mapping")
            pair_ref = _strict_label(raw.get("pair_ref"), "pair_ref", self.config.max_ref_chars)
            if pair_ref in seen:
                raise ValueError("human-like pair_ref must be unique")
            seen.add(pair_ref)
            blind_ref = _blind_ref(pair_ref)
            previous = self._candidate(raw.get("previous"), role="previous")
            candidate = self._candidate(raw.get("candidate"), role="candidate")
            swap = self._swap(blind_ref)
            ordered = (candidate, previous) if swap else (previous, candidate)
            review_rows.append({
                "pair_ref": blind_ref,
                "context_summary": _sanitized_text(
                    raw.get("context_summary"), self.config.max_context_chars,
                ),
                "candidate_a": ordered[0]["output"],
                "candidate_b": ordered[1]["output"],
                "review": {
                    "a": self._empty_score(),
                    "b": self._empty_score(),
                    "preferred": None,
                },
            })
            sealed_rows.append({
                "pair_ref": blind_ref,
                "a": self._sealed_candidate(ordered[0]),
                "b": self._sealed_candidate(ordered[1]),
            })
        review_content_digest = _digest({"rows": _blind_content(review_rows)})
        sealed_payload = {
            "schema_version": self.config.schema_version,
            "marker": "mai_hlc_sealed_manifest",
            "review_content_digest": review_content_digest,
            "rows": sealed_rows,
        }
        commitment = _digest(sealed_payload)
        manifest = {**sealed_payload, "commitment": commitment}
        artifact = {
            "schema_version": self.config.schema_version,
            "marker": "mai_hlc_blind_review",
            "sanitized": True,
            "raw_transcript_included": False,
            "build_identity_hidden": True,
            "director_score_hidden": True,
            "prompt_hidden": True,
            "memory_internals_hidden": True,
            "commitment": commitment,
            "dimensions": dict(self.config.dimensions),
            "ai_smell_tags": list(self.config.ai_smell_tags),
            "status": "pending_human_review",
            "rows": review_rows,
            "human_review": {"reviewer_role": "", "complete": False},
        }
        self._built += 1
        self._record_metric("built")
        return artifact, manifest

    async def finalize(
        self, review_path: Path, sealed_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("human-like calibration is disabled")
        try:
            artifact = await asyncio.to_thread(self._read_persisted, Path(review_path))
            output = self._finalize(artifact, sealed_manifest)
        except Exception:
            self._failed += 1
            self._record_metric("failed")
            raise
        self._finalized += 1
        self._record_metric("finalized")
        return output

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "enabled": self.enabled,
            "running": self._running,
            "built_total": self._built,
            "finalized_total": self._finalized,
            "failed_total": self._failed,
        }

    def get_metrics(self) -> dict[str, Any]:
        return {
            "human_like_built_total": self._built,
            "human_like_finalized_total": self._finalized,
            "human_like_failed_total": self._failed,
        }

    def _candidate(self, value: Any, *, role: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{role} candidate must be a mapping")
        required = {
            "build_identity", "output", "director_score", "prompt_ref", "memory_refs",
        }
        optional = {"sealed_metadata"}
        if not required <= set(value) or set(value) - required - optional:
            raise ValueError(f"{role} candidate keys are invalid")
        output = _sanitized_text(value["output"], self.config.max_output_chars)
        if not output:
            raise ValueError(f"{role} candidate output is required")
        score = value["director_score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("director_score must be a finite number")
        score = float(score)
        if not math.isfinite(score):
            raise ValueError("director_score must be a finite number")
        refs = value["memory_refs"]
        if not isinstance(refs, (list, tuple)):
            raise ValueError("memory_refs must be a list or tuple")
        memory_refs = tuple(
            _strict_label(item, "memory_ref", self.config.max_ref_chars) for item in refs
        )
        if len(memory_refs) != len(set(memory_refs)):
            raise ValueError("memory_refs must be unique")
        candidate = {
            "role": role,
            "build_identity": _strict_label(
                value["build_identity"], "build_identity", self.config.max_ref_chars,
            ),
            "output": output,
            "director_score": score,
            "prompt_ref": _strict_label(
                value["prompt_ref"], "prompt_ref", self.config.max_ref_chars,
            ),
            "memory_refs": memory_refs,
        }
        if "sealed_metadata" in value:
            candidate["sealed_metadata"] = _sealed_metadata(
                value["sealed_metadata"], self.config.max_ref_chars,
            )
        return candidate

    @staticmethod
    def _sealed_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
        candidate = {
            "role": value["role"],
            "build_identity": value["build_identity"],
            "director_score": value["director_score"],
            "prompt_ref": value["prompt_ref"],
            "memory_refs": list(value["memory_refs"]),
        }
        if "sealed_metadata" in value:
            candidate["sealed_metadata"] = value["sealed_metadata"]
        return candidate

    def _empty_score(self) -> dict[str, Any]:
        return {
            "dimensions": {name: None for name in DIMENSIONS},
            "ai_smell": None,
            "ai_smell_tags": [],
            "liveness": None,
            "action_coherence": None,
            "note": "",
        }

    def _swap(self, blind_ref: str) -> bool:
        digest = hashlib.sha256(
            f"{self.config.seed}:{blind_ref}".encode("utf-8"),
        ).digest()
        return bool(digest[0] & 1)

    @staticmethod
    def _read_persisted(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ValueError("persisted human-like review file is required")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("persisted human-like review must be a mapping")
        return value

    def _finalize(
        self, artifact: Mapping[str, Any], sealed_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        artifact_keys = {
            "schema_version", "marker", "sanitized", "raw_transcript_included",
            "build_identity_hidden", "director_score_hidden", "prompt_hidden",
            "memory_internals_hidden", "commitment", "dimensions", "ai_smell_tags",
            "status", "rows", "human_review",
        }
        if set(artifact) != artifact_keys:
            raise ValueError("human-like review artifact keys are invalid")
        if artifact.get("marker") != "mai_hlc_blind_review":
            raise ValueError("invalid human-like review marker")
        if artifact.get("status") != "pending_human_review":
            raise ValueError("human-like review is not pending")
        if artifact.get("sanitized") is not True:
            raise ValueError("human-like review must be sanitized")
        if artifact.get("raw_transcript_included") is not False or any(
            artifact.get(name) is not True
            for name in (
                "build_identity_hidden", "director_score_hidden", "prompt_hidden",
                "memory_internals_hidden",
            )
        ):
            raise ValueError("human-like review blindness flags are invalid")
        if artifact.get("dimensions") != dict(self.config.dimensions):
            raise ValueError("human-like review dimensions were modified")
        if artifact.get("ai_smell_tags") != list(self.config.ai_smell_tags):
            raise ValueError("human-like review tag policy was modified")
        if set(sealed_manifest) != {
            "schema_version", "marker", "review_content_digest", "rows", "commitment",
        }:
            raise ValueError("human-like sealed manifest keys are invalid")
        if sealed_manifest.get("marker") != "mai_hlc_sealed_manifest":
            raise ValueError("invalid human-like sealed manifest")
        manifest_payload = {
            "schema_version": sealed_manifest.get("schema_version"),
            "marker": sealed_manifest.get("marker"),
            "review_content_digest": sealed_manifest.get("review_content_digest"),
            "rows": sealed_manifest.get("rows"),
        }
        commitment = sealed_manifest.get("commitment")
        if not isinstance(commitment, str) or _digest(manifest_payload) != commitment:
            raise ValueError("human-like sealed manifest commitment mismatch")
        if artifact.get("commitment") != commitment:
            raise ValueError("human-like review and manifest do not match")
        review = artifact.get("human_review")
        if not isinstance(review, Mapping):
            raise ValueError("human-like review metadata is required")
        reviewer = _strict_label(
            review.get("reviewer_role"), "reviewer_role", self.config.max_ref_chars,
        )
        rows = artifact.get("rows")
        sealed_rows = sealed_manifest.get("rows")
        if not isinstance(rows, list) or not isinstance(sealed_rows, list):
            raise ValueError("human-like rows are invalid")
        if _digest({"rows": _blind_content(rows)}) != sealed_manifest.get(
            "review_content_digest",
        ):
            raise ValueError("human-like blind review content was modified")
        if len(rows) != len(sealed_rows) or not self.config.min_pairs <= len(rows) <= self.config.max_pairs:
            raise ValueError("human-like row count is invalid")
        by_ref: dict[str, Mapping[str, Any]] = {}
        for item in sealed_rows:
            if not isinstance(item, Mapping):
                raise ValueError("sealed human-like row is invalid")
            ref = _strict_label(item.get("pair_ref"), "pair_ref", self.config.max_ref_chars)
            if ref in by_ref:
                raise ValueError("sealed human-like pair_ref must be unique")
            by_ref[ref] = item
        role_scores: dict[str, list[dict[str, Any]]] = {"previous": [], "candidate": []}
        revealed_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("human-like review row is invalid")
            if set(row) != {
                "pair_ref", "context_summary", "candidate_a", "candidate_b", "review",
            }:
                raise ValueError("human-like review row keys are invalid")
            ref = _strict_label(row.get("pair_ref"), "pair_ref", self.config.max_ref_chars)
            if ref in seen or ref not in by_ref:
                raise ValueError("human-like review pair_ref mismatch")
            seen.add(ref)
            sealed = by_ref[ref]
            row_review = row.get("review")
            if not isinstance(row_review, Mapping) or set(row_review) != {"a", "b", "preferred"}:
                raise ValueError("human-like candidate review keys are invalid")
            preferred = str(row_review["preferred"] or "").upper()
            if preferred not in {"A", "B", "TIE"}:
                raise ValueError("human-like preferred must be A, B, or TIE")
            scores: dict[str, dict[str, Any]] = {}
            reveals: dict[str, dict[str, Any]] = {}
            for key in ("a", "b"):
                score = self._validate_score(row_review[key], key)
                identity = sealed.get(key)
                if not isinstance(identity, Mapping):
                    raise ValueError("sealed candidate identity is invalid")
                role = identity.get("role")
                if role not in role_scores:
                    raise ValueError("sealed candidate role is invalid")
                role_scores[str(role)].append(score)
                scores[key] = score
                reveals[key] = {
                    "role": role,
                    "build_identity": identity.get("build_identity"),
                    "director_score": identity.get("director_score"),
                    "prompt_ref": identity.get("prompt_ref"),
                    "memory_refs": list(identity.get("memory_refs") or []),
                }
                if "sealed_metadata" in identity:
                    reveals[key]["sealed_metadata"] = identity["sealed_metadata"]
            revealed_rows.append({
                "pair_ref": ref,
                "scores": scores,
                "preferred": preferred,
                "revealed": reveals,
            })
        summaries = {
            role: self._summarize(values) for role, values in role_scores.items()
        }
        output = {
            "schema_version": self.config.schema_version,
            "marker": "mai_hlc_finalized_review",
            "sanitized": True,
            "status": "review_complete",
            "automatic_release_decision": False,
            "commitment": commitment,
            "human_review": {"reviewer_role": reviewer, "complete": True},
            "summaries": summaries,
            "previous_build_delta": round(
                summaries["candidate"]["weighted_average"]
                - summaries["previous"]["weighted_average"],
                4,
            ),
            "rows": revealed_rows,
        }
        return output

    def _validate_score(self, value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"human-like score {label} is invalid")
        expected = {
            "dimensions", "ai_smell", "ai_smell_tags", "liveness",
            "action_coherence", "note",
        }
        if set(value) != expected:
            raise ValueError(f"human-like score {label} keys are invalid")
        dimensions = value["dimensions"]
        if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSIONS):
            raise ValueError("human-like dimension score keys are invalid")
        normalized = {
            name: _strict_int(dimensions[name], name, minimum=1, maximum=5)
            for name in DIMENSIONS
        }
        ai_smell = value["ai_smell"]
        if not isinstance(ai_smell, bool):
            raise ValueError("ai_smell must be a bool")
        tags_raw = value["ai_smell_tags"]
        if not isinstance(tags_raw, list):
            raise ValueError("ai_smell_tags must be a list")
        tags = tuple(_strict_label(item, "ai_smell_tag", 60) for item in tags_raw)
        if len(tags) != len(set(tags)) or any(tag not in self.config.ai_smell_tags for tag in tags):
            raise ValueError("ai_smell_tags contain an unsupported or duplicate tag")
        if ai_smell != bool(tags):
            raise ValueError("ai_smell and ai_smell_tags must agree")
        note = _sanitized_text(value["note"], self.config.max_note_chars)
        if not note:
            raise ValueError("human-like review note is required")
        weighted = sum(
            normalized[name] * weight for name, weight in self.config.dimensions
        )
        return {
            "dimensions": normalized,
            "ai_smell": ai_smell,
            "ai_smell_tags": list(tags),
            "liveness": _strict_int(value["liveness"], "liveness", minimum=1, maximum=5),
            "action_coherence": _strict_int(
                value["action_coherence"], "action_coherence", minimum=1, maximum=5,
            ),
            "note": note,
            "weighted_score": round(weighted, 4),
        }

    def _summarize(self, scores: list[dict[str, Any]]) -> dict[str, Any]:
        if not scores:
            raise ValueError("human-like role has no scores")
        dimension_averages = {
            name: round(
                sum(item["dimensions"][name] for item in scores) / len(scores), 4,
            )
            for name in DIMENSIONS
        }
        weakest = min(DIMENSIONS, key=lambda name: (dimension_averages[name], DIMENSIONS.index(name)))
        return {
            "reviewed_candidates": len(scores),
            "weighted_average": round(
                sum(item["weighted_score"] for item in scores) / len(scores), 4,
            ),
            "dimension_averages": dimension_averages,
            "weakest_dimension": weakest,
            "ai_smell_rate": round(
                sum(int(item["ai_smell"]) for item in scores) / len(scores), 4,
            ),
            "liveness_average": round(
                sum(item["liveness"] for item in scores) / len(scores), 4,
            ),
            "action_coherence_average": round(
                sum(item["action_coherence"] for item in scores) / len(scores), 4,
            ),
        }

    def _record_metric(self, outcome: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_human_like_review"):
            try:
                self._metrics.record_human_like_review(outcome)
            except Exception:
                pass


def _finite_weight(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"human-like weight {name} must be finite")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > 1:
        raise ValueError(f"human-like weight {name} must be within (0, 1]")
    return result


def _strict_int(
    value: Any, name: str, *, minimum: int, maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _strict_label(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ValueError(f"{name} exceeds the configured bound")
    return text


def _sanitized_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("review text must be a string")
    return " ".join((mask_pii(value) or "").split())[:limit]


def _blind_ref(value: str) -> str:
    return "pair:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _digest(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _blind_content(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("human-like review row is invalid")
        result.append({
            "pair_ref": row.get("pair_ref"),
            "context_summary": row.get("context_summary"),
            "candidate_a": row.get("candidate_a"),
            "candidate_b": row.get("candidate_b"),
        })
    return result


def _sealed_metadata(value: Any, limit: int) -> dict[str, Any]:
    """Keep bounded scalar identity metadata sealed until human scores persist."""
    if not isinstance(value, Mapping) or not value or len(value) > 20:
        raise ValueError("sealed_metadata must be a bounded non-empty mapping")
    output: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _strict_label(raw_key, "sealed_metadata key", 60)
        if key in output:
            raise ValueError("sealed_metadata keys must be unique")
        if isinstance(raw_value, bool) or raw_value is None:
            output[key] = raw_value
        elif isinstance(raw_value, int) and not isinstance(raw_value, bool):
            output[key] = raw_value
        elif isinstance(raw_value, float) and math.isfinite(raw_value):
            output[key] = raw_value
        elif isinstance(raw_value, str):
            output[key] = _strict_label(raw_value, f"sealed_metadata.{key}", limit)
        else:
            raise ValueError("sealed_metadata values must be bounded JSON scalars")
    return output
