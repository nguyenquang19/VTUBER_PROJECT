"""Test DiscordChatService — Phase Platform.B (không cần bot thật)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from interfaces.input import EventSource
from services.input.discord_chat import DiscordChatError, DiscordChatService

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClient:
    """Giả discord.Client — test push message qua _handle_message trực tiếp."""

    def __init__(self, ready: bool = True) -> None:
        self._ready = ready
        self.closed = False
        self.start_called = False

    def is_ready(self) -> bool: return self._ready
    async def close(self) -> None: self.closed = True
    async def start(self, token: str) -> None:
        self.start_called = True
        # simulate long-running bot — chờ cancel
        await asyncio.Future()


def fake_msg(text: str, user_id: int = 100, user_name: str = "User1",
             msg_id: int = 1, channel_id: int = 999,
             is_bot: bool = False, ts=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=text,
        author=SimpleNamespace(id=user_id, name=user_name, bot=is_bot),
        channel=SimpleNamespace(id=channel_id),
        id=msg_id,
        created_at=ts or datetime.now(),
    )


def make(**over) -> DiscordChatService:
    kw = dict(client=FakeClient())
    kw.update(over)
    return DiscordChatService(**kw)


class TestLifecycle:
    async def test_start_health_ok(self) -> None:
        svc = make()
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is True
        await svc.stop()

    async def test_health_unhealthy_before_start(self) -> None:
        svc = make()
        h = await svc.health_check()
        assert h.is_ok is False

    async def test_health_unhealthy_when_client_not_ready(self) -> None:
        svc = make(client=FakeClient(ready=False))
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is False
        await svc.stop()

    async def test_stop_closes_client(self) -> None:
        c = FakeClient()
        svc = make(client=c)
        await svc.start()
        await svc.stop()
        assert c.closed is True

    async def test_stop_before_start_safe(self) -> None:
        svc = make()
        await svc.stop()  # no raise


class TestMessageHandling:
    async def test_message_pushed_to_queue(self) -> None:
        svc = make()
        await svc.start()
        svc._handle_message(fake_msg("Xin chào Mai"))
        # queue có 1 event
        ev = await asyncio.wait_for(svc._queue.get(), timeout=1.0)
        assert ev.content == "Xin chào Mai"
        assert ev.source == EventSource.CHAT_DISCORD
        assert ev.metadata["platform"] == "discord"
        assert ev.metadata["channel_id"] == 999
        await svc.stop()

    async def test_bot_message_ignored(self) -> None:
        svc = make(ignore_bots=True)
        await svc.start()
        svc._handle_message(fake_msg("bot noise", is_bot=True))
        assert svc._queue.empty()
        assert svc.get_metrics()["discord_events_total"] == 0
        await svc.stop()

    async def test_bot_message_kept_when_ignore_false(self) -> None:
        svc = make(ignore_bots=False)
        await svc.start()
        svc._handle_message(fake_msg("bot noise", is_bot=True))
        assert svc.get_metrics()["discord_events_total"] == 1
        await svc.stop()

    async def test_channel_filter_drops_wrong_channel(self) -> None:
        svc = make(channel_ids=[1000], client=FakeClient())
        await svc.start()
        svc._handle_message(fake_msg("hi", channel_id=9999))
        assert svc.get_metrics()["discord_events_dropped_channel"] == 1
        assert svc._queue.empty()
        svc._handle_message(fake_msg("ok", channel_id=1000))
        assert svc.get_metrics()["discord_events_total"] == 1
        await svc.stop()

    async def test_channel_filter_empty_accepts_all(self) -> None:
        svc = make(channel_ids=[])
        await svc.start()
        svc._handle_message(fake_msg("hi", channel_id=1))
        svc._handle_message(fake_msg("hi2", channel_id=2))
        assert svc.get_metrics()["discord_events_total"] == 2
        await svc.stop()

    async def test_empty_content_skipped(self) -> None:
        svc = make()
        await svc.start()
        svc._handle_message(fake_msg("   "))
        assert svc._queue.empty()
        await svc.stop()

    async def test_queue_full_drops_message(self) -> None:
        svc = make(queue_maxsize=2)
        await svc.start()
        for i in range(5):
            svc._handle_message(fake_msg(f"m{i}", msg_id=i))
        m = svc.get_metrics()
        assert m["discord_events_total"] == 2
        assert m["discord_events_dropped_full"] == 3
        await svc.stop()


class TestEventStream:
    async def test_stream_yields_pushed_messages(self) -> None:
        svc = make()
        await svc.start()
        svc._handle_message(fake_msg("a", msg_id=1))
        svc._handle_message(fake_msg("b", msg_id=2))
        got = []
        async for ev in svc.event_stream():
            got.append(ev.content)
            if len(got) >= 2:
                await svc.stop()
                break
        assert got == ["a", "b"]

    async def test_stream_without_start_raises(self) -> None:
        svc = make()
        with pytest.raises(DiscordChatError):
            async for _ in svc.event_stream():
                break


class TestFromLoader:
    def test_reads_config(self, tmp_path: Path) -> None:
        from orchestrator.config_loader import ConfigLoader

        (tmp_path / "chat_sources.yaml").write_text(
            """discord:
  enabled: false
  token_env_var: DISCORD_BOT_TOKEN
  channel_ids: []
  queue_maxsize: 500
""",
            encoding="utf-8",
        )
        loader = ConfigLoader(tmp_path, required=())
        loader.load_all()
        svc = DiscordChatService.from_loader(loader, client=FakeClient())
        assert svc.token_env_var == "DISCORD_BOT_TOKEN"
        assert svc.channel_ids == set()
        assert svc.queue_maxsize == 500


class TestTokenValidation:
    async def test_missing_token_raises(self, monkeypatch) -> None:
        """Nếu KHÔNG inject client → tự create real client cần token env."""
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        svc = DiscordChatService(client=None, token_env_var="DISCORD_BOT_TOKEN")
        with pytest.raises(DiscordChatError, match="credential_missing"):
            await svc.start()

    async def test_malformed_token_is_not_trimmed_or_exposed(self) -> None:
        secret = " raw-secret "
        svc = DiscordChatService(
            client=None,
            token_env_var="DISCORD_BOT_TOKEN",
            environ={"DISCORD_BOT_TOKEN": secret},
        )
        with pytest.raises(DiscordChatError, match="credential_invalid") as raised:
            await svc.start()
        assert secret not in str(raised.value)
        assert svc.get_metrics()["discord_credential_failures"] == {
            "credential_invalid": 1,
        }

    async def test_real_client_factory_receives_exact_valid_token(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret = "exact-token"
        client = FakeClient()
        captured: list[str] = []

        async def capture_start(token: str) -> None:
            captured.append(token)
            await asyncio.Future()

        client.start = capture_start  # type: ignore[method-assign]
        svc = DiscordChatService(
            client=None,
            token_env_var="DISCORD_BOT_TOKEN",
            environ={"DISCORD_BOT_TOKEN": secret},
        )
        monkeypatch.setattr(svc, "_create_real_client", lambda: client)

        await svc.start()
        await asyncio.sleep(0)
        await svc.stop()

        assert captured == [secret]
