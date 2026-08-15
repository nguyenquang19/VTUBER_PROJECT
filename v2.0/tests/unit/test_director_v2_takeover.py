"""Safety gates for controlled Director V2 conversational takeover."""
from __future__ import annotations

from interfaces.director_v2 import DirectorV2Proposal
from services.director.v2_takeover import DirectorV2Takeover, DirectorV2TakeoverConfig


def _config(stage: str = "READ_CHAT") -> DirectorV2TakeoverConfig:
    return DirectorV2TakeoverConfig(
        stage=stage, max_recent_decisions=2, max_reason_chars=120,
        stage_order=("WAIT", "READ_CHAT", "SELF_TALK", "FOLLOW_UP", "SPEECH_SCHEDULING"),
        stage_actions={
            "WAIT": frozenset({"WAIT"}),
            "READ_CHAT": frozenset({"WAIT", "READ_CHAT", "ACK_DONATION"}),
            "SELF_TALK": frozenset({"WAIT", "READ_CHAT", "ACK_DONATION", "SELF_TALK"}),
            "FOLLOW_UP": frozenset({"WAIT", "READ_CHAT", "ACK_DONATION", "SELF_TALK", "FOLLOW_UP", "CONTINUE_THREAD", "ASK_FOLLOW_UP", "SHARE_GOAL_PROGRESS"}),
            "SPEECH_SCHEDULING": frozenset({"WAIT", "READ_CHAT", "ACK_DONATION", "SELF_TALK", "FOLLOW_UP", "CONTINUE_THREAD", "ASK_FOLLOW_UP", "SHARE_GOAL_PROGRESS"}),
        },
        action_aliases={"CONTINUE_THREAD": "FOLLOW_UP", "ASK_FOLLOW_UP": "FOLLOW_UP", "SHARE_GOAL_PROGRESS": "FOLLOW_UP"},
    )


def _proposal(action: str, candidate: str = "chat-1", reasons: tuple[str, ...] = ("selected", "validated")) -> DirectorV2Proposal:
    return DirectorV2Proposal("p1", 1.0, action, action, candidate, reasons)


def test_disabled_takeover_always_returns_legacy_fallback() -> None:
    selector = DirectorV2Takeover(_config(), enabled=False)
    result = selector.evaluate(legacy_action="READ_CHAT", proposal=_proposal("READ_CHAT"), evidence_ids=("chat-1",))

    assert result.accepted is False
    assert result.reason_code == "feature_disabled"


def test_read_chat_accepts_only_matching_proposal_and_same_tick_ref() -> None:
    selector = DirectorV2Takeover(_config(), enabled=True)

    accepted = selector.evaluate(legacy_action="READ_CHAT", proposal=_proposal("READ_CHAT"), evidence_ids=("chat-1",))
    missing = selector.evaluate(legacy_action="READ_CHAT", proposal=_proposal("READ_CHAT", "other"), evidence_ids=("chat-1",))
    mismatch = selector.evaluate(legacy_action="READ_CHAT", proposal=_proposal("SELF_TALK"), evidence_ids=("chat-1",))

    assert accepted.accepted is True
    assert missing.reason_code == "chat_evidence_missing"
    assert mismatch.reason_code == "action_mismatch"


def test_later_stages_are_blocked_and_aliases_require_thread_goal_evidence() -> None:
    selector = DirectorV2Takeover(_config("READ_CHAT"), enabled=True)
    blocked = selector.evaluate(legacy_action="SELF_TALK", proposal=_proposal("SELF_TALK"))
    follow = DirectorV2Takeover(_config("FOLLOW_UP"), enabled=True)
    accepted = follow.evaluate(legacy_action="CONTINUE_THREAD", proposal=_proposal("FOLLOW_UP", "thread-1"), evidence_ids=("thread-1",))

    assert blocked.reason_code == "stage_blocked"
    assert accepted.accepted is True


def test_rejected_capability_or_hard_hold_never_takes_over_and_records_are_bounded() -> None:
    selector = DirectorV2Takeover(_config(), enabled=True)
    capability = selector.evaluate(legacy_action="READ_CHAT", proposal=_proposal("READ_CHAT", reasons=("capability_permission_denied",)), evidence_ids=("chat-1",))
    held = selector.evaluate(legacy_action="WAIT", proposal=_proposal("WAIT", "wait", ("safety_hold",)))
    selector.evaluate(legacy_action="WAIT", proposal=_proposal("WAIT", "wait", ("selected", "wait")))

    assert capability.reason_code == "capability_rejected"
    assert held.reason_code == "hard_hold"
    assert len(selector.snapshot()["recent"]) == 2  # type: ignore[arg-type]
