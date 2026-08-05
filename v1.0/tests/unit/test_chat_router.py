"""Test ChatRouter — Phase Platform.C (glue InputService → emotion + runner)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent, InputService
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from orchestrator.mood_engine import MoodEngine
from services.emotion.appraisal import AppraisalTable
from services.emotion.classifier import EventClassifier
from services.emotion.modifiers import ModifierEngine
from services.input.chat_router import ChatRouter, _to_emotion_event
from interfaces.animation import MoodState
from services.llm.parser import ParsedResponse

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------- Fakes ----------


class FakeSource(InputService):
    """InputService giả — push event thủ công qua enqueue()."""
    def __init__(self, sid: str = "fake") -> None:
        self.service_id = sid
        self._q: asyncio.Queue[InputEvent] = asyncio.Queue()
        self.started = False
        self.stopped = False

    async def start(self) -> None: self.started = True
    async def stop(self) -> None: self.stopped = True
    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)
    def get_metrics(self) -> dict: return {}

    def enqueue(self, ev: InputEvent) -> None:
        self._q.put_nowait(ev)

    async def event_stream(self):
        while True:
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=0.5)
                yield ev
            except asyncio.TimeoutError:
                # cho phép cancel signal đi qua
                continue


class FakeRunner:
    """LLMTurnRunner giả — record turn calls."""
    def __init__(self, delay_s: float = 0.0, raise_on_call: bool = False,
                 text: str = "Mai reply") -> None:
        self.delay_s = delay_s
        self.raise_on_call = raise_on_call
        self.text = text
        self.calls: list[dict] = []

    async def run_turn(self, request_id, user_text, viewer_id=None,
                       session_id=None, trigger_type=None, event_category=None):
        self.calls.append({
            "request_id": request_id,
            "user_text": user_text,
            "viewer_id": viewer_id,
            "trigger_type": trigger_type,
            "event_category": event_category,
        })
        if self.raise_on_call:
            raise RuntimeError("runner boom")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        parsed = ParsedResponse(text=self.text, mood=MoodState(), raw="", ok=True)
        return parsed, 0


# ---------- Fixtures ----------


@pytest.fixture
def emotion() -> EmotionOrchestrator:
    from orchestrator.config_loader import ConfigLoader
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    return EmotionOrchestrator(
        classifier=EventClassifier.from_loader(loader),
        appraisal=AppraisalTable.from_loader(loader),
        modifiers=ModifierEngine.from_loader(loader, memory=None),
        engine=MoodEngine.from_loader(loader),
        tick_hz=10,
    )


def make_event(text: str, source=EventSource.CHAT_YOUTUBE, user_id="u1",
               event_id: str | None = None, **meta) -> InputEvent:
    return InputEvent(
        event_id=event_id or uuid.uuid4().hex,
        timestamp=datetime.now(),
        source=source,
        user_id=user_id,
        user_name="TestUser",
        content=text,
        metadata=meta or {},
    )


# ---------- Tests ----------


class TestConstruct:
    def test_empty_sources_raises(self, emotion) -> None:
        with pytest.raises(ValueError, match="ít nhất 1 InputService"):
            ChatRouter(sources=[], emotion=emotion, runner=FakeRunner())


class TestLifecycle:
    async def test_start_starts_sources_and_emotion(self, emotion) -> None:
        src = FakeSource()
        r = ChatRouter([src], emotion, FakeRunner())
        await r.start()
        assert src.started is True
        assert r._running is True
        assert len(r._consumers) == 1
        await r.stop()

    async def test_stop_cancels_consumers_and_stops_sources(self, emotion) -> None:
        src = FakeSource()
        r = ChatRouter([src], emotion, FakeRunner())
        await r.start()
        await r.stop()
        assert src.stopped is True
        assert r._running is False
        assert r._consumers == []

    async def test_start_idempotent(self, emotion) -> None:
        src = FakeSource()
        r = ChatRouter([src], emotion, FakeRunner())
        await r.start()
        n = len(r._consumers)
        await r.start()  # không spawn extra consumer
        assert len(r._consumers) == n
        await r.stop()


class TestSingleEventFlow:
    async def test_event_triggers_runner_with_category(self, emotion) -> None:
        src = FakeSource()
        runner = FakeRunner()
        r = ChatRouter([src], emotion, runner)
        await r.start()
        src.enqueue(make_event("Mai giỏi quá đi"))
        # đợi consumer xử lý
        await _wait_for(lambda: len(runner.calls) >= 1, timeout=2.0)
        await r.stop()
        assert runner.calls[0]["user_text"] == "Mai giỏi quá đi"
        assert runner.calls[0]["viewer_id"] == "u1"
        assert runner.calls[0]["event_category"] == "chat_compliment"
        assert runner.calls[0]["trigger_type"] == "chat_youtube"


class TestMultipleSourcesInterleaved:
    async def test_events_from_both_sources_processed(self, emotion) -> None:
        yt = FakeSource("yt")
        dc = FakeSource("dc")
        runner = FakeRunner()
        r = ChatRouter([yt, dc], emotion, runner)
        await r.start()
        yt.enqueue(make_event("từ youtube", source=EventSource.CHAT_YOUTUBE, event_id="yt1"))
        dc.enqueue(make_event("từ discord", source=EventSource.CHAT_DISCORD, event_id="dc1"))
        await _wait_for(lambda: len(runner.calls) >= 2, timeout=2.0)
        await r.stop()
        texts = {c["user_text"] for c in runner.calls}
        assert texts == {"từ youtube", "từ discord"}


class TestSerialization:
    async def test_second_event_waits_for_first_turn(self, emotion) -> None:
        """Runner delay 0.3s → 2 events lần lượt, không đè."""
        src = FakeSource()
        runner = FakeRunner(delay_s=0.3)
        r = ChatRouter([src], emotion, runner)
        await r.start()
        src.enqueue(make_event("first", event_id="1"))
        src.enqueue(make_event("second", event_id="2"))
        # Sau 0.15s (nửa 1 turn): mới 1 turn started
        await asyncio.sleep(0.15)
        assert len(runner.calls) == 1
        # Sau ~0.7s: cả 2 xong
        await _wait_for(lambda: len(runner.calls) >= 2, timeout=2.0)
        await r.stop()
        assert [c["request_id"] for c in runner.calls] == ["1", "2"]  # FIFO


class TestFailSafe:
    async def test_runner_error_does_not_kill_router(self, emotion) -> None:
        """Turn 1 raise → router vẫn xử tiếp turn 2."""
        src = FakeSource()
        runner_err = FakeRunner(raise_on_call=True)
        r = ChatRouter([src], emotion, runner_err)
        await r.start()
        src.enqueue(make_event("dead1", event_id="1"))
        await _wait_for(lambda: r._turns_failed >= 1, timeout=2.0)
        # Router vẫn còn sống, còn nhận event
        assert r._running is True
        src.enqueue(make_event("dead2", event_id="2"))
        await _wait_for(lambda: r._turns_failed >= 2, timeout=2.0)
        await r.stop()
        assert r._turns_failed == 2

    async def test_emotion_error_skips_event_not_crash(self, emotion) -> None:
        """emotion.handle_event raise → skip event, không call runner."""
        # Hack: monkey-patch emotion.handle_event raise
        async def _boom(event): raise RuntimeError("emotion boom")
        emotion.handle_event = _boom

        src = FakeSource()
        runner = FakeRunner()
        r = ChatRouter([src], emotion, runner)
        await r.start()
        src.enqueue(make_event("hi"))
        await asyncio.sleep(0.3)
        await r.stop()
        # runner không được gọi (emotion đã raise trước)
        assert runner.calls == []


class TestSpeakCallback:
    async def test_speak_called_with_parsed_text(self, emotion) -> None:
        src = FakeSource()
        runner = FakeRunner(text="Mai nói câu này")
        spoken: list[tuple[str, str]] = []

        async def speak(req_id, text):
            spoken.append((req_id, text))

        r = ChatRouter([src], emotion, runner, speak=speak)
        await r.start()
        src.enqueue(make_event("hi", event_id="e1"))
        await _wait_for(lambda: len(spoken) >= 1, timeout=2.0)
        await r.stop()
        assert spoken[0] == ("e1", "Mai nói câu này")

    async def test_speak_skipped_when_parse_not_ok(self, emotion) -> None:
        src = FakeSource()

        class BadRunner(FakeRunner):
            async def run_turn(self, request_id, user_text, viewer_id=None,
                               session_id=None, trigger_type=None, event_category=None):
                self.calls.append({"request_id": request_id})
                parsed = ParsedResponse(text="", mood=MoodState(), raw="", ok=False)
                return parsed, 1

        spoken: list = []
        async def speak(req_id, text): spoken.append((req_id, text))

        r = ChatRouter([src], emotion, BadRunner(), speak=speak)
        await r.start()
        src.enqueue(make_event("hi"))
        await asyncio.sleep(0.3)
        await r.stop()
        assert spoken == []


# ---------- Conversion helper ----------


class TestToEmotionEvent:
    def test_normal_chat_becomes_chat_kind(self) -> None:
        ev = make_event("hi")
        emo = _to_emotion_event(ev)
        assert emo.kind.value == "chat"
        assert emo.text == "hi"
        assert emo.meta["viewer_id"] == "u1"
        assert emo.meta["viewer_name"] == "TestUser"

    def test_super_chat_becomes_system_donation(self) -> None:
        ev = make_event("thanks", is_super_chat=True, amount_vnd=100_000)
        emo = _to_emotion_event(ev)
        assert emo.kind.value == "system"
        assert emo.meta["platform_type"] == "donation"
        assert emo.meta["amount_vnd"] == 100_000

    def test_super_chat_without_amount_stays_chat(self) -> None:
        ev = make_event("hi", is_super_chat=True)  # no amount
        emo = _to_emotion_event(ev)
        assert emo.kind.value == "chat"


# ---------- utils ----------


async def _wait_for(cond, timeout: float = 1.0, tick: float = 0.02) -> None:
    """Poll cond() until True hoặc timeout."""
    import time
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if cond():
            return
        await asyncio.sleep(tick)
    raise AssertionError(f"condition không thoả trong {timeout}s")
