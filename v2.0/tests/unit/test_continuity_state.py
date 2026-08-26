from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from interfaces.execution import OutcomeCommit, OutcomeDisposition
from interfaces.state import (
    ContinuityCommitDisposition,
    DeliveredTurnRecord,
)
from services.memory.extractor import MemoryExtractor
from services.state.continuity import ContinuityCommitter, ContinuityConfig


NOW = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)


class _State:
    def __init__(self) -> None:
        self.events = []
        self.agent = SimpleNamespace(open_threads=(), active_goal_ref=None)

    def apply(self, event) -> bool:
        self.events.append(event)
        return True

    def snapshot(self):
        return SimpleNamespace(agent=self.agent, goals=None)


class _History:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.self_talk: list[str] = []

    def commit_turn(self, user: str, assistant: str) -> None:
        self.turns.append((user, assistant))

    def commit_self_talk(self, text: str) -> None:
        self.self_talk.append(text)


class _Goals:
    def __init__(self) -> None:
        self.focused = []

    def focus_delivered_thread(self, parent_id, **kwargs) -> int:
        self.focused.append((parent_id, kwargs))
        return 1


def _config(*, pending: int = 4) -> ContinuityConfig:
    return ContinuityConfig(
        max_records=8,
        dedup_ttl_s=300,
        max_speech_age_s=300,
        max_text_chars=512,
        max_evidence_refs=8,
        max_pending_memory_writes=pending,
        memory_write_timeout_s=1.0,
        allowed_memory_scopes=("session", "viewer"),
        self_talk_history_actions=("self_talk", "follow_up", "transition"),
    )


def _outcome(disposition: OutcomeDisposition = OutcomeDisposition.COMMITTED) -> OutcomeCommit:
    return OutcomeCommit(
        schema_version=1,
        outcome_ref="outcome:verified",
        execution_id="execution:1",
        transaction_id="transaction:1",
        disposition=disposition,
        reason_code="verified_committed",
        evidence_refs=("delivery:req-1",),
        completed_at=NOW,
    )


def _record(**overrides) -> DeliveredTurnRecord:
    values = {
        "schema_version": 1,
        "continuity_id": "continuity:1",
        "outcome_ref": "outcome:verified",
        "transaction_id": "transaction:1",
        "delivery_id": "req-1",
        "session_id": "session-1",
        "source_mode": "chat",
        "action_type": "read_chat",
        "speech_text": "Tớ nhớ rồi, lần sau mình nói tiếp nhé.",
        "history_input": "Tôi thích cà phê rang đậm.",
        "ref_event_ids": ("chat-1",),
        "goal_id": None,
        "intention_id": None,
        "thread_id": None,
        "conversation_move": None,
        "viewer_ref": "viewer-hash",
        "trigger_type": "youtube",
        "output_ok": True,
        "mood_dominant": "vui",
        "mood_intensity": 4,
        "delivered_at": NOW,
        "evidence_refs": ("outcome:verified", "delivery:req-1"),
    }
    values.update(overrides)
    return DeliveredTurnRecord(**values)


@pytest.mark.asyncio
async def test_committed_turn_projects_each_continuity_facet_once() -> None:
    state = _State()
    history = _History()
    goals = _Goals()

    class Memory:
        def __init__(self) -> None:
            self.entries = []

        async def write(self, entry) -> None:
            self.entries.append(entry)

    memory = Memory()
    service = ContinuityCommitter(
        _config(),
        authoritative_state=state,
        prompt_history=history,
        goal_manager=goals,
        memory=memory,
        memory_extractor=MemoryExtractor(),
        clock=lambda: NOW,
    )
    await service.start()

    receipt = service.commit_verified(_outcome(), _record())
    assert receipt.disposition is ContinuityCommitDisposition.COMMITTED
    assert history.turns == [(
        "Tôi thích cà phê rang đậm.",
        "Tớ nhớ rồi, lần sau mình nói tiếp nhé.",
    )]
    assert [event.event_type for event in state.events] == [
        "speech_final", "speech_completed",
    ]
    assert len(goals.focused) == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(memory.entries) == 1
    assert memory.entries[0].metadata["outcome_id"] == "outcome:verified"
    assert service.recent() == (_record(),)
    snapshot_text = str(service.snapshot())
    assert "viewer-hash" not in snapshot_text
    assert "Tớ nhớ rồi" not in snapshot_text

    duplicate = service.commit_verified(_outcome(), _record())
    assert duplicate.disposition is ContinuityCommitDisposition.DUPLICATE
    await asyncio.sleep(0)
    assert len(history.turns) == 1
    assert len(state.events) == 2
    assert len(memory.entries) == 1
    await service.stop()


def test_released_outcome_cannot_create_continuity_fact() -> None:
    service = ContinuityCommitter(
        _config(), authoritative_state=_State(), prompt_history=_History(), clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="committed outcome"):
        service.commit_verified(_outcome(OutcomeDisposition.RELEASED), _record())
    assert service.recent() == ()


def test_same_id_with_changed_payload_is_inconsistent_without_second_write() -> None:
    history = _History()
    service = ContinuityCommitter(
        _config(), authoritative_state=_State(), prompt_history=history, clock=lambda: NOW,
    )
    assert service.commit_verified(
        _outcome(), _record(),
    ).disposition is ContinuityCommitDisposition.COMMITTED
    receipt = service.commit_verified(
        _outcome(), _record(speech_text="Một câu khác."),
    )
    assert receipt.disposition is ContinuityCommitDisposition.INCONSISTENT
    assert receipt.failed_facets == ("idempotency_mismatch",)
    assert len(history.turns) == 1


def test_projection_failure_returns_inconsistency_without_losing_delivered_fact() -> None:
    state = _State()

    def apply_with_completed_failure(event) -> bool:
        state.events.append(event)
        return event.event_type != "speech_completed"

    state.apply = apply_with_completed_failure
    service = ContinuityCommitter(
        _config(), authoritative_state=state, prompt_history=_History(), clock=lambda: NOW,
    )
    receipt = service.commit_verified(_outcome(), _record())
    assert receipt.disposition is ContinuityCommitDisposition.INCONSISTENT
    assert receipt.failed_facets == ("speech_completed",)
    assert service.recent() == (_record(),)
    assert service.get_metrics()["continuity_inconsistency_total"] == 1


@pytest.mark.asyncio
async def test_memory_queue_is_bounded_and_owned_by_continuity_service() -> None:
    blocker = asyncio.Event()

    class Memory:
        async def write(self, _entry) -> None:
            await blocker.wait()

    service = ContinuityCommitter(
        _config(pending=1),
        authoritative_state=_State(),
        prompt_history=_History(),
        memory=Memory(),
        memory_extractor=MemoryExtractor(),
        clock=lambda: NOW,
    )
    await service.start()
    service.commit_verified(_outcome(), _record())
    assert service.get_metrics()["continuity_memory_pending"] == 1
    await service.stop()
    assert service.get_metrics()["continuity_memory_pending"] == 0
