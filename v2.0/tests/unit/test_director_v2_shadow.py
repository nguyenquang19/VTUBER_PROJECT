"""Deterministic and non-mutating checks for the Director V2 shadow."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from interfaces.director_v2 import DirectorV2Candidate, DirectorV2Context
from services.director.v2_shadow import DirectorV2Shadow, DirectorV2ShadowConfig


@dataclass(frozen=True)
class _Availability:
    available: bool
    reason_code: str = "available"


class _Registry:
    def __init__(self, values: dict[str, _Availability]) -> None:
        self.values = values

    def availability(self, capability_id: str) -> _Availability:
        return self.values.get(capability_id, _Availability(False, "unknown_capability"))


def _config(**changes: object) -> DirectorV2ShadowConfig:
    base: dict[str, object] = {
        "tick_seconds": 1.0,
        "max_recent_records": 2,
        "max_candidates_per_source": 2,
        "max_evidence_refs": 2,
        "max_label_chars": 32,
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
    return DirectorV2Shadow(_config(), capability_registry=registry, context_provider=lambda: _context())


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
        DirectorV2Candidate("chat", "donation", "READ_CHAT", "READ_CHAT", score=1, is_donation=True),
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
        DirectorV2Candidate("thread", "b", "SELF_TALK", "SELF_TALK", score=1, evidence_refs=("x", "x", "y")),
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