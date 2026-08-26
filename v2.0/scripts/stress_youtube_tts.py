"""Stress delivered YouTube outputs through real VieNeu-TTS without opening speakers."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from interfaces.tts import AudioChunk  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
from services.operations.metrics import MetricsCollector  # noqa: E402
from services.llm.process_manager import (  # noqa: E402
    LlamaServerConfig,
    LlamaServerProcessManager,
)
from services.tts.subtitle_fallback import SubtitleFallbackService  # noqa: E402
from services.tts.tts_pipeline import TTSPipeline  # noqa: E402
from services.tts.vieneu_service import VieNeuTtsService  # noqa: E402


class MeasurementPlayer:
    """Collect enqueue timing/duration and discard PCM without an audio device."""

    def __init__(self, sample_rate: int, clock: Any = None) -> None:
        self.sample_rate = int(sample_rate)
        self._clock = clock or time.perf_counter
        self._request_id = ""
        self._started_at = 0.0
        self._chunks: list[dict[str, float | int | str]] = []

    def begin(self, request_id: str) -> None:
        self._request_id = str(request_id)
        self._started_at = float(self._clock())
        self._chunks = []

    async def enqueue(self, chunk: AudioChunk) -> None:
        if not chunk.audio_bytes:
            return
        duration_ms = int(chunk.duration_ms)
        if duration_ms <= 0:
            sample_count = len(chunk.audio_bytes) // 4
            duration_ms = int(1000 * sample_count / self.sample_rate)
        self._chunks.append({
            "request_id": str(chunk.request_id),
            "enqueue_ms": round((float(self._clock()) - self._started_at) * 1000, 3),
            "duration_ms": duration_ms,
        })

    async def cancel_current(self, _request_id: str) -> None:
        return None

    async def cancel_all(self) -> None:
        self._chunks = []

    def finish(self) -> dict[str, Any]:
        chunks = list(self._chunks)
        return {
            "chunk_count": len(chunks),
            "audio_ms": sum(int(item["duration_ms"]) for item in chunks),
            "chunks": chunks,
        }


def extract_delivery_timeline(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return delivered outputs in replay order with source offsets and LLM latency."""
    replay = dict(report.get("replay") or {})
    calls = list((report.get("llm") or {}).get("calls") or ())
    latency_by_id = {
        str(item.get("request_id") or ""): _float_or_zero(item.get("latency_ms"))
        for item in calls
    }
    timeline: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trace in replay.get("trace") or ():
        offset_ms = int(trace.get("offset_ms") or 0)
        for delivery in trace.get("deliveries") or ():
            request_id = str(delivery.get("request_id") or "").strip()
            text = str(delivery.get("text") or "").strip()
            if not request_id or not text:
                continue
            if request_id in seen:
                raise ValueError(f"duplicate delivered request_id: {request_id}")
            seen.add(request_id)
            timeline.append({
                "request_id": request_id,
                "offset_ms": offset_ms,
                "llm_latency_ms": latency_by_id.get(request_id, 0.0),
                "text": text,
            })
    expected = int((replay.get("delivery") or {}).get("delivered_turns") or 0)
    if expected and expected != len(timeline):
        raise ValueError(
            f"delivery timeline mismatch: trace={len(timeline)} report={expected}"
        )
    if not timeline:
        raise ValueError("source report has no delivered timeline")
    return timeline


def build_queue_report(
    timeline: Sequence[dict[str, Any]],
    records: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Model serialized playback from measured chunk enqueue timing and duration."""
    by_id = {str(item.get("request_id") or ""): item for item in records}
    pipeline_free_s = 0.0
    playback_end_s = 0.0
    stream_end_s = max(_float_or_zero(item.get("offset_ms")) for item in timeline) / 1000
    queue_waits: list[float] = []
    source_delays: list[float] = []
    backlogs: list[float] = []
    audio_turns = 0
    waiting_turns = 0

    for source in timeline:
        request_id = str(source.get("request_id") or "")
        record = by_id.get(request_id)
        if record is None:
            continue
        source_s = _float_or_zero(source.get("offset_ms")) / 1000
        llm_s = _float_or_zero(source.get("llm_latency_ms")) / 1000
        synthesis_s = _float_or_zero(record.get("synthesis_ms")) / 1000
        generation_start_s = max(source_s, pipeline_free_s)
        tts_start_s = generation_start_s + llm_s
        chunks = list(record.get("chunks") or ())
        first_playback_s: float | None = None
        first_enqueue_s: float | None = None
        for chunk in chunks:
            enqueue_s = tts_start_s + _float_or_zero(chunk.get("enqueue_ms")) / 1000
            duration_s = _float_or_zero(chunk.get("duration_ms")) / 1000
            backlog_s = max(0.0, playback_end_s - enqueue_s)
            play_start_s = max(enqueue_s, playback_end_s)
            playback_end_s = play_start_s + duration_s
            backlogs.append(backlog_s)
            if first_playback_s is None:
                first_playback_s = play_start_s
                first_enqueue_s = enqueue_s
        pipeline_free_s = tts_start_s + synthesis_s
        if first_playback_s is not None and first_enqueue_s is not None:
            audio_turns += 1
            queue_wait = max(0.0, first_playback_s - first_enqueue_s)
            source_delay = max(0.0, first_playback_s - source_s)
            queue_waits.append(queue_wait)
            source_delays.append(source_delay)
            if queue_wait > 0.05:
                waiting_turns += 1

    return {
        "source_stream_end_s": round(stream_end_s, 3),
        "pipeline_free_s": round(pipeline_free_s, 3),
        "playback_end_s": round(playback_end_s, 3),
        "final_drain_after_source_s": round(max(0.0, playback_end_s - stream_end_s), 3),
        "audio_turns": audio_turns,
        "turns_waiting_for_audio_queue": waiting_turns,
        "waiting_turn_ratio": round(waiting_turns / max(1, audio_turns), 4),
        "queue_wait_s": _stats(queue_waits),
        "playback_start_delay_from_source_s": _stats(source_delays),
        "chunk_backlog_s": _stats(backlogs),
    }


def build_quality_report(
    records: Sequence[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    gates = dict(policy.get("gates") or {})
    completed = list(records)
    audio_turns = [item for item in completed if int(item.get("audio_ms") or 0) > 0]
    silent_turns = [
        item for item in completed
        if int(item.get("audio_ms") or 0) <= 0
        and int(item.get("subtitle_sentences") or 0) <= 0
    ]
    subtitle_sentences = sum(int(item.get("subtitle_sentences") or 0) for item in completed)
    failed_sentences = sum(int(item.get("failed_sentences") or 0) for item in completed)
    primary_failures = subtitle_sentences + failed_sentences
    total_sentences = sum(int(item.get("sentences_total") or 0) for item in completed)
    fallback_ratio = subtitle_sentences / max(1, total_sentences)
    ttfa_values = [
        float(item["ttfa_ms"])
        for item in audio_turns if item.get("ttfa_ms") is not None
    ]
    rtf_values = [
        float(item["rtf"])
        for item in audio_turns if item.get("rtf") is not None
    ]
    ttfa = _stats(ttfa_values)
    rtf = _stats(rtf_values)
    checks = {
        "minimum_audio_turns": len(audio_turns) >= int(
            gates.get("minimum_audio_turns", 50)
        ),
        "silent_turns": len(silent_turns) <= int(gates.get("max_silent_turns", 0)),
        "primary_failures": primary_failures <= int(
            gates.get("max_primary_failures", 0)
        ),
        "subtitle_fallback_ratio": fallback_ratio <= float(
            gates.get("max_subtitle_fallback_ratio", 0.0)
        ),
        "ttfa_p95": ttfa["p95"] is not None and float(ttfa["p95"]) <= float(
            gates.get("ttfa_p95_ms", 1000)
        ),
        "rtf_p95": rtf["p95"] is not None and float(rtf["p95"]) <= float(
            gates.get("rtf_p95_max", 1.0)
        ),
    }
    return {
        "tts_technical_ready": all(checks.values()),
        "checks": checks,
        "counts": {
            "turns": len(completed),
            "audio_turns": len(audio_turns),
            "silent_turns": len(silent_turns),
            "silent_request_ids": [
                str(item.get("request_id") or "") for item in silent_turns
            ],
            "primary_failures": primary_failures,
            "undelivered_sentences": failed_sentences,
            "subtitle_sentences": subtitle_sentences,
            "sentences_total": total_sentences,
        },
        "ratios": {"subtitle_fallback": round(fallback_ratio, 4)},
        "ttfa_ms": ttfa,
        "rtf": rtf,
        "synthesis_ms": _stats([
            _float_or_zero(item.get("synthesis_ms")) for item in completed
        ]),
        "audio_ms": _stats([
            float(item["audio_ms"]) for item in audio_turns
        ]),
        "total_audio_seconds": round(
            sum(int(item.get("audio_ms") or 0) for item in completed) / 1000, 3,
        ),
    }


def refresh_completed_report(
    existing: dict[str, Any],
    *,
    source_sha256: str,
    timeline: Sequence[dict[str, Any]],
    records: Sequence[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Recalculate derived gates from a complete checkpoint without synthesis."""
    if str(existing.get("source_sha256") or "") != source_sha256:
        raise ValueError("existing report source_sha256 does not match source report")
    report = dict(existing)
    report["quality"] = build_quality_report(records, policy)
    report["playback_queue"] = build_queue_report(timeline, records)
    report["turn_records_count"] = len(records)
    report_sample = max(1, int(policy.get("report_turn_sample", 40)))
    report["turn_sample"] = list(records[:report_sample])
    return report


def load_checkpoint_records(
    checkpoint: Path,
    *,
    source_sha256: str,
    restart: bool,
) -> list[dict[str, Any]]:
    if restart or not checkpoint.exists():
        return []
    stored = json.loads(checkpoint.read_text(encoding="utf-8"))
    if str(stored.get("source_sha256") or "") != source_sha256:
        raise ValueError("checkpoint source_sha256 does not match source report")
    records = list(stored.get("records") or ())
    ids = [str(item.get("request_id") or "") for item in records]
    if len(ids) != len(set(ids)):
        raise ValueError("checkpoint contains duplicate request_id")
    return records


async def run_stress(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir) if args.config_dir else REPO_ROOT / "config"
    loader = ConfigLoader(config_dir)
    loader.load_all()
    policy = dict(loader.get(
        "evaluation", "evaluation.youtube_tts_stress", {},
    ) or {})
    source = args.source_report or Path(str(policy.get(
        "source_report", "logs/evaluation/youtube_llm_stress.json",
    )))
    output = args.output or Path(str(policy.get(
        "output_file", "logs/evaluation/youtube_tts_stress.json",
    )))
    checkpoint = args.checkpoint or Path(str(policy.get(
        "checkpoint_file", "logs/evaluation/youtube_tts_stress.checkpoint.json",
    )))
    source = _absolute(source)
    output = _absolute(output)
    checkpoint = _absolute(checkpoint)
    source_bytes = source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    stored_report = json.loads(source_bytes.decode("utf-8"))
    timeline = extract_delivery_timeline(stored_report)
    if args.max_turns is not None:
        if args.max_turns <= 0:
            raise ValueError("max_turns must be positive")
        timeline = timeline[:args.max_turns]
    records = load_checkpoint_records(
        checkpoint, source_sha256=source_sha256, restart=bool(args.restart),
    )
    selected_ids = {str(item["request_id"]) for item in timeline}
    records = [
        item for item in records
        if str(item.get("request_id") or "") in selected_ids
    ]
    completed_ids = {str(item.get("request_id") or "") for item in records}
    if len(records) == len(timeline) and output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        report = refresh_completed_report(
            existing,
            source_sha256=source_sha256,
            timeline=timeline,
            records=records,
            policy=policy,
        )
        _write_json(output, report)
        _write_checkpoint(
            checkpoint,
            source_sha256=source_sha256,
            status="complete",
            total=len(timeline),
            records=records,
            output=output,
        )
        _print_summary(output, report)
        return 0 if report["quality"]["tts_technical_ready"] else 1
    checkpoint_every = max(1, int(policy.get("checkpoint_every", 5)))
    keep_llama_resident = bool(policy.get("keep_llama_resident", True))
    llama_manager: LlamaServerProcessManager | None = None
    primary: VieNeuTtsService | None = None
    subtitle: SubtitleFallbackService | None = None
    started_at = time.perf_counter()
    startup_ms: float | None = None
    health_gate_passed = False
    llama_resident_verified = False

    try:
        if keep_llama_resident:
            llama_config = LlamaServerConfig.from_loader(loader)
            llama_manager = LlamaServerProcessManager(llama_config)
            await llama_manager.start()
            llama_resident_verified = await _verify_llama_model(llama_config)
            if not llama_resident_verified:
                raise RuntimeError("llama-server is healthy but configured model is not resident")

        primary = VieNeuTtsService.from_loader(loader)
        startup_started = time.perf_counter()
        await asyncio.wait_for(
            primary.start(),
            timeout=float(loader.get("models", "tts.startup_timeout_s", 30.0)),
        )
        startup_ms = (time.perf_counter() - startup_started) * 1000
        health = await asyncio.wait_for(
            primary.health_check(),
            timeout=float(loader.get("models", "tts.health_timeout_s", 5.0)),
        )
        if not health.is_ok:
            raise RuntimeError(health.message or "VieNeu health gate failed")
        health_gate_passed = True

        subtitle_events: list[str] = []
        subtitle = SubtitleFallbackService(
            on_subtitle=lambda request_id, _text: subtitle_events.append(request_id),
            require_delivery=True,
        )
        await subtitle.start()
        player = MeasurementPlayer(primary.sample_rate)
        pipeline = TTSPipeline.from_loader(
            loader,
            primary=primary,
            subtitle=subtitle,
            player=player,
            fallback=FallbackManager(),
            metrics=MetricsCollector(),
        )

        pending = [item for item in timeline if item["request_id"] not in completed_ids]
        for index, item in enumerate(pending, start=1):
            request_id = str(item["request_id"])
            player.begin(request_id)
            synth_started = time.perf_counter()
            error: str | None = None
            try:
                result = await pipeline.speak(request_id, str(item["text"]))
            except Exception as exc:  # defensive: retain checkpoint evidence
                error = f"{type(exc).__name__}: {exc}"
                result = None
            synthesis_ms = (time.perf_counter() - synth_started) * 1000
            measured = player.finish()
            audio_ms = int(measured["audio_ms"])
            ttfa_ms = pipeline.get_metrics().get("tts_pipeline_last_ttfa_ms")
            record = {
                "request_id": request_id,
                "offset_ms": int(item["offset_ms"]),
                "llm_latency_ms": round(_float_or_zero(item.get("llm_latency_ms")), 3),
                "synthesis_ms": round(synthesis_ms, 3),
                "ttfa_ms": _round_optional(ttfa_ms),
                "audio_ms": audio_ms,
                "rtf": (
                    round((synthesis_ms / 1000) / (audio_ms / 1000), 4)
                    if audio_ms > 0 else None
                ),
                "chunk_count": int(measured["chunk_count"]),
                "chunks": measured["chunks"],
                "delivered": bool(getattr(result, "delivered", False)),
                "mode": str(getattr(getattr(result, "mode", None), "value", "none")),
                "sentences_total": int(getattr(result, "sentences_total", 0)),
                "audio_sentences": int(getattr(result, "audio_sentences", 0)),
                "subtitle_sentences": int(getattr(result, "subtitle_sentences", 0)),
                "failed_sentences": int(getattr(result, "failed_sentences", 0)),
                "error": error,
            }
            records.append(record)
            completed_ids.add(request_id)
            if index % checkpoint_every == 0 or index == len(pending):
                _write_checkpoint(
                    checkpoint,
                    source_sha256=source_sha256,
                    status="running",
                    total=len(timeline),
                    records=records,
                )
                print(json.dumps({
                    "status": "running",
                    "completed": len(records),
                    "total": len(timeline),
                    "last_mode": record["mode"],
                    "last_ttfa_ms": record["ttfa_ms"],
                    "last_rtf": record["rtf"],
                }, ensure_ascii=False), flush=True)
    finally:
        if subtitle is not None:
            await subtitle.stop()
        if primary is not None:
            await primary.stop()
        if llama_manager is not None:
            await llama_manager.stop()

    timeline_order = {
        str(item["request_id"]): index for index, item in enumerate(timeline)
    }
    records.sort(
        key=lambda item: timeline_order.get(str(item.get("request_id") or ""), len(timeline))
    )
    quality = build_quality_report(records, policy)
    queue = build_queue_report(timeline, records)
    report_sample = max(1, int(policy.get("report_turn_sample", 40)))
    report = {
        "schema_version": 1,
        "mode": "youtube_delivered_output_real_vieneu_stress",
        "source_report": str(source.resolve()),
        "source_sha256": source_sha256,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "input": {
            "delivered_turns": len(timeline),
            "first_offset_ms": int(timeline[0]["offset_ms"]),
            "last_offset_ms": int(timeline[-1]["offset_ms"]),
        },
        "runtime": {
            "backend": "VieNeu-TTS v3 Turbo",
            "sample_rate": int(getattr(primary, "sample_rate", 0) or 0),
            "health_gate_passed": health_gate_passed,
            "startup_ms": _round_optional(startup_ms),
            "llama_resident_requested": keep_llama_resident,
            "llama_resident_verified": llama_resident_verified,
            "audio_device_opened": False,
            "pcm_persisted": False,
            "tts_metrics": primary.get_metrics() if primary is not None else {},
            "subtitle_metrics": subtitle.get_metrics() if subtitle is not None else {},
        },
        "quality": quality,
        "playback_queue": queue,
        "turn_records_count": len(records),
        "turn_sample": records[:report_sample],
    }
    _write_json(output, report)
    _write_checkpoint(
        checkpoint,
        source_sha256=source_sha256,
        status="complete",
        total=len(timeline),
        records=records,
        output=output,
    )
    _print_summary(output, report)
    return 0 if quality["tts_technical_ready"] else 1


def _print_summary(output: Path, report: dict[str, Any]) -> None:
    quality = dict(report["quality"])
    print(json.dumps({
        "output": str(output.resolve()),
        "elapsed_seconds": report["elapsed_seconds"],
        "tts_technical_ready": quality["tts_technical_ready"],
        "checks": quality["checks"],
        "counts": quality["counts"],
        "ttfa_ms": quality["ttfa_ms"],
        "rtf": quality["rtf"],
        "total_audio_seconds": quality["total_audio_seconds"],
        "playback_queue": report["playback_queue"],
    }, ensure_ascii=False, indent=2))


async def _verify_llama_model(config: LlamaServerConfig) -> bool:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{config.base_url}/props")
        if response.status_code != 200:
            return False
        model_path = str(response.json().get("model_path") or "")
        return Path(model_path).name.casefold() == Path(config.model_path).name.casefold()


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    finite_values: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            finite_values.append(number)
    ordered = sorted(finite_values)
    if not ordered:
        return {"min": None, "p50": None, "p95": None, "max": None, "average": None}
    return {
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
        "average": round(sum(ordered) / len(ordered), 3),
    }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    if not ordered:
        raise ValueError("percentile requires at least one value")
    index = max(0, math.ceil(float(fraction) * len(ordered)) - 1)
    return float(ordered[index])


def _float_or_zero(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _round_optional(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return round(result, 3) if math.isfinite(result) else None


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def _write_checkpoint(
    path: Path,
    *,
    source_sha256: str,
    status: str,
    total: int,
    records: Sequence[dict[str, Any]],
    output: Path | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "source_sha256": source_sha256,
        "total": int(total),
        "completed": len(records),
        "records": list(records),
    }
    if output is not None:
        value["output"] = str(output.resolve())
    _write_json(path, value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_report", type=Path, nargs="?")
    parser.add_argument("--config-dir", type=str)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument(
        "--restart", action="store_true",
        help="ignore compatible checkpoint records and synthesize selected turns again",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_stress(_parse_args(argv)))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
