from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interfaces.compatibility import EventProvenance, PerceptionEvent
from orchestrator.config_loader import ConfigLoader
from services.perception.ingress import (
    PerceptionIngress,
    PerceptionIngressConfig,
    SystemGroundedRoute,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _config(**changes: object) -> PerceptionIngressConfig:
    values: dict[str, object] = {
        "max_payload_items": 20,
        "max_payload_chars": 512,
        "max_recent_events": 2,
        "max_event_age_s": 60.0,
        "max_future_skew_s": 2.0,
        "dedup_ttl_s": 30.0,
        "max_dedup_keys": 2,
        "chat_producer": "chat_perception_adapter",
        "chat_sources": ("chat_youtube", "chat_discord"),
        "system_producer": "system_perception_adapter",
        "system_input_sources": ("system_timer", "dashboard"),
        "system_grounded_routes": (
            SystemGroundedRoute(
                "stream_runtime", "runtime", "runtime", "stream.runtime", "snapshot",
            ),
        ),
        "obs_producer": "obs_perception_adapter",
        "obs_source": "environment",
        "obs_world_path": "stream.current_scene",
        "obs_poll_interval_s": 2.0,
        "obs_query_timeout_s": 1.0,
    }
    values.update(changes)
    return PerceptionIngressConfig(**values)  # type: ignore[arg-type]


def _event(
    *,
    event_id: str = "input:1",
    producer: str = "chat_perception_adapter",
    source: str = "chat_youtube",
    event_type: str = "input.received",
    timestamp: datetime = NOW,
    payload: dict[str, object] | None = None,
    schema_version: int = 1,
) -> PerceptionEvent:
    return PerceptionEvent(
        schema_version=schema_version,
        event_id=event_id,
        source=source,
        event_type=event_type,
        timestamp=timestamp,
        payload=payload or {"content": "xin chao", "metadata": {}},
        provenance=EventProvenance(producer=producer, source_event_id=event_id),
        dedup_key=event_id,
    )


class _World:
    def __init__(self, result: object = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.events: list[PerceptionEvent] = []

    def apply_event(self, event: PerceptionEvent) -> object:
        self.events.append(event)
        if self.error is not None:
            raise self.error
        return self.result


def _obs_event(event_id: str = "obs:1") -> PerceptionEvent:
    return _event(
        event_id=event_id,
        producer="obs_perception_adapter",
        source="environment",
        event_type="world.observation",
        payload={"path": "stream.current_scene", "value": "Main", "evidence_refs": ("obs:get:1",)},
    )


def test_production_yaml_loads_strict_routes_and_optional_obs_is_off() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()

    config = PerceptionIngressConfig.from_loader(loader)

    assert config.chat_sources == ("chat_twitch", "chat_youtube", "chat_discord")
    assert config.system_grounded_routes[0].world_path == "stream.runtime"
    assert config.obs_world_path == "stream.current_scene"
    assert loader.get("features", "features.obs_perception_adapter.enabled") is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_payload_items", True),
        ("max_recent_events", 0),
        ("max_event_age_s", float("nan")),
        ("max_future_skew_s", -1),
        ("chat_sources", ("chat_youtube", "chat_youtube")),
        ("obs_world_path", "invalid"),
        ("obs_query_timeout_s", "1"),
    ],
)
def test_config_rejects_coercion_duplicate_and_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_stopped_disabled_and_invalid_routes_fail_before_retention() -> None:
    ingress = PerceptionIngress(_config(), clock=lambda: NOW)
    assert ingress.submit(_event()) is False
    asyncio.run(ingress.start())
    assert ingress.submit(_event(producer="unknown")) is False
    assert ingress.submit(_event(schema_version=2)) is False
    ingress.set_enabled(False)
    assert ingress.submit(_event(event_id="input:2")) is False
    assert ingress.recent_events() == ()
    outcomes = ingress.get_metrics()["perception_events"]
    assert outcomes["rejected:chat_youtube:stopped"] == 1
    assert outcomes["rejected:chat_youtube:producer_not_allowed"] == 1
    assert outcomes["rejected:chat_youtube:schema_version"] == 1
    assert outcomes["rejected:chat_youtube:feature_disabled"] == 1


def test_freshness_dedup_and_expiry_are_deterministic() -> None:
    current = [NOW]
    ingress = PerceptionIngress(_config(), clock=lambda: current[0])
    asyncio.run(ingress.start())

    assert ingress.submit(_event(event_id="stale", timestamp=NOW - timedelta(seconds=61))) is False
    assert ingress.submit(_event(event_id="future", timestamp=NOW + timedelta(seconds=3))) is False
    assert ingress.submit(_event()) is True
    assert ingress.submit(_event()) is False
    current[0] += timedelta(seconds=31)
    assert ingress.submit(_event(timestamp=current[0])) is True

    outcomes = ingress.get_metrics()["perception_events"]
    assert outcomes["rejected:chat_youtube:stale"] == 1
    assert outcomes["rejected:chat_youtube:future"] == 1
    assert outcomes["duplicate:chat_youtube:dedup"] == 1


def test_history_and_dedup_are_bounded_and_disable_clears_sensitive_cache() -> None:
    ingress = PerceptionIngress(_config(max_recent_events=1, max_dedup_keys=1), clock=lambda: NOW)
    asyncio.run(ingress.start())
    assert ingress.submit(_event(event_id="input:1"))
    assert ingress.submit(_event(event_id="input:2"))
    assert [event.event_id for event in ingress.recent_events()] == ["input:2"]
    assert ingress.get_metrics()["perception_evicted_total"] == 2

    ingress.set_enabled(False)
    assert ingress.recent_events() == ()
    assert ingress.get_metrics()["perception_dedup_keys"] == 0


@pytest.mark.parametrize(
    ("world", "metric_key"),
    [
        (_World(False), "projection_rejected:environment:world_rejected"),
        (_World("true"), "projection_error:environment:world_invalid_result"),
        (_World(error=RuntimeError("boom")), "projection_error:environment:world_exception"),
    ],
)
def test_world_projection_failure_is_isolated_after_canonical_admission(
    world: _World, metric_key: str,
) -> None:
    ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: NOW)
    asyncio.run(ingress.start())

    event = _obs_event()
    assert ingress.submit(event) is True
    assert ingress.recent_events() == (event,)
    assert ingress.get_metrics()["perception_events"][metric_key] == 1


def test_chat_can_never_claim_a_world_path() -> None:
    world = _World()
    ingress = PerceptionIngress(_config(), world_model=world, clock=lambda: NOW)
    asyncio.run(ingress.start())
    event = _event(payload={"content": "hello", "metadata": {}, "path": "stream.live"})

    assert ingress.submit(event) is False
    assert world.events == []
    assert ingress.recent_events() == ()


def test_metric_callback_failure_never_changes_admission() -> None:
    class BadMetrics:
        def record_perception_event(self, outcome: str, source: str) -> None:
            raise RuntimeError("metric failed")

        def set_perception_recent_events(self, entries: int) -> None:
            raise RuntimeError("gauge failed")

    ingress = PerceptionIngress(_config(), metrics=BadMetrics(), clock=lambda: NOW)
    asyncio.run(ingress.start())
    assert ingress.submit(_event()) is True
    ingress.set_enabled(False)
    assert ingress.recent_events() == ()


def test_stop_is_idempotent_and_clears_cache() -> None:
    ingress = PerceptionIngress(_config(), clock=lambda: NOW)
    asyncio.run(ingress.start())
    assert ingress.submit(_event()) is True
    asyncio.run(ingress.stop())
    asyncio.run(ingress.stop())
    assert ingress.recent_events() == ()
    assert asyncio.run(ingress.health_check()).service_id == "perception_ingress"
