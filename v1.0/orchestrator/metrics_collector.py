"""Metrics collector: prometheus_client (ARCHITECTURE 5.3, Phase 0 task 10).

Phase 0 scope: định nghĩa metric objects thật (5.3) + vài "metric giả" tự cập
nhật để dashboard có gì hiển thị realtime trước khi có LLM/TTS thật.

Dùng CollectorRegistry riêng (không phải global REGISTRY) để test tạo nhiều
instance không bị "Duplicated timeseries".
"""
from __future__ import annotations

import math
import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class MetricsCollector:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
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

        # --- 3 "metric giả" cho Phase 0 (chưa có service thật) ---
        # DoD: "Metric giả cập nhật realtime trên chart"
        self.fake_gpu_util = Gauge(
            "mai_fake_gpu_util_percent", "Fake GPU utilization (Phase 0 demo)",
            registry=self.registry,
        )
        self.fake_vram_mb = Gauge(
            "mai_fake_vram_mb", "Fake VRAM usage (Phase 0 demo)",
            registry=self.registry,
        )
        self.fake_chat_rate = Gauge(
            "mai_fake_chat_rate_per_min", "Fake chat rate (Phase 0 demo)",
            registry=self.registry,
        )

        self._start = time.perf_counter()

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

    # ---------- fake updater (Phase 0 demo) ----------

    def tick_fake_metrics(self, t: float | None = None) -> dict[str, float]:
        """Cập nhật 3 metric giả bằng sóng sin lệch pha → chart có chuyển động.

        Trả snapshot để dashboard push qua WebSocket.
        """
        t = t if t is not None else (time.perf_counter() - self._start)
        gpu = 50 + 40 * math.sin(t / 3)
        vram = 9800 + 300 * math.sin(t / 5 + 1)
        chat = max(0.0, 30 + 25 * math.sin(t / 7 + 2))
        self.fake_gpu_util.set(gpu)
        self.fake_vram_mb.set(vram)
        self.fake_chat_rate.set(chat)
        return {
            "gpu_util_percent": round(gpu, 1),
            "vram_mb": round(vram, 1),
            "chat_rate_per_min": round(chat, 1),
        }

    # ---------- export ----------

    def snapshot(self) -> dict[str, Any]:
        """Giá trị hiện tại của các metric giả (cho dashboard)."""
        return {
            "gpu_util_percent": round(self._gauge_value(self.fake_gpu_util), 1),
            "vram_mb": round(self._gauge_value(self.fake_vram_mb), 1),
            "chat_rate_per_min": round(self._gauge_value(self.fake_chat_rate), 1),
        }

    @staticmethod
    def _gauge_value(gauge: Gauge) -> float:
        # prometheus_client Gauge: đọc value hiện tại
        return gauge._value.get()  # type: ignore[attr-defined]

    def prometheus_text(self) -> bytes:
        """Export Prometheus exposition format (cho /metrics endpoint)."""
        return generate_latest(self.registry)
