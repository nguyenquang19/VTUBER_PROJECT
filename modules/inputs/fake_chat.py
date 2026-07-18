"""
fake_chat.py — Bơm tin nhắn giả theo kịch bản để test khung (thay Discord thật).
Cho phép kiểm tra các tình huống: im lặng dài (tự nói), chat thường,
chat dồn dập, và tin quan trọng (barge-in).
"""
from __future__ import annotations
import asyncio

from core.bus import EventBus
from core.events import ChatMessage, Event, EventType


async def run_scenario(bus: EventBus) -> None:
    """Một kịch bản test kéo ~30s để quan sát hành vi."""

    def send(user_id, user_name, text, is_command=False, priority=0):
        msg = ChatMessage(
            user_id=user_id, user_name=user_name, text=text,
            is_command=is_command, priority_hint=priority,
        )
        bus.publish(Event(type=EventType.CHAT_INCOMING, payload=msg))

    # 0-6s: IM LẶNG hoàn toàn -> AI phải TỰ NÓI (monologue)
    print("\n=== [0s] Im lặng — AI phải tự tám chuyện ===")
    await asyncio.sleep(6)

    # 6s: một tin thường
    print("\n=== [6s] Một tin chat thường ===")
    send("u1", "Minh", "hôm nay trời đẹp ha")
    await asyncio.sleep(5)

    # 11s: câu hỏi (điểm cao hơn)
    print("\n=== [11s] Một câu hỏi ===")
    send("u2", "Lan", "bạn thích ăn gì nhất?")
    await asyncio.sleep(5)

    # 16s: chat dồn dập
    print("\n=== [16s] Chat dồn dập 4 tin ===")
    send("u3", "An", "haha")
    send("u4", "Bình", "vui thế")
    send("u1", "Minh", "kể chuyện đi")
    send("u5", "Cường", "chào mọi người")
    await asyncio.sleep(6)

    # 22s: tin QUAN TRỌNG giữa lúc đang nói -> BARGE-IN
    print("\n=== [22s] Tin quan trọng (lệnh mod) — kỳ vọng BARGE-IN ===")
    send("mod1", "ModKhoa", "!announce có donate mới", is_command=True, priority=50)
    await asyncio.sleep(6)

    # 28s: im lại -> về mạch tự nói
    print("\n=== [28s] Im lại — AI về mạch tự nói ===")
    await asyncio.sleep(6)

    print("\n=== Kết thúc kịch bản ===")
