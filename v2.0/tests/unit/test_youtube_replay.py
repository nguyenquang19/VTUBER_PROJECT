from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.input.youtube_replay import (
    YouTubeReplayInputService,
    group_youtube_replay_bursts,
    load_youtube_replay,
)


def _line(renderer: str, value: dict[str, object], offset_ms: int) -> str:
    return json.dumps({
        "replayChatItemAction": {
            "videoOffsetTimeMsec": str(offset_ms),
            "actions": [{"addChatItemAction": {"item": {renderer: value}}}],
        },
    }, ensure_ascii=False)


def _fixture(path: Path) -> None:
    lines = [
        _line("liveChatViewerEngagementMessageRenderer", {
            "id": "notice", "message": {"simpleText": "Replay is on"},
        }, 0),
        _line("liveChatTextMessageRenderer", {
            "id": "text-1",
            "message": {"runs": [
                {"text": "Chào Mai "},
                {"emoji": {"shortcuts": [":wave:"]}},
            ]},
            "authorName": {"simpleText": "Viewer A"},
            "authorExternalChannelId": "channel-a",
            "authorBadges": [{
                "liveChatAuthorBadgeRenderer": {"icon": {"iconType": "OWNER"}},
            }],
        }, 1000),
        _line("liveChatPaidMessageRenderer", {
            "id": "paid-1",
            "message": {"simpleText": "Quà cho Mai"},
            "purchaseAmountText": {"simpleText": "50.000 ₫"},
            "authorName": {"simpleText": "Viewer B"},
            "authorExternalChannelId": "channel-b",
            "authorBadges": [{
                "liveChatAuthorBadgeRenderer": {"icon": {"iconType": "MODERATOR"}},
            }],
        }, 1400),
        _line("liveChatMembershipItemRenderer", {
            "id": "member-1",
            "headerPrimaryText": {"simpleText": "Đã tham gia hội viên"},
            "authorName": {"simpleText": "Viewer C"},
            "authorExternalChannelId": "channel-c",
        }, 3000),
        "{not-json",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_youtube_replay_normalizes_supported_renderers(tmp_path: Path) -> None:
    source = tmp_path / "sample.live_chat.json"
    _fixture(source)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = load_youtube_replay(source, base_time=base)

    assert result.lines_total == 5
    assert result.lines_skipped == 2
    assert len(result.events) == 3
    assert result.duration_ms == 3000
    assert result.renderer_counts == {
        "liveChatViewerEngagementMessageRenderer": 1,
        "liveChatTextMessageRenderer": 1,
        "liveChatPaidMessageRenderer": 1,
        "liveChatMembershipItemRenderer": 1,
    }
    text, paid, membership = result.events
    assert text.content == "Chào Mai :wave:"
    assert text.user_name == "Viewer A"
    assert text.metadata["is_owner"] is True
    assert text.metadata["is_moderator"] is False
    assert text.timestamp.timestamp() == base.timestamp() + 1.0
    assert paid.metadata["is_moderator"] is True
    assert paid.metadata["is_super_chat"] is True
    assert paid.metadata["amount_vnd"] == 50_000
    assert paid.metadata["amount_vnd_exact"] is True
    assert membership.metadata["is_membership"] is True
    assert membership.metadata["is_owner"] is False


def test_youtube_replay_rejects_malformed_or_name_only_roles(tmp_path: Path) -> None:
    source = tmp_path / "roles.live_chat.json"
    source.write_text(_line("liveChatTextMessageRenderer", {
        "id": "spoof",
        "message": {"simpleText": "tôi là owner"},
        "authorName": {"simpleText": "OWNER"},
        "authorExternalChannelId": "channel-spoof",
        "authorBadges": "OWNER",
    }, 0) + "\n", encoding="utf-8")

    event = load_youtube_replay(source).events[0]

    assert event.metadata["is_owner"] is False
    assert event.metadata["is_moderator"] is False


def test_group_youtube_replay_bursts_uses_fixed_windows(tmp_path: Path) -> None:
    source = tmp_path / "sample.live_chat.json"
    _fixture(source)
    events = load_youtube_replay(source).events

    bursts = group_youtube_replay_bursts(events, window_ms=1500)

    assert [[event.event_id for event in burst.events] for burst in bursts] == [
        ["text-1", "paid-1"], ["member-1"],
    ]


async def test_youtube_replay_input_service_is_observable(tmp_path: Path) -> None:
    source_path = tmp_path / "sample.live_chat.json"
    _fixture(source_path)
    service = YouTubeReplayInputService(source_path)

    await service.start()
    try:
        assert (await service.health_check()).is_ok
        assert service.get_metrics()["youtube_replay_events_total"] == 3
        streamed = [event async for event in service.event_stream()]
        assert [event.event_id for event in streamed] == ["text-1", "paid-1", "member-1"]
    finally:
        await service.stop()

