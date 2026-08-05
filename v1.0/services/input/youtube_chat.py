"""YouTubeChatService — đọc chat từ YouTube Live via pytchat (Platform.A).

Không cần OAuth cho public stream — pytchat scrape URL YouTube live trực tiếp.
Poll interval mặc định 2s (pytchat khuyến nghị 2-5s để không rate-limit).

Event flow:
  pytchat message → parse → InputEvent (source=CHAT_YOUTUBE) → yield qua event_stream()
  ChatRouter (Platform.B) consume stream, dispatch tới emotion + runner.

Test không cần YouTube thật: inject `_chat_client` giả qua constructor.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, AsyncIterator

from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent, InputService
from orchestrator.logger import get_logger


class YouTubeChatError(Exception):
    pass


class YouTubeChatService(InputService):
    service_id = "input_youtube"

    def __init__(
        self,
        video_id: str,
        poll_interval_s: float = 2.0,
        chat_client: Any = None,   # inject pytchat.PytchatCore-like cho test
    ) -> None:
        self.video_id = video_id
        self.poll_interval_s = float(poll_interval_s)
        self._chat_client = chat_client
        self._log = get_logger("input_youtube")
        self._running = False

        self._events_total = 0
        self._errors_total = 0
        self._last_event_ts: datetime | None = None

    @classmethod
    def from_loader(cls, loader, chat_client: Any = None) -> "YouTubeChatService":
        return cls(
            video_id=str(loader.get("chat_sources", "youtube.video_id", "")),
            poll_interval_s=float(loader.get("chat_sources", "youtube.poll_interval_s", 2.0)),
            chat_client=chat_client,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        if self._chat_client is None:
            self._chat_client = await asyncio.to_thread(self._create_pytchat_client)
        self._running = True
        self._log.info(
            "youtube_chat_ready", video_id=self.video_id,
            poll_interval_s=self.poll_interval_s,
        )

    async def stop(self) -> None:
        self._running = False
        try:
            if self._chat_client is not None:
                terminate = getattr(self._chat_client, "terminate", None)
                if callable(terminate):
                    await asyncio.to_thread(terminate)
        except Exception as e:  # pragma: no cover - defensive
            self._log.warning("youtube_terminate_failed", error=str(e))

    async def health_check(self) -> HealthStatus:
        if not self._running or self._chat_client is None:
            return HealthStatus.unhealthy(self.service_id, "chưa start()")
        is_alive = getattr(self._chat_client, "is_alive", None)
        alive = bool(is_alive()) if callable(is_alive) else True
        if not alive:
            return HealthStatus.unhealthy(self.service_id, "chat client dead")
        return HealthStatus.healthy(
            self.service_id, video_id=self.video_id, events=self._events_total,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "youtube_events_total": self._events_total,
            "youtube_errors_total": self._errors_total,
            "youtube_last_event_ts": self._last_event_ts.isoformat() if self._last_event_ts else None,
        }

    def _create_pytchat_client(self) -> Any:
        try:
            import pytchat  # type: ignore
        except ImportError as e:
            raise YouTubeChatError(f"pytchat chưa cài: {e}") from e
        try:
            return pytchat.create(video_id=self.video_id)
        except Exception as e:
            raise YouTubeChatError(f"không tạo được pytchat cho {self.video_id}: {e}") from e

    # ---------- InputService ----------

    async def event_stream(self) -> AsyncIterator[InputEvent]:
        """Yield InputEvent liên tục cho tới khi stop() hoặc client die.

        Poll pytchat mỗi `poll_interval_s`; mỗi lần lấy batch → yield từng cái.
        pytchat.get() blocking (sync) → wrap asyncio.to_thread.
        """
        if not self._running:
            raise YouTubeChatError("chưa start()")
        while self._running:
            try:
                chat = await asyncio.to_thread(self._safe_get)
                if chat is None:
                    # client died
                    self._log.info("youtube_client_ended")
                    break
                items = chat.items if hasattr(chat, "items") else []
                for raw in items:
                    if not self._running:
                        break
                    ev = self._to_event(raw)
                    if ev is None:
                        continue
                    self._events_total += 1
                    self._last_event_ts = ev.timestamp
                    yield ev
            except Exception as e:
                self._errors_total += 1
                self._log.warning("youtube_poll_failed", error=str(e))
            await asyncio.sleep(self.poll_interval_s)

    def _safe_get(self) -> Any:
        client = self._chat_client
        if client is None:
            return None
        try:
            if hasattr(client, "is_alive") and not client.is_alive():
                return None
        except Exception:
            pass
        get = getattr(client, "get", None)
        if not callable(get):
            return None
        return get()

    @staticmethod
    def _to_event(raw: Any) -> InputEvent | None:
        """Parse 1 pytchat message → InputEvent. Trả None nếu thiếu field."""
        try:
            content = getattr(raw, "message", None) or ""
            if not content.strip():
                return None
            author = getattr(raw, "author", None)
            user_id = getattr(author, "channelId", None) if author else None
            user_name = getattr(author, "name", None) if author else None
            ts_iso = getattr(raw, "datetime", None)
            ts = _parse_ts(ts_iso) or datetime.now()
            event_id = getattr(raw, "id", None) or uuid.uuid4().hex
            meta: dict[str, Any] = {"platform": "youtube"}
            # pytchat có thể có amount cho super chat
            amount = getattr(raw, "amountValue", None)
            if amount:
                meta["amount_vnd"] = int(amount)
                meta["is_super_chat"] = True
            return InputEvent(
                event_id=str(event_id),
                timestamp=ts,
                source=EventSource.CHAT_YOUTUBE,
                user_id=str(user_id) if user_id else None,
                user_name=str(user_name) if user_name else None,
                content=content.strip(),
                metadata=meta,
            )
        except Exception:
            return None


def _parse_ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        # pytchat format: "2024-01-15 14:30:00"
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
