from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from services.agent.conversation_move_planner import (
    ConversationMoveConfig, ConversationMovePlanner,
)
from services.agent.types import (
    ConversationMove, OpenThread, ThreadContribution, ThreadSpeaker, ThreadStatus,
)

NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _thread(**overrides) -> OpenThread:
    base = OpenThread(
        "thread-1", "cà phê", "đang bàn về cà phê",
        NOW, NOW, NOW + timedelta(minutes=5),
    )
    return replace(base, **overrides)


def test_move_policy_handles_wait_resume_compare_and_summary() -> None:
    planner = ConversationMovePlanner(ConversationMoveConfig(
        summarize_after_moves=4, invite_after_moves=2,
        compare_after_viewer_contributions=2,
    ))
    contribution = ThreadContribution("chat-1", "cà phê đậm", ThreadSpeaker.VIEWER)
    assert planner.choose(_thread(status=ThreadStatus.WAITING)) is ConversationMove.INVITE
    assert planner.choose(_thread(status=ThreadStatus.PARKED)) is ConversationMove.RESUME
    assert planner.choose(_thread(
        viewer_contributions=(contribution, replace(contribution, source_event_id="chat-2")),
    )) is ConversationMove.COMPARE
    assert planner.choose(_thread(move_count=4)) is ConversationMove.SUMMARIZE
    assert planner.choose(_thread(
        move_count=5, last_move=ConversationMove.SUMMARIZE,
    )) is ConversationMove.PARK


def test_move_policy_develops_without_repeating_same_stage_forever() -> None:
    planner = ConversationMovePlanner(ConversationMoveConfig())
    assert planner.choose(_thread()) is ConversationMove.DEEPEN
    assert planner.choose(_thread(last_move=ConversationMove.DEEPEN)) is ConversationMove.CLARIFY
    assert planner.choose(_thread(move_count=2)) is ConversationMove.INVITE
