from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dashboard.dashboard_server import DashboardServer
from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent, InputService
from interfaces.llm import LLMToken
from orchestrator.config_loader import ConfigLoader
from orchestrator.emotion_orchestrator import EmotionOrchestrator
from orchestrator.fallback_manager import FallbackManager
from orchestrator.metrics_collector import MetricsCollector
from orchestrator.stream_runtime import StreamRuntime, StreamRuntimeConfig
from services.agent.agent_state import AgentState
from services.agent.event_ledger import EventLedger
from services.agent.types import AgentEventKind
from services.input.chat_router import ChatRouter
from services.llm.canned_response import CannedResponder
from services.llm.llm_turn import LLMTurnRunner
from services.llm.parser import ParsedResponse
from services.llm.prompt_cache import PromptCache
from services.llm.prompt_manager import PromptManager

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_RESPONSE = [
    "Tớ thích cà phê sữa.",
    "\n[vui:3 buon:0 buc:0 bon_chon:0 nguong:0]",
]


class FakeSource(InputService):
    service_id = "input_fake"

    def __init__(self) -> None:
        self.queue: asyncio.Queue[InputEvent] = asyncio.Queue()
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {}

    async def event_stream(self):
        while self.running:
            yield await self.queue.get()


class FakeLLM:
    async def generate_stream(self, request):
        for token in VALID_RESPONSE:
            yield LLMToken(request_id=request.request_id, token=token, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)


class ProbeService:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _loader() -> ConfigLoader:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    return loader


def _state(loader: ConfigLoader, metrics: MetricsCollector | None = None) -> AgentState:
    ledger = EventLedger.from_loader(loader, metrics=metrics)
    return AgentState.from_loader(loader, ledger)


def _runner(agent_state: Any, emotion: EmotionOrchestrator) -> LLMTurnRunner:
    return LLMTurnRunner(
        FakeLLM(),
        PromptManager(PromptCache("persona test"), max_history_turns=4),
        FallbackManager(),
        CannedResponder({"default": ["fallback"]}, rng=random.Random(0)),
        emotion=emotion,
        agent_state=agent_state,
        session_id="session-m1",
    )


def _chat(event_id: str, text: str) -> InputEvent:
    return InputEvent(
        event_id=event_id,
        timestamp=datetime.now(timezone.utc),
        source=EventSource.CHAT_YOUTUBE,
        user_id="raw-viewer-id",
        user_name="Alice",
        content=text,
        metadata={},
    )


async def _wait_for(predicate, timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_chat_reply_follow_up_has_grounded_state_without_new_fact() -> None:
    loader = _loader()
    metrics = MetricsCollector()
    state = _state(loader, metrics)
    emotion = EmotionOrchestrator.from_loader(loader, agent_state=state)
    runner = _runner(state, emotion)
    source = FakeSource()
    router = ChatRouter([source], emotion, runner, agent_state=state)

    await state.start()
    await router.start()
    await source.queue.put(_chat("chat-1", "Mai thích cà phê nào?"))
    await _wait_for(lambda: len(runner._pm.history()) >= 2)
    await source.queue.put(_chat("chat-2", "Thế còn uống nóng?"))
    await _wait_for(lambda: len(runner._pm.history()) >= 4)
    await router.stop()

    snapshot = state.snapshot()
    await state.stop()
    kinds = [event.kind for event in snapshot.recent_events]
    assert snapshot.current_topic is not None
    assert snapshot.current_topic.summary == "Mai thích cà phê nào?"
    assert snapshot.last_spoken_summary == "Tớ thích cà phê sữa."
    assert kinds.count(AgentEventKind.CHAT_RECEIVED) == 2
    assert kinds.count(AgentEventKind.EMOTION_APPLIED) == 2
    assert kinds.count(AgentEventKind.SPEECH_FINAL) == 2
    assert all(event.provenance.producer for event in snapshot.recent_events)
    rendered = str(snapshot.to_dict())
    assert "raw-viewer-id" not in rendered
    assert "espresso" not in rendered
    assert metrics.agent_snapshot()["accepted_total"] == 6


async def test_agent_state_failure_does_not_kill_turn() -> None:
    class BrokenState:
        def record(self, event) -> bool:
            raise RuntimeError("state unavailable")

    class FakeRunner:
        session_id = "broken-state-session"

        def __init__(self) -> None:
            self.calls = 0

        async def run_turn(self, **kwargs):
            self.calls += 1
            return ParsedResponse(text="vẫn trả lời", ok=True, raw=""), 0

    loader = _loader()
    broken = BrokenState()
    emotion = EmotionOrchestrator.from_loader(loader, agent_state=broken)
    runner = FakeRunner()
    source = FakeSource()
    router = ChatRouter([source], emotion, runner, agent_state=broken)
    await router.start()
    await source.queue.put(_chat("chat-safe", "Mai ơi?"))
    await _wait_for(lambda: runner.calls == 1)
    assert router._running is True
    await router.stop()


async def test_donation_is_grounded_as_donation_not_plain_chat() -> None:
    loader = _loader()
    state = _state(loader)
    emotion = EmotionOrchestrator.from_loader(loader, agent_state=state)
    runner = _runner(state, emotion)
    source = FakeSource()
    router = ChatRouter([source], emotion, runner, agent_state=state)
    await state.start()
    await router.start()
    donation = _chat("donation-1", "quà nè")
    donation.metadata.update({"is_super_chat": True, "amount_vnd": 100_000})
    await source.queue.put(donation)
    await _wait_for(lambda: state.snapshot().last_spoken_summary is not None)
    snapshot = state.snapshot()
    await router.stop()
    await state.stop()
    donations = [
        event for event in snapshot.recent_events
        if event.kind is AgentEventKind.DONATION_RECEIVED
    ]
    assert len(donations) == 1
    assert donations[0].payload["amount_vnd"] == 100_000


async def test_dashboard_snapshot_is_detached_and_read_only() -> None:
    loader = _loader()
    state = _state(loader)
    emotion = EmotionOrchestrator.from_loader(loader, agent_state=state)
    runner = _runner(state, emotion)
    source = FakeSource()
    router = ChatRouter([source], emotion, runner, agent_state=state)
    await state.start()
    await router.start()
    await source.queue.put(_chat("chat-dashboard", "Nói về cà phê nhé"))
    await _wait_for(lambda: state.snapshot().last_spoken_summary is not None)

    server = DashboardServer(agent_state=state)
    dashboard = await server.build_snapshot()
    dashboard["agent"]["recent_events"][0]["payload"]["category"] = "invented"
    assert "invented" not in str(state.snapshot().to_dict())
    await router.stop()
    await state.stop()


async def test_runtime_records_grounded_environment_and_owns_shared_state() -> None:
    loader = _loader()
    state = _state(loader)
    router = ProbeService()
    router._sources = []
    llm = ProbeService()
    runner = type("Runner", (), {"session_id": "runtime-session", "filter_enabled": False})()
    runtime = StreamRuntime(
        loader=loader,
        llm_svc=llm,
        runner=runner,
        emotion=object(),
        chat_router=router,
        autonomy=None,
        metrics=MetricsCollector(),
        agent_state=state,
        cfg=StreamRuntimeConfig(enable_autonomy=False),
    )
    await runtime.start()
    assert runtime.agent_state is state
    snapshot = state.snapshot()
    assert snapshot.to_dict()["environment_summary"] == {
        "source_services": [],
        "tts_enabled": False,
        "memory_enabled": False,
        "autonomy_enabled": False,
        "dashboard_enabled": False,
    }
    assert snapshot.recent_events[-1].kind is AgentEventKind.ENVIRONMENT_OBSERVED
    await runtime.stop()
