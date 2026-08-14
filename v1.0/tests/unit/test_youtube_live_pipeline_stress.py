from __future__ import annotations

import json
import queue
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from interfaces.tts import AudioChunk
from scripts.stress_youtube_live_pipeline import (
    PacedYouTubeReplayInputService,
    PlaybackObserver,
    SilentRealtimeBackend,
    TrackingAudioPlayer,
    _conversation_boundary_counts,
    build_live_quality_report,
)


def _chat_line(message_id: str, text: str, offset_ms: int) -> str:
    return json.dumps({
        "replayChatItemAction": {
            "videoOffsetTimeMsec": str(offset_ms),
            "actions": [{
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "id": message_id,
                            "authorName": {"simpleText": "viewer"},
                            "message": {"runs": [{"text": text}]},
                        }
                    }
                }
            }],
        }
    }, ensure_ascii=False)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_paced_source_preserves_burst_schedule(tmp_path: Path) -> None:
    source_file = tmp_path / "chat.live_chat.json"
    source_file.write_text("\n".join([
        _chat_line("one", "Một", 0),
        _chat_line("two", "Hai", 100),
        _chat_line("three", "Ba", 200),
    ]), encoding="utf-8")
    clock = _FakeClock()
    source = PacedYouTubeReplayInputService(
        source_file,
        base_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        burst_window_ms=50,
        replay_speed=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    await source.start()

    events = [event async for event in source.event_stream()]

    assert [event.event_id for event in events] == ["one", "two", "three"]
    assert clock.now == 0.2
    assert source.completed.is_set()
    assert source.get_metrics()["replay_schedule_drift_ms"]["p95"] == 0.0


def test_silent_backend_blocks_for_metadata_duration_without_pcm_retention() -> None:
    expected: queue.Queue[dict] = queue.Queue()
    expected.put({"request_id": "turn#0", "duration_ms": 750})
    waits: list[float] = []
    observer = PlaybackObserver()
    backend = SilentRealtimeBackend(expected, observer, wait=waits.append)

    backend.play_blocking(np.zeros(48, dtype=np.float32), 48000)

    assert waits == [0.75]
    assert observer.chunks_started == 1
    assert observer.chunks_completed == 1
    record = observer.snapshot()[0]
    assert record["request_id"] == "turn"
    assert "audio_bytes" not in record


async def test_tracking_player_uses_production_queue_without_audio_device() -> None:
    observer = PlaybackObserver()
    player = TrackingAudioPlayer(
        48000,
        queue_maxsize=4,
        observer=observer,
        backend_wait=lambda _seconds: None,
    )
    await player.start()
    try:
        await player.enqueue(AudioChunk(
            request_id="turn#0",
            chunk_index=0,
            audio_bytes=np.zeros(480, dtype=np.float32).tobytes(),
            is_final=False,
            duration_ms=10,
        ))
        await player.enqueue(AudioChunk(
            request_id="turn#0",
            chunk_index=1,
            audio_bytes=b"",
            is_final=True,
            duration_ms=0,
        ))
        drained, _elapsed = await player.wait_until_idle(timeout_s=1.0)
    finally:
        await player.stop()

    assert drained is True
    assert observer.audio_overlaps == 0
    assert observer.snapshot()[0]["audio_ms"] == 10
    assert player.get_metrics()["audio_queue_maxsize"] == 4


def _policy() -> dict:
    return {
        "gates": {
            "minimum_audio_turns": 1,
            "max_input_schedule_drift_p95_ms": 250,
            "max_selected_chat_age_p95_s": 50,
            "max_chat_to_audio_start_p95_s": 20,
            "max_audio_queue_fill_p95_ratio": 0.95,
            "max_post_source_drain_s": 60,
            "max_audio_overlaps": 0,
            "max_silent_turns": 0,
            "max_primary_failures": 0,
            "max_subtitle_fallback_ratio": 0,
            "max_delivery_commit_mismatches": 0,
            "max_semantic_repetition_ratio": 0,
            "max_continue_before_source_read": 0,
            "max_cross_thread_continue_before_park": 0,
            "max_room_reaction_before_park": 0,
            "max_old_thread_continue_after_room": 0,
            "max_room_reactions_per_minute": 0.50,
            "max_formula_opener_ratio": 0.20,
            "max_question_ending_ratio": 0.20,
        }
    }


def test_live_quality_report_passes_bounded_backpressure() -> None:
    report = build_live_quality_report(
        policy=_policy(),
        source_metrics={
            "replay_duration_ms": 60_000,
            "replay_schedule_drift_ms": {
                "min": 1, "p50": 2, "p95": 3, "max": 4, "average": 2,
            }
        },
        deliveries=[{
            "request_id": "turn",
            "action": "read_chat",
            "text": "Một câu mới.",
            "delivered": True,
            "mode": "audio",
            "sentences_total": 1,
            "subtitle_sentences": 0,
            "failed_sentences": 0,
        }],
        playback_records=[{
            "request_id": "turn",
            "first_play_at": 1.0,
            "last_play_end_at": 2.0,
            "audio_ms": 1000,
        }],
        queue_samples=[0, 2, 4],
        queue_maxsize=10,
        selected_chat_ages_s=[2.0],
        chat_to_audio_start_s=[4.0],
        post_source_drain_s=1.0,
        drain_completed=True,
        audio_overlaps=0,
        committed_transactions=1,
    )

    assert report["live_pipeline_ready"] is True
    assert all(report["checks"].values())
    assert report["counts"]["audio_turns_completed"] == 1


def test_live_quality_report_rejects_silent_and_commit_mismatch() -> None:
    report = build_live_quality_report(
        policy=_policy(),
        source_metrics={
            "replay_duration_ms": 60_000,
            "replay_schedule_drift_ms": {"p95": 0},
        },
        deliveries=[{
            "request_id": "silent",
            "delivered": False,
            "mode": "none",
            "sentences_total": 0,
            "subtitle_sentences": 0,
            "failed_sentences": 0,
        }],
        playback_records=[],
        queue_samples=[10],
        queue_maxsize=10,
        selected_chat_ages_s=[60],
        chat_to_audio_start_s=[],
        post_source_drain_s=90,
        drain_completed=False,
        audio_overlaps=1,
        committed_transactions=1,
    )

    assert report["live_pipeline_ready"] is False
    assert report["checks"]["silent_turns"] is False
    assert report["checks"]["delivery_commit_invariant"] is False
    assert report["checks"]["audio_overlap"] is False
    assert report["checks"]["post_source_drain"] is False


def test_live_quality_report_uses_all_deliveries_for_content_gates() -> None:
    first = "Béo ở đâu chứ? Lôi tớ ra trêu cũng vui thật đấy."
    reversed_order = "Lôi tớ ra trêu cũng vui thật đấy. Béo ở đâu chứ?"
    deliveries = [
        {
            "request_id": "goal-1", "action": "continue_thread", "text": first,
            "delivered": True, "mode": "audio", "sentences_total": 2,
            "subtitle_sentences": 0, "failed_sentences": 0,
        },
        {
            "request_id": "goal-2", "action": "continue_thread",
            "text": reversed_order, "delivered": True, "mode": "audio",
            "sentences_total": 2, "subtitle_sentences": 0, "failed_sentences": 0,
        },
        {
            "request_id": "room_1", "action": "read_chat", "text": "Phòng vui ghê.",
            "delivered": True, "mode": "audio", "sentences_total": 1,
            "subtitle_sentences": 0, "failed_sentences": 0,
        },
    ]
    playback = [
        {
            "request_id": item["request_id"], "first_play_at": float(index),
            "last_play_end_at": float(index + 1), "audio_ms": 1000,
        }
        for index, item in enumerate(deliveries)
    ]

    report = build_live_quality_report(
        policy=_policy(),
        source_metrics={
            "replay_duration_ms": 60_000,
            "replay_schedule_drift_ms": {"p95": 0},
        },
        deliveries=deliveries,
        playback_records=playback,
        queue_samples=[0],
        queue_maxsize=10,
        selected_chat_ages_s=[1],
        chat_to_audio_start_s=[1],
        post_source_drain_s=0,
        drain_completed=True,
        audio_overlaps=0,
        committed_transactions=3,
    )

    assert report["live_pipeline_ready"] is False
    assert report["checks"]["semantic_repetition"] is False
    assert report["checks"]["continue_before_source_read"] is False
    assert report["checks"]["room_reaction_cadence"] is False
    assert report["checks"]["question_ending_ratio"] is False
    assert report["counts"]["semantic_duplicate_outputs"] == 1
    assert report["ratios"]["continue_thread"] == 0.6667


def test_conversation_boundary_accepts_closed_thread_before_room() -> None:
    deliveries = [
        {"request_id": "read-a", "action": "read_chat", "thread_id": "a", "delivered": True},
        {"request_id": "goal-1", "action": "continue_thread", "thread_id": "a", "conversation_move": "deepen", "delivered": True},
        {"request_id": "goal-2", "action": "continue_thread", "thread_id": "a", "conversation_move": "summarize", "delivered": True},
        {"request_id": "goal-3", "action": "continue_thread", "thread_id": "a", "conversation_move": "park", "delivered": True},
        {"request_id": "room-ignored", "action": "read_chat", "delivered": False},
        {"request_id": "room_1", "action": "read_chat", "delivered": True},
        {"request_id": "read-b", "action": "read_chat", "thread_id": "b", "delivered": True},
    ]

    assert _conversation_boundary_counts(deliveries) == {
        "continue_before_source_read": 0,
        "cross_thread_continue_before_park": 0,
        "room_reaction_before_park": 0,
        "old_thread_continue_after_room": 0,
    }


def test_conversation_boundary_rejects_room_then_old_thread_resume() -> None:
    deliveries = [
        {"request_id": "read-a", "action": "read_chat", "thread_id": "a", "delivered": True},
        {"request_id": "room_1", "action": "read_chat", "delivered": True},
        {"request_id": "goal-old", "action": "continue_thread", "thread_id": "a", "conversation_move": "clarify", "delivered": True},
        {"request_id": "goal-unread", "action": "continue_thread", "thread_id": "b", "conversation_move": "deepen", "delivered": True},
    ]

    counts = _conversation_boundary_counts(deliveries)
    assert counts["room_reaction_before_park"] == 1
    assert counts["old_thread_continue_after_room"] == 2
    assert counts["continue_before_source_read"] == 1


def test_live_quality_report_gates_formula_openers_on_all_deliveries() -> None:
    deliveries = [
        {
            "request_id": f"turn-{index}", "action": "read_chat", "text": text,
            "delivered": True, "mode": "audio", "sentences_total": 1,
            "subtitle_sentences": 0, "failed_sentences": 0,
        }
        for index, text in enumerate((
            "Mà chuyện này ổn rồi.", "Ủa, chuyện kia cũng ổn.",
            "Phần này xong rồi.", "Để đó xử lý sau.", "Tớ đồng ý.",
        ))
    ]
    playback = [
        {
            "request_id": item["request_id"], "first_play_at": float(index),
            "last_play_end_at": float(index + 1), "audio_ms": 1000,
        }
        for index, item in enumerate(deliveries)
    ]

    report = build_live_quality_report(
        policy=_policy(),
        source_metrics={
            "replay_duration_ms": 600_000,
            "replay_schedule_drift_ms": {"p95": 0},
        },
        deliveries=deliveries,
        playback_records=playback,
        queue_samples=[0],
        queue_maxsize=10,
        selected_chat_ages_s=[1],
        chat_to_audio_start_s=[1],
        post_source_drain_s=0,
        drain_completed=True,
        audio_overlaps=0,
        committed_transactions=5,
    )

    assert report["checks"]["formula_opener_ratio"] is False
    assert report["counts"]["formula_opener_outputs"] == 2
    assert report["ratios"]["formula_openers"] == 0.4
