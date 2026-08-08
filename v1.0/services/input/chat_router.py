"""ChatRouter — glue InputService → EmotionOrchestrator + LLMTurnRunner (Platform.C).

Nhận event từ N nguồn (YouTube + Discord + tương lai), convert sang EmotionEvent,
apply appraisal, sau đó chạy 1 turn LLM. Serialize turn (asyncio.Lock) — không
chạy 2 LLM turn cùng lúc (llama-server 1 instance).

Serialize policy: 1 turn tại 1 thời điểm. Message đến khi đang bận → wait (Lock
tự FIFO). Đủ cho MVP; nếu spam cao dùng TriggerManager (Phase 2) làm rate-limit.

Fail-safe: emotion error → skip event; runner error → log, tiếp event kế
(không kill router). Không mất session vì 1 message lỗi.
"""
from __future__ import annotations

import asyncio
from datetime import timezone
from typing import Any, Awaitable, Callable

from interfaces.input import EventSource, InputEvent, InputService
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from orchestrator.logger import get_logger
from services.emotion.classifier import EmotionEvent, EventKind
from services.llm.llm_turn import LLMTurnRunner
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)


SpeakFn = Callable[[str, str], Awaitable[None]]  # (request_id, text) → None


class ChatRouter:
    def __init__(
        self,
        sources: list[InputService],
        emotion: EmotionOrchestrator,
        runner: LLMTurnRunner,
        speak: SpeakFn | None = None,
        # C0.4: intake mode — nếu cấp pool+pulse, chat KHÔNG tự đáp mà bơm vào
        # SaliencePool + ChatPulse để Director cầm nhịp (bỏ FIFO). None = FIFO cũ.
        pool: Any = None,
        pulse: Any = None,
        chat_pulse_hook: Callable[[], None] | None = None,
        turn_lock: asyncio.Lock | None = None,
        agent_state: Any = None,
    ) -> None:
        if not sources:
            raise ValueError("cần ít nhất 1 InputService")
        self._sources = list(sources)
        self._emotion = emotion
        self._runner = runner
        self._speak = speak
        self._pool = pool
        self._pulse = pulse
        self._intake_mode = pool is not None and pulse is not None
        self._extra_activity_hook = chat_pulse_hook
        self._agent_state = agent_state

        self._running = False
        # C0.4: share turn_lock với DirectorLoop nếu được cấp (1 driver duy nhất)
        self._turn_lock = turn_lock or asyncio.Lock()
        self._consumers: list[asyncio.Task] = []
        self._log = get_logger("chat_router")

        self._events_received = 0
        self._turns_run = 0
        self._turns_failed = 0
        self._speak_calls = 0
        self._intake_pooled = 0

    # ---------- Lifecycle ----------

    async def start(self) -> None:
        """Start emotion tick loop + spawn 1 consumer task/source. Idempotent."""
        if self._running:
            return
        # Emotion tick loop bg (7.5.C)
        await self._emotion.start()
        # Start each source
        for src in self._sources:
            await src.start()
        # Spawn consumers
        self._consumers = [
            asyncio.create_task(self._consume(src), name=f"router_consume_{src.service_id}")
            for src in self._sources
        ]
        self._running = True
        self._log.info(
            "chat_router_ready",
            sources=[s.service_id for s in self._sources],
        )

    async def stop(self) -> None:
        self._running = False
        # Cancel consumers first (dừng feed event)
        for t in self._consumers:
            if not t.done():
                t.cancel()
        for t in self._consumers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._consumers.clear()
        # Stop sources + emotion
        for src in self._sources:
            try:
                await src.stop()
            except Exception as e:  # pragma: no cover
                self._log.warning("router_source_stop_failed",
                                  source=src.service_id, error=str(e))
        try:
            await self._emotion.stop()
        except Exception as e:  # pragma: no cover
            self._log.warning("router_emotion_stop_failed", error=str(e))

    # ---------- Metrics ----------

    def get_metrics(self) -> dict[str, Any]:
        return {
            "router_events_received": self._events_received,
            "router_turns_run": self._turns_run,
            "router_turns_failed": self._turns_failed,
            "router_speak_calls": self._speak_calls,
            "router_active_sources": len([s for s in self._sources if True]),
            **self._emotion.get_metrics(),
        }

    # ---------- Internal ----------

    async def _consume(self, source: InputService) -> None:
        """Loop: pull event → process. Chạy tới khi cancel."""
        try:
            async for event in source.event_stream():
                if not self._running:
                    break
                self._events_received += 1
                await self._process(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.error(
                "router_consumer_crashed",
                source=source.service_id, error=str(e),
            )

    async def _process(self, event: InputEvent) -> None:
        """1 event → emotion + (intake: bơm pool/pulse) HOẶC (FIFO: run_turn)."""
        emo_event = _to_emotion_event(event)
        try:
            processed = await self._emotion.handle_event(emo_event)
        except Exception as e:
            self._log.warning(
                "router_emotion_failed",
                event_id=event.event_id, error=str(e),
            )
            return   # skip event, không giết router

        self._record_chat_event(event, emo_event, processed.category)

        # C0.4 intake mode: bơm vào SaliencePool + ChatPulse, KHÔNG tự đáp.
        # Director loop sẽ nhặt từ pool khi quyết read_chat.
        if self._intake_mode:
            self._intake(event, emo_event)
            return

        async with self._turn_lock:
            try:
                parsed, level = await self._runner.run_turn(
                    request_id=event.event_id or "no_id",
                    user_text=event.content,
                    viewer_id=event.user_id,
                    trigger_type=event.source.value,
                    event_category=processed.category,
                )
                self._turns_run += 1
            except Exception as e:
                self._turns_failed += 1
                self._log.error(
                    "router_turn_failed",
                    event_id=event.event_id, error=str(e),
                )
                return

            # Speak (TTS) — optional, không await trong lock nếu speak dài
            # nhưng vẫn giữ trong lock để đảm bảo audio Mai không overlap
            if self._speak is not None and parsed.ok and parsed.text:
                try:
                    await self._speak(event.event_id or "no_id", parsed.text)
                    self._speak_calls += 1
                except Exception as e:
                    self._log.warning("router_speak_failed", error=str(e))

    def _record_chat_event(
        self, event: InputEvent, emo_event: EmotionEvent, category: str,
    ) -> None:
        if self._agent_state is None:
            return
        is_donation = bool(emo_event.meta.get("platform_type") == "donation")
        platform = {
            EventSource.CHAT_YOUTUBE: "youtube",
            EventSource.CHAT_DISCORD: "discord",
        }.get(event.source, "chat")
        source = {
            "youtube": AgentEventSource.YOUTUBE,
            "discord": AgentEventSource.DISCORD,
        }.get(platform, AgentEventSource.CHAT)
        payload: dict[str, Any] = {
            "text": event.content,
            "viewer_alias": event.user_name,
            "emotion_category": category,
        }
        if is_donation:
            payload["amount_vnd"] = int(emo_event.meta.get("amount_vnd", 0) or 0)
        try:
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:chat:{event.event_id}",
                kind=(
                    AgentEventKind.DONATION_RECEIVED
                    if is_donation else AgentEventKind.CHAT_RECEIVED
                ),
                source=source,
                timestamp=(
                    event.timestamp.replace(tzinfo=timezone.utc)
                    if event.timestamp.tzinfo is None else event.timestamp
                ),
                confidence=1.0,
                payload=payload,
                provenance=EventProvenance(
                    producer="chat_router",
                    source_event_id=event.event_id,
                    session_id=getattr(self._runner, "session_id", None),
                    platform=platform,
                ),
            ))
        except Exception as exc:
            self._log.warning("router_agent_event_failed", error=str(exc))


    def _intake(self, event: InputEvent, emo_event: EmotionEvent) -> None:
        """C0.4: đẩy chat vào SaliencePool + ChatPulse (không sinh turn)."""
        now = event.timestamp.timestamp() if event.timestamp else _now_ts()
        is_super = bool(emo_event.meta.get("platform_type") == "donation")
        amount = int(emo_event.meta.get("amount_vnd", 0) or 0)
        text = event.content or ""
        kind = _classify_kind(text)
        try:
            self._pool.add(
                msg_id=event.event_id or f"m{self._intake_pooled}",
                text=text, now=now, kind=kind,
                viewer_id=event.user_id, viewer_name=event.user_name,
                amount_vnd=amount, is_super=is_super,
            )
            self._pulse.record(now=now, user_id=event.user_id)
            self._intake_pooled += 1
            if self._extra_activity_hook is not None:
                self._extra_activity_hook()
        except Exception as e:
            self._log.warning("router_intake_failed", event_id=event.event_id, error=str(e))


# ---------- Helpers ----------


_MENTION_KW = ("mai",)


def _now_ts() -> float:
    import time
    return time.time()


def _classify_kind(text: str) -> str:
    """kind cho SaliencePool base_tier: mention > question > chat (rẻ, regex nhẹ)."""
    low = (text or "").lower()
    if any(kw in low for kw in _MENTION_KW):
        return "mention"
    if "?" in text:
        return "question"
    return "chat"


def _to_emotion_event(ev: InputEvent) -> EmotionEvent:
    """InputEvent (nguồn chat) → EmotionEvent (đầu vào classifier T1)."""
    meta = dict(ev.metadata or {})
    if ev.user_id and "viewer_id" not in meta:
        meta["viewer_id"] = ev.user_id
    if ev.user_name and "viewer_name" not in meta:
        meta["viewer_name"] = ev.user_name
    meta.setdefault("source_event_id", ev.event_id)
    meta.setdefault("platform", ev.source.value)
    # Super chat từ YouTube → treat as SYSTEM donation (đủ tin để trigger appraisal
    # donation_large/small qua amount_vnd). Chat thường → CHAT.
    if meta.get("is_super_chat") and meta.get("amount_vnd"):
        return EmotionEvent(
            kind=EventKind.SYSTEM,
            text=ev.content,
            meta={**meta, "platform_type": "donation"},
            timestamp=ev.timestamp,
        )
    return EmotionEvent(
        kind=EventKind.CHAT,
        text=ev.content,
        meta=meta,
        timestamp=ev.timestamp,
    )
