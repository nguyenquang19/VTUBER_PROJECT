"""
main.py — Chạy demo khung V1 (IT-1) với MOCK.

Chạy:  python main.py

Bạn sẽ thấy trên console:
  - AI TỰ NÓI khi im lặng (monologue engine)
  - AI trả lời chat khi có tin đáng trả lời
  - AI bị NGẮT (barge-in) khi có tin quan trọng
  - AI QUAY VỀ MẠCH tự nói sau khi xử lý xong

Khi có model thật: thay MockLLM -> RealLLM, MockTTS -> RealTTS,
và thay fake_chat -> Discord bot. Orchestrator/monologue KHÔNG đổi.
"""
from __future__ import annotations
import asyncio
import logging

from core.bus import EventBus
from core.events import EventType, Event
from core.monologue import MonologueEngine
from core.orchestrator import Orchestrator
from core.persona import Persona, build_monologue_inputs
from modules.mocks import MockLLM, MockTTS
from modules.inputs.fake_chat import run_scenario


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)


async def main() -> None:
    bus = EventBus()

    # Nạp persona thật từ file (Core bất biến + Evolving tiến hóa)
    persona = Persona.load(
        "config/persona_core.yaml",
        "config/persona_evolving.yaml",
    )
    interests, seeds = build_monologue_inputs(persona)
    print(f"Nhân vật: {persona.core['identity']['name']} — đã nạp "
          f"{len(interests)} sở thích, {len(seeds)} topic tự nói.\n")

    monologue = MonologueEngine(
        persona_interests=interests,
        seed_topics=seeds,
        min_develop=2,
        max_develop=4,
    )

    llm = MockLLM()
    tts = MockTTS()

    orch = Orchestrator(
        bus=bus,
        monologue=monologue,
        llm=llm,
        tts=tts,
        tick_interval=1.2,
        silence_before_monologue=3.0,
    )

    # Log gọn khi AI bắt đầu/kết thúc nói (để quan sát nhịp)
    async def on_start(ev: Event):
        src = ev.payload.source.value if ev.payload else "?"
        print(f"── AI bắt đầu nói ({src}) ──")

    bus.subscribe(EventType.TTS_SPEAK_START, on_start)

    # Chạy orchestrator + kịch bản chat song song
    orch_task = asyncio.create_task(orch.run())
    await run_scenario(bus)

    orch.stop()
    orch_task.cancel()
    await bus.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
