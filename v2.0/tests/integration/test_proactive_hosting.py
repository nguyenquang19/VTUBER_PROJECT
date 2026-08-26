from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from interfaces.state import GoalSnapshot
from interfaces.state import AgentStateSnapshot, OpenThread, ThreadEvidence, ThreadKind
from services.director.chat_pulse import ChatPulse
from services.director.director import Director, DirectorAction, Segment
from services.director.director_loop import DirectorLoop
from services.director.proactive_policy import ProactiveHostingPolicy, ProactivePolicyConfig
from services.director.salience import SaliencePool
from services.tts.tts_pipeline import TTSDeliveryMode, TTSDeliveryResult

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


@dataclass
class _Parsed:
    text: str = "grounded continuation"
    ok: bool = True


class _Runner:
    session_id = "m5-session"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_ambient_turn(self, request_id, prompt_text):
        self.prompts.append(prompt_text)
        return _Parsed()

    def commit_self_talk(self, text):
        return None


class _Urge:
    def should_speak_now(self):
        return False


class _Autonomy:
    urge = _Urge()

    def __init__(self) -> None:
        self.calls = []

    def force_generate_for(self, category, mood, ctx):
        self.calls.append((category, list(ctx.working_memory_recent), ctx.environment_summary))
        return type("Decision", (), {"prompt_text": f"grounded:{category}"})()

    def force_generate(self, mood, ctx):
        raise AssertionError("grounded proactive choice must not use random category")

    def check_dedup(self, text):
        return False

    def on_self_spoke(self, text):
        return None


class _State:
    def __init__(self, snapshot: AgentStateSnapshot) -> None:
        self.value = snapshot
        self.events = []

    def snapshot(self):
        return self.value

    def record(self, event):
        self.events.append(event)
        return True


class _Goals:
    def snapshot(self):
        return GoalSnapshot()


async def _delivered_speech(_text: str, _request_id: str) -> TTSDeliveryResult:
    return TTSDeliveryResult(
        request_id=_request_id,
        delivered=True,
        mode=TTSDeliveryMode.SUBTITLE,
        sentences_total=1,
        sentences_delivered=1,
        audio_sentences=0,
        subtitle_sentences=1,
        failed_sentences=0,
        cancelled=False,
    )


async def test_director_continues_grounded_thread_before_silence_and_cools_down() -> None:
    thread = OpenThread(
        "thread-1", "coffee story", "unfinished coffee story", NOW, NOW,
        NOW + timedelta(minutes=5), kind=ThreadKind.STORY,
        evidence=(ThreadEvidence("event-1", "coffee excerpt", "rule"),),
    )
    state = _State(AgentStateSnapshot(open_threads=(thread,)))
    pool = SaliencePool(base_tier={"chat": 10}, floor=1)
    pulse = ChatPulse(cold_silence_seconds=90)
    policy = ProactiveHostingPolicy(ProactivePolicyConfig(source_cooldown_seconds=90))
    director = Director(
        pool, pulse, [Segment("main", "main", 300, {"follow_up", "self_talk"})],
        proactive_policy=policy,
    )
    director.start(100.0)
    autonomy = _Autonomy()
    clock = {"now": 101.0}
    loop = DirectorLoop(
        director, pool, pulse, _Runner(), autonomy=autonomy,
        agent_state=state, goal_manager=_Goals(), clock=lambda: clock["now"],
        speak=_delivered_speech,
    )

    assert await loop.tick_once() is DirectorAction.FOLLOW_UP
    assert autonomy.calls == [("follow_up_topic", ["unfinished coffee story"], None)]
    completed = next(event for event in state.events if event.kind.value == "speech_completed")
    assert completed.payload["action"] == "follow_up"

    clock["now"] = 102.0
    assert await loop.tick_once() is DirectorAction.WAIT


async def test_director_uses_only_grounded_salient_environment() -> None:
    state = _State(AgentStateSnapshot(environment_summary={
        "salient": True,
        "source_event_id": "env-scene-1",
        "summary": "OBS switched to the game scene",
    }))
    pool = SaliencePool(base_tier={"chat": 10}, floor=1)
    pulse = ChatPulse(cold_silence_seconds=90)
    director = Director(
        pool, pulse, [Segment("main", "main", 300, {"self_talk"})],
        proactive_policy=ProactiveHostingPolicy(ProactivePolicyConfig()),
    )
    director.start(100.0)
    autonomy = _Autonomy()
    loop = DirectorLoop(
        director, pool, pulse, _Runner(), autonomy=autonomy,
        agent_state=state, goal_manager=_Goals(), clock=lambda: 101.0,
        speak=_delivered_speech,
    )
    assert await loop.tick_once() is DirectorAction.SELF_TALK
    assert autonomy.calls == [(
        "environment_reaction", [], "OBS switched to the game scene",
    )]
