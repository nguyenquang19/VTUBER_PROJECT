"""Delivery boundary for Director-generated turns.

The boundary owns deferred runner finalization and delivery state transitions.
Business commit/release remains in :mod:`services.director.director_loop`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from interfaces.animation import AnimationCommand, MoodState

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
    ) -> None:
        self._runner = runner
        self._speak = speak
        self._transactions = transactions
        self._animation = animation
        self._embodiment_policy = embodiment_policy
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

        if transaction_id is not None:
            self._transactions.mark_generated(transaction_id)
            self._transactions.mark_delivering(transaction_id)
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
            text=text,
            thread_id=thread_id,
            conversation_move=conversation_move,
        )
        if self._embodiment_policy is not None and bool(getattr(self._embodiment_policy, "enabled", False)):
            try:
                await self._embodiment_policy.apply_mid(request_id, self._mood_provider())
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("embodiment_mid_failed", error=str(exc))
        elif self._animation is not None:
            try:
                await self._animation.express(
                    AnimationCommand(command_type="express", mood=self._mood_provider()),
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("animation_express_failed", error=str(exc))
        return True

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
