from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from scripts.stress_youtube_live_pipeline import PacedYouTubeReplayInputService
from services.director.chat_pulse import ChatPulse
from services.director.salience import SaliencePool
from services.input.chat_router import ChatRouter


def _chat_line(message_id: str, text: str, offset_ms: int) -> str:
    return json.dumps({
        "replayChatItemAction": {
            "videoOffsetTimeMsec": str(offset_ms),
            "actions": [{
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "id": message_id,
                            "authorName": {"simpleText": message_id},
                            "message": {"runs": [{"text": text}]},
                        }
                    }
                }
            }],
        }
    })


class _Emotion:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def handle_event(self, _event):
        return SimpleNamespace(category="neutral")

    def get_metrics(self) -> dict:
        return {}


class _Runner:
    pass


async def test_chat_intake_continues_while_turn_lock_is_blocked(
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "paced.live_chat.json"
    source_file.write_text("\n".join([
        _chat_line("one", "alpha", 0),
        _chat_line("two", "beta?", 20),
        _chat_line("three", "Mai gamma", 40),
    ]), encoding="utf-8")
    source = PacedYouTubeReplayInputService(
        source_file,
        base_time=datetime.now(timezone.utc),
        burst_window_ms=10,
        replay_speed=1.0,
    )
    pool = SaliencePool(
        base_tier={"chat": 10, "question": 25, "mention": 35},
        pool_max=10,
        floor=1,
    )
    pulse = ChatPulse(
        window_seconds=60,
        tempo_low_per_min=2,
        tempo_high_per_min=15,
        diversity_threshold=0.4,
        cold_silence_seconds=90,
    )
    turn_lock = asyncio.Lock()
    await turn_lock.acquire()
    router = ChatRouter(
        [source],
        _Emotion(),  # type: ignore[arg-type]
        _Runner(),  # type: ignore[arg-type]
        pool=pool,
        pulse=pulse,
        turn_lock=turn_lock,
    )
    await router.start()
    try:
        await asyncio.wait_for(source.completed.wait(), timeout=1.0)
        assert turn_lock.locked() is True
        assert router.get_metrics()["router_events_received"] == 3
        assert pool.get_metrics()["salience_added"] == 3
        assert pool.size() == 3
    finally:
        turn_lock.release()
        await router.stop()
