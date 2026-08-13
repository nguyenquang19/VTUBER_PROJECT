"""TTS composition helper for the live runtime.

This module owns startup/health/degraded gates only. ``stream_runtime.py`` remains
the composition root and owns lifecycle ordering.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import get_logger
from orchestrator.metrics_collector import MetricsCollector


@dataclass
class TTSRuntimeStack:
    """Delivery stack after startup gates; primary may degrade to subtitle-only."""

    primary: Any
    subtitle: Any
    player: Any
    pipeline: Any
    degraded_reason: str | None = None


async def build_tts_runtime_stack(
    loader: Any,
    metrics: MetricsCollector,
    *,
    primary_factory: Callable[[Any], Any] | None = None,
    subtitle_factory: Callable[[Any], Any] | None = None,
    player_factory: Callable[[int], Any] | None = None,
) -> TTSRuntimeStack:
    """Start TTS behind bounded health gates and retain a real subtitle sink."""
    from services.tts.audio_player import AudioPlayer
    from services.tts.subtitle_fallback import SubtitleFallbackService
    from services.tts.tts_pipeline import TTSPipeline
    from services.tts.vieneu_service import VieNeuTtsService

    primary_factory = primary_factory or (lambda value: VieNeuTtsService.from_loader(value))
    subtitle_factory = subtitle_factory or (
        lambda value: SubtitleFallbackService.from_loader(value)
    )
    pitch_semitones = float(loader.get("models", "tts.pitch_semitones", 0.0) or 0.0)
    player_factory = player_factory or (
        lambda sample_rate: AudioPlayer(
            sample_rate=sample_rate,
            pitch_semitones=pitch_semitones,
        )
    )
    startup_timeout_s = float(loader.get("models", "tts.startup_timeout_s", 30.0))
    health_timeout_s = float(loader.get("models", "tts.health_timeout_s", 5.0))
    fallback_enabled = bool(loader.get("models", "tts_fallback.enabled", True))
    log = get_logger("stream_runtime")

    subtitle = None
    subtitle_error: str | None = None
    if fallback_enabled:
        candidate = subtitle_factory(loader)
        try:
            await asyncio.wait_for(candidate.start(), timeout=health_timeout_s)
            health = await asyncio.wait_for(
                candidate.health_check(), timeout=health_timeout_s,
            )
            if not health.is_ok:
                raise RuntimeError(health.message or "subtitle health gate failed")
            subtitle = candidate
        except Exception as exc:
            subtitle_error = f"{type(exc).__name__}: {exc}"
            log.warning("subtitle_startup_gate_failed", error=subtitle_error)

    primary = primary_factory(loader)
    player = None
    degraded_reason: str | None = None
    try:
        await asyncio.wait_for(primary.start(), timeout=startup_timeout_s)
        health = await asyncio.wait_for(primary.health_check(), timeout=health_timeout_s)
        if not health.is_ok:
            raise RuntimeError(health.message or "TTS health gate failed")
        player = player_factory(int(getattr(primary, "sample_rate", 48000)))
        await asyncio.wait_for(player.start(), timeout=health_timeout_s)
    except Exception as exc:
        reason = "startup_timeout" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
        degraded_reason = f"{reason}: {exc}".rstrip()
        log.warning(
            "tts_primary_unavailable_subtitle_only",
            error=degraded_reason,
            subtitle_available=subtitle is not None,
        )
        if player is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(player.stop(), timeout=health_timeout_s)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(primary.stop(), timeout=health_timeout_s)
        primary = None
        player = None

    if primary is None and subtitle is None:
        detail = degraded_reason or "TTS primary unavailable"
        if subtitle_error:
            detail += f"; subtitle={subtitle_error}"
        raise RuntimeError(f"không có TTS/subtitle delivery sink: {detail}")

    pipeline = TTSPipeline.from_loader(
        loader,
        primary=primary,
        subtitle=subtitle,
        player=player,
        fallback=FallbackManager(),
        metrics=metrics,
    )
    return TTSRuntimeStack(
        primary=primary,
        subtitle=subtitle,
        player=player,
        pipeline=pipeline,
        degraded_reason=degraded_reason,
    )
