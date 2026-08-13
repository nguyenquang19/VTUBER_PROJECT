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
from orchestrator.runtime_tts import (
    TTSRuntimeStack as _TTSRuntimeStack,
    build_tts_runtime_stack as _build_tts_runtime_stack,
)
from orchestrator.runtime_feature_bindings import (
    attach_boolean_feature,
    attach_set_enabled_feature,
)
from orchestrator.runtime_operations import (
    build_control_plane,
    build_emergency_controller,
    build_health_supervisor,
    build_incident_log,
    configure_shutdown_coordinator,
    start_dashboard,
)
from services.autonomy.material_provider import RuntimeContext
from services.input.chat_router import ChatRouter
from services.llm.canned_response import CannedResponder
from services.llm.llama_cpp_llm import LlamaCppLLMService
from services.llm.llm_turn import LLMTurnRunner
from services.llm.process_manager import LlamaServerConfig, LlamaServerProcessManager
from services.llm.prompt_manager import PromptManager
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)


SpeakFn = Callable[[str, str], Awaitable[Any]]


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
        conversation_context: Any = None,
        repair_policy: Any = None,
        behavior_library: Any = None,
        relationship_manager: Any = None,
        health_supervisor: Any = None,
        llama_process_manager: Any = None,
        dashboard_ref: dict[str, Any] | None = None,
        shutdown_coordinator: Any = None,
        control_plane: Any = None,
        emergency_controller: Any = None,
        incident_log: Any = None,
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
        self._conversation_context = conversation_context
        self._repair_policy = repair_policy
        self._behavior_library = behavior_library
        self._relationship_manager = relationship_manager
        self._health_supervisor = health_supervisor
        self._llama_process_manager = llama_process_manager
        self._dashboard_ref = dashboard_ref
        self._shutdown_coordinator = shutdown_coordinator
        self._control_plane = control_plane
        self._emergency_controller = emergency_controller
        self._incident_log = incident_log
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
        if self._conversation_context is not None:
            await self._conversation_context.start()
        if self._repair_policy is not None:
            await self._repair_policy.start()
        if self._behavior_library is not None:
            await self._behavior_library.start()
        if self._relationship_manager is not None:
            await self._relationship_manager.start()
        if self._control_plane is not None:
            await self._control_plane.start()
        if self._emergency_controller is not None:
            await self._emergency_controller.start()
        if self._incident_log is not None:
            await self._incident_log.start()
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
        if self._health_supervisor is not None:
            await self._health_supervisor.start()
        if self._shutdown_coordinator is not None:
            await self._shutdown_coordinator.start()
        self._log.info(
            "stream_runtime_ready",
            tts=self.cfg.enable_tts, memory=self.cfg.enable_memory,
            director=self._director_loop is not None,
            autonomy=self.cfg.enable_autonomy and self._autonomy is not None,
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._shutdown_coordinator is not None:
            await self._shutdown_coordinator.shutdown()
            return
        await self._stop_all_components()

    async def _stop_recovery(self) -> None:
        if self._health_supervisor is not None:
            self._health_supervisor.pause_recovery("shutdown")
            with contextlib.suppress(Exception):
                await self._health_supervisor.stop()

    async def _stop_driver(self) -> None:
        # Stop director loop (C0.4) hoặc autonomy loop cũ
        if self._director_loop is not None:
            with contextlib.suppress(Exception):
                await self._director_loop.stop()
        if self._autonomy_task is not None and not self._autonomy_task.done():
            self._autonomy_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._autonomy_task
            self._autonomy_task = None

    async def _stop_input(self) -> None:
        # Stop router (cascades sources + emotion)
        try:
            await self._router.stop()
        except Exception as e:  # pragma: no cover
            self._log.warning("router_stop_failed", error=str(e))

    async def _stop_speech(self) -> None:
        # TTS
        if self._tts_pipeline is not None:
            with contextlib.suppress(Exception):
                await self._audio_player.stop()
            with contextlib.suppress(Exception):
                await self._tts_svc.stop()

    async def _stop_supporting_services(self) -> None:
        # Memory
        if self._memory is not None:
            with contextlib.suppress(Exception):
                await self._memory.stop()
        if self._filter_svc is not None:
            with contextlib.suppress(Exception):
                await self._filter_svc.stop()

        for service in (
            self._goal_proposal, self._thread_extractor, self._conversation_context,
            self._repair_policy, self._behavior_library, self._relationship_manager,
            self._control_plane, self._emergency_controller, self._incident_log,
        ):
            if service is not None:
                with contextlib.suppress(Exception):
                    await service.stop()

    async def _stop_dashboard(self) -> None:
        # Dashboard
        server = self._dashboard_ref.get("server") if self._dashboard_ref is not None else None
        if server is not None and hasattr(server, "shutdown"):
            with contextlib.suppress(Exception):
                await server.shutdown()
        dashboard_task = (
            self._dashboard_ref.get("task")
            if self._dashboard_ref is not None else self._dashboard_task
        )
        if dashboard_task is not None and not dashboard_task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(dashboard_task), timeout=2.0)
        if dashboard_task is not None and not dashboard_task.done():
            dashboard_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dashboard_task

    async def _stop_llm(self) -> None:
        # LLM
        with contextlib.suppress(Exception):
            await self._llm_svc.stop()
        if self._llama_process_manager is not None:
            with contextlib.suppress(Exception):
                await self._llama_process_manager.stop()

    async def _stop_agent_state(self) -> None:
        if self._goal_manager is not None:
            with contextlib.suppress(Exception):
                await self._goal_manager.stop()
        if self._agent_state is not None:
            with contextlib.suppress(Exception):
                await self._agent_state.stop()

    async def _stop_all_components(self) -> None:
        await self._stop_recovery()
        await self._stop_driver()
        await self._stop_input()
        await self._stop_speech()
        await self._stop_dashboard()
        await self._stop_supporting_services()
        await self._stop_llm()
        await self._stop_agent_state()

    def set_shutdown_coordinator(self, coordinator: Any) -> None:
        self._shutdown_coordinator = coordinator

    def shutdown_steps(self) -> tuple[tuple[str, Callable[[], Awaitable[None]]], ...]:
        """Return the ordered public shutdown contract used by operations."""
        return (
            ("pause_recovery", self._stop_recovery),
            ("stop_driver", self._stop_driver),
            ("stop_input", self._stop_input),
            ("stop_speech", self._stop_speech),
            ("close_dashboard", self._stop_dashboard),
            ("stop_supporting_services", self._stop_supporting_services),
            ("stop_llm", self._stop_llm),
            ("stop_agent_state", self._stop_agent_state),
        )

    def operations_snapshot(self) -> dict[str, Any]:
        return {
            "runtime": {
                "running": self._running,
                "session_id": getattr(self._runner, "session_id", None),
                "dashboard_enabled": self.cfg.enable_dashboard,
                "tts_enabled": self.cfg.enable_tts and self._tts_pipeline is not None,
            },
            "agent": (
                self._agent_state.snapshot().to_dict() if self._agent_state is not None else None
            ),
            "goals": (
                self._goal_manager.snapshot().to_dict() if self._goal_manager is not None else None
            ),
            "health_supervisor": (
                self._health_supervisor.snapshot() if self._health_supervisor is not None else None
            ),
            "operations": (
                self._control_plane.snapshot() if self._control_plane is not None else None
            ),
            "emergency": (
                self._emergency_controller.snapshot()
                if self._emergency_controller is not None else None
            ),
            "incidents": self._incident_log.snapshot() if self._incident_log is not None else None,
            "decisions": (
                self._director_loop.decision_snapshot()
                if self._director_loop is not None else None
            ),
            "metrics": self.get_metrics(),
        }

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
                ctx = self.runtime_context()
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
        async with self._router.turn_lock:
            deferred = hasattr(self._runner, "finalize_delivery")
            try:
                if deferred:
                    parsed = await self._runner.run_ambient_turn(
                        req_id, decision.prompt_text, defer_delivery_commit=True,
                    )
                else:
                    parsed = await self._runner.run_ambient_turn(
                        req_id, decision.prompt_text,
                    )
            except Exception as e:
                self._log.warning("ambient_turn_failed", error=str(e))
                return

            # Canned fallback giữ ok=False cho data-quality nhưng text vẫn phải
            # được deliver trong legacy autonomy path.
            if not parsed.text:
                if deferred:
                    self._runner.finalize_delivery(req_id, False)
                return

            # Post-check dedup: regen 1 lần nếu quá giống ambient gần đây
            if self._autonomy.check_dedup(parsed.text):
                self._log.info("ambient_dedup_hit_regen", category=decision.category)
                regen_req_id = req_id + "_r"
                try:
                    if deferred:
                        regenerated = await self._runner.run_ambient_turn(
                            regen_req_id,
                            decision.prompt_text,
                            defer_delivery_commit=True,
                        )
                    else:
                        regenerated = await self._runner.run_ambient_turn(
                            regen_req_id, decision.prompt_text,
                        )
                    if regenerated.text:
                        if deferred:
                            self._runner.finalize_delivery(req_id, False)
                        req_id = regen_req_id
                        parsed = regenerated
                    elif deferred:
                        self._runner.finalize_delivery(regen_req_id, False)
                except Exception:
                    pass  # fail-open N7, dùng bản đầu

            if self._speak is None:
                if deferred:
                    self._runner.finalize_delivery(req_id, False)
                self._log.warning("ambient_delivery_sink_missing", request_id=req_id)
                return
            try:
                delivery = await self._speak(req_id, parsed.text)
            except Exception as e:
                if deferred:
                    self._runner.finalize_delivery(req_id, False)
                self._log.warning("ambient_speak_failed", error=str(e))
                return
            delivered = getattr(delivery, "delivered", False) is True
            if deferred:
                self._runner.finalize_delivery(req_id, delivered)
            if not delivered:
                self._log.warning(
                    "ambient_delivery_not_reached",
                    request_id=req_id,
                    mode=str(getattr(delivery, "mode", "unknown")),
                )
                return

            # Continuity/autonomy state changes only after a typed delivery success.
            self._autonomy.on_self_spoke(parsed.text)
            self._runner.commit_self_talk(parsed.text)

    # ─────────────────────── Context builders ───────────────────────

    def _get_current_mood(self) -> MoodState:
        try:
            return self._emotion.current_mood()
        except Exception:
            return MoodState()

    def runtime_context(self) -> RuntimeContext:
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
        environment_summary: str | None = None
        # Working memory từ MemoryFallbackManager nếu có
        if self._memory is not None:
            try:
                snap = self._memory.fallback_snapshot()
                memory_recent = [e.content for e in snap[-3:]]
            except Exception:
                pass

        # Recent grounded chat/state is available even when semantic memory is
        # disabled. This gives Thought Engine a live anchor instead of forcing
        # silence-only introspection.
        if self._agent_state is not None:
            try:
                state = self._agent_state.snapshot()
                grounded_chat = [
                    str(event.payload.get("text") or "").strip()[:240]
                    for event in state.recent_events
                    if event.kind in {
                        AgentEventKind.CHAT_RECEIVED,
                        AgentEventKind.DONATION_RECEIVED,
                    }
                    and str(event.payload.get("text") or "").strip()
                ]
                memory_recent = [*memory_recent, *grounded_chat[-3:]][-3:]
                environment = state.environment_summary or {}
                environment_summary = str(environment.get("summary") or "").strip() or None
            except Exception:
                pass

        return RuntimeContext(
            silence_seconds=silence,
            chat_count_last_10min=len(self._chat_activity_ts),
            operator_online=False,  # MVP chưa detect operator
            consecutive_ignored=self._autonomy.urge.consecutive_ignored,
            working_memory_recent=memory_recent,
            environment_summary=environment_summary,
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
        if self._relationship_manager is not None:
            with contextlib.suppress(Exception):
                m.update(self._relationship_manager.get_metrics())
        if self._health_supervisor is not None:
            with contextlib.suppress(Exception):
                m.update(self._health_supervisor.get_metrics())
        if self._control_plane is not None:
            with contextlib.suppress(Exception):
                m.update(self._control_plane.get_metrics())
        if self._emergency_controller is not None:
            with contextlib.suppress(Exception):
                m.update(self._emergency_controller.get_metrics())
        if self._incident_log is not None:
            with contextlib.suppress(Exception):
                m.update(self._incident_log.get_metrics())
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
                        source_id for source_id in self._router.source_ids
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
    from orchestrator.runtime_config_validation import validate_runtime_config

    validate_runtime_config(loader)

    # 1.1.0: fail-fast nếu record wire-schema lệch fingerprint đã chốt (drift guard).
    from services.data.record_schema import assert_no_schema_drift
    _registry = loader.get("data_schema_registry", "fingerprints", {}) or {}
    assert_no_schema_drift(_registry)

    metrics = MetricsCollector()
    # B0: setup structlog + JSONL sinks (turns.jsonl để baseline eval)
    turn_logger = setup_from_config(loader, metrics=metrics)
    pref_logger = _make_pref_logger(loader)   # T2: DPO pairs sink
    feature_manager = FeatureManager.from_config(loader)

    # M1: one shared grounded working state for every stream producer.
    from services.agent.agent_state import AgentState
    from services.agent.event_ledger import EventLedger
    from services.agent.agenda_policy import AgendaPolicy
    from services.agent.goal_manager import GoalManager
    from services.agent.open_thread_manager import OpenThreadManager
    from services.agent.thread_detector import RuleThreadDetector
    from services.agent.topic_matcher import LexicalTopicMatcher
    from services.agent.conversation_move_planner import ConversationMovePlanner
    from services.agent.session_recap import SessionRecapManager
    from services.agent.mood_policy import MoodActionPolicy

    event_ledger = EventLedger.from_loader(loader, metrics=metrics)
    topic_matcher = LexicalTopicMatcher.from_loader(loader, metrics=metrics)
    conversation_move_planner = ConversationMovePlanner.from_loader(
        loader, metrics=metrics,
    )
    thread_detector = RuleThreadDetector.from_loader(loader, matcher=topic_matcher)
    open_thread_manager = OpenThreadManager.from_loader(
        loader, metrics=metrics, detector=thread_detector,
        move_planner=conversation_move_planner, matcher=topic_matcher,
    )
    session_recap = SessionRecapManager.from_loader(loader, metrics=metrics)
    agent_state = AgentState.from_loader(
        loader, event_ledger, thread_manager=open_thread_manager,
        recap_manager=session_recap,
    )
    try:
        mood_policy_status = await feature_manager.get_status("mood_behavior_policy")
        mood_policy_enabled = mood_policy_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("mood_behavior_policy_feature_missing")
        mood_policy_enabled = False
    mood_policy = MoodActionPolicy.from_loader(
        loader, metrics=metrics, enabled=mood_policy_enabled,
    )
    agenda_policy = AgendaPolicy.from_loader(loader, mood_policy=mood_policy)
    goal_manager = GoalManager.from_loader(
        loader, metrics=metrics, on_active_changed=agent_state.set_active_goal_ref,
        audit_sink=agent_state.record, agenda_policy=agenda_policy,
    )
    agent_state.add_event_listener(goal_manager.handle_event)

    # M7: pseudonymous relationship persistence is independent from semantic memory.
    from orchestrator.migration_runner import MigrationRunner
    from services.relationship.manager import RelationshipManager
    from services.relationship.store import RelationshipStore
    try:
        relationship_status = await feature_manager.get_status("relationship_memory")
        relationship_enabled = relationship_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("relationship_memory_feature_missing")
        relationship_enabled = False
    MigrationRunner.from_config(loader).initialize()
    relationship_manager = RelationshipManager.from_loader(
        loader,
        store=RelationshipStore(loader.get("system", "paths.db_file", "data/mai.db")),
        metrics=metrics,
        enabled=relationship_enabled,
        evidence_exists=lambda event_id: any(
            item.event_id == event_id for item in agent_state.snapshot().recent_events
        ),
    )

    attach_set_enabled_feature(
        feature_manager, "relationship_memory", relationship_manager,
    )

    # ─── LLM stack ───
    try:
        operations_status = await feature_manager.get_status("live_operations")
        operations_enabled = operations_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("live_operations_feature_missing")
        operations_enabled = False
    llama_process_manager = None
    if operations_enabled and bool(loader.get(
        "operations", "health_supervisor.manage_llama_process", True,
    )):
        llama_process_manager = LlamaServerProcessManager(
            LlamaServerConfig.from_loader(loader),
        )
        await llama_process_manager.start()
    llm_svc = LlamaCppLLMService.from_loader(loader)
    await llm_svc.start()
    health = await llm_svc.health_check()
    if not health.is_ok:
        await llm_svc.stop()
        if llama_process_manager is not None:
            await llama_process_manager.stop()
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

    attach_set_enabled_feature(
        feature_manager, "thread_extraction", thread_extractor,
    )
    attach_set_enabled_feature(feature_manager, "goal_proposals", goal_proposal)

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
    from services.agent.conversation_context import ConversationContextComposer
    from services.agent.repair_policy import ConversationRepairPolicy
    repair_policy = ConversationRepairPolicy.from_loader(loader, metrics=metrics)
    conversation_context = ConversationContextComposer.from_loader(
        loader, goal_provider=goal_manager.snapshot, metrics=metrics,
        repair_policy=repair_policy,
        relationship_context=relationship_manager,
    )
    try:
        continuity_status = await feature_manager.get_status("conversation_continuity")
        continuity_enabled = continuity_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("conversation_continuity_feature_missing")
        continuity_enabled = False
    open_thread_manager.set_enabled(continuity_enabled)

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
    emotion = EmotionOrchestrator.from_loader(
        loader, memory=None, agent_state=agent_state, metrics=metrics,
    )
    goal_manager.set_mood_context_providers(
        emotion.current_mood, emotion.active_tone_flags,
    )

    # ─── Memory (optional) ───
    memory = None
    memory_extractor = None
    if cfg.enable_memory:
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
        emotion.set_memory_service(memory)
    relationship_manager.set_memory_service(memory)

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
        conversation_context_renderer=conversation_context if continuity_enabled else None,
    )

    async def _enable_conversation_continuity() -> None:
        open_thread_manager.set_enabled(True)
        runner.set_conversation_context_renderer(conversation_context)

    async def _disable_conversation_continuity() -> None:
        open_thread_manager.set_enabled(False)
        runner.set_conversation_context_renderer(None)

    async def _conversation_continuity_health() -> bool:
        return runner.conversation_context_enabled and open_thread_manager.enabled

    feature_manager.attach_handlers(
        "conversation_continuity",
        enable=_enable_conversation_continuity,
        disable=_disable_conversation_continuity,
        health=_conversation_continuity_health,
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
    emergency_ref: dict[str, Any] = {"controller": None}
    if cfg.enable_tts:
        from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult

        tts_stack = await _build_tts_runtime_stack(loader, metrics)
        tts_svc = tts_stack.primary
        audio_player = tts_stack.player
        tts_pipeline = tts_stack.pipeline

        async def _speak(req_id: str, text: str) -> TTSDeliveryResult:
            return await tts_pipeline.speak(req_id, text)

        speak_callback = _speak

    # ─── A3: Response pacing + filler ───
    # Wrap speak_callback: delay biến thiên trước khi nói + filler audio (nếu có clip).
    # Áp cho CẢ chat reply lẫn ambient (dùng chung _speak boundary).
    from services.tts.pacing import FillerManager, ResponsePacer
    from services.tts.natural_timing import NaturalTimingPolicy

    pacer = ResponsePacer.from_loader(loader)
    filler = FillerManager.from_loader(loader)
    try:
        timing_status = await feature_manager.get_status("natural_timing")
        timing_enabled = timing_status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)
    except KeyError:
        get_logger("stream_runtime").warning("natural_timing_feature_missing")
        timing_enabled = False
    natural_timing = NaturalTimingPolicy.from_loader(
        loader, metrics=metrics, enabled=timing_enabled,
    )

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

        async def _paced_speak(req_id: str, text: str) -> Any:
            emergency = emergency_ref.get("controller")
            if emergency is not None and not emergency.permits_speech():
                return TTSDeliveryResult(
                    request_id=req_id, mode=TTSDeliveryMode.CANCELLED, cancelled=True,
                )
            plan = natural_timing.plan(req_id, text, pacer)
            if plan.delay_seconds > 0:
                await asyncio.sleep(plan.delay_seconds)
            if emergency is not None and not emergency.permits_speech():
                return TTSDeliveryResult(
                    request_id=req_id, mode=TTSDeliveryMode.CANCELLED, cancelled=True,
                )
            if plan.allow_filler:
                clip = filler.maybe_pick(time.time())
                if clip is not None:
                    await _play_filler_clip(req_id, clip)
            if emergency is not None and not emergency.permits_speech():
                if audio_player is not None:
                    await audio_player.cancel_all()
                return TTSDeliveryResult(
                    request_id=req_id, mode=TTSDeliveryMode.CANCELLED, cancelled=True,
                )
            delivery = await _raw_speak(req_id, text)
            if tts_pipeline is not None:
                natural_timing.observe_ttfa(
                    tts_pipeline.get_metrics().get("tts_pipeline_last_ttfa_ms")
                )
            return delivery

        speak_callback = _paced_speak

    # ─── Autonomy ───
    autonomy = None
    if cfg.enable_autonomy:
        autonomy = AutonomyEngine.from_loader(loader)

    # Self-talk content planner is separate from Director timing/priority.
    from services.autonomy.self_talk_planner import SelfTalkPlanner
    from services.emotion.mood_style import MoodStyleTable
    try:
        self_talk_status = await feature_manager.get_status("self_talk_planner")
        self_talk_enabled = self_talk_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("self_talk_planner_feature_missing")
        self_talk_enabled = False
    self_talk_planner = SelfTalkPlanner.from_loader(
        loader,
        mood_style=MoodStyleTable.from_loader(loader),
        enabled=self_talk_enabled,
    )

    # ─── C0.4: Director stack — cầm nhịp thay FIFO ───
    from services.director.chat_pulse import ChatPulse
    from services.director.action_context import ActionContextBuilder
    from services.director.director import Director
    from services.director.director_loop import DirectorLoop
    from services.director.proactive_policy import ProactiveHostingPolicy
    from services.director.salience import SaliencePool

    pool = SaliencePool.from_loader(loader)
    pulse = ChatPulse.from_loader(loader)
    try:
        proactive_status = await feature_manager.get_status("proactive_hosting")
        proactive_enabled = proactive_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("proactive_hosting_feature_missing")
        proactive_enabled = False
    proactive_policy = ProactiveHostingPolicy.from_loader(
        loader, metrics=metrics, enabled=proactive_enabled,
    )
    from services.agent.behavior_library import BehaviorLibrary
    try:
        behavior_status = await feature_manager.get_status("behavior_library")
        behavior_enabled = behavior_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("behavior_library_feature_missing")
        behavior_enabled = False
    behavior_library = BehaviorLibrary.from_loader(
        loader, metrics=metrics, enabled=behavior_enabled,
    )
    try:
        chat_gate_status = await feature_manager.get_status("director_chat_gate")
        chat_gate_enabled = chat_gate_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("director_chat_gate_feature_missing")
        chat_gate_enabled = False
    director = Director.from_loader(
        pool, pulse, loader, mood_policy=mood_policy,
        proactive_policy=proactive_policy,
        chat_gate_enabled=chat_gate_enabled,
    )

    async def _enable_director_chat_gate() -> None:
        director.set_chat_gate_enabled(True)

    async def _disable_director_chat_gate() -> None:
        director.set_chat_gate_enabled(False)

    async def _director_chat_gate_health() -> bool:
        return director.chat_gate_enabled

    feature_manager.attach_handlers(
        "director_chat_gate",
        enable=_enable_director_chat_gate,
        disable=_disable_director_chat_gate,
        health=_director_chat_gate_health,
    )
    action_context_builder = ActionContextBuilder.from_loader(loader)
    from services.director.action_transaction import ActionTransactionManager
    try:
        transaction_status = await feature_manager.get_status("action_transactions")
        transactions_enabled = transaction_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("action_transactions_feature_missing")
        transactions_enabled = False
    action_transactions = ActionTransactionManager.from_loader(
        loader, metrics=metrics, enabled=transactions_enabled,
    )
    from services.director.decision_record import DecisionRecordManager
    try:
        decision_record_status = await feature_manager.get_status("decision_records")
        decision_records_enabled = decision_record_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("decision_records_feature_missing")
        decision_records_enabled = False
    decision_records = DecisionRecordManager.from_loader(
        loader, metrics=metrics, enabled=decision_records_enabled,
    )
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
        relationship_manager=relationship_manager,
    )

    # Animation adapter (VTube Studio) — gate qua feature `animation_smooth`.
    from services.animation.vts_service import VTSAnimationService
    try:
        animation_status = await feature_manager.get_status("animation_smooth")
        animation_enabled = animation_status in (
            FeatureStatus.ENABLED, FeatureStatus.DEGRADED,
        )
    except KeyError:
        get_logger("stream_runtime").warning("animation_smooth_feature_missing")
        animation_enabled = False
    animation = VTSAnimationService.from_loader(loader, enabled=animation_enabled)
    await animation.start()

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
        behavior_library=behavior_library,
        repair_policy=repair_policy,
        transaction_manager=action_transactions,
        decision_records=decision_records,
        self_talk_planner=self_talk_planner,
        thread_manager=open_thread_manager,
        animation=animation,
    )

    attach_set_enabled_feature(
        feature_manager, "self_talk_planner", self_talk_planner,
    )

    async def _enable_animation() -> None:
        animation.set_enabled(True)
        if not animation._transport.connected:
            await animation.start()

    async def _disable_animation() -> None:
        animation.set_enabled(False)

    async def _animation_health() -> bool:
        return (await animation.health_check()).is_ok

    feature_manager.attach_handlers(
        "animation_smooth",
        enable=_enable_animation,
        disable=_disable_animation,
        health=_animation_health,
    )

    attach_set_enabled_feature(
        feature_manager, "action_transactions", action_transactions,
    )
    attach_set_enabled_feature(feature_manager, "decision_records", decision_records)
    attach_boolean_feature(
        feature_manager,
        "director_goal_arbiter",
        set_enabled=director_loop.set_goal_arbitration_enabled,
        is_enabled=lambda: director_loop.goal_arbitration_enabled,
    )
    attach_set_enabled_feature(
        feature_manager, "mood_behavior_policy", mood_policy,
    )

    attach_boolean_feature(
        feature_manager,
        "mood_v2_shadow",
        set_enabled=emotion.set_affect_shadow_enabled,
        is_enabled=lambda: emotion.affect_shadow_enabled,
    )
    attach_boolean_feature(
        feature_manager,
        "mood_v2_prompt",
        set_enabled=emotion.set_affect_prompt_enabled,
        is_enabled=lambda: emotion.affect_prompt_enabled,
    )
    attach_set_enabled_feature(feature_manager, "proactive_hosting", proactive_policy)
    attach_set_enabled_feature(feature_manager, "behavior_library", behavior_library)
    attach_set_enabled_feature(feature_manager, "natural_timing", natural_timing)

    # ─── M9 operator control plane ───
    control_plane = build_control_plane(
        enabled=operations_enabled,
        director_loop=director_loop,
        goal_manager=goal_manager,
        pool=pool,
        loader=loader,
        metrics=metrics,
    )
    incident_log = build_incident_log(
        enabled=operations_enabled, loader=loader, metrics=metrics,
    )

    # ─── Dashboard (optional) ───
    dashboard_task, dashboard_ref, dashboard_server = start_dashboard(
        enabled=cfg.enable_dashboard,
        loader=loader,
        feature_manager=feature_manager,
        metrics=metrics,
        filter_svc=filter_svc,
        regenerator=regenerator,
        emotion=emotion,
        runner=runner,
        agent_state=agent_state,
        goal_manager=goal_manager,
        relationship_manager=relationship_manager,
        decision_records=decision_records,
        self_talk_planner=self_talk_planner,
        control_plane=control_plane,
        incident_log=incident_log,
    )

    health_supervisor = build_health_supervisor(
        enabled=operations_enabled,
        loader=loader,
        metrics=metrics,
        incident_log=incident_log,
        turn_lock=turn_lock,
        llm_svc=llm_svc,
        llama_process_manager=llama_process_manager,
        router=router,
        tts_svc=tts_svc,
        dashboard_ref=dashboard_ref,
        dashboard_server=dashboard_server,
    )
    emergency_controller = build_emergency_controller(
        enabled=operations_enabled,
        loader=loader,
        metrics=metrics,
        control_plane=control_plane,
        director_loop=director_loop,
        tts_pipeline=tts_pipeline,
        audio_player=audio_player,
        goal_manager=goal_manager,
        health_supervisor=health_supervisor,
        emergency_ref=emergency_ref,
        dashboard_server=dashboard_server,
    )

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
        conversation_context=conversation_context,
        repair_policy=repair_policy,
        behavior_library=behavior_library,
        relationship_manager=relationship_manager,
        health_supervisor=health_supervisor,
        llama_process_manager=llama_process_manager,
        dashboard_ref=dashboard_ref,
        control_plane=control_plane,
        emergency_controller=emergency_controller,
        incident_log=incident_log,
    )
    configure_shutdown_coordinator(
        enabled=operations_enabled,
        loader=loader,
        runtime=rt,
        animation=animation,
        metrics=metrics,
    )
    # DirectorLoop dùng runtime ctx của rt (silence/chat_count/memory) cho self_talk material
    director_loop.set_runtime_context_provider(rt.runtime_context)

    # Hook chat activity — chat đến → reset silence + đếm activity (cho ChatPulse/urge)
    def _on_input_activity(event) -> None:
        if autonomy is not None:
            autonomy.on_external_activity()
        director_loop.on_chat_activity()
        rt.note_chat_activity()

    router.add_activity_listener(_on_input_activity)

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
