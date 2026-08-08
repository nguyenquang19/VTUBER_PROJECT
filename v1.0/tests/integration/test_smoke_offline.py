from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orchestrator.config_loader import ConfigLoader
from scripts.smoke_offline import (
    SmokeStatus,
    check_dashboard,
    check_discord_config,
    check_shutdown_cancellation,
    check_youtube_config,
    main,
    run_smoke,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _loader(config_dir: Path = REPO_ROOT / "config") -> ConfigLoader:
    loader = ConfigLoader(config_dir)
    loader.load_all()
    return loader


def _write_config(tmp_path: Path, chat_sources: dict) -> ConfigLoader:
    (tmp_path / "system.yaml").write_text(
        yaml.safe_dump({"smoke": {"request_timeout_s": 1.0, "shutdown_timeout_s": 1.0}}),
        encoding="utf-8",
    )
    (tmp_path / "chat_sources.yaml").write_text(
        yaml.safe_dump(chat_sources), encoding="utf-8",
    )
    return _loader(tmp_path)


@pytest.mark.asyncio
async def test_repository_smoke_has_no_failures_or_live_connections() -> None:
    results = await run_smoke(_loader())
    assert len(results) == 4
    assert all(result.status is not SmokeStatus.FAIL for result in results)


@pytest.mark.asyncio
async def test_dashboard_runs_in_process_without_binding_port() -> None:
    result = await check_dashboard(_loader())
    assert result.status is SmokeStatus.PASS
    assert "without port bind" in result.detail


@pytest.mark.asyncio
async def test_enabled_platforms_use_offline_clients_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_DISCORD_SECRET", raising=False)
    loader = _write_config(tmp_path, {
        "youtube": {"enabled": True, "video_id": "offline-video", "poll_interval_s": 1.0},
        "discord": {
            "enabled": True,
            "token_env_var": "MISSING_DISCORD_SECRET",
            "channel_ids": [123],
            "queue_maxsize": 10,
        },
    })
    youtube = await check_youtube_config(loader)
    discord = await check_discord_config(loader)
    assert youtube.status is SmokeStatus.PASS
    assert discord.status is SmokeStatus.PASS
    assert "secret not required" in discord.detail


@pytest.mark.asyncio
async def test_invalid_platform_config_reports_actionable_failure(tmp_path: Path) -> None:
    loader = _write_config(tmp_path, {
        "youtube": {"enabled": True, "video_id": "", "poll_interval_s": 1.0},
        "discord": {
            "enabled": True,
            "token_env_var": "DISCORD_BOT_TOKEN",
            "channel_ids": [-1],
            "queue_maxsize": 10,
        },
    })
    youtube = await check_youtube_config(loader)
    discord = await check_discord_config(loader)
    assert youtube.status is SmokeStatus.FAIL
    assert "video_id" in youtube.detail
    assert discord.status is SmokeStatus.FAIL
    assert "positive" in discord.detail


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_tasks_within_configured_timeout() -> None:
    result = await check_shutdown_cancellation(_loader())
    assert result.status is SmokeStatus.PASS
    assert "cancelled cleanly" in result.detail


def test_json_cli_output_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--output-format", "Json", "--config-dir", str(REPO_ROOT / "config")])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["summary"]["fail"] == 0
    assert len(payload["checks"]) == 4
