"""Trust boundary tests cho dataset wire-schema (1.1.0)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from services.data.record_schema import (
    CanonicalTurnV1,
    SchemaDriftError,
    SchemaValidationError,
    assert_no_schema_drift,
    current_fingerprints,
    validate_delivery,
    validate_turn,
)

_REGISTRY = Path(__file__).resolve().parents[2] / "config" / "data_schema_registry.yaml"


def _real_turn() -> dict:
    """Turn record đúng shape _log_turn ghi ra."""
    return {
        "schema_version": 3, "turn_id": 1, "request_id": "r1",
        "record_type": "generation_attempt", "kind": "chat",
        "user_text": "hi", "mai_text": "chào", "raw_had_mood_block": False,
        "parse_ok": True, "mood_dominant": "vui", "mood_intensity": 6,
        "trigger_type": "chat_youtube", "level_used": 0, "latency_ms": 420,
        "viewer_id": "hash123", "session_id": "s1", "source": "chat",
        "architecture_version": "mai-agent-v1",
        "context_schema_version": "mai-context-v1",
        "agenda_policy_version": "mai-agenda-v1",
        "persona_version": "a755c6d68383", "context_block": "[ctx]",
        "mood_state": {"vui": 6}, "mood_cause": {"alias": "[PII]"},
        "event_category": "chat_compliment", "history_len": 4,
        "was_regen": False, "filter_verdict": {"passed": True, "regen": False},
        "timestamp": "2026-08-12T00:00:00Z",
    }


# ---- fingerprint drift guard ----

def test_registry_matches_current_fingerprints():
    """Đổi model mà quên bump version → test này fail (drift guard CI)."""
    registry = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))["fingerprints"]
    assert_no_schema_drift(registry)   # không raise = khớp


def test_drift_detected_when_fingerprint_wrong():
    registry = dict(current_fingerprints())
    registry["turn_v3"] = "deadbeef00000000"
    with pytest.raises(SchemaDriftError):
        assert_no_schema_drift(registry)


def test_missing_version_in_registry_is_drift():
    with pytest.raises(SchemaDriftError):
        assert_no_schema_drift({})


# ---- write-time validation ----

def test_real_turn_record_validates():
    assert validate_turn(_real_turn()) is not None


def test_unknown_field_rejected():
    bad = _real_turn()
    bad["new_engine_field"] = "x"
    with pytest.raises(SchemaValidationError) as e:
        validate_turn(bad)
    assert "extra_forbidden" in e.value.reason


def test_wrong_type_rejected():
    bad = _real_turn()
    bad["turn_id"] = "not-an-int"
    with pytest.raises(SchemaValidationError):
        validate_turn(bad)


def test_unknown_schema_version_rejected():
    bad = _real_turn()
    bad["schema_version"] = 99
    with pytest.raises(SchemaValidationError) as e:
        validate_turn(bad)
    assert "no model" in e.value.reason


def test_delivery_outcome_validates():
    ok = {
        "schema_version": 1, "record_type": "delivery_outcome", "session_id": "s1",
        "request_id": "r1", "turn_id": 1, "delivered": True, "mode": "audio",
        "timestamp": "2026-08-12T00:00:00Z",
    }
    assert validate_delivery(ok) is not None


def test_delivery_missing_required_rejected():
    with pytest.raises(SchemaValidationError):
        validate_delivery({"schema_version": 1, "delivered": True})  # thiếu turn_id/request_id


# ---- canonical projection ----

def test_canonical_projection_drops_unknown_fields():
    raw = {**_real_turn(), "junk_engine_field": "DROP"}
    projected = CanonicalTurnV1(
        schema_version=1, data_contract_id="mai-agent-v1", source_schema_version=3,
        canonical_adapter_id="turn-v3-to-canonical-v1",
        session_id=raw.get("session_id"), turn_id=raw["turn_id"],
        request_id=raw["request_id"], mai_text=raw.get("mai_text"),
        user_text=raw.get("user_text"), level_used=raw.get("level_used"),
        parse_ok=raw.get("parse_ok"), filter_verdict=raw.get("filter_verdict"),
        context_block=raw.get("context_block"),
    ).model_dump()
    assert "junk_engine_field" not in projected
    # field SFT cần vẫn còn
    for field in ("mai_text", "user_text", "level_used", "parse_ok", "filter_verdict"):
        assert field in projected
