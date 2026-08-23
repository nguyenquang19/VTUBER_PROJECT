"""Replay a yt-dlp YouTube live-chat JSONL through Mai's Director offline."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_GENERATION_RETRY_SUFFIX = re.compile(r"_(?:r\d+|s\d+|shape)$")


def generation_turn_id(request_id: str) -> str:
    """Return the stable public-turn lineage for one generation attempt."""
    value = str(request_id).strip()
    if not value:
        raise ValueError("generation request_id must be non-empty")
    while True:
        parent = _GENERATION_RETRY_SUFFIX.sub("", value)
        if parent == value:
            return value
        value = parent

from interfaces.animation import MoodState  # noqa: E402
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.autonomy_engine import AutonomyConfig, AutonomyEngine  # noqa: E402
from services.autonomy.material_provider import MaterialProvider, RuntimeContext  # noqa: E402
from services.autonomy.self_talk_planner import SelfTalkPlanner  # noqa: E402
from services.agent.agent_state import AgentState  # noqa: E402
from services.agent.agenda_policy import AgendaPolicy  # noqa: E402
from services.agent.conversation_move_planner import ConversationMovePlanner  # noqa: E402
from services.agent.event_ledger import EventLedger  # noqa: E402
from services.agent.goal_manager import GoalManager  # noqa: E402
from services.agent.open_thread_manager import OpenThreadManager  # noqa: E402
from services.agent.thread_detector import RuleThreadDetector  # noqa: E402
from services.agent.topic_matcher import LexicalTopicMatcher  # noqa: E402
from services.director.action_transaction import ActionTransactionManager  # noqa: E402
from services.director.action_context import ActionContextBuilder  # noqa: E402
from services.director.action_types import DirectorInput  # noqa: E402
from services.director.chat_pulse import ChatPulse  # noqa: E402
from services.director.decision_record import DecisionRecordManager  # noqa: E402
from services.director.director import (  # noqa: E402
    Director,
    DirectorAction,
    ReadMode,
    Segment,
)
from services.director.director_loop import DirectorLoop  # noqa: E402
from services.director.proactive_policy import ProactiveHostingPolicy  # noqa: E402
from services.director.salience import SaliencePool  # noqa: E402
from services.director.v2_shadow import (  # noqa: E402
    DirectorV2Shadow,
    DirectorV2ShadowConfig,
    director_v2_snapshot_id,
)
from services.director.v2_takeover import DirectorV2Takeover  # noqa: E402
from services.capability.registry import CapabilityRegistry  # noqa: E402
from interfaces.director_v2 import DirectorV2Candidate, DirectorV2Context  # noqa: E402
from services.emotion.classifier import EventClassifier  # noqa: E402
from services.emotion.mood_style import MoodStyleTable  # noqa: E402
from services.input.chat_router import ChatRouter  # noqa: E402
from services.input.youtube_replay import YouTubeReplayInputService  # noqa: E402
from services.llm.parser import ParsedResponse  # noqa: E402


class _ReplayEmotion:
    def __init__(self, classifier: EventClassifier) -> None:
        self.classifier = classifier
        self.events_total = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def handle_event(self, event: Any) -> Any:
        self.events_total += 1
        return SimpleNamespace(category=self.classifier.classify(event))

    def get_metrics(self) -> dict[str, Any]:
        return {"replay_emotion_events_total": self.events_total}


class _ReplayRunner:
    session_id = "youtube-replay-offline"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.committed_self_talk: list[str] = []

    async def run_turn(self, request_id: str, user_text: str, **kwargs: Any) -> Any:
        response = f"[MÔ PHỎNG] Mai phản hồi: {user_text[:180]}"
        self.calls.append({
            "request_id": request_id,
            "attempt_id": request_id,
            "turn_id": generation_turn_id(request_id),
            "kind": "chat",
            "input": user_text,
            "response": response,
            "viewer_id": kwargs.get("viewer_id"),
        })
        return ParsedResponse(
            text=response, mood=MoodState(), ok=True, raw=response,
        ), 0

    async def run_ambient_turn(
        self, request_id: str, prompt_text: str, **_kwargs: Any,
    ) -> ParsedResponse:
        stage = _prompt_field(prompt_text, "Chặng") or "grounded"
        cause = _prompt_field(prompt_text, "Nguyên nhân ý nghĩ")
        anchor = _prompt_field(prompt_text, "Mỏ neo đã biết") or prompt_text
        tag = hashlib.sha1(anchor.encode("utf-8")).hexdigest()[:6]
        if stage == "invite":
            response = f"[MÔ PHỎNG] Ở mạch {tag}, mọi người nhìn chi tiết đó theo hướng nào?"
        elif stage == "develop":
            response = (
                f"[MÔ PHỎNG] Với mạch {tag}, có lẽ tớ nên giữ lại một chút "
                "do dự trước khi chốt ý."
            )
        elif cause == "silence":
            response = "[MÔ PHỎNG] Im một chút cũng là một nhịp nghỉ."
        else:
            response = (
                f"[MÔ PHỎNG] Tớ vừa để ý mạch chat {tag} có một chi tiết đáng nghĩ."
            )
        self.calls.append({
            "request_id": request_id,
            "attempt_id": request_id,
            "turn_id": generation_turn_id(request_id),
            "kind": "ambient",
            "input": prompt_text,
            "response": response,
        })
        return ParsedResponse(
            text=response, mood=MoodState(), ok=True, raw=response,
        )

    async def run_directed_turn(
        self, request_id: str, system_context: str, **_kwargs: Any,
    ) -> ParsedResponse:
        move = _prompt_field(system_context, "Conversation move") or "deepen"
        topic = _prompt_field(system_context, "Thread topic") or "chủ đề đang mở"
        response = f"[MÔ PHỎNG] Mai {move} về {topic}."
        self.calls.append({
            "request_id": request_id,
            "attempt_id": request_id,
            "turn_id": generation_turn_id(request_id),
            "kind": "directed",
            "input": system_context,
            "response": response,
            "conversation_move": move,
        })
        return ParsedResponse(text=response, mood=MoodState(), ok=True, raw=response)

    def commit_self_talk(self, text: str) -> None:
        self.committed_self_talk.append(text)


def _director_from_config(
    loader: ConfigLoader,
    pool: SaliencePool,
    pulse: ChatPulse,
    *,
    duration_seconds: float,
    clock: Any,
    proactive_policy: ProactiveHostingPolicy,
) -> Director:
    values = loader.get("director", "director", {}) or {}
    segment = Segment(
        "youtube_replay",
        "offline replay of one complete YouTube live chat",
        max(1.0, duration_seconds + 1.0),
        {
            "read_chat", "ack_donation", "self_talk", "follow_up",
            "continue_thread", "ask_follow_up",
        },
    )
    return Director(
        pool,
        pulse,
        [segment],
        dead_air_seconds=float(values.get("dead_air_seconds", 20.0)),
        self_talk_cooldown_seconds=float(
            values.get("self_talk_cooldown_seconds", 45.0)
        ),
        room_reaction_cooldown_seconds=float(
            (values.get("room_reaction") or {}).get("cooldown_seconds", 30.0)
        ),
        max_consecutive_read_chat=int(values.get("max_consecutive_read_chat", 3)),
        max_refs_per_turn=int(values.get("max_refs_per_turn", 3)),
        backlog_summary_threshold=int(values.get("backlog_summary_threshold", 12)),
        summary_score_ceiling=float(values.get("summary_score_ceiling", 15.0)),
        min_actionable_score=float(values.get("min_actionable_score", 15.0)),
        chat_gate_enabled=bool(loader.get(
            "features", "features.director_chat_gate.enabled", True,
        )),
        ask_follow_up_before_expiry_s=float(
            (values.get("arbiter") or {}).get("ask_follow_up_before_expiry_s", 20.0)
        ),
        proactive_policy=proactive_policy,
        clock=clock,
    )


def _transaction_manager(loader: ConfigLoader, clock: Any) -> ActionTransactionManager:
    return ActionTransactionManager(
        max_recent=int(loader.get(
            "director", "director.transactions.max_recent", 256,
        )),
        clock=clock,
    )


def _decision_manager(loader: ConfigLoader, clock: Any) -> DecisionRecordManager:
    base = "director.decision_records"
    return DecisionRecordManager(
        max_recent=int(loader.get("director", f"{base}.max_recent", 256)),
        max_evidence_refs=int(loader.get(
            "director", f"{base}.max_evidence_refs", 8,
        )),
        max_label_chars=int(loader.get(
            "director", f"{base}.max_label_chars", 120,
        )),
        hard_rejection_reasons=tuple(loader.get(
            "director", f"{base}.hard_rejection_reasons", [],
        ) or ()),
        clock=clock,
    )


async def simulate_replay(
    input_path: Path,
    *,
    loader: ConfigLoader,
    tick_window_ms: int,
    max_trace_items: int,
    runner_factory: Callable[[AgentState, GoalManager], Any] | None = None,
) -> dict[str, Any]:
    if tick_window_ms <= 0 or max_trace_items <= 0:
        raise ValueError("simulation bounds must be positive")
    replay_config = "evaluation.youtube_replay"
    clock_start = float(loader.get(
        "evaluation", f"{replay_config}.clock_start_epoch", 1767225600,
    ))
    base_time = datetime.fromtimestamp(clock_start, tz=timezone.utc)
    source = YouTubeReplayInputService(input_path, base_time=base_time)
    await source.start()
    if source.result is None:
        raise RuntimeError("YouTube replay parser did not produce a result")

    pool = SaliencePool.from_loader(loader)
    pulse = ChatPulse.from_loader(loader)
    emotion = _ReplayEmotion(EventClassifier.from_loader(loader))
    clock = {"now": clock_start}
    clock_fn = lambda: clock["now"]
    datetime_clock = lambda: datetime.fromtimestamp(clock["now"], tz=timezone.utc)
    replay_seed = loader.get(
        "evaluation", f"{replay_config}.random_seed", None,
    )
    if isinstance(replay_seed, bool) or not isinstance(replay_seed, int):
        raise ValueError("youtube replay random_seed must be an int")
    replay_rng = random.Random(replay_seed)
    topic_matcher = LexicalTopicMatcher.from_loader(loader)
    move_planner = ConversationMovePlanner.from_loader(loader)
    thread_detector = RuleThreadDetector.from_loader(loader, matcher=topic_matcher)
    thread_manager = OpenThreadManager.from_loader(
        loader, clock=datetime_clock, detector=thread_detector,
        matcher=topic_matcher, move_planner=move_planner,
    )
    ledger = EventLedger.from_loader(loader, clock=datetime_clock)
    agent_state = AgentState.from_loader(
        loader, ledger, clock=datetime_clock, thread_manager=thread_manager,
    )
    agenda = AgendaPolicy.from_loader(loader, clock=datetime_clock)
    goal_manager = GoalManager.from_loader(
        loader, clock=datetime_clock, on_active_changed=agent_state.set_active_goal_ref,
        audit_sink=agent_state.record, agenda_policy=agenda,
    )
    runner = (
        runner_factory(agent_state, goal_manager)
        if runner_factory is not None else _ReplayRunner()
    )
    agent_state.add_event_listener(goal_manager.handle_event)
    router = ChatRouter(
        [source], emotion, runner, pool=pool, pulse=pulse, agent_state=agent_state,
    )
    duration_seconds = source.result.duration_ms / 1000.0
    proactive_policy = ProactiveHostingPolicy.from_loader(loader, enabled=True)
    director = _director_from_config(
        loader, pool, pulse, duration_seconds=duration_seconds, clock=clock_fn,
        proactive_policy=proactive_policy,
    )
    transactions = _transaction_manager(loader, clock_fn)
    decisions = _decision_manager(loader, clock_fn)
    self_talk_planner = SelfTalkPlanner.from_loader(
        loader, mood_style=MoodStyleTable.from_loader(loader), enabled=True,
    )
    autonomy = AutonomyEngine(
        AutonomyConfig.from_loader(loader),
        MaterialProvider.from_loader(loader, rng=replay_rng),
        clock=clock_fn,
        rng=replay_rng,
        mood_style=MoodStyleTable.from_loader(loader),
    )

    deliveries: list[dict[str, str]] = []

    async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
        call = next(
            (
                item for item in reversed(runner.calls)
                if str(item.get("request_id") or "") == request_id
            ),
            None,
        )
        deliveries.append({
            "request_id": request_id,
            "attempt_id": str((call or {}).get("attempt_id") or request_id),
            "turn_id": str(
                (call or {}).get("turn_id") or generation_turn_id(request_id)
            ),
            "text": text,
        })
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    loop = DirectorLoop(
        director,
        pool,
        pulse,
        runner,
        autonomy=autonomy,
        speak=deliver,
        clock=clock_fn,
        transaction_manager=transactions,
        decision_records=decisions,
        self_talk_planner=self_talk_planner,
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
        speech_style_formula_phrases=tuple(loader.get(
            "director", "director.speech_style.formula_phrases", (),
        ) or ()),
        speech_style_language_integrity_fragments=tuple(loader.get(
            "director", "director.speech_style.language_integrity_fragments", (),
        ) or ()),
        speech_style_malformed_token_fragments=tuple(loader.get(
            "director", "director.speech_style.malformed_token_fragments", (),
        ) or ()),
        speech_style_malformed_token_allowlist=tuple(loader.get(
            "director", "director.speech_style.malformed_token_allowlist", (),
        ) or ()),
        speech_style_malformed_mixed_case_min_prefix_chars=int(loader.get(
            "director",
            "director.speech_style.malformed_mixed_case_min_prefix_chars", 0,
        )),
        speech_style_vague_input_max_words=int(loader.get(
            "director", "director.speech_style.vague_input_max_words", 1,
        )),
        speech_style_vague_grounding_forbidden_patterns=tuple(loader.get(
            "director",
            "director.speech_style.vague_grounding_forbidden_patterns", (),
        ) or ()),
        speech_style_semantic_over_inference_patterns=tuple(loader.get(
            "director", "director.speech_style.semantic_over_inference_patterns", (),
        ) or ()),
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

    capability_enabled = loader.get(
        "features", "features.capability_registry.enabled", False,
    )
    shadow_enabled = loader.get(
        "features", "features.director_v2_shadow.enabled", False,
    )
    takeover_enabled = loader.get(
        "features", "features.director_v2_takeover.enabled", False,
    )
    if not all(isinstance(value, bool) for value in (
        capability_enabled, shadow_enabled, takeover_enabled,
    )):
        raise ValueError("Director V2 replay feature flags must be bool")

    capability_registry = CapabilityRegistry.from_loader(
        loader,
        world_snapshot_provider=lambda: {},
        self_snapshot_provider=lambda: {},
        transaction_snapshot_provider=transactions.snapshot,
        health_snapshot_provider=lambda: {"targets": {
            "tts": {"health": "healthy"},
            "input_router": {"health": "healthy"},
            "local_wait": {"health": "healthy"},
            "mock_external": {"health": "healthy"},
            "avatar_adapter": {"health": "unhealthy"},
            "obs_websocket": {"health": "unhealthy"},
        }},
        enabled=capability_enabled,
        clock=datetime_clock,
    )
    for verifier_id in (
        "speech_delivery", "local_wait", "input_reader", "avatar_state",
        "mock_media", "obs_scene_state", "mock_call",
    ):
        capability_registry.register_verifier(verifier_id)
    director_v2_config = DirectorV2ShadowConfig.from_loader(loader)

    def director_v2_context() -> DirectorV2Context:
        now = clock_fn()
        candidates: list[DirectorV2Candidate] = []
        for message in pool.top_cluster(
            now, max_refs=director_v2_config.max_candidates_per_source,
        ):
            candidates.append(DirectorV2Candidate(
                source="chat",
                candidate_id=message.msg_id,
                action_type="READ_CHAT",
                capability_id="READ_CHAT",
                score=pool.current_score(message, now),
                evidence_refs=(f"chat:{message.msg_id}",),
                is_donation=message.is_super,
            ))
        goal_snapshot = goal_manager.snapshot()
        if goal_snapshot.active is not None:
            goal = goal_snapshot.active
            candidates.append(DirectorV2Candidate(
                source="goal",
                candidate_id=goal.goal_id,
                action_type="FOLLOW_UP",
                capability_id="FOLLOW_UP",
                score=goal.priority,
                evidence_refs=(f"goal:{goal.goal_id}",),
            ))
        agent_snapshot = agent_state.snapshot()
        thread_choice = proactive_policy.choose_open_thread(
            DirectorInput(
                now=now,
                agent_state=agent_snapshot,
                goals=goal_snapshot,
            ),
            allowed_actions=set(director.current_segment().allowed_actions),
        )
        if thread_choice is not None and thread_choice.action is DirectorAction.FOLLOW_UP:
            candidates.append(DirectorV2Candidate(
                source="thread",
                candidate_id=thread_choice.source_id,
                action_type="FOLLOW_UP",
                capability_id="FOLLOW_UP",
                score=0.0,
                evidence_refs=(f"thread:{thread_choice.source_id}",),
            ))
        urge_ready = autonomy.urge.should_speak_now()
        self_talk_ready, _ = loop.self_talk_candidate_readiness(
            now, urge_ready=urge_ready,
        )
        if self_talk_ready:
            candidates.append(DirectorV2Candidate(
                source="proactive",
                candidate_id="urge",
                action_type="SELF_TALK",
                capability_id="SELF_TALK",
                evidence_refs=("proactive:urge",),
            ))
        transaction_snapshot = transactions.snapshot()
        transaction_conflict = any(
            item.get("state") in {"reserved", "generated", "delivering", "delivered"}
            for item in transaction_snapshot.get("recent", ())
            if isinstance(item, dict)
        )
        capability_snapshot = capability_registry.snapshot()
        capability_identity = [{
            "capability_id": item["capability"]["capability_id"],
            "action_type": item["capability"]["action_type"],
            "available": item["availability"]["available"],
            "reason_code": item["availability"]["reason_code"],
            "mock_only": item["mock_only"],
        } for item in capability_snapshot["capabilities"]]
        self_identity = {
            "active_goal_id": (
                goal_snapshot.active.goal_id if goal_snapshot.active is not None else None
            ),
            "open_thread_ids": tuple(
                thread.thread_id for thread in agent_snapshot.open_threads
            ),
            "transaction_conflict": transaction_conflict,
        }
        return DirectorV2Context(
            created_at=now,
            world_snapshot_id=director_v2_snapshot_id("world", {}),
            self_snapshot_id=director_v2_snapshot_id("self", self_identity),
            capability_snapshot_id=director_v2_snapshot_id(
                "capabilities", capability_identity,
            ),
            candidates=tuple(candidates),
            permission_hold=not capability_registry.enabled,
            transaction_conflict=transaction_conflict,
        )

    director_v2_shadow = DirectorV2Shadow(
        director_v2_config,
        capability_registry=capability_registry,
        context_provider=director_v2_context,
        enabled=shadow_enabled,
        clock=clock_fn,
    )
    director_v2_takeover = DirectorV2Takeover.from_loader(
        loader, enabled=takeover_enabled, clock=clock_fn,
    )
    loop.configure_director_v2_takeover(
        director_v2_shadow, director_v2_takeover,
    )
    recent_context: list[str] = []
    activity = {"last": clock_start, "count": 0}

    def note_activity(event: Any) -> None:
        activity["last"] = clock["now"]
        activity["count"] += 1
        autonomy.on_external_activity()
        text = " ".join(str(getattr(event, "content", "")).split())[:240]
        if text:
            recent_context.append(text)
            del recent_context[:-3]
        loop.on_chat_activity(clock["now"])

    router.add_activity_listener(note_activity)
    loop.set_runtime_context_provider(lambda: RuntimeContext(
        silence_seconds=max(0.0, clock["now"] - activity["last"]),
        chat_count_last_10min=activity["count"],
        working_memory_recent=list(recent_context),
    ))
    director.start(clock_start)
    await transactions.start()
    await decisions.start()
    await capability_registry.start()
    await director_v2_takeover.start()
    await self_talk_planner.start()
    await agent_state.start()
    await goal_manager.start()

    by_tick: dict[int, list[Any]] = {}
    for event in source.result.events:
        offset_ms = int(event.metadata.get("replay_offset_ms", 0))
        by_tick.setdefault(offset_ms // tick_window_ms, []).append(event)
    last_tick = source.result.duration_ms // tick_window_ms
    action_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    trace: list[dict[str, Any]] = []
    max_pool_size = 0
    nonempty_ticks = 0
    selected_event_ids: set[str] = set()
    self_talk_offsets_ms: list[int] = []
    room_reaction_offsets_ms: list[int] = []
    delivery_offsets_ms: list[int] = []
    false_thread_commits = 0
    next_autonomy_tick_ms = int(round(autonomy.cfg.tick_seconds * 1000.0))

    try:
        for tick_index in range(last_tick + 1):
            clock["now"] = clock_start + ((tick_index + 1) * tick_window_ms / 1000.0)
            elapsed_ms = (tick_index + 1) * tick_window_ms
            while next_autonomy_tick_ms <= elapsed_ms:
                autonomy.tick(MoodState())
                next_autonomy_tick_ms += int(round(autonomy.cfg.tick_seconds * 1000.0))
            incoming = by_tick.get(tick_index, [])
            if incoming:
                nonempty_ticks += 1
                await asyncio.gather(*(router._process(event) for event in incoming))
            max_pool_size = max(max_pool_size, pool.size())
            expected = loop.preview_decision(clock["now"])
            calls_before = len(runner.calls)
            deliveries_before = len(deliveries)
            moves_before = {
                thread.thread_id: thread.move_count
                for thread in agent_state.snapshot().open_threads
            }
            action = await loop.tick_once()
            decision_snapshot = decisions.snapshot().get("current") or {}
            if len(deliveries) == deliveries_before:
                false_thread_commits += sum(
                    thread.move_count > moves_before.get(thread.thread_id, 0)
                    for thread in agent_state.snapshot().open_threads
                )
            action_counts[action.value] += 1
            if (
                action is DirectorAction.SELF_TALK
                and len(deliveries) > deliveries_before
            ):
                self_talk_offsets_ms.append((tick_index + 1) * tick_window_ms)
            if len(deliveries) > deliveries_before:
                delivery_offsets_ms.extend(
                    [(tick_index + 1) * tick_window_ms]
                    * (len(deliveries) - deliveries_before)
                )
                if expected.read_mode in (ReadMode.SUMMARY, ReadMode.VIBE):
                    room_reaction_offsets_ms.append(
                        (tick_index + 1) * tick_window_ms
                    )
            actual_reason = str(decision_snapshot.get("reason") or expected.reason)
            reason_counts[actual_reason] += 1
            selected = [
                {
                    "event_id": ref.msg_id,
                    "viewer": ref.viewer_name,
                    "text": ref.text,
                    "kind": ref.kind,
                    "score": round(ref.score, 3),
                    "is_super": ref.is_super,
                    "cluster_count": ref.cluster_count,
                }
                for ref in expected.refs
            ]
            selected_event_ids.update(item["event_id"] for item in selected)
            should_trace = bool(incoming) or action is not DirectorAction.WAIT
            if should_trace and len(trace) < max_trace_items:
                trace.append({
                    "tick": tick_index,
                    "offset_ms": (tick_index + 1) * tick_window_ms,
                    "incoming_count": len(incoming),
                    "incoming": [
                        {
                            "event_id": event.event_id,
                            "viewer": event.user_name,
                            "text": event.content,
                        }
                        for event in incoming
                    ],
                    "action": action.value,
                    "reason": actual_reason,
                    "read_mode": expected.read_mode.value if expected.read_mode else None,
                    "selected": selected,
                    "pool_after": pool.size(),
                    "runner_calls": runner.calls[calls_before:],
                    "deliveries": deliveries[deliveries_before:],
                    "director_v2": {
                        "proposal": director_v2_shadow.snapshot().get("current"),
                        "selection": director_v2_takeover.snapshot().get("current"),
                        "decision": decision_snapshot,
                    },
                    "threads": [
                        {
                            "thread_id": thread.thread_id,
                            "status": thread.status.value,
                            "next_move": thread.next_move.value if thread.next_move else None,
                            "move_count": thread.move_count,
                        }
                        for thread in agent_state.snapshot().open_threads
                    ],
                })
    finally:
        await director_v2_takeover.stop()
        await capability_registry.stop()
        await goal_manager.stop()
        await agent_state.stop()
        await self_talk_planner.stop()
        await decisions.stop()
        await transactions.stop()
        await source.stop()

    result = source.result
    self_talk_gaps_s = [
        (current - previous) / 1000.0
        for previous, current in zip(self_talk_offsets_ms, self_talk_offsets_ms[1:])
    ]
    room_reaction_gaps_s = [
        (current - previous) / 1000.0
        for previous, current in zip(
            room_reaction_offsets_ms, room_reaction_offsets_ms[1:],
        )
    ]
    delivery_gaps_s = [
        (current - previous) / 1000.0
        for previous, current in zip(delivery_offsets_ms, delivery_offsets_ms[1:])
    ]
    duration_minutes = max(result.duration_ms / 60000.0, 1 / 60.0)
    return {
        "schema_version": 2,
        "mode": "offline_youtube_replay",
        "input_file": str(input_path.resolve()),
        "timing": {
            "tick_window_ms": tick_window_ms,
            "random_seed": replay_seed,
            "duration_ms": result.duration_ms,
            "ticks_total": last_tick + 1,
            "ticks_with_chat": nonempty_ticks,
            "chat_in_each_tick_is_concurrent": True,
        },
        "input": {
            "json_lines": result.lines_total,
            "events": len(result.events),
            "lines_skipped": result.lines_skipped,
            "renderer_counts": result.renderer_counts,
            "unique_viewers": len({event.user_id for event in result.events if event.user_id}),
        },
        "director": {
            "action_counts": dict(sorted(action_counts.items())),
            "reason_counts": dict(reason_counts.most_common()),
            "selected_unique_events": len(selected_event_ids),
            "max_pool_size": max_pool_size,
            "final_pool_size": pool.size(),
            "salience_metrics": pool.get_metrics(),
            "self_talk_cadence": {
                "count": len(self_talk_offsets_ms),
                "minimum_gap_s": min(self_talk_gaps_s) if self_talk_gaps_s else None,
                "average_gap_s": (
                    round(sum(self_talk_gaps_s) / len(self_talk_gaps_s), 3)
                    if self_talk_gaps_s else None
                ),
                "maximum_gap_s": max(self_talk_gaps_s) if self_talk_gaps_s else None,
                "gaps_below_configured_cooldown": sum(
                    gap < float(loader.get(
                        "director", "director.self_talk_cooldown_seconds", 45.0,
                    ))
                    for gap in self_talk_gaps_s
                ),
            },
            "room_reaction_cadence": {
                "count": len(room_reaction_offsets_ms),
                "reactions_per_minute": round(
                    len(room_reaction_offsets_ms) / duration_minutes, 3,
                ),
                "minimum_gap_s": (
                    min(room_reaction_gaps_s) if room_reaction_gaps_s else None
                ),
                "average_gap_s": (
                    round(sum(room_reaction_gaps_s) / len(room_reaction_gaps_s), 3)
                    if room_reaction_gaps_s else None
                ),
                "maximum_gap_s": (
                    max(room_reaction_gaps_s) if room_reaction_gaps_s else None
                ),
                "gaps_below_configured_cooldown": sum(
                    gap < float(loader.get(
                        "director", "director.room_reaction.cooldown_seconds", 30.0,
                    ))
                    for gap in room_reaction_gaps_s
                ),
            },
            "delivery_cadence": {
                "count": len(delivery_offsets_ms),
                "deliveries_per_minute": round(
                    len(delivery_offsets_ms) / duration_minutes, 3,
                ),
                "minimum_gap_s": min(delivery_gaps_s) if delivery_gaps_s else None,
                "average_gap_s": (
                    round(sum(delivery_gaps_s) / len(delivery_gaps_s), 3)
                    if delivery_gaps_s else None
                ),
                "maximum_gap_s": max(delivery_gaps_s) if delivery_gaps_s else None,
            },
            "metrics": loop.get_metrics(),
            "director_v2": {
                "shadow": director_v2_shadow.snapshot(),
                "takeover": director_v2_takeover.snapshot(),
                "capability_registry": capability_registry.get_metrics(),
            },
        },
        "delivery": {
            "generation_attempts": len(runner.calls),
            "public_turns": len({
                str(call.get("turn_id") or generation_turn_id(
                    str(call.get("request_id") or "")
                ))
                for call in runner.calls
            }),
            "delivered_turns": len(deliveries),
            "mode": str(getattr(runner, "delivery_mode", "subtitle_stub")),
            "transactions": transactions.snapshot()["counts"],
            "items": list(deliveries),
        },
        "thought_engine": {
            "snapshot": self_talk_planner.snapshot(),
            "metrics": self_talk_planner.get_metrics(),
        },
        "conversation_threads": {
            "open": [thread.to_dict() for thread in agent_state.snapshot().open_threads],
            "terminal": [
                {"thread": thread.to_dict(), "reason": reason}
                for thread, reason in thread_manager.recent_terminal()
            ],
            "metrics": thread_manager.get_metrics(),
            "goal_metrics": goal_manager.get_metrics(),
            "false_commits": false_thread_commits,
        },
        "trace_truncated": len(trace) >= max_trace_items,
        "trace": trace,
    }


def _prompt_field(prompt: str, name: str) -> str:
    prefix = f"{name}:"
    for line in prompt.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix):].strip().rstrip(".")
    return ""


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a yt-dlp *.live_chat.json through Mai without network/LLM/TTS",
    )
    parser.add_argument("input", type=Path, help="path to yt-dlp live_chat JSONL")
    parser.add_argument("--burst-window-ms", type=int)
    parser.add_argument("--max-trace-items", type=int)
    parser.add_argument("--console-decisions", type=int)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    base = "evaluation.youtube_replay"
    window_ms = args.burst_window_ms or int(loader.get(
        "evaluation", f"{base}.burst_window_ms", 1500,
    ))
    max_trace = args.max_trace_items or int(loader.get(
        "evaluation", f"{base}.max_trace_items", 2000,
    ))
    console_count = (
        args.console_decisions
        if args.console_decisions is not None
        else int(loader.get("evaluation", f"{base}.console_decisions", 20))
    )
    output = args.output or Path(str(loader.get(
        "evaluation", f"{base}.output_file",
        "logs/evaluation/youtube_replay_simulation.json",
    )))
    if not output.is_absolute():
        output = REPO_ROOT / output
    report = await simulate_replay(
        args.input,
        loader=loader,
        tick_window_ms=window_ms,
        max_trace_items=max_trace,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        output.write_text,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "input": report["input"],
        "timing": report["timing"],
        "director": report["director"],
        "delivery": report["delivery"],
        "output": str(output.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    decisions = [item for item in report["trace"] if item["action"] != "wait"]
    if console_count > 0:
        print("\nCác quyết định đầu tiên:")
        for item in decisions[:console_count]:
            selected = ", ".join(ref["text"] for ref in item["selected"]) or "-"
            print(
                f"  t={item['offset_ms'] / 1000:7.1f}s "
                f"action={item['action']:<12} reason={item['reason']:<28} selected={selected}"
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
