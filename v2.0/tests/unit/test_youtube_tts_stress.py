from __future__ import annotations

import json
from pathlib import Path

import pytest

from interfaces.tts import AudioChunk
from scripts.stress_youtube_tts import (
    MeasurementPlayer,
    _write_checkpoint,
    build_quality_report,
    build_queue_report,
    extract_delivery_timeline,
    load_checkpoint_records,
    refresh_completed_report,
)


def _policy(*, minimum_audio_turns: int = 2) -> dict:
    return {
        "gates": {
            "minimum_audio_turns": minimum_audio_turns,
            "max_silent_turns": 0,
            "max_primary_failures": 0,
            "max_subtitle_fallback_ratio": 0.0,
            "ttfa_p95_ms": 1000,
            "rtf_p95_max": 1.0,
        }
    }


def _record(
    request_id: str,
    *,
    synthesis_ms: float = 500,
    ttfa_ms: float | None = 200,
    audio_ms: int = 2000,
    subtitle_sentences: int = 0,
    failed_sentences: int = 0,
) -> dict:
    audio_sentences = 1 if audio_ms else 0
    return {
        "request_id": request_id,
        "synthesis_ms": synthesis_ms,
        "ttfa_ms": ttfa_ms,
        "audio_ms": audio_ms,
        "rtf": synthesis_ms / audio_ms if audio_ms else None,
        "sentences_total": audio_sentences + subtitle_sentences + failed_sentences,
        "audio_sentences": audio_sentences,
        "subtitle_sentences": subtitle_sentences,
        "failed_sentences": failed_sentences,
        "chunks": [],
    }


def test_extract_delivery_timeline_preserves_replay_order_and_llm_latency() -> None:
    report = {
        "llm": {
            "calls": [
                {"request_id": "two", "latency_ms": 700},
                {"request_id": "one", "latency_ms": 500},
            ]
        },
        "replay": {
            "delivery": {"delivered_turns": 2},
            "trace": [
                {
                    "offset_ms": 1000,
                    "deliveries": [{"request_id": "one", "text": "Câu một."}],
                },
                {
                    "offset_ms": 2000,
                    "deliveries": [{"request_id": "two", "text": "Câu hai."}],
                },
            ],
        },
    }

    timeline = extract_delivery_timeline(report)

    assert [item["request_id"] for item in timeline] == ["one", "two"]
    assert [item["offset_ms"] for item in timeline] == [1000, 2000]
    assert [item["llm_latency_ms"] for item in timeline] == [500.0, 700.0]


def test_extract_delivery_timeline_rejects_duplicate_request_id() -> None:
    report = {
        "replay": {
            "delivery": {"delivered_turns": 2},
            "trace": [{
                "offset_ms": 1000,
                "deliveries": [
                    {"request_id": "same", "text": "Một."},
                    {"request_id": "same", "text": "Hai."},
                ],
            }],
        }
    }

    with pytest.raises(ValueError, match="duplicate delivered request_id"):
        extract_delivery_timeline(report)


def test_queue_report_serializes_audio_and_reports_final_drain() -> None:
    timeline = [
        {"request_id": "one", "offset_ms": 0, "llm_latency_ms": 0},
        {"request_id": "two", "offset_ms": 1000, "llm_latency_ms": 0},
    ]
    records = [
        {
            **_record("one"),
            "chunks": [{"enqueue_ms": 0, "duration_ms": 2000}],
        },
        {
            **_record("two"),
            "chunks": [{"enqueue_ms": 0, "duration_ms": 1000}],
        },
    ]

    queue = build_queue_report(timeline, records)

    assert queue["playback_end_s"] == 3.0
    assert queue["final_drain_after_source_s"] == 2.0
    assert queue["turns_waiting_for_audio_queue"] == 1
    assert queue["waiting_turn_ratio"] == 0.5
    assert queue["queue_wait_s"]["max"] == 1.0
    assert queue["chunk_backlog_s"]["max"] == 1.0


def test_quality_report_passes_real_audio_within_gates() -> None:
    records = [
        _record("one", synthesis_ms=300, ttfa_ms=100, audio_ms=2000),
        _record("two", synthesis_ms=600, ttfa_ms=200, audio_ms=3000),
    ]

    report = build_quality_report(records, _policy())

    assert report["tts_technical_ready"] is True
    assert all(report["checks"].values())
    assert report["counts"]["audio_turns"] == 2
    assert report["total_audio_seconds"] == 5.0


def test_quality_report_counts_subtitle_as_primary_failure() -> None:
    records = [
        _record("audio", audio_ms=2000),
        _record(
            "subtitle",
            ttfa_ms=None,
            audio_ms=0,
            subtitle_sentences=1,
        ),
    ]

    report = build_quality_report(records, _policy(minimum_audio_turns=1))

    assert report["tts_technical_ready"] is False
    assert report["counts"]["primary_failures"] == 1
    assert report["checks"]["primary_failures"] is False
    assert report["checks"]["subtitle_fallback_ratio"] is False


def test_quality_report_rejects_delivered_text_without_audio_or_subtitle() -> None:
    records = [
        _record("audio", audio_ms=2000),
        _record("ellipsis", ttfa_ms=None, audio_ms=0),
    ]

    report = build_quality_report(records, _policy(minimum_audio_turns=1))

    assert report["tts_technical_ready"] is False
    assert report["counts"]["silent_turns"] == 1
    assert report["counts"]["silent_request_ids"] == ["ellipsis"]
    assert report["checks"]["silent_turns"] is False


def test_checkpoint_resume_validates_source_hash_and_unique_ids(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "tts.checkpoint.json"
    records = [_record("one")]
    _write_checkpoint(
        checkpoint,
        source_sha256="correct",
        status="running",
        total=2,
        records=records,
    )

    assert load_checkpoint_records(
        checkpoint,
        source_sha256="correct",
        restart=False,
    ) == records
    with pytest.raises(ValueError, match="does not match"):
        load_checkpoint_records(
            checkpoint,
            source_sha256="different",
            restart=False,
        )

    stored = json.loads(checkpoint.read_text(encoding="utf-8"))
    stored["records"].append(dict(stored["records"][0]))
    checkpoint.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate request_id"):
        load_checkpoint_records(
            checkpoint,
            source_sha256="correct",
            restart=False,
        )


async def test_measurement_player_discards_pcm_and_keeps_timing_metadata() -> None:
    moments = iter([10.0, 10.125])
    player = MeasurementPlayer(48000, clock=lambda: next(moments))
    player.begin("turn")

    await player.enqueue(AudioChunk(
        request_id="turn#0",
        chunk_index=0,
        audio_bytes=b"\x00" * (48000 * 4),
        is_final=False,
        duration_ms=1000,
    ))

    result = player.finish()
    assert result == {
        "chunk_count": 1,
        "audio_ms": 1000,
        "chunks": [{
            "request_id": "turn#0",
            "enqueue_ms": 125.0,
            "duration_ms": 1000,
        }],
    }
    assert "audio_bytes" not in result["chunks"][0]


def test_refresh_completed_report_recalculates_quality_without_runtime_loss() -> None:
    timeline = [{"request_id": "ellipsis", "offset_ms": 1000, "llm_latency_ms": 0}]
    records = [_record("ellipsis", ttfa_ms=None, audio_ms=0)]
    existing = {
        "source_sha256": "source",
        "elapsed_seconds": 123.0,
        "runtime": {"backend": "VieNeu-TTS v3 Turbo"},
        "quality": {"tts_technical_ready": True},
    }

    refreshed = refresh_completed_report(
        existing,
        source_sha256="source",
        timeline=timeline,
        records=records,
        policy={**_policy(minimum_audio_turns=0), "report_turn_sample": 1},
    )

    assert refreshed["elapsed_seconds"] == 123.0
    assert refreshed["runtime"] == existing["runtime"]
    assert refreshed["quality"]["tts_technical_ready"] is False
    assert refreshed["quality"]["counts"]["silent_request_ids"] == ["ellipsis"]
    assert refreshed["turn_sample"] == records
