"""Prometheus metrics collector for runtime services.

GPU/VRAM production metrics are sampled from nvidia-smi; unavailable data is
reported explicitly and is never replaced with synthetic values.

Dùng CollectorRegistry riêng (không phải global REGISTRY) để test tạo nhiều
instance không bị "Duplicated timeseries".
"""
from __future__ import annotations

import math
import subprocess
import time
from typing import Any, Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


COGNITIVE_CONTRACT_REJECTION_REASONS = frozenset({
    "invalid_type", "invalid_schema", "invalid_bound", "invalid_time",
    "invalid_mode", "invalid_combination", "invalid_reference",
})
COGNITIVE_FEATURE_TOGGLE_OUTCOMES = frozenset({
    "disabled", "enable_rejected", "stopped",
})
COGNITIVE_CONTEXT_BUILD_OUTCOMES = frozenset({
    "ready", "degraded", "unavailable", "rejected",
})
COGNITIVE_CONTEXT_SOURCES = frozenset({
    "hard_state", "world", "self", "capability", "agent_state",
    "goal", "thread", "memory", "delivery",
})
COGNITIVE_CONTEXT_SOURCE_OUTCOMES = frozenset({"accepted", "omitted", "failed"})
COGNITIVE_FOCUS_OUTCOMES = frozenset({
    "present", "absent", "stale", "mismatch", "invalid",
})
COGNITIVE_SNAPSHOT_KINDS = frozenset({"context", "focus"})
COGNITIVE_OPPORTUNITY_KINDS = frozenset({
    "CHAT_INPUT", "DONATION_OR_OPERATOR", "VERIFIED_OUTCOME",
    "CONVERSATION_CONTINUATION", "PROACTIVE_READY",
})
COGNITIVE_OPPORTUNITY_OUTCOMES = frozenset({
    "offered", "debounced", "blocked", "superseded",
})
COGNITIVE_BRAIN_OUTCOMES = frozenset({
    "PROPOSED", "SKIPPED_DISABLED", "SKIPPED_HARD_HOLD",
    "SKIPPED_NO_CHANGE", "SKIPPED_BUSY", "SUPERSEDED", "STALE",
    "PREFLIGHT_REJECTED", "PREEMPTED", "CANCELLED", "TIMEOUT",
    "PARSE_REJECTED", "SCHEMA_REJECTED", "SERVICE_ERROR",
})
COGNITIVE_BRAIN_MODES = frozenset({"WAIT", "SPEAK"})
COGNITIVE_AB_STRATA = frozenset({
    "direct_chat", "continuation", "proactive_wait", "vague_unknown",
    "unsupported_claim", "repetition_contradiction", "priority_interrupt",
    "adversarial_grounding",
})
COGNITIVE_AB_OUTCOMES = frozenset({
    "COMPLETED", "PREFLIGHT_REJECTED", "TIMEOUT", "PARSE_REJECTED",
    "SCHEMA_REJECTED", "FILTER_REJECTED", "STALE", "CANCELLED",
    "SERVICE_ERROR",
})
COGNITIVE_AB_ROLES = frozenset({"compatibility", "brain"})
COGNITIVE_AB_PAIR_OUTCOMES = frozenset({"built", "finalized", "failed"})
LLM_WORKLOAD_CLASS_PAIRS = frozenset({"live_shadow"})


class MetricsCollector:
    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        gpu_query_runner: Callable[[str, float], str] | None = None,
    ) -> None:
        self.registry = registry or CollectorRegistry()

        # --- Metric thật theo spec 5.3 ---
        self.ttfa_seconds = Histogram(
            "mai_pipeline_ttfa_seconds",
            "Time to first audio playback",
            buckets=[0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0],
            registry=self.registry,
        )
        self.trigger_decisions_total = Counter(
            "mai_trigger_decisions_total",
            "Trigger manager decisions",
            ["trigger_type", "decision"],
            registry=self.registry,
        )
        self.state_transitions_total = Counter(
            "mai_state_transitions_total",
            "State machine transitions",
            ["from_state", "to_state"],
            registry=self.registry,
        )

        # --- LLM metrics thật (Phase 1, dashboard 1.F) ---
        self.llm_ttft_seconds = Histogram(
            "mai_llm_ttft_seconds",
            "LLM time-to-first-token",
            buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 2.0, 5.0],
            registry=self.registry,
        )
        self.llm_decode_tps = Gauge(
            "mai_llm_decode_tps", "LLM decode tokens/sec (lần gần nhất)",
            registry=self.registry,
        )
        self.llm_requests_total = Counter(
            "mai_llm_requests_total", "Tổng lượt LLM turn",
            registry=self.registry,
        )
        self.llm_fallback_total = Counter(
            "mai_llm_fallback_total", "Số lần rơi xuống canned (fallback level>0)",
            registry=self.registry,
        )
        self.llm_parse_total = Counter(
            "mai_llm_parse_total", "Kết quả parse mood block", ["result"],
            registry=self.registry,
        )
        self.llm_workload_overlap_total_c = Counter(
            "llm_workload_overlap_total", "LLM workload class overlap after grace",
            ["classes"], registry=self.registry,
        )
        self._llm_workload_overlap: dict[str, int] = {}
        self._last_ttft_ms: float | None = None
        self._last_decode_tps: float | None = None
        self._parse_ok = 0
        self._parse_fail = 0
        self._fallback = 0
        self._llm_requests = 0

        # --- Filter metrics (Phase 3, dashboard 3.C) ---
        self.filter_checks_total_c = Counter(
            "mai_filter_checks_total", "Số lượt filter check",
            registry=self.registry,
        )
        self.filter_hits_total_c = Counter(
            "mai_filter_hits_total", "Số lượt filter bắt vi phạm", ["category"],
            registry=self.registry,
        )
        self.filter_regen_total_c = Counter(
            "mai_filter_regen_total", "Kết quả regen", ["result"],  # recovered/exhausted/none
            registry=self.registry,
        )
        self._filter_checks = 0
        self._filter_hits = 0
        self._filter_by_cat: dict[str, int] = {}
        self._filter_regen_recovered = 0
        self._filter_regen_exhausted = 0
        self._filter_fail_open = 0
        self._filter_recent: list[dict[str, Any]] = []  # tối đa 10 mục gần nhất

        # --- TTS metrics (Phase 4, dashboard 4.E) ---
        self.tts_ttfa_seconds = Histogram(
            "mai_tts_ttfa_seconds", "TTS time-to-first-audio (end-to-end pipeline)",
            buckets=[0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0],
            registry=self.registry,
        )
        self.tts_turns_total = Counter(
            "mai_tts_turns_total", "Tổng lượt TTS pipeline",
            registry=self.registry,
        )
        self.tts_subtitle_total_c = Counter(
            "mai_tts_subtitle_fallback_total", "Số lượt rơi xuống subtitle fallback (level>0)",
            registry=self.registry,
        )
        self._tts_turns = 0
        self._tts_subtitle_fallback = 0
        self._tts_last_ttfa_ms: float | None = None

        # --- Agent state / grounded ledger metrics (Master Plan M1.2) ---
        self.agent_events_total_c = Counter(
            "mai_agent_events_total",
            "Grounded agent events accepted or dropped",
            ["outcome", "reason"],
            registry=self.registry,
        )
        self._agent_events: dict[tuple[str, str], int] = {}

        # --- GoalManager metrics (Master Plan M2) ---
        self.goal_events_total_c = Counter(
            "mai_agent_goals_total",
            "Goal lifecycle and validation outcomes",
            ["outcome", "reason"],
            registry=self.registry,
        )
        self.goal_active_age_seconds_g = Gauge(
            "mai_agent_goal_active_age_seconds",
            "Age of the current active goal",
            registry=self.registry,
        )
        self._goal_events: dict[tuple[str, str], int] = {}
        self.intention_events_total_c = Counter(
            "mai_agent_intentions_total",
            "Short-intention lifecycle and authoritative action outcomes",
            ["outcome", "reason"],
            registry=self.registry,
        )
        self.intention_active_age_seconds_g = Gauge(
            "mai_agent_intention_active_age_seconds",
            "Age of the current active short intention",
            registry=self.registry,
        )
        self.intention_current_step_g = Gauge(
            "mai_agent_intention_current_step",
            "One-based current short-intention step, or zero when inactive",
            registry=self.registry,
        )
        self._intention_events: dict[tuple[str, str], int] = {}

        # --- Director Action Arbiter metrics (Master Plan M3) ---
        self.director_actions_total_c = Counter(
            "mai_director_actions_total",
            "Director arbiter decisions",
            ["action", "reason"],
            registry=self.registry,
        )
        self._director_actions: dict[tuple[str, str], int] = {}
        self.action_transactions_total_c = Counter(
            "mai_action_transactions_total",
            "Delivery-aware Director action transaction state changes",
            ["state"],
            registry=self.registry,
        )
        self._action_transactions: dict[str, int] = {}
        self.director_decision_records_total_c = Counter(
            "mai_director_decision_records_total",
            "Versioned Director decision record outcomes",
            ["action", "outcome"],
            registry=self.registry,
        )
        self._director_decision_records: dict[tuple[str, str], int] = {}
        self.operator_dashboard_views_total_c = Counter(
            "mai_operator_dashboard_views_total",
            "Operator dashboard page views by version",
            ["version"],
            registry=self.registry,
        )
        self._operator_dashboard_views: dict[str, int] = {}
        self.logging_sink_errors_total_c = Counter(
            "mai_logging_sink_errors_total",
            "Fail-safe JSONL sink errors that did not escape into runtime",
            ["sink", "error"],
            registry=self.registry,
        )
        self._logging_sink_errors: dict[tuple[str, str], int] = {}

        # --- Conversation continuity metrics (Master Plan M4) ---
        self.thread_events_total_c = Counter(
            "mai_conversation_threads_total",
            "Open conversation thread lifecycle",
            ["outcome", "kind"],
            registry=self.registry,
        )
        self._thread_events: dict[tuple[str, str], int] = {}
        self.session_recap_chars_g = Gauge(
            "mai_session_recap_chars",
            "Characters retained in the bounded session recap",
            registry=self.registry,
        )
        self.conversation_context_chars_h = Histogram(
            "mai_conversation_context_chars",
            "Rendered grounded conversation context size in characters",
            buckets=[200, 400, 600, 800, 1000, 1200, 1400, 1800],
            registry=self.registry,
        )
        self._context_chars_last = 0
        self.conversation_repairs_total_c = Counter(
            "mai_conversation_repairs_total",
            "Conversation repair decisions",
            ["kind"],
            registry=self.registry,
        )
        self._repair_counts: dict[str, int] = {}
        self.grounded_recall_rate_g = Gauge(
            "mai_grounded_recall_rate",
            "Share of continuity recall checks backed by the expected grounded evidence",
            registry=self.registry,
        )
        self._grounded_recall_matched = 0
        self._grounded_recall_total = 0
        self.mood_adjustments_total_c = Counter(
            "mai_mood_behavior_adjustments_total",
            "Mood or tone-flag adjustments applied to agenda or Director scoring",
            ["target", "reason"],
            registry=self.registry,
        )
        self._mood_adjustments: dict[tuple[str, str], int] = {}
        self.affect_v2_events_total_c = Counter(
            "mai_affect_v2_events_total",
            "Mood v2 shadow observations by style and outcome",
            ["style", "outcome"],
            registry=self.registry,
        )
        self._affect_v2_events: dict[tuple[str, str], int] = {}
        self.mood_ab_reviews_total_c = Counter(
            "mai_mood_ab_reviews_total",
            "Blind Mood v1/v2 review outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self._mood_ab_reviews: dict[str, int] = {}
        self.proactive_candidates_total_c = Counter(
            "mai_proactive_candidates_total",
            "Grounded proactive candidate selection and completion",
            ["source", "outcome"],
            registry=self.registry,
        )
        self._proactive_candidates: dict[tuple[str, str], int] = {}
        self.host_behaviors_total_c = Counter(
            "mai_host_behaviors_total",
            "Structured host behavior selections",
            ["behavior", "reason"],
            registry=self.registry,
        )
        self._host_behaviors: dict[tuple[str, str], int] = {}
        self.natural_timing_total_c = Counter(
            "mai_natural_timing_decisions_total",
            "TTFA-calibrated pacing decisions",
            ["turn_kind", "reason"],
            registry=self.registry,
        )
        self.natural_timing_ttfa_ms_g = Gauge(
            "mai_natural_timing_ttfa_ms",
            "Latest real TTS TTFA sample used by natural timing",
            registry=self.registry,
        )
        self._natural_timing: dict[tuple[str, str], int] = {}
        self.relationship_events_total_c = Counter(
            "mai_relationship_events_total",
            "Privacy-safe relationship lifecycle outcomes",
            ["outcome", "reason"],
            registry=self.registry,
        )
        self._relationship_events: dict[tuple[str, str], int] = {}
        self.eval_scenarios_total_c = Counter(
            "mai_eval_scenarios_total",
            "Versioned evaluation scenario outcomes",
            ["group", "outcome"],
            registry=self.registry,
        )
        self._eval_scenarios: dict[tuple[str, str], int] = {}
        self.eval_acceptance_runs_total_c = Counter(
            "mai_eval_acceptance_runs_total",
            "Deterministic text acceptance run outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self._eval_acceptance_runs: dict[str, int] = {}
        self.human_like_reviews_total_c = Counter(
            "mai_human_like_reviews_total",
            "Strict MAI-HLC blind review workflow outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self._human_like_reviews: dict[str, int] = {}
        self.trajectory_records_total_c = Counter(
            "mai_trajectory_records_total",
            "Structured Director trajectory lifecycle and replay outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self._trajectory_records: dict[str, int] = {}
        self.closed_loop_canary_total_c = Counter(
            "mai_closed_loop_canary_total",
            "Operator-triggered closed-loop canary outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self._closed_loop_canary: dict[str, int] = {}
        self.health_supervisor_actions_total_c = Counter(
            "mai_health_supervisor_actions_total",
            "Bounded health supervisor observations and recovery actions",
            ["service_id", "action"],
            registry=self.registry,
        )
        self._health_supervisor_actions: dict[tuple[str, str], int] = {}
        self.shutdown_steps_total_c = Counter(
            "mai_shutdown_steps_total",
            "Graceful shutdown step outcomes",
            ["step", "outcome"],
            registry=self.registry,
        )
        self._shutdown_steps: dict[tuple[str, str], int] = {}
        self.operator_controls_total_c = Counter(
            "mai_operator_controls_total",
            "Audited live operator control outcomes",
            ["action", "outcome"],
            registry=self.registry,
        )
        self._operator_controls: dict[tuple[str, str], int] = {}
        self.emergency_controls_total_c = Counter(
            "mai_emergency_controls_total",
            "Emergency latch control outcomes",
            ["action", "outcome"],
            registry=self.registry,
        )
        self._emergency_controls: dict[tuple[str, str], int] = {}
        self.incidents_total_c = Counter(
            "mai_incidents_total", "Versioned live incident events",
            ["severity", "status"], registry=self.registry,
        )
        self._incidents: dict[tuple[str, str], int] = {}
        # --- World Model shadow metrics (Phase 2; never decision inputs) ---
        self.world_model_events_total_c = Counter(
            "mai_world_model_events_total", "World Model shadow reducer outcomes",
            ["outcome", "reason"], registry=self.registry,
        )
        self.world_model_state_entries_g = Gauge(
            "mai_world_model_state_entries", "Fresh entries in the World Model shadow",
            registry=self.registry,
        )
        self.world_model_stale_evictions_total_c = Counter(
            "mai_world_model_stale_evictions_total", "World Model stale entries evicted",
            registry=self.registry,
        )
        self._world_model_events: dict[tuple[str, str], int] = {}
        self._world_model_stale_evictions = 0
        # --- Perception ingress metrics (Phase 10; no decision side effects) ---
        self.perception_events_total_c = Counter(
            "mai_perception_events_total", "Canonical perception ingress and adapter outcomes",
            ["outcome", "source"], registry=self.registry,
        )
        self.perception_recent_events_g = Gauge(
            "mai_perception_recent_events", "Bounded retained canonical perception events",
            registry=self.registry,
        )
        self._perception_events: dict[tuple[str, str], int] = {}
        # --- Self Model projection metrics (Phase 3; read-only) ---
        self.self_model_snapshots_total_c = Counter(
            "mai_self_model_snapshots_total", "Self Model projection outcomes",
            ["outcome"], registry=self.registry,
        )
        self.self_model_degraded_g = Gauge(
            "mai_self_model_degraded", "1 when the latest Self Model projection is degraded",
            registry=self.registry,
        )
        self.self_model_recent_actions_g = Gauge(
            "mai_self_model_recent_actions", "Bounded recent action IDs in the latest Self Model projection",
            registry=self.registry,
        )
        self._self_model_snapshots: dict[str, int] = {}

        # --- Capability registry metrics (Phase 4; availability only) ---
        self.capability_availability_checks_total_c = Counter(
            "mai_capability_availability_checks_total",
            "Capability registry availability checks", ["reason_code"],
            registry=self.registry,
        )
        self.capability_available_g = Gauge(
            "mai_capability_available", "Capabilities available in the latest check",
            registry=self.registry,
        )
        self.capability_declarations_g = Gauge(
            "mai_capability_declarations", "Declared capabilities in the registry",
            registry=self.registry,
        )
        self._capability_availability_checks: dict[str, int] = {}

        # --- General action mock loop metrics (Phase 5; mock-only) ---
        self.action_mock_outcomes_total_c = Counter(
            "mai_action_mock_outcomes_total", "Mock action closed-loop outcomes",
            ["outcome"], registry=self.registry,
        )
        self.action_mock_world_projection_inconsistencies_total_c = Counter(
            "mai_action_mock_world_projection_inconsistencies_total",
            "Committed mock actions whose World projection failed",
            registry=self.registry,
        )
        self._action_mock_outcomes: dict[str, int] = {}
        self._action_mock_world_projection_inconsistencies = 0
        self.director_v2_shadow_total_c = Counter(
            "mai_director_v2_shadow_total", "Director V2 shadow proposal outcomes",
            ["outcome"], registry=self.registry,
        )
        self.director_v2_shadow_retained_g = Gauge(
            "mai_director_v2_shadow_retained", "Bounded Director V2 shadow records",
            registry=self.registry,
        )
        self._director_v2_shadow: dict[str, int] = {}
        self.director_v2_takeover_total_c = Counter(
            "mai_director_v2_takeover_total", "Director V2 takeover agreement outcomes",
            ["stage", "reason"], registry=self.registry,
        )
        self.director_v2_takeover_retained_g = Gauge(
            "mai_director_v2_takeover_retained", "Bounded Director V2 takeover records",
            registry=self.registry,
        )
        self._director_v2_takeover: dict[tuple[str, str], int] = {}

        # --- Cognitive Brain MCB-1 contract/disabled-feature metrics ---
        self.cognitive_contract_rejected_total_c = Counter(
            "cognitive_contract_rejected_total",
            "Strict Cognitive Brain contract rejection outcomes",
            ["reason"], registry=self.registry,
        )
        self.cognitive_feature_toggle_total_c = Counter(
            "cognitive_feature_toggle_total",
            "Cognitive Brain disabled/blocked feature outcomes",
            ["outcome"], registry=self.registry,
        )
        self._cognitive_contract_rejected: dict[str, int] = {}
        self._cognitive_feature_toggle: dict[str, int] = {}
        self.cognitive_context_build_total_c = Counter(
            "cognitive_context_build_total",
            "Read-only Cognitive Context build outcomes",
            ["outcome"], registry=self.registry,
        )
        self.cognitive_context_source_total_c = Counter(
            "cognitive_context_source_total",
            "Bounded Cognitive Context source adaptation outcomes",
            ["source", "outcome"], registry=self.registry,
        )
        self.cognitive_context_build_duration_seconds_h = Histogram(
            "cognitive_context_build_duration_seconds",
            "Read-only Cognitive Context build duration",
            buckets=[0.001, 0.003, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
            registry=self.registry,
        )
        self.cognitive_context_serialized_chars_h = Histogram(
            "cognitive_context_serialized_chars",
            "Canonical serialized Cognitive Context size",
            buckets=[1024, 2048, 4096, 8192, 16384, 32768],
            registry=self.registry,
        )
        self.cognitive_focus_projection_total_c = Counter(
            "cognitive_focus_projection_total",
            "Read-only Focus projection outcomes",
            ["outcome"], registry=self.registry,
        )
        self.cognitive_snapshot_evicted_total_c = Counter(
            "cognitive_snapshot_evicted_total",
            "Bounded in-memory cognition snapshot evictions",
            ["kind"], registry=self.registry,
        )
        self._cognitive_context_build: dict[str, int] = {}
        self._cognitive_context_source: dict[tuple[str, str], int] = {}
        self._cognitive_focus_projection: dict[str, int] = {}
        self._cognitive_snapshot_evicted: dict[str, int] = {}
        self.cognitive_opportunity_total_c = Counter(
            "cognitive_opportunity_total", "Bounded Brain opportunity outcomes",
            ["kind", "outcome"], registry=self.registry,
        )
        self.cognitive_brain_queue_depth_g = Gauge(
            "cognitive_brain_queue_depth", "Current bounded Brain queue depth",
            registry=self.registry,
        )
        self.cognitive_brain_queue_wait_seconds_h = Histogram(
            "cognitive_brain_queue_wait_seconds", "Brain shadow queue wait",
            buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
            registry=self.registry,
        )
        self.cognitive_brain_request_total_c = Counter(
            "cognitive_brain_request_total", "Brain shadow request outcomes",
            ["outcome"], registry=self.registry,
        )
        self.cognitive_brain_ttft_seconds_h = Histogram(
            "cognitive_brain_ttft_seconds", "Brain shadow time to first token",
            ["outcome"], buckets=[0.05, 0.1, 0.2, 0.5, 1, 2, 4, 6],
            registry=self.registry,
        )
        self.cognitive_brain_generation_seconds_h = Histogram(
            "cognitive_brain_generation_seconds", "Brain shadow generation duration",
            ["outcome"], buckets=[0.1, 0.25, 0.5, 1, 2, 4, 6, 8],
            registry=self.registry,
        )
        self.cognitive_brain_input_tokens_h = Histogram(
            "cognitive_brain_input_tokens", "Brain shadow exact input tokens",
            buckets=[128, 256, 512, 1024, 2048, 4096, 8192],
            registry=self.registry,
        )
        self.cognitive_brain_output_tokens_h = Histogram(
            "cognitive_brain_output_tokens", "Brain shadow output tokens",
            buckets=[8, 16, 32, 64, 96, 128, 192], registry=self.registry,
        )
        self.cognitive_brain_turn_total_c = Counter(
            "cognitive_brain_turn_total", "Validated Brain shadow turn modes",
            ["mode"], registry=self.registry,
        )
        self.cognitive_brain_preemption_total_c = Counter(
            "cognitive_brain_preemption_total", "Brain shadow preemption outcomes",
            ["outcome"], registry=self.registry,
        )
        self._cognitive_opportunities: dict[tuple[str, str], int] = {}
        self._cognitive_brain_requests: dict[str, int] = {}
        self._cognitive_brain_turns: dict[str, int] = {}
        self.cognitive_ab_case_total_c = Counter(
            "cognitive_ab_case_total", "Offline cognitive A/B case outcomes",
            ["stratum", "outcome"], registry=self.registry,
        )
        self.cognitive_ab_candidate_total_c = Counter(
            "cognitive_ab_candidate_total", "Offline cognitive A/B candidate outcomes",
            ["role", "outcome"], registry=self.registry,
        )
        self.cognitive_ab_mode_total_c = Counter(
            "cognitive_ab_mode_total", "Offline cognitive A/B candidate modes",
            ["role", "mode"], registry=self.registry,
        )
        self.cognitive_ab_pair_total_c = Counter(
            "cognitive_ab_pair_total", "Offline cognitive A/B review artifact outcomes",
            ["outcome"], registry=self.registry,
        )
        self._cognitive_ab_cases: dict[tuple[str, str], int] = {}
        self._cognitive_ab_candidates: dict[tuple[str, str], int] = {}
        self._cognitive_ab_modes: dict[tuple[str, str], int] = {}
        self._cognitive_ab_pairs: dict[str, int] = {}

        # --- Real NVIDIA device metrics for the operator dashboard ---
        self.gpu_util = Gauge(
            "mai_gpu_util_percent", "NVIDIA GPU utilization",
            registry=self.registry,
        )
        self.vram_used_mb = Gauge(
            "mai_vram_used_mb", "NVIDIA VRAM currently used",
            registry=self.registry,
        )
        self.vram_total_mb = Gauge(
            "mai_vram_total_mb", "NVIDIA VRAM total",
            registry=self.registry,
        )
        self.gpu_metrics_available = Gauge(
            "mai_gpu_metrics_available", "1 when the latest NVIDIA query succeeded",
            registry=self.registry,
        )
        self.gpu_query_failures = Counter(
            "mai_gpu_query_failures_total", "Failed NVIDIA metric queries",
            registry=self.registry,
        )
        self._gpu_query_runner = gpu_query_runner
        self._gpu_util_percent: float | None = None
        self._vram_used_mb: float | None = None
        self._vram_total_mb: float | None = None
        self._gpu_available = False
        self._gpu_last_error = "not_sampled"
        self._gpu_last_attempt = 0.0

    # ---------- recorders (service thật gọi ở phase sau) ----------

    def record_state_transition(self, from_state: str, to_state: str) -> None:
        self.state_transitions_total.labels(from_state=from_state, to_state=to_state).inc()

    def record_cognitive_contract_rejected(self, reason: str) -> None:
        if reason not in COGNITIVE_CONTRACT_REJECTION_REASONS:
            raise ValueError("unsupported cognitive contract rejection reason")
        self._cognitive_contract_rejected[reason] = (
            self._cognitive_contract_rejected.get(reason, 0) + 1
        )
        self.cognitive_contract_rejected_total_c.labels(reason=reason).inc()

    def record_cognitive_feature_toggle(self, outcome: str) -> None:
        if outcome not in COGNITIVE_FEATURE_TOGGLE_OUTCOMES:
            raise ValueError("unsupported cognitive feature toggle outcome")
        self._cognitive_feature_toggle[outcome] = (
            self._cognitive_feature_toggle.get(outcome, 0) + 1
        )
        self.cognitive_feature_toggle_total_c.labels(outcome=outcome).inc()

    def cognition_snapshot(self) -> dict[str, dict[str, int]]:
        return {
            "contract_rejected": dict(sorted(self._cognitive_contract_rejected.items())),
            "feature_toggle": dict(sorted(self._cognitive_feature_toggle.items())),
        }

    def record_cognitive_context_build(self, outcome: str) -> None:
        if outcome not in COGNITIVE_CONTEXT_BUILD_OUTCOMES:
            raise ValueError("unsupported cognitive context build outcome")
        self._cognitive_context_build[outcome] = (
            self._cognitive_context_build.get(outcome, 0) + 1
        )
        self.cognitive_context_build_total_c.labels(outcome=outcome).inc()

    def record_cognitive_context_source(self, source: str, outcome: str) -> None:
        if source not in COGNITIVE_CONTEXT_SOURCES:
            raise ValueError("unsupported cognitive context source")
        if outcome not in COGNITIVE_CONTEXT_SOURCE_OUTCOMES:
            raise ValueError("unsupported cognitive context source outcome")
        key = (source, outcome)
        self._cognitive_context_source[key] = self._cognitive_context_source.get(key, 0) + 1
        self.cognitive_context_source_total_c.labels(source=source, outcome=outcome).inc()

    def observe_cognitive_context_build_duration(self, seconds: float) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or seconds < 0
        ):
            raise ValueError("cognitive context duration must be non-negative")
        self.cognitive_context_build_duration_seconds_h.observe(float(seconds))

    def observe_cognitive_context_serialized_chars(self, chars: int) -> None:
        if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
            raise ValueError("cognitive context serialized chars must be non-negative")
        self.cognitive_context_serialized_chars_h.observe(chars)

    def record_cognitive_focus_projection(self, outcome: str) -> None:
        if outcome not in COGNITIVE_FOCUS_OUTCOMES:
            raise ValueError("unsupported cognitive focus outcome")
        self._cognitive_focus_projection[outcome] = (
            self._cognitive_focus_projection.get(outcome, 0) + 1
        )
        self.cognitive_focus_projection_total_c.labels(outcome=outcome).inc()

    def record_cognitive_snapshot_evicted(self, kind: str, count: int = 1) -> None:
        if kind not in COGNITIVE_SNAPSHOT_KINDS:
            raise ValueError("unsupported cognitive snapshot kind")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("cognitive snapshot eviction count must be positive")
        self._cognitive_snapshot_evicted[kind] = (
            self._cognitive_snapshot_evicted.get(kind, 0) + count
        )
        self.cognitive_snapshot_evicted_total_c.labels(kind=kind).inc(count)

    def cognition_context_snapshot(self) -> dict[str, dict[str, int]]:
        return {
            "build": dict(sorted(self._cognitive_context_build.items())),
            "source": {
                f"{source}:{outcome}": count
                for (source, outcome), count in sorted(self._cognitive_context_source.items())
            },
            "focus": dict(sorted(self._cognitive_focus_projection.items())),
            "evicted": dict(sorted(self._cognitive_snapshot_evicted.items())),
        }

    def record_cognitive_brain_opportunity(self, kind: str, outcome: str) -> None:
        if kind not in COGNITIVE_OPPORTUNITY_KINDS:
            raise ValueError("unsupported cognitive opportunity kind")
        if outcome not in COGNITIVE_OPPORTUNITY_OUTCOMES:
            raise ValueError("unsupported cognitive opportunity outcome")
        key = (kind, outcome)
        self._cognitive_opportunities[key] = self._cognitive_opportunities.get(key, 0) + 1
        self.cognitive_opportunity_total_c.labels(kind=kind, outcome=outcome).inc()

    def set_cognitive_brain_queue_depth(self, depth: int) -> None:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth not in (0, 1):
            raise ValueError("cognitive Brain queue depth must be zero or one")
        self.cognitive_brain_queue_depth_g.set(depth)

    def observe_cognitive_brain_queue_wait(self, seconds: float) -> None:
        self._observe_non_negative(seconds, self.cognitive_brain_queue_wait_seconds_h)

    def record_cognitive_brain_request(self, outcome: str) -> None:
        if outcome not in COGNITIVE_BRAIN_OUTCOMES:
            raise ValueError("unsupported cognitive Brain outcome")
        self._cognitive_brain_requests[outcome] = self._cognitive_brain_requests.get(outcome, 0) + 1
        self.cognitive_brain_request_total_c.labels(outcome=outcome).inc()
        if outcome in {"PREEMPTED", "CANCELLED"}:
            self.cognitive_brain_preemption_total_c.labels(outcome=outcome).inc()

    def observe_cognitive_brain_generation(self, outcome: str, seconds: float) -> None:
        if outcome not in COGNITIVE_BRAIN_OUTCOMES:
            raise ValueError("unsupported cognitive Brain outcome")
        self._observe_non_negative(
            seconds, self.cognitive_brain_generation_seconds_h.labels(outcome=outcome),
        )

    def observe_cognitive_brain_ttft(self, outcome: str, seconds: float) -> None:
        if outcome not in COGNITIVE_BRAIN_OUTCOMES:
            raise ValueError("unsupported cognitive Brain outcome")
        self._observe_non_negative(
            seconds, self.cognitive_brain_ttft_seconds_h.labels(outcome=outcome),
        )

    def observe_cognitive_brain_tokens(self, input_tokens: int, output_tokens: int) -> None:
        for name, value in (("input", input_tokens), ("output", output_tokens)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"cognitive Brain {name} tokens must be non-negative")
        self.cognitive_brain_input_tokens_h.observe(input_tokens)
        self.cognitive_brain_output_tokens_h.observe(output_tokens)

    def record_cognitive_brain_turn(self, mode: str) -> None:
        if mode not in COGNITIVE_BRAIN_MODES:
            raise ValueError("unsupported cognitive Brain mode")
        self._cognitive_brain_turns[mode] = self._cognitive_brain_turns.get(mode, 0) + 1
        self.cognitive_brain_turn_total_c.labels(mode=mode).inc()

    def cognition_brain_snapshot(self) -> dict[str, Any]:
        return {
            "opportunities": {
                f"{kind}:{outcome}": count
                for (kind, outcome), count in sorted(self._cognitive_opportunities.items())
            },
            "requests": dict(sorted(self._cognitive_brain_requests.items())),
            "turns": dict(sorted(self._cognitive_brain_turns.items())),
        }

    def record_cognitive_ab_case(self, stratum: str, outcome: str) -> None:
        if stratum not in COGNITIVE_AB_STRATA or outcome != "validated":
            raise ValueError("unsupported cognitive A/B case metric")
        key = (stratum, outcome)
        self._cognitive_ab_cases[key] = self._cognitive_ab_cases.get(key, 0) + 1
        self.cognitive_ab_case_total_c.labels(stratum=stratum, outcome=outcome).inc()

    def record_cognitive_ab_candidate(self, role: str, outcome: str) -> None:
        if role not in COGNITIVE_AB_ROLES or outcome not in COGNITIVE_AB_OUTCOMES:
            raise ValueError("unsupported cognitive A/B candidate metric")
        key = (role, outcome)
        self._cognitive_ab_candidates[key] = self._cognitive_ab_candidates.get(key, 0) + 1
        self.cognitive_ab_candidate_total_c.labels(role=role, outcome=outcome).inc()

    def record_cognitive_ab_mode(self, role: str, mode: str) -> None:
        if role not in COGNITIVE_AB_ROLES or mode not in COGNITIVE_BRAIN_MODES:
            raise ValueError("unsupported cognitive A/B mode metric")
        key = (role, mode)
        self._cognitive_ab_modes[key] = self._cognitive_ab_modes.get(key, 0) + 1
        self.cognitive_ab_mode_total_c.labels(role=role, mode=mode).inc()

    def record_cognitive_ab_pair(self, outcome: str) -> None:
        if outcome not in COGNITIVE_AB_PAIR_OUTCOMES:
            raise ValueError("unsupported cognitive A/B pair metric")
        self._cognitive_ab_pairs[outcome] = self._cognitive_ab_pairs.get(outcome, 0) + 1
        self.cognitive_ab_pair_total_c.labels(outcome=outcome).inc()

    def cognition_ab_snapshot(self) -> dict[str, dict[str, int]]:
        return {
            "cases": {
                f"{stratum}:{outcome}": count
                for (stratum, outcome), count in sorted(self._cognitive_ab_cases.items())
            },
            "candidates": {
                f"{role}:{outcome}": count
                for (role, outcome), count in sorted(self._cognitive_ab_candidates.items())
            },
            "modes": {
                f"{role}:{mode}": count
                for (role, mode), count in sorted(self._cognitive_ab_modes.items())
            },
            "pairs": dict(sorted(self._cognitive_ab_pairs.items())),
        }

    @staticmethod
    def _observe_non_negative(value: float, histogram: Any) -> None:
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or value < 0
        ):
            raise ValueError("metric observation must be finite and non-negative")
        histogram.observe(float(value))

    def record_director_action(self, action: str, reason: str) -> None:
        key = (str(action), str(reason))
        self._director_actions[key] = self._director_actions.get(key, 0) + 1
        self.director_actions_total_c.labels(action=key[0], reason=key[1]).inc()

    def record_thread_event(self, outcome: str, kind: str) -> None:
        key = (str(outcome), str(kind))
        self._thread_events[key] = self._thread_events.get(key, 0) + 1
        self.thread_events_total_c.labels(outcome=key[0], kind=key[1]).inc()

    def thread_snapshot(self) -> dict[str, int]:
        return {
            f"{outcome}:{kind}": count
            for (outcome, kind), count in sorted(self._thread_events.items())
        }

    def set_session_recap_chars(self, chars: int) -> None:
        self.session_recap_chars_g.set(max(0, int(chars)))

    def observe_context_chars(self, chars: int) -> None:
        self._context_chars_last = max(0, int(chars))
        self.conversation_context_chars_h.observe(self._context_chars_last)

    def record_repair(self, kind: str) -> None:
        key = str(kind)
        self._repair_counts[key] = self._repair_counts.get(key, 0) + 1
        self.conversation_repairs_total_c.labels(kind=key).inc()

    def set_grounded_recall_rate(self, matched: int, total: int) -> None:
        matched_value = max(0, int(matched))
        total_value = max(0, int(total))
        if matched_value > total_value:
            raise ValueError("grounded recall matches cannot exceed total checks")
        self._grounded_recall_matched = matched_value
        self._grounded_recall_total = total_value
        rate = matched_value / total_value if total_value else 0.0
        self.grounded_recall_rate_g.set(rate)

    def continuity_snapshot(self) -> dict[str, Any]:
        total = self._grounded_recall_total
        return {
            "context_chars_last": self._context_chars_last,
            "repairs": dict(sorted(self._repair_counts.items())),
            "grounded_recall_matched": self._grounded_recall_matched,
            "grounded_recall_total": total,
            "grounded_recall_rate": (
                self._grounded_recall_matched / total if total else 0.0
            ),
        }

    def record_mood_adjustment(self, target: str, reason: str) -> None:
        key = (str(target), str(reason))
        self._mood_adjustments[key] = self._mood_adjustments.get(key, 0) + 1
        self.mood_adjustments_total_c.labels(target=key[0], reason=key[1]).inc()

    def record_affect_v2_event(self, style: str, outcome: str) -> None:
        key = (str(style), str(outcome))
        self._affect_v2_events[key] = self._affect_v2_events.get(key, 0) + 1
        self.affect_v2_events_total_c.labels(style=key[0], outcome=key[1]).inc()

    def affect_v2_snapshot(self) -> dict[str, int]:
        return {
            f"{style}:{outcome}": count
            for (style, outcome), count in sorted(self._affect_v2_events.items())
        }

    def record_mood_ab_review(self, outcome: str) -> None:
        key = str(outcome)
        self._mood_ab_reviews[key] = self._mood_ab_reviews.get(key, 0) + 1
        self.mood_ab_reviews_total_c.labels(outcome=key).inc()

    def mood_ab_review_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._mood_ab_reviews.items()))

    def record_human_like_review(self, outcome: str) -> None:
        key = str(outcome)
        self._human_like_reviews[key] = self._human_like_reviews.get(key, 0) + 1
        self.human_like_reviews_total_c.labels(outcome=key).inc()

    def human_like_review_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._human_like_reviews.items()))

    def record_trajectory(self, outcome: str) -> None:
        key = str(outcome)
        self._trajectory_records[key] = self._trajectory_records.get(key, 0) + 1
        self.trajectory_records_total_c.labels(outcome=key).inc()

    def trajectory_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._trajectory_records.items()))

    def record_closed_loop_canary(self, outcome: str) -> None:
        key = str(outcome)
        self._closed_loop_canary[key] = self._closed_loop_canary.get(key, 0) + 1
        self.closed_loop_canary_total_c.labels(outcome=key).inc()

    def closed_loop_canary_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._closed_loop_canary.items()))

    def record_action_transaction(self, state: str) -> None:
        key = str(state)
        self._action_transactions[key] = self._action_transactions.get(key, 0) + 1
        self.action_transactions_total_c.labels(state=key).inc()

    def record_director_decision_record(self, action: str, outcome: str) -> None:
        key = (str(action), str(outcome))
        self._director_decision_records[key] = (
            self._director_decision_records.get(key, 0) + 1
        )
        self.director_decision_records_total_c.labels(
            action=key[0], outcome=key[1],
        ).inc()

    def record_logging_sink_error(self, sink: str, error: str) -> None:
        key = (str(sink), str(error))
        self._logging_sink_errors[key] = self._logging_sink_errors.get(key, 0) + 1
        self.logging_sink_errors_total_c.labels(sink=key[0], error=key[1]).inc()

    def logging_sink_snapshot(self) -> dict[str, int]:
        return {
            f"{sink}:{error}": count
            for (sink, error), count in sorted(self._logging_sink_errors.items())
        }

    def action_transaction_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._action_transactions.items()))

    def director_decision_record_snapshot(self) -> dict[str, int]:
        return {
            f"{action}:{outcome}": count
            for (action, outcome), count in sorted(self._director_decision_records.items())
        }

    def record_operator_dashboard_view(self, version: str) -> None:
        key = str(version)
        self._operator_dashboard_views[key] = self._operator_dashboard_views.get(key, 0) + 1
        self.operator_dashboard_views_total_c.labels(version=key).inc()

    def operator_dashboard_view_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._operator_dashboard_views.items()))

    def mood_adjustment_snapshot(self) -> dict[str, int]:
        return {
            f"{target}:{reason}": count
            for (target, reason), count in sorted(self._mood_adjustments.items())
        }

    def record_proactive_candidate(self, source: str, outcome: str) -> None:
        key = (str(source), str(outcome))
        self._proactive_candidates[key] = self._proactive_candidates.get(key, 0) + 1
        self.proactive_candidates_total_c.labels(source=key[0], outcome=key[1]).inc()

    def proactive_candidate_snapshot(self) -> dict[str, int]:
        return {
            f"{source}:{outcome}": count
            for (source, outcome), count in sorted(self._proactive_candidates.items())
        }

    def record_host_behavior(self, behavior: str, reason: str) -> None:
        key = (str(behavior), str(reason))
        self._host_behaviors[key] = self._host_behaviors.get(key, 0) + 1
        self.host_behaviors_total_c.labels(behavior=key[0], reason=key[1]).inc()

    def host_behavior_snapshot(self) -> dict[str, int]:
        return {
            f"{behavior}:{reason}": count
            for (behavior, reason), count in sorted(self._host_behaviors.items())
        }

    def record_natural_timing(self, turn_kind: str, reason: str) -> None:
        key = (str(turn_kind), str(reason))
        self._natural_timing[key] = self._natural_timing.get(key, 0) + 1
        self.natural_timing_total_c.labels(turn_kind=key[0], reason=key[1]).inc()

    def observe_natural_timing_ttfa(self, ttfa_ms: float) -> None:
        self.natural_timing_ttfa_ms_g.set(max(0.0, float(ttfa_ms)))

    def natural_timing_snapshot(self) -> dict[str, int]:
        return {
            f"{kind}:{reason}": count
            for (kind, reason), count in sorted(self._natural_timing.items())
        }

    def record_relationship_event(self, outcome: str, reason: str) -> None:
        key = (str(outcome), str(reason))
        self._relationship_events[key] = self._relationship_events.get(key, 0) + 1
        self.relationship_events_total_c.labels(outcome=key[0], reason=key[1]).inc()

    def relationship_snapshot(self) -> dict[str, int]:
        return {
            f"{outcome}:{reason}": count
            for (outcome, reason), count in sorted(self._relationship_events.items())
        }

    def record_eval_scenario(self, group: str, outcome: str) -> None:
        key = (str(group), str(outcome))
        self._eval_scenarios[key] = self._eval_scenarios.get(key, 0) + 1
        self.eval_scenarios_total_c.labels(group=key[0], outcome=key[1]).inc()

    def eval_scenario_snapshot(self) -> dict[str, int]:
        return {
            f"{group}:{outcome}": count
            for (group, outcome), count in sorted(self._eval_scenarios.items())
        }

    def record_eval_acceptance_run(self, outcome: str) -> None:
        key = str(outcome)
        self._eval_acceptance_runs[key] = self._eval_acceptance_runs.get(key, 0) + 1
        self.eval_acceptance_runs_total_c.labels(outcome=key).inc()

    def eval_acceptance_snapshot(self) -> dict[str, int]:
        return dict(sorted(self._eval_acceptance_runs.items()))

    def record_health_supervisor_action(self, service_id: str, action: str) -> None:
        key = (str(service_id), str(action))
        self._health_supervisor_actions[key] = self._health_supervisor_actions.get(key, 0) + 1
        self.health_supervisor_actions_total_c.labels(
            service_id=key[0], action=key[1],
        ).inc()

    def health_supervisor_snapshot(self) -> dict[str, int]:
        return {
            f"{service_id}:{action}": count
            for (service_id, action), count in sorted(self._health_supervisor_actions.items())
        }

    def record_shutdown_step(self, step: str, outcome: str) -> None:
        key = (str(step), str(outcome))
        self._shutdown_steps[key] = self._shutdown_steps.get(key, 0) + 1
        self.shutdown_steps_total_c.labels(step=key[0], outcome=key[1]).inc()

    def shutdown_snapshot(self) -> dict[str, int]:
        return {
            f"{step}:{outcome}": count
            for (step, outcome), count in sorted(self._shutdown_steps.items())
        }

    def record_operator_control(self, action: str, outcome: str) -> None:
        key = (str(action), str(outcome))
        self._operator_controls[key] = self._operator_controls.get(key, 0) + 1
        self.operator_controls_total_c.labels(action=key[0], outcome=key[1]).inc()

    def operator_control_snapshot(self) -> dict[str, int]:
        return {
            f"{action}:{outcome}": count
            for (action, outcome), count in sorted(self._operator_controls.items())
        }

    def record_emergency_control(self, action: str, outcome: str) -> None:
        key = (str(action), str(outcome))
        self._emergency_controls[key] = self._emergency_controls.get(key, 0) + 1
        self.emergency_controls_total_c.labels(action=key[0], outcome=key[1]).inc()

    def emergency_control_snapshot(self) -> dict[str, int]:
        return {
            f"{action}:{outcome}": count
            for (action, outcome), count in sorted(self._emergency_controls.items())
        }

    def record_incident(self, severity: str, status: str) -> None:
        key = (str(severity), str(status))
        self._incidents[key] = self._incidents.get(key, 0) + 1
        self.incidents_total_c.labels(severity=key[0], status=key[1]).inc()

    def incident_snapshot(self) -> dict[str, int]:
        return {
            f"{severity}:{status}": count
            for (severity, status), count in sorted(self._incidents.items())
        }

    def director_action_snapshot(self) -> dict[str, int]:
        return {
            f"{action}:{reason}": count
            for (action, reason), count in sorted(self._director_actions.items())
        }

    def record_trigger_decision(self, trigger_type: str, decision: str) -> None:
        self.trigger_decisions_total.labels(trigger_type=trigger_type, decision=decision).inc()

    def observe_ttfa(self, seconds: float) -> None:
        self.ttfa_seconds.observe(seconds)

    def record_llm_turn(
        self,
        ttft_ms: float | None,
        decode_tps: float | None,
        parse_ok: bool,
        level_used: int,
    ) -> None:
        """1 lượt LLM xong (LLMTurnRunner gọi). Cập nhật metric cho dashboard."""
        self._llm_requests += 1
        self.llm_requests_total.inc()
        if ttft_ms is not None:
            self._last_ttft_ms = ttft_ms
            self.llm_ttft_seconds.observe(ttft_ms / 1000.0)
        if decode_tps is not None:
            self._last_decode_tps = decode_tps
            self.llm_decode_tps.set(decode_tps)
        if parse_ok:
            self._parse_ok += 1
            self.llm_parse_total.labels(result="ok").inc()
        else:
            self._parse_fail += 1
            self.llm_parse_total.labels(result="fail").inc()
        if level_used > 0:
            self._fallback += 1
            self.llm_fallback_total.inc()

    def record_filter_check(
        self,
        passed: bool,
        categories: list[str] | None = None,
        action: str = "allow",
        fail_open: bool = False,
    ) -> None:
        """1 lượt filter check (LLMTurnRunner gọi qua last_filter_verdict). 3.C dashboard."""
        self._filter_checks += 1
        self.filter_checks_total_c.inc()
        if fail_open:
            self._filter_fail_open += 1
        if not passed:
            self._filter_hits += 1
            for c in categories or []:
                self._filter_by_cat[c] = self._filter_by_cat.get(c, 0) + 1
                self.filter_hits_total_c.labels(category=c).inc()
            self._filter_recent.append({"categories": list(categories or []), "action": action})
            if len(self._filter_recent) > 10:
                del self._filter_recent[: len(self._filter_recent) - 10]

    def record_filter_regeneration(self, outcome: str) -> None:
        """Record one filter regeneration outcome for Prometheus."""
        if outcome not in {"none", "recovered", "exhausted"}:
            raise ValueError(f"unknown filter regeneration outcome: {outcome}")
        self.filter_regen_total_c.labels(result=outcome).inc()
        if outcome == "recovered":
            self._filter_regen_recovered += 1
        elif outcome == "exhausted":
            self._filter_regen_exhausted += 1

    def filter_snapshot(self) -> dict[str, Any]:
        total = self._filter_checks
        hit_rate = round(100.0 * self._filter_hits / total, 1) if total else None
        return {
            "checks_total": self._filter_checks,
            "hits_total": self._filter_hits,
            "hit_rate_percent": hit_rate,
            "by_category": dict(self._filter_by_cat),
            "fail_open_total": self._filter_fail_open,
            "recent": list(reversed(self._filter_recent)),  # mới nhất trước
        }

    def record_tts_turn(self, ttfa_ms: float | None, level_used: int) -> None:
        """1 lượt TTSPipeline.speak xong. 4.E dashboard đọc."""
        self._tts_turns += 1
        self.tts_turns_total.inc()
        if ttfa_ms is not None:
            self._tts_last_ttfa_ms = ttfa_ms
            self.tts_ttfa_seconds.observe(ttfa_ms / 1000.0)
            # Dùng lại pipeline TTFA histogram tổng (5.3) khi có
            try:
                self.ttfa_seconds.observe(ttfa_ms / 1000.0)
            except Exception:
                pass
        if level_used > 0:
            self._tts_subtitle_fallback += 1
            self.tts_subtitle_total_c.inc()

    def tts_snapshot(self) -> dict[str, Any]:
        return {
            "turns_total": self._tts_turns,
            "last_ttfa_ms": round(self._tts_last_ttfa_ms, 1) if self._tts_last_ttfa_ms is not None else None,
            "subtitle_fallback_total": self._tts_subtitle_fallback,
        }

    def llm_snapshot(self) -> dict[str, Any]:
        total = self._parse_ok + self._parse_fail
        rate = round(100.0 * self._parse_ok / total, 1) if total else None
        return {
            "last_ttft_ms": round(self._last_ttft_ms, 1) if self._last_ttft_ms is not None else None,
            "last_decode_tps": round(self._last_decode_tps, 1) if self._last_decode_tps is not None else None,
            "requests_total": self._llm_requests,
            "fallback_total": self._fallback,
            "parse_ok": self._parse_ok,
            "parse_total": total,
            "parse_rate_percent": rate,
        }

    def record_agent_event(self, outcome: str, reason: str) -> None:
        key = (str(outcome), str(reason))
        self._agent_events[key] = self._agent_events.get(key, 0) + 1
        self.agent_events_total_c.labels(outcome=key[0], reason=key[1]).inc()

    def record_llm_workload_overlap(self, classes: str) -> None:
        if classes not in LLM_WORKLOAD_CLASS_PAIRS:
            raise ValueError("unsupported LLM workload overlap classes")
        self._llm_workload_overlap[classes] = self._llm_workload_overlap.get(classes, 0) + 1
        self.llm_workload_overlap_total_c.labels(classes=classes).inc()

    def agent_snapshot(self) -> dict[str, Any]:
        accepted = sum(
            count for (outcome, _reason), count in self._agent_events.items()
            if outcome == "accepted"
        )
        dropped = sum(
            count for (outcome, _reason), count in self._agent_events.items()
            if outcome == "dropped"
        )
        return {
            "accepted_total": accepted,
            "dropped_total": dropped,
            "dropped_by_reason": {
                reason: count
                for (outcome, reason), count in sorted(self._agent_events.items())
                if outcome == "dropped"
            },
        }

    def record_action_mock_outcome(self, outcome: str) -> None:
        key = str(outcome)
        self._action_mock_outcomes[key] = self._action_mock_outcomes.get(key, 0) + 1
        self.action_mock_outcomes_total_c.labels(outcome=key).inc()

    def action_mock_snapshot(self) -> dict[str, Any]:
        return {
            "outcomes": dict(sorted(self._action_mock_outcomes.items())),
            "world_projection_inconsistencies": (
                self._action_mock_world_projection_inconsistencies
            ),
        }

    def record_action_mock_world_projection_inconsistency(self) -> None:
        self._action_mock_world_projection_inconsistencies += 1
        self.action_mock_world_projection_inconsistencies_total_c.inc()

    def record_director_v2_shadow(self, outcome: str, retained: int) -> None:
        key = str(outcome)
        self._director_v2_shadow[key] = self._director_v2_shadow.get(key, 0) + 1
        self.director_v2_shadow_total_c.labels(outcome=key).inc()
        self.director_v2_shadow_retained_g.set(max(0, int(retained)))

    def director_v2_shadow_snapshot(self) -> dict[str, Any]:
        return {"outcomes": dict(sorted(self._director_v2_shadow.items()))}
    def record_director_v2_takeover(self, stage: str, reason: str, retained: int) -> None:
        key = (str(stage), str(reason))
        self._director_v2_takeover[key] = self._director_v2_takeover.get(key, 0) + 1
        self.director_v2_takeover_total_c.labels(stage=key[0], reason=key[1]).inc()
        self.director_v2_takeover_retained_g.set(max(0, int(retained)))

    def director_v2_takeover_snapshot(self) -> dict[str, Any]:
        return {
            f"{stage}:{reason}": count
            for (stage, reason), count in sorted(self._director_v2_takeover.items())
        }
    def record_capability_availability(
        self, reason_code: str, available: bool, declarations: int,
    ) -> None:
        reason = str(reason_code)
        self._capability_availability_checks[reason] = (
            self._capability_availability_checks.get(reason, 0) + 1
        )
        self.capability_availability_checks_total_c.labels(reason_code=reason).inc()
        self.capability_declarations_g.set(max(0, int(declarations)))

    def set_capability_registry_counts(self, declarations: int, available: int) -> None:
        self.capability_declarations_g.set(max(0, int(declarations)))
        self.capability_available_g.set(max(0, int(available)))

    def capability_registry_snapshot(self) -> dict[str, Any]:
        return {"checks": dict(sorted(self._capability_availability_checks.items()))}

    def record_self_model_snapshot(self, outcome: str, degraded: bool, recent_actions: int) -> None:
        key = str(outcome)
        self._self_model_snapshots[key] = self._self_model_snapshots.get(key, 0) + 1
        self.self_model_snapshots_total_c.labels(outcome=key).inc()
        self.self_model_degraded_g.set(1 if degraded else 0)
        self.self_model_recent_actions_g.set(max(0, int(recent_actions)))

    def self_model_snapshot(self) -> dict[str, Any]:
        return {"snapshots": dict(sorted(self._self_model_snapshots.items()))}
    def record_world_model_event(self, outcome: str, reason: str) -> None:
        key = (str(outcome), str(reason))
        self._world_model_events[key] = self._world_model_events.get(key, 0) + 1
        self.world_model_events_total_c.labels(outcome=key[0], reason=key[1]).inc()

    def set_world_model_entries(self, entries: int) -> None:
        self.world_model_state_entries_g.set(max(0, int(entries)))

    def record_world_model_eviction(self, count: int) -> None:
        count = max(0, int(count))
        if count:
            self._world_model_stale_evictions += count
            self.world_model_stale_evictions_total_c.inc(count)

    def world_model_snapshot(self) -> dict[str, Any]:
        return {
            "events": {
                f"{outcome}:{reason}": count
                for (outcome, reason), count in sorted(self._world_model_events.items())
            },
            "stale_evictions_total": self._world_model_stale_evictions,
        }
    def record_perception_event(self, outcome: str, source: str) -> None:
        key = (str(outcome), str(source))
        self._perception_events[key] = self._perception_events.get(key, 0) + 1
        self.perception_events_total_c.labels(outcome=key[0], source=key[1]).inc()

    def set_perception_recent_events(self, entries: int) -> None:
        self.perception_recent_events_g.set(max(0, int(entries)))

    def perception_snapshot(self) -> dict[str, Any]:
        return {"events": {
            f"{outcome}:{source}": count
            for (outcome, source), count in sorted(self._perception_events.items())
        }}

    def record_goal_event(self, outcome: str, reason: str) -> None:
        key = (str(outcome), str(reason))
        self._goal_events[key] = self._goal_events.get(key, 0) + 1
        self.goal_events_total_c.labels(outcome=key[0], reason=key[1]).inc()

    def set_goal_active_age(self, seconds: float) -> None:
        self.goal_active_age_seconds_g.set(max(0.0, float(seconds)))

    def goal_snapshot(self) -> dict[str, Any]:
        return {
            "events": {
                f"{outcome}:{reason}": count
                for (outcome, reason), count in sorted(self._goal_events.items())
            }
        }

    def record_intention_event(self, outcome: str, reason: str) -> None:
        key = (str(outcome), str(reason))
        self._intention_events[key] = self._intention_events.get(key, 0) + 1
        self.intention_events_total_c.labels(outcome=key[0], reason=key[1]).inc()

    def set_intention_active(self, age_seconds: float, current_step: int) -> None:
        self.intention_active_age_seconds_g.set(max(0.0, float(age_seconds)))
        self.intention_current_step_g.set(max(0, int(current_step)))

    def intention_snapshot(self) -> dict[str, Any]:
        return {
            "events": {
                f"{outcome}:{reason}": count
                for (outcome, reason), count in sorted(self._intention_events.items())
            }
        }

    # ---------- NVIDIA system metrics ----------

    def sample_gpu_metrics(
        self,
        *,
        command: str = "nvidia-smi",
        timeout_s: float = 1.0,
        refresh_s: float = 2.0,
    ) -> dict[str, Any]:
        """Sample the first NVIDIA GPU, with bounded refresh and explicit staleness."""
        now = time.monotonic()
        if now - self._gpu_last_attempt < refresh_s:
            return self.snapshot()
        self._gpu_last_attempt = now
        try:
            runner = self._gpu_query_runner or self._run_nvidia_smi
            output = runner(command, timeout_s)
            return self.update_gpu_metrics_from_csv(output)
        except Exception as exc:
            self._gpu_available = False
            self._gpu_last_error = f"{type(exc).__name__}: {exc}"
            self.gpu_metrics_available.set(0)
            self.gpu_query_failures.inc()
            return self.snapshot()

    def update_gpu_metrics_from_csv(self, output: str) -> dict[str, Any]:
        """Parse `utilization.gpu,memory.used,memory.total` nvidia-smi CSV."""
        line = next((item.strip() for item in output.splitlines() if item.strip()), "")
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            raise ValueError("nvidia-smi returned an unexpected column count")
        utilization, used_mb, total_mb = (float(part) for part in parts)
        if not 0.0 <= utilization <= 100.0:
            raise ValueError("GPU utilization is outside 0..100")
        if used_mb < 0.0 or total_mb <= 0.0 or used_mb > total_mb:
            raise ValueError("VRAM values are invalid")
        self._gpu_util_percent = utilization
        self._vram_used_mb = used_mb
        self._vram_total_mb = total_mb
        self._gpu_available = True
        self._gpu_last_error = ""
        self.gpu_util.set(utilization)
        self.vram_used_mb.set(used_mb)
        self.vram_total_mb.set(total_mb)
        self.gpu_metrics_available.set(1)
        return self.snapshot()

    @staticmethod
    def _run_nvidia_smi(command: str, timeout_s: float) -> str:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                command,
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=flags,
        )
        return completed.stdout

    # ---------- export ----------

    def snapshot(self) -> dict[str, Any]:
        """Latest real GPU/VRAM sample; stale data remains explicitly marked."""
        return {
            "gpu_util_percent": (
                round(self._gpu_util_percent, 1)
                if self._gpu_util_percent is not None else None
            ),
            "vram_mb": (
                round(self._vram_used_mb, 1) if self._vram_used_mb is not None else None
            ),
            "vram_total_mb": (
                round(self._vram_total_mb, 1) if self._vram_total_mb is not None else None
            ),
            "gpu_metrics_available": self._gpu_available,
            "gpu_metrics_stale": not self._gpu_available and self._gpu_util_percent is not None,
            "gpu_metrics_error": self._gpu_last_error or None,
            "source": "nvidia-smi",
            "chat_rate_per_min": None,
        }

    def prometheus_text(self) -> bytes:
        """Export Prometheus exposition format (cho /metrics endpoint)."""
        return generate_latest(self.registry)
