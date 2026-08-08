"""Frozen M8 dataset contract, quarantine decisions, and session-level splits."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


Record = dict[str, Any]
Identity = tuple[str, int]
SPLITS = ("train", "validation", "holdout")


@dataclass(frozen=True)
class DataContract:
    schema_version: int
    contract_id: str
    architecture_version: str
    persona_version: str
    turn_schema_version: int
    sft_schema_version: int
    dpo_schema_version: int
    context_schema_version: str
    agenda_policy_version: str
    required_turn_fields: tuple[str, ...]
    split_seed: int
    train_ratio: float
    validation_ratio: float
    holdout_ratio: float

    def __post_init__(self) -> None:
        if not self.contract_id or self.schema_version < 1:
            raise ValueError("data contract id and schema version are required")
        total = self.train_ratio + self.validation_ratio + self.holdout_ratio
        if abs(total - 1.0) > 1e-9 or min(
            self.train_ratio, self.validation_ratio, self.holdout_ratio,
        ) <= 0:
            raise ValueError("data split ratios must be positive and sum to 1")


@dataclass(frozen=True)
class QualityDecision:
    eligible: bool
    reasons: tuple[str, ...]


def load_data_contract(path: str | Path) -> DataContract:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("data contract root must be a mapping")
    split = value.get("split")
    if not isinstance(split, dict):
        raise ValueError("data contract split is required")
    return DataContract(
        schema_version=int(value["schema_version"]),
        contract_id=str(value["contract_id"]),
        architecture_version=str(value["architecture_version"]),
        persona_version=str(value["persona_version"]),
        turn_schema_version=int(value["turn_schema_version"]),
        sft_schema_version=int(value["sft_schema_version"]),
        dpo_schema_version=int(value["dpo_schema_version"]),
        context_schema_version=str(value["context_schema_version"]),
        agenda_policy_version=str(value["agenda_policy_version"]),
        required_turn_fields=tuple(str(item) for item in value["required_turn_fields"]),
        split_seed=int(split["seed"]),
        train_ratio=float(split["train_ratio"]),
        validation_ratio=float(split["validation_ratio"]),
        holdout_ratio=float(split["holdout_ratio"]),
    )


class DatasetQualityGate:
    """Pure quality gate: it reports reasons and never mutates raw records."""

    def __init__(self, contract: DataContract) -> None:
        self.contract = contract

    def assess_turn(self, record: Mapping[str, Any]) -> QualityDecision:
        reasons: list[str] = []
        for field in self.contract.required_turn_fields:
            value = record.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                reasons.append(f"missing:{field}")
        if record.get("schema_version") != self.contract.turn_schema_version:
            reasons.append("incompatible:turn_schema_version")
        expected_versions = {
            "architecture_version": self.contract.architecture_version,
            "persona_version": self.contract.persona_version,
            "context_schema_version": self.contract.context_schema_version,
            "agenda_policy_version": self.contract.agenda_policy_version,
        }
        for field, expected in expected_versions.items():
            if record.get(field) is not None and record.get(field) != expected:
                reasons.append(f"incompatible:{field}")
        session_id = record.get("session_id")
        if isinstance(session_id, str) and session_id.startswith("legacy:"):
            reasons.append("incompatible:legacy_session")
        if record.get("level_used", 0) != 0:
            reasons.append("quality:not_primary")
        if record.get("parse_ok") is False:
            reasons.append("quality:parse_failed")
        if not str(record.get("mai_text") or "").strip():
            reasons.append("quality:empty_target")
        verdict = record.get("filter_verdict") or {}
        if isinstance(verdict, Mapping) and verdict.get("passed") is False and not verdict.get("regen"):
            reasons.append("quality:filter_blocked")
        return QualityDecision(not reasons, tuple(sorted(set(reasons))))

    def assess_preference(self, record: Mapping[str, Any]) -> QualityDecision:
        reasons: list[str] = []
        prompt = record.get("prompt_ref") or {}
        if not isinstance(prompt, Mapping):
            prompt = {}
        if record.get("schema_version") != self.contract.dpo_schema_version:
            reasons.append("incompatible:dpo_schema_version")
        if not str(record.get("session_id") or "").strip():
            reasons.append("missing:session_id")
        for field, expected in (
            ("architecture_version", self.contract.architecture_version),
            ("persona_version", self.contract.persona_version),
            ("context_schema_version", self.contract.context_schema_version),
            ("agenda_policy_version", self.contract.agenda_policy_version),
        ):
            if prompt.get(field) != expected:
                reasons.append(f"incompatible:{field}")
        if not str(prompt.get("persona_version") or "").strip():
            reasons.append("missing:persona_version")
        if not str(record.get("chosen") or "").strip() or not str(record.get("rejected") or "").strip():
            reasons.append("quality:empty_preference")
        if record.get("chosen") == record.get("rejected"):
            reasons.append("quality:identical_preference")
        return QualityDecision(not reasons, tuple(sorted(set(reasons))))

    def split_for_session(self, session_id: str) -> str:
        if not session_id:
            raise ValueError("session id is required for dataset split")
        digest = hashlib.sha256(
            f"{self.contract.split_seed}:{session_id}".encode("utf-8")
        ).digest()
        point = int.from_bytes(digest[:8], "big") / float(2**64)
        if point < self.contract.train_ratio:
            return "train"
        if point < self.contract.train_ratio + self.contract.validation_ratio:
            return "validation"
        return "holdout"

    def partition(self, records: Sequence[Record]) -> dict[str, list[Record]]:
        output = {name: [] for name in SPLITS}
        for record in records:
            session_id = str(record.get("session_id") or "")
            output[self.split_for_session(session_id)].append(record)
        return output


def quality_report(
    turns: Sequence[Record],
    gate: DatasetQualityGate,
    *,
    ratings: Mapping[Identity, str],
    corrections: set[Identity],
) -> tuple[list[Record], dict[str, Any]]:
    eligible: list[Record] = []
    quarantine: list[dict[str, Any]] = []
    distributions: dict[str, Counter[str]] = {
        name: Counter() for name in (
            "kind", "mood", "goal", "source", "operator_rating", "correction", "filter_hit",
        )
    }
    for record in turns:
        decision = gate.assess_turn(record)
        identity = _strict_identity(record)
        if not decision.eligible:
            quarantine.append({
                "session_ref": _session_ref(record.get("session_id")),
                "turn_id": record.get("turn_id"),
                "reasons": list(decision.reasons),
            })
            continue
        eligible.append(record)
        distributions["kind"][str(record.get("kind") or "unknown")] += 1
        distributions["mood"][str(record.get("mood_dominant") or "unknown")] += 1
        distributions["goal"][str(record.get("goal_id") or "none")] += 1
        distributions["source"][str(record.get("source") or "unknown")] += 1
        distributions["operator_rating"][str(ratings.get(identity, "unrated"))] += 1
        distributions["correction"]["corrected" if identity in corrections else "original"] += 1
        verdict = record.get("filter_verdict") or {}
        hit = bool(isinstance(verdict, Mapping) and verdict.get("categories"))
        distributions["filter_hit"]["hit" if hit else "clean"] += 1
    split_sessions: dict[str, set[str]] = {name: set() for name in SPLITS}
    for record in eligible:
        session = str(record["session_id"])
        split_sessions[gate.split_for_session(session)].add(session)
    report = {
        "contract_id": gate.contract.contract_id,
        "raw_turns": len(turns),
        "eligible_turns": len(eligible),
        "quarantined_turns": len(quarantine),
        "quarantine_reason_counts": dict(sorted(Counter(
            reason for row in quarantine for reason in row["reasons"]
        ).items())),
        "quarantine": quarantine,
        "distribution": {
            key: dict(sorted(counter.items())) for key, counter in distributions.items()
        },
        "split_session_counts": {
            key: len(value) for key, value in split_sessions.items()
        },
        "session_leakage": False,
    }
    return eligible, report


def _strict_identity(record: Mapping[str, Any]) -> Identity | None:
    try:
        session_id = str(record["session_id"])
        turn_id = int(record["turn_id"])
    except (KeyError, TypeError, ValueError):
        return None
    return (session_id, turn_id) if session_id else None


def _session_ref(value: Any) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"session:{digest}"
