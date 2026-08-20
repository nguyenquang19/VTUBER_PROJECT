"""Conversation state machine with five states and nine transitions.

N1 YAGNI: đúng 5 state, 9 transition. Không LISTENING (gộp vào THINKING),
không INTERRUPTED (dùng flag `last_turn_interrupted`), không ERROR
(llm_fail transition thẳng THINKING → COOLDOWN + fallback).

Action thật (load context, start LLM, start TTS...) do caller đăng ký qua
`set_action()` — state machine không gọi thẳng service nào (N8).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from transitions.extensions.asyncio import AsyncMachine

from orchestrator.logger import get_logger

ActionHook = Callable[..., Awaitable[None]]
QueuePredicate = Callable[[], Awaitable[bool]]


class ConversationState(str, Enum):
    IDLE = "IDLE"           # Không có gì đang xảy ra
    THINKING = "THINKING"   # Nhận trigger, đang build context + LLM generate
    SPEAKING = "SPEAKING"   # TTS đang phát
    COOLDOWN = "COOLDOWN"   # Vừa nói xong, wait trước turn tiếp
    PAUSED = "PAUSED"       # Emergency stop hoặc manual pause


#: Tên các action hook mà caller có thể đăng ký (khớp cột Action ở bảng 7.10.2)
ACTION_NAMES = (
    "load_context_and_start_llm",
    "start_tts",
    "use_fallback_response",
    "finalize_turn",
    "stop_tts_graceful",
    "on_paused",
    "on_resumed",
)


@dataclass(frozen=True)
class StateTransition:
    """One schema-stable transition record in a turn log."""

    from_state: str
    to_state: str
    trigger: str
    at: str
    elapsed_ms: int
    turn_id: int | None = None
    interrupted: bool = False

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state,
            "to": self.to_state,
            "trigger": self.trigger,
            "at": self.at,
            "elapsed_ms": self.elapsed_ms,
        }


class ConversationStateMachine:
    """5 state / 9 transition. Trigger là coroutine: `await sm.first_token()`."""

    states = [s.value for s in ConversationState]

    def __init__(
        self,
        cooldown_ms: int = 500,
        event_bus: Any = None,
        history_limit: int = 200,
        auto_cooldown: bool = True,
        initial_state: str = ConversationState.IDLE.value,
    ) -> None:
        if initial_state not in ConversationStateMachine.states:
            raise ValueError(
                f"initial_state không hợp lệ: {initial_state}. "
                f"Hợp lệ: {ConversationStateMachine.states}"
            )
        self.cooldown_ms = cooldown_ms
        self.current_turn_id: int | None = None
        self.last_turn_interrupted = False
        self.state_entered_at = datetime.now(timezone.utc)
        self.previous_state: str | None = None

        self._event_bus = event_bus
        self._log = get_logger("state_machine")
        self._history: list[StateTransition] = []
        self._history_limit = history_limit
        self._auto_cooldown = auto_cooldown
        self._cooldown_task: asyncio.Task[None] | None = None
        self._actions: dict[str, ActionHook] = {}
        self._has_queued_trigger: QueuePredicate | None = None
        self._transition_counts: dict[tuple[str, str], int] = {}

        self.machine = AsyncMachine(
            model=self,
            states=ConversationStateMachine.states,
            initial=initial_state,
            transitions=self._get_transitions(),
            after_state_change="_on_state_change",
            send_event=True,
            auto_transitions=False,   # N1: không sinh to_IDLE()/to_PAUSED()... ngoài 9 transition
        )

    # ---------- transition table (7.10.2) ----------

    @staticmethod
    def _get_transitions() -> list[dict[str, Any]]:
        return [
            # From IDLE
            {"trigger": "trigger_received", "source": "IDLE", "dest": "THINKING",
             "conditions": "is_valid_trigger", "after": "_act_load_context_and_start_llm"},
            # From THINKING
            {"trigger": "first_token", "source": "THINKING", "dest": "SPEAKING",
             "after": "_act_start_tts"},
            {"trigger": "llm_fail", "source": "THINKING", "dest": "COOLDOWN",
             "after": "_act_use_fallback_response"},
            # From SPEAKING
            {"trigger": "tts_complete", "source": "SPEAKING", "dest": "COOLDOWN",
             "after": "_act_finalize_turn"},
            {"trigger": "interrupted", "source": "SPEAKING", "dest": "COOLDOWN",
             "after": "_act_stop_tts_graceful_and_flag"},
            # From COOLDOWN
            {"trigger": "cooldown_elapsed", "source": "COOLDOWN", "dest": "IDLE"},
            {"trigger": "queued_trigger_pending", "source": "COOLDOWN", "dest": "THINKING",
             "conditions": "has_queued_trigger", "after": "_act_load_context_and_start_llm"},
            # Emergency (từ mọi state)
            {"trigger": "emergency_stop", "source": "*", "dest": "PAUSED",
             "after": "_act_on_paused"},
            {"trigger": "resume", "source": "PAUSED", "dest": "IDLE",
             "after": "_act_on_resumed"},
        ]

    # ---------- wiring ----------

    def set_action(self, name: str, hook: ActionHook) -> None:
        """Đăng ký action thật. `name` phải thuộc ACTION_NAMES."""
        if name not in ACTION_NAMES:
            raise ValueError(f"Action không hợp lệ: {name}. Hợp lệ: {ACTION_NAMES}")
        self._actions[name] = hook

    def set_queue_predicate(self, predicate: QueuePredicate) -> None:
        """Register the TriggerManager predicate for pending work."""
        self._has_queued_trigger = predicate

    async def _run_action(self, name: str, event: Any) -> None:
        hook = self._actions.get(name)
        if hook is None:
            return
        try:
            await hook(event)
        except Exception as e:
            # Action lỗi không được làm state machine kẹt (N7 fail-safe).
            # State đã chuyển rồi; log để dashboard/watchdog thấy.
            self._log.error(
                "state_action_failed",
                action=name,
                state=self.state,
                turn_id=self.current_turn_id,
                error=str(e),
            )

    # ---------- conditions ----------

    async def is_valid_trigger(self, event: Any = None) -> bool:
        """Compatibility condition; callers provide an already validated trigger."""
        return True

    async def has_queued_trigger(self, event: Any = None) -> bool:
        if self._has_queued_trigger is None:
            return False
        try:
            return await self._has_queued_trigger()
        except Exception as e:
            self._log.error("queue_predicate_failed", error=str(e))
            return False

    # ---------- action adapters ----------

    async def _act_load_context_and_start_llm(self, event: Any) -> None:
        self.last_turn_interrupted = False
        await self._run_action("load_context_and_start_llm", event)

    async def _act_start_tts(self, event: Any) -> None:
        await self._run_action("start_tts", event)

    async def _act_use_fallback_response(self, event: Any) -> None:
        await self._run_action("use_fallback_response", event)

    async def _act_finalize_turn(self, event: Any) -> None:
        await self._run_action("finalize_turn", event)

    async def _act_stop_tts_graceful_and_flag(self, event: Any) -> None:
        """SPEAKING → COOLDOWN do interrupt: set flag thay vì state riêng (7.10.1)."""
        self.last_turn_interrupted = True
        await self._run_action("stop_tts_graceful", event)

    async def _act_on_paused(self, event: Any) -> None:
        await self._run_action("on_paused", event)

    async def _act_on_resumed(self, event: Any) -> None:
        await self._run_action("on_resumed", event)

    # ---------- state change hook ----------

    async def _on_state_change(self, event: Any) -> None:
        now = datetime.now(timezone.utc)
        elapsed_ms = int((now - self.state_entered_at).total_seconds() * 1000)
        from_state = event.transition.source if event and event.transition else "?"
        trigger_name = event.event.name if event and event.event else "?"

        record = StateTransition(
            from_state=from_state,
            to_state=self.state,
            trigger=trigger_name,
            at=now.isoformat(),
            elapsed_ms=elapsed_ms,
            turn_id=self.current_turn_id,
            interrupted=self.last_turn_interrupted,
        )
        self._history.append(record)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

        key = (from_state, self.state)
        self._transition_counts[key] = self._transition_counts.get(key, 0) + 1

        # Every accepted transition is observable in the turn log.
        self._log.info(
            "state_change",
            from_state=from_state,
            to_state=self.state,
            trigger=trigger_name,
            elapsed_ms=elapsed_ms,
            turn_id=self.current_turn_id,
            interrupted=self.last_turn_interrupted,
        )
        if self._event_bus is not None:
            self._event_bus.publish("state_change", record.to_log_dict())

        self.previous_state = from_state
        self.state_entered_at = now

        # Rời COOLDOWN → timer cũ không còn ý nghĩa
        self._cancel_cooldown_timer()
        if self.state == ConversationState.COOLDOWN.value and self._auto_cooldown:
            self._cooldown_task = asyncio.create_task(self._cooldown_timer())

    # ---------- cooldown timer ----------

    def _cancel_cooldown_timer(self) -> None:
        task = self._cooldown_task
        self._cooldown_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _cooldown_timer(self) -> None:
        try:
            await asyncio.sleep(self.cooldown_ms / 1000)
        except asyncio.CancelledError:
            return
        if self.state != ConversationState.COOLDOWN.value:
            return
        # Queue trước, IDLE sau (7.10.3)
        if await self.has_queued_trigger():
            await self.queued_trigger_pending()  # type: ignore[attr-defined]
        else:
            await self.cooldown_elapsed()  # type: ignore[attr-defined]

    async def wait_cooldown(self, timeout: float = 5.0) -> None:
        """Chờ cooldown timer chạy xong (dùng trong test/shutdown)."""
        task = self._cooldown_task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return

    # ---------- inspection ----------

    @property
    def current_state(self) -> ConversationState:
        return ConversationState(self.state)

    def time_in_state_ms(self) -> int:
        delta = datetime.now(timezone.utc) - self.state_entered_at
        return int(delta.total_seconds() * 1000)

    def history(self, limit: int | None = None) -> list[StateTransition]:
        return self._history[-limit:] if limit else list(self._history)

    def transition_counts(self) -> dict[tuple[str, str], int]:
        return dict(self._transition_counts)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "state_current": self.state,
            "state_time_in_state_ms": self.time_in_state_ms(),
            "state_transitions_total": sum(self._transition_counts.values()),
            "state_turn_interrupted": int(self.last_turn_interrupted),
        }

    async def shutdown(self) -> None:
        self._cancel_cooldown_timer()

    # ---------- factory ----------

    @classmethod
    def from_config(cls, loader, event_bus: Any = None, **kw) -> ConversationStateMachine:
        cooldown = loader.get("state_machine", "state_machine.cooldown_ms", None)
        if cooldown is None:
            # state_machine.yaml chưa có → dùng conversation.cooldown_ms ở system.yaml
            cooldown = loader.get("system", "conversation.cooldown_ms", 500)
        initial = loader.get(
            "state_machine", "state_machine.initial_state", ConversationState.IDLE.value
        )
        return cls(
            cooldown_ms=int(cooldown),
            event_bus=event_bus,
            initial_state=str(initial),
            **kw,
        )
