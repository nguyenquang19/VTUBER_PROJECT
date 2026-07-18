"""
events.py — Định nghĩa các loại sự kiện (event) chảy qua hệ thống.

Mọi khối (Discord, Orchestrator, LLM, TTS, Avatar) giao tiếp bằng cách
publish/subscribe các event này qua EventBus. Đây là "ngôn ngữ chung"
của toàn hệ thống — chốt kỹ ở đây thì các khối ghép vào không lệch nhau.

V1: chạy in-process (asyncio). Khi tách nhiều máy (V2) chỉ cần thay
EventBus bằng bản Redis, các event giữ nguyên -> không phải viết lại logic.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class EventType(str, Enum):
    # --- Input đến từ khán giả ---
    CHAT_INCOMING = "chat.incoming"        # có tin nhắn Discord mới

    # --- Quyết định của orchestrator ---
    SPEAK_REQUEST = "speak.request"        # yêu cầu AI nói (tự nói HOẶC trả lời chat)

    # --- Luồng sinh lời của LLM ---
    LLM_SENTENCE = "llm.sentence"          # LLM sinh xong MỘT câu (sentence streaming)
    LLM_DONE = "llm.done"                  # LLM sinh xong toàn bộ lượt nói

    # --- Luồng âm thanh ---
    TTS_AUDIO_READY = "tts.audio_ready"    # một chunk audio đã tổng hợp xong, sẵn sàng phát
    TTS_SPEAK_START = "tts.speak_start"    # bắt đầu phát tiếng (AI bắt đầu "nói")
    TTS_SPEAK_END = "tts.speak_end"        # phát xong (AI "im")

    # --- Avatar ---
    AVATAR_EMOTION = "avatar.emotion"      # đổi biểu cảm Live2D

    # --- Điều khiển / vòng đời ---
    BARGE_IN = "control.barge_in"          # ngắt lời (tin quan trọng chen vào)
    TICK = "control.tick"                  # nhịp định kỳ để orchestrator đánh giá
    SHUTDOWN = "control.shutdown"


# Nguồn gốc của một lượt nói — để orchestrator biết đây là tự nói hay phản hồi
class SpeakSource(str, Enum):
    MONOLOGUE = "monologue"    # AI tự nói theo mạch của mình
    CHAT_REPLY = "chat_reply"  # phản hồi một tin chat
    FILLER = "filler"          # câu ngắn lấp khoảng lặng
    EVENT = "event"            # phản ứng sự kiện (donate, người mới...)


@dataclass
class ChatMessage:
    """Một tin nhắn chat đã chuẩn hoá từ Discord (hoặc nguồn khác sau này)."""
    user_id: str                 # ID duy nhất (KHÔNG dùng tên hiển thị làm khoá)
    user_name: str               # tên hiển thị (để đọc/nhắc)
    text: str                    # nội dung đã chuẩn hoá (bỏ mention/link thừa)
    channel_id: str = ""
    is_command: bool = False     # tin dạng lệnh (!skip, !ping...)
    priority_hint: int = 0       # gợi ý ưu tiên (mod/donate = cao) — điền sau
    ts: float = field(default_factory=_now)
    id: str = field(default_factory=_new_id)


@dataclass
class SpeakRequest:
    """Yêu cầu AI nói một điều gì đó. Orchestrator tạo ra, LLM tiêu thụ."""
    source: SpeakSource
    # prompt_context: thông tin để LLM sinh lời (topic tự nói, hoặc tin cần trả lời)
    context: dict = field(default_factory=dict)
    interruptible: bool = True   # lượt nói này có được phép bị ngắt giữa chừng không
    ts: float = field(default_factory=_now)
    id: str = field(default_factory=_new_id)


@dataclass
class Event:
    """Bao ngoài chung cho mọi thứ chảy qua bus."""
    type: EventType
    payload: object = None
    ts: float = field(default_factory=_now)
    id: str = field(default_factory=_new_id)
