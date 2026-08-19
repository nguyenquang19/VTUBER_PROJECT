from __future__ import annotations

import json
from pathlib import Path

from scripts.sample_youtube_replay import select_stratified_lines, write_sample
from services.input.youtube_replay import load_youtube_replay


def _chat(message_id: str, offset_ms: int) -> str:
    return json.dumps({
        "replayChatItemAction": {
            "videoOffsetTimeMsec": str(offset_ms),
            "actions": [{"addChatItemAction": {"item": {
                "liveChatTextMessageRenderer": {
                    "id": message_id,
                    "message": {"simpleText": f"message {message_id}"},
                },
            }}}],
        },
    })


def test_stratified_sample_keeps_chat_lines_in_source_order(tmp_path: Path) -> None:
    source = tmp_path / "source.live_chat.json"
    notice = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatViewerEngagementMessageRenderer": {"id": "notice"},
    }}}]}})
    source.write_text("\n".join([
        _chat("one", 0), notice, _chat("two", 10), _chat("three", 20),
        _chat("four", 30), _chat("five", 40),
    ]) + "\n", encoding="utf-8")

    lines, summary = select_stratified_lines(source, count=3)
    output = tmp_path / "sample.live_chat.json"
    write_sample(output, lines)

    parsed = load_youtube_replay(output)
    assert summary["eligible_chat_lines"] == 5
    assert summary["selected_chat_lines"] == 3
    assert len(summary["input_sha256"]) == 64
    assert [event.event_id for event in parsed.events] == ["one", "three", "five"]


def test_stratified_sample_rejects_unavailable_count(tmp_path: Path) -> None:
    source = tmp_path / "source.live_chat.json"
    source.write_text(_chat("one", 0) + "\n", encoding="utf-8")

    try:
        select_stratified_lines(source, count=2)
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("expected sample count rejection")