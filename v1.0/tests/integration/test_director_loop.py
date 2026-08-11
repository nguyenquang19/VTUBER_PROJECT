"""Integration C0.4 — DirectorLoop turn driver với FakeLLM (docs/03_COMPONENT_REFERENCE.md §C0.4).

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
from interfaces.filter import FilterCategory, FilterVerdict
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from services.director.director_loop import DirectorLoop, _self_talk_correction_prompt
from services.director.salience import SaliencePool
from services.autonomy.self_talk_planner import SelfTalkPlanner
from services.autonomy.material_provider import RuntimeContext
from services.agent.goal_manager import GoalLimits, GoalManager
from services.agent.goal_types import Goal, GoalKind, GoalSource, GoalStatus
from services.agent.types import AgentStateSnapshot

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    )
    director.start(now)
    # move past opening so read_chat/ack allowed (main segment)
    director.advance_segment(now)
    director.mark_spoke(DirectorAction.TRANSITION, now)
    return loop, director, pool, pulse, runner, clock


@pytest.mark.asyncio
class TestDirectorLoop:
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

    async def test_self_talk_planner_advances_only_when_delivery_succeeds(self) -> None:
        planner = SelfTalkPlanner(
            cognitive_moves=("nhận ra một chi tiết nhỏ trong mỏ neo",),
            wait_for_chat_seconds=60.0,
            min_silence_seconds=20.0,
            max_previous_text_chars=80,
        )
        loop, _, _, _, runner, _ = _make(autonomy=FakeAutonomy())
        loop._self_talk_planner = planner
        loop.set_runtime_context_provider(
            lambda: RuntimeContext(
                silence_seconds=30.0,
                working_memory_recent=["chat đang bàn về trà"],
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
        assert await loop._exec_self_talk(_Decision(), 21.0) is True
        assert planner.snapshot()["stage"] == "develop"
        assert len(runner.committed) == 1

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
        ev = InputEvent(event_id="e1", timestamp=datetime.now(timezone.utc),
                        source=EventSource.CHAT_YOUTUBE, content="Mai ơi", user_id="v1")
        asyncio.new_event_loop().run_until_complete(router._process(ev))
        assert pool.size() == 1
        assert pool.peek_top(now=0.0).kind == "mention"
