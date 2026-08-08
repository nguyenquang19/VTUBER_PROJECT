"""StreamRuntime — compose full stack cho stream/cli mode (Aut.D).

Bao gồm:
- LLM stack (LlamaCppLLMService + PromptManager + Canned + Runner)
- EmotionOrchestrator (mood engine + appraisal)
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
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from interfaces.animation import MoodState
from interfaces.input import InputService
from orchestrator.autonomy_engine import AutonomyEngine
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from orchestrator.fallback_manager import FallbackManager
from orchestrator.features import FeatureManager, FeatureStatus
from orchestrator.logger import bind_log_session, get_logger, setup_from_config
from orchestrator.metrics_collector import MetricsCollector
from services.autonomy.material_provider import RuntimeContext
from services.input.chat_router import ChatRouter
from services.llm.canned_response import CannedResponder
from services.llm.llama_cpp_llm import LlamaCppLLMService
from services.llm.llm_turn import LLMTurnRunner
from services.llm.prompt_manager import PromptManager
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)


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
        feature_manager: FeatureManager | None = None,
        filter_svc: Any = None,
        regenerator: Any = None,
        metrics: MetricsCollector,
        dashboard_task: asyncio.Task | None = None,
        speak: SpeakFn | None = None,
        filler: Any = None,   # A3 FillerManager (metrics only; wiring ở speak wrapper)
        director_loop: Any = None,   # C0.4 DirectorLoop — turn driver (thay autonomy loop)
        agent_state: Any = None,
        goal_manager: Any = None,
        goal_proposal: Any = None,
        thread_extractor: Any = None,
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
        self._feature_manager = feature_manager
        self._filter_svc = filter_svc
        self._regenerator = regenerator
        self._metrics = metrics
        self._dashboard_task = dashboard_task
        self._speak = speak
        self._filler = filler
        self._director_loop = director_loop
        self._agent_state = agent_state
        self._goal_manager = goal_manager
        self._goal_proposal = goal_proposal
        self._thread_extractor = thread_extractor
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
        if self._agent_state is not None:
            await self._agent_state.start()
        if self._goal_manager is not None:
            await self._goal_manager.start()
        if self._goal_proposal is not None:
            await self._goal_proposal.start()
        if self._thread_extractor is not None:
            await self._thread_extractor.start()
        await self._router.start()
        self._running = True
        # C0.4: DirectorLoop cầm nhịp (thay autonomy loop cũ). Fallback: autonomy loop
        # nếu không có director (backward compat / test).
        if self._director_loop is not None:
            await self._director_loop.start()
        elif self.cfg.enable_autonomy and self._autonomy is not None:
            self._autonomy_task = asyncio.create_task(
                self._autonomy_loop(), name="autonomy_tick",
            )
        self._record_environment_observation()
        self._log.info(
            "stream_runtime_ready",
            tts=self.cfg.enable_tts, memory=self.cfg.enable_memory,
            director=self._director_loop is not None,
            autonomy=self.cfg.enable_autonomy and self._autonomy is not None,
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        # Stop director loop (C0.4) hoặc autonomy loop cũ
        if self._director_loop is not None:
            with contextlib.suppress(Exception):
                await self._director_loop.stop()
        if self._autonomy_task is not None and not self._autonomy_task.done():
            self._autonomy_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
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
        if self._filter_svc is not None:
            with contextlib.suppress(Exception):
                await self._filter_svc.stop()
        # Dashboard
        if self._dashboard_task is not None and not self._dashboard_task.done():
            self._dashboard_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._dashboard_task
        # LLM
        with contextlib.suppress(Exception):
            await self._llm_svc.stop()
        if self._goal_proposal is not None:
            with contextlib.suppress(Exception):
                await self._goal_proposal.stop()
        if self._thread_extractor is not None:
            with contextlib.suppress(Exception):
                await self._thread_extractor.stop()
        if self._goal_manager is not None:
            with contextlib.suppress(Exception):
                await self._goal_manager.stop()
        if self._agent_state is not None:
            with contextlib.suppress(Exception):
                await self._agent_state.stop()

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
            # A6: ghi self-talk vào history → chat đáp lại sẽ khớp continuity
            self._runner.commit_self_talk(parsed.text)

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
            "runtime_filter_enabled": self._runner.filter_enabled,
        }
        if self._autonomy is not None:
            m.update(self._autonomy.get_metrics())
        if self._filler is not None:
            m.update(self._filler.get_metrics())
        if self._director_loop is not None:
            with contextlib.suppress(Exception):
                m.update(self._director_loop.get_metrics())
        if self._agent_state is not None:
            with contextlib.suppress(Exception):
                m.update(self._agent_state.get_metrics())
        if self._goal_manager is not None:
            with contextlib.suppress(Exception):
                m.update(self._goal_manager.get_metrics())
        if self._goal_proposal is not None:
            with contextlib.suppress(Exception):
                m.update(self._goal_proposal.get_metrics())
        return m

    @property
    def agent_state(self) -> Any:
        """The one shared state instance used by all stream producers."""
        return self._agent_state

    @property
    def goal_manager(self) -> Any:
        return self._goal_manager

    @property
    def goal_proposal(self) -> Any:
        return self._goal_proposal

    def _record_environment_observation(self) -> None:
        if self._agent_state is None:
            return
        session_id = getattr(self._runner, "session_id", None)
        try:
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:environment:{session_id or 'runtime'}:startup",
                kind=AgentEventKind.ENVIRONMENT_OBSERVED,
                source=AgentEventSource.RUNTIME,
                timestamp=datetime.now(timezone.utc),
                confidence=1.0,
                payload={
                    "source_services": [
                        getattr(source, "service_id", "unknown")
                        for source in getattr(self._router, "_sources", [])
                    ],
                    "tts_enabled": self.cfg.enable_tts and self._tts_pipeline is not None,
                    "memory_enabled": self.cfg.enable_memory and self._memory is not None,
                    "autonomy_enabled": self.cfg.enable_autonomy and self._autonomy is not None,
                    "dashboard_enabled": self.cfg.enable_dashboard,
                },
                provenance=EventProvenance(
                    producer="stream_runtime", session_id=session_id,
                ),
            ))
        except Exception as exc:
            self._log.warning("runtime_agent_event_failed", error=str(exc))


# ─────────────────────── Factory ───────────────────────


async def build_stream_runtime(
    *,
    loader,
    sources: list[InputService],
    cfg: StreamRuntimeConfig,
) -> StreamRuntime:
    """Build đầy đủ stack theo flags. Raise nếu llama-server không chạy."""
    # B0: setup structlog + JSONL sinks (turns.jsonl để baseline eval)
    turn_logger = setup_from_config(loader)
    pref_logger = _make_pref_logger(loader)   # T2: DPO pairs sink

    metrics = MetricsCollector()
    feature_manager = FeatureManager.from_config(loader)

    # M1: one shared grounded working state for every stream producer.
    from services.agent.agent_state import AgentState
    from services.agent.event_ledger import EventLedger
    from services.agent.agenda_policy import AgendaPolicy
    from services.agent.goal_manager import GoalManager
    from services.agent.open_thread_manager import OpenThreadManager
    from services.agent.thread_detector import RuleThreadDetector
    from services.agent.session_recap import SessionRecapManager

    event_ledger = EventLedger.from_loader(loader, metrics=metrics)
    thread_detector = RuleThreadDetector.from_loader(loader)
    open_thread_manager = OpenThreadManager.from_loader(
        loader, metrics=metrics, detector=thread_detector,
    )
    session_recap = SessionRecapManager.from_loader(loader, metrics=metrics)
    agent_state = AgentState.from_loader(
        loader, event_ledger, thread_manager=open_thread_manager,
        recap_manager=session_recap,
    )
    agenda_policy = AgendaPolicy.from_loader(loader)
    goal_manager = GoalManager.from_loader(
        loader, metrics=metrics, on_active_changed=agent_state.set_active_goal_ref,
        audit_sink=agent_state.record, agenda_policy=agenda_policy,
    )
    agent_state.add_event_listener(goal_manager.handle_event)

    # ─── LLM stack ───
    llm_svc = LlamaCppLLMService.from_loader(loader)
    await llm_svc.start()
    health = await llm_svc.health_check()
    if not health.is_ok:
        await llm_svc.stop()
        raise RuntimeError(f"llama-server chưa chạy: {health.message}")

    from services.agent.goal_proposal import GoalProposalGenerator
    try:
        proposal_status = await feature_manager.get_status("goal_proposals")
        proposal_enabled = proposal_status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
    except KeyError:
        get_logger("stream_runtime").warning("goal_proposals_feature_missing")
        proposal_enabled = False
    goal_proposal = GoalProposalGenerator.from_loader(
        loader, llm_svc, metrics=metrics, enabled=proposal_enabled,
    )

    from services.agent.thread_extraction import PostHocThreadExtractor
    try:
        extraction_status = await feature_manager.get_status("thread_extraction")
        extraction_enabled = extraction_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("thread_extraction_feature_missing")
        extraction_enabled = False
    thread_extractor = PostHocThreadExtractor.from_loader(
        loader, llm_svc, metrics=metrics, enabled=extraction_enabled,
    )

    def _observe_thread_extraction(event, state) -> None:
        thread_extractor.observe(event, state, open_thread_manager)

    agent_state.add_event_listener(_observe_thread_extraction)

    async def _enable_thread_extraction() -> None:
        thread_extractor.set_enabled(True)

    async def _disable_thread_extraction() -> None:
        thread_extractor.set_enabled(False)

    async def _thread_extraction_health() -> bool:
        return thread_extractor.enabled

    feature_manager.attach_handlers(
        "thread_extraction", enable=_enable_thread_extraction,
        disable=_disable_thread_extraction, health=_thread_extraction_health,
    )

    async def _enable_goal_proposals() -> None:
        goal_proposal.set_enabled(True)

    async def _disable_goal_proposals() -> None:
        goal_proposal.set_enabled(False)

    async def _goal_proposals_health() -> bool:
        return goal_proposal.enabled

    feature_manager.attach_handlers(
        "goal_proposals", enable=_enable_goal_proposals,
        disable=_disable_goal_proposals, health=_goal_proposals_health,
    )

    pm = PromptManager.from_loader(loader)
    canned = CannedResponder.from_loader(loader)
    fb = FallbackManager()
    from services.agent.context_renderer import AgentContextRenderer
    agent_context_renderer = AgentContextRenderer.from_loader(loader)
    try:
        agent_context_status = await feature_manager.get_status("agent_context")
        agent_context_enabled = agent_context_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("agent_context_feature_missing")
        agent_context_enabled = False

    # ─── Output filter (M0.2) ───
    # Tạo service cả khi feature đang OFF để dashboard có thể bật runtime về sau.
    filter_svc = None
    regenerator = None
    filter_enabled = False
    try:
        filter_status = await feature_manager.get_status("filter_rule")
    except KeyError:
        get_logger("stream_runtime").warning("filter_rule_feature_missing")
    else:
        from services.filter.regenerator import FilterRegenerator
        from services.filter.rule_filter import RuleFilter

        filter_svc = RuleFilter.from_config(loader)
        regenerator = FilterRegenerator.from_loader(
            loader, filter_svc, llm_svc, metrics=metrics,
        )
        filter_enabled = filter_status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
        if filter_enabled:
            await filter_svc.start()

    # ─── Emotion ───
    # A1: drift_detector đã bỏ (Kênh B tắt, LLM không tự report mood)
    emotion = EmotionOrchestrator.from_loader(loader, memory=None, agent_state=agent_state)

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
    session_id = str(uuid.uuid4())
    bind_log_session(session_id)
    runner = LLMTurnRunner.from_loader(
        loader, llm_svc, pm, fb, canned,
        on_token=cfg.on_token or (lambda _t: None),
        metrics=metrics,
        regenerator=regenerator if filter_enabled else None,
        memory=memory, memory_extractor=memory_extractor,
        emotion=emotion,
        turn_logger=turn_logger,
        pref_logger=pref_logger,
        session_id=session_id,
        agent_state=agent_state,
        agent_context_renderer=agent_context_renderer if agent_context_enabled else None,
    )

    async def _enable_agent_context() -> None:
        runner.set_agent_context_renderer(agent_context_renderer)

    async def _disable_agent_context() -> None:
        runner.set_agent_context_renderer(None)

    async def _agent_context_health() -> bool:
        return runner.agent_context_enabled

    feature_manager.attach_handlers(
        "agent_context",
        enable=_enable_agent_context,
        disable=_disable_agent_context,
        health=_agent_context_health,
    )

    if filter_svc is not None and regenerator is not None:
        async def _enable_rule_filter() -> None:
            await filter_svc.start()
            runner.set_regenerator(regenerator)

        async def _disable_rule_filter() -> None:
            await filter_svc.stop()
            runner.set_regenerator(None)

        async def _filter_health() -> bool:
            return (await filter_svc.health_check()).is_ok

        feature_manager.attach_handlers(
            "filter_rule",
            enable=_enable_rule_filter,
            disable=_disable_rule_filter,
            health=_filter_health,
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

    # ─── A3: Response pacing + filler ───
    # Wrap speak_callback: delay biến thiên trước khi nói + filler audio (nếu có clip).
    # Áp cho CẢ chat reply lẫn ambient (dùng chung _speak boundary).
    from services.tts.pacing import FillerManager, ResponsePacer

    pacer = ResponsePacer.from_loader(loader)
    filler = FillerManager.from_loader(loader)

    async def _play_filler_clip(req_id: str, clip_path: str) -> None:
        """Load clip wav → enqueue AudioPlayer TRƯỚC câu. Fail-safe: lỗi → skip (N7)."""
        if audio_player is None:
            return
        try:
            import numpy as np
            import soundfile as sf
            from interfaces.tts import AudioChunk

            data, sr = sf.read(clip_path, dtype="float32", always_2d=False)
            if getattr(data, "ndim", 1) > 1:
                data = data[:, 0]  # mono hoá
            if sr != audio_player.sample_rate:
                # tránh phát sai cao độ — bỏ qua, cảnh báo (user thu clip đúng sr)
                get_logger("stream_runtime").warning(
                    "filler_sr_mismatch_skip", clip=clip_path,
                    clip_sr=sr, player_sr=audio_player.sample_rate,
                )
                return
            fid = f"{req_id}_filler"
            dur_ms = int(len(data) / max(1, sr) * 1000)
            await audio_player.enqueue(AudioChunk(
                request_id=fid, chunk_index=0,
                audio_bytes=np.asarray(data, dtype=np.float32).tobytes(),
                is_final=False, duration_ms=dur_ms,
            ))
            # final marker để AudioPlayer reset current sau filler
            await audio_player.enqueue(AudioChunk(
                request_id=fid, chunk_index=1, audio_bytes=b"",
                is_final=True, duration_ms=0,
            ))
        except Exception as e:
            get_logger("stream_runtime").warning("filler_play_failed", error=str(e))

    if speak_callback is not None:
        _raw_speak = speak_callback

        async def _paced_speak(req_id: str, text: str) -> None:
            d = pacer.delay(text)
            if d > 0:
                await asyncio.sleep(d)
            clip = filler.maybe_pick(time.time())
            if clip is not None:
                await _play_filler_clip(req_id, clip)
            await _raw_speak(req_id, text)

        speak_callback = _paced_speak

    # ─── Autonomy ───
    autonomy = None
    if cfg.enable_autonomy:
        autonomy = AutonomyEngine.from_loader(loader)

    # ─── C0.4: Director stack — cầm nhịp thay FIFO ───
    from services.director.chat_pulse import ChatPulse
    from services.director.action_context import ActionContextBuilder
    from services.director.director import Director
    from services.director.director_loop import DirectorLoop
    from services.director.salience import SaliencePool

    pool = SaliencePool.from_loader(loader)
    pulse = ChatPulse.from_loader(loader)
    director = Director.from_loader(pool, pulse, loader)
    action_context_builder = ActionContextBuilder.from_loader(loader)
    turn_lock = asyncio.Lock()   # 1 lock chung: ChatRouter intake + DirectorLoop
    try:
        arbiter_status = await feature_manager.get_status("director_goal_arbiter")
        arbiter_enabled = arbiter_status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
    except KeyError:
        get_logger("stream_runtime").warning("director_goal_arbiter_feature_missing")
        arbiter_enabled = False

    # ─── ChatRouter (intake mode: bơm pool+pulse, KHÔNG tự đáp) ───
    router = ChatRouter(
        sources=sources, emotion=emotion, runner=runner, speak=speak_callback,
        pool=pool, pulse=pulse, turn_lock=turn_lock, agent_state=agent_state,
    )

    # TASK 4: director tick TÁCH khỏi autonomy (autonomy 5s làm chat chờ lâu).
    director_tick = float(loader.get("director", "director.tick_seconds", 1.5))
    director_loop = DirectorLoop(
        director=director, pool=pool, pulse=pulse, runner=runner,
        emotion=emotion, autonomy=autonomy, speak=speak_callback,
        turn_lock=turn_lock,
        tick_seconds=director_tick,
        agent_state=agent_state,
        goal_manager=goal_manager,
        metrics=metrics,
        goal_arbitration_enabled=arbiter_enabled,
        action_context_builder=action_context_builder,
    )

    async def _enable_director_goal_arbiter() -> None:
        director_loop.set_goal_arbitration_enabled(True)

    async def _disable_director_goal_arbiter() -> None:
        director_loop.set_goal_arbitration_enabled(False)

    async def _director_goal_arbiter_health() -> bool:
        return director_loop.goal_arbitration_enabled

    feature_manager.attach_handlers(
        "director_goal_arbiter",
        enable=_enable_director_goal_arbiter,
        disable=_disable_director_goal_arbiter,
        health=_director_goal_arbiter_health,
    )

    # ─── Dashboard (optional) ───
    dashboard_task = None
    if cfg.enable_dashboard:
        from dashboard.dashboard_server import DashboardServer
        ds = DashboardServer(feature_manager=feature_manager, metrics=metrics,
                             filter_svc=filter_svc, regenerator=regenerator,
                             emotion=emotion, runner=runner,
                             agent_state=agent_state,
                             goal_manager=goal_manager,
                             data_dir=loader.get("logging", "jsonl.dir", "logs"))
        dashboard_task = asyncio.create_task(ds.serve(), name="dashboard")

    rt = StreamRuntime(
        loader=loader, llm_svc=llm_svc, runner=runner, emotion=emotion,
        chat_router=router, autonomy=autonomy,
        tts_svc=tts_svc, audio_player=audio_player, tts_pipeline=tts_pipeline,
        memory=memory, feature_manager=feature_manager,
        filter_svc=filter_svc, regenerator=regenerator,
        metrics=metrics, dashboard_task=dashboard_task,
        speak=speak_callback, filler=filler, director_loop=director_loop,
        agent_state=agent_state, cfg=cfg,
        goal_manager=goal_manager,
        goal_proposal=goal_proposal,
        thread_extractor=thread_extractor,
    )
    # DirectorLoop dùng runtime ctx của rt (silence/chat_count/memory) cho self_talk material
    director_loop._runtime_ctx_fn = rt._build_runtime_context  # noqa: SLF001

    # Hook chat activity — chat đến → reset silence + đếm activity (cho ChatPulse/urge)
    _orig_process = router._process  # noqa: SLF001

    async def _hook_process(event):
        if autonomy is not None:
            autonomy.on_external_activity()
        rt.note_chat_activity()
        await _orig_process(event)

    router._process = _hook_process  # noqa: SLF001

    return rt


def _make_pref_logger(loader):
    """T2: JsonlWriter cho logs/pref_pairs.jsonl (DPO pairs). None nếu lỗi."""
    try:
        from pathlib import Path

        from orchestrator.logger import JsonlWriter
        log_dir = loader.get("logging", "jsonl.dir", "logs")
        return JsonlWriter(Path(log_dir) / "pref_pairs.jsonl")
    except Exception:
        return None
