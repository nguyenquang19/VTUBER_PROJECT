"""Deterministic opportunity mapping at the Director observation tap."""
from __future__ import annotations

from services.agent.goal_types import GoalSnapshot
from services.agent.types import AgentStateSnapshot
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.director import DirectorAction, DirectorDecision, ReadMode
from orchestrator.cognitive_observer import CognitiveDirectorObserver
from tests.unit.test_cognitive_brain_shadow import _config


class _Scheduler:
    def __init__(self) -> None:
        self.offers = []
        self.preemptions = 0

    def offer(self, value) -> bool:
        self.offers.append(value)
        return True

    def preempt_for_live(self) -> None:
        self.preemptions += 1


def _input(now: float = 1_777_000_000.0) -> DirectorInput:
    return DirectorInput(
        now=now, agent_state=AgentStateSnapshot(), goals=GoalSnapshot(),
    )


def test_idle_heartbeat_never_becomes_a_brain_opportunity() -> None:
    scheduler = _Scheduler()
    observer = CognitiveDirectorObserver(
        config=_config(), scheduler=scheduler, session_id="session-real",
    )
    decision = DirectorDecision(DirectorAction.WAIT, "main", "idle")
    for _ in range(100):
        assert observer.observe_decision(decision, _input(), None) is False
    assert scheduler.offers == []


def test_chat_and_donation_keep_authoritative_event_and_runtime_session_identity() -> None:
    scheduler = _Scheduler()
    observer = CognitiveDirectorObserver(
        config=_config(), scheduler=scheduler, session_id="session-real",
    )
    chat = DirectorChatRef(
        msg_id="input-1", text="Mai ơi", kind="chat", score=20,
        created_at=1_777_000_000.0, is_super=True,
    )
    decision = DirectorDecision(
        DirectorAction.ACK_DONATION, "main", "superchat_priority",
        refs=(chat,), read_mode=ReadMode.ACK,
    )
    assert observer.observe_decision(decision, _input(), "decision-1") is True
    opportunity = scheduler.offers[0]
    assert opportunity.kind.value == "DONATION_OR_OPERATOR"
    assert opportunity.material_change_ref == "agent:chat:input-1"
    assert opportunity.context_request.trigger_event_ref == "agent:chat:input-1"
    assert opportunity.context_request.session_id == "session-real"
    assert opportunity.compatibility.decision_ref == "decision-1"


def test_chat_activity_only_requests_shadow_preemption() -> None:
    scheduler = _Scheduler()
    observer = CognitiveDirectorObserver(config=_config(), scheduler=scheduler)
    observer.preempt_for_live()
    assert scheduler.preemptions == 1
