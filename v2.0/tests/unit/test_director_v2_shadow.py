"""Deterministic and non-mutating checks for the Director V2 shadow."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from interfaces.base import HealthState
from interfaces.compatibility import Capability, CapabilityAvailability
from interfaces.director_v2 import DirectorV2Candidate, DirectorV2Context, DirectorV2Proposal
from orchestrator.config_loader import ConfigLoader
from services.director.v2_shadow import (
    DirectorV2Shadow, DirectorV2ShadowConfig, director_v2_snapshot_id,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Availability:
    available: bool
    reason_code: str = "available"


class _Registry:
    def __init__(self, values: dict[str, _Availability]) -> None:
        self.values = values

    def capability(self, capability_id: str) -> Capability | None:
        if capability_id not in self.values:
            return None
        return Capability(
            capability_id=capability_id,
            action_type=capability_id,
            description="test capability",
            executor_id="test",
            verifier_id="test",
            risk_level="low",
            required_permissions=(),
            parameter_schema={},
            transaction_policy="none",
        )

    def availability(self, capability_id: str) -> CapabilityAvailability:
        value = self.values.get(
            capability_id, _Availability(False, "unknown_capability"),
        )
        return CapabilityAvailability(
            capability_id=capability_id,
            available=value.available,
            reason_code=value.reason_code,
            checked_at=NOW,
            evidence_refs=(),
        )


def _config(**changes: object) -> DirectorV2ShadowConfig:
    base: dict[str, object] = {
        "tick_seconds": 1.0,
        "max_recent_records": 2,
        "max_candidates_per_source": 2,
        "max_evidence_refs": 2,
        "max_label_chars": 64,
        "source_weights": {
            "chat": 0.0, "thread": 0.0, "goal": 0.0, "world": 0.0,
            "capability": 0.0, "proactive": 0.0, "wait": 0.0,
        },
        "source_priority": ("chat", "thread", "goal", "world", "capability", "proactive", "wait"),
    }
    base.update(changes)
    return DirectorV2ShadowConfig(**base)  # type: ignore[arg-type]


def _context(**changes: object) -> DirectorV2Context:
    base: dict[str, object] = {
        "created_at": 100.0,
        "world_snapshot_id": "world-1",
        "self_snapshot_id": "self-1",
        "capability_snapshot_id": "caps-1",
        "candidates": (),
    }
    base.update(changes)
    return DirectorV2Context(**base)  # type: ignore[arg-type]


def _shadow(registry: _Registry) -> DirectorV2Shadow:
    return DirectorV2Shadow(
        _config(), capability_registry=registry,
        context_provider=lambda: _context(), clock=lambda: 100.0,
    )


def test_hard_priority_holds_before_any_candidate() -> None:
    shadow = _shadow(_Registry({"READ_CHAT": _Availability(True)}))
    context = _context(
        emergency=True,
        candidates=(DirectorV2Candidate("chat", "chat-1", "READ_CHAT", "READ_CHAT", score=99),),
    )

    proposal = shadow.propose(context)

    assert proposal.action_type == "WAIT"
    assert proposal.reason_codes == ("emergency",)
    assert shadow.snapshot()["current"]["outcome"] == "hard_hold"  # type: ignore[index]


def test_hard_priority_order_includes_permission_before_transaction_conflict() -> None:
    shadow = _shadow(_Registry({"READ_CHAT": _Availability(True)}))
    proposal = shadow.propose(_context(permission_hold=True, transaction_conflict=True))

    assert proposal.reason_codes == ("permission_hold",)

def test_donation_wins_over_scored_chat_and_is_validated() -> None:
    shadow = _shadow(_Registry({"READ_CHAT": _Availability(True)}))
    context = _context(candidates=(
        DirectorV2Candidate("chat", "normal", "READ_CHAT", "READ_CHAT", score=100),
        DirectorV2Candidate(
            "chat", "donation", "READ_CHAT", "READ_CHAT", score=1,
            evidence_refs=("chat:donation",), is_donation=True,
        ),
    ))

    proposal = shadow.propose(context)

    assert proposal.candidate_id == "donation"
    assert proposal.reason_codes == ("donation_priority", "validated")


def test_unavailable_action_is_rejected_to_wait_without_side_effect() -> None:
    shadow = _shadow(_Registry({"READ_CHAT": _Availability(False, "permission_denied")}))
    proposal = shadow.propose(_context(candidates=(
        DirectorV2Candidate("chat", "chat-1", "READ_CHAT", "READ_CHAT", score=10),
    )))

    assert proposal.action_type == "WAIT"
    assert proposal.reason_codes == ("selected", "source_chat", "capability_permission_denied")
    assert shadow.snapshot()["current"]["outcome"] == "validation_rejected"  # type: ignore[index]


def test_tie_replay_and_retention_are_deterministic_and_bounded() -> None:
    shadow = _shadow(_Registry({"READ_CHAT": _Availability(True), "SELF_TALK": _Availability(True)}))
    context = _context(candidates=(
        DirectorV2Candidate(
            "thread", "b", "SELF_TALK", "SELF_TALK", score=1,
            evidence_refs=("x", "y"),
        ),
        DirectorV2Candidate("chat", "a", "READ_CHAT", "READ_CHAT", score=1),
    ))

    first = shadow.propose(context)
    second = shadow.propose(context)
    shadow.propose(_context(created_at=101.0))

    assert first == second
    assert first.candidate_id == "a"
    assert len(shadow.snapshot()["recent"]) == 2  # type: ignore[arg-type]


def test_disabled_shadow_does_not_retain_or_call_registry() -> None:
    registry = _Registry({"READ_CHAT": _Availability(True)})
    shadow = _shadow(registry)
    shadow.set_enabled(False)

    proposal = shadow.propose(_context(candidates=(
        DirectorV2Candidate("chat", "chat-1", "READ_CHAT", "READ_CHAT", score=1),
    )))

    assert proposal.reason_codes == ("feature_disabled",)
    assert shadow.snapshot()["recent"] == []


def test_every_candidate_source_is_bounded_and_not_injected_into_legacy_director() -> None:
    shadow = _shadow(_Registry({"SELF_TALK": _Availability(True)}))
    sources = ("chat", "thread", "goal", "world", "capability", "proactive")
    proposal = shadow.propose(_context(candidates=tuple(
        DirectorV2Candidate(source, f"{source}-id", "SELF_TALK", "SELF_TALK", score=1)
        for source in sources
    )))
    source = (Path(__file__).parents[2] / "orchestrator" / "stream_runtime.py").read_text(encoding="utf-8")
    director_call = source[source.index("director_loop = DirectorLoop("):source.index("# ─── M9 operator control plane")]

    assert proposal.action_type == "SELF_TALK"
    assert "director_v2_shadow=" not in director_call


def test_interface_contracts_reject_coercion_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="source must be"):
        DirectorV2Candidate(None, "id", "WAIT", "WAIT")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source is unsupported"):
        DirectorV2Candidate("unknown", "id", "WAIT", "WAIT")
    with pytest.raises(ValueError, match="score must be"):
        DirectorV2Candidate("chat", "id", "READ_CHAT", "READ_CHAT", score=float("nan"))
    with pytest.raises(ValueError, match="evidence_refs must be a tuple"):
        DirectorV2Candidate(
            "chat", "id", "READ_CHAT", "READ_CHAT", evidence_refs=["chat:id"],
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="is_donation must be a bool"):
        DirectorV2Candidate(
            "chat", "id", "READ_CHAT", "READ_CHAT", is_donation="false",
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="candidates must be a tuple"):
        _context(candidates=[])
    with pytest.raises(ValueError, match="operator_hold must be a bool"):
        _context(operator_hold="false")
    with pytest.raises(ValueError, match="source_failures must be unique"):
        _context(source_failures=("world", "world"))
    with pytest.raises(ValueError, match="unsupported source"):
        _context(source_failures=("invented",))
    with pytest.raises(ValueError, match="reason_codes must be a tuple"):
        DirectorV2Proposal(
            "p", 1.0, "WAIT", "WAIT", "wait", ["selected"], (), 0.0,
        )  # type: ignore[arg-type]


def test_shadow_config_is_strict_deep_immutable_and_loads_yaml() -> None:
    config = _config()
    assert isinstance(config.source_weights, MappingProxyType)
    with pytest.raises(TypeError):
        config.source_weights["chat"] = 1.0  # type: ignore[index]
    with pytest.raises(ValueError, match="max_recent_records"):
        _config(max_recent_records=True)
    with pytest.raises(ValueError, match="tick_seconds"):
        _config(tick_seconds="1.0")
    with pytest.raises(ValueError, match="source_weights.chat"):
        _config(source_weights={**dict(config.source_weights), "chat": float("inf")})
    with pytest.raises(ValueError, match="source_priority"):
        _config(source_priority=("chat",) * 7)

    class Loader:
        def get(self, *_args: object) -> dict[str, object]:
            return {
                "tick_seconds": "1.0",
                "max_recent_records": True,
                "max_candidates_per_source": 1,
                "max_evidence_refs": 1,
                "max_label_chars": 8,
                "source_weights": dict(config.source_weights),
                "source_priority": list(config.source_priority),
            }

    with pytest.raises(ValueError, match="tick_seconds"):
        DirectorV2ShadowConfig.from_loader(Loader())
    root = Path(__file__).parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    loaded = DirectorV2ShadowConfig.from_loader(loader)
    assert loaded.max_candidates_per_source == 4


def test_donation_selection_is_order_independent_and_requires_evidence() -> None:
    registry = _Registry({"READ_CHAT": _Availability(True)})
    a = DirectorV2Candidate(
        "chat", "a", "READ_CHAT", "READ_CHAT", score=1,
        evidence_refs=("chat:a",), is_donation=True,
    )
    b = DirectorV2Candidate(
        "chat", "b", "READ_CHAT", "READ_CHAT", score=1,
        evidence_refs=("chat:b",), is_donation=True,
    )
    first = _shadow(registry).propose(_context(candidates=(b, a)))
    second = _shadow(registry).propose(_context(candidates=(a, b)))
    bounded_shadow = DirectorV2Shadow(
        _config(max_candidates_per_source=1),
        capability_registry=registry, context_provider=lambda: _context(),
    )
    bounded = bounded_shadow.propose(_context(candidates=(
        DirectorV2Candidate("chat", "normal", "READ_CHAT", "READ_CHAT", score=100),
        b,
    )))
    invalid = _shadow(registry).propose(_context(candidates=(
        DirectorV2Candidate(
            "chat", "bad", "READ_CHAT", "READ_CHAT", is_donation=True,
        ),
    )))

    assert first == second
    assert first.candidate_id == "a"
    assert bounded.candidate_id == "b"
    assert invalid.action_type == "WAIT"
    assert invalid.reason_codes == ("donation_candidate_invalid",)


def test_duplicate_overflow_and_long_label_fail_closed_to_wait() -> None:
    registry = _Registry({"READ_CHAT": _Availability(True)})
    duplicate = DirectorV2Candidate("chat", "same", "READ_CHAT", "READ_CHAT")
    duplicate_result = _shadow(registry).propose(
        _context(candidates=(duplicate, duplicate)),
    )
    overflow_shadow = DirectorV2Shadow(
        _config(max_candidates_per_source=1),
        capability_registry=registry, context_provider=lambda: _context(),
    )
    overflow_result = overflow_shadow.propose(_context(candidates=tuple(
        DirectorV2Candidate("chat", f"chat-{index}", "READ_CHAT", "READ_CHAT")
        for index in range(8)
    )))
    long_result = _shadow(registry).propose(_context(candidates=(
        DirectorV2Candidate("chat", "x" * 65, "READ_CHAT", "READ_CHAT"),
    )))

    assert duplicate_result.reason_codes == ("candidate_duplicate",)
    assert overflow_result.reason_codes == ("candidate_total_overflow",)
    assert long_result.reason_codes == ("candidate_label_overflow",)


def test_action_capability_mismatch_and_malformed_availability_fail_closed() -> None:
    mismatch = _shadow(_Registry({"READ_CHAT": _Availability(True)})).propose(
        _context(candidates=(DirectorV2Candidate(
            "capability", "mismatch", "CALL_GUEST", "READ_CHAT", score=10,
        ),)),
    )

    class MalformedRegistry(_Registry):
        def availability(self, capability_id: str) -> object:
            return {"available": "false", "reason_code": "available"}

    malformed = _shadow(MalformedRegistry({"READ_CHAT": _Availability(True)})).propose(
        _context(candidates=(DirectorV2Candidate(
            "chat", "chat-1", "READ_CHAT", "READ_CHAT", score=10,
        ),)),
    )

    assert mismatch.action_type == "WAIT"
    assert mismatch.reason_codes[-1] == "capability_action_mismatch"
    assert malformed.reason_codes[-1] == "capability_availability_malformed"


def test_hard_hold_precedes_source_failure_and_delivered_conflict() -> None:
    shadow = _shadow(_Registry({}))
    emergency = shadow.propose(_context(
        emergency=True, operator_hold=True, source_failures=("world",),
    ))
    delivered = shadow.propose(_context(transaction_conflict=True))
    source_failed = shadow.propose(_context(source_failures=("capability", "world")))

    assert emergency.reason_codes == ("emergency",)
    assert delivered.reason_codes == ("transaction_conflict",)
    assert source_failed.reason_codes == ("source_capability_failed",)


def test_metrics_failure_is_isolated_and_repeated_proposals_are_retained() -> None:
    shadow = _shadow(_Registry({"READ_CHAT": _Availability(True)}))

    class BrokenMetrics:
        def record_director_v2_shadow(self, _outcome: str, _retained: int) -> None:
            raise RuntimeError("metrics unavailable")

    shadow._metrics = BrokenMetrics()
    context = _context(candidates=(
        DirectorV2Candidate("chat", "chat-1", "READ_CHAT", "READ_CHAT"),
    ))
    first = shadow.propose(context)
    second = shadow.propose(context)

    assert first == second
    assert len(shadow.snapshot()["recent"]) == 2  # type: ignore[arg-type]
    assert shadow.get_metrics()["director_v2_shadow_outcomes"]["selected"] == 2


def test_context_provider_failure_and_disabled_feature_are_side_effect_free() -> None:
    calls = 0

    def failed_provider() -> DirectorV2Context:
        nonlocal calls
        calls += 1
        raise RuntimeError("context unavailable")

    shadow = DirectorV2Shadow(
        _config(), capability_registry=_Registry({}),
        context_provider=failed_provider, clock=lambda: 100.0,
    )
    failed = shadow.propose_current()
    assert failed.reason_codes == ("source_context_failed",)
    assert calls == 1

    shadow.set_enabled(False)
    disabled = shadow.propose_current()
    assert disabled.reason_codes == ("feature_disabled",)
    assert calls == 1
    assert shadow.snapshot()["recent"] == []


def test_worker_lifecycle_and_health_are_fail_closed() -> None:
    shadow = _shadow(_Registry({}))

    async def scenario() -> tuple[HealthState, HealthState, HealthState]:
        before = (await shadow.health_check()).state
        await shadow.start()
        await asyncio.sleep(0)
        running = (await shadow.health_check()).state
        assert shadow._task is not None
        shadow._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shadow._task
        dead = (await shadow.health_check()).state
        await shadow.stop()
        return before, running, dead

    assert asyncio.run(scenario()) == (
        HealthState.STOPPED, HealthState.HEALTHY, HealthState.DEGRADED,
    )


def test_snapshot_identity_is_stable_and_content_sensitive() -> None:
    first = director_v2_snapshot_id(
        "capabilities", [{"id": "A", "available": True}],
    )
    reordered = director_v2_snapshot_id(
        "capabilities", [{"available": True, "id": "A"}],
    )
    changed = director_v2_snapshot_id(
        "capabilities", [{"id": "A", "available": False}],
    )

    assert first == reordered
    assert first != changed
    with pytest.raises(ValueError):
        director_v2_snapshot_id("capabilities", {"score": float("nan")})


def test_runtime_context_uses_strict_holds_and_stable_projection_identity() -> None:
    source = (
        Path(__file__).parents[2] / "orchestrator" / "stream_runtime.py"
    ).read_text(encoding="utf-8")
    context_source = source[
        source.index("def _director_v2_context()"):
        source.index("director_v2_shadow = DirectorV2Shadow(")
    ]

    assert '"delivered"' in context_source
    assert "emergency_controller.snapshot()" in context_source
    assert "control_plane.paused" in context_source
    assert "director_v2_snapshot_id(" in context_source
    assert "available_count" not in context_source
