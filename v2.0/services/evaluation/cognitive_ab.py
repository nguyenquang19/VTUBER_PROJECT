"""Strict source-bound MCB-4 offline comparison and blind-review artifacts."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from services.data.sanitize import mask_pii
from services.evaluation.human_like import HumanLikeCalibration


COGNITIVE_AB_MODES = ("WAIT", "SPEAK")
COGNITIVE_AB_OUTCOMES = (
    "COMPLETED",
    "PREFLIGHT_REJECTED",
    "TIMEOUT",
    "PARSE_REJECTED",
    "SCHEMA_REJECTED",
    "FILTER_REJECTED",
    "STALE",
    "CANCELLED",
    "SERVICE_ERROR",
)
_CONFIG_KEYS = {
    "schema_version",
    "corpus_file",
    "seed",
    "minimum_cases",
    "minimum_blind_pairs",
    "maximum_blind_pairs",
    "minimum_selected_per_arc",
    "max_prior_turns",
    "wait_display_marker",
    "max_context_summary_chars",
    "max_candidate_output_chars",
    "generation_max_tokens",
    "generation_temperature",
    "strict_source_clean_for_gate",
    "required_strata",
    "required_arcs",
}
_IDENTITY_KEYS = {
    "config_digest",
    "corpus_digest",
    "model_digest",
    "persona_digest",
    "compatibility_prompt_digest",
    "brain_prompt_digest",
}
_ROW_KEYS = {
    "case_id",
    "context_id",
    "context_summary",
    "same_input_context",
    "profile_ref",
    "model_ref",
    "seed",
    "max_tokens",
    "temperature",
    "hard_flags",
    "compatibility",
    "brain",
}
_CANDIDATE_KEYS = {
    "mode",
    "action_label",
    "output",
    "outcome",
    "prompt_ref",
    "latency_ms",
    "input_tokens",
    "output_tokens",
}


@dataclass(frozen=True)
class CognitiveABConfig:
    schema_version: int
    corpus_file: Path
    seed: int
    minimum_cases: int
    minimum_blind_pairs: int
    maximum_blind_pairs: int
    minimum_selected_per_arc: int
    max_prior_turns: int
    wait_display_marker: str
    max_context_summary_chars: int
    max_candidate_output_chars: int
    generation_max_tokens: int
    generation_temperature: float
    strict_source_clean_for_gate: bool
    required_strata: tuple[str, ...]
    required_arcs: tuple[str, ...]

    @classmethod
    def from_loader(cls, loader: Any) -> "CognitiveABConfig":
        raw = loader.get("evaluation", "evaluation.cognitive_ab", None)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: Any) -> "CognitiveABConfig":
        if not isinstance(raw, dict) or set(raw) != _CONFIG_KEYS:
            raise ValueError("evaluation.cognitive_ab keys are invalid")
        strata_raw = raw["required_strata"]
        if not isinstance(strata_raw, list) or not strata_raw:
            raise ValueError("cognitive A/B required_strata must be a non-empty list")
        strata = tuple(_label(value, "stratum", 64) for value in strata_raw)
        if len(strata) != len(set(strata)):
            raise ValueError("cognitive A/B required_strata must be unique")
        arcs_raw = raw["required_arcs"]
        if not isinstance(arcs_raw, list) or not arcs_raw:
            raise ValueError("cognitive A/B required_arcs must be a non-empty list")
        arcs = tuple(_label(value, "arc", 64) for value in arcs_raw)
        if len(arcs) != len(set(arcs)):
            raise ValueError("cognitive A/B required_arcs must be unique")
        corpus_text = _label(raw["corpus_file"], "corpus_file", 260)
        minimum_cases = _integer(raw["minimum_cases"], "minimum_cases", minimum=1)
        minimum_pairs = _integer(
            raw["minimum_blind_pairs"], "minimum_blind_pairs", minimum=30,
        )
        maximum_pairs = _integer(
            raw["maximum_blind_pairs"], "maximum_blind_pairs", minimum=minimum_pairs,
        )
        if minimum_cases < minimum_pairs:
            raise ValueError("minimum_cases must cover minimum_blind_pairs")
        minimum_arc = _integer(
            raw["minimum_selected_per_arc"], "minimum_selected_per_arc", minimum=1,
        )
        if minimum_arc * len(arcs) > maximum_pairs:
            raise ValueError("maximum_blind_pairs cannot cover required arc selection")
        clean = raw["strict_source_clean_for_gate"]
        if not isinstance(clean, bool):
            raise ValueError("strict_source_clean_for_gate must be a bool")
        temperature = _number(
            raw["generation_temperature"], "generation_temperature", minimum=0.0,
        )
        if temperature > 2.0:
            raise ValueError("generation_temperature is outside the allowed range")
        return cls(
            schema_version=_integer(raw["schema_version"], "schema_version", minimum=1),
            corpus_file=Path(corpus_text),
            seed=_integer(raw["seed"], "seed", minimum=0),
            minimum_cases=minimum_cases,
            minimum_blind_pairs=minimum_pairs,
            maximum_blind_pairs=maximum_pairs,
            minimum_selected_per_arc=minimum_arc,
            max_prior_turns=_integer(
                raw["max_prior_turns"], "max_prior_turns", minimum=1,
            ),
            wait_display_marker=_label(
                raw["wait_display_marker"], "wait_display_marker", 80,
            ),
            max_context_summary_chars=_integer(
                raw["max_context_summary_chars"],
                "max_context_summary_chars",
                minimum=1,
            ),
            max_candidate_output_chars=_integer(
                raw["max_candidate_output_chars"],
                "max_candidate_output_chars",
                minimum=1,
            ),
            generation_max_tokens=_integer(
                raw["generation_max_tokens"], "generation_max_tokens", minimum=1,
            ),
            generation_temperature=temperature,
            strict_source_clean_for_gate=clean,
            required_strata=strata,
            required_arcs=arcs,
        )


@dataclass(frozen=True)
class CognitiveABScenario:
    kind: str
    sender_role: str
    evidence_state: str
    chat_score: float
    pulse_state: str
    urge_ready: bool
    safety_hold: bool
    operator_hold: bool
    self_talk_ready: bool
    tone_flags: tuple[str, ...]
    amount_vnd: int


@dataclass(frozen=True)
class CognitiveABPriorTurn:
    role: str
    text: str


@dataclass(frozen=True)
class CognitiveABCase:
    case_id: str
    arc_id: str
    arc_title: str
    turn_index: int
    arc_length: int
    stratum: str
    context_summary: str
    input_text: str
    prior_turns: tuple[CognitiveABPriorTurn, ...]
    scenario: CognitiveABScenario

    @property
    def review_context(self) -> str:
        lines = [f"Tập: {self.arc_title} · Lượt {self.turn_index}/{self.arc_length}"]
        if self.prior_turns:
            transcript = " / ".join(
                f'{ {"viewer": "Viewer", "mai": "Mai", "operator": "Operator"}[turn.role] }: {turn.text}'
                for turn in self.prior_turns
            )
            lines.append(f"Trước đó: {transcript}")
        lines.append(f"Tình huống: {self.context_summary}")
        lines.append(f"Tin nhắn hiện tại: {self.input_text}")
        return " | ".join(lines)


@dataclass(frozen=True)
class CognitiveABCorpus:
    schema_version: int
    corpus_id: str
    digest: str
    cases: tuple[CognitiveABCase, ...]

    @classmethod
    def load(cls, path: Path, config: CognitiveABConfig) -> "CognitiveABCorpus":
        target = Path(path)
        if not target.is_file():
            raise ValueError("cognitive A/B corpus file is required")
        raw_bytes = target.read_bytes()
        raw = yaml.safe_load(raw_bytes.decode("utf-8"))
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "corpus_id", "sanitized", "cases",
        }:
            raise ValueError("cognitive A/B corpus shape is invalid")
        if raw["sanitized"] is not True:
            raise ValueError("cognitive A/B corpus must be sanitized")
        if raw["schema_version"] != config.schema_version:
            raise ValueError("cognitive A/B corpus schema_version mismatch")
        rows = raw["cases"]
        if not isinstance(rows, list) or len(rows) < config.minimum_cases:
            raise ValueError("cognitive A/B corpus has too few cases")
        cases: list[CognitiveABCase] = []
        seen: set[str] = set()
        counts: Counter[str] = Counter()
        for value in rows:
            if not isinstance(value, dict) or set(value) != {
                "case_id", "arc_id", "arc_title", "turn_index", "arc_length",
                "stratum", "context_summary", "input_text", "prior_turns", "scenario",
            }:
                raise ValueError("cognitive A/B case shape is invalid")
            case_id = _label(value["case_id"], "case_id", 120)
            if case_id in seen:
                raise ValueError("cognitive A/B case_id must be unique")
            seen.add(case_id)
            arc_id = _label(value["arc_id"], "arc_id", 64)
            if arc_id not in config.required_arcs:
                raise ValueError("cognitive A/B case uses an unsupported arc")
            turn_index = _integer(value["turn_index"], "turn_index", minimum=1)
            arc_length = _integer(value["arc_length"], "arc_length", minimum=1)
            if turn_index > arc_length:
                raise ValueError("cognitive A/B turn_index exceeds arc_length")
            stratum = _label(value["stratum"], "stratum", 64)
            if stratum not in config.required_strata:
                raise ValueError("cognitive A/B case uses an unsupported stratum")
            context = _text(
                value["context_summary"],
                "context_summary",
                config.max_context_summary_chars,
            )
            input_text = _text(
                value["input_text"], "input_text", config.max_candidate_output_chars,
            )
            prior_turns = _prior_turns(value["prior_turns"], config)
            if (turn_index == 1) != (not prior_turns):
                raise ValueError("first story beat alone must have empty prior_turns")
            if len(prior_turns) > 2 * (turn_index - 1):
                raise ValueError("prior_turns contains future story beats")
            case = CognitiveABCase(
                case_id=case_id,
                arc_id=arc_id,
                arc_title=_text(value["arc_title"], "arc_title", 120),
                turn_index=turn_index,
                arc_length=arc_length,
                stratum=stratum,
                context_summary=context,
                input_text=input_text,
                prior_turns=prior_turns,
                scenario=_scenario(value["scenario"]),
            )
            _text(case.review_context, "review_context", config.max_context_summary_chars)
            cases.append(case)
            counts[stratum] += 1
        if any(counts[name] == 0 for name in config.required_strata):
            raise ValueError("cognitive A/B corpus does not cover required_strata")
        _validate_story_arcs(cases, config)
        return cls(
            schema_version=config.schema_version,
            corpus_id=_label(raw["corpus_id"], "corpus_id", 120),
            digest=hashlib.sha256(raw_bytes).hexdigest(),
            cases=tuple(cases),
        )


class CognitiveABEvaluation:
    """Validate paired evidence and build one tamper-evident MAI-HLC review."""

    def __init__(
        self,
        config: CognitiveABConfig,
        corpus: CognitiveABCorpus,
        human_like: HumanLikeCalibration,
        *,
        metrics: Any = None,
    ) -> None:
        self.config = config
        self.corpus = corpus
        self.human_like = human_like
        self._metrics = metrics
        self._built = 0
        self._finalized = 0
        self._failed = 0

    @classmethod
    def from_loader(
        cls, loader: Any, *, repo_root: Path, metrics: Any = None,
    ) -> "CognitiveABEvaluation":
        config = CognitiveABConfig.from_loader(loader)
        corpus_path = config.corpus_file
        if not corpus_path.is_absolute():
            corpus_path = Path(repo_root) / corpus_path
        corpus = CognitiveABCorpus.load(corpus_path, config)
        return cls(
            config,
            corpus,
            HumanLikeCalibration.from_loader(loader, metrics=metrics, enabled=True),
            metrics=metrics,
        )

    def build(
        self, source: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        try:
            normalized = self._source(source)
            selected, summary = self._select(normalized["rows"])
            source_digest = _digest(normalized)
            comparisons = tuple(
                self._human_comparison(row, source_digest) for row in selected
            )
            review, manifest = self.human_like.build(comparisons)
            private = {
                "schema_version": self.config.schema_version,
                "marker": "mai_cognitive_ab_private_comparison",
                "sanitized": True,
                "automatic_release_decision": False,
                "source_digest": source_digest,
                "review_commitment": review["commitment"],
                "source_revision": normalized["source_revision"],
                "source_clean": normalized["source_clean"],
                "source_gate_eligible": (
                    normalized["source_clean"]
                    or not self.config.strict_source_clean_for_gate
                ),
                "product_version": normalized["product_version"],
                "evidence_identity": normalized["evidence_identity"],
                "corpus": {
                    "corpus_id": self.corpus.corpus_id,
                    "corpus_digest": self.corpus.digest,
                    "case_count": len(self.corpus.cases),
                },
                "summary": summary,
                "selected_pair_refs": [row["case_id"] for row in selected],
                "rows": normalized["rows"],
            }
        except Exception:
            self._failed += 1
            self._record("pair", "failed")
            raise
        self._built += 1
        self._record("pair", "built")
        return private, review, manifest

    async def finalize(
        self,
        review_path: Path,
        manifest: Mapping[str, Any],
        private: Mapping[str, Any],
    ) -> dict[str, Any]:
        if private.get("marker") != "mai_cognitive_ab_private_comparison":
            raise ValueError("cognitive A/B private artifact is invalid")
        if private.get("review_commitment") != manifest.get("commitment"):
            raise ValueError("cognitive A/B private/review commitment mismatch")
        try:
            final = await self.human_like.finalize(Path(review_path), manifest)
        except Exception:
            self._failed += 1
            self._record("pair", "failed")
            raise
        self._finalized += 1
        self._record("pair", "finalized")
        return {
            "schema_version": self.config.schema_version,
            "marker": "mai_cognitive_ab_finalized_review",
            "sanitized": True,
            "status": "review_complete",
            "automatic_release_decision": False,
            "owner_go_no_go_required": True,
            "source_digest": private.get("source_digest"),
            "review_commitment": private.get("review_commitment"),
            "source_revision": private.get("source_revision"),
            "source_clean": private.get("source_clean"),
            "source_gate_eligible": private.get("source_gate_eligible"),
            "product_version": private.get("product_version"),
            "evidence_identity": private.get("evidence_identity"),
            "technical_summary": private.get("summary"),
            "human_like": final,
        }

    def snapshot(self) -> dict[str, int]:
        return {
            "built_total": self._built,
            "finalized_total": self._finalized,
            "failed_total": self._failed,
        }

    def _source(self, source: Mapping[str, Any]) -> dict[str, Any]:
        expected = {
            "schema_version", "marker", "source_revision", "source_clean",
            "product_version", "evidence_identity", "rows",
        }
        if not isinstance(source, Mapping) or set(source) != expected:
            raise ValueError("cognitive A/B source artifact keys are invalid")
        if source["schema_version"] != self.config.schema_version:
            raise ValueError("cognitive A/B source schema_version mismatch")
        if source["marker"] != "mai_cognitive_ab_source":
            raise ValueError("cognitive A/B source marker is invalid")
        source_clean = source["source_clean"]
        if not isinstance(source_clean, bool):
            raise ValueError("cognitive A/B source_clean must be a bool")
        identities = source["evidence_identity"]
        if not isinstance(identities, Mapping) or set(identities) != _IDENTITY_KEYS:
            raise ValueError("cognitive A/B evidence identity keys are invalid")
        normalized_identity = {
            key: _digest_label(identities[key], key) for key in sorted(_IDENTITY_KEYS)
        }
        if normalized_identity["corpus_digest"] != self.corpus.digest:
            raise ValueError("cognitive A/B corpus digest mismatch")
        raw_rows = source["rows"]
        if not isinstance(raw_rows, list) or len(raw_rows) < self.config.minimum_cases:
            raise ValueError("cognitive A/B source has too few cases")
        cases = {case.case_id: case for case in self.corpus.cases}
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            row = self._row(raw, cases)
            if row["case_id"] in seen:
                raise ValueError("cognitive A/B source case_id must be unique")
            seen.add(row["case_id"])
            if row["profile_ref"] != normalized_identity["persona_digest"]:
                raise ValueError("cognitive A/B profile identity mismatch")
            if row["model_ref"] != normalized_identity["model_digest"]:
                raise ValueError("cognitive A/B model identity mismatch")
            if (
                row["compatibility"]["prompt_ref"]
                != normalized_identity["compatibility_prompt_digest"]
                or row["brain"]["prompt_ref"]
                != normalized_identity["brain_prompt_digest"]
            ):
                raise ValueError("cognitive A/B prompt identity mismatch")
            rows.append(row)
        if set(cases) != seen:
            raise ValueError("cognitive A/B source must contain every corpus case exactly once")
        return {
            "schema_version": self.config.schema_version,
            "marker": "mai_cognitive_ab_source",
            "source_revision": _label(source["source_revision"], "source_revision", 120),
            "source_clean": source_clean,
            "product_version": _label(source["product_version"], "product_version", 32),
            "evidence_identity": normalized_identity,
            "rows": rows,
        }

    def _row(
        self, raw: Any, cases: Mapping[str, CognitiveABCase],
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _ROW_KEYS:
            raise ValueError("cognitive A/B row keys are invalid")
        case_id = _label(raw["case_id"], "case_id", 120)
        case = cases.get(case_id)
        if case is None:
            raise ValueError("cognitive A/B row references an unknown case")
        if raw["same_input_context"] is not True:
            raise ValueError("cognitive A/B pair must use the same input context")
        context_summary = _text(
            raw["context_summary"],
            "context_summary",
            self.config.max_context_summary_chars,
        )
        if context_summary != case.review_context:
            raise ValueError("cognitive A/B context summary does not match corpus")
        seed = _integer(raw["seed"], "seed", minimum=0)
        max_tokens = _integer(raw["max_tokens"], "max_tokens", minimum=1)
        if max_tokens != self.config.generation_max_tokens:
            raise ValueError("cognitive A/B max_tokens mismatch")
        temperature = _number(raw["temperature"], "temperature", minimum=0.0)
        if not math.isclose(
            temperature, self.config.generation_temperature, abs_tol=1e-12,
        ):
            raise ValueError("cognitive A/B temperature mismatch")
        hard_raw = raw["hard_flags"]
        if not isinstance(hard_raw, list):
            raise ValueError("cognitive A/B hard_flags must be a list")
        hard_flags = tuple(_label(value, "hard_flag", 64) for value in hard_raw)
        if len(hard_flags) != len(set(hard_flags)):
            raise ValueError("cognitive A/B hard_flags must be unique")
        compatibility = self._candidate(raw["compatibility"], "compatibility")
        brain = self._candidate(raw["brain"], "brain")
        self._record("case", "validated", role=case.stratum)
        self._record("candidate", compatibility["outcome"], role="compatibility")
        self._record("candidate", brain["outcome"], role="brain")
        self._record("mode", compatibility["mode"], role="compatibility")
        self._record("mode", brain["mode"], role="brain")
        return {
            "case_id": case_id,
            "arc_id": case.arc_id,
            "arc_title": case.arc_title,
            "turn_index": case.turn_index,
            "arc_length": case.arc_length,
            "stratum": case.stratum,
            "context_id": _label(raw["context_id"], "context_id", 128),
            "context_summary": context_summary,
            "same_input_context": True,
            "profile_ref": _digest_label(raw["profile_ref"], "profile_ref"),
            "model_ref": _digest_label(raw["model_ref"], "model_ref"),
            "seed": seed,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "hard_flags": list(hard_flags),
            "compatibility": compatibility,
            "brain": brain,
        }

    def _candidate(self, raw: Any, role: str) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != _CANDIDATE_KEYS:
            raise ValueError(f"cognitive A/B {role} candidate keys are invalid")
        mode = _label(raw["mode"], "mode", 16).upper()
        if mode not in COGNITIVE_AB_MODES:
            raise ValueError("cognitive A/B candidate mode is invalid")
        outcome = _label(raw["outcome"], "outcome", 32).upper()
        if outcome not in COGNITIVE_AB_OUTCOMES:
            raise ValueError("cognitive A/B candidate outcome is invalid")
        output = raw["output"]
        if outcome == "COMPLETED" and mode == "SPEAK":
            output = _text(
                output, "candidate output", self.config.max_candidate_output_chars,
            )
        elif output is not None:
            raise ValueError("WAIT or failed cognitive A/B candidate must not contain output")
        return {
            "mode": mode,
            "action_label": _label(raw["action_label"], "action_label", 64),
            "output": output,
            "outcome": outcome,
            "prompt_ref": _digest_label(raw["prompt_ref"], "prompt_ref"),
            "latency_ms": _optional_number(raw["latency_ms"], "latency_ms"),
            "input_tokens": _optional_integer(raw["input_tokens"], "input_tokens"),
            "output_tokens": _optional_integer(raw["output_tokens"], "output_tokens"),
        }

    def _select(
        self, rows: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        matrix: Counter[str] = Counter()
        exclusions: Counter[str] = Counter()
        per_stratum: Counter[str] = Counter()
        eligible: list[dict[str, Any]] = []
        for row in rows:
            old = row["compatibility"]
            new = row["brain"]
            matrix[f'{old["mode"]}->{new["mode"]}'] += 1
            if old["outcome"] != "COMPLETED":
                exclusions[f'compatibility:{old["outcome"]}'] += 1
                continue
            if new["outcome"] != "COMPLETED":
                exclusions[f'brain:{new["outcome"]}'] += 1
                continue
            if old["mode"] == new["mode"] == "WAIT":
                exclusions["both_wait"] += 1
                continue
            eligible.append(row)
            per_stratum[row["stratum"]] += 1
        groups = {
            name: sorted(
                (row for row in eligible if row["stratum"] == name),
                key=lambda row: _stable_order(self.config.seed, row["case_id"]),
            )
            for name in self.config.required_strata
        }
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        arc_groups = {
            arc: sorted(
                (row for row in eligible if row["arc_id"] == arc),
                key=lambda row: _stable_order(self.config.seed, row["case_id"]),
            )
            for arc in self.config.required_arcs
        }
        for arc in self.config.required_arcs:
            if len(arc_groups[arc]) < self.config.minimum_selected_per_arc:
                raise ValueError("cognitive A/B arc has insufficient informative pairs")
            for row in arc_groups[arc][: self.config.minimum_selected_per_arc]:
                selected.append(row)
                selected_ids.add(row["case_id"])
        while len(selected) < self.config.maximum_blind_pairs and any(groups.values()):
            for name in self.config.required_strata:
                while groups[name] and groups[name][0]["case_id"] in selected_ids:
                    groups[name].pop(0)
                if groups[name] and len(selected) < self.config.maximum_blind_pairs:
                    row = groups[name].pop(0)
                    selected.append(row)
                    selected_ids.add(row["case_id"])
        if len(selected) < self.config.minimum_blind_pairs:
            raise ValueError("cognitive A/B has fewer than 30 informative blind pairs")
        arc_order = {arc: index for index, arc in enumerate(self.config.required_arcs)}
        selected.sort(key=lambda row: (arc_order[row["arc_id"]], row["turn_index"]))
        eligible_per_arc = Counter(row["arc_id"] for row in eligible)
        selected_per_arc = Counter(row["arc_id"] for row in selected)
        summary = {
            "total_cases": len(rows),
            "eligible_pairs": len(eligible),
            "selected_pairs": len(selected),
            "both_wait": exclusions.get("both_wait", 0),
            "decision_matrix": dict(sorted(matrix.items())),
            "exclusions": dict(sorted(exclusions.items())),
            "eligible_per_stratum": {
                name: per_stratum.get(name, 0) for name in self.config.required_strata
            },
            "selected_per_stratum": dict(sorted(Counter(
                row["stratum"] for row in selected
            ).items())),
            "eligible_per_arc": {
                arc: eligible_per_arc.get(arc, 0) for arc in self.config.required_arcs
            },
            "selected_per_arc": {
                arc: selected_per_arc.get(arc, 0) for arc in self.config.required_arcs
            },
            "source_clean_gate_required": self.config.strict_source_clean_for_gate,
        }
        return selected, summary

    def _human_comparison(
        self, row: Mapping[str, Any], source_digest: str,
    ) -> dict[str, Any]:
        def candidate(role: str) -> dict[str, Any]:
            value = row[role]
            output = (
                self.config.wait_display_marker
                if value["mode"] == "WAIT" else value["output"]
            )
            metadata = {
                "case_id": row["case_id"],
                "arc_id": row["arc_id"],
                "turn_index": row["turn_index"],
                "stratum": row["stratum"],
                "role": role,
                "mode": value["mode"],
                "action_label": value["action_label"],
                "outcome": value["outcome"],
                "context_id": row["context_id"],
                "source_digest": source_digest,
                "profile_ref": row["profile_ref"],
                "model_ref": row["model_ref"],
                "seed": row["seed"],
            }
            return {
                "build_identity": f'{role}:{source_digest[:24]}',
                "output": output,
                "director_score": 0.0,
                "prompt_ref": value["prompt_ref"],
                "memory_refs": [row["context_id"]],
                "sealed_metadata": metadata,
            }

        return {
            "pair_ref": row["case_id"],
            "context_summary": row["context_summary"],
            "previous": candidate("compatibility"),
            "candidate": candidate("brain"),
        }

    def _record(
        self, kind: str, outcome: str, *, role: str | None = None,
    ) -> None:
        if self._metrics is None:
            return
        method = getattr(self._metrics, f"record_cognitive_ab_{kind}", None)
        if not callable(method):
            return
        try:
            if role is None:
                method(outcome)
            else:
                method(role, outcome)
        except Exception:
            pass


def _stable_order(seed: int, case_id: str) -> str:
    return hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest()


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest_label(value: Any, name: str) -> str:
    result = _label(value, name, 128)
    if len(result) != 64 or any(char not in "0123456789abcdefABCDEF" for char in result):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return result.lower()


def _label(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    result = " ".join(value.split())
    if len(result) > limit:
        raise ValueError(f"{name} exceeds the configured bound")
    return result


def _text(value: Any, name: str, limit: int) -> str:
    result = _label(value, name, limit * 2)
    result = " ".join(str(mask_pii(result) or "").split())
    if not result or len(result) > limit:
        raise ValueError(f"{name} exceeds the configured bound")
    return result


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name, minimum=0)


def _number(value: Any, name: str, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return result


def _optional_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name, minimum=0.0)


def _scenario(raw: Any) -> CognitiveABScenario:
    keys = {
        "kind", "sender_role", "evidence_state", "chat_score", "pulse_state", "urge_ready", "safety_hold",
        "operator_hold", "self_talk_ready", "tone_flags", "amount_vnd",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("cognitive A/B scenario shape is invalid")
    kind = _label(raw["kind"], "scenario.kind", 32)
    if kind not in {"chat", "donation", "proactive"}:
        raise ValueError("cognitive A/B scenario kind is invalid")
    sender_role = _label(raw["sender_role"], "scenario.sender_role", 32)
    if sender_role not in {"viewer", "moderator", "operator"}:
        raise ValueError("cognitive A/B scenario sender_role is invalid")
    evidence_state = _label(raw["evidence_state"], "scenario.evidence_state", 32)
    if evidence_state not in {"fresh", "stale", "missing", "malformed"}:
        raise ValueError("cognitive A/B scenario evidence_state is invalid")
    pulse = _label(raw["pulse_state"], "scenario.pulse_state", 32)
    if pulse not in {"cold", "normal", "lively", "hype_spam"}:
        raise ValueError("cognitive A/B scenario pulse_state is invalid")
    booleans: dict[str, bool] = {}
    for name in ("urge_ready", "safety_hold", "operator_hold", "self_talk_ready"):
        value = raw[name]
        if not isinstance(value, bool):
            raise ValueError(f"cognitive A/B scenario {name} must be a bool")
        booleans[name] = value
    flags_raw = raw["tone_flags"]
    if not isinstance(flags_raw, list):
        raise ValueError("cognitive A/B scenario tone_flags must be a list")
    flags = tuple(_label(value, "tone_flag", 64) for value in flags_raw)
    if len(flags) != len(set(flags)):
        raise ValueError("cognitive A/B scenario tone_flags must be unique")
    amount = _integer(raw["amount_vnd"], "scenario.amount_vnd", minimum=0)
    if kind == "donation" and amount <= 0:
        raise ValueError("cognitive A/B donation scenario requires amount_vnd")
    if kind != "donation" and amount != 0:
        raise ValueError("only donation scenarios may carry amount_vnd")
    return CognitiveABScenario(
        kind=kind,
        sender_role=sender_role,
        evidence_state=evidence_state,
        chat_score=_number(raw["chat_score"], "scenario.chat_score", minimum=0.0),
        pulse_state=pulse,
        urge_ready=booleans["urge_ready"],
        safety_hold=booleans["safety_hold"],
        operator_hold=booleans["operator_hold"],
        self_talk_ready=booleans["self_talk_ready"],
        tone_flags=flags,
        amount_vnd=amount,
    )


def _prior_turns(raw: Any, config: CognitiveABConfig) -> tuple[CognitiveABPriorTurn, ...]:
    if not isinstance(raw, list) or len(raw) > config.max_prior_turns:
        raise ValueError("cognitive A/B prior_turns must be a bounded list")
    turns: list[CognitiveABPriorTurn] = []
    for value in raw:
        if not isinstance(value, dict) or set(value) != {"role", "text"}:
            raise ValueError("cognitive A/B prior turn shape is invalid")
        role = _label(value["role"], "prior_turn.role", 16)
        if role not in {"viewer", "mai", "operator"}:
            raise ValueError("cognitive A/B prior turn role is invalid")
        turns.append(CognitiveABPriorTurn(
            role=role,
            text=_text(value["text"], "prior_turn.text", 800),
        ))
    return tuple(turns)


def _validate_story_arcs(
    cases: list[CognitiveABCase], config: CognitiveABConfig,
) -> None:
    groups = {
        arc: [case for case in cases if case.arc_id == arc]
        for arc in config.required_arcs
    }
    for arc, values in groups.items():
        if not values:
            raise ValueError(f"cognitive A/B corpus is missing arc {arc}")
        titles = {case.arc_title for case in values}
        lengths = {case.arc_length for case in values}
        if len(titles) != 1 or lengths != {len(values)}:
            raise ValueError("cognitive A/B arc title/length is inconsistent")
        indices = sorted(case.turn_index for case in values)
        if indices != list(range(1, len(values) + 1)):
            raise ValueError("cognitive A/B arc turn order is incomplete")
