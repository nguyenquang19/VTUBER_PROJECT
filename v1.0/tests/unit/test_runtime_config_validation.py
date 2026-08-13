"""M10.2 runtime-critical config must fail before service startup."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigError, ConfigLoader
from orchestrator.runtime_config_validation import validate_runtime_config
from orchestrator.stream_runtime import StreamRuntimeConfig, build_stream_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]


class OverrideLoader:
    def __init__(self, overrides: dict[tuple[str, str], object]) -> None:
        self._overrides = overrides

    def get(self, name: str, key: str, default=None):
        return self._overrides.get((name, key), default)


def test_repository_runtime_config_is_valid() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    validated = validate_runtime_config(loader)
    assert validated.dashboard_port == 7860
    assert validated.logging_buffer_records > 0
    assert validated.self_talk_thought_ledger_size == 32
    assert validated.self_talk_output_repeat_threshold == 0.88
    assert validated.self_talk_stage_repeat_threshold == 0.72
    assert validated.self_talk_stage_repeat_min_tokens == 4
    assert validated.self_talk_silence_allow_question is True
    assert validated.self_talk_question_particles == ("nhỉ", "hả", "ư")
    assert validated.self_talk_lore_max_anchor_chars == 280
    assert validated.self_talk_lore_no_repeat_last_n == 6
    assert "Thích" in validated.self_talk_lore_sections


@pytest.mark.parametrize(
    ("name", "key", "value", "field"),
    [
        ("logging", "fail_safe.buffer_records", 0, "logging_buffer_records"),
        ("director", "director.tick_seconds", -1, "director_tick_seconds"),
        (
            "director", "director.self_talk_cooldown_seconds", 0,
            "director_self_talk_cooldown_seconds",
        ),
        (
            "director", "director.decision_records.max_recent", 0,
            "decision_record_max_recent",
        ),
        (
            "self_talk", "self_talk.semantic_repeat_threshold", 1.1,
            "self_talk_semantic_repeat_threshold",
        ),
        (
            "self_talk", "self_talk.stage_repeat_threshold", 1.1,
            "self_talk_stage_repeat_threshold",
        ),
        (
            "self_talk", "self_talk.lore_material.max_anchor_chars", 0,
            "self_talk_lore_max_anchor_chars",
        ),
        (
            "self_talk", "self_talk.lore_material.no_repeat_last_n", -1,
            "self_talk_lore_no_repeat_last_n",
        ),
        ("system", "dashboard.port", 70000, "dashboard_port"),
        ("system", "dashboard.gpu_metrics.timeout_s", 0, "gpu_metrics_timeout_s"),
        ("models", "tts.timeout_primary_s", 0, "tts_primary_timeout_s"),
        ("models", "tts.startup_timeout_s", 0, "tts_startup_timeout_s"),
    ],
)
def test_invalid_critical_value_names_the_failed_field(
    name: str, key: str, value: object, field: str,
) -> None:
    with pytest.raises(ConfigError, match=field):
        validate_runtime_config(OverrideLoader({(name, key): value}))


@pytest.mark.asyncio
async def test_build_rejects_invalid_config_before_composing_services() -> None:
    loader = OverrideLoader({("system", "dashboard.port"): 0})
    with pytest.raises(ConfigError, match="dashboard_port"):
        await build_stream_runtime(
            loader=loader,
            sources=[],
            cfg=StreamRuntimeConfig(),
        )
