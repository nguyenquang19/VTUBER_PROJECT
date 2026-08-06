"""Integration C0.4 — DirectorLoop turn driver với FakeLLM (ROADMAP §C0.4).

Verify Director cầm nhịp (không FIFO): chat vào pool → Director nhặt → sinh turn.
DoD: superchat acked first; read gỡ khỏi pool; dead-air→self_talk; no infinite read.
Dùng Fake runner (không cần llama/GPU).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from services.director.director_loop import DirectorLoop
from services.director.salience import SaliencePool

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        self.committed: list[str] = []

    async def run_turn(self, request_id, user_text, viewer_id=None,
                       trigger_type=None, event_category=None):
        self.read_calls.append(user_text)
        return FakeParsed(text=f"reply:{user_text}"), 0

    async def run_ambient_turn(self, request_id, prompt_text):
        self.ambient_calls.append(prompt_text)
        return FakeParsed(text=f"self:{prompt_text[:20]}")

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
                {"read_chat", "self_talk", "ack_donation", "transition"}),
        Segment("closing", "kết", 10.0, {"self_talk", "transition"}),
    ]


def _make(now=0.0, autonomy=None, **dir_over):
    pool = SaliencePool(base_tier={"chat": 10, "question": 25, "mention": 35},
                        tau_seconds=50.0, floor=3.0, cluster_coef=5.0)
    pulse = ChatPulse(window_seconds=60.0, tempo_low_per_min=2.0,
                      tempo_high_per_min=15.0, diversity_threshold=0.4,
                      cold_silence_seconds=90.0)
    clock = {"t": now}
    kw = dict(dead_air_seconds=20.0, max_consecutive_read_chat=3, max_refs_per_turn=3,
              backlog_summary_threshold=12, summary_score_ceiling=15.0)
    kw.update(dir_over)
    director = Director(pool, pulse, _segments(), clock=lambda: clock["t"], **kw)
    runner = FakeRunner()
    loop = DirectorLoop(
        director=director, pool=pool, pulse=pulse, runner=runner,
        emotion=None, autonomy=autonomy, speak=None,
        clock=lambda: clock["t"],
    )
    director.start(now)
    # move past opening so read_chat/ack allowed (main segment)
    director.advance_segment(now)
    director.mark_spoke(DirectorAction.TRANSITION, now)
    return loop, director, pool, pulse, runner, clock


@pytest.mark.asyncio
class TestDirectorLoop:
    async def test_read_pulls_and_removes_from_pool(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("m1", "Mai ơi chơi gì", now=0.0, kind="mention")
        clock["t"] = 1.0
        action = await loop.tick_once()
        assert action == DirectorAction.READ_CHAT
        assert runner.read_calls == ["Mai ơi chơi gì"]
        assert pool.size() == 0   # đã gỡ

    async def test_superchat_acked_before_chat(self) -> None:
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("c", "chat thường", now=0.0, kind="chat")
        pool.add("sc", "quà nè", now=0.0, kind="chat", amount_vnd=500_000, is_super=True)
        clock["t"] = 1.0
        action = await loop.tick_once()
        assert action == DirectorAction.ACK_DONATION
        # ack prompt phải nói tới superchat, chưa đụng chat thường
        assert "superchat" in runner.read_calls[0].lower() or "quà" in runner.read_calls[0]
        assert any(m.msg_id == "c" for m in pool.top_cluster(now=1.0, max_refs=10))

    async def test_ack_uses_viewer_name_not_id(self) -> None:
        # TASK 2: prompt ack gọi tên hiển thị, không channel id
        loop, director, pool, pulse, runner, clock = _make()
        pool.add("sc", "quà nè", now=0.0, kind="chat", viewer_id="UCxq3fZZ",
                 viewer_name="Alice", amount_vnd=500_000, is_super=True)
        clock["t"] = 1.0
        await loop.tick_once()
        assert "Alice" in runner.read_calls[0]
        assert "UCxq3fZZ" not in runner.read_calls[0]

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
        action = await loop.tick_once()
        assert action == DirectorAction.SELF_TALK
        assert runner.ambient_calls == []   # không sinh gì (không material)

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
