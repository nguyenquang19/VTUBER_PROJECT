"""Typed executable materialization for strict Director V2 primary ownership."""
from __future__ import annotations

from dataclasses import replace

from interfaces.director_v2 import DirectorV2Proposal
from services.agent.goal_types import Goal, GoalKind
from services.director.action_types import DirectorChatRef, DirectorInput
from services.director.director import (
    Director,
    DirectorAction,
    DirectorDecision,
    ReadMode,
    Segment,
)


class DirectorV2MaterializationError(ValueError):
    """Proposal was valid for selection but cannot form a safe live payload."""


class DirectorV2DecisionMaterializer:
    """Create one immutable DirectorDecision without side effects."""

    def __init__(self, director: Director) -> None:
        if not isinstance(director, Director):
            raise ValueError("director must be Director")
        self._director = director

    def materialize(
        self, proposal: DirectorV2Proposal, value: DirectorInput,
    ) -> DirectorDecision:
        if not isinstance(proposal, DirectorV2Proposal):
            raise DirectorV2MaterializationError("proposal_malformed")
        if not isinstance(value, DirectorInput):
            raise DirectorV2MaterializationError("director_input_malformed")
        segment = self._director.current_segment()
        action = proposal.action_type
        if action == "WAIT":
            if proposal.candidate_id != "wait" or proposal.capability_id != "WAIT":
                raise DirectorV2MaterializationError("wait_identity_invalid")
            return self._owned(
                proposal,
                DirectorDecision(DirectorAction.WAIT, segment.name, "v2_primary_wait"),
            )
        if action == "READ_CHAT":
            return self._materialize_chat(proposal, value, segment)
        if action == "SELF_TALK":
            return self._materialize_self_talk(proposal, value, segment)
        if action == "FOLLOW_UP":
            return self._materialize_follow_up(proposal, value, segment)
        raise DirectorV2MaterializationError("proposal_action_unsupported")

    def _materialize_chat(
        self, proposal: DirectorV2Proposal, value: DirectorInput, segment: Segment,
    ) -> DirectorDecision:
        ref = next(
            (item for item in value.chat_candidates
             if item.msg_id == proposal.candidate_id),
            None,
        )
        if ref is None:
            raise DirectorV2MaterializationError("chat_evidence_missing")
        if ref.is_super:
            if "ack_donation" not in segment.allowed_actions:
                raise DirectorV2MaterializationError("action_not_allowed")
            decision = DirectorDecision(
                DirectorAction.ACK_DONATION,
                segment.name,
                "v2_primary_donation",
                refs=(ref,),
                read_mode=ReadMode.ACK,
                goal_id=self._matching_donation_goal(value.goals.active, ref),
            )
        else:
            if "read_chat" not in segment.allowed_actions:
                raise DirectorV2MaterializationError("action_not_allowed")
            decision = DirectorDecision(
                DirectorAction.READ_CHAT,
                segment.name,
                "v2_primary_chat",
                refs=(ref,),
                read_mode=ReadMode.SINGLE,
            )
        return self._owned(proposal, decision)

    def _materialize_self_talk(
        self, proposal: DirectorV2Proposal, value: DirectorInput, segment: Segment,
    ) -> DirectorDecision:
        if proposal.candidate_id != "urge":
            raise DirectorV2MaterializationError("proactive_evidence_missing")
        if not value.urge_ready or not value.self_talk_ready or value.safety_hold:
            raise DirectorV2MaterializationError("self_talk_not_ready")
        if "self_talk" not in segment.allowed_actions:
            raise DirectorV2MaterializationError("action_not_allowed")
        return self._owned(
            proposal,
            DirectorDecision(
                DirectorAction.SELF_TALK,
                segment.name,
                "v2_primary_proactive",
                proactive_source="urge",
                proactive_source_id="urge",
                proactive_category="urge",
                proactive_evidence_ids=proposal.evidence_refs,
            ),
        )

    def _materialize_follow_up(
        self, proposal: DirectorV2Proposal, value: DirectorInput, segment: Segment,
    ) -> DirectorDecision:
        goal = value.goals.active
        if goal is not None and goal.goal_id == proposal.candidate_id:
            return self._owned(
                proposal, self._goal_decision(goal, value, segment),
            )
        thread = next(
            (item for item in value.agent_state.open_threads
             if item.thread_id == proposal.candidate_id),
            None,
        )
        if thread is None:
            raise DirectorV2MaterializationError("thread_goal_evidence_missing")
        if "follow_up" not in segment.allowed_actions:
            raise DirectorV2MaterializationError("action_not_allowed")
        evidence_ids = tuple(
            item.source_event_id for item in thread.evidence if item.source_event_id
        )
        return self._owned(
            proposal,
            DirectorDecision(
                DirectorAction.FOLLOW_UP,
                segment.name,
                "v2_primary_thread",
                proactive_source="open_thread",
                proactive_source_id=thread.thread_id,
                proactive_category=thread.kind.value,
                proactive_evidence_ids=evidence_ids,
                proactive_summary=thread.summary,
            ),
        )

    def _goal_decision(
        self, goal: Goal, value: DirectorInput, segment: Segment,
    ) -> DirectorDecision:
        allowed = segment.allowed_actions
        if goal.kind is GoalKind.ANSWER_FOLLOW_UP:
            event_id = str(goal.metadata.get("chat_event_id") or "")
            ref = next(
                (item for item in value.chat_candidates
                 if self._same_event(item.msg_id, event_id)),
                None,
            )
            if ref is None or "read_chat" not in allowed:
                raise DirectorV2MaterializationError("follow_up_evidence_missing")
            return DirectorDecision(
                DirectorAction.READ_CHAT, segment.name, "v2_primary_answer_follow_up",
                refs=(ref,), read_mode=ReadMode.SINGLE, goal_id=goal.goal_id,
            )
        intention = value.goals.current_intention
        if intention is None or intention.goal_id != goal.goal_id:
            raise DirectorV2MaterializationError("goal_intention_missing")
        if goal.kind is GoalKind.CONTINUE_THREAD:
            thread_exists = any(
                item.thread_id == goal.parent_thread_id
                for item in value.agent_state.open_threads
            )
            if not thread_exists or "continue_thread" not in allowed:
                raise DirectorV2MaterializationError("thread_or_action_missing")
            return DirectorDecision(
                DirectorAction.CONTINUE_THREAD, segment.name,
                "v2_primary_continue_thread", goal_id=goal.goal_id,
            )
        if goal.kind is GoalKind.WAIT_FOR_CHAT_ANSWER:
            remaining = goal.expires_at.timestamp() - value.now
            if (
                goal.metadata.get("follow_up_asked")
                or remaining > self._director.ask_follow_up_before_expiry_seconds
                or "ask_follow_up" not in allowed
            ):
                raise DirectorV2MaterializationError("follow_up_not_due")
            return DirectorDecision(
                DirectorAction.ASK_FOLLOW_UP, segment.name,
                "v2_primary_ask_follow_up", goal_id=goal.goal_id,
            )
        if goal.kind is GoalKind.OPERATOR_PINNED:
            if goal.metadata.get("progress_shared") or "share_goal_progress" not in allowed:
                raise DirectorV2MaterializationError("goal_progress_not_due")
            return DirectorDecision(
                DirectorAction.SHARE_GOAL_PROGRESS, segment.name,
                "v2_primary_goal_progress", goal_id=goal.goal_id,
            )
        raise DirectorV2MaterializationError("goal_kind_unsupported")

    @staticmethod
    def _owned(
        proposal: DirectorV2Proposal, decision: DirectorDecision,
    ) -> DirectorDecision:
        return replace(
            decision,
            decision_owner="director_v2",
            director_v2_proposal_id=proposal.proposal_id,
        )

    @staticmethod
    def _same_event(message_id: str, event_id: str) -> bool:
        return bool(event_id) and (
            message_id == event_id or event_id.endswith(f":{message_id}")
        )

    @classmethod
    def _matching_donation_goal(
        cls, goal: Goal | None, ref: DirectorChatRef,
    ) -> str | None:
        if goal is None or goal.kind is not GoalKind.ACK_DONATION:
            return None
        source_event_id = str(goal.metadata.get("source_event_id") or "")
        if not source_event_id or cls._same_event(ref.msg_id, source_event_id):
            return goal.goal_id
        return None
