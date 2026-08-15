from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from interfaces.llm import LLMToken
from services.agent.agenda_policy import AgendaPolicy, AgendaPolicyConfig
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.goal_proposal import GoalProposalGenerator
from services.agent.goal_types import GoalKind, GoalSource
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    AgentStateSnapshot,
    EventProvenance,
    GroundedEvent,
    OpenThread,
)

NOW = datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc)


class FakeLLM:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list[Any] = []

    async def generate_stream(self, request):
        self.requests.append(request)
        yield LLMToken(request_id=request.request_id, token=self.output, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


def _state() -> AgentStateSnapshot:
    event = GroundedEvent(
        event_id="chat-1", kind=AgentEventKind.CHAT_RECEIVED,
        source=AgentEventSource.CHAT, timestamp=NOW, confidence=1.0,
        payload={"text": "Kể tiếp chuyện cà phê đi"},
        provenance=EventProvenance("test", source_event_id="chat-1"),
    )
    thread = OpenThread(
        thread_id="thread-1", topic="cà phê", summary="chuyện cà phê",
        created_at=NOW, updated_at=NOW, expires_at=NOW + timedelta(minutes=5),
    )
    return AgentStateSnapshot(recent_events=(event,), open_threads=(thread,))


def _generator(output: str, *, enabled: bool = True) -> tuple[GoalProposalGenerator, FakeLLM]:
    llm = FakeLLM(output)
    return GoalProposalGenerator(
        llm, "strict prompt", allowed_kinds=(GoalKind.CONTINUE_THREAD,),
        evidence_max_items=3, max_tokens=100, temperature=0.1,
        max_reason_chars=80, enabled=enabled,
    ), llm


def _manager() -> GoalManager:
    config = AgendaPolicyConfig(
        priorities={kind: 40 for kind in GoalKind},
        ttl_seconds={kind: 60 for kind in GoalKind},
    )
    return GoalManager(
        GoalLimits(8, 4, 8, 160), clock=lambda: NOW,
        agenda_policy=AgendaPolicy(config, clock=lambda: NOW),
    )


async def test_disabled_feature_never_calls_llm() -> None:
    generator, llm = _generator("{}", enabled=False)
    assert await generator.propose(_state()) is None
    assert llm.requests == []
    assert generator.get_metrics()["goal_proposals_rejected_total"] == 1


async def test_valid_strict_proposal_is_validated_and_accepted_by_manager() -> None:
    output = json.dumps({
        "kind": "continue_thread",
        "reason": "continue grounded coffee thread",
        "success_condition": "address thread in speech",
        "source_event_id": "chat-1",
        "parent_thread_id": "thread-1",
    })
    generator, llm = _generator(output)
    proposal = await generator.propose(_state())
    assert proposal is not None
    assert _manager().accept_proposal(proposal, _state())
    manager = _manager()
    assert manager.accept_proposal(proposal, _state())
    active = manager.snapshot().active
    assert active and active.source is GoalSource.LLM_PROPOSAL
    assert active.priority == 40  # policy owns priority; LLM schema has no priority
    request_evidence = json.loads(llm.requests[0].messages[1].content)
    assert request_evidence["events"][0]["event_id"] == "chat-1"


@pytest.mark.parametrize("output", [
    "not json",
    "[]",
    json.dumps({
        "kind": "continue_thread", "reason": "x", "success_condition": "y",
        "source_event_id": "chat-1", "parent_thread_id": "thread-1", "priority": 999,
    }),
    json.dumps({
        "kind": "ack_donation", "reason": "x", "success_condition": "y",
        "source_event_id": "chat-1", "parent_thread_id": None,
    }),
])
async def test_malformed_extra_field_or_disallowed_kind_is_rejected(output: str) -> None:
    generator, _llm = _generator(output)
    assert await generator.propose(_state()) is None


async def test_unknown_event_or_thread_is_rejected_by_manager() -> None:
    output = json.dumps({
        "kind": "continue_thread", "reason": "x", "success_condition": "y",
        "source_event_id": "invented", "parent_thread_id": "invented",
    })
    generator, _llm = _generator(output)
    proposal = await generator.propose(_state())
    assert proposal is not None
    assert not _manager().accept_proposal(proposal, _state())


async def test_service_lifecycle_and_runtime_toggle() -> None:
    generator, _llm = _generator("{}", enabled=False)
    assert not (await generator.health_check()).is_ok
    await generator.start()
    generator.set_enabled(True)
    assert (await generator.health_check()).is_ok
    assert generator.enabled
    await generator.stop()
