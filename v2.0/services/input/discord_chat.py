"""DiscordChatService — đọc chat từ Discord channel via discord.py (Platform.B).

Bot cần được setup ở Discord Developer Portal + join server + có quyền read
messages ở channel_ids định. Token đọc từ env var (SECURITY — không hard-code).

Khác pytchat (polling): Discord event-driven qua callback `on_message`. Bridge:
- callback push message vào asyncio.Queue
- event_stream() consumer loop queue.get() → yield InputEvent

Test không cần bot thật: inject `_client` giả có method dispatch message vào queue.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, AsyncIterator, Mapping

from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent, InputService
from orchestrator.credential_contract import (
    CredentialContractError,
    require_environment_secret,
    validate_environment_reference,
)
from orchestrator.logger import get_logger


class DiscordChatError(Exception):
    pass


class DiscordChatService(InputService):
    service_id = "input_discord"

    def __init__(
        self,
        token_env_var: str = "DISCORD_BOT_TOKEN",
        channel_ids: list[int] | None = None,
        queue_maxsize: int = 500,
        client: Any = None,        # inject discord.Client giả cho test
        ignore_bots: bool = True,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.token_env_var = validate_environment_reference(
            token_env_var, "discord.token_env_var",
        )
        self.channel_ids: set[int] = set(channel_ids or [])
        self.queue_maxsize = queue_maxsize
        self.ignore_bots = ignore_bots

        self._client = client
        self._environ = os.environ if environ is None else environ
        self._client_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        self._running = False
        self._log = get_logger("input_discord")

        self._events_total = 0
        self._events_dropped_channel = 0   # message ở channel khác channel_ids
        self._events_dropped_full = 0      # queue full
        self._credential_failures: dict[str, int] = {}
        self._last_event_ts: datetime | None = None

    @classmethod
    def from_loader(cls, loader, client: Any = None) -> "DiscordChatService":
        raw_ids = loader.get("chat_sources", "discord.channel_ids", []) or []
        return cls(
            token_env_var=loader.get(
                "chat_sources", "discord.token_env_var", "DISCORD_BOT_TOKEN",
            ),
            channel_ids=[int(x) for x in raw_ids],
            queue_maxsize=int(loader.get("chat_sources", "discord.queue_maxsize", 500)),
            client=client,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        if self._client is None:
            try:
                token = require_environment_secret(
                    self._environ, self.token_env_var,
                )
            except CredentialContractError as exc:
                self._credential_failures[exc.reason_code] = (
                    self._credential_failures.get(exc.reason_code, 0) + 1
                )
                raise DiscordChatError(
                    f"Discord credential {exc.reason_code} in env var {self.token_env_var}"
                ) from exc
            self._client = self._create_real_client()
            # Chạy bot ở background task (client.start() blocking cho tới close)
            self._client_task = asyncio.create_task(
                self._run_client(token), name="discord_bot",
            )
        self._running = True
        self._log.info(
            "discord_chat_ready",
            channel_ids=sorted(self.channel_ids) if self.channel_ids else "any",
        )

    async def stop(self) -> None:
        self._running = False
        if self._client is not None:
            try:
                close = getattr(self._client, "close", None)
                if callable(close):
                    await close()
            except Exception as e:  # pragma: no cover
                self._log.warning("discord_close_failed", error=type(e).__name__)
        if self._client_task is not None and not self._client_task.done():
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):
                pass
            self._client_task = None

    async def health_check(self) -> HealthStatus:
        if not self._running or self._client is None:
            return HealthStatus.unhealthy(self.service_id, "chưa start()")
        is_ready = getattr(self._client, "is_ready", None)
        if callable(is_ready) and not is_ready():
            return HealthStatus.unhealthy(self.service_id, "bot chưa ready")
        return HealthStatus.healthy(
            self.service_id, events=self._events_total,
            queue_size=self._queue.qsize(),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "discord_events_total": self._events_total,
            "discord_events_dropped_channel": self._events_dropped_channel,
            "discord_events_dropped_full": self._events_dropped_full,
            "discord_credential_failures": dict(sorted(self._credential_failures.items())),
            "discord_queue_size": self._queue.qsize(),
            "discord_last_event_ts": self._last_event_ts.isoformat() if self._last_event_ts else None,
        }

    def _create_real_client(self) -> Any:
        try:
            import discord  # type: ignore
        except ImportError as e:
            raise DiscordChatError(f"discord.py chưa cài: {e}") from e
        intents = discord.Intents.default()
        intents.message_content = True   # cần bật ở Discord Developer Portal
        client = discord.Client(intents=intents)

        @client.event
        async def on_message(msg):
            self._handle_message(msg)

        return client

    async def _run_client(self, token: str) -> None:
        try:
            await self._client.start(token)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.error("discord_client_crashed", error=type(e).__name__)

    # ---------- Message ingestion ----------

    def _handle_message(self, msg: Any) -> None:
        """Callback từ discord.py — push vào queue. Non-blocking, drop nếu full."""
        try:
            if self.ignore_bots and getattr(getattr(msg, "author", None), "bot", False):
                return
            if self.channel_ids:
                channel = getattr(msg, "channel", None)
                cid = int(getattr(channel, "id", 0)) if channel else 0
                if cid not in self.channel_ids:
                    self._events_dropped_channel += 1
                    return
            ev = self._to_event(msg)
            if ev is None:
                return
            try:
                self._queue.put_nowait(ev)
                self._events_total += 1
                self._last_event_ts = ev.timestamp
            except asyncio.QueueFull:
                self._events_dropped_full += 1
                self._log.warning("discord_queue_full_drop", event_id=ev.event_id)
        except Exception as e:
            self._log.warning("discord_handle_failed", error=str(e))

    # ---------- InputService ----------

    async def event_stream(self) -> AsyncIterator[InputEvent]:
        if not self._running:
            raise DiscordChatError("chưa start()")
        while self._running:
            try:
                # timeout để loop check _running flag mỗi 0.5s
                ev = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                yield ev
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _to_event(msg: Any) -> InputEvent | None:
        try:
            content = str(getattr(msg, "content", "") or "").strip()
            if not content:
                return None
            author = getattr(msg, "author", None)
            user_id = getattr(author, "id", None) if author else None
            user_name = getattr(author, "name", None) if author else None
            channel = getattr(msg, "channel", None)
            channel_id = getattr(channel, "id", None) if channel else None
            msg_id = getattr(msg, "id", None)
            ts = getattr(msg, "created_at", None) or datetime.now()
            meta: dict[str, Any] = {"platform": "discord"}
            if channel_id is not None:
                meta["channel_id"] = int(channel_id)
            return InputEvent(
                event_id=str(msg_id) if msg_id else "",
                timestamp=ts,
                source=EventSource.CHAT_DISCORD,
                user_id=str(user_id) if user_id else None,
                user_name=str(user_name) if user_name else None,
                content=content,
                metadata=meta,
            )
        except Exception:
            return None
