"""StreamRuntime — compose full stack cho stream/cli mode (Aut.D).

Bao gồm:
- LLM stack (LlamaCppLLMService + PromptManager + Canned + Runner)
- EmotionOrchestrator (mood engine + appraisal + drift)
- MemoryFallbackManager (optional)
- TTSPipeline (optional, speak callback)
- ChatRouter (sources → emotion + runner)
- AutonomyEngine tick loop (bg task, Mai tự nói)

Chia sẻ `turn_lock` giữa ChatRouter và autonomy loop — không chạy 2 turn LLM
cùng lúc (llama-server 1 instance).

Caller (stream_youtube.py / stream_discord.py / cli.py):
    rt = await StreamRuntime.build(loader, sources=[...], enable_tts=True, ...)
    await rt.start()
    await rt.wait_until_stopped()   # blocks Ctrl+C
    await rt.stop()
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from interfaces.animation import MoodState
from interfaces.input import InputService
from orchestrator.autonomy_engine import AutonomyEngine
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import get_logger
from orchestrator.metrics_collector import MetricsCollector
from services.autonomy.material_provider import RuntimeContext
from services.input.chat_router import ChatRouter
from services.llm.canned_response import CannedResponder
from services.llm.llama_cpp_llm import LlamaCppLLMService
from services.llm.llm_turn import LLMTurnRunner
from services.llm.prompt_manager import PromptManager
from services.qc.drift_detector import DriftDetector


SpeakFn = Callable[[str, str], Awaitable[None]]


@dataclass
class StreamRuntimeConfig:
    """Flags điều khiển build."""
    enable_tts: bool = False
    enable_memory: bool = False
    enable_autonomy: bool = True
    enable_dashboard: bool = False
    on_token: Callable[[str], None] | None = None


class StreamRuntime:
    def __init__(
        self,
        *,
        loader,
        llm_svc: LlamaCppLLMService,
        runner: LLMTurnRunner,
        emotion: EmotionOrchestrator,
        chat_router: ChatRouter,
        autonomy: AutonomyEngine | None,
        tts_svc: Any = None,
        audio_player: Any = None,
        tts_pipeline: Any = None,
        memory: Any = None,
        metrics: MetricsCollector,
        dashboard_task: asyncio.Task | None = None,
        speak: SpeakFn | None = None,
        cfg: StreamRuntimeConfig | None = None,
    ) -> None:
        self._loader = loader
        self._llm_svc = llm_svc
        self._runner = runner
        self._emotion = emotion
        self._router = chat_router
        self._autonomy = autonomy
        self._tts_svc = tts_svc
        self._audio_player = audio_player
        self._tts_pipeline = tts_pipeline
        self._memory = memory
        self._metrics = metrics
        self._dashboard_task = dashboard_task
        self._speak = speak
        self.cfg = cfg or StreamRuntimeConfig()

        self._running = False
        self._autonomy_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._log = get_logger("stream_runtime")

        self._chat_activity_ts: list[float] = []  # timestamps 10 phút gần nhất

    # ─────────────────────── Lifecycle ───────────────────────

    async def start(self) -> None:
        if self._running:
            return
        await self._router.start()
        self._running = True
        # Autonomy tick loop nếu bật + có engine
        if self.cfg.enable_autonomy and self._autonomy is not None:
            self._autonomy_task = asyncio.create_task(
                self._autonomy_loop(), name="autonomy_tick",
            )
        self._log.info(
            "stream_runtime_ready",
            tts=self.cfg.enable_tts, memory=self.cfg.enable_memory,
            autonomy=self.cfg.enable_autonomy and self._autonomy is not None,
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        # Cancel autonomy first
        if self._autonomy_task is not None and not self._autonomy_task.done():
            self._autonomy_task.cancel()
            with contextlib.suppress(Exception):
                await self._autonomy_task
            self._autonomy_task = None
        # Stop router (cascades sources + emotion)
        try:
            await self._router.stop()
        except Exception as e:  # pragma: no cover
            self._log.warning("router_stop_failed", error=str(e))
        # TTS
        if self._tts_pipeline is not None:
            with contextlib.suppress(Exception):
                await self._audio_player.stop()
            with contextlib.suppress(Exception):
                await self._tts_svc.stop()
        # Memory
        if self._memory is not None:
            with contextlib.suppress(Exception):
                await self._memory.stop()
        # Dashboard
        if self._dashboard_task is not None and not self._dashboard_task.done():
            self._dashboard_task.cancel()
            with contextlib.suppress(Exception):
                await self._dashboard_task
        # LLM
        with contextlib.suppress(Exception):
            await self._llm_svc.stop()

    async def wait_until_stopped(self) -> None:
        """Block cho tới khi stop() được gọi (VD từ signal handler)."""
        await self._stop_event.wait()

    # ─────────────────────── Autonomy loop ───────────────────────

    async def _autonomy_loop(self) -> None:
        """Tick engine mỗi cfg.tick_seconds, gọi maybe_generate → LLM ambient turn."""
        tick_s = self._autonomy.cfg.tick_seconds
        while self._running:
            try:
                await asyncio.sleep(tick_s)
                mood = self._get_current_mood()
                self._autonomy.tick(mood)
                ctx = self._build_runtime_context()
                decision = self._autonomy.maybe_generate(mood, ctx)
                if decision is not None:
                    await self._execute_ambient(decision)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                self._log.error("autonomy_loop_failed", error=str(e))

    async def _execute_ambient(self, decision) -> None:
        """Chạy 1 ambient turn — share turn_lock với ChatRouter (không đè chat turn)."""
        req_id = f"ambient_{uuid.uuid4().hex[:8]}"
        async with self._router._turn_lock:   # noqa: SLF001 — share intentionally
            try:
                parsed = await self._runner.run_ambient_turn(req_id, decision.prompt_text)
            except Exception as e:
                self._log.warning("ambient_turn_failed", error=str(e))
                return

            if not parsed.ok or not parsed.text:
                return

            # Post-check dedup: regen 1 lần nếu quá giống ambient gần đây
            if self._autonomy.check_dedup(parsed.text):
                self._log.info("ambient_dedup_hit_regen", category=decision.category)
                try:
                    parsed = await self._runner.run_ambient_turn(req_id + "_r", decision.prompt_text)
                except Exception:
                    pass  # fail-open N7, dùng bản đầu

            # Confirm spoke: reset urge + record opener + dedup
            self._autonomy.on_self_spoke(parsed.text)

            # TTS phát audio (nếu wire)
            if self._speak is not None and parsed.text:
                try:
                    await self._speak(req_id, parsed.text)
                except Exception as e:
                    self._log.warning("ambient_speak_failed", error=str(e))

    # ─────────────────────── Context builders ───────────────────────

    def _get_current_mood(self) -> MoodState:
        try:
            return self._emotion.current_mood()
        except Exception:
            return MoodState()

    def _build_runtime_context(self) -> RuntimeContext:
        now = time.time()
        # Cắt buckets > 10 phút
        cutoff = now - 600
        self._chat_activity_ts = [t for t in self._chat_activity_ts if t >= cutoff]

        silence = 0.0
        try:
            silence = now - self._autonomy.urge.last_external_activity_ts
        except Exception:
            pass

        memory_recent: list[str] = []
        # Working memory từ MemoryFallbackManager nếu có
        if self._memory is not None:
            try:
                snap = self._memory._fallback.snapshot()  # noqa: SLF001
                memory_recent = [e.content for e in snap[-3:]]
            except Exception:
                pass

        return RuntimeContext(
            silence_seconds=silence,
            chat_count_last_10min=len(self._chat_activity_ts),
            operator_online=False,  # MVP chưa detect operator
            consecutive_ignored=self._autonomy.urge.consecutive_ignored,
            working_memory_recent=memory_recent,
        )

    def note_chat_activity(self) -> None:
        """Chat từ platform → record cho complain_silence material.
        ChatRouter có thể gọi qua hook (Aut.D wire tuỳ chọn)."""
        self._chat_activity_ts.append(time.time())

    def get_metrics(self) -> dict[str, Any]:
        m = {
            "runtime_running": self._running,
            "runtime_tts_enabled": self.cfg.enable_tts,
            "runtime_memory_enabled": self.cfg.enable_memory,
            "runtime_autonomy_enabled": self.cfg.enable_autonomy and self._autonomy is not None,
        }
        if self._autonomy is not None:
            m.update(self._autonomy.get_metrics())
        return m


# ─────────────────────── Factory ───────────────────────


async def build_stream_runtime(
    *,
    loader,
    sources: list[InputService],
    cfg: StreamRuntimeConfig,
) -> StreamRuntime:
    """Build đầy đủ stack theo flags. Raise nếu llama-server không chạy."""
    metrics = MetricsCollector()

    # ─── LLM stack ───
    llm_svc = LlamaCppLLMService.from_loader(loader)
    await llm_svc.start()
    health = await llm_svc.health_check()
    if not health.is_ok:
        await llm_svc.stop()
        raise RuntimeError(f"llama-server chưa chạy: {health.message}")

    pm = PromptManager.from_loader(loader)
    canned = CannedResponder.from_loader(loader)
    fb = FallbackManager()

    # ─── Emotion ───
    emotion = EmotionOrchestrator.from_loader(loader, memory=None)
    drift = DriftDetector.from_loader(loader)

    # ─── Memory (optional) ───
    memory = None
    memory_extractor = None
    if cfg.enable_memory:
        from orchestrator.migration_runner import MigrationRunner
        from services.memory.embedder import BgeM3Embedder
        from services.memory.extractor import MemoryExtractor
        from services.memory.memory_fallback import MemoryFallbackManager
        from services.memory.semantic_memory import SemanticMemoryService
        from services.memory.sqlite_vec_store import SqliteVecStore
        from services.memory.working_memory import WorkingMemoryService

        db_path = loader.get("system", "paths.db_file", "data/mai.db")
        MigrationRunner.from_config(loader).initialize()
        store = SqliteVecStore(db_path=db_path)
        embedder = BgeM3Embedder.from_loader(loader)
        semantic = SemanticMemoryService(store=store, embedder=embedder)
        working = WorkingMemoryService.from_loader(loader)
        memory = MemoryFallbackManager(primary=semantic, fallback=working)
        await memory.start()
        memory_extractor = MemoryExtractor()
        emotion._modifiers._memory = memory  # noqa: SLF001 rewire

    # ─── Runner ───
    runner = LLMTurnRunner.from_loader(
        loader, llm_svc, pm, fb, canned,
        on_token=cfg.on_token or (lambda _t: None),
        metrics=metrics,
        memory=memory, memory_extractor=memory_extractor,
        emotion=emotion, drift_detector=drift,
    )

    # ─── TTS (optional) ───
    tts_svc = None
    audio_player = None
    tts_pipeline = None
    speak_callback: SpeakFn | None = None
    if cfg.enable_tts:
        from services.tts.audio_player import AudioPlayer
        from services.tts.subtitle_fallback import SubtitleFallbackService
        from services.tts.tts_pipeline import TTSPipeline
        from services.tts.vieneu_service import VieNeuTtsService

        tts_svc = VieNeuTtsService.from_loader(loader)
        try:
            await tts_svc.start()
            audio_player = AudioPlayer(sample_rate=tts_svc.sample_rate)
            await audio_player.start()
            subtitle = SubtitleFallbackService(
                on_subtitle=lambda rid, txt: None,
            )
            tts_pipeline = TTSPipeline(
                primary=tts_svc, subtitle=subtitle, player=audio_player,
                fallback=FallbackManager(), metrics=metrics,
            )

            async def _speak(req_id: str, text: str) -> None:
                await tts_pipeline.speak(req_id, text)

            speak_callback = _speak
        except Exception as e:
            get_logger("stream_runtime").warning(
                "tts_load_failed_continue_without", error=str(e),
            )
            tts_svc = None
            audio_player = None
            tts_pipeline = None

    # ─── Autonomy ───
    autonomy = None
    if cfg.enable_autonomy:
        autonomy = AutonomyEngine.from_loader(loader)

    # ─── ChatRouter ───
    router = ChatRouter(
        sources=sources, emotion=emotion, runner=runner, speak=speak_callback,
    )

    # ─── Dashboard (optional) ───
    dashboard_task = None
    if cfg.enable_dashboard:
        from dashboard.dashboard_server import DashboardServer
        ds = DashboardServer(metrics=metrics)
        dashboard_task = asyncio.create_task(ds.serve(), name="dashboard")

    rt = StreamRuntime(
        loader=loader, llm_svc=llm_svc, runner=runner, emotion=emotion,
        chat_router=router, autonomy=autonomy,
        tts_svc=tts_svc, audio_player=audio_player, tts_pipeline=tts_pipeline,
        memory=memory, metrics=metrics, dashboard_task=dashboard_task,
        speak=speak_callback, cfg=cfg,
    )
    # Hook chat activity vào autonomy engine — chat đến → reset silence
    if autonomy is not None:
        _orig_process = router._process  # noqa: SLF001

        async def _hook_process(event):
            autonomy.on_external_activity()
            rt.note_chat_activity()
            await _orig_process(event)

        router._process = _hook_process  # noqa: SLF001

    return rt
