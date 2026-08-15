"""Turn orchestrator — glue trigger ↔ state machine (ARCHITECTURE 7.9/7.10, 2.E).

Nối TriggerManager + ConversationStateMachine + StateWatchdog thành vòng turn-taking:

  send_event → process_event → (INTERRUPT_CURRENT → cắt câu đang nói) / (QUEUE)
  consumer loop: khi IDLE + có trigger → run_turn:
      trigger_received (→THINKING) → think() → first_token (→SPEAKING) → speak
      → tts_complete (→COOLDOWN) → mark_spoke → cooldown (→IDLE)

Phase 2 chưa có TTS thật (Phase 4) → `speak` là sleep mô phỏng, `think` inject được
(fake trong test, LLMTurnRunner thật khi wire ở main). N8: chỉ dùng API công khai.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable

from interfaces.input import InputEvent
from interfaces.trigger import Trigger, TriggerAction, TriggerDecision
from orchestrator.logger import get_logger

#: think(trigger) -> text | None. None = LLM fail → fallback (llm_fail transition).
ThinkFn = Callable[[Trigger], Awaitable[str | None]]


class TurnOrchestrator:
    def __init__(
        self,
        trigger_manager: Any,
        state_machine: Any,
        think: ThinkFn,
        speak_seconds: float = 0.0,
        watchdog: Any = None,
        poll_s: float = 0.3,
        event_bus: Any = None,
    ) -> None:
        self._tm = trigger_manager
        self._sm = state_machine
        self._think = think
        self._speak_seconds = speak_seconds
        self._wd = watchdog
        self._poll_s = poll_s
        self._event_bus = event_bus
        self._log = get_logger("turn_orchestrator")

        self.processed_turns = 0
        self.current_trigger: Trigger | None = None
        self._running = False
        self._wake = asyncio.Event()
        self._consumer: asyncio.Task[None] | None = None
        self._speak_task: asyncio.Task[None] | None = None

        # interrupt policy cần biết Mai đang nói + đã nói bao lâu (N8: provider)
        self._tm.set_speaking_context(
            lambda: (self._sm.state == "SPEAKING", self._sm.time_in_state_ms())
        )

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._running = True
        self._consumer = asyncio.create_task(self._consume_loop())
        if self._wd is not None:
            self._wd.start()

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._consumer is not None:
            self._consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer
            self._consumer = None
        if self._speak_task is not None:
            self._speak_task.cancel()
        if self._wd is not None:
            await self._wd.stop()
        await self._sm.shutdown()

    # ---------- inbound ----------

    async def send_event(self, event: InputEvent) -> TriggerDecision:
        decision = await self._tm.process_event(event)
        if decision.action is TriggerAction.INTERRUPT_CURRENT:
            await self._interrupt_current()
        self._wake.set()
        return decision

    async def _interrupt_current(self) -> None:
        if self._sm.state != "SPEAKING":
            return
        if self._speak_task is not None:
            self._speak_task.cancel()
        from transitions.core import MachineError

        with contextlib.suppress(MachineError):
            await self._sm.interrupted()  # SPEAKING → COOLDOWN, flag interrupted

    async def emergency_stop(self) -> None:
        if self._speak_task is not None:
            self._speak_task.cancel()
        await self._sm.emergency_stop()
        await self._tm.clear_queue("emergency_stop")

    async def resume(self) -> None:
        await self._sm.resume()
        self._wake.set()

    # ---------- consumer loop ----------

    async def _consume_loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            while self._running and self._sm.state == "IDLE":
                trigger = await self._tm.get_next_trigger()
                if trigger is None:
                    break
                try:
                    await self._run_turn(trigger)
                except Exception as e:  # 1 turn lỗi không giết loop (N7)
                    self._log.error("turn_failed", error=str(e))

    async def _run_turn(self, trigger: Trigger) -> None:
        self.current_trigger = trigger
        await self._sm.trigger_received(trigger)  # IDLE → THINKING

        try:
            text = await self._think(trigger)
        except Exception as e:
            self._log.error("think_failed", error=str(e))
            text = None

        if not text:
            await self._sm.llm_fail()  # THINKING → COOLDOWN (fallback)
        else:
            await self._sm.first_token()  # THINKING → SPEAKING
            self._speak_task = asyncio.create_task(self._do_speak())
            with contextlib.suppress(asyncio.CancelledError):
                await self._speak_task
            self._speak_task = None
            # Nếu chưa bị interrupt (vẫn SPEAKING) → hoàn tất; nếu bị interrupt thì
            # _interrupt_current đã chuyển sang COOLDOWN rồi.
            if self._sm.state == "SPEAKING":
                await self._sm.tts_complete()  # SPEAKING → COOLDOWN

        self._tm.mark_spoke()
        self.processed_turns += 1
        await self._sm.wait_cooldown()  # COOLDOWN → IDLE (timer)
        self._wake.set()  # về IDLE → xem còn trigger không

    async def _do_speak(self) -> None:
        if self._speak_seconds > 0:
            await asyncio.sleep(self._speak_seconds)

    # ---------- inspection ----------

    @property
    def state(self) -> str:
        return self._sm.state

    @property
    def last_turn_interrupted(self) -> bool:
        return self._sm.last_turn_interrupted

    async def trigger_queue_size(self) -> int:
        return (await self._tm.get_queue_stats()).size

    async def wait_for_state(self, target: str, timeout: float = 3.0) -> bool:
        """Poll tới khi state == target. True nếu đạt, False nếu timeout."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._sm.state == target:
                return True
            await asyncio.sleep(0.005)
        return self._sm.state == target
