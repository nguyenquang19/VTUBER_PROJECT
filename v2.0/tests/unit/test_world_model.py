from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dashboard.dashboard_server import DashboardServer
from interfaces.compatibility import EventProvenance, PerceptionEvent
from interfaces.world import WorldModelService
from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.agent.types import AgentEventKind, AgentEventSource, EventProvenance as AgentProvenance, GroundedEvent
from services.world.world_model import (
    WorldModelConfig,
    WorldModelShadow,
    perception_event_from_grounded_observation,
)


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _config(**overrides: object) -> WorldModelConfig:
    data: dict[str, object] = {
        "allowed_domains": ("stream", "social", "call", "media", "physical", "game"),
        "default_ttl_s": 30.0,
        "source_authority": {"operator": 100, "environment": 80, "runtime": 60},
        "max_state_entries": 4,
        "max_evidence_refs": 2,
        "max_payload_items": 8,
        "max_payload_chars": 256,
        "dedup_ttl_s": 60.0,
        "max_dedup_keys": 4,
    }
    data.update(overrides)
    return WorldModelConfig(**data)  # type: ignore[arg-type]


def _event(
    event_id: str,
    *,
    path: str = "stream.live",
    value: object = True,
    source: str = "environment",
    timestamp: datetime = NOW,
    confidence: float = 0.8,
    **payload_extra: object,
) -> PerceptionEvent:
    return PerceptionEvent(
        schema_version=1,
        event_id=event_id,
        source=source,
        event_type="world.observation",
        timestamp=timestamp,
        payload={"path": path, "value": value, **payload_extra},
        provenance=EventProvenance(producer="test", source_event_id=event_id),
        confidence=confidence,
    )


def test_world_model_reduces_immutable_snapshot_and_records_metrics() -> None:
    clock = Clock()
    metrics = MetricsCollector()
    model = WorldModelShadow(_config(), metrics=metrics, clock=clock)

    assert isinstance(model, WorldModelService)
    assert model.apply_event(_event("observed-1", evidence_refs=["camera:1"]))

    value = model.query("stream.live")
    assert value is not None
    assert value.value is True
    assert value.authority == 80
    assert value.evidence_refs == ("test:observed-1", "camera:1")
    assert value.confidence == 0.8
    snapshot = model.snapshot()
    assert snapshot.stream["live"] is value
    assert model.get_metrics()["world_model_state_entries"] == 1
    assert metrics.world_model_snapshot()["events"]["accepted:new"] == 1

def test_world_model_yaml_config_is_loaded_and_feature_is_registered() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()

    config = WorldModelConfig.from_loader(loader)
    assert config.allowed_domains == ("stream", "social", "call", "media", "physical", "game")
    assert config.source_authority["environment"] == 80
    assert loader.get("features", "features.world_model_shadow.enabled") is True


@pytest.mark.parametrize(
    "overrides, error",
    [
        ({"allowed_domains": ("stream", "unknown")}, "unsupported domain"),
        ({"default_ttl_s": "30"}, "must be numeric"),
        ({"max_state_entries": True}, "must be an integer"),
        ({"source_authority": {"environment": True}}, "must be an integer"),
    ],
)
def test_world_model_config_rejects_coercion_and_unknown_domains(
    overrides: dict[str, object], error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        _config(**overrides)


def test_world_model_config_is_immutable() -> None:
    config = _config()
    with pytest.raises(TypeError):
        config.source_authority["environment"] = 1  # type: ignore[index]


def test_world_model_dedup_authority_and_recency_are_deterministic() -> None:
    model = WorldModelShadow(_config(), clock=Clock())
    assert model.apply_event(_event("e1", value="first"))
    assert not model.apply_event(_event("e1", value="duplicate"))
    assert not model.apply_event(_event(
        "e2", value="lower", source="runtime", timestamp=NOW + timedelta(seconds=2),
        confidence=1.0,
    ))
    assert model.apply_event(_event(
        "e3", value="newer", timestamp=NOW + timedelta(seconds=3), confidence=0.1,
    ))
    assert model.query("stream.live").confidence == 0.1  # type: ignore[union-attr]
    assert model.apply_event(_event("e4", value="operator", source="operator", timestamp=NOW))
    assert model.query("stream.live").value == "operator"  # type: ignore[union-attr]

    outcomes = model.get_metrics()["world_model_events"]
    assert outcomes["duplicate:dedup"] == 1
    assert outcomes["rejected:lower_authority"] == 1


def test_world_model_ttl_bounds_and_invalid_events_are_isolated() -> None:
    clock = Clock()
    model = WorldModelShadow(_config(max_state_entries=1), clock=clock)
    assert not model.apply_event(_event(
        "already-stale", timestamp=NOW - timedelta(seconds=31),
    ))
    assert model.apply_event(_event("one", path="stream.live"))
    assert not model.apply_event(_event("bad-path", path="unknown.state"))
    assert not model.apply_event(_event("full", path="media.track", value="song"))
    assert model.query("stream.live") is not None

    clock.value = NOW + timedelta(seconds=31)
    assert model.apply_event(_event(
        "fresh-after-expiry", path="media.track", value="song", timestamp=clock.value,
    ))
    assert model.query("stream.live") is None
    assert model.snapshot().stream == {}
    assert model.snapshot().media["track"].value == "song"
    outcomes = model.get_metrics()["world_model_events"]
    assert outcomes["rejected:invalid"] == 1
    assert outcomes["rejected:capacity"] == 1
    assert outcomes["rejected:stale"] == 1
    assert model.get_metrics()["world_model_evicted_total"] == 1


def test_query_evicts_stale_state_and_synchronizes_metrics() -> None:
    clock = Clock()
    metrics = MetricsCollector()
    model = WorldModelShadow(_config(), metrics=metrics, clock=clock)
    assert model.apply_event(_event("one"))

    clock.value = NOW + timedelta(seconds=31)
    assert model.query("stream.live") is None
    assert model.get_metrics()["world_model_state_entries"] == 0
    assert metrics.world_model_snapshot()["stale_evictions_total"] == 1


def test_world_payload_path_and_evidence_are_strict_and_fully_bounded() -> None:
    model = WorldModelShadow(_config(max_payload_items=6, max_payload_chars=96), clock=Clock())

    assert not model.apply_event(_event("path-type", path=123))  # type: ignore[arg-type]
    assert not model.apply_event(_event("ref-type", evidence_refs=(123,)))
    assert not model.apply_event(_event("too-many", value=tuple(range(8))))
    assert not model.apply_event(_event("too-long", path=f"stream.{'x' * 100}"))
    assert not model.apply_event(object())  # type: ignore[arg-type]

    outcomes = model.get_metrics()["world_model_events"]
    assert outcomes["rejected:invalid"] == 5


def test_world_provenance_is_deduplicated_and_bounded() -> None:
    model = WorldModelShadow(_config(max_evidence_refs=2), clock=Clock())
    assert model.apply_event(_event(
        "observed-1", evidence_refs=("test:observed-1", "camera:1", "camera:1"),
    ))

    value = model.query("stream.live")
    assert value is not None
    assert value.evidence_refs == ("test:observed-1", "camera:1")


def test_world_model_feature_disable_clears_shadow_and_rejects_new_events() -> None:
    model = WorldModelShadow(_config(), clock=Clock())
    assert model.apply_event(_event("one"))
    model.set_enabled(False)

    assert model.snapshot().stream == {}
    assert not model.apply_event(_event("two"))
    assert model.get_metrics()["world_model_events"]["disabled:feature_disabled"] == 1


def test_grounded_bridge_only_accepts_structured_environment_observation() -> None:
    grounded = GroundedEvent(
        event_id="grounded:1",
        kind=AgentEventKind.ENVIRONMENT_OBSERVED,
        source=AgentEventSource.ENVIRONMENT,
        timestamp=NOW,
        confidence=0.7,
        payload={"state_path": "physical.door_open", "value": False, "evidence_refs": ["sensor:1"]},
        provenance=AgentProvenance(producer="sensor"),
    )
    perception = perception_event_from_grounded_observation(grounded)
    assert perception is not None
    assert perception.event_type == "world.observation"
    assert perception.payload["path"] == "physical.door_open"

    chat = GroundedEvent(
        event_id="chat:1",
        kind=AgentEventKind.CHAT_RECEIVED,
        source=AgentEventSource.CHAT,
        timestamp=NOW,
        confidence=1.0,
        payload={"text": "door is open"},
        provenance=AgentProvenance(producer="chat"),
    )
    assert perception_event_from_grounded_observation(chat) is None


def test_dashboard_exposes_world_shadow_read_only_and_runtime_does_not_pass_it_to_director() -> None:
    model = WorldModelShadow(_config(), clock=Clock())
    assert model.apply_event(_event("one"))
    snapshot = asyncio.run(DashboardServer(world_model=model).build_snapshot())
    assert snapshot["world"]["snapshot"]["stream"]["live"]["value"] is True

    source = (Path(__file__).resolve().parents[2] / "orchestrator" / "stream_runtime.py").read_text(encoding="utf-8")
    director_call = source[source.index("director_loop = DirectorLoop("):source.index("# ─── M9 operator control plane")]
    assert "world_model=world_model" not in director_call
    root = Path(__file__).resolve().parents[2]
    assert 'id="system-world"' in (root / "dashboard" / "templates" / "operator_v2.html").read_text(encoding="utf-8")
    assert 'fillDl("system-world"' in (root / "dashboard" / "static" / "operator_v2.js").read_text(encoding="utf-8")
    loader = ConfigLoader(root / "config")
    loader.load_all()
    assert loader.get("features", "features.context_selector.enabled") is False
