"""Fail-fast checks before starting a Mai live session on Windows."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.credential_contract import (  # noqa: E402
    RuntimeCredentialReferences,
    inspect_environment_secret,
    validate_runtime_credential_contract,
)


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    detail: str
    blocking: bool = True


def _resolve_path(value: str, repo_root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _write_json_atomic(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _health_check(host: str, port: int, timeout_s: float) -> PreflightCheck:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            passed = 200 <= int(response.status) < 300
        return PreflightCheck("llama_health", passed, f"{url} returned HTTP {response.status}")
    except (OSError, urllib.error.URLError) as exc:
        return PreflightCheck("llama_health", False, f"{url} unavailable: {type(exc).__name__}")


def run_preflight(
    loader: ConfigLoader,
    *,
    platform_name: str,
    video_id: str | None = None,
    with_discord: bool = False,
    check_server_health: bool = True,
    repo_root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
    python_version: Sequence[int] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return a sanitized readiness report; credential values are never included."""
    env = environ if environ is not None else os.environ
    current_os = os_name or platform.system()
    version = tuple(python_version or sys.version_info[:3])
    checks: list[PreflightCheck] = [
        PreflightCheck("windows", current_os == "Windows", f"operating system: {current_os}"),
        PreflightCheck(
            "python", version >= (3, 11),
            f"Python {'.'.join(str(part) for part in version)} (requires 3.11+)",
        ),
    ]
    credential_references: RuntimeCredentialReferences | None
    try:
        credential_references = validate_runtime_credential_contract(loader)
    except ValueError as exc:
        credential_references = None
        checks.append(PreflightCheck(
            "credential_contract",
            False,
            f"credential references invalid: {exc}",
        ))
    else:
        checks.append(PreflightCheck(
            "credential_contract",
            True,
            "credential environment variables and VTS token file valid: "
            f"{credential_references.discord_token}, {credential_references.obs_password}",
        ))

    binary = _resolve_path(str(loader.get("models", "llm_main.binary", "")), repo_root)
    model = _resolve_path(str(loader.get("models", "llm_main.model_path", "")), repo_root)
    reference = _resolve_path(str(loader.get("models", "tts.reference_audio", "")), repo_root)
    checks.extend([
        PreflightCheck("llama_binary", binary.is_file(), f"llama.cpp binary: {binary}"),
        PreflightCheck("llm_model", model.is_file(), f"GGUF model: {model}"),
        PreflightCheck("tts_reference", reference.is_file(), f"TTS reference: {reference}"),
        PreflightCheck(
            "hybrid_mood", bool(loader.get("features", "features.mood_v2_prompt.enabled", False)),
            "Hybrid v1 mood + v2 turn policy enabled",
        ),
        PreflightCheck(
            "transactions", bool(loader.get("features", "features.action_transactions.enabled", False)),
            "Transactional delivery enabled",
        ),
        PreflightCheck(
            "decision_records", bool(loader.get("features", "features.decision_records.enabled", False)),
            "Decision records enabled",
        ),
        PreflightCheck(
            "subtitle_fallback",
            bool(loader.get("models", "tts_fallback.enabled", False))
            and bool(loader.get("models", "tts_fallback.require_delivery", False)),
            "Subtitle fallback requires a real delivery sink",
        ),
    ])

    subtitle = _resolve_path(
        str(loader.get("models", "tts_fallback.output_file", "logs/live/subtitle.txt")), repo_root,
    )
    parent = subtitle.parent
    writable_parent = parent.exists() and parent.is_dir() and os.access(parent, os.W_OK)
    if not parent.exists():
        existing_parent = next((item for item in parent.parents if item.exists()), None)
        writable_parent = existing_parent is not None and os.access(existing_parent, os.W_OK)
    checks.append(PreflightCheck(
        "subtitle_path", writable_parent,
        f"subtitle overlay target: {subtitle}",
    ))

    normalized_platform = platform_name.strip().lower()
    if normalized_platform not in {"youtube", "discord"}:
        checks.append(PreflightCheck("platform", False, "platform must be youtube or discord"))
    else:
        checks.append(PreflightCheck("platform", True, f"primary platform: {normalized_platform}"))

    needs_youtube = normalized_platform == "youtube" or bool(video_id)
    if needs_youtube:
        checks.append(PreflightCheck(
            "youtube_video", bool((video_id or "").strip()),
            "YouTube video ID supplied" if video_id else "YouTube video ID is missing",
        ))

    needs_discord = normalized_platform == "discord" or with_discord
    if needs_discord:
        channel_ids = list(loader.get("chat_sources", "discord.channel_ids", []))
        if credential_references is None:
            discord_present = False
            discord_detail = "Discord credential unavailable: invalid credential contract"
        else:
            discord = inspect_environment_secret(
                env, credential_references.discord_token,
            )
            discord_present = discord.present
            discord_detail = (
                f"Discord credential {discord.state.value} in environment variable "
                f"{discord.reference}"
            )
        checks.extend([
            PreflightCheck(
                "discord_token", discord_present, discord_detail,
            ),
            PreflightCheck(
                "discord_channels", bool(channel_ids),
                f"Discord configured channel count: {len(channel_ids)}",
                blocking=False,
            ),
        ])

    obs_enabled = loader.get(
        "features", "features.obs_scene_executor.enabled", False,
    ) is True
    if obs_enabled:
        if credential_references is None:
            obs_present = False
            obs_detail = "OBS credential unavailable: invalid credential contract"
        else:
            obs = inspect_environment_secret(
                env, credential_references.obs_password,
            )
            obs_present = obs.present
            obs_detail = (
                f"OBS credential {obs.state.value} in environment variable {obs.reference}"
            )
        checks.append(PreflightCheck("obs_password", obs_present, obs_detail))

    if check_server_health:
        checks.append(_health_check(
            str(loader.get("models", "llm_main.host", "127.0.0.1")),
            int(loader.get("models", "llm_main.port", 8080)),
            min(float(loader.get("models", "llm_main.health_timeout_s", 30.0)), 5.0),
        ))
    else:
        checks.append(PreflightCheck(
            "llama_health", True,
            "deferred: StreamRuntime starts llama.cpp and performs the blocking health check",
        ))

    ready = all(check.passed for check in checks if check.blocking)
    generated_at = now_utc or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return {
        "schema_version": 1,
        "marker": "mai_live_preflight",
        "sanitized": True,
        "product_version": str(loader.get("system", "app.version", "")),
        "generated_at_utc": generated_at.astimezone(timezone.utc).isoformat(),
        "ready": ready,
        "platform": normalized_platform,
        "checks": [asdict(check) for check in checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Mai live prerequisites")
    parser.add_argument("--platform", required=True, choices=("youtube", "discord"))
    parser.add_argument("--video")
    parser.add_argument("--with-discord", action="store_true")
    parser.add_argument("--skip-server-health", action="store_true")
    parser.add_argument("--config-dir", default=str(REPO_ROOT / "config"))
    parser.add_argument("--output")
    args = parser.parse_args()

    loader = ConfigLoader(Path(args.config_dir))
    loader.load_all()
    report = run_preflight(
        loader,
        platform_name=args.platform,
        video_id=args.video,
        with_discord=args.with_discord,
        check_server_health=not args.skip_server_health,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output)
        _write_json_atomic(output, rendered)
    print(rendered, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
