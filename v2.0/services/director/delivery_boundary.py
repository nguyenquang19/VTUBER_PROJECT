"""Delivery boundary for Director-generated turns.

The boundary owns deferred runner finalization and delivery state transitions.
Business commit/release remains in :mod:`services.director.director_loop`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from interfaces.animation import AnimationCommand, MoodState
from interfaces.compatibility import ActionRequest, ActionStatus

SpeakFn = Callable[[str, str], Awaitable[Any]]
MoodProvider = Callable[[], MoodState]
SpeechCompletedFn = Callable[..., None]
FilterRejectedFn = Callable[..., None]


class DirectorDeliveryBoundary:
    """Deliver one generated turn without owning its business transaction."""

    def __init__(
        self,
        *,
        runner: Any,
        speak: SpeakFn | None,
        mood_provider: MoodProvider,
        speech_completed: SpeechCompletedFn,
        filter_rejected: FilterRejectedFn,
        logger: Any,
        transactions: Any = None,
        animation: Any = None,
        embodiment_policy: Any = None,
        action_adapter_boundary: Any = None,
    ) -> None:
        self._runner = runner
        self._speak = speak
        self._transactions = transactions
        self._animation = animation
        self._embodiment_policy = embodiment_policy
        self._action_adapter_boundary = action_adapter_boundary
        self._mood_provider = mood_provider
        self._speech_completed = speech_completed
        self._filter_rejected = filter_rejected
        self._log = logger

    async def deliver(
        self,
        request_id: str,
        parsed: Any,
        action: Any,
        refs: list[Any],
        *,
        goal_id: str | None = None,
        intention_id: str | None = None,
        transaction_id: str | None = None,
        thread_id: str | None = None,
        conversation_move: str | None = None,
    ) -> bool:
        """Attempt delivery and finalize only delivery-related state."""
        # ``ok`` measures parse/model quality for datasets. A canned fallback may
        # intentionally have ok=False while its text is still deliverable.
        text = getattr(parsed, "text", "")
        if not text:
            self.finalize_runner_delivery(request_id, False)
            return False

        verdict = getattr(self._runner, "last_filter_verdict", None)
        if verdict is not None and getattr(verdict, "passed", True) is not True:
            self.finalize_runner_delivery(request_id, False)
            self._filter_rejected(refs=refs, thread_id=thread_id, goal_id=goal_id)
            self._log.warning(
                "director_filter_rejected",
                request_id=request_id,
                action=str(getattr(verdict, "suggested_action", "unknown")),
                categories=[
                    str(getattr(category, "value", category))
                    for category in getattr(verdict, "categories_hit", ())
                ],
            )
            return False

        if goal_id is not None and intention_id is None:
            self.finalize_runner_delivery(request_id, False)
            self._log.warning(
                "director_goal_intention_missing",
                goal_id=goal_id,
                request_id=request_id,
            )
            return False

        if transaction_id is not None:
            self._transactions.mark_generated(transaction_id)
            self._transactions.mark_delivering(transaction_id)
        use_action_adapter = (
            self._action_adapter_boundary is not None
            and getattr(self._action_adapter_boundary, "speech_enabled", None) is True
        )
        if use_action_adapter:
            action_type = self._speech_action_type(action)
            request = ActionRequest(
                schema_version=1,
                action_id=request_id,
                capability_id=action_type,
                action_type=action_type,
                target=None,
                arguments={"text": text},
                intention_id=intention_id,
                evidence_refs=(f"delivery:{request_id}",),
                idempotency_key=f"speech:{request_id}",
                priority=0.0,
                requested_at=datetime.now(timezone.utc),
                transaction_policy="delivery_aware",
            )
            try:
                result = await self._action_adapter_boundary.execute(request)
            except Exception as exc:
                self.finalize_runner_delivery(request_id, False)
                self._log.warning("director_speech_adapter_failed", error=str(exc))
                return False
            if not (
                getattr(result, "action_id", None) == request_id
                and getattr(result, "status", None) is ActionStatus.SUCCESS
                and getattr(result, "verified", None) is True
                and getattr(result, "verification_source", None) == "tts_delivery"
            ):
                self.finalize_runner_delivery(request_id, False)
                self._log.warning(
                    "director_speech_adapter_unverified",
                    error_code=str(getattr(result, "error_code", "invalid_result")),
                )
                return False
        else:
            # Exact compatibility path when typed speech adaptation is disabled.
            if self._speak is None:
                self.finalize_runner_delivery(request_id, False)
                self._log.warning("director_delivery_sink_missing", request_id=request_id)
                return False
            try:
                delivery = await self._speak(request_id, text)
            except Exception as exc:
                self.finalize_runner_delivery(request_id, False)
                self._log.warning("director_speak_failed", error=str(exc))
                return False
            if getattr(delivery, "delivered", False) is not True:
                self.finalize_runner_delivery(request_id, False)
                self._log.warning(
                    "director_delivery_not_reached",
                    mode=str(getattr(delivery, "mode", "unknown")),
                )
                return False

        if transaction_id is not None:
            self._transactions.mark_delivered(transaction_id)
        self.finalize_runner_delivery(request_id, True)
        self._speech_completed(
            request_id,
            action,
            refs,
            goal_id=goal_id,
            intention_id=intention_id,
            text=text,
            thread_id=thread_id,
            conversation_move=conversation_move,
        )
        if self._embodiment_policy is not None and bool(getattr(self._embodiment_policy, "enabled", False)):
            try:
                await self._embodiment_policy.apply_mid(request_id, self._mood_provider())
            except asyncio.CancelledError:
                self._log.warning("embodiment_mid_cancelled_after_delivery")
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("embodiment_mid_failed", error=str(exc))
        elif self._animation is not None:
            try:
                await self._animation.express(
                    AnimationCommand(command_type="express", mood=self._mood_provider()),
                )
            except asyncio.CancelledError:
                self._log.warning("animation_express_cancelled_after_delivery")
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("animation_express_failed", error=str(exc))
        return True

    @staticmethod
    def _speech_action_type(action: Any) -> str:
        value = getattr(action, "value", action)
        if value == "self_talk":
            return "SELF_TALK"
        if value in {
            "follow_up", "continue_thread", "ask_follow_up", "share_goal_progress",
        }:
            return "FOLLOW_UP"
        return "SPEAK"

    async def run_turn_deferred(self, **kwargs: Any) -> Any:
        if hasattr(self._runner, "finalize_delivery"):
            kwargs["defer_delivery_commit"] = True
        return await self._runner.run_turn(**kwargs)

    async def run_ambient_deferred(self, request_id: str, prompt: str) -> Any:
        if hasattr(self._runner, "finalize_delivery"):
            return await self._runner.run_ambient_turn(
                request_id, prompt, defer_delivery_commit=True,
            )
        return await self._runner.run_ambient_turn(request_id, prompt)

    async def run_directed_deferred(self, request_id: str, context: str) -> Any:
        if hasattr(self._runner, "finalize_delivery"):
            return await self._runner.run_directed_turn(
                request_id, context, defer_delivery_commit=True,
            )
        return await self._runner.run_directed_turn(request_id, context)

    def finalize_runner_delivery(self, request_id: str, success: bool) -> None:
        finalize = getattr(self._runner, "finalize_delivery", None)
        if callable(finalize):
            finalize(request_id, success)
