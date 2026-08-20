"""Offline M0 smoke checks for dashboard, chat config and cancellation.

This command never creates a real YouTube/Discord client and never reads or
prints a secret value. It is safe to run on a fresh clone without llama-server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import httpx
from prometheus_client import CollectorRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.dashboard_server import DashboardServer
from orchestrator.config_loader import ConfigLoader
from orchestrator.credential_contract import validate_environment_reference
from orchestrator.metrics_collector import MetricsCollector
from orchestrator.stream_runtime import StreamRuntime, StreamRuntimeConfig
from services.input.discord_chat import DiscordChatService
from services.input.youtube_chat import YouTubeChatService


class SmokeStatus(str, Enum):
    PASS = "PASS"
    SKIP = "SKIP"
    FAIL = "FAIL"


@dataclass(frozen=True)
class SmokeResult:
    name: str
    status: SmokeStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class _FakeYouTubeClient:
    def __init__(self) -> None:
        self.terminated = False

    def is_alive(self) -> bool:
        return True

    def terminate(self) -> None:
        self.terminated = True


class _FakeDiscordClient:
    def __init__(self) -> None:
        self.closed = False

    def is_ready(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class _LifecycleProbe:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def check_dashboard(loader: ConfigLoader) -> SmokeResult:
    timeout_s = float(loader.get("system", "smoke.request_timeout_s"))
    metrics = MetricsCollector(registry=CollectorRegistry())
    server = DashboardServer(metrics=metrics)
    transport = httpx.ASGITransport(app=server.app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            timeout=timeout_s,
        ) as client:
            snapshot = await client.get("/api/snapshot")
            prometheus = await client.get("/metrics")
        if snapshot.status_code != 200 or not isinstance(snapshot.json(), dict):
            return SmokeResult("dashboard", SmokeStatus.FAIL, "snapshot endpoint invalid")
        if prometheus.status_code != 200 or "mai_" not in prometheus.text:
            return SmokeResult("dashboard", SmokeStatus.FAIL, "metrics endpoint invalid")
        return SmokeResult(
            "dashboard", SmokeStatus.PASS, "ASGI snapshot and metrics available without port bind",
        )
    except Exception as exc:
        return SmokeResult("dashboard", SmokeStatus.FAIL, f"{type(exc).__name__}: {exc}")


async def check_youtube_config(loader: ConfigLoader) -> SmokeResult:
    enabled = bool(loader.get("chat_sources", "youtube.enabled", False))
    video_id = str(loader.get("chat_sources", "youtube.video_id", "")).strip()
    poll_interval_s = float(loader.get("chat_sources", "youtube.poll_interval_s", 0))
    if poll_interval_s <= 0:
        return SmokeResult("youtube_config", SmokeStatus.FAIL, "poll_interval_s must be > 0")
    if enabled and not video_id:
        return SmokeResult("youtube_config", SmokeStatus.FAIL, "enabled source needs video_id")
    if not enabled:
        return SmokeResult("youtube_config", SmokeStatus.SKIP, "source disabled; config shape valid")

    client = _FakeYouTubeClient()
    service = YouTubeChatService(video_id, poll_interval_s, chat_client=client)
    try:
        await service.start()
        health = await service.health_check()
        await service.stop()
    except Exception as exc:
        return SmokeResult("youtube_config", SmokeStatus.FAIL, f"{type(exc).__name__}: {exc}")
    if not health.is_ok or not client.terminated:
        return SmokeResult("youtube_config", SmokeStatus.FAIL, "offline lifecycle check failed")
    return SmokeResult("youtube_config", SmokeStatus.PASS, "validated with injected offline client")


async def check_discord_config(loader: ConfigLoader) -> SmokeResult:
    enabled = bool(loader.get("chat_sources", "discord.enabled", False))
    try:
        token_env_var = validate_environment_reference(
            loader.get("chat_sources", "discord.token_env_var", ""),
            "discord.token_env_var",
        )
    except ValueError as exc:
        return SmokeResult("discord_config", SmokeStatus.FAIL, str(exc))
    raw_ids = loader.get("chat_sources", "discord.channel_ids", []) or []
    queue_maxsize = int(loader.get("chat_sources", "discord.queue_maxsize", 0))
    if queue_maxsize <= 0:
        return SmokeResult("discord_config", SmokeStatus.FAIL, "queue_maxsize must be > 0")
    try:
        channel_ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError):
        return SmokeResult("discord_config", SmokeStatus.FAIL, "channel_ids must contain integers")
    if any(item <= 0 for item in channel_ids):
        return SmokeResult("discord_config", SmokeStatus.FAIL, "channel_ids must be positive")
    if not enabled:
        return SmokeResult("discord_config", SmokeStatus.SKIP, "source disabled; config shape valid")

    client = _FakeDiscordClient()
    service = DiscordChatService(
        token_env_var=token_env_var,
        channel_ids=channel_ids,
        queue_maxsize=queue_maxsize,
        client=client,
    )
    try:
        await service.start()
        health = await service.health_check()
        await service.stop()
    except Exception as exc:
        return SmokeResult("discord_config", SmokeStatus.FAIL, f"{type(exc).__name__}: {exc}")
    if not health.is_ok or not client.closed:
        return SmokeResult("discord_config", SmokeStatus.FAIL, "offline lifecycle check failed")
    return SmokeResult(
        "discord_config", SmokeStatus.PASS,
        "validated with injected offline client; live secret not required",
    )


async def check_shutdown_cancellation(loader: ConfigLoader) -> SmokeResult:
    timeout_s = float(loader.get("system", "smoke.shutdown_timeout_s"))
    router = _LifecycleProbe()
    llm = _LifecycleProbe()
    cancellation_count = 0

    async def pending() -> None:
        nonlocal cancellation_count
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellation_count += 1
            raise

    dashboard_task = asyncio.create_task(pending(), name="smoke_dashboard")
    runtime = StreamRuntime(
        loader=loader,
        llm_svc=llm,
        runner=object(),
        emotion=object(),
        chat_router=router,
        autonomy=None,
        metrics=MetricsCollector(registry=CollectorRegistry()),
        dashboard_task=dashboard_task,
        cfg=StreamRuntimeConfig(enable_autonomy=False),
    )
    try:
        await runtime.start()
        autonomy_task = asyncio.create_task(pending(), name="smoke_autonomy")
        runtime._autonomy_task = autonomy_task
        await asyncio.wait_for(runtime.stop(), timeout=timeout_s)
    except Exception as exc:
        for task in (dashboard_task, runtime._autonomy_task):
            if task is not None and not task.done():
                task.cancel()
        return SmokeResult("shutdown", SmokeStatus.FAIL, f"{type(exc).__name__}: {exc}")

    if cancellation_count != 2 or not router.stopped or not llm.stopped:
        return SmokeResult("shutdown", SmokeStatus.FAIL, "runtime did not cancel and stop all probes")
    return SmokeResult("shutdown", SmokeStatus.PASS, "autonomy/dashboard tasks cancelled cleanly")


async def run_smoke(loader: ConfigLoader) -> list[SmokeResult]:
    return [
        await check_dashboard(loader),
        await check_youtube_config(loader),
        await check_discord_config(loader),
        await check_shutdown_cancellation(loader),
    ]


def summarize(results: Sequence[SmokeResult]) -> dict[str, int]:
    return {
        status.value.lower(): sum(result.status is status for result in results)
        for status in SmokeStatus
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Mai offline smoke checks")
    parser.add_argument(
        "--output-format", choices=("Text", "Json"), default="Text",
        help="human-readable output or JSON for automation",
    )
    parser.add_argument("--config-dir", type=Path, default=REPO_ROOT / "config")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    loader = ConfigLoader(args.config_dir)
    try:
        loader.load_all()
        results = asyncio.run(run_smoke(loader))
    except Exception as exc:
        results = [SmokeResult("config_load", SmokeStatus.FAIL, f"{type(exc).__name__}: {exc}")]

    summary = summarize(results)
    if args.output_format == "Json":
        print(json.dumps(
            {"checks": [result.to_dict() for result in results], "summary": summary},
            ensure_ascii=False,
        ))
    else:
        for result in results:
            print(f"{result.status.value:<4} {result.name}: {result.detail}")
        print(
            "SUMMARY "
            f"pass={summary['pass']} skip={summary['skip']} fail={summary['fail']}"
        )
    return 1 if summary["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
