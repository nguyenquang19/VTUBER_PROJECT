"""Wire-format models cho journal train — nguồn chân lý duy nhất (release 1.1.0).

Mục đích: tách định dạng record khỏi nội tạng engine. Engine phải map VÀO các model
này; `extra="forbid"` nên field lạ do engine drift bị bắt ngay tại write-time thay
vì âm thầm làm bẩn corpus.

Ba lớp:
  - TurnRecordV3      : generation attempt (logs/turns.jsonl), turn schema v3
  - DeliveryOutcomeV1 : delivery outcome  (logs/delivery_outcomes.jsonl), schema v1
  - CanonicalTurnV1   : projection cố định cho canonical/ (schema v1)

Fingerprint: `schema_fingerprint()` hash field-set + kiểu; chốt trong
config/data_schema_registry.yaml. Đổi model mà quên bump version → fingerprint lệch →
startup fail-fast (xem orchestrator startup guard) + CI test.

Field GIỮ NGUYÊN so với baseline: không đổi turn v3 / delivery v1 / canonical v1.
Khi cần đổi thật: thêm TurnRecordV4 + fingerprint mới + adapter canonical mới; KHÔNG
sửa V3 tại chỗ.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

_STRICT = ConfigDict(extra="forbid")

# Version constants — nguồn duy nhất, thay cho hằng số rải trong llm_turn.py.
TURN_SCHEMA_VERSION = 3
DELIVERY_OUTCOME_SCHEMA_VERSION = 1
CANONICAL_SCHEMA_VERSION = 1


class TurnRecordV3(BaseModel):
    """generation_attempt — logs/turns.jsonl. extra=forbid để bắt drift."""

    model_config = _STRICT

    # identity
    schema_version: Literal[3] = 3
    turn_id: int
    request_id: str
    record_type: Literal["generation_attempt"] = "generation_attempt"
    session_id: str | None = None
    kind: str

    # input/output (đã sanitize ở caller)
    user_text: str | None = None
    mai_text: str | None = None
    viewer_id: str | None = None   # đã hash ở caller, không phải raw id

    # generation
    parse_ok: bool | None = None
    level_used: int | None = None
    latency_ms: int | None = None
    trigger_type: str | None = None
    source: str | None = None
    history_len: int | None = None
    was_regen: bool | None = None

    # mood snapshot bounded
    mood_dominant: str | None = None
    mood_intensity: int | None = None
    mood_state: dict[str, Any] | None = None
    mood_cause: dict[str, Any] | None = None
    raw_had_mood_block: bool | None = None

    # context/filter
    event_category: str | None = None
    context_block: str | None = None
    filter_verdict: dict[str, Any] | None = None

    # provenance / contract versions
    persona_version: str | None = None
    architecture_version: str | None = None
    context_schema_version: str | None = None
    agenda_policy_version: str | None = None

    # writer-stamped
    timestamp: str | None = None


class DeliveryOutcomeV1(BaseModel):
    """delivery_outcome — logs/delivery_outcomes.jsonl."""

    model_config = _STRICT

    schema_version: Literal[1] = 1
    record_type: Literal["delivery_outcome"] = "delivery_outcome"
    session_id: str | None = None
    request_id: str
    turn_id: int
    delivered: bool
    mode: str | None = None
    timestamp: str | None = None


class CanonicalTurnV1(BaseModel):
    """Projection ổn định cho canonical/turns.jsonl. Field-set CỐ ĐỊNH.

    Chỉ giữ field cần cho train/eval + provenance; field engine mới KHÔNG rò vào đây
    trừ khi thêm tường minh (bump canonical version + adapter).
    """

    model_config = _STRICT

    schema_version: Literal[1] = 1
    record_type: Literal["canonical_turn"] = "canonical_turn"
    data_contract_id: str
    source_schema_version: int
    canonical_adapter_id: str

    session_id: str | None = None
    turn_id: int
    request_id: str
    kind: str | None = None
    user_text: str | None = None
    mai_text: str | None = None
    event_category: str | None = None
    mood_dominant: str | None = None
    mood_intensity: int | None = None
    persona_version: str | None = None
    architecture_version: str | None = None
    context_schema_version: str | None = None
    agenda_policy_version: str | None = None

    # Field consumer (build_sft) cần để lọc chất lượng + dựng prompt.
    level_used: int | None = None
    parse_ok: bool | None = None
    filter_verdict: dict[str, Any] | None = None
    context_block: str | None = None


# Registry version → model. Thêm version mới phải thêm vào đây.
TURN_MODELS: dict[int, type[BaseModel]] = {3: TurnRecordV3}
DELIVERY_MODELS: dict[int, type[BaseModel]] = {1: DeliveryOutcomeV1}
CANONICAL_MODELS: dict[int, type[BaseModel]] = {1: CanonicalTurnV1}


def schema_fingerprint(model: type[BaseModel]) -> str:
    """Hash ổn định của field-set + kiểu. Đổi field/kiểu → đổi fingerprint.

    Deterministic: sort field theo tên, lấy (name, type_repr, required).
    """
    fields = []
    for name in sorted(model.model_fields):
        info = model.model_fields[name]
        type_repr = str(info.annotation)
        fields.append((name, type_repr, info.is_required()))
    blob = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def current_fingerprints() -> dict[str, str]:
    """Fingerprint hiện tại của mọi model — để so với registry."""
    out: dict[str, str] = {}
    for v, m in TURN_MODELS.items():
        out[f"turn_v{v}"] = schema_fingerprint(m)
    for v, m in DELIVERY_MODELS.items():
        out[f"delivery_v{v}"] = schema_fingerprint(m)
    for v, m in CANONICAL_MODELS.items():
        out[f"canonical_v{v}"] = schema_fingerprint(m)
    return out


class SchemaDriftError(Exception):
    """Model schema lệch fingerprint đã chốt — đổi model mà quên bump version."""


def assert_no_schema_drift(registry: dict[str, str]) -> None:
    """Fail-fast nếu fingerprint model hiện tại lệch registry đã chốt.

    `registry`: dict {name: fingerprint} đọc từ config/data_schema_registry.yaml.
    Gọi lúc startup (stream_runtime) và trong CI test. Raise SchemaDriftError.
    """
    current = current_fingerprints()
    mismatches = []
    for name, fp in current.items():
        expected = registry.get(name)
        if expected is None:
            mismatches.append(f"{name}: chưa có trong registry")
        elif expected != fp:
            mismatches.append(f"{name}: registry={expected} != model={fp}")
    if mismatches:
        raise SchemaDriftError(
            "record schema drift (đổi model phải bump version + cập nhật "
            "config/data_schema_registry.yaml): " + "; ".join(mismatches)
        )


class SchemaValidationError(Exception):
    """Record không khớp model — caller route sang quarantine."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


def validate_turn(record: dict[str, Any]) -> dict[str, Any]:
    """Validate 1 turn record theo version của nó. Raise SchemaValidationError."""
    return _validate(record, TURN_MODELS, "turn")


def validate_delivery(record: dict[str, Any]) -> dict[str, Any]:
    return _validate(record, DELIVERY_MODELS, "delivery")


def _validate(
    record: dict[str, Any], models: dict[int, type[BaseModel]], kind: str,
) -> dict[str, Any]:
    version = record.get("schema_version")
    model = models.get(version) if isinstance(version, int) else None
    if model is None:
        raise SchemaValidationError(kind, f"no model for schema_version={version!r}")
    try:
        model.model_validate(record)
    except ValidationError as e:
        raise SchemaValidationError(kind, _compact_error(e)) from e
    return record


def _compact_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}:{err.get('type')}")
    return ";".join(parts)[:400]
