"""
discord_bot.py — [BƯỚC C] Nhận chat Discord thật (thay fake_chat).

CHƯA HOÀN THIỆN — stub. Sẽ code đầy đủ ở Bước C.

Nhiệm vụ: lắng nghe message Discord -> chuẩn hoá -> publish CHAT_INCOMING.

Ghi chú triển khai (Bước C) — LỖI HAY GẶP:
  - BẮT BUỘC bật "Message Content Intent" trong Discord Developer Portal,
    nếu không bot sẽ "câm" (nhận event nhưng content rỗng).
  - Chuẩn hoá: bỏ mention/link thừa, cắt độ dài tối đa, nhận diện lệnh (!skip...).
  - Dùng user_id (KHÔNG dùng tên hiển thị) làm khoá định danh cho memory.
  - Auto-reconnect + backoff khi mất mạng (không để crash).
  - Token đọc từ .env (KHÔNG hardcode, KHÔNG commit).
"""
from __future__ import annotations
from core.bus import EventBus
from core.events import ChatMessage, Event, EventType


class DiscordInput:
    def __init__(self, bus: EventBus, token: str, channel_ids: list[str]) -> None:
        self.bus = bus
        self.token = token
        self.channel_ids = channel_ids
        # TODO(BƯỚC C): khởi tạo discord.Client với intents.message_content = True

    def _emit(self, user_id, user_name, text, is_command=False):
        """Chuẩn hoá 1 message rồi đẩy lên bus (dùng chung với fake_chat)."""
        msg = ChatMessage(
            user_id=str(user_id), user_name=user_name,
            text=text, is_command=is_command,
        )
        self.bus.publish(Event(type=EventType.CHAT_INCOMING, payload=msg))

    async def start(self) -> None:
        # TODO(BƯỚC C): chạy discord client; trong on_message gọi self._emit(...)
        raise NotImplementedError("Sẽ hoàn thiện ở Bước C")
