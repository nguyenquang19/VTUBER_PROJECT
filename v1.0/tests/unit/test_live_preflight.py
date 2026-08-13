from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator.config_loader import ConfigLoader
from scripts.live_preflight import run_preflight


def _loader(tmp_path: Path) -> ConfigLoader:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    files = {
        "models.yaml": {
            "llm_main": {
                "binary": str(tmp_path / "llama-server.exe"),
                "model_path": str(tmp_path / "model.gguf"),
                "host": "127.0.0.1", "port": 8080,
            },
            "tts": {"reference_audio": str(tmp_path / "voice.wav")},
            "tts_fallback": {
                "enabled": True, "require_delivery": True,
                "output_file": str(tmp_path / "logs" / "subtitle.txt"),
            },
        },
        "features.yaml": {"features": {
            "mood_v2_prompt": {"enabled": True},
            "action_transactions": {"enabled": True},
            "decision_records": {"enabled": True},
        }},
        "chat_sources.yaml": {"discord": {
            "token_env_var": "DISCORD_BOT_TOKEN", "channel_ids": [123],
        }},
        "system.yaml": {"app": {"version": "1.4.2"}},
    }
    for name, content in files.items():
        (config_dir / name).write_text(yaml.safe_dump(content), encoding="utf-8")
    for name in ("llama-server.exe", "model.gguf", "voice.wav"):
        (tmp_path / name).write_bytes(b"ok")
    (tmp_path / "logs").mkdir()
    loader = ConfigLoader(config_dir, required=())
    loader.load_all()
    return loader


def test_youtube_preflight_ready_without_running_server(tmp_path: Path) -> None:
    report = run_preflight(
        _loader(tmp_path), platform_name="youtube", video_id="video123",
        check_server_health=False, repo_root=tmp_path,
        os_name="Windows", python_version=(3, 11, 9),
    )
    assert report["ready"] is True
    assert report["marker"] == "mai_live_preflight"
    assert report["sanitized"] is True
    assert report["product_version"] == "1.4.2"
    assert all("token" not in item["detail"].lower() or "variable" in item["detail"].lower()
               for item in report["checks"])


def test_discord_preflight_requires_token_but_never_exposes_it(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    missing = run_preflight(
        loader, platform_name="discord", check_server_health=False,
        repo_root=tmp_path, environ={}, os_name="Windows", python_version=(3, 11, 0),
    )
    assert missing["ready"] is False
    secret = "super-secret-token"
    ready = run_preflight(
        loader, platform_name="discord", check_server_health=False,
        repo_root=tmp_path, environ={"DISCORD_BOT_TOKEN": secret},
        os_name="Windows", python_version=(3, 11, 0),
    )
    assert ready["ready"] is True
    assert secret not in str(ready)


def test_preflight_fails_when_transaction_contract_is_disabled(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader._data["features"]["features"]["action_transactions"]["enabled"] = False
    report = run_preflight(
        loader, platform_name="youtube", video_id="x", check_server_health=False,
        repo_root=tmp_path, os_name="Windows", python_version=(3, 11, 0),
    )
    assert report["ready"] is False
    assert next(item for item in report["checks"] if item["name"] == "transactions")["passed"] is False
