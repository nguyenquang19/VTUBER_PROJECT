"""
test_barge_in.py — Test barge-in: tin quan trọng cắt ngang câu đang nói,
rồi xử lý tin đó, rồi về mạch. Viết theo chuẩn pytest (có assert).

Chạy:  pytest tests/test_barge_in.py -v
Hoặc chạy trực tiếp:  python -m tests.test_barge_in  (in log quan sát)
"""
from __future__ import annotations
import asyncio
import pytest

from core.bus import EventBus
from core.events import ChatMessage, Event, EventType, SpeakRequest
from core.monologue import MonologueEngine
from core.orchestrator import Orchestrator


class SlowLLM:
    """LLM giả: monologue dài; nếu urgent -> câu đánh dấu rõ."""
    async def generate(self, req: SpeakRequest):
        if req.context.get("urgent"):
            yield "URGENT_HANDLED"
            return
        for i in range(5):
            await asyncio.sleep(0.1)
            yield f"monologue cau {i+1}"


class RecordingTTS:
    """TTS giả: ghi lại câu đã phát + có thể bị stop giữa chừng."""
    def __init__(self):
        self.spoken: list[str] = []
        self._stopped = False
    async def speak(self, text: str):
        self._stopped = False
        self.spoken.append(text)
        elapsed = 0.0
        while elapsed < 1.0 and not self._stopped:
            await asyncio.sleep(0.05); elapsed += 0.05
    async def stop(self):
        self._stopped = True


async def _run_barge_in_scenario():
    bus = EventBus()
    mono = MonologueEngine(persona_interests=["test"])
    tts = RecordingTTS()
    orch = Orchestrator(bus, mono, SlowLLM(), tts,
                        tick_interval=0.3, silence_before_monologue=0.5)

    barge_events: list = []
    urgent_handled = {"v": False}

    async def on_barge(ev: Event):
        barge_events.append(ev)
    bus.subscribe(EventType.BARGE_IN, on_barge)

    orch_task = asyncio.create_task(orch.run())
    await asyncio.sleep(1.5)

    bus.publish(Event(type=EventType.CHAT_INCOMING, payload=ChatMessage(
        user_id="mod", user_name="Mod", text="!urgent",
        is_command=True, priority_hint=50)))

    await asyncio.sleep(2.5)

    if "URGENT_HANDLED" in tts.spoken:
        urgent_handled["v"] = True

    orch.stop()
    orch_task.cancel()
    await bus.close()
    return barge_events, urgent_handled["v"], tts.spoken


@pytest.mark.asyncio
async def test_barge_in_fires():
    """Barge-in event phải được phát khi tin quan trọng chen vào lúc đang nói."""
    barge_events, urgent_handled, spoken = await _run_barge_in_scenario()
    assert len(barge_events) >= 1, "Barge-in khong kich hoat"


@pytest.mark.asyncio
async def test_urgent_handled_after_barge_in():
    """Sau khi ngắt lời, tin urgent phải được xử lý (không bị bỏ quên)."""
    barge_events, urgent_handled, spoken = await _run_barge_in_scenario()
    assert urgent_handled, f"Tin urgent bi bo quen. Da phat: {spoken}"


@pytest.mark.asyncio
async def test_monologue_before_interrupt():
    """Trước khi bị ngắt, AI phải đã nói monologue (mạch tự nói chạy)."""
    barge_events, urgent_handled, spoken = await _run_barge_in_scenario()
    assert any("monologue" in s for s in spoken), "Monologue khong chay"


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s.%(msecs)03d [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    be, uh, sp = asyncio.run(_run_barge_in_scenario())
    print("\n=== KET QUA ===")
    print("Barge-in events:", len(be), "->", "PASS" if be else "FAIL")
    print("Urgent handled :", "PASS" if uh else "FAIL")
    print("Da phat:", sp)
