"""Fail-fast schema for runtime-critical config boundaries (M10.2)."""
from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from orchestrator.config_loader import ConfigError
from interfaces.cognition import CognitionConfig
from interfaces.execution import ExecutionConfig
from interfaces.turn_kernel import KernelConfig
from orchestrator.credential_contract import (
    validate_runtime_credential_contract,
)
from services.animation.embodiment_policy import EmbodimentPolicyConfig
from services.director.trajectory import TrajectoryConfig
from services.operations.surface import OperationsSurfaceConfig
from services.operations.turn_journal import TurnJournalConfig


def _strict_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"Runtime config không hợp lệ tại {field_name}: phải là list string")
    if any(not isinstance(item, str) for item in value):
        raise ConfigError(
            f"Runtime config không hợp lệ tại {field_name}: item phải là string",
        )
    if any(not item or item != item.strip() for item in value):
        raise ConfigError(
            f"Runtime config không hợp lệ tại {field_name}: item phải trim và không rỗng",
        )
    return tuple(value)


class RuntimeCriticalConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    log_dir: str = Field(min_length=1)
    events_file: str = Field(min_length=1)
    turns_file: str = Field(min_length=1)
    delivery_outcomes_file: str = Field(min_length=1)
    rotation_max_size_mb: int = Field(gt=0)
    rotation_keep_files: int = Field(gt=0)
    logging_buffer_records: int = Field(gt=0)
    director_tick_seconds: float = Field(gt=0)
    director_dead_air_seconds: float = Field(gt=0)
    director_self_talk_cooldown_seconds: float = Field(gt=0)
    director_room_reaction_cooldown_seconds: float = Field(gt=0)
    director_room_reaction_retry_defer_seconds: float = Field(gt=0)
    director_room_reaction_recent_window: int = Field(gt=0)
    director_room_reaction_similarity_threshold: float = Field(gt=0, le=1)
    director_room_reaction_max_regenerations: int = Field(ge=0, le=1)
    director_speech_dedup_recent_window: int = Field(gt=0)
    director_speech_dedup_similarity_threshold: float = Field(gt=0, le=1)
    director_speech_dedup_max_regenerations: int = Field(ge=0, le=1)
    director_speech_style_recent_window: int = Field(gt=0)
    director_speech_style_formula_openers: tuple[str, ...] = Field(min_length=1)
    director_speech_style_max_formula_openers: int = Field(ge=0)
    director_speech_style_max_same_opener: int = Field(ge=0)
    director_speech_style_formula_phrases: tuple[str, ...] = Field(min_length=1)
    director_speech_style_language_integrity_fragments: tuple[str, ...] = Field(
        min_length=1,
    )
    director_speech_style_malformed_token_fragments: tuple[str, ...] = Field(
        min_length=1,
    )
    director_speech_style_malformed_token_allowlist: tuple[str, ...] = Field(
        min_length=1,
    )
    director_speech_style_malformed_mixed_case_min_prefix_chars: int = Field(ge=1)
    director_speech_style_vague_input_max_words: int = Field(ge=0)
    director_speech_style_vague_grounding_forbidden_patterns: tuple[str, ...] = Field(
        min_length=1,
    )
    director_speech_style_semantic_over_inference_patterns: tuple[str, ...] = Field(
        min_length=1,
    )
    director_speech_style_max_questions: int = Field(ge=0)
    director_speech_style_question_endings: tuple[str, ...] = Field(min_length=1)
    director_speech_style_max_sentences: int = Field(gt=0)
    director_speech_style_max_words: int = Field(gt=0)
    director_speech_style_max_regenerations: int = Field(ge=0, le=2)
    conversation_summarize_after_moves: int = Field(gt=0)
    conversation_invite_after_moves: int = Field(gt=0)
    conversation_compare_after_viewer_contributions: int = Field(gt=0)
    self_talk_wait_for_chat_seconds: float = Field(gt=0)
    self_talk_resume_after_chat_seconds: float = Field(ge=0)
    self_talk_min_silence_seconds: float = Field(gt=0)
    self_talk_unavailable_retry_seconds: float = Field(gt=0)
    self_talk_thought_ledger_size: int = Field(gt=0)
    self_talk_silence_repeat_last_n: int = Field(gt=0)
    self_talk_semantic_repeat_threshold: float = Field(ge=0, le=1)
    self_talk_output_repeat_threshold: float = Field(ge=0, le=1)
    self_talk_stage_repeat_threshold: float = Field(ge=0, le=1)
    self_talk_stage_repeat_min_tokens: int = Field(gt=0)
    self_talk_silence_intention: str = Field(min_length=1)
    self_talk_silence_allow_question: bool
    self_talk_question_endings: tuple[str, ...] = Field(min_length=1)
    self_talk_question_starters: tuple[str, ...] = Field(min_length=1)
    self_talk_question_particles: tuple[str, ...] = Field(min_length=1)
    self_talk_max_previous_text_chars: int = Field(gt=0)
    self_talk_lore_sections: tuple[str, ...] = Field(min_length=1)
    self_talk_lore_max_anchor_chars: int = Field(gt=0)
    self_talk_lore_no_repeat_last_n: int = Field(ge=0)
    transaction_max_recent: int = Field(gt=0)
    decision_record_max_recent: int = Field(gt=0)
    decision_record_max_evidence_refs: int = Field(gt=0)
    decision_record_max_label_chars: int = Field(gt=0)
    manage_llama_process: bool
    dashboard_host: str = Field(min_length=1)
    dashboard_control_token_env: str = Field(min_length=1)
    dashboard_port: int = Field(ge=1, le=65535)
    dashboard_push_interval_s: float = Field(gt=0)
    gpu_metrics_command: str = Field(min_length=1)
    gpu_metrics_timeout_s: float = Field(gt=0)
    gpu_metrics_refresh_s: float = Field(gt=0)
    llm_primary_timeout_s: float = Field(gt=0)
    llm_canned_timeout_s: float = Field(gt=0)
    tts_primary_timeout_s: float = Field(gt=0)
    tts_subtitle_timeout_s: float = Field(gt=0)
    tts_startup_timeout_s: float = Field(gt=0)
    tts_health_timeout_s: float = Field(gt=0)

    @field_validator("dashboard_host")
    @classmethod
    def validate_dashboard_loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("dashboard host must be an explicit loopback address")
        return value

    @model_validator(mode="after")
    def validate_speech_style_budgets(self) -> "RuntimeCriticalConfig":
        window = self.director_speech_style_recent_window
        if self.director_speech_style_max_formula_openers > window:
            raise ValueError("speech style formula budget exceeds recent window")
        if self.director_speech_style_max_same_opener > window:
            raise ValueError("speech style same-opener budget exceeds recent window")
        if self.director_speech_style_max_questions > window:
            raise ValueError("speech style question budget exceeds recent window")
        if any(not value.strip() for value in self.director_speech_style_formula_openers):
            raise ValueError("speech style formula openers must be non-empty")
        if any(not value.strip() for value in self.director_speech_style_question_endings):
            raise ValueError("speech style question endings must be non-empty")
        if len(set(self.director_speech_style_formula_phrases)) != len(
            self.director_speech_style_formula_phrases
        ):
            raise ValueError("speech style formula phrases must be unique")
        if len(set(self.director_speech_style_language_integrity_fragments)) != len(
            self.director_speech_style_language_integrity_fragments
        ):
            raise ValueError("speech style language fragments must be unique")
        if len(set(self.director_speech_style_malformed_token_fragments)) != len(
            self.director_speech_style_malformed_token_fragments
        ):
            raise ValueError("speech style malformed token fragments must be unique")
        if len(set(self.director_speech_style_malformed_token_allowlist)) != len(
            self.director_speech_style_malformed_token_allowlist
        ):
            raise ValueError("speech style malformed token allowlist must be unique")
        if len(set(
            self.director_speech_style_vague_grounding_forbidden_patterns
        )) != len(self.director_speech_style_vague_grounding_forbidden_patterns):
            raise ValueError("speech style vague grounding patterns must be unique")
        if len(set(
            self.director_speech_style_semantic_over_inference_patterns
        )) != len(self.director_speech_style_semantic_over_inference_patterns):
            raise ValueError("semantic over-inference patterns must be unique")
        if self.self_talk_silence_repeat_last_n > self.self_talk_thought_ledger_size:
            raise ValueError("self-talk silence repeat window exceeds thought ledger")
        return self


def validate_runtime_config(loader: Any) -> RuntimeCriticalConfig:
    """Validate values whose failure after startup would corrupt operation."""
    try:
        CognitionConfig.from_mapping(loader.section("cognition"))
    except (AttributeError, ValueError) as exc:
        raise ConfigError(f"Runtime cognition config không hợp lệ: {exc}") from exc
    try:
        KernelConfig.from_mapping(loader.section("kernel"))
    except (AttributeError, ValueError) as exc:
        raise ConfigError(f"Runtime kernel config không hợp lệ: {exc}") from exc
    try:
        execution_section = loader.section("execution")
        # Compatibility loaders used by offline tools may not expose new section
        # APIs yet. ConfigLoader itself always registers and validates this file.
        if execution_section:
            ExecutionConfig.from_mapping(execution_section)
    except (AttributeError, ValueError) as exc:
        raise ConfigError(f"Runtime execution config không hợp lệ: {exc}") from exc
    try:
        validate_runtime_credential_contract(loader)
    except ValueError as exc:
        raise ConfigError(f"Runtime credential config không hợp lệ: {exc}") from exc
    try:
        EmbodimentPolicyConfig.from_loader(loader)
    except ValueError as exc:
        raise ConfigError(f"Runtime embodiment config không hợp lệ: {exc}") from exc
    try:
        TrajectoryConfig.from_loader(loader)
    except ValueError as exc:
        raise ConfigError(f"Runtime trajectory config không hợp lệ: {exc}") from exc
    try:
        TurnJournalConfig.from_loader(loader)
        OperationsSurfaceConfig.from_loader(loader)
    except ValueError as exc:
        raise ConfigError(f"Runtime operations config không hợp lệ: {exc}") from exc
    try:
        return RuntimeCriticalConfig(
            log_dir=loader.get("logging", "jsonl.dir", "logs"),
            events_file=loader.get("logging", "jsonl.events_file", "events.jsonl"),
            turns_file=loader.get("logging", "jsonl.turns_file", "turns.jsonl"),
            delivery_outcomes_file=loader.get(
                "logging", "jsonl.delivery_outcomes_file", "delivery_outcomes.jsonl",
            ),
            rotation_max_size_mb=loader.get("logging", "rotation.max_size_mb", 100),
            rotation_keep_files=loader.get("logging", "rotation.keep_files", 5),
            logging_buffer_records=loader.get(
                "logging", "fail_safe.buffer_records", 256,
            ),
            director_tick_seconds=loader.get("kernel", "tick_seconds", 1.5),
            director_dead_air_seconds=loader.get(
                "director", "director.dead_air_seconds", 20.0,
            ),
            director_self_talk_cooldown_seconds=loader.get(
                "director", "director.self_talk_cooldown_seconds", 45.0,
            ),
            director_room_reaction_cooldown_seconds=loader.get(
                "director", "director.room_reaction.cooldown_seconds", 30.0,
            ),
            director_room_reaction_retry_defer_seconds=loader.get(
                "director", "director.room_reaction.retry_defer_seconds", 30.0,
            ),
            director_room_reaction_recent_window=loader.get(
                "director", "director.room_reaction.recent_window", 16,
            ),
            director_room_reaction_similarity_threshold=loader.get(
                "director", "director.room_reaction.similarity_threshold", 0.72,
            ),
            director_room_reaction_max_regenerations=loader.get(
                "director", "director.room_reaction.max_regenerations", 1,
            ),
            director_speech_dedup_recent_window=loader.get(
                "director", "director.speech_dedup.recent_window", 32,
            ),
            director_speech_dedup_similarity_threshold=loader.get(
                "director", "director.speech_dedup.similarity_threshold", 0.72,
            ),
            director_speech_dedup_max_regenerations=loader.get(
                "director", "director.speech_dedup.max_regenerations", 1,
            ),
            director_speech_style_recent_window=loader.get(
                "director", "director.speech_style.recent_window", 12,
            ),
            director_speech_style_formula_openers=_strict_string_tuple(loader.get(
                "director", "director.speech_style.formula_openers",
                ("mà", "trời ơi", "ủa", "ơ kìa"),
            ), "director_speech_style_formula_openers"),
            director_speech_style_max_formula_openers=loader.get(
                "director", "director.speech_style.max_formula_openers", 2,
            ),
            director_speech_style_max_same_opener=loader.get(
                "director", "director.speech_style.max_same_opener", 1,
            ),
            director_speech_style_formula_phrases=_strict_string_tuple(loader.get(
                "director", "director.speech_style.formula_phrases",
                ("làm tớ thấy", "rồi đấy"),
            ), "director_speech_style_formula_phrases"),
            director_speech_style_language_integrity_fragments=_strict_string_tuple(
                loader.get(
                    "director", "director.speech_style.language_integrity_fragments",
                    ("kalau",),
                ),
                "director_speech_style_language_integrity_fragments",
            ),
            director_speech_style_malformed_token_fragments=_strict_string_tuple(
                loader.get(
                    "director", "director.speech_style.malformed_token_fragments",
                    None,
                ),
                "director_speech_style_malformed_token_fragments",
            ),
            director_speech_style_malformed_token_allowlist=_strict_string_tuple(
                loader.get(
                    "director", "director.speech_style.malformed_token_allowlist",
                    None,
                ),
                "director_speech_style_malformed_token_allowlist",
            ),
            director_speech_style_malformed_mixed_case_min_prefix_chars=loader.get(
                "director",
                "director.speech_style.malformed_mixed_case_min_prefix_chars",
                None,
            ),
            director_speech_style_vague_input_max_words=loader.get(
                "director", "director.speech_style.vague_input_max_words", 1,
            ),
            director_speech_style_vague_grounding_forbidden_patterns=(
                _strict_string_tuple(loader.get(
                    "director",
                    "director.speech_style.vague_grounding_forbidden_patterns",
                    ("chắc chắn",),
                ), "director_speech_style_vague_grounding_forbidden_patterns")
            ),
            director_speech_style_semantic_over_inference_patterns=(
                _strict_string_tuple(loader.get(
                    "director",
                    "director.speech_style.semantic_over_inference_patterns",
                    None,
                ), "director_speech_style_semantic_over_inference_patterns")
            ),
            director_speech_style_max_questions=loader.get(
                "director", "director.speech_style.max_questions", 2,
            ),
            director_speech_style_question_endings=_strict_string_tuple(loader.get(
                "director", "director.speech_style.question_endings", ("nhỉ",),
            ), "director_speech_style_question_endings"),
            director_speech_style_max_sentences=loader.get(
                "director", "director.speech_style.max_sentences", 2,
            ),
            director_speech_style_max_words=loader.get(
                "director", "director.speech_style.max_words", 32,
            ),
            director_speech_style_max_regenerations=loader.get(
                "director", "director.speech_style.max_regenerations", 2,
            ),
            conversation_summarize_after_moves=loader.get(
                "conversation", "move_planner.summarize_after_moves", 2,
            ),
            conversation_invite_after_moves=loader.get(
                "conversation", "move_planner.invite_after_moves", 2,
            ),
            conversation_compare_after_viewer_contributions=loader.get(
                "conversation", "move_planner.compare_after_viewer_contributions", 2,
            ),
            self_talk_wait_for_chat_seconds=loader.get(
                "self_talk", "self_talk.wait_for_chat_seconds", 75.0,
            ),
            self_talk_resume_after_chat_seconds=loader.get(
                "self_talk", "self_talk.resume_after_chat_seconds", 12.0,
            ),
            self_talk_min_silence_seconds=loader.get(
                "self_talk", "self_talk.min_silence_seconds", 20.0,
            ),
            self_talk_unavailable_retry_seconds=loader.get(
                "self_talk", "self_talk.unavailable_retry_seconds", 90.0,
            ),
            self_talk_thought_ledger_size=loader.get(
                "self_talk", "self_talk.thought_ledger_size", 32,
            ),
            self_talk_silence_repeat_last_n=loader.get(
                "self_talk", "self_talk.silence_repeat_last_n", 8,
            ),
            self_talk_semantic_repeat_threshold=loader.get(
                "self_talk", "self_talk.semantic_repeat_threshold", 0.72,
            ),
            self_talk_output_repeat_threshold=loader.get(
                "self_talk", "self_talk.output_repeat_threshold", 0.88,
            ),
            self_talk_stage_repeat_threshold=loader.get(
                "self_talk", "self_talk.stage_repeat_threshold", 0.72,
            ),
            self_talk_stage_repeat_min_tokens=loader.get(
                "self_talk", "self_talk.stage_repeat_min_tokens", 4,
            ),
            self_talk_silence_intention=loader.get(
                "self_talk", "self_talk.silence_intention", "silence",
            ),
            self_talk_silence_allow_question=loader.get(
                "self_talk", "self_talk.silence_allow_question", True,
            ),
            self_talk_question_endings=_strict_string_tuple(loader.get(
                "self_talk", "self_talk.question_endings", ("nhỉ",),
            ), "self_talk_question_endings"),
            self_talk_question_starters=_strict_string_tuple(loader.get(
                "self_talk", "self_talk.question_starters", ("ai",),
            ), "self_talk_question_starters"),
            self_talk_question_particles=_strict_string_tuple(loader.get(
                "self_talk", "self_talk.question_particles", ("nhỉ",),
            ), "self_talk_question_particles"),
            self_talk_max_previous_text_chars=loader.get(
                "self_talk", "self_talk.max_previous_text_chars", 280,
            ),
            self_talk_lore_sections=_strict_string_tuple(loader.get(
                "self_talk", "self_talk.lore_material.section_allowlist", ("Thích",),
            ), "self_talk_lore_sections"),
            self_talk_lore_max_anchor_chars=loader.get(
                "self_talk", "self_talk.lore_material.max_anchor_chars", 280,
            ),
            self_talk_lore_no_repeat_last_n=loader.get(
                "self_talk", "self_talk.lore_material.no_repeat_last_n", 6,
            ),
            transaction_max_recent=loader.get(
                "execution", "transactions.max_recent", 256,
            ),
            decision_record_max_recent=loader.get(
                "director", "director.decision_records.max_recent", 256,
            ),
            decision_record_max_evidence_refs=loader.get(
                "director", "director.decision_records.max_evidence_refs", 8,
            ),
            decision_record_max_label_chars=loader.get(
                "director", "director.decision_records.max_label_chars", 120,
            ),
            manage_llama_process=loader.get(
                "operations", "health_supervisor.manage_llama_process", True,
            ),
            dashboard_host=loader.get("system", "dashboard.host", "127.0.0.1"),
            dashboard_control_token_env=loader.get(
                "system", "dashboard.control_token_env", "MAI_DASHBOARD_CONTROL_TOKEN",
            ),
            dashboard_port=loader.get("system", "dashboard.port", 7860),
            dashboard_push_interval_s=loader.get(
                "system", "dashboard.push_interval_s", 1.0,
            ),
            gpu_metrics_command=loader.get(
                "system", "dashboard.gpu_metrics.command", "nvidia-smi",
            ),
            gpu_metrics_timeout_s=loader.get(
                "system", "dashboard.gpu_metrics.timeout_s", 1.0,
            ),
            gpu_metrics_refresh_s=loader.get(
                "system", "dashboard.gpu_metrics.refresh_s", 2.0,
            ),
            llm_primary_timeout_s=loader.get(
                "models", "llm_canned.timeout_primary_s", 5.0,
            ),
            llm_canned_timeout_s=loader.get(
                "models", "llm_canned.timeout_canned_s", 0.1,
            ),
            tts_primary_timeout_s=loader.get(
                "models", "tts.timeout_primary_s", 15.0,
            ),
            tts_subtitle_timeout_s=loader.get(
                "models", "tts.timeout_subtitle_s", 0.5,
            ),
            tts_startup_timeout_s=loader.get(
                "models", "tts.startup_timeout_s", 30.0,
            ),
            tts_health_timeout_s=loader.get(
                "models", "tts.health_timeout_s", 5.0,
            ),
        )
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        field = ".".join(str(part) for part in first.get("loc", ()))
        raise ConfigError(
            f"Runtime config không hợp lệ tại {field}: {first.get('msg', 'invalid')}"
        ) from exc
