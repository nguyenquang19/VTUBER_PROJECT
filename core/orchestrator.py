"""
orchestrator.py — Bộ điều phối (turn-taking hai chiều).

Đây là logic cốt lõi hiện thực hoá "đảo ngược tư duy":
  AI LUÔN có mạch tự nói làm nền; chat CHEN VÀO mạch đó rồi AI quay về mạch.

Mỗi TICK (~1-2s) orchestrator tính điểm và chọn 1 trong:
  - PHẢN HỒI CHAT   (nếu có tin đáng trả lời)
  - TỰ NÓI           (monologue engine)
  - FILLER           (lấp khoảng lặng ngắn)
  - IM               (nghỉ một nhịp cho tự nhiên)

Xử lý barge-in: nếu đang nói mà có tin VƯỢT NGƯỠNG -> ngắt, xử tin, về mạch.

V1 (khung): dùng hệ điểm bằng luật (Tầng 3 rút gọn). LLM/TTS là interface,
có thể cắm mock hoặc đồ thật.
"""
from __future__ import annotations
import asyncio
import logging
import time

from .bus import EventBus
from .events import (
    ChatMessage, Event, EventType, SpeakRequest, SpeakSource,
)
from .monologue import MonologueEngine

log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(
        self,
        bus: EventBus,
        monologue: MonologueEngine,
        llm,                  # đối tượng có .generate(SpeakRequest) -> async yield câu
        tts,                  # đối tượng có .speak(text) -> async
        *,
        tick_interval: float = 1.2,
        silence_before_monologue: float = 3.0,   # im bao lâu thì tự nói
        barge_in_threshold: int = 70,            # điểm tin phải vượt để ngắt lời
        reply_threshold: int = 25,               # điểm tối thiểu để đáng trả lời
        casual_pickup_prob: float = 0.35,        # xác suất nhặt 1 tin thường lên đọc
    ) -> None:
        self.bus = bus
        self.monologue = monologue
        self.llm = llm
        self.tts = tts

        self.tick_interval = tick_interval
        self.silence_before_monologue = silence_before_monologue
        self.barge_in_threshold = barge_in_threshold
        self.reply_threshold = reply_threshold
        self.casual_pickup_prob = casual_pickup_prob

        self._chat_queue: list[ChatMessage] = []
        self._is_speaking = False
        self._current_task: asyncio.Task | None = None
        self._last_activity = time.time()   # lần cuối AI nói HOẶC có tin
        self._running = False
        self._pending_urgent: ChatMessage | None = None  # tin gây barge-in, xử ngay

        bus.subscribe(EventType.CHAT_INCOMING, self._on_chat)

    # ---------- nhận chat ----------
    async def _on_chat(self, event: Event) -> None:
        msg: ChatMessage = event.payload
        score = self._score_message(msg)
        msg.priority_hint = score
        self._chat_queue.append(msg)
        self._last_activity = time.time()
        log.info("Chat vào: [%s] %s (điểm=%d)", msg.user_name, msg.text, score)

        # Barge-in: đang nói mà tin quá quan trọng -> ngắt ngay và
        # đánh dấu tin này PHẢI xử lý ở nhịp tick kế tiếp (không để cạnh tranh).
        if self._is_speaking and score >= self.barge_in_threshold:
            log.info(">>> BARGE-IN bởi tin điểm %d", score)
            if msg in self._chat_queue:
                self._chat_queue.remove(msg)
            self._pending_urgent = msg
            await self._interrupt()

    def _score_message(self, msg: ChatMessage) -> int:
        """Chấm điểm độ 'đáng phản hồi' của một tin. Luật đơn giản cho V1."""
        score = 20  # nền: mọi tin đều có chút giá trị
        low = msg.text.lower()
        if msg.is_command:
            score += 60
        if "?" in msg.text:                      # câu hỏi
            score += 25
        # nhắc tên AI (placeholder — thay bằng tên thật trong config)
        if any(k in low for k in ("ai", "bot", "neuro", "em ơi", "bạn ơi")):
            score += 30
        if msg.priority_hint:                    # mod/donate đã gắn sẵn
            score += msg.priority_hint
        return min(score, 100)

    # ---------- vòng lặp chính ----------
    async def run(self) -> None:
        self._running = True
        log.info("Orchestrator bắt đầu vòng lặp.")
        while self._running:
            await asyncio.sleep(self.tick_interval)
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("Lỗi trong tick")

    async def _tick(self) -> None:
        # Đang nói dở thì để yên (barge-in xử riêng ở _on_chat)
        if self._is_speaking:
            return

        # 0) Có tin urgent vừa gây barge-in? -> xử lý NGAY, ưu tiên tuyệt đối
        if self._pending_urgent is not None:
            msg = self._pending_urgent
            self._pending_urgent = None
            await self._speak(SpeakRequest(
                source=SpeakSource.CHAT_REPLY,
                context={"chat": {"user": msg.user_name, "text": msg.text},
                         "urgent": True},
                interruptible=False,   # tin urgent: không cho ngắt tiếp
            ))
            return

        # 1) Có tin đáng trả lời không?
        msg = self._pop_best_chat()
        if msg is not None:
            await self._speak(SpeakRequest(
                source=SpeakSource.CHAT_REPLY,
                context={"chat": {"user": msg.user_name, "text": msg.text}},
                interruptible=True,
            ))
            return

        # 2) Im đủ lâu -> tự nói (monologue)
        silence = time.time() - self._last_activity
        if silence >= self.silence_before_monologue and self.monologue.has_material():
            beat = self.monologue.next_beat()
            await self._speak(SpeakRequest(
                source=SpeakSource.MONOLOGUE,
                context=beat,
                interruptible=True,
            ))
            return

        # 3) Còn lại: nghỉ một nhịp (im tự nhiên) — không nói liên tục như máy

    def _pop_best_chat(self) -> ChatMessage | None:
        """Lấy tin đáng trả lời nhất, nếu vượt ngưỡng."""
        if not self._chat_queue:
            return None
        # sắp theo điểm giảm dần, rồi theo độ mới
        self._chat_queue.sort(key=lambda m: (-m.priority_hint, -m.ts))
        best = self._chat_queue[0]
        if best.priority_hint >= self.reply_threshold:
            self._chat_queue.pop(0)
            # bỏ luôn các tin quá cũ để tránh trả lời lỗi thời (giữ tối đa 10)
            self._chat_queue = self._chat_queue[:10]
            return best
        # Không tin nào vượt ngưỡng trả lời "nghiêm túc".
        # Với Just Chatting: thỉnh thoảng vẫn nhặt 1 tin thường lên đọc cho ấm
        # (không lạnh lùng bỏ hết). Xác suất thấp để không spam.
        import random as _r
        if self._chat_queue and _r.random() < self.casual_pickup_prob:
            picked = self._chat_queue.pop(0)
            self._chat_queue = self._chat_queue[:10]
            return picked
        return None

    # ---------- nói ----------
    async def _speak(self, req: SpeakRequest) -> None:
        self._is_speaking = True
        self.bus.publish(Event(type=EventType.TTS_SPEAK_START, payload=req))
        try:
            self._current_task = asyncio.create_task(self._do_speak(req))
            await self._current_task
        except asyncio.CancelledError:
            log.info("Lượt nói bị huỷ (barge-in).")
        finally:
            self._is_speaking = False
            self._last_activity = time.time()
            self.bus.publish(Event(type=EventType.TTS_SPEAK_END, payload=req))
            # sau khi trả lời chat xong -> monologue giữ nguyên mạch để 'về mạch'
            if req.source == SpeakSource.CHAT_REPLY:
                self.monologue.interrupt_for_chat()

    async def _do_speak(self, req: SpeakRequest) -> None:
        """
        Sinh lời theo SENTENCE STREAMING: LLM yield từng câu -> đẩy TTS ngay.
        (mock hay thật đều dùng chung interface này)
        """
        async for sentence in self.llm.generate(req):
            self.monologue.register_spoken(sentence)
            # đẩy câu xuống TTS ngay (không đợi cả đoạn)
            self.bus.publish(Event(type=EventType.LLM_SENTENCE, payload=sentence))
            await self.tts.speak(sentence)
        self.bus.publish(Event(type=EventType.LLM_DONE, payload=req))

    async def _interrupt(self) -> None:
        """Ngắt lượt nói hiện tại (barge-in)."""
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            self.bus.publish(Event(type=EventType.BARGE_IN))
            try:
                await self.tts.stop()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._running = False
