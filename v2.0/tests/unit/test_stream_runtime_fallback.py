"""Regression for deliverable canned text in the legacy autonomy path."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest

from interfaces.base import HealthStatus
from interfaces.tts import TTSDeliveryMode, TTSDeliveryResult
from orchestrator.metrics_collector import MetricsCollector
from orchestrator.stream_runtime import (
    StreamRuntime,
    StreamRuntimeConfig,
    _build_tts_runtime_stack,
)
from services.tts.subtitle_fallback import SubtitleFallbackService


class _Router:
    def __init__(self) -> None:
        import asyncio

        self.turn_lock = asyncio.Lock()


class _Runner:
    def __init__(self) -> None:
        self.committed: list[str] = []
        self.finalized: list[tuple[str, bool]] = []

    async def run_ambient_turn(
        self,
        request_id: str,
        prompt_text: str,
        *,
        defer_delivery_commit: bool = False,
    ) -> SimpleNamespace:
        assert defer_delivery_commit is True
        return SimpleNamespace(text="Câu dự phòng", ok=False)

    def finalize_delivery(self, request_id: str, success: bool) -> bool:
        self.finalized.append((request_id, success))
        return True

    def commit_self_talk(self, text: str) -> None:
        self.committed.append(text)


class _Autonomy:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def check_dedup(self, text: str) -> bool:
        return False

    def on_self_spoke(self, text: str) -> None:
        self.spoken.append(text)


def _legacy_runtime(
    speak: Callable[[str, str], Awaitable[Any]] | None,
) -> tuple[StreamRuntime, _Runner, _Autonomy]:
    runner = _Runner()
    autonomy = _Autonomy()
    runtime = StreamRuntime(
        loader=object(),
        llm_svc=object(),
        runner=runner,
        emotion=object(),
        chat_router=_Router(),
        autonomy=autonomy,
        metrics=object(),
        speak=speak,
        cfg=StreamRuntimeConfig(enable_autonomy=True),
    )
    return runtime, runner, autonomy


async def test_legacy_ambient_delivers_nonempty_canned_fallback() -> None:
    deliveries: list[tuple[str, str]] = []

    async def speak(request_id: str, text: str) -> TTSDeliveryResult:
        deliveries.append((request_id, text))
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=True,
            mode=TTSDeliveryMode.SUBTITLE,
            sentences_total=1,
            sentences_delivered=1,
            subtitle_sentences=1,
        )

    runtime, runner, autonomy = _legacy_runtime(speak)
    decision = SimpleNamespace(category="fallback", prompt_text="prompt")

    await runtime._execute_ambient(decision)

    assert len(deliveries) == 1
    assert deliveries[0][1] == "Câu dự phòng"
    assert runner.committed == ["Câu dự phòng"]
    assert autonomy.spoken == ["Câu dự phòng"]
    assert [success for _, success in runner.finalized] == [True]


async def test_legacy_ambient_missing_sink_releases_without_commit() -> None:
    runtime, runner, autonomy = _legacy_runtime(None)

    await runtime._execute_ambient(SimpleNamespace(category="fallback", prompt_text="prompt"))

    assert runner.committed == []
    assert autonomy.spoken == []
    assert [success for _, success in runner.finalized] == [False]


async def test_legacy_ambient_untyped_delivery_releases_without_commit() -> None:
    async def speak(request_id: str, text: str) -> None:
        return None

    runtime, runner, autonomy = _legacy_runtime(speak)

    await runtime._execute_ambient(SimpleNamespace(category="fallback", prompt_text="prompt"))

    assert runner.committed == []
    assert autonomy.spoken == []
    assert [success for _, success in runner.finalized] == [False]


async def test_legacy_ambient_failed_delivery_releases_without_commit() -> None:
    async def speak(request_id: str, text: str) -> TTSDeliveryResult:
        return TTSDeliveryResult(
            request_id=request_id,
            delivered=False,
            mode=TTSDeliveryMode.NONE,
            sentences_total=1,
            sentences_delivered=0,
        )

    runtime, runner, autonomy = _legacy_runtime(speak)

    await runtime._execute_ambient(SimpleNamespace(category="fallback", prompt_text="prompt"))

    assert runner.committed == []
    assert autonomy.spoken == []
    assert [success for _, success in runner.finalized] == [False]


async def test_legacy_ambient_delivery_exception_releases_without_commit() -> None:
    async def speak(request_id: str, text: str) -> TTSDeliveryResult:
        raise RuntimeError("delivery failed")

    runtime, runner, autonomy = _legacy_runtime(speak)

    await runtime._execute_ambient(SimpleNamespace(category="fallback", prompt_text="prompt"))

    assert runner.committed == []
    assert autonomy.spoken == []
    assert [success for _, success in runner.finalized] == [False]


class _TTSLoader:
    def __init__(self, *, startup_timeout_s: float = 0.01) -> None:
        self.values = {
            ("models", "tts.startup_timeout_s"): startup_timeout_s,
            ("models", "tts.health_timeout_s"): 0.1,
            ("models", "tts.timeout_primary_s"): 0.1,
            ("models", "tts.timeout_subtitle_s"): 0.1,
            ("models", "tts_fallback.enabled"): True,
        }

    def get(self, name: str, key: str, default=None):
        return self.values.get((name, key), default)


class _SlowPrimary:
    sample_rate = 48000

    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self.stopped = True

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy("tts")


class _UnhealthyPrimary(_SlowPrimary):
    async def start(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.unhealthy("tts", "voice not enrolled")


class _TrackingSubtitle(SubtitleFallbackService):
    def __init__(self, *, healthy: bool = True) -> None:
        super().__init__(output_file="subtitle.txt", require_delivery=True)
        self.healthy = healthy
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    async def health_check(self) -> HealthStatus:
        if self.healthy:
            return HealthStatus.healthy(self.service_id)
        return HealthStatus.unhealthy(self.service_id, "subtitle unavailable")


class _HealthyPrimary(_SlowPrimary):
    async def start(self) -> None:
        return None


class _CancelledPrimary(_HealthyPrimary):
    async def start(self) -> None:
        raise asyncio.CancelledError


class _Player:
    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


async def test_tts_startup_timeout_keeps_subtitle_only_delivery(tmp_path) -> None:
    primary = _SlowPrimary()
    overlay = tmp_path / "subtitle.txt"
    stack = await _build_tts_runtime_stack(
        _TTSLoader(),
        MetricsCollector(),
        primary_factory=lambda _loader: primary,
        subtitle_factory=lambda _loader: SubtitleFallbackService(
            output_file=overlay, require_delivery=True,
        ),
        player_factory=lambda _sample_rate: (_ for _ in ()).throw(
            AssertionError("player must not start after primary timeout")
        ),
    )

    result = await stack.pipeline.speak("timeout", "Mai đang ở subtitle mode.")

    assert stack.primary is None
    assert stack.player is None
    assert stack.degraded_reason is not None
    assert result.delivered is True
    assert result.mode is TTSDeliveryMode.SUBTITLE
    assert overlay.read_text(encoding="utf-8") == "Mai đang ở subtitle mode."
    assert primary.stopped is True


async def test_unhealthy_tts_startup_gate_keeps_subtitle_only_delivery(tmp_path) -> None:
    primary = _UnhealthyPrimary()
    overlay = tmp_path / "subtitle.txt"
    stack = await _build_tts_runtime_stack(
        _TTSLoader(startup_timeout_s=0.1),
        MetricsCollector(),
        primary_factory=lambda _loader: primary,
        subtitle_factory=lambda _loader: SubtitleFallbackService(
            output_file=overlay, require_delivery=True,
        ),
    )

    result = await stack.pipeline.speak("health", "Fallback vẫn giao được nội dung.")

    assert stack.primary is None
    assert result.delivered is True
    assert result.mode is TTSDeliveryMode.SUBTITLE
    assert primary.stopped is True


async def test_unhealthy_subtitle_candidate_is_cleaned_before_primary_mode() -> None:
    subtitle = _TrackingSubtitle(healthy=False)
    primary = _HealthyPrimary()
    player = _Player()

    stack = await _build_tts_runtime_stack(
        _TTSLoader(startup_timeout_s=0.1),
        MetricsCollector(),
        primary_factory=lambda _loader: primary,
        subtitle_factory=lambda _loader: subtitle,
        player_factory=lambda _sample_rate: player,
    )

    assert stack.primary is primary
    assert stack.subtitle is None
    assert subtitle.stopped is True


async def test_cancelled_tts_startup_cleans_primary_and_started_subtitle() -> None:
    subtitle = _TrackingSubtitle()
    primary = _CancelledPrimary()

    with pytest.raises(asyncio.CancelledError):
        await _build_tts_runtime_stack(
            _TTSLoader(startup_timeout_s=0.1),
            MetricsCollector(),
            primary_factory=lambda _loader: primary,
            subtitle_factory=lambda _loader: subtitle,
        )

    assert primary.stopped is True
    assert subtitle.stopped is True
