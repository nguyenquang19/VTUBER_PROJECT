"""Offline YouTube simulation from pytchat-shaped messages to delivery.

No network, credential, real video ID, llama.cpp, or audio device is used.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from interfaces.animation import MoodState
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from services.input.chat_router import ChatRouter
from services.input.youtube_chat import YouTubeChatService
from services.llm.parser import ParsedResponse
from orchestrator.config_loader import ConfigLoader
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from services.director.director_loop import DirectorLoop
from services.director.salience import SaliencePool


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakePytchat:
    def __init__(self, batches: list[list[object]]) -> None:
        self._batches = list(batches)
        self._alive = True
        self.terminated = False

    def is_alive(self) -> bool:
        return self._alive

    def get(self):
        if not self._batches:
            self._alive = False
            return None
        return SimpleNamespace(items=self._batches.pop(0))

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False


class _Emotion:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def handle_event(self, event):
        category = (
            "system_donation"
            if event.meta.get("platform_type") == "donation"
            else "chat_message"
        )
        return SimpleNamespace(category=category)

    def get_metrics(self) -> dict[str, object]:
        return {}


class _Runner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_turn(
        self,
        request_id,
        user_text,
        viewer_id=None,
        session_id=None,
        trigger_type=None,
        event_category=None,
        **kwargs,
    ):
        self.calls.append({
            "request_id": request_id,
            "user_text": user_text,
            "viewer_id": viewer_id,
            "trigger_type": trigger_type,
            "event_category": event_category,
        })
        canned = user_text == "force fallback"
        return ParsedResponse(
            text="Câu dự phòng" if canned else f"Mai: {user_text}",
            mood=MoodState(),
            ok=not canned,
            raw="<canned>" if canned else user_text,
        ), 1 if canned else 0

    async def run_ambient_turn(self, request_id, prompt, **kwargs):
        self.calls.append({
            "request_id": request_id,
            "user_text": prompt,
            "trigger_type": "ambient",
        })
        return ParsedResponse(
            text="Mai phản ứng cả phòng chat",
            mood=MoodState(),
            ok=True,
            raw=prompt,
        )

    def commit_self_talk(self, text: str) -> None:
        return None


def _message(
    text: str,
    *,
    message_id: str,
    viewer_id: str = "viewer-1",
    viewer_name: str = "Viewer",
    amount: int | None = None,
    timestamp: str = "2026-08-11 10:00:00+00:00",
):
    return SimpleNamespace(
        id=message_id,
        message=text,
        datetime=timestamp,
        author=SimpleNamespace(channelId=viewer_id, name=viewer_name),
        amountValue=amount,
    )


async def _simulate(messages: list[object], expected_turns: int):
    client = _FakePytchat([messages])
    source = YouTubeChatService(
        video_id="offline-video",
        poll_interval_s=0.001,
        chat_client=client,
    )
    runner = _Runner()
    deliveries: list[tuple[str, str]] = []

    async def speak(request_id: str, text: str) -> TTSDeliveryResult:
        deliveries.append((request_id, text))
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    router = ChatRouter([source], _Emotion(), runner, speak=speak)
    await router.start()
    try:
        await _wait_for(lambda: len(runner.calls) >= expected_turns)
    finally:
        await router.stop()
    return client, runner, deliveries, source.get_metrics(), router.get_metrics()


async def _wait_for(predicate, timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("YouTube simulation did not finish before timeout")


async def test_normal_and_superchat_keep_order_and_metadata() -> None:
    client, runner, deliveries, source_metrics, router_metrics = await _simulate([
        _message("xin chào Mai", message_id="yt-1", viewer_id="u-1"),
        _message("quà cho Mai", message_id="yt-2", viewer_id="u-2", amount=50_000),
    ], expected_turns=2)

    assert [call["request_id"] for call in runner.calls] == ["yt-1", "yt-2"]
    assert [call["trigger_type"] for call in runner.calls] == [
        "chat_youtube", "chat_youtube",
    ]
    assert [call["event_category"] for call in runner.calls] == [
        "chat_message", "system_donation",
    ]
    assert [request_id for request_id, _ in deliveries] == ["yt-1", "yt-2"]
    assert source_metrics["youtube_events_total"] == 2
    assert router_metrics["router_turns_run"] == 2
    assert client.terminated is True


async def test_malformed_and_blank_messages_do_not_kill_the_stream() -> None:
    _, runner, deliveries, source_metrics, _ = await _simulate([
        SimpleNamespace(),
        _message("   ", message_id="blank"),
        _message("tin hợp lệ", message_id="valid"),
    ], expected_turns=1)

    assert [call["request_id"] for call in runner.calls] == ["valid"]
    assert deliveries == [("valid", "Mai: tin hợp lệ")]
    assert source_metrics["youtube_events_total"] == 1


async def test_youtube_canned_fallback_still_reaches_delivery() -> None:
    _, runner, deliveries, _, router_metrics = await _simulate([
        _message("force fallback", message_id="fallback"),
    ], expected_turns=1)

    assert runner.calls[0]["request_id"] == "fallback"
    assert deliveries == [("fallback", "Câu dự phòng")]
    assert router_metrics["router_speak_calls"] == 1


async def test_youtube_director_intake_promotes_question_without_mark() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    pool = SaliencePool.from_loader(loader)
    pulse = ChatPulse.from_loader(loader)
    source = YouTubeChatService(
        video_id="offline-video",
        poll_interval_s=0.001,
        chat_client=_FakePytchat([[
            _message("xin chào", message_id="normal"),
            _message("em có nên tập trung học tiếp", message_id="question"),
        ]]),
    )
    router = ChatRouter(
        [source], _Emotion(), _Runner(), pool=pool, pulse=pulse,
    )
    await router.start()
    try:
        await _wait_for(lambda: pool.get_metrics()["salience_added"] == 2)
    finally:
        await router.stop()

    director = Director.from_loader(
        pool, pulse, loader, clock=lambda: 0.0, chat_gate_enabled=True,
    )
    director.start(now=0.0)
    decision = director.decide(now=0.0)

    assert decision.action is DirectorAction.READ_CHAT
    assert decision.refs[0].msg_id == "question"
    assert decision.refs[0].kind == "question"


_RANDOM_SEEDS = tuple(range(20))
_BURSTS_PER_SEED = 50
_BURST_MAX_MESSAGES = 75

_NORMAL_TEXTS = (
    "xin chào", "game này căng quá", "đi ngủ thôi", "hay đấy",
    "lag rồi", "12 giờ rồi", "cố lên", "vừa sập live",
)
_QUESTION_TEXTS = (
    "chị chơi game gì?", "bao giờ stream tiếp", "tại sao lại thua vậy",
    "em có nên tập trung học tiếp", "được không chị", "ở đâu thế",
)
_MENTION_TEXTS = (
    "Mai ơi đọc chat em", "chị Mai xem cái này", "Mai à hôm nay vui không?",
)
_HYPE_TEXTS = ("W", "haha", "game về nước rồi", "đỉnh quá")
_NOISE_TEXTS = ("=)))", "🔥🔥🔥", "...", "ㅋㅋㅋ")


class _ConcurrentEmotion(_Emotion):
    async def handle_event(self, event):
        # Buộc các coroutine intake nhường event loop để burst thực sự interleave.
        await asyncio.sleep(0)
        return await super().handle_event(event)


class _BurstSource:
    service_id = "youtube_random_burst"


def _random_youtube_burst(
    rng: random.Random,
    *,
    seed: int,
    burst_index: int,
    timestamp: datetime,
) -> list[object]:
    count = rng.randint(0, _BURST_MAX_MESSAGES)
    viewer_count = rng.randint(1, max(1, min(count, 40)))
    timestamp_text = timestamp.isoformat(sep=" ")
    raw: list[object] = []
    for index in range(count):
        category = rng.choices(
            ("normal", "question", "mention", "hype", "noise", "donation"),
            weights=(52, 18, 8, 12, 7, 3),
            k=1,
        )[0]
        amount = None
        if category == "normal":
            text = rng.choice(_NORMAL_TEXTS)
        elif category == "question":
            text = rng.choice(_QUESTION_TEXTS)
        elif category == "mention":
            text = rng.choice(_MENTION_TEXTS)
        elif category == "hype":
            text = rng.choice(_HYPE_TEXTS)
        elif category == "noise":
            text = rng.choice(_NOISE_TEXTS)
        else:
            text = rng.choice(("quà cho Mai", "ủng hộ chị", "superchat nè"))
            amount = rng.choice((10_000, 20_000, 50_000, 100_000, 500_000))
        viewer = rng.randrange(viewer_count)
        raw.append(_message(
            text,
            message_id=f"seed{seed}-burst{burst_index}-msg{index}",
            viewer_id=f"viewer-{viewer}",
            viewer_name=f"Viewer {viewer}",
            amount=amount,
            timestamp=timestamp_text,
        ))
    return raw


async def _run_random_youtube_live(seed: int) -> tuple[tuple[object, ...], int]:
    rng = random.Random(seed)
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    pool = SaliencePool.from_loader(loader)
    pulse = ChatPulse.from_loader(loader)
    runner = _Runner()
    router = ChatRouter(
        [_BurstSource()], _ConcurrentEmotion(), runner, pool=pool, pulse=pulse,
    )
    clock = {"now": datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc).timestamp()}
    segment = Segment(
        "main", "randomized YouTube soak", 1_000_000.0,
        {"read_chat", "ack_donation", "self_talk"},
    )
    director = Director(
        pool, pulse, [segment],
        dead_air_seconds=20.0,
        max_consecutive_read_chat=3,
        max_refs_per_turn=3,
        backlog_summary_threshold=12,
        summary_score_ceiling=15.0,
        min_actionable_score=15.0,
        chat_gate_enabled=True,
        clock=lambda: clock["now"],
    )
    director.start(clock["now"])

    async def deliver(request_id: str, _text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    loop = DirectorLoop(
        director, pool, pulse, runner,
        clock=lambda: clock["now"],
        speak=deliver,
    )

    trace: list[object] = []
    valid_total = 0
    current = datetime.fromtimestamp(clock["now"], tz=timezone.utc)
    for burst_index in range(_BURSTS_PER_SEED):
        current += timedelta(seconds=rng.uniform(0.05, 6.0))
        clock["now"] = current.timestamp()
        raw = _random_youtube_burst(
            rng, seed=seed, burst_index=burst_index, timestamp=current,
        )
        events = [
            event for item in raw
            if (event := YouTubeChatService._to_event(item)) is not None
        ]
        assert len({event.timestamp for event in events}) <= 1, (
            f"seed={seed} burst={burst_index}: timestamp không đồng thời"
        )
        await asyncio.gather(*(router._process(event) for event in events))
        valid_total += len(events)

        pool.evict_stale(clock["now"])
        ranked = pool.top_cluster(clock["now"], max_refs=3)
        expected = director.decide(now=clock["now"])
        if any(item.is_super for item in ranked):
            assert expected.action is DirectorAction.ACK_DONATION, (
                f"seed={seed} burst={burst_index}: donation không thắng"
            )
        if expected.refs:
            assert ranked and expected.refs[0].msg_id == ranked[0].msg_id, (
                f"seed={seed} burst={burst_index}: không chọn top score"
            )

        calls_before = len(runner.calls)
        action = await loop.tick_once()
        calls_delta = len(runner.calls) - calls_before
        assert action is expected.action, f"seed={seed} burst={burst_index}"
        assert calls_delta <= 1, (
            f"seed={seed} burst={burst_index}: {calls_delta} turn trong một tick"
        )
        assert pool.size() <= 50, f"seed={seed} burst={burst_index}: pool overflow"
        assert pool.get_metrics()["salience_added"] == valid_total
        trace.append((
            len(events), action.value, expected.reason,
            tuple(ref.msg_id for ref in expected.refs), pool.size(), calls_delta,
        ))
    return tuple(trace), valid_total


async def test_many_random_simultaneous_youtube_bursts_are_bounded_and_ranked() -> None:
    total_events = 0
    for seed in _RANDOM_SEEDS:
        _trace, events = await _run_random_youtube_live(seed)
        total_events += events

    # 20 seed × 50 burst = 1.000 lần Director đối mặt một batch YouTube random.
    assert len(_RANDOM_SEEDS) * _BURSTS_PER_SEED == 1_000
    assert total_events >= 20_000


async def test_random_youtube_burst_trace_is_reproducible() -> None:
    first, first_events = await _run_random_youtube_live(20260811)
    second, second_events = await _run_random_youtube_live(20260811)

    assert first_events == second_events
    assert first == second
