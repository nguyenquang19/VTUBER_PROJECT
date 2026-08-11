"""Blind human A/B review workflow for Mood v1 versus Mood v2."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from interfaces.base import HealthStatus
from interfaces.evaluation import MoodABReviewService
from services.data.sanitize import mask_pii


class MoodABReview(MoodABReviewService):
    service_id = "mood_ab_review"

    def __init__(
        self,
        *,
        seed: int,
        min_turns: int,
        min_appropriate_ratio: float,
        max_note_chars: int = 400,
        metrics: Any = None,
    ) -> None:
        if min_turns <= 0 or not 0 <= min_appropriate_ratio <= 1 or max_note_chars <= 0:
            raise ValueError("mood A/B review bounds are invalid")
        self.seed = int(seed)
        self.min_turns = int(min_turns)
        self.min_appropriate_ratio = float(min_appropriate_ratio)
        self.max_note_chars = int(max_note_chars)
        self._metrics = metrics
        self._running = False
        self._built = 0
        self._finalized = 0

    @classmethod
    def from_loader(cls, loader: Any, *, metrics: Any = None) -> "MoodABReview":
        policy = loader.get("affect_v2", "policy", {}) or {}
        return cls(
            seed=int(policy.get("ab_seed", 20260809)),
            min_turns=int(policy.get("ab_min_turns", 50)),
            min_appropriate_ratio=float(policy.get("ab_min_appropriate_ratio", 0.8)),
            metrics=metrics,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, built=self._built, finalized=self._finalized,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {"mood_ab_built_total": self._built, "mood_ab_finalized_total": self._finalized}

    def build(self, comparisons: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(comparisons):
            turn_ref = _safe_ref(raw.get("turn_ref"), index)
            if turn_ref in seen:
                raise ValueError(f"duplicate mood A/B turn_ref: {turn_ref}")
            seen.add(turn_ref)
            v1 = _candidate(raw.get("v1_output", raw.get("v1_directive")))
            v2 = _candidate(raw.get("v2_output", raw.get("v2_directive")))
            if not v1 or not v2:
                raise ValueError("mood A/B comparison requires both candidate outputs")
            blind_ref = _evidence_ref(turn_ref)
            swap = self._swap(blind_ref)
            rows.append({
                "turn_ref": blind_ref,
                "event_category": _safe_category(raw.get("event_category")),
                "input": _evidence_text(raw.get("input"), 500),
                "context": _evidence_text(raw.get("context"), 800),
                "candidate_a": v2 if swap else v1,
                "candidate_b": v1 if swap else v2,
                "review": {
                    "emotional_appropriate": None,
                    "persona": None,
                    "naturalness": None,
                    "overacting": None,
                    "preferred": None,
                    "note": "",
                },
            })
        self._built += 1
        self._record("pending_human_review")
        return {
            "schema_version": 1,
            "milestone": "M10.6",
            "marker": "mood_ab_blind_review",
            "seed": self.seed,
            "sanitized": True,
            "raw_transcript_included": False,
            "same_input_context_per_pair": True,
            "minimum_turns": self.min_turns,
            "minimum_appropriate_ratio": self.min_appropriate_ratio,
            "turn_count": len(rows),
            "correctness_unchanged": True,
            "status": "pending_human_review",
            "rows": rows,
            "human_review": {"reviewer_role": "", "complete": False},
        }

    def finalize(self, artifact: dict[str, Any]) -> dict[str, Any]:
        if artifact.get("marker") != "mood_ab_blind_review" or artifact.get("sanitized") is not True:
            raise ValueError("mood A/B review requires a sanitized blind artifact")
        rows = [dict(item) for item in artifact.get("rows") or []]
        if len(rows) < self.min_turns:
            raise ValueError(f"mood A/B review requires at least {self.min_turns} turns")
        reviewer = " ".join(str((artifact.get("human_review") or {}).get("reviewer_role") or "").split())
        if not reviewer:
            raise ValueError("mood A/B human reviewer role is required")
        appropriate = overacting = v1_wins = v2_wins = ties = 0
        persona_total = naturalness_total = 0
        for row in rows:
            review = dict(row.get("review") or {})
            if not isinstance(review.get("emotional_appropriate"), bool):
                raise ValueError("emotional_appropriate must be reviewed as boolean")
            if not isinstance(review.get("overacting"), bool):
                raise ValueError("overacting must be reviewed as boolean")
            persona = _score(review.get("persona"), "persona")
            naturalness = _score(review.get("naturalness"), "naturalness")
            preferred = str(review.get("preferred") or "").upper()
            if preferred not in {"A", "B", "TIE"}:
                raise ValueError("preferred must be A, B, or TIE")
            note = " ".join(mask_pii(str(review.get("note") or "")).split())[: self.max_note_chars]
            if not note:
                raise ValueError("mood A/B review note is required")
            review["note"] = note
            row["review"] = review
            appropriate += int(review["emotional_appropriate"])
            overacting += int(review["overacting"])
            persona_total += persona
            naturalness_total += naturalness
            if preferred == "TIE":
                ties += 1
            else:
                selected_v2 = (preferred == "A") == self._swap(str(row["turn_ref"]))
                if selected_v2:
                    v2_wins += 1
                else:
                    v1_wins += 1
        ratio = appropriate / len(rows)
        correctness = artifact.get("correctness_unchanged") is True
        passed = correctness and ratio >= self.min_appropriate_ratio and v2_wins >= v1_wins
        output = dict(artifact)
        output["rows"] = rows
        output["status"] = "passed" if passed else "failed"
        output["passed"] = passed
        output["cutover_recommended"] = passed
        output["human_review"] = {
            "reviewer_role": reviewer,
            "complete": True,
            "appropriate_ratio": round(ratio, 4),
            "persona_average": round(persona_total / len(rows), 3),
            "naturalness_average": round(naturalness_total / len(rows), 3),
            "overacting_ratio": round(overacting / len(rows), 4),
            "v1_wins": v1_wins,
            "v2_wins": v2_wins,
            "ties": ties,
        }
        self._finalized += 1
        self._record(output["status"])
        return output

    def _swap(self, turn_ref: str) -> bool:
        digest = hashlib.sha256(f"{self.seed}:{turn_ref}".encode("utf-8")).digest()
        return bool(digest[0] & 1)

    def _record(self, outcome: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_mood_ab_review"):
            try:
                self._metrics.record_mood_ab_review(outcome)
            except Exception:
                pass


def _candidate(value: Any) -> str:
    return " ".join(mask_pii(str(value or "")).split())[:400]


def _evidence_text(value: Any, max_chars: int) -> str:
    return " ".join(mask_pii(str(value or "")).split())[:max_chars]


def _safe_category(value: Any) -> str:
    category = str(value or "unknown").strip().lower()
    return category if re.fullmatch(r"[a-z0-9_:-]{1,60}", category) else "unknown"


def _safe_ref(value: Any, index: int) -> str:
    clean = str(value or f"turn-{index + 1}").strip()
    return clean[:120]


def _evidence_ref(value: str) -> str:
    return "turn:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _score(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError(f"{name} score must be an integer within [1, 5]")
    return value
