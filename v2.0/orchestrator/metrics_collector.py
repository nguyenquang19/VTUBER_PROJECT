"""Metrics collector: prometheus_client (ARCHITECTURE 5.3, Phase 0 task 10).

GPU/VRAM production metrics are sampled from nvidia-smi; unavailable data is
reported explicitly and is never replaced with synthetic values.

Dùng CollectorRegistry riêng (không phải global REGISTRY) để test tạo nhiều
instance không bị "Duplicated timeseries".
"""
from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


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
