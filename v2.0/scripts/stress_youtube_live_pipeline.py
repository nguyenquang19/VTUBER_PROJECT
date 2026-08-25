"""Wall-clock YouTube replay through Director, llama.cpp, VieNeu and silent playback."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import queue
import sys
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from interfaces.base import HealthStatus  # noqa: E402
from interfaces.input import InputEvent, InputService  # noqa: E402
from interfaces.tts import AudioChunk, TTSDeliveryResult  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.autonomy_engine import AutonomyEngine  # noqa: E402
from orchestrator.emotion_orchestrator import EmotionOrchestrator  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
from orchestrator.metrics_collector import MetricsCollector  # noqa: E402
from orchestrator.runtime_tts import build_tts_runtime_stack  # noqa: E402
from interfaces.agent import AgentStateService  # noqa: E402
from services.agent.agenda_policy import AgendaPolicy  # noqa: E402
from services.cognition.context_builder import build_compatibility_context_projection  # noqa: E402
from services.agent.conversation_move_planner import (  # noqa: E402
    ConversationMovePlanner,
)
from services.state.agent import AgentState  # noqa: E402
from services.state.event_ledger import EventLedger  # noqa: E402
from services.state.world import WorldModelShadow  # noqa: E402
from services.state.authoritative import (  # noqa: E402
    AuthoritativeStateConfig,
    AuthoritativeStateReducer,
)
from services.ingress.adapters import (  # noqa: E402
    CanonicalAgentStateAdapter,
    CanonicalEventIngress,
)
from services.ingress.normalizer import CanonicalEventNormalizer  # noqa: E402
from services.agent.goal_manager import GoalManager  # noqa: E402
from services.agent.open_thread_manager import OpenThreadManager  # noqa: E402
from services.agent.thread_detector import RuleThreadDetector  # noqa: E402
from services.agent.topic_matcher import LexicalTopicMatcher  # noqa: E402
from interfaces.state import AgentEventKind  # noqa: E402
from services.autonomy.dedup import DedupBuffer  # noqa: E402
from services.autonomy.material_provider import RuntimeContext  # noqa: E402
from services.autonomy.self_talk_planner import SelfTalkPlanner  # noqa: E402
from services.director.action_context import ActionContextBuilder  # noqa: E402
from services.director.chat_pulse import ChatPulse  # noqa: E402
from services.director.director import DirectorAction  # noqa: E402
from services.director.director_loop import DirectorLoop  # noqa: E402
from services.director.salience import SaliencePool  # noqa: E402
from services.director.speech_style import summarize_speech_style  # noqa: E402
from services.emotion.mood_style import MoodStyleTable  # noqa: E402
from services.filter.regenerator import FilterRegenerator  # noqa: E402
from services.filter.rule_filter import RuleFilter  # noqa: E402
from services.input.chat_router import ChatRouter  # noqa: E402
from services.input.youtube_replay import (  # noqa: E402
    YouTubeReplayBurst,
    YouTubeReplayParseResult,
    group_youtube_replay_bursts,
    load_youtube_replay,
)
from services.llm.canned_response import CannedResponder  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.llm_turn import LLMTurnRunner  # noqa: E402
from services.llm.process_manager import (  # noqa: E402
    LlamaServerConfig,
    LlamaServerProcessManager,
)
from services.llm.prompt_manager import PromptManager  # noqa: E402
from services.tts.audio_player import AudioPlayer  # noqa: E402
from services.tts.natural_timing import NaturalTimingPolicy  # noqa: E402
from services.tts.pacing import ResponsePacer  # noqa: E402
from services.tts.subtitle_fallback import SubtitleFallbackService  # noqa: E402
from services.tts.vieneu_service import VieNeuTtsService  # noqa: E402

from scripts.simulate_youtube_replay import (  # noqa: E402
    _decision_manager,
    _director_from_config,
    _transaction_manager,
)
from scripts.stress_youtube_llm import InstrumentedLLMRunner  # noqa: E402


Clock = Callable[[], float]
AsyncSleep = Callable[[float], Awaitable[None]]


class PacedYouTubeReplayInputService(InputService):
    """Yield replay bursts on a wall-clock schedule while recording feed drift."""

    service_id = "input_youtube_wallclock_replay"

    def __init__(
        self,
        path: Path,
        *,
        base_time: datetime,
        burst_window_ms: int,
        replay_speed: float,
        clock: Clock = time.perf_counter,
        sleep: AsyncSleep = asyncio.sleep,
    ) -> None:
        if burst_window_ms <= 0 or replay_speed <= 0:
            raise ValueError("replay timing values must be positive")
        self.path = Path(path)
        self.base_time = base_time
        self.burst_window_ms = int(burst_window_ms)
        self.replay_speed = float(replay_speed)
        self._clock = clock
        self._sleep = sleep
        self.result: YouTubeReplayParseResult | None = None
        self.bursts: tuple[YouTubeReplayBurst, ...] = ()
        self.started = asyncio.Event()
        self.completed = asyncio.Event()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self._running = False
        self._events_emitted = 0
        self._drift_ms: list[float] = []

    async def start(self) -> None:
        if self._running:
            return
        self.result = await asyncio.to_thread(
            load_youtube_replay,
            self.path,
            base_time=self.base_time,
        )
        self.bursts = group_youtube_replay_bursts(
            self.result.events,
            window_ms=self.burst_window_ms,
        )
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running or self.result is None:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            events=len(self.result.events),
            emitted=self._events_emitted,
        )

    def get_metrics(self) -> dict[str, Any]:
        result = self.result
        return {
            "replay_events_total": len(result.events) if result else 0,
            "replay_events_emitted": self._events_emitted,
            "replay_bursts_total": len(self.bursts),
            "replay_duration_ms": result.duration_ms if result else 0,
            "replay_schedule_drift_ms": _stats(self._drift_ms),
        }

    async def event_stream(self) -> AsyncIterator[InputEvent]:
        if not self._running or self.result is None:
            raise RuntimeError("paced replay source has not been started")
        self.started_at = self._clock()
        self.started.set()
        try:
            for burst in self.bursts:
                if not self._running:
                    break
                target = self.started_at + (
                    burst.offset_ms / 1000.0 / self.replay_speed
                )
                delay = target - self._clock()
                if delay > 0:
                    await self._sleep(delay)
                actual = self._clock()
                self._drift_ms.append(max(0.0, (actual - target) * 1000.0))
                for event in burst.events:
                    if not self._running:
                        break
                    self._events_emitted += 1
                    yield event
        finally:
            self.completed_at = self._clock()
            self.completed.set()


class PlaybackObserver:
    """Thread-safe per-request playback evidence; never retains PCM."""

    def __init__(self, clock: Clock = time.perf_counter) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._active_chunks = 0
        self.audio_overlaps = 0
        self.chunks_started = 0
        self.chunks_completed = 0

    @staticmethod
    def parent_id(request_id: str) -> str:
        return str(request_id).split("#", 1)[0]

    def on_enqueue(
        self,
        chunk: AudioChunk,
        *,
        enqueue_started: float,
        enqueue_finished: float,
        queue_size: int,
    ) -> None:
        parent = self.parent_id(chunk.request_id)
        with self._lock:
            record = self._record(parent)
            if record["first_enqueue_at"] is None:
                record["first_enqueue_at"] = enqueue_finished
            record["last_enqueue_at"] = enqueue_finished
            record["enqueue_block_ms"] += max(
                0.0, (enqueue_finished - enqueue_started) * 1000.0,
            )
            record["max_queue_size"] = max(record["max_queue_size"], queue_size)
            if chunk.audio_bytes:
                record["chunk_count"] += 1
                record["audio_ms"] += int(chunk.duration_ms)

    def on_play_start(self, metadata: dict[str, Any]) -> None:
        now = self._clock()
        parent = self.parent_id(str(metadata["request_id"]))
        with self._lock:
            if self._active_chunks > 0:
                self.audio_overlaps += 1
            self._active_chunks += 1
            self.chunks_started += 1
            record = self._record(parent)
            if record["first_play_at"] is None:
                record["first_play_at"] = now

    def on_play_end(self, metadata: dict[str, Any]) -> None:
        now = self._clock()
        parent = self.parent_id(str(metadata["request_id"]))
        with self._lock:
            self._active_chunks = max(0, self._active_chunks - 1)
            self.chunks_completed += 1
            self._record(parent)["last_play_end_at"] = now

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._records.values()]

    def _record(self, request_id: str) -> dict[str, Any]:
        return self._records.setdefault(request_id, {
            "request_id": request_id,
            "first_enqueue_at": None,
            "last_enqueue_at": None,
            "first_play_at": None,
            "last_play_end_at": None,
            "enqueue_block_ms": 0.0,
            "max_queue_size": 0,
            "chunk_count": 0,
            "audio_ms": 0,
        })


class SilentRealtimeBackend:
    """Audio backend that blocks for PCM duration and never opens a device."""

    def __init__(
        self,
        expected: queue.Queue[dict[str, Any]],
        observer: PlaybackObserver,
        *,
        wait: Callable[[float], Any] | None = None,
    ) -> None:
        self._expected = expected
        self._observer = observer
        self._stop_event = threading.Event()
        self._wait = wait

    def play_blocking(self, _samples: np.ndarray, _sample_rate: int) -> None:
        metadata = self._expected.get(timeout=5.0)
        self._stop_event.clear()
        self._observer.on_play_start(metadata)
        duration_s = max(0.0, float(metadata["duration_ms"]) / 1000.0)
        if self._wait is not None:
            self._wait(duration_s)
        else:
            self._stop_event.wait(duration_s)
        self._observer.on_play_end(metadata)

    def stop(self) -> None:
        self._stop_event.set()


class TrackingAudioPlayer:
    """Proxy production AudioPlayer and correlate its backend calls with request IDs."""

    def __init__(
        self,
        sample_rate: int,
        *,
        queue_maxsize: int,
        observer: PlaybackObserver,
        pitch_semitones: float = 0.0,
        clock: Clock = time.perf_counter,
        backend_wait: Callable[[float], Any] | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self._clock = clock
        self._observer = observer
        self._expected: queue.Queue[dict[str, Any]] = queue.Queue()
        self._backend = SilentRealtimeBackend(
            self._expected,
            observer,
            wait=backend_wait,
        )
        self._delegate = AudioPlayer(
            sample_rate=self.sample_rate,
            backend=self._backend,
            queue_maxsize=int(queue_maxsize),
            pitch_semitones=pitch_semitones,
        )
        self.queue_maxsize = int(queue_maxsize)

    async def start(self) -> None:
        await self._delegate.start()

    async def stop(self) -> None:
        await self._delegate.stop()

    async def enqueue(self, chunk: AudioChunk) -> None:
        started = self._clock()
        if chunk.audio_bytes:
            self._expected.put({
                "request_id": str(chunk.request_id),
                "duration_ms": int(chunk.duration_ms),
            })
        await self._delegate.enqueue(chunk)
        finished = self._clock()
        metrics = self._delegate.get_metrics()
        self._observer.on_enqueue(
            chunk,
            enqueue_started=started,
            enqueue_finished=finished,
            queue_size=int(metrics.get("audio_queue_size") or 0),
        )

    async def cancel_current(self, request_id: str) -> None:
        await self._delegate.cancel_current(request_id)

    async def cancel_all(self) -> None:
        await self._delegate.cancel_all()

    async def wait_until_idle(
        self,
        *,
        timeout_s: float,
        poll_s: float = 0.05,
    ) -> tuple[bool, float]:
        started = self._clock()
        while self._clock() - started < timeout_s:
            metrics = self._delegate.get_metrics()
            if (
                int(metrics.get("audio_queue_size") or 0) == 0
                and not bool(metrics.get("audio_is_playing"))
            ):
                return True, self._clock() - started
            await asyncio.sleep(poll_s)
        return False, self._clock() - started

    def get_metrics(self) -> dict[str, Any]:
        return {
            **self._delegate.get_metrics(),
            "audio_queue_maxsize": self.queue_maxsize,
        }


class _ReadOnlyAgentState:
    def __init__(self, state: AgentStateService) -> None:
        self._state = state

    def snapshot(self) -> Any:
        return self._state.snapshot()

    def record(self, _event: Any) -> bool:
        return False


def build_live_quality_report(
    *,
    policy: dict[str, Any],
    source_metrics: dict[str, Any],
    deliveries: Sequence[dict[str, Any]],
    playback_records: Sequence[dict[str, Any]],
    queue_samples: Sequence[int],
    queue_maxsize: int,
    selected_chat_ages_s: Sequence[float],
    chat_to_audio_start_s: Sequence[float],
    post_source_drain_s: float,
    drain_completed: bool,
    audio_overlaps: int,
    committed_transactions: int,
    semantic_recent_window: int = 32,
    semantic_similarity_threshold: float = 0.72,
    formula_openers: tuple[str, ...] = ("mà", "trời ơi", "ủa", "ơ kìa"),
    question_endings: tuple[str, ...] = (
        "nhỉ", "hả", "à", "ư", "không", "chưa", "sao", "gì", "nào",
    ),
) -> dict[str, Any]:
    gates = dict(policy.get("gates") or {})
    playback_by_id = {
        str(item.get("request_id") or ""): item for item in playback_records
    }
    audio_turns = [
        item for item in playback_records
        if item.get("first_play_at") is not None and int(item.get("audio_ms") or 0) > 0
    ]
    subtitle_sentences = sum(
        int(item.get("subtitle_sentences") or 0) for item in deliveries
    )
    failed_sentences = sum(
        int(item.get("failed_sentences") or 0) for item in deliveries
    )
    total_sentences = sum(
        int(item.get("sentences_total") or 0) for item in deliveries
    )
    silent = [
        item for item in deliveries
        if str(item.get("mode") or "none") == "none"
        and str(item.get("request_id") or "") not in playback_by_id
    ]
    primary_failures = subtitle_sentences + failed_sentences
    subtitle_ratio = subtitle_sentences / max(1, total_sentences)
    successful_deliveries = sum(bool(item.get("delivered")) for item in deliveries)
    commit_mismatches = abs(int(committed_transactions) - successful_deliveries)
    semantic_buffer = DedupBuffer(
        window=semantic_recent_window,
        threshold=semantic_similarity_threshold,
    )
    semantic_duplicate_ids: list[str] = []
    for item in deliveries:
        if not bool(item.get("delivered")):
            continue
        text = str(item.get("text") or "").strip()
        if semantic_buffer.check(text):
            semantic_duplicate_ids.append(str(item.get("request_id") or ""))
        semantic_buffer.record(text)
    semantic_repetition_ratio = (
        len(semantic_duplicate_ids) / max(1, successful_deliveries)
    )
    continue_thread_deliveries = sum(
        bool(item.get("delivered")) and item.get("action") == "continue_thread"
        for item in deliveries
    )
    continue_thread_ratio = continue_thread_deliveries / max(1, successful_deliveries)
    room_reaction_deliveries = sum(
        bool(item.get("delivered"))
        and str(item.get("request_id") or "").startswith("room_")
        for item in deliveries
    )
    replay_minutes = max(
        1.0 / 60.0,
        float(source_metrics.get("replay_duration_ms") or 0) / 60_000.0,
    )
    room_reactions_per_minute = room_reaction_deliveries / replay_minutes
    delivered_texts = [
        str(item.get("text") or "").strip()
        for item in deliveries
        if bool(item.get("delivered")) and str(item.get("text") or "").strip()
    ]
    style = summarize_speech_style(
        delivered_texts,
        formula_openers=formula_openers,
        question_endings=question_endings,
    )
    drift = dict(source_metrics.get("replay_schedule_drift_ms") or {})
    queue_fill = [
        max(0.0, min(1.0, float(value) / max(1, queue_maxsize)))
        for value in queue_samples
    ]
    selected_age = _stats(selected_chat_ages_s)
    chat_audio = _stats(chat_to_audio_start_s)
    queue_fill_stats = _stats(queue_fill)
    boundary = _conversation_boundary_counts(deliveries)
    checks = {
        "minimum_audio_turns": len(audio_turns) >= int(
            gates.get("minimum_audio_turns", 50)
        ),
        "input_schedule_drift": _at_most(
            drift.get("p95"), gates.get("max_input_schedule_drift_p95_ms", 250)
        ),
        "selected_chat_age": _at_most(
            selected_age.get("p95"), gates.get("max_selected_chat_age_p95_s", 50)
        ),
        "chat_to_audio_start": _at_most(
            chat_audio.get("p95"), gates.get("max_chat_to_audio_start_p95_s", 20)
        ),
        "audio_queue_fill": _at_most(
            queue_fill_stats.get("p95"),
            gates.get("max_audio_queue_fill_p95_ratio", 0.95),
        ),
        "post_source_drain": drain_completed and post_source_drain_s <= float(
            gates.get("max_post_source_drain_s", 60)
        ),
        "audio_overlap": audio_overlaps <= int(gates.get("max_audio_overlaps", 0)),
        "silent_turns": len(silent) <= int(gates.get("max_silent_turns", 0)),
        "primary_failures": primary_failures <= int(
            gates.get("max_primary_failures", 0)
        ),
        "subtitle_fallback_ratio": subtitle_ratio <= float(
            gates.get("max_subtitle_fallback_ratio", 0.0)
        ),
        "delivery_commit_invariant": commit_mismatches <= int(
            gates.get("max_delivery_commit_mismatches", 0)
        ),
        "semantic_repetition": semantic_repetition_ratio <= float(
            gates.get("max_semantic_repetition_ratio", 1.0)
        ),
        "continue_before_source_read": boundary["continue_before_source_read"] <= int(
            gates.get("max_continue_before_source_read", 0)
        ),
        "cross_thread_continue_before_park": boundary[
            "cross_thread_continue_before_park"
        ] <= int(gates.get("max_cross_thread_continue_before_park", 0)),
        "room_reaction_before_park": boundary["room_reaction_before_park"] <= int(
            gates.get("max_room_reaction_before_park", 0)
        ),
        "old_thread_continue_after_room": boundary[
            "old_thread_continue_after_room"
        ] <= int(gates.get("max_old_thread_continue_after_room", 0)),
        "room_reaction_cadence": room_reactions_per_minute <= float(
            gates.get("max_room_reactions_per_minute", math.inf)
        ),
        "formula_opener_ratio": style.formula_opener_ratio <= float(
            gates.get("max_formula_opener_ratio", 1.0)
        ),
        "question_ending_ratio": style.question_ratio <= float(
            gates.get("max_question_ending_ratio", 1.0)
        ),
    }
    return {
        "live_pipeline_ready": all(checks.values()),
        "checks": checks,
        "counts": {
            "speak_attempts": len(deliveries),
            "successful_deliveries": successful_deliveries,
            "audio_turns_started": len(audio_turns),
            "audio_turns_completed": sum(
                item.get("last_play_end_at") is not None for item in audio_turns
            ),
            "silent_turns": len(silent),
            "silent_request_ids": [
                str(item.get("request_id") or "") for item in silent
            ],
            "subtitle_sentences": subtitle_sentences,
            "failed_sentences": failed_sentences,
            "primary_failures": primary_failures,
            "sentences_total": total_sentences,
            "committed_transactions": int(committed_transactions),
            "delivery_commit_mismatches": commit_mismatches,
            "audio_overlaps": int(audio_overlaps),
            "semantic_duplicate_outputs": len(semantic_duplicate_ids),
            "semantic_duplicate_request_ids": semantic_duplicate_ids,
            "continue_thread_deliveries": int(continue_thread_deliveries),
            **boundary,
            "room_reaction_deliveries": int(room_reaction_deliveries),
            "formula_opener_outputs": style.formula_openers,
            "question_outputs": style.questions,
        },
        "ratios": {
            "subtitle_fallback": round(subtitle_ratio, 4),
            "semantic_repetition": round(semantic_repetition_ratio, 4),
            "continue_thread": round(continue_thread_ratio, 4),
            "room_reactions_per_minute": round(room_reactions_per_minute, 4),
            "formula_openers": round(style.formula_opener_ratio, 4),
            "question_endings": round(style.question_ratio, 4),
        },
        "input_schedule_drift_ms": drift,
        "selected_chat_age_s": selected_age,
        "chat_to_audio_start_s": chat_audio,
        "audio_queue_fill_ratio": queue_fill_stats,
        "post_source_drain_s": round(float(post_source_drain_s), 3),
        "drain_completed": bool(drain_completed),
    }


def _conversation_boundary_counts(
    deliveries: Sequence[dict[str, Any]],
) -> dict[str, int]:
    """Evaluate public topic order from successful full-delivery metadata."""
    counts = {
        "continue_before_source_read": 0,
        "cross_thread_continue_before_park": 0,
        "room_reaction_before_park": 0,
        "old_thread_continue_after_room": 0,
    }
    source_read_threads: set[str] = set()
    current_thread: str | None = None
    after_room = False
    for item in deliveries:
        if not bool(item.get("delivered")):
            continue
        action = str(item.get("action") or "")
        request_id = str(item.get("request_id") or "")
        thread_id = str(item.get("thread_id") or "").strip() or None
        move = str(item.get("conversation_move") or "").strip().lower()
        is_room = request_id.startswith("room_")
        if is_room:
            if current_thread is not None:
                counts["room_reaction_before_park"] += 1
            current_thread = None
            after_room = True
            continue
        if action == "read_chat":
            if current_thread is not None and thread_id != current_thread:
                counts["cross_thread_continue_before_park"] += 1
            current_thread = thread_id
            if thread_id is not None:
                source_read_threads.add(thread_id)
            after_room = False
            continue
        if action != "continue_thread":
            continue
        if thread_id is None or thread_id not in source_read_threads:
            counts["continue_before_source_read"] += 1
        if current_thread is not None and thread_id != current_thread:
            counts["cross_thread_continue_before_park"] += 1
        if after_room:
            counts["old_thread_continue_after_room"] += 1
        current_thread = thread_id
        if move in {"park", "close", "invite"}:
            current_thread = None
    return counts


async def run_stress(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir) if args.config_dir else REPO_ROOT / "config"
    loader = ConfigLoader(config_dir)
    loader.load_all()
    policy = dict(loader.get(
        "evaluation", "evaluation.youtube_live_pipeline_stress", {},
    ) or {})
    source_path = _absolute(args.input or Path(str(policy.get("input_file"))))
    output = _absolute(args.output or Path(str(policy.get("output_file"))))
    replay_speed = float(
        args.replay_speed if args.replay_speed is not None
        else policy.get("replay_speed", 1.0)
    )
    burst_window_ms = int(policy.get("burst_window_ms", 1500))
    queue_maxsize = int(policy.get("audio_queue_maxsize", 128))
    sample_interval_s = float(policy.get("sample_interval_s", 0.25))
    drain_timeout_s = float(policy.get("drain_timeout_s", 90.0))
    progress_interval_s = float(policy.get("progress_interval_s", 30.0))
    max_trace_items = int(policy.get("max_trace_items", 1200))
    report_turn_sample = int(policy.get("report_turn_sample", 40))
    if replay_speed <= 0 or queue_maxsize <= 0 or sample_interval_s <= 0:
        raise ValueError("live stress timing and queue bounds must be positive")

    base_time = datetime.now(timezone.utc)
    source = PacedYouTubeReplayInputService(
        source_path,
        base_time=base_time,
        burst_window_ms=burst_window_ms,
        replay_speed=replay_speed,
    )
    await source.start()
    if source.result is None:
        raise RuntimeError("YouTube replay did not parse")

    metrics = MetricsCollector()
    pool = SaliencePool.from_loader(loader)
    pulse = ChatPulse.from_loader(loader)
    topic_matcher = LexicalTopicMatcher.from_loader(loader)
    move_planner = ConversationMovePlanner.from_loader(loader)
    thread_detector = RuleThreadDetector.from_loader(loader, matcher=topic_matcher)
    thread_manager = OpenThreadManager.from_loader(
        loader,
        detector=thread_detector,
        matcher=topic_matcher,
        move_planner=move_planner,
    )
    agent_state_store = AgentState.from_loader(
        loader,
        EventLedger.from_loader(loader),
        thread_manager=thread_manager,
    )
    world_state_store = WorldModelShadow.from_loader(loader, enabled=False)
    canonical_normalizer = CanonicalEventNormalizer.from_loader(loader)
    authoritative_state = AuthoritativeStateReducer(
        AuthoritativeStateConfig.from_loader(loader),
        agent_state=agent_state_store,
        world_model=world_state_store,
    )
    agent_state = CanonicalAgentStateAdapter(
        agent_state_store,
        canonical_normalizer,
        CanonicalEventIngress(authoritative_state),
    )
    goal_manager = GoalManager.from_loader(
        loader,
        on_active_changed=agent_state.set_active_goal_ref,
        audit_sink=agent_state.record,
        agenda_policy=AgendaPolicy.from_loader(loader),
    )
    agent_state.add_event_listener(goal_manager.handle_event)
    emotion = EmotionOrchestrator.from_loader(
        loader,
        agent_state=agent_state,
        metrics=metrics,
    )
    autonomy = AutonomyEngine.from_loader(loader)
    director = _director_from_config(
        loader,
        pool,
        pulse,
        duration_seconds=source.result.duration_ms / 1000.0,
        clock=time.time,
    )
    transactions = _transaction_manager(loader, time.time)
    decisions = _decision_manager(loader, time.time)
    self_talk = SelfTalkPlanner.from_loader(
        loader,
        mood_style=MoodStyleTable.from_loader(loader),
        enabled=bool(loader.get(
            "features", "features.self_talk_planner.enabled", True,
        )),
    )
    llama_manager = LlamaServerProcessManager(LlamaServerConfig.from_loader(loader))
    llm_service = LlamaCppLLMService.from_loader(loader)
    filter_service: RuleFilter | None = None
    tts_stack: Any = None
    player: TrackingAudioPlayer | None = None
    observer = PlaybackObserver()
    subtitle_events: list[str] = []
    deliveries: list[dict[str, Any]] = []
    decision_trace: list[dict[str, Any]] = []
    queue_samples: list[int] = []
    pool_samples: list[int] = []
    active_decision: dict[str, Any] = {}
    stop_monitor = asyncio.Event()
    runner: InstrumentedLLMRunner | None = None
    router: ChatRouter | None = None
    loop: DirectorLoop | None = None
    monitor_task: asyncio.Task[None] | None = None
    started_perf = time.perf_counter()
    started_components: list[str] = []

    try:
        await llama_manager.start()
        started_components.append("llama")
        await llm_service.start()
        health = await llm_service.health_check()
        if not health.is_ok:
            raise RuntimeError(health.message or "llama.cpp health gate failed")
        started_components.append("llm")

        regenerator: FilterRegenerator | None = None
        if bool(loader.get("features", "features.filter_rule.enabled", False)):
            filter_service = RuleFilter.from_config(loader)
            await filter_service.start()
            regenerator = FilterRegenerator.from_loader(
                loader,
                filter_service,
                llm_service,
                metrics=metrics,
            )
            started_components.append("filter")

        context = build_compatibility_context_projection(
            loader,
            goal_provider=goal_manager.snapshot,
        )
        delegate = LLMTurnRunner.from_loader(
            loader,
            llm_service,
            PromptManager.from_loader(loader),
            FallbackManager(),
            CannedResponder.from_loader(loader),
            regenerator=regenerator,
            session_id="youtube-live-pipeline-stress",
            agent_state=_ReadOnlyAgentState(agent_state),
            conversation_context_renderer=context,
        )
        runner = InstrumentedLLMRunner(
            delegate,
            llm_service,
            input_max_chars=int(loader.get(
                "evaluation", "evaluation.youtube_llm_stress.input_max_chars", 2400,
            )),
        )

        pitch = float(loader.get("models", "tts.pitch_semitones", 0.0) or 0.0)
        player_holder: dict[str, TrackingAudioPlayer] = {}

        def player_factory(sample_rate: int) -> TrackingAudioPlayer:
            value = TrackingAudioPlayer(
                sample_rate,
                queue_maxsize=queue_maxsize,
                observer=observer,
                pitch_semitones=pitch,
            )
            player_holder["player"] = value
            return value

        def subtitle_factory(_loader: Any) -> SubtitleFallbackService:
            return SubtitleFallbackService(
                on_subtitle=lambda request_id, _text: subtitle_events.append(request_id),
                require_delivery=True,
            )

        tts_stack = await build_tts_runtime_stack(
            loader,
            metrics,
            primary_factory=lambda value: VieNeuTtsService.from_loader(value),
            subtitle_factory=subtitle_factory,
            player_factory=player_factory,
        )
        player = player_holder.get("player")
        if tts_stack.primary is None or player is None:
            raise RuntimeError("real VieNeu/audio player unavailable for integrated stress")
        started_components.append("tts")
        pacer = ResponsePacer.from_loader(loader)
        natural_timing = NaturalTimingPolicy.from_loader(
            loader,
            metrics=metrics,
            enabled=bool(loader.get(
                "features", "features.natural_timing.enabled", True,
            )),
        )

        async def speak(request_id: str, text: str) -> TTSDeliveryResult:
            started = time.perf_counter()
            context_snapshot = dict(active_decision)
            plan = natural_timing.plan(request_id, text, pacer)
            if plan.delay_seconds > 0:
                await asyncio.sleep(plan.delay_seconds)
            result = await tts_stack.pipeline.speak(request_id, text)
            natural_timing.observe_ttfa(
                tts_stack.pipeline.get_metrics().get("tts_pipeline_last_ttfa_ms")
            )
            deliveries.append({
                "request_id": request_id,
                "action": context_snapshot.get("action"),
                "reason": context_snapshot.get("reason"),
                "selected_ids": list(context_snapshot.get("selected_ids") or ()),
                "selected_offsets_s": list(
                    context_snapshot.get("selected_offsets_s") or ()
                ),
                "selected_ages_s": list(context_snapshot.get("selected_ages_s") or ()),
                "speak_started_at": started,
                "enqueue_completed_at": time.perf_counter(),
                "pacing_delay_s": round(float(plan.delay_seconds), 3),
                "text": " ".join(str(text).split())[:500],
                "delivered": bool(result.delivered),
                "mode": result.mode.value,
                "sentences_total": result.sentences_total,
                "audio_sentences": result.audio_sentences,
                "subtitle_sentences": result.subtitle_sentences,
                "failed_sentences": result.failed_sentences,
                "ttfa_ms": _round_optional(
                    tts_stack.pipeline.get_metrics().get("tts_pipeline_last_ttfa_ms")
                ),
            })
            return result

        turn_lock = asyncio.Lock()
        router = ChatRouter(
            [source],
            emotion,
            runner,
            pool=pool,
            pulse=pulse,
            turn_lock=turn_lock,
            agent_state=agent_state,
        )
        loop = DirectorLoop(
            director,
            pool,
            pulse,
            runner,
            emotion=emotion,
            autonomy=autonomy,
            speak=speak,
            turn_lock=turn_lock,
            tick_seconds=float(loader.get(
                "director", "director.tick_seconds", 1.5,
            )),
            max_refs=int(loader.get(
                "director", "director.max_refs_per_turn", 3,
            )),
            clock=time.time,
            transaction_manager=transactions,
            decision_records=decisions,
            self_talk_planner=self_talk,
            agent_state=agent_state,
            goal_manager=goal_manager,
            thread_manager=thread_manager,
            action_context_builder=ActionContextBuilder.from_loader(loader),
            room_reaction_recent_window=int(loader.get(
                "director", "director.room_reaction.recent_window", 16,
            )),
            room_reaction_similarity_threshold=float(loader.get(
                "director", "director.room_reaction.similarity_threshold", 0.72,
            )),
            room_reaction_max_regenerations=int(loader.get(
                "director", "director.room_reaction.max_regenerations", 1,
            )),
            room_reaction_retry_defer_seconds=float(loader.get(
                "director", "director.room_reaction.retry_defer_seconds", 30.0,
            )),
            speech_dedup_recent_window=int(loader.get(
                "director", "director.speech_dedup.recent_window", 32,
            )),
            speech_dedup_similarity_threshold=float(loader.get(
                "director", "director.speech_dedup.similarity_threshold", 0.72,
            )),
            speech_dedup_max_regenerations=int(loader.get(
                "director", "director.speech_dedup.max_regenerations", 1,
            )),
            speech_style_recent_window=int(loader.get(
                "director", "director.speech_style.recent_window", 12,
            )),
            speech_style_formula_openers=tuple(loader.get(
                "director", "director.speech_style.formula_openers",
                ("mà", "trời ơi", "ủa", "ơ kìa"),
            ) or ()),
            speech_style_max_formula_openers=int(loader.get(
                "director", "director.speech_style.max_formula_openers", 2,
            )),
            speech_style_max_same_opener=int(loader.get(
                "director", "director.speech_style.max_same_opener", 1,
            )),
            speech_style_max_questions=int(loader.get(
                "director", "director.speech_style.max_questions", 2,
            )),
            speech_style_question_endings=tuple(loader.get(
                "director", "director.speech_style.question_endings", ("nhỉ",),
            ) or ()),
            speech_style_max_sentences=int(loader.get(
                "director", "director.speech_style.max_sentences", 2,
            )),
            speech_style_max_words=int(loader.get(
                "director", "director.speech_style.max_words", 65,
            )),
            speech_style_max_regenerations=int(loader.get(
                "director", "director.speech_style.max_regenerations", 1,
            )),
        )
        recent_context: list[str] = []
        activity = {"last": time.time(), "count": 0}

        def note_activity(event: InputEvent) -> None:
            now = time.time()
            activity["last"] = now
            activity["count"] += 1
            compact = " ".join(str(event.content).split())[:240]
            if compact:
                recent_context.append(compact)
                del recent_context[:-3]
            loop.on_chat_activity(now)

        router.add_activity_listener(note_activity)
        loop.set_runtime_context_provider(lambda: RuntimeContext(
            silence_seconds=max(0.0, time.time() - float(activity["last"])),
            chat_count_last_10min=int(activity["count"]),
            working_memory_recent=list(recent_context),
        ))

        await agent_state.start()
        await goal_manager.start()
        await transactions.start()
        await decisions.start()
        await self_talk.start()
        director.start(time.time())
        await router.start()
        started_components.append("runtime")
        await source.started.wait()

        async def monitor() -> None:
            last_progress = time.perf_counter()
            while not stop_monitor.is_set():
                player_metrics = player.get_metrics()
                queue_samples.append(int(player_metrics.get("audio_queue_size") or 0))
                pool_samples.append(pool.size())
                now = time.perf_counter()
                if now - last_progress >= progress_interval_s:
                    print(json.dumps({
                        "status": "running",
                        "source_events": source.get_metrics()["replay_events_emitted"],
                        "source_total": source.get_metrics()["replay_events_total"],
                        "llm_calls": len(runner.calls),
                        "deliveries": len(deliveries),
                        "audio_queue": player_metrics.get("audio_queue_size"),
                        "pool": pool.size(),
                    }, ensure_ascii=False), flush=True)
                    last_progress = now
                try:
                    await asyncio.wait_for(stop_monitor.wait(), timeout=sample_interval_s)
                except asyncio.TimeoutError:
                    continue

        monitor_task = asyncio.create_task(monitor(), name="live_stress_monitor")
        tick_s = float(loader.get("director", "director.tick_seconds", 1.5))
        while not source.completed.is_set():
            await asyncio.sleep(tick_s)
            preview = loop.preview_decision(time.time())
            selected = list(preview.refs)
            now_epoch = time.time()
            active_decision.clear()
            active_decision.update({
                "action": preview.action.value,
                "reason": preview.reason,
                "selected_ids": [item.msg_id for item in selected],
                "selected_offsets_s": [
                    max(0.0, item.created_at - base_time.timestamp())
                    for item in selected
                ],
                "selected_ages_s": [
                    max(0.0, now_epoch - item.created_at) for item in selected
                ],
            })
            delivery_start = len(deliveries)
            tick_started = time.perf_counter()
            action = await loop.tick_once()
            tick_finished = time.perf_counter()
            if len(deliveries) > delivery_start:
                snapshot = agent_state.snapshot()
                completed_by_request = {
                    str(event.provenance.source_event_id or ""): event
                    for event in snapshot.recent_events
                    if event.kind is AgentEventKind.SPEECH_COMPLETED
                }
                for delivered_item in deliveries[delivery_start:]:
                    request_id = str(delivered_item.get("request_id") or "")
                    event = completed_by_request.get(request_id)
                    if event is not None:
                        delivered_item["thread_id"] = event.payload.get("thread_id")
                        delivered_item["conversation_move"] = event.payload.get(
                            "conversation_move"
                        )
                    if (
                        delivered_item.get("action") == "read_chat"
                        and not request_id.startswith("room_")
                        and not delivered_item.get("thread_id")
                    ):
                        source_ids = {
                            f"agent:chat:{value}"
                            for value in delivered_item.get("selected_ids") or ()
                        }
                        matching_threads = [
                            thread for thread in snapshot.open_threads
                            if (
                                thread.origin_event_id in source_ids
                                or any(
                                    evidence.source_event_id in source_ids
                                    for evidence in thread.evidence
                                )
                            )
                        ]
                        if matching_threads:
                            delivered_item["thread_id"] = max(
                                matching_threads,
                                key=lambda item: (item.updated_at, item.thread_id),
                            ).thread_id
            if len(decision_trace) < max_trace_items:
                decision_trace.append({
                    "started_s": round(tick_started - source.started_at, 3),
                    "duration_s": round(tick_finished - tick_started, 3),
                    "preview_action": preview.action.value,
                    "actual_action": action.value,
                    "reason": preview.reason,
                    "selected_ids": list(active_decision["selected_ids"]),
                    "selected_ages_s": [
                        round(float(value), 3)
                        for value in active_decision["selected_ages_s"]
                    ],
                    "pool_after": pool.size(),
                    "queue_after": player.get_metrics().get("audio_queue_size"),
                })
            active_decision.clear()

        source_finished_at = source.completed_at or time.perf_counter()
        drain_completed, _drain_wait = await player.wait_until_idle(
            timeout_s=drain_timeout_s,
        )
        drain_finished_at = time.perf_counter()
        stop_monitor.set()
        if monitor_task is not None:
            await monitor_task
            monitor_task = None

        playback_records = observer.snapshot()
        playback_by_id = {
            str(item["request_id"]): item for item in playback_records
        }
        selected_ages = [
            float(age)
            for item in deliveries
            for age in item.get("selected_ages_s") or ()
        ]
        chat_audio_delays: list[float] = []
        for item in deliveries:
            playback = playback_by_id.get(str(item["request_id"]))
            if playback is None or playback.get("first_play_at") is None:
                continue
            play_relative = float(playback["first_play_at"]) - float(source.started_at)
            chat_audio_delays.extend(
                max(0.0, play_relative - float(offset))
                for offset in item.get("selected_offsets_s") or ()
            )
        transaction_snapshot = transactions.snapshot()
        committed = int(
            (transaction_snapshot.get("counts") or {}).get("committed", 0)
        )
        quality = build_live_quality_report(
            policy=policy,
            source_metrics=source.get_metrics(),
            deliveries=deliveries,
            playback_records=playback_records,
            queue_samples=queue_samples,
            queue_maxsize=queue_maxsize,
            selected_chat_ages_s=selected_ages,
            chat_to_audio_start_s=chat_audio_delays,
            post_source_drain_s=max(0.0, drain_finished_at - source_finished_at),
            drain_completed=drain_completed,
            audio_overlaps=observer.audio_overlaps,
            committed_transactions=committed,
            semantic_recent_window=int(loader.get(
                "director", "director.speech_dedup.recent_window", 32,
            )),
            semantic_similarity_threshold=float(loader.get(
                "director", "director.speech_dedup.similarity_threshold", 0.72,
            )),
            formula_openers=tuple(loader.get(
                "director", "director.speech_style.formula_openers",
                ("mà", "trời ơi", "ủa", "ơ kìa"),
            ) or ()),
            question_endings=tuple(loader.get(
                "director", "director.speech_style.question_endings", ("nhỉ",),
            ) or ()),
        )
        action_counts = Counter(item["actual_action"] for item in decision_trace)
        report = {
            "schema_version": 1,
            "mode": "youtube_wallclock_real_llm_tts_silent_playback",
            "input_file": str(source_path.resolve()),
            "elapsed_seconds": round(time.perf_counter() - started_perf, 3),
            "runtime": {
                "llm_backend": "llama.cpp",
                "tts_backend": "VieNeu-TTS v3 Turbo",
                "replay_speed": replay_speed,
                "audio_device_opened": False,
                "pcm_persisted": False,
                "audio_queue_maxsize": queue_maxsize,
                "llm_metrics": llm_service.get_metrics(),
                "tts_metrics": tts_stack.primary.get_metrics(),
                "tts_pipeline_metrics": tts_stack.pipeline.get_metrics(),
                "audio_metrics": player.get_metrics(),
            },
            "input": source.get_metrics(),
            "director": {
                "metrics": loop.get_metrics(),
                "action_counts": dict(sorted(action_counts.items())),
                "trace_count": len(decision_trace),
                "trace": decision_trace,
                "pool_metrics": pool.get_metrics(),
                "pool_size_samples": _stats(pool_samples),
            },
            "generation": {
                "calls": len(runner.calls),
                "latency_ms": _stats([
                    float(item["latency_ms"]) for item in runner.calls
                ]),
                "fallbacks": sum(
                    int(item.get("level_used") or 0) > 0 for item in runner.calls
                ),
            },
            "delivery": {
                "records": deliveries[:max(1, report_turn_sample)],
                "records_count": len(deliveries),
                "transactions": transaction_snapshot,
            },
            "playback": {
                "records": playback_records[:max(1, report_turn_sample)],
                "records_count": len(playback_records),
                "queue_size_samples": _stats(queue_samples),
                "queue_fill_ratio": _stats([
                    value / max(1, queue_maxsize) for value in queue_samples
                ]),
                "chunks_started": observer.chunks_started,
                "chunks_completed": observer.chunks_completed,
                "overlaps": observer.audio_overlaps,
            },
            "quality": quality,
        }
        _write_json(output, report)
        _print_summary(output, report)
        return 0 if quality["live_pipeline_ready"] else 1
    finally:
        stop_monitor.set()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        if router is not None and "runtime" in started_components:
            await router.stop()
        if loop is not None:
            await transactions.stop()
            await decisions.stop()
            await self_talk.stop()
        await goal_manager.stop()
        await agent_state.stop()
        if tts_stack is not None:
            if player is not None:
                await player.stop()
            if tts_stack.subtitle is not None:
                await tts_stack.subtitle.stop()
            if tts_stack.primary is not None:
                await tts_stack.primary.stop()
        if filter_service is not None:
            await filter_service.stop()
        if "llm" in started_components:
            await llm_service.stop()
        if "llama" in started_components:
            await llama_manager.stop()


def _at_most(value: Any, limit: Any) -> bool:
    if value is None:
        return False
    try:
        return float(value) <= float(limit)
    except (TypeError, ValueError):
        return False


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    finite: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            finite.append(number)
    ordered = sorted(finite)
    if not ordered:
        return {"min": None, "p50": None, "p95": None, "max": None, "average": None}
    return {
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
        "average": round(sum(ordered) / len(ordered), 3),
    }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    index = max(0, math.ceil(float(fraction) * len(ordered)) - 1)
    return float(ordered[index])


def _round_optional(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 3) if math.isfinite(number) else None


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _print_summary(output: Path, report: dict[str, Any]) -> None:
    quality = dict(report["quality"])
    print(json.dumps({
        "output": str(output.resolve()),
        "elapsed_seconds": report["elapsed_seconds"],
        "live_pipeline_ready": quality["live_pipeline_ready"],
        "checks": quality["checks"],
        "counts": quality["counts"],
        "input_schedule_drift_ms": quality["input_schedule_drift_ms"],
        "selected_chat_age_s": quality["selected_chat_age_s"],
        "chat_to_audio_start_s": quality["chat_to_audio_start_s"],
        "audio_queue_fill_ratio": quality["audio_queue_fill_ratio"],
        "post_source_drain_s": quality["post_source_drain_s"],
    }, ensure_ascii=False, indent=2))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?")
    parser.add_argument("--config-dir", type=str)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replay-speed", type=float)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_stress(_parse_args(argv)))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
