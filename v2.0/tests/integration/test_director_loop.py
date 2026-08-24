"""Integration C0.4 — DirectorLoop turn driver với FakeLLM (docs/MAI_V2_SYSTEM_SPEC.md).

Verify Director cầm nhịp (không FIFO): chat vào pool → Director nhặt → sinh turn.
DoD: superchat acked first; read gỡ khỏi pool; dead-air→self_talk; no infinite read.
Dùng Fake runner (không cần llama/GPU).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from interfaces.animation import MoodState
from interfaces.director_v2 import DirectorV2Proposal, DirectorV2TakeoverSelection
from interfaces.filter import FilterCategory, FilterVerdict
from services.director.chat_pulse import ChatPulse
from services.director.action_types import DirectorInput
from services.director.director import (
    Director, DirectorAction, DirectorDecision, ReadMode, Segment,
)
from services.director.director_loop import DirectorLoop, _self_talk_correction_prompt
from services.director.salience import SaliencePool
from services.autonomy.self_talk_planner import SelfTalkPlanner
from services.autonomy.material_provider import RuntimeContext
from services.autonomy.lore_material import LoreMaterial, LoreMaterialProvider
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.goal_types import (
    Goal, GoalKind, GoalSnapshot, GoalSource, GoalStatus,
    ShortIntention, ShortIntentionStatus,
)
from services.agent.types import (
    AgentStateSnapshot, ConversationMove, OpenThread, ThreadEvidence,
)
from services.llm.parser import ParsedResponse

REPO_ROOT = Path(__file__).resolve().parents[2]


def _goal_snapshot(goal: Goal) -> GoalSnapshot:
    intention = ShortIntention(
        intention_id=f"intention:{goal.goal_id}:1",
        goal_id=goal.goal_id,
        status=ShortIntentionStatus.ACTIVE,
        step_index=0,
        step_count=len(goal.steps),
        step=goal.steps[0],
        created_at=goal.created_at,
        updated_at=goal.created_at,
        expires_at=goal.expires_at,
        reason_code="activated",
    )
    return GoalSnapshot(active=goal, current_intention=intention)


def test_self_talk_correction_explains_semantic_question_and_stage_repeat() -> None:
    prompt = _self_talk_correction_prompt(
        "prompt gốc",
        "Câu trước bị lặp lại, mọi người thấy sao nhỉ.",
        max_sentences=1,
        allow_question=False,
        require_question=False,
        reasons=(
            "stage_repeat", "question_not_allowed", "invitation_question_count",
        ),
    )
    assert "Không chép lại hoặc diễn đạt lại" in prompt
    assert "được nhận diện theo nghĩa" in prompt
    assert "Kết thúc bằng một nhận xét khẳng định" in prompt
    assert "Không được dùng câu hỏi" in prompt
    assert "Xóa các câu hỏi thừa" in prompt


@dataclass
class FakeParsed:
    text: str
    ok: bool = True
    raw: str = ""


class FakeRunner:
    """Ghi lại call. run_turn/run_ambient_turn trả text = user_text nhận vào."""
    def __init__(self) -> None:
        self.read_calls: list[str] = []
        self.ambient_calls: list[str] = []
        self.directed_calls: list[str] = []
        self.committed: list[str] = []
        self.hist_calls: list[tuple] = []
        self.stage_calls: list = []

    async def run_turn(self, request_id, user_text, viewer_id=None,
                       trigger_type=None, event_category=None,
                       history_user_text=None, commit_history=True,
                       stage_direction=None):
        self.read_calls.append(user_text)
        self.hist_calls.append((history_user_text, commit_history))
        self.stage_calls.append(stage_direction)
        return FakeParsed(text=f"reply:{user_text}"), 0

    async def run_ambient_turn(self, request_id, prompt_text):
        self.ambient_calls.append(prompt_text)
        return FakeParsed(text=f"self:{prompt_text[:20]}")

    async def run_directed_turn(self, request_id, system_context):
        self.directed_calls.append(system_context)
        return FakeParsed(text="directed reply")

    def commit_self_talk(self, text):
        self.committed.append(text)


class FakeUrge:
    def __init__(self, ready=False):
        self._ready = ready
    def should_speak_now(self):
        return self._ready
    def on_self_spoke(self):
        pass


class FakeAutonomy:
    def __init__(self, ready=False, has_material=True):
        self.urge = FakeUrge(ready)
        self._has_material = has_material
        self.spoke: list[str] = []

    def force_generate(self, mood, ctx):
        if not self._has_material:
            return None
        @dataclass
        class _D:
            prompt_text: str = "seed prompt tự nói"
        return _D()

    def check_dedup(self, text):
        return False

    def on_self_spoke(self, text):
        self.spoke.append(text)


def _segments():
    return [
        Segment("opening", "chào", 10.0, {"self_talk", "read_chat", "transition"}),
        Segment("main", "chính", 300.0,
                {"read_chat", "self_talk", "ack_donation", "continue_thread",
                 "ask_follow_up", "share_goal_progress", "transition"}),
        Segment("closing", "kết", 10.0, {"self_talk", "transition"}),
    ]


def _make(now=0.0, autonomy=None, agent_state=None, goal_manager=None, **dir_over):
    room_window = int(dir_over.pop("room_reaction_recent_window", 16))
    room_threshold = float(dir_over.pop("room_reaction_similarity_threshold", 0.72))
    room_regenerations = int(dir_over.pop("room_reaction_max_regenerations", 1))
    room_retry_defer = float(dir_over.pop("room_reaction_retry_defer_seconds", 30.0))
    speech_window = int(dir_over.pop("speech_dedup_recent_window", 32))
    speech_threshold = float(dir_over.pop("speech_dedup_similarity_threshold", 0.72))
    speech_regenerations = int(dir_over.pop("speech_dedup_max_regenerations", 1))
    style_window = int(dir_over.pop("speech_style_recent_window", 12))
    style_openers = tuple(dir_over.pop(
        "speech_style_formula_openers", ("mà", "trời ơi", "ủa", "ơ kìa"),
    ))
    style_formula_max = int(dir_over.pop("speech_style_max_formula_openers", 2))
    style_phrases = tuple(dir_over.pop("speech_style_formula_phrases", ()))
    style_language = tuple(dir_over.pop(
        "speech_style_language_integrity_fragments", (),
    ))
    style_malformed = tuple(dir_over.pop(
        "speech_style_malformed_token_fragments", (),
    ))
    style_malformed_allowlist = tuple(dir_over.pop(
        "speech_style_malformed_token_allowlist", (),
    ))
    style_mixed_case_prefix = int(dir_over.pop(
        "speech_style_malformed_mixed_case_min_prefix_chars", 0,
    ))
    style_vague_words = int(dir_over.pop(
        "speech_style_vague_input_max_words", 1,
    ))
    style_vague_patterns = tuple(dir_over.pop(
        "speech_style_vague_grounding_forbidden_patterns", (),
    ))
    style_semantic_patterns = tuple(dir_over.pop(
        "speech_style_semantic_over_inference_patterns", (),
    ))
    style_same_max = int(dir_over.pop("speech_style_max_same_opener", 1))
    style_question_max = int(dir_over.pop("speech_style_max_questions", 2))
    style_endings = tuple(dir_over.pop(
        "speech_style_question_endings", ("nhỉ", "hả", "sao", "nào"),
    ))
    style_max_sentences = int(dir_over.pop("speech_style_max_sentences", 2))
    style_max_words = int(dir_over.pop("speech_style_max_words", 32))
    style_regenerations = int(dir_over.pop("speech_style_max_regenerations", 1))
    pool = SaliencePool(base_tier={"chat": 10, "question": 25, "mention": 35},
                        tau_seconds=50.0, floor=3.0, cluster_coef=5.0)
    pulse = ChatPulse(window_seconds=60.0, tempo_low_per_min=2.0,
                      tempo_high_per_min=15.0, diversity_threshold=0.4,
                      cold_silence_seconds=90.0)
    clock = {"t": now}
    kw = dict(dead_air_seconds=20.0, max_consecutive_read_chat=3, max_refs_per_turn=3,
              backlog_summary_threshold=12, summary_score_ceiling=15.0,
              chat_gate_enabled=True)
    kw.update(dir_over)
    director = Director(pool, pulse, _segments(), clock=lambda: clock["t"], **kw)
    runner = FakeRunner()

    async def delivered(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    loop = DirectorLoop(
        director=director, pool=pool, pulse=pulse, runner=runner,
        emotion=None, autonomy=autonomy, speak=delivered,
        clock=lambda: clock["t"],
        agent_state=agent_state,
        goal_manager=goal_manager,
        room_reaction_recent_window=room_window,
        room_reaction_similarity_threshold=room_threshold,
        room_reaction_max_regenerations=room_regenerations,
        room_reaction_retry_defer_seconds=room_retry_defer,
        speech_dedup_recent_window=speech_window,
        speech_dedup_similarity_threshold=speech_threshold,
        speech_dedup_max_regenerations=speech_regenerations,
        speech_style_recent_window=style_window,
        speech_style_formula_openers=style_openers,
        speech_style_max_formula_openers=style_formula_max,
        speech_style_formula_phrases=style_phrases,
        speech_style_language_integrity_fragments=style_language,
        speech_style_malformed_token_fragments=style_malformed,
        speech_style_malformed_token_allowlist=style_malformed_allowlist,
        speech_style_malformed_mixed_case_min_prefix_chars=style_mixed_case_prefix,
        speech_style_vague_input_max_words=style_vague_words,
        speech_style_vague_grounding_forbidden_patterns=style_vague_patterns,
        speech_style_semantic_over_inference_patterns=style_semantic_patterns,
        speech_style_max_same_opener=style_same_max,
        speech_style_max_questions=style_question_max,
        speech_style_question_endings=style_endings,
        speech_style_max_sentences=style_max_sentences,
        speech_style_max_words=style_max_words,
        speech_style_max_regenerations=style_regenerations,
    )
    director.start(now)
    # move past opening so read_chat/ack allowed (main segment)
    director.advance_segment(now)
    director.mark_spoke(DirectorAction.TRANSITION, now)
    return loop, director, pool, pulse, runner, clock


@pytest.mark.asyncio
async def test_cognitive_observer_failure_cannot_change_compatibility_wait() -> None:
    loop, _director, _pool, _pulse, runner, clock = _make()

    class BrokenObserver:
        def observe_decision(self, *args: object) -> None:
            raise RuntimeError("observer failed")

        def preempt_for_live(self) -> None:
            raise RuntimeError("preemption metric failed")

    loop.configure_cognitive_observer(BrokenObserver())
    for _index in range(100):
        clock["t"] = 1.0
        assert await loop.tick_once() is DirectorAction.WAIT
    loop.on_chat_activity()
    assert runner.read_calls == []
    assert runner.ambient_calls == []


@pytest.mark.asyncio
async def test_cognitive_tap_runs_before_public_generation_and_result_is_unread() -> None:
    loop, _director, pool, _pulse, runner, clock = _make()
    observed: list[str] = []

    class Observer:
        def observe_decision(self, decision, value, decision_id) -> bool:
            del value, decision_id
            assert runner.read_calls == []
            observed.append(decision.action.value)
            return True

        def observe_verified_outcome(self, decision, value, decision_id) -> bool:
            del value, decision_id
            observed.append(f"verified:{decision.action.value}")
            return True

        def preempt_for_live(self) -> None:
            return None

    loop.configure_cognitive_observer(Observer())
    pool.add("m-observe", "Mai ơi", now=0.0, kind="mention")
    clock["t"] = 1.0
    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert observed == ["read_chat", "verified:read_chat"]
    assert runner.read_calls == ["Mai ơi"]


@pytest.mark.asyncio
class TestDirectorLoop:
    async def test_primary_takeover_materializes_divergent_action_without_compatibility_decide(
        self,
    ) -> None:
        proposal = DirectorV2Proposal(
            "p-primary-read", 1.0, "READ_CHAT", "READ_CHAT", "m1",
            ("selected", "validated"), ("chat:m1",),
        )

        class StaticShadow:
            @staticmethod
            def propose_current() -> DirectorV2Proposal:
                return proposal

        class PrimarySelector:
            enabled = True
            ownership_mode = "primary"

            @staticmethod
            def evaluate(**kwargs: object) -> DirectorV2TakeoverSelection:
                assert kwargs["legacy_action"] is None
                return DirectorV2TakeoverSelection(
                    True, "SPEECH_SCHEDULING", "accepted", "READ_CHAT",
                    proposal.proposal_id, "director_v2",
                )

        loop, director, pool, _pulse, runner, clock = _make()
        loop.configure_director_v2_takeover(StaticShadow(), PrimarySelector())
        pool.add("m1", "Mai primary nhé", now=0.0, kind="mention")
        clock["t"] = 1.0

        def forbidden_compatibility(_value: DirectorInput) -> DirectorDecision:
            raise AssertionError("compatibility decide must not run on primary success")

        director.decide = forbidden_compatibility  # type: ignore[method-assign]
        assert await loop.tick_once() is DirectorAction.READ_CHAT
        assert runner.read_calls == ["Mai primary nhé"]
        metrics = loop.get_metrics()
        assert metrics["director_v2_primary_selected_total"] == 1
        assert metrics["director_v2_primary_fallback_total"] == 0

    async def test_primary_failure_invokes_compatibility_fallback_once(self) -> None:
        class BrokenShadow:
            @staticmethod
            def propose_current() -> None:
                raise RuntimeError("proposal unavailable")

        class PrimarySelector:
            enabled = True
            ownership_mode = "primary"

            @staticmethod
            def evaluate(**_kwargs: object) -> DirectorV2TakeoverSelection:
                return DirectorV2TakeoverSelection(
                    False, "SPEECH_SCHEDULING", "proposal_missing", "WAIT",
                )

        loop, director, pool, _pulse, runner, clock = _make()
        loop.configure_director_v2_takeover(BrokenShadow(), PrimarySelector())
        pool.add("m1", "fallback once", now=0.0, kind="mention")
        clock["t"] = 1.0
        original = director.decide
        calls = 0

        def counted(value: DirectorInput) -> DirectorDecision:
            nonlocal calls
            calls += 1
            return original(value)

        director.decide = counted  # type: ignore[method-assign]
        assert await loop.tick_once() is DirectorAction.READ_CHAT
        assert runner.read_calls == ["fallback once"]
        assert calls == 1
        assert loop.get_metrics()["director_v2_primary_fallback_total"] == 1

    async def test_primary_segment_transition_is_hard_preemption_without_soft_policy(
        self,
    ) -> None:
        class UnusedShadow:
            @staticmethod
            def propose_current() -> None:
                raise AssertionError("shadow must not override a due transition")

        class PrimarySelector:
            enabled = True
            ownership_mode = "primary"

        loop, director, _pool, _pulse, runner, clock = _make()
        loop.configure_director_v2_takeover(UnusedShadow(), PrimarySelector())
        clock["t"] = 301.0

        def forbidden_compatibility(_value: DirectorInput) -> DirectorDecision:
            raise AssertionError("compatibility soft policy must not run")

        director.decide = forbidden_compatibility  # type: ignore[method-assign]
        assert await loop.tick_once() is DirectorAction.TRANSITION
        assert len(runner.ambient_calls) == 1
        assert loop.get_metrics()["director_v2_hard_preemption_total"] == 1

    async def test_primary_safety_hold_waits_without_shadow_or_compatibility_policy(
        self,
    ) -> None:
        class UnusedShadow:
            @staticmethod
            def propose_current() -> None:
                raise AssertionError("shadow must not override safety hold")

        class PrimarySelector:
            enabled = True
            ownership_mode = "primary"

        loop, director, pool, _pulse, runner, clock = _make()
        loop.configure_director_v2_takeover(UnusedShadow(), PrimarySelector())
        loop._safety_hold_fn = lambda: True
        pool.add("m1", "unsafe turn", now=0.0, kind="mention")
        clock["t"] = 1.0

        def forbidden_compatibility(_value: DirectorInput) -> DirectorDecision:
            raise AssertionError("compatibility soft policy must not run")

        director.decide = forbidden_compatibility  # type: ignore[method-assign]
        assert await loop.tick_once() is DirectorAction.WAIT
        assert runner.read_calls == []
        assert loop.get_metrics()["director_v2_hard_preemption_total"] == 1

    async def test_accepted_takeover_owns_a_compatibility_identical_decision(self) -> None:
        proposal = DirectorV2Proposal(
            "p-read", 1.0, "READ_CHAT", "READ_CHAT", "m1",
            ("selected", "validated"), ("chat:m1",),
        )

        class StaticShadow:
            @staticmethod
            def propose_current() -> DirectorV2Proposal:
                return proposal

        class AcceptingSelector:
            @staticmethod
            def evaluate(**_kwargs: object) -> DirectorV2TakeoverSelection:
                return DirectorV2TakeoverSelection(
                    True, "READ_CHAT", "accepted", "READ_CHAT", "p-read",
                    "director_v2",
                )

        loop, director, pool, _pulse, _runner, clock = _make()
        loop.configure_director_v2_takeover(StaticShadow(), AcceptingSelector())
        pool.add("m1", "Mai ơi chơi gì", now=0.0, kind="mention")
        clock["t"] = 1.0
        director_input = loop._build_director_input(1.0, False)
        legacy = director.decide(director_input)
        selected = loop._apply_director_v2_takeover(legacy, director_input)

        assert selected is not legacy
        assert selected.decision_owner == "director_v2"
        assert selected.director_v2_proposal_id == "p-read"
        assert selected.action is legacy.action
        assert selected.refs == legacy.refs
        assert selected.read_mode is legacy.read_mode
        assert selected.reason == legacy.reason

    async def test_rejected_or_malformed_takeover_returns_exact_legacy_object(self) -> None:
        proposal = DirectorV2Proposal(
            "p-read", 1.0, "READ_CHAT", "READ_CHAT", "m1",
            ("selected", "validated"), ("chat:m1",),
        )

        class StaticShadow:
            @staticmethod
            def propose_current() -> DirectorV2Proposal:
                return proposal

        class RejectedSelector:
            @staticmethod
            def evaluate(**_kwargs: object) -> DirectorV2TakeoverSelection:
                return DirectorV2TakeoverSelection(
                    False, "READ_CHAT", "action_mismatch", "READ_CHAT", "p-read",
                )

        loop, _director, _pool, _pulse, _runner, _clock = _make()
        value = DirectorDecision(DirectorAction.WAIT, "main", "idle")
        director_input = loop._build_director_input(1.0, False)
        loop.configure_director_v2_takeover(StaticShadow(), RejectedSelector())
        assert loop._apply_director_v2_takeover(value, director_input) is value

        class MalformedSelector:
            @staticmethod
            def evaluate(**_kwargs: object) -> object:
                return object()

        loop.configure_director_v2_takeover(StaticShadow(), MalformedSelector())
        assert loop._apply_director_v2_takeover(value, director_input) is value

    async def test_shadow_failure_cannot_change_legacy_decision(self) -> None:
        class BrokenShadow:
            def propose_current(self) -> None:
                raise RuntimeError("shadow unavailable")

        class BrokenSelector:
            def evaluate(self, **_kwargs: object) -> None:
                raise RuntimeError("selector unavailable")

        loop, _director, pool, _pulse, runner, clock = _make()
        loop.configure_director_v2_takeover(BrokenShadow(), BrokenSelector())
        pool.add("m1", "Mai ơi chơi gì", now=0.0, kind="mention")
        clock["t"] = 1.0

        assert await loop.tick_once() == DirectorAction.READ_CHAT
        assert runner.read_calls == ["Mai ơi chơi gì"]

    async def test_targeted_read_focuses_only_its_delivered_thread(self) -> None:
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        thread = OpenThread(
            "thread-selected", "topic", "summary", now, now,
            now + timedelta(minutes=5),
            evidence=(ThreadEvidence(
                "agent:chat:selected", "selected question", "test", 1.0,
            ),),
            origin_event_id="agent:chat:selected",
        )

        class _State:
            @staticmethod
            def snapshot() -> AgentStateSnapshot:
                return AgentStateSnapshot(open_threads=(thread,))

        class _Goals:
            def __init__(self) -> None:
                self.focused: list[tuple[str | None, set[str]]] = []

            def focus_delivered_thread(
                self, parent_thread_id: str | None, *, source_event_ids: set[str],
            ) -> int:
                self.focused.append((parent_thread_id, source_event_ids))
                return 1

        goals = _Goals()
        loop, _, pool, _, _, _ = _make(agent_state=_State(), goal_manager=goals)
        pool.add("selected", "selected question", now=0.0, kind="question")
        ref = pool.peek_top(1.0)
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
            proactive_source=None,
        )

        assert await loop._exec_read(decision, 1.0) is True
        assert goals.focused == [(
            "thread-selected", {"selected", "agent:chat:selected"},
        )]
        assert loop.get_metrics()["director_thread_focus_total"] == 1

    async def test_room_reaction_clears_soft_continuations_only_after_delivery(self) -> None:
        class _Goals:
            def __init__(self) -> None:
                self.reasons: list[str] = []

            def clear_continue_threads(self, *, reason: str) -> int:
                self.reasons.append(reason)
                return 2

        goals = _Goals()
        loop, _, _, _, _, _ = _make(goal_manager=goals)
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.VIBE,
            refs=(),
            proactive_source=None,
        )

        assert await loop._exec_room_reaction(decision, 1.0) is True
        assert goals.reasons == ["room_reaction_delivered"]
        assert loop.get_metrics()["director_thread_boundary_clear_total"] == 2

        async def failed_delivery(
            request_id: str, _text: str,
        ) -> TTSDeliveryResult:
            return TTSDeliveryResult(
                request_id=request_id,
                delivered=False,
                mode=TTSDeliveryMode.NONE,
            )

        loop._speak = failed_delivery
        assert await loop._exec_room_reaction(decision, 2.0) is False
        assert goals.reasons == ["room_reaction_delivered"]

    async def test_execute_exception_increments_observable_metric(self) -> None:
        loop, _, pool, pulse, _, clock = _make()
        pool.add("chat-error", "Mai ơi?", now=0.0, kind="mention")
        pulse.record(now=0.0, user_id="viewer")
        clock["t"] = 1.0

        async def fail_execute(*_args: object, **_kwargs: object) -> bool:
            raise TypeError("bad parsed copy")

        loop._execute = fail_execute  # type: ignore[method-assign]

        assert await loop.tick_once() is DirectorAction.READ_CHAT
        assert loop.get_metrics()["director_execute_failed_total"] == 1

    async def test_room_duplicate_releases_first_candidate_before_regeneration(self) -> None:
        loop, director, _, _, runner, _ = _make()
        outputs = iter(("Chat chạy nhanh ghê.", "Cả phòng đang tăng tốc thấy rõ luôn."))
        events: list[tuple[str, str, bool | None]] = []
        deliveries: list[str] = []

        async def generate(request_id: str, prompt: str, defer_delivery_commit=False):
            events.append(("generate", request_id, None))
            return FakeParsed(next(outputs))

        def finalize(request_id: str, success: bool) -> None:
            events.append(("finalize", request_id, success))

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_ambient_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        loop._room_reaction_dedup.record("Chat chạy nhanh ghê.")
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SUMMARY,
            refs=(),
            proactive_source=None,
        )

        assert await loop._exec_room_reaction(decision, 10.0) is True
        assert events[0][0] == "generate"
        assert events[1][0] == "finalize" and events[1][2] is False
        assert events[2][0] == "generate"
        assert events[-1][0] == "finalize" and events[-1][2] is True
        assert deliveries == ["Cả phòng đang tăng tốc thấy rõ luôn."]
        assert director.get_metrics()["director_last_room_reaction_ts"] == 10.0
        metrics = loop.get_metrics()
        assert metrics["director_room_reaction_duplicate_total"] == 1
        assert metrics["director_room_reaction_regenerated_total"] == 1

    async def test_second_room_duplicate_is_suppressed_without_delivery(self) -> None:
        loop, director, pool, _, runner, _ = _make()
        finalizations: list[bool] = []
        deliveries: list[str] = []
        pool.add("keep", "tin vẫn phải còn", now=0.0, kind="chat")

        async def generate(request_id: str, prompt: str, defer_delivery_commit=False):
            return FakeParsed("Chat chạy nhanh ghê.")

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def should_not_deliver(request_id: str, text: str):
            deliveries.append(text)
            raise AssertionError("duplicate room output must not reach delivery")

        runner.run_ambient_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = should_not_deliver
        loop._room_reaction_dedup.record("Chat chạy nhanh ghê.")
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SUMMARY,
            refs=(),
            proactive_source=None,
        )

        assert await loop._exec_room_reaction(decision, 10.0) is False
        assert finalizations == [False, False]
        assert deliveries == []
        assert pool.size() == 1
        assert director.get_metrics()["director_last_room_reaction_ts"] is None
        assert director.get_metrics()["director_room_reaction_deferred_until"] == 40.0
        assert loop.get_metrics()["director_room_reaction_suppressed_total"] == 1

    async def test_failed_room_delivery_does_not_record_recent_output(self) -> None:
        loop, director, _, _, runner, _ = _make()

        async def failed(request_id: str, text: str) -> TTSDeliveryResult:
            return TTSDeliveryResult(
                request_id=request_id, delivered=False, mode=TTSDeliveryMode.NONE,
            )

        loop._speak = failed
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.VIBE,
            refs=(),
            proactive_source=None,
        )

        assert await loop._exec_room_reaction(decision, 10.0) is False
        assert loop.get_metrics()["director_room_reaction_recent_count"] == 0
        assert loop.get_metrics()["director_speech_dedup_recent_count"] == 0
        assert loop.get_metrics()["director_speech_style_recent_count"] == 0
        assert director.get_metrics()["director_last_room_reaction_ts"] is None

    async def test_read_duplicate_with_reversed_sentences_regenerates_before_delivery(
        self,
    ) -> None:
        loop, _, pool, _, runner, _ = _make()
        first = "Béo ở đâu chứ? Lôi tớ ra trêu cũng vui thật đấy."
        reversed_order = "Lôi tớ ra trêu cũng vui thật đấy. Béo ở đâu chứ?"
        outputs = iter((reversed_order, "Tớ nhận vụ trêu này, nhưng đừng bịa cân nặng nha."))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        ref = pool.add("chat-1", "Mai béo", now=1.0, kind="chat")
        loop._speech_dedup.record(first)

        async def generate(**_kwargs):
            text = next(outputs)
            return ParsedResponse(
                text=text, mood=MoodState(), ok=True, raw=text,
            ), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert finalizations == [False, True]
        assert deliveries == ["Tớ nhận vụ trêu này, nhưng đừng bịa cân nặng nha."]
        assert pool.size() == 0
        metrics = loop.get_metrics()
        assert metrics["director_speech_dedup_duplicate_total"] == 1
        assert metrics["director_speech_dedup_regenerated_total"] == 1
        assert metrics["director_speech_dedup_suppressed_total"] == 0

    async def test_read_formula_opener_regenerates_once_before_delivery(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_max_formula_openers=0,
        )
        outputs = iter(("Mà chuyện này ổn rồi.", "Chuyện này ổn rồi."))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        stages: list[str] = []
        ref = pool.add("chat-style", "ổn chưa", now=1.0, kind="chat")

        async def generate(**_kwargs):
            stages.append(str(_kwargs.get("stage_direction") or ""))
            return FakeParsed(next(outputs)), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert finalizations == [False, True]
        assert deliveries == ["Chuyện này ổn rồi."]
        assert "SỬA VĂN PHONG" in stages[1]
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_violation_total"] == 1
        assert metrics["director_speech_style_regenerated_total"] == 1
        assert metrics["director_speech_style_exhausted_total"] == 0

    async def test_read_human_like_guards_repair_language_and_keep_literal_grounding(
        self,
    ) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_formula_phrases=("rồi đấy",),
            speech_style_language_integrity_fragments=("kalau",),
            speech_style_max_regenerations=2,
        )
        outputs = iter(("Kalau câu này ổn rồi đấy.", "Câu này ổn rồi đấy."))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        stages: list[str] = []
        ref = pool.add("chat-grounding", ":)", now=1.0, kind="chat")

        async def generate(**kwargs):
            stages.append(str(kwargs.get("stage_direction") or ""))
            return FakeParsed(next(outputs)), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert finalizations == [False, True]
        assert deliveries == ["Câu này ổn rồi đấy."]
        assert "[Literal grounding]" in stages[0]
        assert "Never invent viewer intent" in stages[0]
        assert "SỬA VĂN PHONG" in stages[1]
        assert "kalau" in stages[1].casefold()
        metrics = loop.get_metrics()
        assert metrics[
            "director_speech_style_formula_observed_delivery_total"
        ] == 1
        assert metrics["director_speech_style_formula_observed_hit_total"] == 1
        assert metrics["director_speech_style_language_violation_total"] == 1
        assert metrics["director_speech_style_regenerated_total"] == 1

    async def test_read_repairs_generated_malformed_mixed_case_token(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_malformed_token_fragments=("thiệt da",),
            speech_style_malformed_token_allowlist=("OpenAI",),
            speech_style_malformed_mixed_case_min_prefix_chars=3,
            speech_style_max_regenerations=1,
        )
        outputs = iter((
            "Đừng để cái mặt nghClient trăn trở mãi.",
            "Cậu cứ thong thả chọn đi.",
        ))
        ref = pool.add("chat-malformed", "cứ thong thả chọn", now=1.0, kind="chat")
        delivered: list[str] = []

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        async def speak(request_id: str, text: str) -> TTSDeliveryResult:
            delivered.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        loop._speak = speak
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT, read_mode=ReadMode.SINGLE,
            refs=(ref,), goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert delivered == ["Cậu cứ thong thả chọn đi."]
        metrics = loop.get_metrics()
        assert metrics["director_malformed_token_violation_total"] == 1
        assert metrics["director_malformed_token_suppressed_total"] == 0

    async def test_read_suppresses_persistent_malformed_token(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_malformed_token_fragments=("thiệt da",),
            speech_style_malformed_token_allowlist=("OpenAI",),
            speech_style_malformed_mixed_case_min_prefix_chars=3,
            speech_style_max_regenerations=2,
        )
        outputs = iter((
            "Câu này nghe thiệt da.",
            "Vẫn thiệt da như vậy.",
            "Tớ cứ thấy thiệt da.",
        ))
        ref = pool.add("chat-malformed-exhaust", "câu này nghe lạ", now=1.0, kind="chat")
        delivered: list[str] = []

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        async def speak(_request_id: str, text: str) -> TTSDeliveryResult:
            delivered.append(text)
            raise AssertionError("persistent malformed output must not reach delivery")

        runner.run_turn = generate
        loop._speak = speak
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT, read_mode=ReadMode.SINGLE,
            refs=(ref,), goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is False
        assert delivered == []
        metrics = loop.get_metrics()
        assert metrics["director_malformed_token_violation_total"] == 3
        assert metrics["director_malformed_token_suppressed_total"] == 1

    async def test_read_suppresses_persistent_semantic_over_inference(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_semantic_over_inference_patterns=("là biết",),
            speech_style_max_regenerations=2,
        )
        outputs = iter((
            "Nhìn icon là biết cậu đang bực mình.",
            "Nhìn vậy là biết cậu đang có chuyện.",
            "Thế là biết cậu đang giấu điều gì đó.",
        ))
        ref = pool.add("chat-semantic", ":)", now=1.0, kind="chat")
        delivered: list[str] = []

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        async def speak(_request_id: str, text: str) -> TTSDeliveryResult:
            delivered.append(text)
            raise AssertionError("persistent inference must not reach delivery")

        runner.run_turn = generate
        loop._speak = speak
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT, read_mode=ReadMode.SINGLE,
            refs=(ref,), goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is False
        assert delivered == []
        assert runner.committed == []
        metrics = loop.get_metrics()
        assert metrics["director_semantic_over_inference_violation_total"] == 3
        assert metrics["director_semantic_over_inference_suppressed_total"] == 1

    async def test_vague_grounding_is_repaired_before_delivery(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_vague_input_max_words=1,
            speech_style_vague_grounding_forbidden_patterns=("chắc chắn",),
            speech_style_max_regenerations=2,
        )
        outputs = iter((
            "Nụ cười này chắc chắn là cậu đang trêu tớ rồi.",
            "Tớ chỉ thấy một dấu cười, còn ý nghĩa thì chưa rõ.",
        ))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        ref = pool.add("chat-vague", ":)", now=1.0, kind="chat")

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert finalizations == [False, True]
        assert deliveries == ["Tớ chỉ thấy một dấu cười, còn ý nghĩa thì chưa rõ."]
        metrics = loop.get_metrics()
        assert metrics["director_grounding_violation_total"] == 1
        assert metrics["director_grounding_suppressed_total"] == 0

    async def test_exhausted_question_budget_drops_only_question_sentence(
        self,
    ) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_max_questions=0,
            speech_style_max_regenerations=1,
        )
        outputs = iter((
            "Phần đã biết vẫn ổn. Cậu nghĩ sao?",
            "Ý chính vẫn giữ nguyên. Cậu thấy thế nào?",
        ))
        deliveries: list[str] = []
        ref = pool.add("chat-question-clamp", "ổn", now=1.0, kind="chat")

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert deliveries == ["Ý chính vẫn giữ nguyên."]
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_clamped_total"] == 1
        assert metrics["director_speech_style_exhausted_total"] == 0

    async def test_exhausted_vague_grounding_is_not_delivered_or_committed(
        self,
    ) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_vague_input_max_words=1,
            speech_style_vague_grounding_forbidden_patterns=("âm mưu",),
            speech_style_max_regenerations=2,
        )
        outputs = iter((
            "Cậu đang âm mưu trêu tớ.",
            "Dấu cười này là một âm mưu rồi.",
            "Tớ vẫn thấy có âm mưu trong đó.",
        ))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        ref = pool.add("chat-vague-hard", ":)", now=1.0, kind="chat")

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(_request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            raise AssertionError("ungrounded candidate must not reach delivery")

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is False
        assert finalizations == [False, False, False]
        assert deliveries == []
        assert pool.size() == 1
        metrics = loop.get_metrics()
        assert metrics["director_grounding_violation_total"] == 3
        assert metrics["director_grounding_suppressed_total"] == 1

    async def test_read_question_can_use_second_bounded_style_repair(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_max_questions=0,
            speech_style_max_regenerations=2,
        )
        outputs = iter((
            "Cậu muốn kể tiếp không?",
            "Vẫn còn chuyện khác hả?",
            "Nghe vậy tớ vẫn tò mò về phần tiếp theo.",
        ))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        ref = pool.add("chat-question", "còn chuyện nữa", now=1.0, kind="chat")

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert finalizations == [False, False, True]
        assert deliveries == ["Nghe vậy tớ vẫn tò mò về phần tiếp theo."]
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_regenerated_total"] == 2
        assert metrics["director_speech_style_exhausted_total"] == 0

    async def test_exhausted_question_repair_keeps_existing_statement(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_max_questions=0,
            speech_style_max_regenerations=1,
        )
        outputs = iter((
            "Sao lại gọi sai tên thế? Tớ là Mai cơ mà, nhớ kỹ vào đấy.",
            "Sao lại gọi sai tên thế? Tớ là Mai cơ mà, nhớ kỹ vào đấy.",
        ))
        deliveries: list[str] = []
        ref = pool.add("chat-question-clamp", "Anami ơi", now=1.0, kind="chat")

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert deliveries == ["Tớ là Mai cơ mà, nhớ kỹ vào đấy."]
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_regenerated_total"] == 1
        assert metrics["director_speech_style_clamped_total"] == 1
        assert metrics["director_speech_style_exhausted_total"] == 0

    async def test_exhausted_style_correction_fails_open_without_quarantine(
        self,
    ) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_max_formula_openers=0,
        )
        outputs = iter(("Mà chuyện này ổn rồi.", "Ủa, chuyện này ổn rồi."))
        finalizations: list[bool] = []
        deliveries: list[str] = []
        ref = pool.add("chat-style", "ổn chưa", now=1.0, kind="chat")

        async def generate(**_kwargs):
            return FakeParsed(next(outputs)), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert finalizations == [False, True]
        assert deliveries == ["Ủa, chuyện này ổn rồi."]
        assert pool.size() == 0
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_violation_total"] == 2
        assert metrics["director_speech_style_exhausted_total"] == 1
        assert metrics["director_speech_dedup_quarantined_total"] == 0

    async def test_exhausted_shape_correction_is_clamped_before_delivery(self) -> None:
        loop, _, pool, _, runner, _ = _make(
            speech_style_max_sentences=1,
            speech_style_max_words=8,
        )
        outputs = iter((
            "Một hai ba bốn năm sáu. Bảy tám chín.",
            "Câu sửa vẫn khá dài. Câu thứ hai thừa.",
        ))
        deliveries: list[str] = []
        ref = pool.add("chat-shape", "kể ngắn thôi", now=1.0, kind="chat")

        async def generate(**_kwargs):
            text = next(outputs)
            return ParsedResponse(
                text=text, mood=MoodState(), ok=True, raw=text,
            ), 0

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_turn = generate
        loop._speak = deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is True
        assert deliveries == ["Câu sửa vẫn khá dài."]
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_regenerated_total"] == 1
        assert metrics["director_speech_style_clamped_total"] == 1
        assert metrics["director_speech_style_exhausted_total"] == 0

    async def test_continue_thread_duplicate_regenerates_with_bounded_context(
        self,
    ) -> None:
        loop, _, _, _, runner, _ = _make()
        first = "Đế chế thú bông mạnh lắm đấy. Mỗi con có quyền lực riêng."
        reversed_order = "Mỗi con có quyền lực riêng. Đế chế thú bông mạnh lắm đấy."
        outputs = iter((reversed_order, "Đội quân này còn thiếu một con chuyên canh bánh quy."))
        contexts: list[str] = []
        finalizations: list[bool] = []
        loop._speech_dedup.record(first)
        now = datetime.fromtimestamp(10.0, tz=timezone.utc)
        thread = OpenThread(
            "thread-1", "đế chế thú bông", "đang bàn về thú bông",
            now, now, now + timedelta(minutes=5), next_move=ConversationMove.DEEPEN,
        )
        goal = Goal(
            goal_id="goal:continue", kind=GoalKind.CONTINUE_THREAD,
            status=GoalStatus.ACTIVE, priority=50, reason="continue",
            source=GoalSource.RULE, created_at=now,
            expires_at=now + timedelta(minutes=5),
            success_conditions=("speech_completed",), parent_thread_id=thread.thread_id,
            metadata={"source_event_id": "chat-1"},
        )
        director_input = DirectorInput(
            now=10.0,
            agent_state=AgentStateSnapshot(open_threads=(thread,)),
            goals=_goal_snapshot(goal),
        )

        async def generate(_request_id: str, context: str, **_kwargs):
            contexts.append(context)
            return FakeParsed(next(outputs))

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        runner.run_directed_turn = generate
        runner.finalize_delivery = finalize
        decision = SimpleNamespace(
            action=DirectorAction.CONTINUE_THREAD,
            goal_id=goal.goal_id,
        )

        assert await loop._exec_goal_action(decision, 10.0, director_input) is True
        assert finalizations == [False, True]
        assert len(contexts) == 2
        assert "SỬA CÂU BỊ LẶP" in contexts[1]
        assert runner.committed == ["Đội quân này còn thiếu một con chuyên canh bánh quy."]

    async def test_second_public_duplicate_is_suppressed_without_delivery(self) -> None:
        loop, _, pool, _, runner, _ = _make()
        repeated = "Béo ở đâu chứ? Lôi tớ ra trêu cũng vui thật đấy."
        finalizations: list[bool] = []
        deliveries: list[str] = []
        ref = pool.add("chat-1", "Mai béo", now=1.0, kind="chat")
        loop._speech_dedup.record(repeated)

        async def generate(**_kwargs):
            return FakeParsed(repeated), 0

        def finalize(_request_id: str, success: bool) -> None:
            finalizations.append(success)

        async def should_not_deliver(request_id: str, text: str) -> TTSDeliveryResult:
            deliveries.append(text)
            raise AssertionError(f"duplicate {request_id} must not reach delivery")

        runner.run_turn = generate
        runner.finalize_delivery = finalize
        loop._speak = should_not_deliver
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.SINGLE,
            refs=(ref,),
            goal_id=None,
        )

        assert await loop._exec_read(decision, 10.0) is False
        assert finalizations == [False, False]
        assert deliveries == []
        assert pool.size() == 0
        assert loop.get_metrics()["director_speech_dedup_suppressed_total"] == 1
        assert loop.get_metrics()["director_speech_dedup_quarantined_total"] == 1

    async def test_public_speech_recent_buffer_is_bounded(self) -> None:
        loop, _, _, _, _, _ = _make(speech_dedup_recent_window=2)
        for index, text in enumerate(("Một.", "Hai.", "Ba."), start=1):
            assert await loop._maybe_speak(
                f"turn-{index}", FakeParsed(text), DirectorAction.READ_CHAT, [],
            ) is True

        assert loop.get_metrics()["director_speech_dedup_recent_count"] == 2
        assert loop._speech_dedup.recent() == ["Hai.", "Ba."]

    async def test_duplicate_quarantine_resolves_thread_and_cancels_goal(self) -> None:
        loop, _, _, _, _, _ = _make()

        class _Threads:
            def __init__(self) -> None:
                self.resolved: list[tuple[str, str]] = []

            def resolve(self, thread_id: str, *, reason: str) -> bool:
                self.resolved.append((thread_id, reason))
                return True

        class _Goals:
            def __init__(self) -> None:
                self.cancelled: list[tuple[str, str]] = []

            def cancel(self, goal_id: str, *, reason: str) -> bool:
                self.cancelled.append((goal_id, reason))
                return True

        threads = _Threads()
        goals = _Goals()
        loop._thread_manager = threads
        loop._goal_manager = goals

        loop._quarantine_repetition_context(
            refs=[], thread_id="thread-1", goal_id="goal-1",
        )

        assert threads.resolved == [("thread-1", "speech_duplicate")]
        assert goals.cancelled == [("goal-1", "speech_duplicate")]
        assert loop.get_metrics()["director_speech_dedup_quarantined_total"] == 1

    async def test_room_recent_buffer_remains_bounded(self) -> None:
        loop, _, _, _, runner, _ = _make(room_reaction_recent_window=2)
        outputs = iter(("Một.", "Hai.", "Ba."))

        async def generate(request_id: str, prompt: str):
            return FakeParsed(next(outputs))

        runner.run_ambient_turn = generate
        decision = SimpleNamespace(
            action=DirectorAction.READ_CHAT,
            read_mode=ReadMode.VIBE,
            refs=(),
            proactive_source=None,
        )
        for now in (1.0, 2.0, 3.0):
            assert await loop._exec_room_reaction(decision, now) is True

        assert loop.get_metrics()["director_room_reaction_recent_count"] == 2
        assert loop._room_reaction_dedup.recent() == ["Hai.", "Ba."]

    async def test_filter_reject_never_reaches_delivery_or_commit(self) -> None:
        loop, _, _, _, runner, _ = _make()
        deliveries: list[str] = []

        async def should_not_deliver(request_id: str, _text: str) -> TTSDeliveryResult:
            deliveries.append(request_id)
            return TTSDeliveryResult(
                request_id=request_id,
                delivered=True,
                mode=TTSDeliveryMode.SUBTITLE,
            )

        runner.last_filter_verdict = FilterVerdict(
            passed=False,
            categories_hit=[FilterCategory.PERSONA_BREAK],
            severity="medium",
            suggested_action="regenerate",
            reason="regeneration exhausted",
        )
        loop._speak = should_not_deliver

        spoken = await loop._maybe_speak(
            "filtered", FakeParsed(text="Nếu tớ là Anami thì..."),
            DirectorAction.READ_CHAT, [],
        )

        assert spoken is False
        assert deliveries == []

    async def test_filter_reject_quarantines_thread_goal_and_chat_ref(self) -> None:
        loop, _, pool, _, runner, _ = _make()
        runner.last_filter_verdict = FilterVerdict(
            passed=False,
            categories_hit=[FilterCategory.PERSONA_BREAK],
            severity="medium",
            suggested_action="regenerate",
            reason="unsafe",
            latency_ms=0,
        )

        class _Threads:
            resolved: list[tuple[str, str]] = []

            def resolve(self, thread_id: str, *, reason: str) -> bool:
                self.resolved.append((thread_id, reason))
                return True

        class _Goals:
            cancelled: list[tuple[str, str]] = []

            def cancel(self, goal_id: str, *, reason: str) -> bool:
                self.cancelled.append((goal_id, reason))
                return True

        threads = _Threads()
        goals = _Goals()
        loop._thread_manager = threads
        loop._goal_manager = goals
        ref = SimpleNamespace(msg_id="unsafe-chat")

        spoken = await loop._maybe_speak(
            "unsafe", FakeParsed("bad"), DirectorAction.CONTINUE_THREAD,
            [ref], thread_id="thread-1", goal_id="goal-1",
        )

        assert spoken is False
        assert threads.resolved == [("thread-1", "filter_rejected")]
        assert goals.cancelled == [("goal-1", "filter_rejected")]
        assert loop._filter_context_quarantined_total == 1
        assert runner.committed == []

    async def test_stale_thread_goal_is_reconciled_before_chat_arbitration(self) -> None:
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        goal_manager = GoalManager(
            GoalLimits(4, 2, 8, 120), clock=lambda: now,
        )
        assert goal_manager.submit(Goal(
            goal_id="stale-thread-goal",
            kind=GoalKind.CONTINUE_THREAD,
            status=GoalStatus.CANDIDATE,
            priority=40,
            reason="continue expired topic",
            source=GoalSource.RULE,
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            success_conditions=("continue once",),
            parent_thread_id="expired-thread",
        ))

        class _State:
            @staticmethod
            def snapshot() -> AgentStateSnapshot:
                return AgentStateSnapshot(open_threads=())

        loop, _, pool, pulse, _, clock = _make(
            agent_state=_State(), goal_manager=goal_manager,
        )
        pool.add("live-chat", "Mai ơi trả lời câu này?", now=0.0, kind="mention")
        pulse.record(now=0.0, user_id="viewer")
        clock["t"] = 1.0

        decision = loop.preview_decision(1.0)

        assert decision.action is DirectorAction.READ_CHAT
        assert decision.reason == "top_single"
        snapshot = goal_manager.snapshot()
        assert snapshot.active is None
        assert snapshot.recent_terminal[-1].suspend_reason == "parent_thread_missing"

    async def test_self_talk_duplicate_of_public_delivery_is_suppressed(self) -> None:
        lore_material = LoreMaterialProvider((LoreMaterial(
            material_id="plushies",
            section="Thích",
            anchor="Lore đã xác thực về Mai: Mai sưu tầm thú bông.",
        ),))
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            lore_material=lore_material,
            wait_for_chat_seconds=60.0,
            min_silence_seconds=20.0,
            max_previous_text_chars=80,
        )
        loop, _, _, _, runner, _ = _make(autonomy=FakeAutonomy())
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(silence_seconds=30.0, working_memory_recent=[]),
        )
        repeated = "Đội quân thú bông của tớ đang đông lên rồi."
        loop._speech_dedup.record(repeated)
        deliveries: list[str] = []
        finalizations: list[bool] = []

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        async def generate(
            _request_id: str, _prompt: str, **_kwargs: object,
        ) -> FakeParsed:
            return FakeParsed(repeated)

        async def should_not_deliver(
            request_id: str, text: str,
        ) -> TTSDeliveryResult:
            deliveries.append(text)
            raise AssertionError(f"duplicate {request_id} must not reach delivery")

        runner.run_ambient_turn = generate
        runner.finalize_delivery = lambda _request_id, success: finalizations.append(success)
        loop._speak = should_not_deliver

        assert await loop._exec_self_talk(_Decision(), 20.0) is False
        assert deliveries == []
        assert finalizations == [False]
        assert planner.snapshot()["pending_plan_id"] is None
        assert runner.committed == []
        metrics = loop.get_metrics()
        assert metrics["director_speech_dedup_suppressed_total"] == 1
        assert metrics["director_speech_dedup_quarantined_total"] == 0

    async def test_self_talk_planner_advances_only_when_delivery_succeeds(self) -> None:
        lore_material = LoreMaterialProvider((LoreMaterial(
            material_id="plushies",
            section="Thích",
            anchor="Lore đã xác thực về Mai: Mai sưu tầm thú bông.",
        ),))
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            lore_material=lore_material,
            wait_for_chat_seconds=60.0,
            min_silence_seconds=20.0,
            max_previous_text_chars=80,
        )
        loop, _, _, _, runner, _ = _make(autonomy=FakeAutonomy())
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=[],
            ),
        )

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        async def failed(request_id: str, _text: str) -> TTSDeliveryResult:
            return TTSDeliveryResult(
                request_id=request_id,
                delivered=False,
                mode=TTSDeliveryMode.SUBTITLE,
            )

        loop._speak = failed
        assert await loop._exec_self_talk(_Decision(), 20.0) is False
        assert planner.snapshot()["stage"] == "open"
        assert planner.snapshot()["pending_plan_id"] is None
        assert runner.committed == []
        assert lore_material.get_metrics()["self_talk_lore_releases_total"] == 1
        assert loop.get_metrics()["director_self_talk_deferred_until"] == 110.0
        assert planner.readiness(21.0).reason == "thought_unavailable"

        async def delivered(request_id: str, _text: str) -> TTSDeliveryResult:
            return TTSDeliveryResult(
                request_id=request_id,
                delivered=True,
                mode=TTSDeliveryMode.SUBTITLE,
                sentences_total=1,
                sentences_delivered=1,
                subtitle_sentences=1,
            )

        loop._speak = delivered
        assert await loop._exec_self_talk(_Decision(), 111.0) is True
        assert planner.snapshot()["stage"] == "develop"
        assert len(runner.committed) == 1
        assert lore_material.get_metrics()["self_talk_lore_commits_total"] == 1

    async def test_self_talk_shape_regenerates_once_before_delivery(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            min_silence_seconds=20.0,
            stage_limits={"open": {"max_sentences": 1, "allow_question": False}},
        )
        loop, _, _, _, runner, _ = _make(autonomy=FakeAutonomy())
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["chat đang bàn về trà"],
            ),
        )
        outputs = iter((
            FakeParsed("Tớ nghĩ một ý. Rồi thêm ý nữa?"),
            FakeParsed("Tự nhiên tớ vừa để ý khoảng im này."),
        ))

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            return next(outputs)

        spoken: list[str] = []

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            spoken.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_ambient_turn = generate
        loop._speak = deliver

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is True
        assert len(runner.ambient_calls) == 2
        assert spoken == ["Tự nhiên tớ vừa để ý khoảng im này."]
        assert planner.get_metrics()["self_talk_planner_output_rejected_total"] == 1

    async def test_self_talk_style_guard_repairs_language_after_shape_rewrite(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            min_silence_seconds=20.0,
            stage_limits={"open": {"max_sentences": 1, "allow_question": False}},
        )
        loop, _, _, _, runner, _ = _make(
            autonomy=FakeAutonomy(),
            speech_style_language_integrity_fragments=("kalau",),
            speech_style_max_regenerations=1,
        )
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["chat đang bàn về trà"],
            ),
        )
        outputs = iter((
            FakeParsed("Tớ nghĩ một ý. Rồi thêm ý nữa?"),
            FakeParsed("Nhưng kalau là tớ thì sẽ chờ."),
            FakeParsed("Tớ sẽ chờ thêm một chút."),
        ))

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            return next(outputs)

        spoken: list[str] = []

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            spoken.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_ambient_turn = generate
        loop._speak = deliver

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is True
        assert len(runner.ambient_calls) == 3
        assert "SỬA VĂN PHONG" in runner.ambient_calls[2]
        assert spoken == ["Tớ sẽ chờ thêm một chút."]
        assert planner.snapshot()["stage"] == "develop"
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_language_violation_total"] == 1
        assert metrics["director_speech_style_regenerated_total"] == 1

    async def test_self_talk_repairs_semantic_inference_against_plan_grounding(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nêu phản ứng vào chi tiết literal",),
            min_silence_seconds=20.0,
            stage_limits={"open": {"max_sentences": 1, "allow_question": False}},
        )
        loop, _, _, _, runner, _ = _make(
            autonomy=FakeAutonomy(),
            speech_style_semantic_over_inference_patterns=("muốn tạo", "là biết"),
            speech_style_max_regenerations=1,
        )
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=[
                    "vẫy tay với tui đi nữ hoàng :hugging_face:",
                ],
            ),
        )
        outputs = iter((
            FakeParsed("Nhìn icon là biết cậu muốn tạo không khí vui vẻ."),
            FakeParsed("Tớ chỉ thấy một lời nhờ vẫy tay khá dễ thương."),
        ))

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            return next(outputs)

        spoken: list[str] = []

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            spoken.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_ambient_turn = generate
        loop._speak = deliver

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is True
        assert spoken == ["Tớ chỉ thấy một lời nhờ vẫy tay khá dễ thương."]
        assert len(runner.ambient_calls) == 2
        metrics = loop.get_metrics()
        assert metrics["director_semantic_over_inference_violation_total"] == 1
        assert metrics["director_semantic_over_inference_suppressed_total"] == 0

    async def test_self_talk_semantic_exhaustion_releases_and_defers_plan(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nêu phản ứng vào chi tiết literal",),
            min_silence_seconds=20.0,
            stage_limits={"open": {"max_sentences": 1, "allow_question": False}},
        )
        loop, _, _, _, runner, _ = _make(
            autonomy=FakeAutonomy(),
            speech_style_semantic_over_inference_patterns=("là biết",),
            speech_style_max_regenerations=2,
        )
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["cậu vừa gửi một icon vẫy tay"],
            ),
        )
        outputs = iter((
            FakeParsed("Nhìn là biết cậu đang vui."),
            FakeParsed("Thế là biết cậu muốn trêu tớ."),
            FakeParsed("Vậy là biết cậu đang hào hứng."),
        ))

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            return next(outputs)

        runner.run_ambient_turn = generate

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is False
        assert runner.committed == []
        assert planner.snapshot()["pending_plan_id"] is None
        assert planner.readiness(31.0).reason == "thought_unavailable"
        metrics = loop.get_metrics()
        assert metrics["director_semantic_over_inference_violation_total"] == 3
        assert metrics["director_semantic_over_inference_suppressed_total"] == 1

    async def test_self_talk_suppresses_persistent_configured_language(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            min_silence_seconds=20.0,
            stage_limits={"open": {"max_sentences": 1, "allow_question": False}},
        )
        loop, _, _, _, runner, _ = _make(
            autonomy=FakeAutonomy(),
            speech_style_language_integrity_fragments=("kalau",),
            speech_style_max_regenerations=2,
        )
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["chat đang bàn về trà"],
            ),
        )
        outputs = iter((
            FakeParsed("Kalau thì tớ sẽ chờ."),
            FakeParsed("Tớ kalau sẽ chờ."),
            FakeParsed("Tớ vẫn kalau chờ."),
        ))

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            return next(outputs)

        runner.run_ambient_turn = generate

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is False
        assert len(runner.ambient_calls) == 3
        assert runner.committed == []
        assert planner.snapshot()["pending_plan_id"] is None
        assert planner.readiness(31.0).reason == "thought_unavailable"
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_language_violation_total"] == 3
        assert metrics["director_speech_style_exhausted_total"] == 1

    async def test_self_talk_receives_global_style_directive_before_generation(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            min_silence_seconds=20.0,
            stage_limits={"open": {"max_sentences": 1, "allow_question": False}},
        )
        loop, _, _, _, runner, _ = _make(
            autonomy=FakeAutonomy(),
            speech_style_max_formula_openers=0,
        )
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["chat đang bàn về trà"],
            ),
        )
        output = FakeParsed("Tớ vừa để ý mạch chat này.")

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            return output

        spoken: list[str] = []

        async def deliver(request_id: str, text: str) -> TTSDeliveryResult:
            spoken.append(text)
            return TTSDeliveryResult(
                request_id=request_id, delivered=True,
                mode=TTSDeliveryMode.SUBTITLE, sentences_total=1,
                sentences_delivered=1, subtitle_sentences=1,
            )

        runner.run_ambient_turn = generate
        loop._speak = deliver

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is True
        assert "Ràng buộc nhịp văn phong" in runner.ambient_calls[0]
        assert "Không mở câu trả lời này" in runner.ambient_calls[0]
        assert "tối đa 1 câu" in runner.ambient_calls[0]
        assert len(runner.ambient_calls) == 1
        assert spoken == ["Tớ vừa để ý mạch chat này."]
        metrics = loop.get_metrics()
        assert metrics["director_speech_style_regenerated_total"] == 0
        assert metrics["director_speech_style_exhausted_total"] == 0

    async def test_chat_during_generation_blocks_ambient_delivery_and_preserves_arc(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            min_silence_seconds=20.0,
        )
        loop, _, _, _, runner, _ = _make(autonomy=FakeAutonomy())
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["chat đang bàn về trà"],
            ),
        )

        async def generate(_request_id: str, prompt: str):
            runner.ambient_calls.append(prompt)
            planner.on_chat(31.0)
            return FakeParsed("Tự nhiên tớ vừa để ý khoảng im này.")

        delivered: list[str] = []

        async def deliver(_request_id: str, text: str):
            delivered.append(text)
            raise AssertionError("interrupted ambient output must not reach TTS")

        runner.run_ambient_turn = generate
        loop._speak = deliver

        class _Decision:
            action = DirectorAction.SELF_TALK
            proactive_source = None
            proactive_summary = None
            proactive_category = None

        assert await loop._exec_self_talk(_Decision(), 30.0) is False
        assert delivered == []
        assert planner.snapshot()["stage"] == "open"
        assert planner.snapshot()["pending_plan_id"] is None

    async def test_chat_gate_suppression_is_counted_by_loop_not_director(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("low", "xin chào", now=0.0, kind="chat")
        pulse.record(now=0.0, user_id="u1")
        pulse.record(now=0.0, user_id="u2")
        before = director.get_metrics()
        clock["t"] = 1.0

        action = await loop.tick_once()

        assert action is DirectorAction.WAIT
        assert director.get_metrics() == before
        assert loop.get_metrics()["director_chat_suppressed_total"] == 1
        assert runner.read_calls == []

    async def test_canned_fallback_text_reaches_delivery_boundary(self) -> None:
        delivered: list[tuple[str, str]] = []

        async def speak(request_id: str, text: str):
            delivered.append((request_id, text))
            return TTSDeliveryResult(
                request_id=request_id,
                delivered=True,
                mode=TTSDeliveryMode.SUBTITLE,
                sentences_total=1,
                sentences_delivered=1,
                subtitle_sentences=1,
            )

        loop, *_ = _make()
        loop._speak = speak
        parsed = FakeParsed(text="Câu dự phòng", ok=False, raw="<canned>")

        reached = await loop._maybe_speak(
            "fallback", parsed, DirectorAction.SELF_TALK, [],
        )

        assert reached is True
        assert delivered == [("fallback", "Câu dự phòng")]

    async def test_successful_action_publishes_grounded_director_event(self) -> None:
        class State:
            def __init__(self) -> None:
                self.events = []
            def record(self, event) -> bool:
                self.events.append(event)
                return True
            def snapshot(self):
                return type("Snapshot", (), {"active_goal_ref": "goal-read"})()

        state = State()
        loop, director, pool, pulse, runner, clock = _make(agent_state=state)
        pool.add("m1", "Mai ơi chơi gì", now=0.0, kind="mention")
        clock["t"] = 1.0
        await loop.tick_once()
        assert [event.kind.value for event in state.events] == [
            "speech_completed", "director_action",
        ]
        assert state.events[0].payload["action"] == "read_chat"
        assert state.events[0].payload["goal_id"] == "goal-read"
        assert state.events[1].payload["action"] == "read_chat"

    async def test_successful_self_talk_publishes_grounded_event(self) -> None:
        class State:
            def __init__(self) -> None:
                self.events = []
            def record(self, event) -> bool:
                self.events.append(event)
                return True

        state = State()
        loop, director, pool, pulse, runner, clock = _make(
            autonomy=FakeAutonomy(has_material=True), agent_state=state,
        )
        clock["t"] = 25.0
        await loop.tick_once()
        assert [event.kind.value for event in state.events] == [
            "self_talk_completed", "director_action",
        ]

    async def test_agent_state_failure_does_not_fail_director_action(self) -> None:
        class BrokenState:
            def record(self, event) -> bool:
                raise RuntimeError("state unavailable")

        loop, director, pool, pulse, runner, clock = _make(agent_state=BrokenState())
        pool.add("m1", "Mai ơi chơi gì", now=0.0, kind="mention")
        clock["t"] = 1.0
        assert await loop.tick_once() == DirectorAction.READ_CHAT
        assert runner.read_calls == ["Mai ơi chơi gì"]

    async def test_read_pulls_and_removes_from_pool(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("m1", "Mai ơi chơi gì", now=0.0, kind="mention")
        clock["t"] = 1.0
        action = await loop.tick_once()
        assert action == DirectorAction.READ_CHAT
        assert runner.read_calls == ["Mai ơi chơi gì"]
        assert pool.size() == 0   # đã gỡ
        # TASK 5: SINGLE commit history = text chat gốc
        assert runner.hist_calls[0] == ("Mai ơi chơi gì", True)

    async def test_summary_via_ambient_no_user_turn(self) -> None:
        # De-AI register: SUMMARY react cả phòng → đường ambient (không run_turn/user turn)
        loop, director, pool, pulse, runner, clock = _make()
        distinct = [
            "trời hôm nay đẹp ghê", "ăn phở hay bún đây", "mèo nhà tao dễ thương",
            "deadline sắp tới rồi", "cà phê sáng ngon quá", "đi ngủ đây bye",
            "game mới hay không", "nhạc gì đang nghe vậy", "mưa to quá trời",
            "học bài chán ghê", "code lỗi hoài à", "đói bụng muốn xỉu",
            "xem phim gì tối nay", "cuối tuần đi chơi", "buồn ngủ dã man",
        ]
        for i, txt in enumerate(distinct):
            pool.add(f"c{i}", txt, now=0.0, kind="chat")
        clock["t"] = 1.0
        await loop.tick_once()
        assert runner.ambient_calls          # đi đường ambient
        assert runner.read_calls == []       # KHÔNG giả user turn

    async def test_superchat_acked_before_chat(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("c", "chat thường", now=0.0, kind="chat")
        pool.add("sc", "quà nè", now=0.0, kind="chat", amount_vnd=500_000, is_super=True)
        clock["t"] = 1.0
        action = await loop.tick_once()
        assert action == DirectorAction.ACK_DONATION
        # De-AI register: user turn = text chat thật; "cách xử" (ack) ở stage_direction
        assert "quà" in runner.read_calls[0]
        assert "superchat" in (runner.stage_calls[0] or "").lower()
        assert any(m.msg_id == "c" for m in pool.top_cluster(now=1.0, max_refs=10))

    async def test_ack_uses_viewer_name_not_id(self) -> None:
        # TASK 2: chỉ thị ack gọi tên hiển thị (ở stage_direction), không channel id
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("sc", "quà nè", now=0.0, kind="chat", viewer_id="UCxq3fZZ",
                 viewer_name="Alice", amount_vnd=500_000, is_super=True)
        clock["t"] = 1.0
        await loop.tick_once()
        stage = runner.stage_calls[0] or ""
        assert "Alice" in stage
        assert "UCxq3fZZ" not in stage
        assert "UCxq3fZZ" not in runner.read_calls[0]   # user turn cũng không rò id

    async def test_owner_role_is_grounded_in_stage_and_history_only(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        pool.add(
            "owner", "hiện tại tớ mới làm được Wordle", now=0.0,
            kind="question", viewer_name="Channel Owner", is_owner=True,
        )
        clock["t"] = 1.0

        await loop.tick_once()

        assert runner.read_calls[0] == "hiện tại tớ mới làm được Wordle"
        stage = runner.stage_calls[0] or ""
        assert "operator/chủ kênh" in stage
        assert "không tóm tắt lại lời người xem" in stage
        assert runner.hist_calls[0] == (
            "[Nguồn: operator/chủ kênh] hiện tại tớ mới làm được Wordle",
            True,
        )

    async def test_dead_air_triggers_self_talk(self) -> None:
        auto = FakeAutonomy(ready=False, has_material=True)
        loop, director, pool, pulse, runner, clock = _make(autonomy=auto)
        clock["t"] = 25.0   # 25s không nói (mark_spoke ở now=0) → dead-air
        action = await loop.tick_once()
        assert action == DirectorAction.SELF_TALK
        assert len(runner.ambient_calls) == 1
        assert runner.committed   # self-talk đã commit history

    async def test_self_talk_no_material_no_crash(self) -> None:
        auto = FakeAutonomy(ready=False, has_material=False)
        loop, director, pool, pulse, runner, clock = _make(autonomy=auto)
        clock["t"] = 25.0
        last_spoke_before = director._last_speak_ts
        action = await loop.tick_once()
        assert action == DirectorAction.SELF_TALK
        assert runner.ambient_calls == []   # không sinh gì (không material)
        assert director._last_speak_ts == last_spoke_before

    async def test_no_infinite_read_forces_self_talk(self) -> None:
        # DoD: sau max_consecutive_read_chat → ép self_talk dù pool còn tin
        auto = FakeAutonomy(ready=False, has_material=True)
        loop, director, pool, pulse, runner, clock = _make(autonomy=auto,
                                                           max_consecutive_read_chat=3)
        # bơm tin mention KHÁC NHAU (tránh cluster) để có nhiều tin đáp liên tiếp
        distinct = [
            "Mai thích ăn món gì", "Mai chơi tựa game nào", "Mai mấy tuổi rồi",
            "Mai hát được không đấy", "Mai có buồn hông", "Mai khoẻ chứ hôm nay",
        ]
        for i, txt in enumerate(distinct):
            pool.add(f"m{i}", txt, now=float(i), kind="mention")
            pulse.record(now=float(i), user_id=f"u{i}")
        actions = []
        for i in range(5):
            clock["t"] = float(i) + 0.5
            actions.append(await loop.tick_once())
        # không được có 4 read_chat liên tiếp
        streak = 0
        maxstreak = 0
        for a in actions:
            if a == DirectorAction.READ_CHAT:
                streak += 1
                maxstreak = max(maxstreak, streak)
            else:
                streak = 0
        assert maxstreak <= 3
        assert DirectorAction.SELF_TALK in actions

    async def test_summary_purges_backlog_no_repeat(self) -> None:
        # TASK 3: 1 SUMMARY dọn sạch backlog thấp → tick kế KHÔNG lại SUMMARY
        from services.director.director import ReadMode
        loop, director, pool, pulse, runner, clock = _make()
        distinct = [
            "trời hôm nay đẹp ghê", "ăn phở hay bún đây", "mèo nhà tao dễ thương",
            "deadline sắp tới rồi", "cà phê sáng ngon quá", "đi ngủ đây bye",
            "game mới hay không", "nhạc gì đang nghe vậy", "mưa to quá trời",
            "học bài chán ghê", "code lỗi hoài à", "đói bụng muốn xỉu",
            "xem phim gì tối nay", "cuối tuần đi đâu chơi", "buồn ngủ dã man",
        ]
        for i, txt in enumerate(distinct):
            pool.add(f"c{i}", txt, now=0.0, kind="chat")
        clock["t"] = 1.0
        action = await loop.tick_once()
        assert action == DirectorAction.READ_CHAT
        # backlog điểm thấp đã bị purge → pool còn rất ít / rỗng
        assert pool.size() <= 1
        # tick kế: không còn backlog → không SUMMARY (WAIT hoặc self_talk)
        clock["t"] = 1.5
        action2 = await loop.tick_once()
        assert action2 != DirectorAction.READ_CHAT or \
            director._read_decision(director.current_segment(),
                                    pool.peek_top(1.5), 1.5).read_mode != ReadMode.SUMMARY

    async def test_pulse_hype_pushes_mood_event_debounced(self) -> None:
        # TASK 7: chat sôi (HYPE_SPAM) → 1 emotion event chat_hype (edge, debounce)
        class FakeEmotion:
            def __init__(self):
                self.events = []
            def current_mood(self):
                from interfaces.animation import MoodState
                return MoodState()
            async def handle_event(self, ev):
                self.events.append(ev.meta.get("platform_type"))
                @dataclass
                class P:
                    category: str = "chat_hype"
                return P()

        emo = FakeEmotion()
        loop, director, pool, pulse, runner, clock = _make()
        loop._emotion = emo
        # bơm burst hype (nhiều tin, ít người)
        for i in range(30):
            pulse.record(now=float(i) * 0.1, user_id=f"u{i % 2}")
        clock["t"] = 3.0
        await loop.tick_once()
        assert "chat_hype" in emo.events
        n1 = len(emo.events)
        # tick lại cùng state → KHÔNG đẩy nữa (debounce)
        clock["t"] = 3.5
        await loop.tick_once()
        assert len(emo.events) == n1

    async def test_baseline_updated_each_tick(self) -> None:
        # TASK 6: tick_once cập nhật baseline → accel không kẹt 1.0 khi tempo đổi
        loop, director, pool, pulse, runner, clock = _make()
        # tick vài lần lúc chat sôi để baseline hấp thụ tempo cao
        for i in range(40):
            pulse.record(now=float(i) * 0.1, user_id=f"u{i % 3}")
        clock["t"] = 4.0
        await loop.tick_once()
        # baseline đã được set (khác None) → accel tính được (không mặc định 1.0 cứng)
        assert pulse._baseline_tempo is not None

    async def test_transition_advances_segment(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        # đang ở main (300s). Ép transition bằng cách nhảy quá giờ.
        clock["t"] = 400.0
        action = await loop.tick_once()
        assert action == DirectorAction.TRANSITION
        assert director.current_segment().name == "closing"
        assert runner.ambient_calls   # đã sinh câu báo chuyển


class TestChatRouterIntake:
    def test_intake_pushes_to_pool_not_turn(self) -> None:
        # ChatRouter intake mode: chat → pool, KHÔNG run_turn
        import asyncio
        from datetime import datetime, timezone

        from interfaces.input import EventSource, InputEvent
        from services.input.chat_router import ChatRouter

        pool = SaliencePool(base_tier={"chat": 10, "question": 25, "mention": 35})
        pulse = ChatPulse()

        class FakeEmotion:
            async def handle_event(self, ev):
                @dataclass
                class P:
                    category: str = "chat_neutral"
                return P()

        class FakeSource:
            service_id = "fake"

        router = ChatRouter(
            sources=[FakeSource()], emotion=FakeEmotion(),
            runner=FakeRunner(), pool=pool, pulse=pulse,
        )
        ev = InputEvent(
            event_id="e1", timestamp=datetime.now(timezone.utc),
            source=EventSource.CHAT_YOUTUBE, content="Mai ơi", user_id="v1",
            metadata={"is_owner": True, "is_moderator": False},
        )
        asyncio.new_event_loop().run_until_complete(router._process(ev))
        assert pool.size() == 1
        assert pool.peek_top(now=0.0).kind == "mention"
        assert pool.peek_top(now=0.0).is_owner is True
