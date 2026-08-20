"""M10.2 runtime-critical config must fail before service startup."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigError, ConfigLoader
from orchestrator.credential_contract import CredentialContractError
from orchestrator.runtime_config_validation import validate_runtime_config
from orchestrator.stream_runtime import StreamRuntimeConfig, build_stream_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
EMBODIMENT_CONFIG = {
    "mid_cooldown_s": 2.0,
    "mid_timeout_s": 1.0,
    "intentional_cooldown_s": 3.0,
    "intentional_lease_ttl_s": 120.0,
    "max_evidence_refs": 4,
    "max_recent_records": 32,
    "max_id_chars": 160,
    "max_gesture_id_chars": 64,
}


class OverrideLoader:
    def __init__(self, overrides: dict[tuple[str, str], object]) -> None:
        self._overrides = overrides

    def get(self, name: str, key: str, default=None):
        if (name, key) == ("animation", "animation.embodiment"):
            return self._overrides.get((name, key), dict(EMBODIMENT_CONFIG))
        return self._overrides.get((name, key), default)


def test_repository_runtime_config_is_valid() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    validated = validate_runtime_config(loader)
    assert validated.dashboard_port == 7860
    assert validated.dashboard_host == "127.0.0.1"
    assert validated.dashboard_control_token_env == "MAI_DASHBOARD_CONTROL_TOKEN"
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
    assert validated.director_room_reaction_cooldown_seconds == 120
    assert validated.director_room_reaction_retry_defer_seconds == 60
    assert validated.director_room_reaction_recent_window == 16
    assert validated.director_room_reaction_similarity_threshold == 0.72
    assert validated.director_room_reaction_max_regenerations == 1
    assert validated.director_speech_dedup_recent_window == 64
    assert validated.director_speech_dedup_similarity_threshold == 0.72
    assert validated.director_speech_dedup_max_regenerations == 1
    assert validated.director_speech_style_recent_window == 12
    assert validated.director_speech_style_formula_openers == (
        "mà", "trời ơi", "ủa", "ơ kìa",
    )
    assert validated.director_speech_style_max_formula_openers == 2
    assert validated.director_speech_style_max_same_opener == 1
    assert validated.director_speech_style_max_questions == 2
    assert validated.director_speech_style_max_sentences == 2
    assert validated.director_speech_style_max_words == 65
    assert validated.director_speech_style_max_regenerations == 1
    assert validated.conversation_summarize_after_moves == 2
    assert validated.manage_llama_process is True


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
            "director", "director.room_reaction.cooldown_seconds", 0,
            "director_room_reaction_cooldown_seconds",
        ),
        (
            "director", "director.room_reaction.recent_window", 0,
            "director_room_reaction_recent_window",
        ),
        (
            "director", "director.room_reaction.similarity_threshold", 0,
            "director_room_reaction_similarity_threshold",
        ),
        (
            "director", "director.room_reaction.max_regenerations", 2,
            "director_room_reaction_max_regenerations",
        ),
        (
            "director", "director.speech_dedup.recent_window", 0,
            "director_speech_dedup_recent_window",
        ),
        (
            "director", "director.speech_dedup.similarity_threshold", 1.1,
            "director_speech_dedup_similarity_threshold",
        ),
        (
            "director", "director.speech_dedup.max_regenerations", 2,
            "director_speech_dedup_max_regenerations",
        ),
        (
            "director", "director.speech_style.recent_window", 0,
            "director_speech_style_recent_window",
        ),
        (
            "director", "director.speech_style.formula_openers", (),
            "director_speech_style_formula_openers",
        ),
        (
            "director", "director.speech_style.max_regenerations", 2,
            "director_speech_style_max_regenerations",
        ),
        (
            "director", "director.speech_style.max_sentences", 0,
            "director_speech_style_max_sentences",
        ),
        (
            "director", "director.speech_style.max_words", 0,
            "director_speech_style_max_words",
        ),
        (
            "conversation", "move_planner.summarize_after_moves", 0,
            "conversation_summarize_after_moves",
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
        ("system", "dashboard.port", "7860", "dashboard_port"),
        ("system", "dashboard.host", "0.0.0.0", "dashboard_host"),
        ("director", "director.tick_seconds", "1.5", "director_tick_seconds"),
        (
            "self_talk", "self_talk.silence_allow_question", "false",
            "self_talk_silence_allow_question",
        ),
        (
            "operations", "health_supervisor.manage_llama_process", "false",
            "manage_llama_process",
        ),
        (
            "self_talk", "self_talk.question_particles", "nhỉ",
            "self_talk_question_particles",
        ),
        (
            "director", "director.speech_style.formula_openers", ["mà", 1],
            "director_speech_style_formula_openers",
        ),
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


def test_speech_style_budget_cannot_exceed_recent_window() -> None:
    with pytest.raises(ConfigError, match="formula budget exceeds recent window"):
        validate_runtime_config(OverrideLoader({
            ("director", "director.speech_style.recent_window"): 2,
            ("director", "director.speech_style.max_formula_openers"): 3,
        }))


@pytest.mark.parametrize(
    "value",
    [
        None,
        {**EMBODIMENT_CONFIG, "extra": 1},
        {key: item for key, item in EMBODIMENT_CONFIG.items() if key != "max_id_chars"},
        {**EMBODIMENT_CONFIG, "intentional_lease_ttl_s": "120"},
        {**EMBODIMENT_CONFIG, "max_recent_records": True},
    ],
)
def test_runtime_rejects_invalid_embodiment_config_before_composition(value: object) -> None:
    with pytest.raises(ConfigError, match="Runtime embodiment config"):
        validate_runtime_config(OverrideLoader({
            ("animation", "animation.embodiment"): value,
        }))


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {("chat_sources", "discord.token_env_var"): "discord-token"},
            "discord.token_env_var",
        ),
        (
            {("capabilities", "external_actions.obs.password_env"): "OBS-PASSWORD"},
            "external_actions.obs.password_env",
        ),
        (
            {("animation", "animation.token_file"): " vts_token.txt "},
            "animation.token_file",
        ),
        (
            {("system", "dashboard.control_token_env"): "dashboard-token"},
            "dashboard.control_token_env",
        ),
        (
            {
                ("chat_sources", "discord.token_env_var"): "SHARED_SECRET",
                ("capabilities", "external_actions.obs.password_env"): "SHARED_SECRET",
            },
            "distinct",
        ),
        (
            {
                ("chat_sources", "discord.token_env_var"): "SHARED_SECRET",
                ("system", "dashboard.control_token_env"): "SHARED_SECRET",
            },
            "distinct",
        ),
    ],
)
def test_runtime_rejects_invalid_credential_references_before_composition(
    overrides: dict[tuple[str, str], object], field: str,
) -> None:
    with pytest.raises(ConfigError, match=field):
        validate_runtime_config(OverrideLoader(overrides))


@pytest.mark.asyncio
async def test_build_rejects_invalid_config_before_composing_services() -> None:
    loader = OverrideLoader({("system", "dashboard.port"): 0})
    with pytest.raises(ConfigError, match="dashboard_port"):
        await build_stream_runtime(
            loader=loader,
            sources=[],
            cfg=StreamRuntimeConfig(),
        )


@pytest.mark.asyncio
async def test_build_requires_dashboard_token_before_composing_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAI_DASHBOARD_CONTROL_TOKEN", raising=False)
    with pytest.raises(CredentialContractError, match="credential_missing"):
        await build_stream_runtime(
            loader=OverrideLoader({}),
            sources=[],
            cfg=StreamRuntimeConfig(enable_dashboard=True),
        )
