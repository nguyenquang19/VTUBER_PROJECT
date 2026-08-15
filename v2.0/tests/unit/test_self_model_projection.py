from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.dashboard_server import DashboardServer
from interfaces.base import HealthState
from interfaces.self_model import SelfModelService
from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.self_model.projection import SelfModelConfig, SelfModelProjection


NOW = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)


class SnapshotSource:
    def __init__(self, value: object) -> None:
        self.value = value

    def snapshot(self) -> object:
        return self.value


class BrokenSource:
    def snapshot(self) -> object:
        raise RuntimeError("unavailable")


class Player:
    def __init__(self, is_playing: bool = False) -> None:
        self.is_playing = is_playing


class Animation:
    enabled = True

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected

    def get_metrics(self) -> dict[str, bool]:
        return {"animation_connected": self.connected}


def _agent(*, topic: str = "music", thread_id: str = "thread-2") -> SnapshotSource:
    return SnapshotSource(SimpleNamespace(
        current_topic=SimpleNamespace(summary=topic),
        open_threads=(
            SimpleNamespace(thread_id="thread-1", updated_at=NOW),
            SimpleNamespace(thread_id=thread_id, updated_at=NOW.replace(minute=1)),
        ),
    ))


def _transactions() -> SnapshotSource:
    return SnapshotSource({"recent": [
        {"transaction_id": "act-old", "state": "committed", "updated_at": 10.0},
        {"transaction_id": "act-live", "state": "delivering", "updated_at": 30.0},
        {"transaction_id": "act-next", "state": "reserved", "updated_at": 20.0},
    ]})


def _model(**overrides: object) -> tuple[SelfModelProjection, MetricsCollector]:
    metrics = MetricsCollector()
    values: dict[str, object] = {
        "config": SelfModelConfig(max_recent_action_ids=2),
        "agent_state": _agent(),
        "goal_manager": SnapshotSource(SimpleNamespace(active=SimpleNamespace(goal_id="goal-1"))),
        "action_transactions": _transactions(),
        "audio_player": Player(),
        "animation": Animation(),
        "health_snapshot_provider": lambda: {"targets": {"tts": {"health": "healthy"}}},
        "metrics": metrics,
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return SelfModelProjection(**values), metrics  # type: ignore[arg-type]


def test_self_model_projects_immutable_authoritative_state_and_metrics() -> None:
    model, metrics = _model()

    assert isinstance(model, SelfModelService)
    snapshot = model.snapshot()
    assert snapshot.speaking is False
    assert snapshot.busy is True
    assert snapshot.degraded is False
    assert snapshot.current_action_id == "act-live"
    assert snapshot.active_goal_id == "goal-1"
    assert snapshot.focused_thread_id == "thread-2"
    assert snapshot.current_topic == "music"
    assert snapshot.avatar_state == {"enabled": True, "connected": True}
    assert snapshot.recent_action_ids == ("act-live", "act-next")
    with pytest.raises(TypeError):
        snapshot.avatar_state["enabled"] = False  # type: ignore[index]
    assert model.snapshot().snapshot_id == snapshot.snapshot_id
    assert metrics.self_model_snapshot()["snapshots"] == {"projected": 2}


def test_self_model_reflects_source_changes_and_bounds_recent_actions() -> None:
    agent = _agent(topic="games", thread_id="thread-3")
    transactions = SnapshotSource({"recent": [
        {"transaction_id": "act-1", "state": "committed", "updated_at": 1.0},
        {"transaction_id": "act-2", "state": "generated", "updated_at": 2.0},
        {"transaction_id": "act-3", "state": "reserved", "updated_at": 3.0},
    ]})
    model, _ = _model(agent_state=agent, action_transactions=transactions, audio_player=Player(True))

    snapshot = model.snapshot()
    assert snapshot.speaking is True
    assert snapshot.busy is True
    assert snapshot.current_action_id == "act-3"
    assert snapshot.current_topic == "games"
    assert snapshot.focused_thread_id == "thread-3"
    assert snapshot.recent_action_ids == ("act-3", "act-2")


def test_self_model_degrades_without_fabricating_state_and_feature_disable_is_explicit() -> None:
    model, _ = _model(
        agent_state=BrokenSource(),
        animation=Animation(False),
        health_snapshot_provider=lambda: {"targets": {"executor": {"health": "unhealthy"}}},
    )
    degraded = model.snapshot()
    assert degraded.degraded is True
    assert degraded.current_topic is None
    assert degraded.focused_thread_id is None
    assert degraded.avatar_state == {"enabled": True, "connected": False}

    model.set_enabled(False)
    disabled = model.snapshot()
    assert disabled.snapshot_id == "self-disabled"
    assert disabled.degraded is True
    assert disabled.recent_action_ids == ()
    assert asyncio.run(model.health_check()).state is HealthState.STOPPED


def test_self_model_yaml_feature_dashboard_and_director_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    assert SelfModelConfig.from_loader(loader).max_recent_action_ids == 8
    assert loader.get("features", "features.self_model_projection.enabled") is True

    model, _ = _model()
    dashboard = asyncio.run(DashboardServer(self_model=model).build_snapshot())
    assert dashboard["self"]["snapshot"]["current_action_id"] == "act-live"
    assert dashboard["self"]["metrics"]["self_model_enabled"] is True

    source = (root / "orchestrator" / "stream_runtime.py").read_text(encoding="utf-8")
    director_call = source[source.index("director_loop = DirectorLoop("):source.index("# ─── M9 operator control plane")]
    assert "self_model=self_model" not in director_call
    template = (root / "dashboard" / "templates" / "operator_v2.html").read_text(encoding="utf-8")
    assert 'id="system-self"' in template
