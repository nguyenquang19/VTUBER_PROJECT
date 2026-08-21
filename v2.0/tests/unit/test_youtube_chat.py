"""Test YouTubeChatService — Phase Platform.A (không cần YouTube thật)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from interfaces.input import EventSource, InputEvent
from services.input.youtube_chat import YouTubeChatError, YouTubeChatService

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeChatClient:
    """Giả pytchat client — control items trả cho mỗi get()."""

    def __init__(self, batches: list[list[SimpleNamespace]] | None = None,
                 alive: bool = True) -> None:
        self.batches = batches or []
        self._alive = alive
        self._idx = 0
        self.get_calls = 0
        self.terminated = False

    def is_alive(self) -> bool: return self._alive
    def terminate(self) -> None: self.terminated = True

    def get(self):
        self.get_calls += 1
        if self._idx >= len(self.batches):
            self._alive = False
            return None
        batch = self.batches[self._idx]
        self._idx += 1
        return SimpleNamespace(items=batch)


def fake_msg(text: str, user_id: str = "u1", user_name: str = "User1",
             msg_id: str = "m1", amount: int | None = None, *,
             is_owner: object = False, is_moderator: object = False) -> SimpleNamespace:
    return SimpleNamespace(
        message=text,
        author=SimpleNamespace(
            channelId=user_id,
            name=user_name,
            isChatOwner=is_owner,
            isChatModerator=is_moderator,
        ),
        datetime="2026-01-15 14:30:00",
        id=msg_id,
        amountValue=amount,
    )


def make(chat_client=None, **over) -> YouTubeChatService:
    kw = dict(video_id="fake_video", poll_interval_s=0.01, chat_client=chat_client)
    kw.update(over)
    return YouTubeChatService(**kw)


class TestLifecycle:
    async def test_start_health_ok(self) -> None:
        svc = make(chat_client=FakeChatClient())
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is True

    async def test_health_unhealthy_before_start(self) -> None:
        svc = make(chat_client=FakeChatClient())
        h = await svc.health_check()
        assert h.is_ok is False

    async def test_health_unhealthy_when_client_dead(self) -> None:
        svc = make(chat_client=FakeChatClient(alive=False))
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is False

    async def test_stop_terminates_client(self) -> None:
        c = FakeChatClient()
        svc = make(chat_client=c)
        await svc.start()
        await svc.stop()
        assert c.terminated is True


class TestEventStream:
    async def test_yields_events_from_batch(self) -> None:
        client = FakeChatClient(batches=[
            [fake_msg("Xin chào Mai!", "u1", "Alice", "m1"),
             fake_msg("cậu giỏi quá", "u2", "Bob", "m2")],
        ])
        svc = make(chat_client=client)
        await svc.start()
        events = []
        async for ev in svc.event_stream():
            events.append(ev)
            if len(events) >= 2:
                await svc.stop()
                break
        assert len(events) == 2
        assert events[0].content == "Xin chào Mai!"
        assert events[0].source == EventSource.CHAT_YOUTUBE
        assert events[0].user_id == "u1"
        assert events[0].user_name == "Alice"
        assert events[0].metadata["platform"] == "youtube"

    async def test_stream_without_start_raises(self) -> None:
        svc = make(chat_client=FakeChatClient())
        with pytest.raises(YouTubeChatError, match="chưa start"):
            async for _ in svc.event_stream():
                break

    async def test_empty_batch_skipped(self) -> None:
        client = FakeChatClient(batches=[[], [fake_msg("hi", msg_id="m1")]])
        svc = make(chat_client=client)
        await svc.start()
        got = None
        async for ev in svc.event_stream():
            got = ev
            await svc.stop()
            break
        assert got is not None
        assert got.content == "hi"

    async def test_stream_ends_when_client_dies(self) -> None:
        """No batches → client return None → stream ends."""
        svc = make(chat_client=FakeChatClient(batches=[]))
        await svc.start()
        events = []
        async for ev in svc.event_stream():
            events.append(ev)
        assert events == []

    async def test_super_chat_amount_extracted(self) -> None:
        client = FakeChatClient(batches=[
            [fake_msg("thanks Mai!", amount=50000)],
        ])
        svc = make(chat_client=client)
        await svc.start()
        async for ev in svc.event_stream():
            assert ev.metadata["amount_vnd"] == 50000
            assert ev.metadata["is_super_chat"] is True
            await svc.stop()
            break

    async def test_author_badge_roles_are_typed_and_fail_safe(self) -> None:
        client = FakeChatClient(batches=[[
            fake_msg("owner", msg_id="owner", is_owner=True),
            fake_msg("mod", msg_id="mod", is_moderator=True),
            fake_msg("spoof", msg_id="spoof", is_owner="true"),
        ]])
        svc = make(chat_client=client)
        await svc.start()
        events = []
        async for event in svc.event_stream():
            events.append(event)
            if len(events) == 3:
                await svc.stop()
                break

        assert events[0].metadata["is_owner"] is True
        assert events[0].metadata["is_moderator"] is False
        assert events[1].metadata["is_moderator"] is True
        assert events[2].metadata["is_owner"] is False

    async def test_empty_message_skipped(self) -> None:
        client = FakeChatClient(batches=[
            [fake_msg("   ", msg_id="empty"), fake_msg("real message", msg_id="ok")],
        ])
        svc = make(chat_client=client)
        await svc.start()
        got = []
        async for ev in svc.event_stream():
            got.append(ev)
            if len(got) >= 1:
                await svc.stop()
                break
        assert len(got) == 1
        assert got[0].content == "real message"

    async def test_metrics_track_events(self) -> None:
        client = FakeChatClient(batches=[
            [fake_msg("a", msg_id="1"), fake_msg("b", msg_id="2"), fake_msg("c", msg_id="3")],
        ])
        svc = make(chat_client=client)
        await svc.start()
        count = 0
        async for _ in svc.event_stream():
            count += 1
            if count >= 3:
                await svc.stop()
                break
        m = svc.get_metrics()
        assert m["youtube_events_total"] == 3
        assert m["youtube_last_event_ts"] is not None


class TestParseErrors:
    async def test_malformed_message_skipped_not_crash(self) -> None:
        """Msg thiếu field → skip nhưng không giết stream."""
        client = FakeChatClient(batches=[[
            SimpleNamespace(),  # rỗng hoàn toàn
            fake_msg("valid", msg_id="ok"),
        ]])
        svc = make(chat_client=client)
        await svc.start()
        got = None
        async for ev in svc.event_stream():
            got = ev
            await svc.stop()
            break
        assert got is not None
        assert got.content == "valid"


class TestFromLoader:
    def test_reads_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        svc = YouTubeChatService.from_loader(loader, chat_client=FakeChatClient())
        assert svc.video_id == ""
        assert svc.poll_interval_s == 2.0
