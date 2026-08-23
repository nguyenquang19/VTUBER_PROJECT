"""Strict proposal-only contracts for the Cognitive Brain boundary.

MCB-1 defines immutable values only.  Nothing in this module calls an LLM,
executes an action, mutates domain state, or owns a background task.
"""
from __future__ import annotations

import json
import math
import re
from abc import abstractmethod
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from interfaces.base import Service


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CognitiveEvidenceSource(str, Enum):
    CHAT = "CHAT"
    THREAD = "THREAD"
    GOAL = "GOAL"
    WORLD = "WORLD"
    SELF = "SELF"
    CAPABILITY = "CAPABILITY"
    ENVIRONMENT = "ENVIRONMENT"
    OPERATOR = "OPERATOR"


class CognitiveMode(str, Enum):
    WAIT = "WAIT"
    SPEAK = "SPEAK"
    PROPOSE_ACTION = "PROPOSE_ACTION"


class CognitiveUncertainty(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class FocusOrigin(str, Enum):
    CHAT = "CHAT"
    SELF = "SELF"
    MEMORY = "MEMORY"
    GOAL = "GOAL"
    ENVIRONMENT = "ENVIRONMENT"
    OPERATOR = "OPERATOR"


class FocusOperation(str, Enum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    KEEP = "KEEP"
    CLEAR = "CLEAR"


class MemoryKind(str, Enum):
    EPISODIC = "EPISODIC"
    PREFERENCE = "PREFERENCE"
    RELATIONSHIP_NOTE = "RELATIONSHIP_NOTE"
    SELF_SUMMARY = "SELF_SUMMARY"


class MemoryScope(str, Enum):
    SESSION = "SESSION"
    VIEWER = "VIEWER"
    SELF = "SELF"


class MemoryClaimBasis(str, Enum):
    OBSERVED_INPUT = "OBSERVED_INPUT"
    DELIVERED_SPEECH = "DELIVERED_SPEECH"
    VERIFIED_ACTION = "VERIFIED_ACTION"
    SELF_SUMMARY = "SELF_SUMMARY"


class MemoryRetentionClass(str, Enum):
    TURN = "TURN"
    SESSION = "SESSION"
    PERSISTENT_CANDIDATE = "PERSISTENT_CANDIDATE"


@dataclass(frozen=True)
class CognitionConfig:
    """Strict immutable validation values loaded from ``config/cognition.yaml``."""

    schema_version: int
    rollout_mode: str
    max_id_chars: int
    max_label_chars: int
    max_text_chars: int
    max_speech_chars: int
    max_attention_items: int
    max_memory_items: int
    max_recent_delivered_speech: int
    max_available_actions: int
    max_evidence_refs: int
    max_reason_codes: int
    max_memory_proposals: int
    max_unresolved_items: int
    max_focus_claims: int
    max_action_argument_items: int
    max_action_argument_chars: int
    focus_ttl_seconds: int
    source_failure_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    speech_source_modes: tuple[str, ...]

    _KEYS = frozenset({
        "schema_version", "rollout_mode", "max_id_chars", "max_label_chars",
        "max_text_chars", "max_speech_chars", "max_attention_items",
        "max_memory_items", "max_recent_delivered_speech",
        "max_available_actions", "max_evidence_refs", "max_reason_codes",
        "max_memory_proposals", "max_unresolved_items", "max_focus_claims",
        "max_action_argument_items", "max_action_argument_chars",
        "focus_ttl_seconds", "source_failure_codes", "reason_codes",
        "speech_source_modes",
    })

    def __post_init__(self) -> None:
        if _positive_int(self.schema_version, "schema_version") != 1:
            raise ValueError("schema_version must be 1")
        if self.rollout_mode != "disabled":
            raise ValueError("rollout_mode must be disabled in MCB-1")
        integer_fields = (
            "max_id_chars", "max_label_chars", "max_text_chars",
            "max_speech_chars", "max_attention_items", "max_memory_items",
            "max_recent_delivered_speech", "max_available_actions",
            "max_evidence_refs", "max_reason_codes", "max_memory_proposals",
            "max_unresolved_items", "max_focus_claims",
            "max_action_argument_items", "max_action_argument_chars",
            "focus_ttl_seconds",
        )
        for name in integer_fields:
            _positive_int(getattr(self, name), name)
        if self.max_speech_chars > self.max_text_chars:
            raise ValueError("max_speech_chars must not exceed max_text_chars")
        allowlist_caps = {
            "source_failure_codes": self.max_evidence_refs,
            "reason_codes": self.max_reason_codes,
            "speech_source_modes": self.max_reason_codes,
        }
        for name, max_items in allowlist_caps.items():
            values = _string_tuple(
                getattr(self, name), name, max_items=max_items,
                max_chars=self.max_label_chars, unique=True, non_empty=True,
            )
            object.__setattr__(self, name, values)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CognitionConfig:
        if not isinstance(value, Mapping):
            raise ValueError("cognition config must be a mapping")
        keys = set(value)
        if keys != cls._KEYS:
            missing = sorted(cls._KEYS - keys)
            unknown = sorted(keys - cls._KEYS)
            raise ValueError(
                f"cognition config keys mismatch: missing={missing}, unknown={unknown}"
            )
        payload = dict(value)
        for name in ("source_failure_codes", "reason_codes", "speech_source_modes"):
            raw = payload[name]
            if not isinstance(raw, list):
                raise ValueError(f"{name} must be a YAML list")
            payload[name] = tuple(raw)
        return cls(**payload)


@dataclass(frozen=True)
class CognitiveHardState:
    config: InitVar[CognitionConfig]
    schema_version: int
    emergency: bool
    operator_hold: bool
    safety_hold: bool
    permission_hold: bool
    transaction_conflict: bool
    critical_state: bool
    source_failure_codes: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        for name in (
            "emergency", "operator_hold", "safety_hold", "permission_hold",
            "transaction_conflict", "critical_state",
        ):
            _strict_bool(getattr(self, name), name)
        failures = _string_tuple(
            self.source_failure_codes, "source_failure_codes",
            max_items=config.max_evidence_refs, max_chars=config.max_label_chars,
            unique=True,
        )
        if any(item not in config.source_failure_codes for item in failures):
            raise ValueError("source_failure_codes contains an unsupported code")
        object.__setattr__(self, "source_failure_codes", failures)


@dataclass(frozen=True)
class CognitiveEvidenceItem:
    config: InitVar[CognitionConfig]
    schema_version: int
    evidence_id: str
    source: CognitiveEvidenceSource
    summary: str
    provenance_refs: tuple[str, ...]
    observed_at: datetime
    expires_at: datetime | None

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        object.__setattr__(self, "evidence_id", _identifier(
            self.evidence_id, "evidence_id", config,
        ))
        _enum(self.source, CognitiveEvidenceSource, "source")
        object.__setattr__(self, "summary", _text(
            self.summary, "summary", config.max_text_chars,
        ))
        object.__setattr__(self, "provenance_refs", _refs(
            self.provenance_refs, "provenance_refs", config, non_empty=True,
        ))
        observed = _utc(self.observed_at, "observed_at")
        expires = _optional_utc(self.expires_at, "expires_at")
        if expires is not None and expires <= observed:
            raise ValueError("expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)


@dataclass(frozen=True)
class CognitiveConversationState:
    config: InitVar[CognitionConfig]
    schema_version: int
    topic: str | None
    thread_ref: str | None
    goal_ref: str | None
    intention_ref: str | None
    summary: str | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        for name in ("topic", "summary"):
            object.__setattr__(self, name, _optional_text(
                getattr(self, name), name, config.max_text_chars,
            ))
        for name in ("thread_ref", "goal_ref", "intention_ref"):
            object.__setattr__(self, name, _optional_identifier(
                getattr(self, name), name, config,
            ))
        refs = _refs(self.evidence_refs, "evidence_refs", config)
        object.__setattr__(self, "evidence_refs", refs)
        if not any((self.topic, self.thread_ref, self.goal_ref, self.intention_ref,
                    self.summary, refs)):
            raise ValueError("conversation_state must not be empty")


@dataclass(frozen=True)
class CognitiveMemoryItem:
    config: InitVar[CognitionConfig]
    schema_version: int
    memory_ref: str
    kind: MemoryKind
    summary: str
    scope: MemoryScope
    viewer_ref: str | None
    provenance_refs: tuple[str, ...]
    observed_at: datetime
    expires_at: datetime | None
    confidence: float

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        object.__setattr__(self, "memory_ref", _identifier(
            self.memory_ref, "memory_ref", config,
        ))
        _enum(self.kind, MemoryKind, "kind")
        _enum(self.scope, MemoryScope, "scope")
        object.__setattr__(self, "summary", _text(
            self.summary, "summary", config.max_text_chars,
        ))
        viewer = _optional_identifier(self.viewer_ref, "viewer_ref", config)
        _validate_scope(self.scope, viewer)
        object.__setattr__(self, "viewer_ref", viewer)
        object.__setattr__(self, "provenance_refs", _refs(
            self.provenance_refs, "provenance_refs", config, non_empty=True,
        ))
        observed = _utc(self.observed_at, "observed_at")
        expires = _optional_utc(self.expires_at, "expires_at")
        if expires is not None and expires <= observed:
            raise ValueError("expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "confidence", _unit_interval(
            self.confidence, "confidence",
        ))


@dataclass(frozen=True)
class CognitiveSpeechSummary:
    config: InitVar[CognitionConfig]
    schema_version: int
    delivery_id: str
    speech_text: str
    delivered_at: datetime
    source_mode: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        object.__setattr__(self, "delivery_id", _identifier(
            self.delivery_id, "delivery_id", config,
        ))
        object.__setattr__(self, "speech_text", _text(
            self.speech_text, "speech_text", config.max_speech_chars,
        ))
        object.__setattr__(self, "delivered_at", _utc(
            self.delivered_at, "delivered_at",
        ))
        mode = _text(self.source_mode, "source_mode", config.max_label_chars)
        if mode not in config.speech_source_modes:
            raise ValueError("source_mode is unsupported")
        object.__setattr__(self, "source_mode", mode)
        object.__setattr__(self, "evidence_refs", _refs(
            self.evidence_refs, "evidence_refs", config, non_empty=True,
        ))


@dataclass(frozen=True)
class CognitiveActionEnvelope:
    config: InitVar[CognitionConfig]
    schema_version: int
    capability_id: str
    action_type: str
    description: str
    argument_schema: Mapping[str, Any]
    target_required: bool
    allows_speech: bool
    availability_ref: str
    checked_at: datetime
    evidence_refs: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        for name in ("capability_id", "availability_ref"):
            object.__setattr__(self, name, _identifier(
                getattr(self, name), name, config,
            ))
        object.__setattr__(self, "action_type", _text(
            self.action_type, "action_type", config.max_label_chars,
        ))
        object.__setattr__(self, "description", _text(
            self.description, "description", config.max_text_chars,
        ))
        schema = _bounded_mapping(self.argument_schema, "argument_schema", config)
        _validate_schema_shape(schema)
        object.__setattr__(self, "argument_schema", schema)
        _strict_bool(self.target_required, "target_required")
        _strict_bool(self.allows_speech, "allows_speech")
        object.__setattr__(self, "checked_at", _utc(self.checked_at, "checked_at"))
        object.__setattr__(self, "evidence_refs", _refs(
            self.evidence_refs, "evidence_refs", config, non_empty=True,
        ))


@dataclass(frozen=True)
class CognitiveContext:
    config: InitVar[CognitionConfig]
    schema_version: int
    context_id: str
    created_at: datetime
    session_id: str
    world_snapshot_id: str
    self_snapshot_id: str
    capability_snapshot_id: str
    focus_snapshot_id: str | None
    operator_state: CognitiveHardState
    available_modes: tuple[CognitiveMode, ...]
    available_actions: tuple[CognitiveActionEnvelope, ...]
    chat_digest: CognitiveEvidenceItem | None
    attention_items: tuple[CognitiveEvidenceItem, ...]
    conversation_state: CognitiveConversationState
    memory_items: tuple[CognitiveMemoryItem, ...]
    recent_delivered_speech: tuple[CognitiveSpeechSummary, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        context_id = _text(self.context_id, "context_id", config.max_id_chars)
        if _SHA256_RE.fullmatch(context_id) is None:
            raise ValueError("context_id must be a lowercase SHA-256 hash")
        object.__setattr__(self, "context_id", context_id)
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        for name in (
            "session_id", "world_snapshot_id", "self_snapshot_id",
            "capability_snapshot_id",
        ):
            object.__setattr__(self, name, _identifier(
                getattr(self, name), name, config,
            ))
        object.__setattr__(self, "focus_snapshot_id", _optional_identifier(
            self.focus_snapshot_id, "focus_snapshot_id", config,
        ))
        _instance(self.operator_state, CognitiveHardState, "operator_state")
        modes = _typed_tuple(self.available_modes, CognitiveMode, "available_modes", 3)
        if not modes or len(set(modes)) != len(modes):
            raise ValueError("available_modes must be non-empty and unique")
        object.__setattr__(self, "available_modes", modes)
        object.__setattr__(self, "available_actions", _typed_tuple(
            self.available_actions, CognitiveActionEnvelope, "available_actions",
            config.max_available_actions,
        ))
        if self.chat_digest is not None:
            _instance(self.chat_digest, CognitiveEvidenceItem, "chat_digest")
            if self.chat_digest.source is not CognitiveEvidenceSource.CHAT:
                raise ValueError("chat_digest source must be CHAT")
        object.__setattr__(self, "attention_items", _typed_tuple(
            self.attention_items, CognitiveEvidenceItem, "attention_items",
            config.max_attention_items,
        ))
        _instance(self.conversation_state, CognitiveConversationState, "conversation_state")
        object.__setattr__(self, "memory_items", _typed_tuple(
            self.memory_items, CognitiveMemoryItem, "memory_items", config.max_memory_items,
        ))
        object.__setattr__(self, "recent_delivered_speech", _typed_tuple(
            self.recent_delivered_speech, CognitiveSpeechSummary,
            "recent_delivered_speech", config.max_recent_delivered_speech,
        ))
        evidence_ids = [item.evidence_id for item in self.attention_items]
        if self.chat_digest is not None:
            evidence_ids.append(self.chat_digest.evidence_id)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("context evidence_id values must be unique")
        action_keys = [(item.capability_id, item.action_type) for item in self.available_actions]
        if len(action_keys) != len(set(action_keys)):
            raise ValueError("available_actions must be unique by capability_id/action_type")
        expiring_items = list(self.attention_items) + list(self.memory_items)
        if self.chat_digest is not None:
            expiring_items.append(self.chat_digest)
        if any(
            item.expires_at is not None and item.expires_at <= self.created_at
            for item in expiring_items
        ):
            raise ValueError("CognitiveContext must omit expired evidence or memory")


@dataclass(frozen=True)
class FocusState:
    config: InitVar[CognitionConfig]
    schema_version: int
    focus_id: str
    topic: str
    stance: str | None
    unresolved_items: tuple[str, ...]
    claims_delivered: tuple[str, ...]
    continuation_pressure: float
    saturation: float
    origin: FocusOrigin
    evidence_refs: tuple[str, ...]
    born_at: datetime
    updated_at: datetime
    expires_at: datetime

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        object.__setattr__(self, "focus_id", _identifier(self.focus_id, "focus_id", config))
        object.__setattr__(self, "topic", _text(self.topic, "topic", config.max_text_chars))
        object.__setattr__(self, "stance", _optional_text(
            self.stance, "stance", config.max_text_chars,
        ))
        object.__setattr__(self, "unresolved_items", _string_tuple(
            self.unresolved_items, "unresolved_items",
            max_items=config.max_unresolved_items, max_chars=config.max_text_chars,
            unique=True,
        ))
        object.__setattr__(self, "claims_delivered", _string_tuple(
            self.claims_delivered, "claims_delivered",
            max_items=config.max_focus_claims, max_chars=config.max_text_chars,
            unique=True,
        ))
        object.__setattr__(self, "continuation_pressure", _unit_interval(
            self.continuation_pressure, "continuation_pressure",
        ))
        object.__setattr__(self, "saturation", _unit_interval(self.saturation, "saturation"))
        _enum(self.origin, FocusOrigin, "origin")
        object.__setattr__(self, "evidence_refs", _refs(
            self.evidence_refs, "evidence_refs", config, non_empty=True,
        ))
        born = _utc(self.born_at, "born_at")
        updated = _utc(self.updated_at, "updated_at")
        expires = _utc(self.expires_at, "expires_at")
        if not born <= updated < expires:
            raise ValueError("FocusState timestamps are out of order")
        if (expires - born).total_seconds() > config.focus_ttl_seconds:
            raise ValueError("FocusState exceeds focus_ttl_seconds")
        object.__setattr__(self, "born_at", born)
        object.__setattr__(self, "updated_at", updated)
        object.__setattr__(self, "expires_at", expires)


@dataclass(frozen=True)
class FocusProposal:
    config: InitVar[CognitionConfig]
    schema_version: int
    proposal_id: str
    context_id: str
    operation: FocusOperation
    base_focus_id: str | None
    topic: str | None
    stance: str | None
    unresolved_items: tuple[str, ...]
    continuation_pressure: float | None
    saturation: float | None
    origin: FocusOrigin | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        object.__setattr__(self, "proposal_id", _identifier(
            self.proposal_id, "proposal_id", config,
        ))
        object.__setattr__(self, "context_id", _context_hash(self.context_id, config))
        _enum(self.operation, FocusOperation, "operation")
        object.__setattr__(self, "base_focus_id", _optional_identifier(
            self.base_focus_id, "base_focus_id", config,
        ))
        object.__setattr__(self, "topic", _optional_text(
            self.topic, "topic", config.max_text_chars,
        ))
        object.__setattr__(self, "stance", _optional_text(
            self.stance, "stance", config.max_text_chars,
        ))
        object.__setattr__(self, "unresolved_items", _string_tuple(
            self.unresolved_items, "unresolved_items",
            max_items=config.max_unresolved_items, max_chars=config.max_text_chars,
            unique=True,
        ))
        if self.continuation_pressure is not None:
            object.__setattr__(self, "continuation_pressure", _unit_interval(
                self.continuation_pressure, "continuation_pressure",
            ))
        if self.saturation is not None:
            object.__setattr__(self, "saturation", _unit_interval(
                self.saturation, "saturation",
            ))
        if self.origin is not None:
            _enum(self.origin, FocusOrigin, "origin")
        refs = _refs(self.evidence_refs, "evidence_refs", config)
        object.__setattr__(self, "evidence_refs", refs)
        mutation_values = (
            self.topic, self.stance, self.unresolved_items,
            self.continuation_pressure, self.saturation, self.origin,
        )
        if self.operation is FocusOperation.CREATE:
            if self.base_focus_id is not None:
                raise ValueError("CREATE requires base_focus_id=None")
            _require_focus_state_fields(self, refs)
        elif self.operation is FocusOperation.UPDATE:
            if self.base_focus_id is None:
                raise ValueError("UPDATE requires base_focus_id")
            _require_focus_state_fields(self, refs)
        elif self.operation is FocusOperation.KEEP:
            if self.base_focus_id is None or any(mutation_values) or refs:
                raise ValueError("KEEP requires a base and no mutation/evidence")
        elif self.operation is FocusOperation.CLEAR:
            if self.base_focus_id is None or any(mutation_values) or not refs:
                raise ValueError("CLEAR requires a base, clear evidence, and no mutation")


@dataclass(frozen=True)
class MemoryProposal:
    config: InitVar[CognitionConfig]
    schema_version: int
    proposal_id: str
    context_id: str
    kind: MemoryKind
    content: str
    scope: MemoryScope
    viewer_ref: str | None
    claim_basis: MemoryClaimBasis
    provenance_refs: tuple[str, ...]
    outcome_ref: str | None
    confidence: float
    retention_class: MemoryRetentionClass

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        object.__setattr__(self, "proposal_id", _identifier(
            self.proposal_id, "proposal_id", config,
        ))
        object.__setattr__(self, "context_id", _context_hash(self.context_id, config))
        _enum(self.kind, MemoryKind, "kind")
        _enum(self.scope, MemoryScope, "scope")
        _enum(self.claim_basis, MemoryClaimBasis, "claim_basis")
        _enum(self.retention_class, MemoryRetentionClass, "retention_class")
        object.__setattr__(self, "content", _text(
            self.content, "content", config.max_text_chars,
        ))
        viewer = _optional_identifier(self.viewer_ref, "viewer_ref", config)
        _validate_scope(self.scope, viewer)
        object.__setattr__(self, "viewer_ref", viewer)
        object.__setattr__(self, "provenance_refs", _refs(
            self.provenance_refs, "provenance_refs", config, non_empty=True,
        ))
        outcome = _optional_identifier(self.outcome_ref, "outcome_ref", config)
        if self.claim_basis in {
            MemoryClaimBasis.DELIVERED_SPEECH, MemoryClaimBasis.VERIFIED_ACTION,
        }:
            if outcome is None:
                raise ValueError("delivered/verified memory requires outcome_ref")
        elif outcome is not None:
            raise ValueError("observed/self-summary memory requires outcome_ref=None")
        object.__setattr__(self, "outcome_ref", outcome)
        object.__setattr__(self, "confidence", _unit_interval(
            self.confidence, "confidence",
        ))


@dataclass(frozen=True)
class CognitiveActionProposal:
    config: InitVar[CognitionConfig]
    schema_version: int
    proposal_id: str
    context_id: str
    capability_id: str
    action_type: str
    target_ref: str | None
    arguments: Mapping[str, Any]
    intention_id: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig) -> None:
        _schema(self.schema_version, config)
        for name in ("proposal_id", "capability_id", "intention_id"):
            object.__setattr__(self, name, _identifier(
                getattr(self, name), name, config,
            ))
        object.__setattr__(self, "context_id", _context_hash(self.context_id, config))
        object.__setattr__(self, "action_type", _text(
            self.action_type, "action_type", config.max_label_chars,
        ))
        object.__setattr__(self, "target_ref", _optional_identifier(
            self.target_ref, "target_ref", config,
        ))
        object.__setattr__(self, "arguments", _bounded_mapping(
            self.arguments, "arguments", config,
        ))
        object.__setattr__(self, "evidence_refs", _refs(
            self.evidence_refs, "evidence_refs", config, non_empty=True,
        ))


@dataclass(frozen=True)
class CognitiveTurn:
    config: InitVar[CognitionConfig]
    context: InitVar[CognitiveContext]
    schema_version: int
    turn_id: str
    context_id: str
    mode: CognitiveMode
    attention_target_id: str | None
    intent: str | None
    speech_text: str | None
    action_proposal: CognitiveActionProposal | None
    focus_proposal: FocusProposal | None
    memory_proposals: tuple[MemoryProposal, ...]
    evidence_refs: tuple[str, ...]
    uncertainty: CognitiveUncertainty
    reason_codes: tuple[str, ...]

    def __post_init__(self, config: CognitionConfig, context: CognitiveContext) -> None:
        _schema(self.schema_version, config)
        _instance(context, CognitiveContext, "context")
        object.__setattr__(self, "turn_id", _identifier(self.turn_id, "turn_id", config))
        context_id = _context_hash(self.context_id, config)
        if context_id != context.context_id:
            raise ValueError("CognitiveTurn context_id is stale or mismatched")
        object.__setattr__(self, "context_id", context_id)
        _enum(self.mode, CognitiveMode, "mode")
        if self.mode not in context.available_modes:
            raise ValueError("mode is not available in current context")
        target = _optional_identifier(self.attention_target_id, "attention_target_id", config)
        evidence_items = list(context.attention_items)
        if context.chat_digest is not None:
            evidence_items.append(context.chat_digest)
        evidence_ids = {item.evidence_id for item in evidence_items}
        if target is not None and target not in evidence_ids:
            raise ValueError("attention_target_id is not current evidence")
        object.__setattr__(self, "attention_target_id", target)
        object.__setattr__(self, "intent", _optional_text(
            self.intent, "intent", config.max_text_chars,
        ))
        object.__setattr__(self, "speech_text", _optional_text(
            self.speech_text, "speech_text", config.max_speech_chars,
        ))
        if self.action_proposal is not None:
            _instance(self.action_proposal, CognitiveActionProposal, "action_proposal")
        if self.focus_proposal is not None:
            _instance(self.focus_proposal, FocusProposal, "focus_proposal")
        memories = _typed_tuple(
            self.memory_proposals, MemoryProposal, "memory_proposals",
            config.max_memory_proposals,
        )
        object.__setattr__(self, "memory_proposals", memories)
        refs = _refs(self.evidence_refs, "evidence_refs", config)
        object.__setattr__(self, "evidence_refs", refs)
        _enum(self.uncertainty, CognitiveUncertainty, "uncertainty")
        reasons = _string_tuple(
            self.reason_codes, "reason_codes", max_items=config.max_reason_codes,
            max_chars=config.max_label_chars, unique=True, non_empty=True,
        )
        if any(reason not in config.reason_codes for reason in reasons):
            raise ValueError("reason_codes contains an unsupported code")
        object.__setattr__(self, "reason_codes", reasons)

        current_refs = _context_reference_set(context)
        if any(ref not in current_refs for ref in refs):
            raise ValueError("CognitiveTurn evidence_refs contains a stale reference")
        if self.focus_proposal is not None:
            _validate_focus_against_context(self.focus_proposal, context, current_refs)
        for proposal in memories:
            if proposal.context_id != context.context_id:
                raise ValueError("MemoryProposal context_id is stale or mismatched")
            if any(ref not in current_refs for ref in proposal.provenance_refs):
                raise ValueError("MemoryProposal provenance contains a stale reference")

        if self.mode is CognitiveMode.WAIT:
            if any((self.intent, self.speech_text, self.action_proposal,
                    self.focus_proposal, memories)):
                raise ValueError("WAIT must not contain intent, speech, or proposals")
        elif self.mode is CognitiveMode.SPEAK:
            if self.intent is None or self.speech_text is None or self.action_proposal is not None:
                raise ValueError("SPEAK requires intent/speech and forbids action")
        elif self.mode is CognitiveMode.PROPOSE_ACTION:
            if self.intent is None or self.action_proposal is None:
                raise ValueError("PROPOSE_ACTION requires intent and action")
            envelope = _matching_envelope(self.action_proposal, context)
            if self.speech_text is not None and not envelope.allows_speech:
                raise ValueError("current action envelope does not allow speech")


class CognitiveBrainService(Service):
    """Proposal-only Brain boundary; the kernel retains all mutation authority."""

    @abstractmethod
    async def propose(self, context: CognitiveContext) -> CognitiveTurn:
        """Return one proposal without executing or committing any side effect."""


def _schema(value: Any, config: CognitionConfig) -> None:
    if _positive_int(value, "schema_version") != config.schema_version:
        raise ValueError("schema_version does not match cognition config")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a bool")
    return value


def _text(value: Any, name: str, max_chars: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{name} must be a trimmed non-empty string")
    if len(value) > max_chars:
        raise ValueError(f"{name} exceeds configured bound")
    return value


def _optional_text(value: Any, name: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, max_chars)


def _identifier(value: Any, name: str, config: CognitionConfig) -> str:
    return _text(value, name, config.max_id_chars)


def _optional_identifier(value: Any, name: str, config: CognitionConfig) -> str | None:
    if value is None:
        return None
    return _identifier(value, name, config)


def _context_hash(value: Any, config: CognitionConfig) -> str:
    result = _text(value, "context_id", config.max_id_chars)
    if _SHA256_RE.fullmatch(result) is None:
        raise ValueError("context_id must be a lowercase SHA-256 hash")
    return result


def _string_tuple(
    value: Any,
    name: str,
    *,
    max_items: int,
    max_chars: int,
    unique: bool = False,
    non_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    if len(value) > max_items:
        raise ValueError(f"{name} exceeds configured capacity")
    result = tuple(_text(item, name, max_chars) for item in value)
    if non_empty and not result:
        raise ValueError(f"{name} must not be empty")
    if unique and len(result) != len(set(result)):
        raise ValueError(f"{name} must be unique")
    return result


def _refs(
    value: Any, name: str, config: CognitionConfig, *, non_empty: bool = False,
) -> tuple[str, ...]:
    return _string_tuple(
        value, name, max_items=config.max_evidence_refs,
        max_chars=config.max_id_chars, unique=True, non_empty=non_empty,
    )


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _optional_utc(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    return _utc(value, name)


def _unit_interval(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be within [0, 1]")
    return result


def _enum(value: Any, enum_type: type[Enum], name: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{name} must be {enum_type.__name__}")


def _instance(value: Any, expected: type[Any], name: str) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"{name} must be {expected.__name__}")


def _typed_tuple(value: Any, expected: type[Any], name: str, max_items: int) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    if len(value) > max_items or any(not isinstance(item, expected) for item in value):
        raise ValueError(f"{name} has invalid type or capacity")
    return value


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("mapping contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or raw_key != raw_key.strip() or not raw_key:
                raise ValueError("mapping keys must be trimmed non-empty strings")
            if raw_key in frozen:
                raise ValueError("mapping keys must be unique")
            frozen[raw_key] = _freeze_json(raw_value)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("mapping contains an unsupported JSON value")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _bounded_mapping(
    value: Any, name: str, config: CognitionConfig,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    frozen = _freeze_json(value)
    if _contains_forbidden_mapping_key(frozen):
        raise ValueError(f"{name} contains a forbidden sensitive/internal key")

    def item_count(item: Any) -> int:
        if isinstance(item, Mapping):
            return len(item) + sum(item_count(child) for child in item.values())
        if isinstance(item, tuple):
            return sum(item_count(child) for child in item)
        return 0

    if item_count(frozen) > config.max_action_argument_items:
        raise ValueError(f"{name} exceeds configured item capacity")
    encoded = json.dumps(
        _plain_json(frozen), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    if len(encoded) > config.max_action_argument_chars:
        raise ValueError(f"{name} exceeds configured serialized bound")
    return frozen


def _contains_forbidden_mapping_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if any(marker in normalized for marker in (
                "token", "secret", "password", "credential", "authorization",
                "api_key", "callback", "executor", "verifier", "permission",
            )):
                return True
            if _contains_forbidden_mapping_key(item):
                return True
    elif isinstance(value, tuple):
        return any(_contains_forbidden_mapping_key(item) for item in value)
    return False


def _validate_scope(scope: MemoryScope, viewer_ref: str | None) -> None:
    if scope is MemoryScope.VIEWER and viewer_ref is None:
        raise ValueError("VIEWER scope requires viewer_ref")
    if scope is not MemoryScope.VIEWER and viewer_ref is not None:
        raise ValueError("SESSION/SELF scope requires viewer_ref=None")


def _require_focus_state_fields(proposal: FocusProposal, refs: tuple[str, ...]) -> None:
    if (
        proposal.topic is None
        or proposal.continuation_pressure is None
        or proposal.saturation is None
        or proposal.origin is None
        or not refs
    ):
        raise ValueError("CREATE/UPDATE requires complete proposal-owned state and evidence")


def _context_reference_set(context: CognitiveContext) -> set[str]:
    refs = set(context.conversation_state.evidence_refs)
    items = list(context.attention_items)
    if context.chat_digest is not None:
        items.append(context.chat_digest)
    for item in items:
        refs.add(item.evidence_id)
        refs.update(item.provenance_refs)
    for item in context.memory_items:
        refs.add(item.memory_ref)
        refs.update(item.provenance_refs)
    for item in context.recent_delivered_speech:
        refs.add(item.delivery_id)
        refs.update(item.evidence_refs)
    for item in context.available_actions:
        refs.add(item.availability_ref)
        refs.update(item.evidence_refs)
    return refs


def _validate_focus_against_context(
    proposal: FocusProposal, context: CognitiveContext, current_refs: set[str],
) -> None:
    if proposal.context_id != context.context_id:
        raise ValueError("FocusProposal context_id is stale or mismatched")
    active = context.focus_snapshot_id
    if active is None:
        if proposal.operation is not FocusOperation.CREATE:
            raise ValueError("only CREATE is valid without an active Focus")
    else:
        if proposal.operation is FocusOperation.CREATE:
            raise ValueError("CREATE is invalid with an active Focus")
        if proposal.base_focus_id != active:
            raise ValueError("FocusProposal base_focus_id is stale or mismatched")
    if any(ref not in current_refs for ref in proposal.evidence_refs):
        raise ValueError("FocusProposal evidence contains a stale reference")


def _matching_envelope(
    proposal: CognitiveActionProposal, context: CognitiveContext,
) -> CognitiveActionEnvelope:
    if proposal.context_id != context.context_id:
        raise ValueError("CognitiveActionProposal context_id is stale or mismatched")
    matches = [
        item for item in context.available_actions
        if item.capability_id == proposal.capability_id
        and item.action_type == proposal.action_type
    ]
    if len(matches) != 1:
        raise ValueError("action is not present in current capability envelope")
    envelope = matches[0]
    if envelope.target_required and proposal.target_ref is None:
        raise ValueError("current action envelope requires target_ref")
    _validate_arguments_against_schema(proposal.arguments, envelope.argument_schema)
    current_refs = _context_reference_set(context)
    if any(ref not in current_refs for ref in proposal.evidence_refs):
        raise ValueError("CognitiveActionProposal evidence contains a stale reference")
    return envelope


def _validate_arguments_against_schema(
    arguments: Mapping[str, Any], schema: Mapping[str, Any],
) -> None:
    if set(arguments) != set(schema):
        raise ValueError("arguments do not match current capability schema keys")
    type_checks = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ),
        "boolean": lambda value: isinstance(value, bool),
        "object": lambda value: isinstance(value, Mapping),
        "array": lambda value: isinstance(value, tuple),
    }
    for key, expected in schema.items():
        if not isinstance(expected, str) or expected not in type_checks:
            raise ValueError("current capability schema contains an unsupported type")
        if not type_checks[expected](arguments[key]):
            raise ValueError(f"arguments.{key} does not match capability schema")


def _validate_schema_shape(schema: Mapping[str, Any]) -> None:
    supported = {"string", "integer", "number", "boolean", "object", "array"}
    if any(not isinstance(value, str) or value not in supported for value in schema.values()):
        raise ValueError("argument_schema contains an unsupported type")
