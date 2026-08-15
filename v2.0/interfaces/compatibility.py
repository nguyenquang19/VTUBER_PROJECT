"""Immutable V2 compatibility contracts; intentionally not wired into runtime in Phase 1."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _required(value: str, *, field_name: str) -> str:
    clean = str(value).strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


def _confidence(value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("confidence must be finite and within [0, 1]")
    return number


def _finite(value: float, *, field_name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _frozen_strings(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    result = tuple(_required(value, field_name=field_name) for value in values)
    return result


def _freeze(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("contract payload must not contain non-finite floats")
        return value
    if isinstance(value, datetime):
        return _as_utc(value, field_name="payload timestamp")
    if isinstance(value, Enum):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            frozen[_required(str(key), field_name="mapping key")] = _freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise ValueError(f"contract payload type is not JSON-safe: {type(value).__name__}")


def _frozen_mapping(value: Mapping[str, Any], *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return _freeze(value)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    raise TypeError(f"contract value is not JSON-serializable: {type(value).__name__}")


def _bounded_payload(
    payload: Mapping[str, Any], *, max_payload_items: int, max_payload_chars: int,
) -> Mapping[str, Any]:
    if max_payload_items <= 0 or max_payload_chars <= 0:
        raise ValueError("payload limits must be positive")
    frozen = _frozen_mapping(payload, field_name="payload")

    def count_items(value: Any) -> int:
        if isinstance(value, Mapping):
            return len(value) + sum(count_items(item) for item in value.values())
        if isinstance(value, tuple):
            return sum(count_items(item) for item in value)
        return 0

    if count_items(frozen) > max_payload_items:
        raise ValueError("payload exceeds max_payload_items")
    encoded = json.dumps(_json_value(frozen), ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_payload_chars:
        raise ValueError("payload exceeds max_payload_chars")
    return frozen


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in {"viewer_id", "user_id", "user_name", "author_id", "author_name", "display_name"}:
        return True
    return any(marker in normalized for marker in (
        "token", "secret", "password", "credential", "authorization", "api_key",
    ))


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                raise ValueError("perception payload contains a sensitive key")
            _reject_sensitive_keys(item)
    elif isinstance(value, tuple):
        for item in value:
            _reject_sensitive_keys(item)


def _sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for raw_key, value in metadata.items():
        key = str(raw_key).strip()
        if not key or _is_sensitive_key(key):
            continue
        if isinstance(value, Mapping):
            sanitized[key] = _sanitize_metadata(value)
        else:
            sanitized[key] = value
    return sanitized


@dataclass(frozen=True)
class EventProvenance:
    producer: str
    source_event_id: str | None = None
    session_id: str | None = None
    platform: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "producer", _required(self.producer, field_name="producer"))
        for name in ("source_event_id", "session_id", "platform"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required(value, field_name=name))

    @classmethod
    def from_legacy(cls, provenance: Any) -> "EventProvenance":
        """Copy public fields from the existing agent provenance value without importing it."""
        return cls(
            producer=getattr(provenance, "producer"),
            source_event_id=getattr(provenance, "source_event_id", None),
            session_id=getattr(provenance, "session_id", None),
            platform=getattr(provenance, "platform", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class PerceptionEvent:
    schema_version: int
    event_id: str
    source: str
    event_type: str
    timestamp: datetime
    payload: Mapping[str, Any]
    provenance: EventProvenance
    entities: tuple[str, ...] = ()
    confidence: float = 1.0
    dedup_key: str | None = None

    def __post_init__(self) -> None:
        if int(self.schema_version) <= 0:
            raise ValueError("schema_version must be positive")
        object.__setattr__(self, "schema_version", int(self.schema_version))
        for name in ("event_id", "source", "event_type"):
            object.__setattr__(self, name, _required(getattr(self, name), field_name=name))
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp, field_name="timestamp"))
        payload = _frozen_mapping(self.payload, field_name="payload")
        _reject_sensitive_keys(payload)
        object.__setattr__(self, "payload", payload)
        if not isinstance(self.provenance, EventProvenance):
            raise ValueError("provenance must be EventProvenance")
        object.__setattr__(self, "entities", _frozen_strings(self.entities, field_name="entity"))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        if self.dedup_key is not None:
            object.__setattr__(self, "dedup_key", _required(self.dedup_key, field_name="dedup_key"))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class StateValue:
    value: Any
    source: str
    confidence: float
    updated_at: datetime
    evidence_refs: tuple[str, ...]
    expires_at: datetime | None = None
    authority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze(self.value))
        object.__setattr__(self, "source", _required(self.source, field_name="source"))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "updated_at", _as_utc(self.updated_at, field_name="updated_at"))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs, field_name="evidence_ref"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _as_utc(self.expires_at, field_name="expires_at"))
        object.__setattr__(self, "authority", int(self.authority))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


def _state_domains(value: Mapping[str, StateValue], *, field_name: str) -> Mapping[str, StateValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    result: dict[str, StateValue] = {}
    for key, state in value.items():
        if not isinstance(state, StateValue):
            raise ValueError(f"{field_name} values must be StateValue")
        result[_required(str(key), field_name=f"{field_name} key")] = state
    return MappingProxyType(result)


@dataclass(frozen=True)
class WorldSnapshot:
    snapshot_id: str
    created_at: datetime
    stream: Mapping[str, StateValue] = field(default_factory=dict)
    social: Mapping[str, StateValue] = field(default_factory=dict)
    call: Mapping[str, StateValue] = field(default_factory=dict)
    media: Mapping[str, StateValue] = field(default_factory=dict)
    physical: Mapping[str, StateValue] = field(default_factory=dict)
    game: Mapping[str, StateValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _required(self.snapshot_id, field_name="snapshot_id"))
        object.__setattr__(self, "created_at", _as_utc(self.created_at, field_name="created_at"))
        for name in ("stream", "social", "call", "media", "physical", "game"):
            object.__setattr__(self, name, _state_domains(getattr(self, name), field_name=name))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class SelfSnapshot:
    snapshot_id: str
    created_at: datetime
    speaking: bool
    busy: bool
    degraded: bool
    current_action_id: str | None
    current_intention_id: str | None
    active_goal_id: str | None
    focused_thread_id: str | None
    current_topic: str | None
    attention_target: str | None
    avatar_state: Mapping[str, Any]
    recent_action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _required(self.snapshot_id, field_name="snapshot_id"))
        object.__setattr__(self, "created_at", _as_utc(self.created_at, field_name="created_at"))
        for name in (
            "current_action_id", "current_intention_id", "active_goal_id", "focused_thread_id",
            "current_topic", "attention_target",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required(value, field_name=name))
        object.__setattr__(self, "avatar_state", _frozen_mapping(self.avatar_state, field_name="avatar_state"))
        object.__setattr__(self, "recent_action_ids", _frozen_strings(self.recent_action_ids, field_name="recent_action_id"))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class Capability:
    capability_id: str
    action_type: str
    description: str
    executor_id: str
    verifier_id: str
    risk_level: str
    required_permissions: tuple[str, ...]
    parameter_schema: Mapping[str, Any]
    transaction_policy: str

    def __post_init__(self) -> None:
        for name in (
            "capability_id", "action_type", "description", "executor_id", "verifier_id",
            "risk_level", "transaction_policy",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), field_name=name))
        object.__setattr__(self, "required_permissions", _frozen_strings(self.required_permissions, field_name="required_permission"))
        object.__setattr__(self, "parameter_schema", _frozen_mapping(self.parameter_schema, field_name="parameter_schema"))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class CapabilityAvailability:
    capability_id: str
    available: bool
    reason_code: str
    checked_at: datetime
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _required(self.capability_id, field_name="capability_id"))
        object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "reason_code", _required(self.reason_code, field_name="reason_code"))
        object.__setattr__(self, "checked_at", _as_utc(self.checked_at, field_name="checked_at"))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs, field_name="evidence_ref"))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class ActionRequest:
    schema_version: int
    action_id: str
    capability_id: str
    action_type: str
    target: str | None
    arguments: Mapping[str, Any]
    intention_id: str | None
    evidence_refs: tuple[str, ...]
    idempotency_key: str
    priority: float
    requested_at: datetime
    transaction_policy: str

    def __post_init__(self) -> None:
        if int(self.schema_version) <= 0:
            raise ValueError("schema_version must be positive")
        object.__setattr__(self, "schema_version", int(self.schema_version))
        for name in ("action_id", "capability_id", "action_type", "idempotency_key", "transaction_policy"):
            object.__setattr__(self, name, _required(getattr(self, name), field_name=name))
        for name in ("target", "intention_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required(value, field_name=name))
        object.__setattr__(self, "arguments", _frozen_mapping(self.arguments, field_name="arguments"))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs, field_name="evidence_ref"))
        object.__setattr__(self, "priority", _finite(self.priority, field_name="priority"))
        object.__setattr__(self, "requested_at", _as_utc(self.requested_at, field_name="requested_at"))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class ActionResult:
    schema_version: int
    action_id: str
    status: ActionStatus
    started_at: datetime
    completed_at: datetime
    verified: bool
    verification_source: str | None
    result_data: Mapping[str, Any]
    error_code: str | None = None

    def __post_init__(self) -> None:
        if int(self.schema_version) <= 0:
            raise ValueError("schema_version must be positive")
        object.__setattr__(self, "schema_version", int(self.schema_version))
        object.__setattr__(self, "action_id", _required(self.action_id, field_name="action_id"))
        try:
            object.__setattr__(self, "status", ActionStatus(self.status))
        except ValueError as exc:
            raise ValueError("status is invalid") from exc
        started_at = _as_utc(self.started_at, field_name="started_at")
        completed_at = _as_utc(self.completed_at, field_name="completed_at")
        if completed_at < started_at:
            raise ValueError("completed_at cannot precede started_at")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "verified", bool(self.verified))
        for name in ("verification_source", "error_code"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required(value, field_name=name))
        object.__setattr__(self, "result_data", _frozen_mapping(self.result_data, field_name="result_data"))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


def perception_event_from_input(
    event: Any,
    *,
    max_payload_items: int,
    max_payload_chars: int,
    producer: str = "compatibility_input",
    session_id: str | None = None,
    event_type: str = "input.received",
) -> PerceptionEvent:
    """Map an existing InputEvent without changing the chat routing path."""
    metadata = _sanitize_metadata(getattr(event, "metadata", {}) or {})
    payload = _bounded_payload(
        {"content": str(getattr(event, "content")), "metadata": metadata},
        max_payload_items=max_payload_items,
        max_payload_chars=max_payload_chars,
    )
    source = getattr(event, "source")
    source_value = getattr(source, "value", source)
    return PerceptionEvent(
        schema_version=1,
        event_id=getattr(event, "event_id"),
        source=str(source_value),
        event_type=event_type,
        timestamp=getattr(event, "timestamp"),
        payload=payload,
        provenance=EventProvenance(
            producer=producer,
            source_event_id=getattr(event, "event_id"),
            session_id=session_id,
            platform=str(source_value),
        ),
        dedup_key=getattr(event, "event_id"),
    )


def action_request_from_transaction(
    transaction: Any,
    *,
    capability_id: str,
    requested_at: datetime,
    transaction_policy: str,
    target: str | None = None,
    arguments: Mapping[str, Any] | None = None,
    intention_id: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    priority: float = 0.0,
) -> ActionRequest:
    """Adapt the public fields of an existing transaction; this function has no side effects."""
    return ActionRequest(
        schema_version=1,
        action_id=getattr(transaction, "transaction_id"),
        capability_id=capability_id,
        action_type=getattr(transaction, "action"),
        target=target,
        arguments=arguments or {},
        intention_id=intention_id,
        evidence_refs=evidence_refs,
        idempotency_key=getattr(transaction, "idempotency_key"),
        priority=priority,
        requested_at=requested_at,
        transaction_policy=transaction_policy,
    )


def action_result_from_tts_delivery(
    delivery: Any,
    *,
    action_id: str,
    started_at: datetime,
    completed_at: datetime,
    result_data: Mapping[str, Any] | None = None,
) -> ActionResult:
    """Adapt the authoritative TTS delivery boundary without committing any transaction."""
    delivered = bool(getattr(delivery, "delivered"))
    data = {
        "request_id": str(getattr(delivery, "request_id")),
        "mode": str(getattr(getattr(delivery, "mode", None), "value", getattr(delivery, "mode", "none"))),
        "sentences_total": int(getattr(delivery, "sentences_total", 0)),
        "sentences_delivered": int(getattr(delivery, "sentences_delivered", 0)),
        **dict(result_data or {}),
    }
    return ActionResult(
        schema_version=1,
        action_id=action_id,
        status=ActionStatus.SUCCESS if delivered else ActionStatus.FAILED,
        started_at=started_at,
        completed_at=completed_at,
        verified=delivered,
        verification_source="tts_delivery" if delivered else None,
        result_data=data,
        error_code=None if delivered else "delivery_not_confirmed",
    )