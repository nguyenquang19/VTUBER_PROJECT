"""EmotionOrchestrator — glue Tầng 1→3 + tick loop 10Hz (Phase 7.5.C).

Spec EMOTION_SIMULATION Mục 5: mọi event → classifier → appraisal → modifier
→ saturate → MoodEngine. Background tick loop 10Hz đảm bảo mood decay/spring
chạy liên tục ngay cả khi không có event.

Buffer per-tick: nếu nhiều event fire trong cùng 1 tick window (spam, donation
mass) → gom targets rồi saturate 1 lần → tránh overshoot (spec Mục 5.4).

Cờ tone (force_gentle_tone/force_deflect) lưu ở `_active_flags` sau khi event
có flag; Prompt/Filter (7.5.D) đọc → gọi `clear_tone_flags()` sau khi xử.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from interfaces.animation import MoodState
from orchestrator.logger import get_logger
from orchestrator.mood_engine import MoodEngine
from services.emotion.appraisal import AppraisalTable
from services.emotion.classifier import EmotionEvent, EventClassifier
from services.emotion.modifiers import ModifierEngine


@dataclass
class ProcessedEvent:
    """Kết quả xử lý 1 event — trả cho caller để log/debug."""
    category: str
    targets: dict[str, float]
    tone_flag: str | None


class EmotionOrchestrator:
    def __init__(
        self,
        classifier: EventClassifier,
        appraisal: AppraisalTable,
        modifiers: ModifierEngine,
        engine: MoodEngine,
        tick_hz: int = 10,
    ) -> None:
        self._classifier = classifier
        self._appraisal = appraisal
        self._modifiers = modifiers
        self._engine = engine
        self._tick_hz = int(tick_hz)
        self._dt = 1.0 / self._tick_hz

        # Buffer per-dim: targets[dim] = list of float (nhiều event trong 1 tick)
        self._pending: dict[str, list[float]] = defaultdict(list)
        self._active_flags: set[str] = set()
        self._last_category: str | None = None
        self._events_total = 0

        self._tick_task: asyncio.Task | None = None
        self._log = get_logger("emotion")

    @classmethod
    def from_loader(
        cls,
        loader,
        classifier: EventClassifier | None = None,
        appraisal: AppraisalTable | None = None,
        modifiers: ModifierEngine | None = None,
        engine: MoodEngine | None = None,
        memory: Any = None,
        filter_service: Any = None,
    ) -> "EmotionOrchestrator":
        classifier = classifier or EventClassifier.from_loader(loader, filter_service=filter_service)
        appraisal = appraisal or AppraisalTable.from_loader(loader)
        modifiers = modifiers or ModifierEngine.from_loader(loader, memory=memory)
        engine = engine or MoodEngine.from_loader(loader)
        return cls(
            classifier=classifier,
            appraisal=appraisal,
            modifiers=modifiers,
            engine=engine,
            tick_hz=engine.tick_hz,
        )

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Start background tick loop 10Hz. Idempotent."""
        if self._tick_task is not None and not self._tick_task.done():
            return
        self._tick_task = asyncio.create_task(self._tick_loop(), name="emotion_tick")
        self._log.info("emotion_orchestrator_ready", tick_hz=self._tick_hz)

    async def stop(self) -> None:
        if self._tick_task is None:
            return
        self._tick_task.cancel()
        try:
            await self._tick_task
        except asyncio.CancelledError:
            pass
        self._tick_task = None

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._dt)
                self.flush_and_tick(self._dt)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                self._log.error("emotion_tick_failed", error=str(e))

    # ---------- API ----------

    async def handle_event(self, event: EmotionEvent) -> ProcessedEvent:
        """Full pipeline 1 event: classify → appraise → modifier → buffer.

        KHÔNG áp thẳng vào engine — chờ tick flush để saturate với event khác cùng tick.
        """
        self._events_total += 1
        category = self._classifier.classify(event)
        self._last_category = category

        base_targets = self._appraisal.target_for(category)
        viewer_id = event.meta.get("viewer_id") if event.meta else None
        final_targets = await self._modifiers.apply(category, base_targets, viewer_id)

        # Buffer per-dim (saturation ở flush)
        for dim, val in final_targets.items():
            self._pending[dim].append(float(val))

        # Tone flag (nếu có) — Prompt/Filter đọc sau
        flag = self._appraisal.tone_flag(category)
        if flag:
            self._active_flags.add(flag)

        return ProcessedEvent(
            category=category, targets=final_targets, tone_flag=flag,
        )

    def flush_and_tick(self, dt: float | None = None) -> MoodState:
        """Flush buffer → saturate → apply_appraisal → tick 1 lần.

        Public để test call trực tiếp bypass loop. Nếu buffer rỗng → chỉ tick (decay).
        """
        if self._pending:
            saturated = self._engine.saturate(dict(self._pending))
            self._engine.apply_appraisal(saturated)
            self._pending.clear()
        return self._engine.tick(dt or self._dt)

    def apply_llm_hint(self, mood_state: MoodState) -> None:
        """Kênh B: LLM self-report từ turn kế → nudge target nhẹ."""
        self._engine.apply_llm_hint(mood_state)

    def current_mood(self) -> MoodState:
        """Snapshot mood hiện tại (không tick). Prompt/Animation gọi."""
        return self._engine.current_state()

    def active_tone_flags(self) -> set[str]:
        """Cờ tone hiện đang active (sinh từ event gần đây, chưa clear)."""
        return set(self._active_flags)

    def clear_tone_flags(self) -> None:
        """Prompt/Filter gọi sau khi đã đọc & xử cờ (1 lần dùng cho 1 turn)."""
        self._active_flags.clear()

    def reset_session(self) -> None:
        """Session mới: reset modifier counters (không reset mood engine)."""
        self._modifiers.reset_session()

    # ---------- introspection ----------

    def get_metrics(self) -> dict[str, Any]:
        return {
            "emotion_events_total": self._events_total,
            "emotion_pending_dims": len(self._pending),
            "emotion_active_flags": len(self._active_flags),
            "emotion_last_category": self._last_category,
            **self._engine.get_metrics(),
            **self._modifiers.get_metrics(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Dashboard/debug — trạng thái đầy đủ."""
        return {
            "mood_pos": self._engine.snapshot()["pos"],
            "mood_target": self._engine.snapshot()["target"],
            "current_mood": self.current_mood().model_dump(),
            "active_flags": sorted(self._active_flags),
            "last_category": self._last_category,
            "pending_events": {d: list(v) for d, v in self._pending.items()},
        }
