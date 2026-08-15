from __future__ import annotations

from interfaces.animation import MoodState
from orchestrator.config_loader import ConfigLoader
from orchestrator.metrics_collector import MetricsCollector
from services.agent.behavior_library import BehaviorKind, BehaviorLibrary


def _library(metrics=None) -> BehaviorLibrary:
    from pathlib import Path

    loader = ConfigLoader(Path(__file__).resolve().parents[2] / "config")
    loader.load_all()
    return BehaviorLibrary.from_loader(loader, metrics=metrics)


def test_all_seven_behaviors_have_applicability_and_safety_guard() -> None:
    library = _library()
    assert set(library.config.behaviors) == set(BehaviorKind)
    for spec in library.config.behaviors.values():
        assert spec.directive
        assert spec.actions
        assert isinstance(spec.forbidden_flags, tuple)


def test_strong_buc_selects_tease_for_read_chat_only() -> None:
    selected = _library().select("read_chat", MoodState(buc=8))
    assert selected.kind is BehaviorKind.TEASE
    assert selected.applicable
    assert "grounded" in selected.directive


def test_force_gentle_always_wins_tease() -> None:
    selected = _library().select(
        "read_chat", MoodState(buc=10), {"force_gentle_tone"},
    )
    assert selected.kind is BehaviorKind.ACKNOWLEDGE
    assert "do not roast" in selected.directive.lower()


def test_force_deflect_always_wins_persona_roast() -> None:
    selected = _library().select(
        "read_chat", MoodState(buc=10), {"force_deflect"},
    )
    assert selected.kind is BehaviorKind.DEFLECT
    assert "do not flirt back" in selected.directive.lower()


def test_repair_wins_regular_action_default() -> None:
    selected = _library().select(
        "read_chat", MoodState(), repair_kind="missing_evidence",
    )
    assert selected.kind is BehaviorKind.REPAIR
    assert "do not guess" in selected.directive.lower()


def test_action_defaults_cover_invite_transition_and_acknowledge() -> None:
    library = _library()
    assert library.select("ask_follow_up", MoodState()).kind is BehaviorKind.INVITE
    assert library.select("transition", MoodState()).kind is BehaviorKind.TRANSITION
    assert library.select("ack_donation", MoodState()).kind is BehaviorKind.ACKNOWLEDGE


def test_service_lifecycle_toggle_and_metrics() -> None:
    metrics = MetricsCollector()
    library = _library(metrics)
    library.select("read_chat", MoodState())
    assert metrics.host_behavior_snapshot()
    library.set_enabled(False)
    assert not library.select("read_chat", MoodState()).applicable


async def test_behavior_library_lifecycle() -> None:
    library = _library()
    assert not (await library.health_check()).is_ok
    await library.start()
    assert (await library.health_check()).is_ok
    await library.stop()
