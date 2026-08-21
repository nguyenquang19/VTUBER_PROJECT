"""Offline adapter for YouTube/yt-dlp ``*.live_chat.json`` replay files."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent, InputService


_SUPPORTED_RENDERERS = (
    "liveChatTextMessageRenderer",
    "liveChatPaidMessageRenderer",
    "liveChatPaidStickerRenderer",
    "liveChatMembershipItemRenderer",
)
_VND_MARKERS = ("₫", "đ", "vnd")


@dataclass(frozen=True)
class YouTubeReplayParseResult:
    events: tuple[InputEvent, ...]
    lines_total: int
    lines_skipped: int
    renderer_counts: dict[str, int]
    duration_ms: int


@dataclass(frozen=True)
class YouTubeReplayBurst:
    offset_ms: int
    events: tuple[InputEvent, ...]


def load_youtube_replay(
    path: str | Path,
    *,
    base_time: datetime | None = None,
) -> YouTubeReplayParseResult:
    """Parse yt-dlp's JSONL replay into normalized, deterministic ``InputEvent`` values."""
    source_path = Path(path)
    origin = base_time or datetime(2026, 1, 1, tzinfo=timezone.utc)
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)

    events: list[InputEvent] = []
    counts: dict[str, int] = {}
    lines_total = 0
    skipped = 0
    with source_path.open("r", encoding="utf-8-sig") as handle:
        for line_index, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            lines_total += 1
            try:
                payload = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                skipped += 1
                continue
            offset_ms = _as_nonnegative_int(
                (payload.get("replayChatItemAction") or {}).get("videoOffsetTimeMsec")
            )
            parsed_on_line = 0
            actions = (payload.get("replayChatItemAction") or {}).get("actions") or ()
            for action in actions:
                item = ((action or {}).get("addChatItemAction") or {}).get("item") or {}
                renderer_name = next((name for name in item if name.endswith("Renderer")), None)
                if renderer_name is not None:
                    counts[renderer_name] = counts.get(renderer_name, 0) + 1
                if renderer_name not in _SUPPORTED_RENDERERS:
                    continue
                renderer = item.get(renderer_name) or {}
                event = _renderer_to_event(
                    renderer_name,
                    renderer,
                    offset_ms=offset_ms,
                    timestamp=origin + timedelta(milliseconds=offset_ms),
                    fallback_id=f"replay-line-{line_index}-{parsed_on_line}",
                )
                if event is not None:
                    events.append(event)
                    parsed_on_line += 1
            if parsed_on_line == 0:
                skipped += 1

    events.sort(key=lambda event: (
        int(event.metadata.get("replay_offset_ms", 0)), event.event_id,
    ))
    duration_ms = max(
        (int(event.metadata.get("replay_offset_ms", 0)) for event in events),
        default=0,
    )
    return YouTubeReplayParseResult(
        events=tuple(events),
        lines_total=lines_total,
        lines_skipped=skipped,
        renderer_counts=counts,
        duration_ms=duration_ms,
    )


def group_youtube_replay_bursts(
    events: tuple[InputEvent, ...],
    *,
    window_ms: int,
) -> tuple[YouTubeReplayBurst, ...]:
    """Group messages into fixed replay-time windows delivered concurrently to intake."""
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    buckets: dict[int, list[InputEvent]] = {}
    for event in events:
        offset_ms = int(event.metadata.get("replay_offset_ms", 0))
        bucket = offset_ms // window_ms
        buckets.setdefault(bucket, []).append(event)
    return tuple(
        YouTubeReplayBurst(
            offset_ms=max(
                int(event.metadata.get("replay_offset_ms", 0)) for event in bucket_events
            ),
            events=tuple(bucket_events),
        )
        for _, bucket_events in sorted(buckets.items())
    )


class YouTubeReplayInputService(InputService):
    """Read-only InputService used by the offline replay simulator."""

    service_id = "input_youtube_replay"

    def __init__(
        self,
        path: str | Path,
        *,
        base_time: datetime | None = None,
    ) -> None:
        self.path = Path(path)
        self.base_time = base_time
        self.result: YouTubeReplayParseResult | None = None
        self._running = False

    async def start(self) -> None:
        self.result = await asyncio.to_thread(
            load_youtube_replay, self.path, base_time=self.base_time,
        )
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running or self.result is None:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            events=len(self.result.events),
            lines_skipped=self.result.lines_skipped,
        )

    def get_metrics(self) -> dict[str, Any]:
        result = self.result
        return {
            "youtube_replay_lines_total": result.lines_total if result else 0,
            "youtube_replay_events_total": len(result.events) if result else 0,
            "youtube_replay_lines_skipped": result.lines_skipped if result else 0,
            "youtube_replay_duration_ms": result.duration_ms if result else 0,
        }

    async def event_stream(self) -> AsyncIterator[InputEvent]:
        if not self._running or self.result is None:
            raise RuntimeError("YouTubeReplayInputService has not been started")
        for event in self.result.events:
            if not self._running:
                break
            yield event

    def bursts(self, *, window_ms: int) -> tuple[YouTubeReplayBurst, ...]:
        if self.result is None:
            raise RuntimeError("YouTubeReplayInputService has not been started")
        return group_youtube_replay_bursts(self.result.events, window_ms=window_ms)


def _renderer_to_event(
    renderer_name: str,
    renderer: dict[str, Any],
    *,
    offset_ms: int,
    timestamp: datetime,
    fallback_id: str,
) -> InputEvent | None:
    content = _renderer_content(renderer_name, renderer).strip()
    if not content:
        return None
    author_name = _text_value(renderer.get("authorName")) or None
    author_id = str(renderer.get("authorExternalChannelId") or "").strip() or None
    metadata: dict[str, Any] = {
        "platform": "youtube",
        "replay_offset_ms": offset_ms,
        "replay_renderer": renderer_name,
        **_author_role_metadata(renderer),
    }
    timestamp_usec = str(renderer.get("timestampUsec") or "").strip()
    if timestamp_usec:
        metadata["youtube_timestamp_usec"] = timestamp_usec
    if renderer_name in {"liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"}:
        amount_display = _text_value(renderer.get("purchaseAmountText"))
        amount_vnd = _parse_vnd(amount_display)
        metadata.update({
            "is_super_chat": True,
            "amount_display": amount_display,
            # Preserve donation priority for non-VND exports without pretending to convert FX.
            "amount_vnd": amount_vnd if amount_vnd is not None else 1,
            "amount_vnd_exact": amount_vnd is not None,
        })
    elif renderer_name == "liveChatMembershipItemRenderer":
        metadata["is_membership"] = True
    return InputEvent(
        event_id=str(renderer.get("id") or fallback_id),
        timestamp=timestamp,
        source=EventSource.CHAT_YOUTUBE,
        user_id=author_id,
        user_name=author_name,
        content=content,
        metadata=metadata,
    )


def _renderer_content(renderer_name: str, renderer: dict[str, Any]) -> str:
    if renderer_name == "liveChatPaidStickerRenderer":
        sticker = renderer.get("sticker") or {}
        label = ((sticker.get("accessibility") or {}).get("accessibilityData") or {}).get("label")
        return str(label or "Super Sticker")
    for field in ("message", "headerPrimaryText", "headerSubtext"):
        text = _text_value(renderer.get(field))
        if text:
            return text
    return "Thành viên mới" if renderer_name == "liveChatMembershipItemRenderer" else ""


def _author_role_metadata(renderer: dict[str, Any]) -> dict[str, bool]:
    """Return only typed YouTube badge roles; never infer authority from names."""
    is_owner = False
    is_moderator = False
    badges = renderer.get("authorBadges")
    if not isinstance(badges, list):
        return {"is_owner": False, "is_moderator": False}
    for badge in badges:
        if not isinstance(badge, dict):
            continue
        rendered = badge.get("liveChatAuthorBadgeRenderer")
        if not isinstance(rendered, dict):
            continue
        icon = rendered.get("icon")
        icon_type = icon.get("iconType") if isinstance(icon, dict) else None
        if icon_type == "OWNER":
            is_owner = True
        elif icon_type == "MODERATOR":
            is_moderator = True
    return {"is_owner": is_owner, "is_moderator": is_moderator}


def _text_value(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    simple = value.get("simpleText")
    if simple is not None:
        return str(simple)
    parts: list[str] = []
    for run in value.get("runs") or ():
        if "text" in run:
            parts.append(str(run.get("text") or ""))
            continue
        emoji = run.get("emoji") or {}
        shortcuts = emoji.get("shortcuts") or ()
        parts.append(str(shortcuts[0] if shortcuts else emoji.get("emojiId") or ""))
    return "".join(parts)


def _parse_vnd(value: str) -> int | None:
    normalized = value.strip().lower()
    if not normalized or not any(marker in normalized for marker in _VND_MARKERS):
        return None
    digits = re.sub(r"\D", "", normalized)
    return int(digits) if digits else None


def _as_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
