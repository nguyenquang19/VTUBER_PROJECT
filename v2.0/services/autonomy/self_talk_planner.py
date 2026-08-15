"""Cause-first thought planning with grounded mini-arcs and bounded output."""
from __future__ import annotations

import re
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from interfaces.animation import MoodState
from interfaces.base import HealthState, HealthStatus
from interfaces.self_talk import (
    SelfTalkContext,
    SelfTalkPlan,
    SelfTalkPlanningService,
    SelfTalkReadiness,
    SelfTalkStage,
    SelfTalkValidation,
    ThoughtCause,
)
from services.autonomy.lore_material import LoreMaterialProvider
from services.tts.sentence_splitter import split_vn


@dataclass(frozen=True)
class Thought:
    thought_id: str
    cause: ThoughtCause
    anchor: str
    intention: str
    evidence_refs: tuple[str, ...] = ()


@dataclass
class _Arc:
    thought: Thought
    stage: SelfTalkStage = SelfTalkStage.OPEN
    previous_text: str = ""
    wait_until: float = 0.0
    resume_after: float = 0.0


class SelfTalkPlanner(SelfTalkPlanningService):
    """One active bounded thought arc; no fixed semantic topic pool is used."""

    service_id = "self_talk_planner"

    def __init__(
        self,
        *,
        cognitive_moves: tuple[str, ...],
        mood_style: Any = None,
        lore_material: LoreMaterialProvider | None = None,
        enabled: bool = True,
        wait_for_chat_seconds: float = 75.0,
        resume_after_chat_seconds: float = 12.0,
        min_silence_seconds: float = 20.0,
        unavailable_retry_seconds: float = 90.0,
        thought_ledger_size: int = 32,
        semantic_repeat_threshold: float = 0.72,
        output_repeat_threshold: float = 0.88,
        stage_repeat_threshold: float = 0.72,
        stage_repeat_min_tokens: int = 4,
        max_previous_text_chars: int = 280,
        recent_context_min_tokens: int = 3,
        silence_intention: str = (
            "nhận xét thật ngắn về chính khoảng im lặng mà không suy đoán nguyên nhân"
        ),
        silence_allow_question: bool = True,
        grounded_categories: tuple[str, ...] = (
            "follow_up_topic", "environment_reaction", "roast_chat",
        ),
        cause_directions: dict[str, str] | None = None,
        question_endings: tuple[str, ...] = (
            "nhỉ", "hả", "à", "ư", "không", "chưa", "sao", "gì", "nào",
        ),
        question_starters: tuple[str, ...] = (
            "ai", "gì", "sao", "tại sao", "vì sao", "bao nhiêu", "khi nào",
            "ở đâu", "có phải", "liệu",
        ),
        question_particles: tuple[str, ...] = ("nhỉ", "hả", "ư"),
        stage_directions: dict[str, str] | None = None,
        stage_limits: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        moves = tuple(item.strip() for item in cognitive_moves if item.strip())
        if not moves:
            raise ValueError("thought engine cần ít nhất một cognitive move")
        if not 0.0 <= semantic_repeat_threshold <= 1.0:
            raise ValueError("semantic_repeat_threshold phải trong khoảng 0..1")
        if not 0.0 <= output_repeat_threshold <= 1.0:
            raise ValueError("output_repeat_threshold phải trong khoảng 0..1")
        if not 0.0 <= stage_repeat_threshold <= 1.0:
            raise ValueError("stage_repeat_threshold phải trong khoảng 0..1")
        if stage_repeat_min_tokens <= 0:
            raise ValueError("stage_repeat_min_tokens phải dương")
        if not silence_intention.strip():
            raise ValueError("silence_intention không được rỗng")
        self._moves = moves
        self._mood_style = mood_style
        self._lore_material = lore_material
        self._enabled = bool(enabled)
        self._running = False
        self._wait_for_chat_s = float(wait_for_chat_seconds)
        self._resume_after_chat_s = float(resume_after_chat_seconds)
        self._min_silence_s = float(min_silence_seconds)
        self._unavailable_retry_s = float(unavailable_retry_seconds)
        self._repeat_threshold = float(semantic_repeat_threshold)
        self._output_repeat_threshold = float(output_repeat_threshold)
        self._stage_repeat_threshold = float(stage_repeat_threshold)
        self._stage_repeat_min_tokens = int(stage_repeat_min_tokens)
        self._max_previous_chars = int(max_previous_text_chars)
        self._recent_context_min_tokens = max(1, int(recent_context_min_tokens))
        self._silence_intention = silence_intention.strip()
        self._silence_allow_question = bool(silence_allow_question)
        self._grounded_categories = set(grounded_categories)
        self._cause_directions = dict(cause_directions or {})
        self._question_endings = tuple(
            item for item in (_question_normalise(value) for value in question_endings)
            if item
        )
        self._question_starters = tuple(
            item for item in (_question_normalise(value) for value in question_starters)
            if item
        )
        self._question_particles = tuple(
            item for item in (_question_normalise(value) for value in question_particles)
            if item
        )
        self._directions = dict(stage_directions or {})
        self._stage_limits = dict(stage_limits or {})
        self._ledger: deque[dict[str, Any]] = deque(maxlen=max(1, int(thought_ledger_size)))
        self._move_cursor = 0
        self._arc: _Arc | None = None
        self._pending: SelfTalkPlan | None = None
        self._pending_thought: Thought | None = None
        self._interrupted_plan_id: str | None = None
        self._chat_quiet_until = 0.0
        self._silence_consumed = False
        self._metrics: dict[str, int] = {
            "plans_total": 0,
            "commits_total": 0,
            "releases_total": 0,
            "arcs_started_total": 0,
            "arcs_completed_total": 0,
            "chat_yields_total": 0,
            "chat_suspends_total": 0,
            "wait_suppressed_total": 0,
            "grounded_one_shots_total": 0,
            "no_material_total": 0,
            "repeat_suppressed_total": 0,
            "output_rejected_total": 0,
            "chat_quiet_suppressed_total": 0,
            "silence_one_shots_total": 0,
            "stage_repeat_rejected_total": 0,
            "semantic_question_rejected_total": 0,
            "recent_context_rejected_total": 0,
        }

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        mood_style: Any = None,
        lore_material: LoreMaterialProvider | None = None,
        enabled: bool = True,
    ) -> "SelfTalkPlanner":
        raw = loader.get("self_talk", "self_talk", {}) or {}
        return cls(
            cognitive_moves=tuple(raw.get("cognitive_moves", []) or ()),
            mood_style=mood_style,
            lore_material=lore_material,
            enabled=enabled,
            wait_for_chat_seconds=float(raw.get("wait_for_chat_seconds", 75.0)),
            resume_after_chat_seconds=float(raw.get("resume_after_chat_seconds", 12.0)),
            min_silence_seconds=float(raw.get("min_silence_seconds", 20.0)),
            unavailable_retry_seconds=float(raw.get("unavailable_retry_seconds", 90.0)),
            thought_ledger_size=int(raw.get("thought_ledger_size", 32)),
            semantic_repeat_threshold=float(raw.get("semantic_repeat_threshold", 0.72)),
            output_repeat_threshold=float(raw.get("output_repeat_threshold", 0.88)),
            stage_repeat_threshold=float(raw.get("stage_repeat_threshold", 0.72)),
            stage_repeat_min_tokens=int(raw.get("stage_repeat_min_tokens", 4)),
            max_previous_text_chars=int(raw.get("max_previous_text_chars", 280)),
            recent_context_min_tokens=int(raw.get("recent_context_min_tokens", 3)),
            silence_intention=str(raw.get(
                "silence_intention",
                "nhận xét thật ngắn về chính khoảng im lặng mà không suy đoán nguyên nhân",
            )),
            silence_allow_question=bool(raw.get("silence_allow_question", True)),
            grounded_categories=tuple(raw.get("grounded_categories", []) or ()),
            cause_directions=raw.get("cause_directions", {}) or {},
            question_endings=tuple(raw.get("question_endings", []) or ()),
            question_starters=tuple(raw.get("question_starters", []) or ()),
            question_particles=tuple(raw.get("question_particles", []) or ()),
            stage_directions=raw.get("stage_directions", {}) or {},
            stage_limits=raw.get("stage_limits", {}) or {},
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._release_lore(self._pending_thought)
        self._pending = None
        self._pending_thought = None

    async def health_check(self) -> HealthStatus:
        if not self._enabled:
            return HealthStatus.stopped(self.service_id)
        if not self._moves:
            return HealthStatus.unhealthy(self.service_id, "không có cognitive move")
        state = HealthState.HEALTHY if self._running else HealthState.DEGRADED
        return HealthStatus(
            state=state,
            service_id=self.service_id,
            message="chưa start" if not self._running else "",
            details={"cognitive_moves": len(self._moves)},
        )

    def prepare(
        self,
        *,
        mood: MoodState,
        now: float,
        base_prompt: str | None = None,
        category: str | None = None,
        tone_flags: tuple[str, ...] = (),
        context: SelfTalkContext | None = None,
    ) -> SelfTalkPlan | None:
        if not self._enabled or self._pending is not None:
            return None
        if now < self._chat_quiet_until:
            self._metrics["chat_quiet_suppressed_total"] += 1
            return None
        ctx = context or SelfTalkContext()
        if base_prompt and category in self._grounded_categories:
            thought = self._make_grounded_thought(base_prompt, category)
            plan = SelfTalkPlan(
                plan_id=self._new_id(),
                thought_id=thought.thought_id,
                cause=thought.cause,
                intention=thought.intention,
                evidence_refs=thought.evidence_refs,
                stage=SelfTalkStage.GROUNDED,
                prompt_text=self._grounded_prompt(thought, mood, tone_flags),
                one_shot=True,
                **self._limits_for(SelfTalkStage.GROUNDED),
            )
            self._pending = plan
            self._pending_thought = thought
            self._metrics["plans_total"] += 1
            self._metrics["grounded_one_shots_total"] += 1
            return plan

        if self._arc is not None and self._arc.stage is SelfTalkStage.WAIT:
            if now < self._arc.wait_until:
                self._metrics["wait_suppressed_total"] += 1
                return None
            self._complete_arc()
        if self._arc is not None and now < self._arc.resume_after:
            self._metrics["wait_suppressed_total"] += 1
            return None
        if self._arc is None:
            thought = self._compose_thought(ctx, base_prompt, category)
            if thought is None:
                self._metrics["no_material_total"] += 1
                return None
            if thought.cause is not ThoughtCause.SILENCE and self._is_repeated(thought):
                self._release_lore(thought)
                self._metrics["repeat_suppressed_total"] += 1
                return None
            if thought.cause is ThoughtCause.SILENCE:
                limits = self._limits_for(SelfTalkStage.OPEN)
                limits["allow_question"] = self._silence_allow_question
                plan = SelfTalkPlan(
                    plan_id=self._new_id(),
                    thought_id=thought.thought_id,
                    cause=thought.cause,
                    intention=thought.intention,
                    evidence_refs=thought.evidence_refs,
                    stage=SelfTalkStage.OPEN,
                    prompt_text=self._arc_prompt(_Arc(thought=thought), mood, tone_flags),
                    one_shot=True,
                    **limits,
                )
                self._pending = plan
                self._pending_thought = thought
                self._metrics["plans_total"] += 1
                self._metrics["silence_one_shots_total"] += 1
                return plan
            self._arc = _Arc(thought=thought)
            self._metrics["arcs_started_total"] += 1

        if (
            self._arc.stage is SelfTalkStage.OPEN
            and not self._ensure_lore_reservation(self._arc.thought)
        ):
            self._arc = None
            self._metrics["no_material_total"] += 1
            return None

        plan = SelfTalkPlan(
            plan_id=self._new_id(),
            thought_id=self._arc.thought.thought_id,
            cause=self._arc.thought.cause,
            intention=self._arc.thought.intention,
            evidence_refs=self._arc.thought.evidence_refs,
            stage=self._arc.stage,
            prompt_text=self._arc_prompt(self._arc, mood, tone_flags),
            **self._limits_for(self._arc.stage),
        )
        self._pending = plan
        self._pending_thought = self._arc.thought
        self._metrics["plans_total"] += 1
        return plan

    def validate_output(self, plan_id: str, text: str) -> SelfTalkValidation:
        plan = self._pending
        if plan is None or plan.plan_id != plan_id:
            return SelfTalkValidation(valid=False, reasons=("plan_not_pending",))
        stripped = text.strip()
        reasons: list[str] = []
        sentences = split_vn(stripped)
        if not stripped:
            reasons.append("empty")
        if len(sentences) > plan.max_sentences:
            reasons.append("too_many_sentences")
        question_like = _looks_like_question(
            stripped,
            self._question_endings,
            self._question_starters,
            self._question_particles,
        )
        if not plan.allow_question and question_like:
            reasons.append("question_not_allowed")
            self._metrics["semantic_question_rejected_total"] += 1
        if plan.stage is SelfTalkStage.INVITE and not question_like:
            reasons.append("invitation_missing_question")
        if plan.stage is SelfTalkStage.INVITE:
            question_count = sum(
                _looks_like_question(
                    sentence,
                    self._question_endings,
                    self._question_starters,
                    self._question_particles,
                )
                for sentence in sentences
            )
            if question_count != 1:
                reasons.append("invitation_question_count")
        if self._repeats_previous_stage(stripped, plan):
            reasons.append("stage_repeat")
            self._metrics["stage_repeat_rejected_total"] += 1
        if self._text_repeats_ledger(
            stripped,
            plan.thought_id,
            exclude_same_thought=not plan.one_shot,
        ):
            reasons.append("semantic_repeat")
        if reasons:
            self._metrics["output_rejected_total"] += 1
        return SelfTalkValidation(valid=not reasons, reasons=tuple(reasons))

    def can_deliver(self, plan_id: str) -> bool:
        allowed = (
            self._pending is not None
            and self._pending.plan_id == plan_id
            and self._interrupted_plan_id != plan_id
        )
        if not allowed or self._pending_thought is None:
            return allowed
        lore_id = self._lore_id(self._pending_thought)
        if lore_id is None or self._pending.stage is not SelfTalkStage.OPEN:
            return True
        return bool(
            self._lore_material is not None
            and self._lore_material.has_reservation(lore_id)
        )

    def readiness(self, now: float) -> SelfTalkReadiness:
        if not self._enabled:
            return SelfTalkReadiness(ready=False, reason="disabled")
        if self._pending is not None:
            return SelfTalkReadiness(ready=False, reason="thought_pending")
        if self._arc is not None and now < self._arc.resume_after:
            return SelfTalkReadiness(
                ready=False, reason="thought_suspended", retry_at=self._arc.resume_after,
            )
        if now < self._chat_quiet_until:
            return SelfTalkReadiness(
                ready=False, reason="chat_quiet_gate", retry_at=self._chat_quiet_until,
            )
        if (
            self._arc is not None
            and self._arc.stage is SelfTalkStage.WAIT
            and now < self._arc.wait_until
        ):
            return SelfTalkReadiness(
                ready=False, reason="thought_wait_chat", retry_at=self._arc.wait_until,
            )
        return SelfTalkReadiness(ready=True)

    def commit(self, plan_id: str, delivered_text: str, now: float) -> bool:
        plan = self._pending
        if plan is None or plan.plan_id != plan_id or not delivered_text.strip():
            return False
        thought = self._pending_thought
        self._pending = None
        self._pending_thought = None
        self._interrupted_plan_id = None
        self._metrics["commits_total"] += 1
        self._ledger.append({
            "thought_id": plan.thought_id,
            "cause": plan.cause.value,
            "intention": plan.intention,
            "text": delivered_text.strip()[-self._max_previous_chars:],
            "tokens": sorted(_token_set(delivered_text)),
            "thought_tokens": sorted(_token_set(
                f"{thought.anchor} {thought.intention}" if thought is not None else plan.intention
            )),
            "committed_at": float(now),
        })
        self._commit_lore(thought)
        if plan.cause is ThoughtCause.SILENCE:
            self._silence_consumed = True
        if plan.one_shot:
            return True
        if self._arc is None or self._arc.thought.thought_id != plan.thought_id:
            return False
        self._arc.previous_text = delivered_text.strip()[-self._max_previous_chars:]
        if plan.stage is SelfTalkStage.OPEN:
            self._arc.stage = SelfTalkStage.DEVELOP
        elif plan.stage is SelfTalkStage.DEVELOP:
            self._arc.stage = SelfTalkStage.INVITE
        elif plan.stage is SelfTalkStage.INVITE:
            self._arc.stage = SelfTalkStage.WAIT
            self._arc.wait_until = now + self._wait_for_chat_s
        return True

    def release(self, plan_id: str) -> None:
        if self._pending is not None and self._pending.plan_id == plan_id:
            self._release_lore(self._pending_thought)
            self._pending = None
            self._pending_thought = None
            self._interrupted_plan_id = None
            self._metrics["releases_total"] += 1

    def on_chat(self, now: float) -> None:
        self._chat_quiet_until = max(
            self._chat_quiet_until, float(now) + self._resume_after_chat_s,
        )
        self._silence_consumed = False
        if self._arc is None and self._pending is None:
            return
        self._metrics["chat_yields_total"] += 1
        if self._pending is not None:
            self._interrupted_plan_id = self._pending.plan_id
        if self._arc is not None and self._arc.stage is SelfTalkStage.WAIT:
            self._complete_arc()
            return
        if self._arc is not None:
            self._arc.resume_after = self._chat_quiet_until
            self._metrics["chat_suspends_total"] += 1

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._release_lore(self._pending_thought)
            self._pending = None
            self._pending_thought = None
            self._arc = None
            self._interrupted_plan_id = None
            self._chat_quiet_until = 0.0
            self._silence_consumed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_lore_enabled(self, enabled: bool) -> None:
        if self._lore_material is None:
            return
        if not enabled:
            pending_is_lore = self._lore_id(self._pending_thought) is not None
            arc_is_lore = bool(
                self._arc is not None and self._lore_id(self._arc.thought) is not None
            )
            self._release_lore(self._pending_thought)
            if pending_is_lore:
                self._pending = None
                self._pending_thought = None
                self._interrupted_plan_id = None
            if arc_is_lore:
                self._arc = None
        self._lore_material.set_enabled(enabled)

    @property
    def lore_enabled(self) -> bool:
        return bool(self._lore_material is not None and self._lore_material.enabled)

    @property
    def unavailable_retry_seconds(self) -> float:
        return self._unavailable_retry_s

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "active_thought_id": self._arc.thought.thought_id if self._arc else None,
            "cause": self._arc.thought.cause.value if self._arc else None,
            "intention": self._arc.thought.intention if self._arc else None,
            "stage": self._arc.stage.value if self._arc else None,
            "pending_plan_id": self._pending.plan_id if self._pending else None,
            "pending_interrupted": self._interrupted_plan_id is not None,
            "chat_quiet_until": self._chat_quiet_until,
            "silence_consumed": self._silence_consumed,
            "ledger": list(self._ledger),
        }

    def get_metrics(self) -> dict[str, Any]:
        values = {
            **{f"self_talk_planner_{key}": value for key, value in self._metrics.items()},
            "self_talk_planner_enabled": self._enabled,
            "self_talk_planner_active": self._arc is not None,
            "self_talk_planner_stage": self._arc.stage.value if self._arc else "idle",
        }
        if self._lore_material is not None:
            values.update(self._lore_material.get_metrics())
        return values

    def _complete_arc(self) -> None:
        if self._arc is not None:
            self._metrics["arcs_completed_total"] += 1
        self._arc = None

    def _arc_prompt(
        self, arc: _Arc, mood: MoodState, tone_flags: tuple[str, ...],
    ) -> str:
        direction = self._directions.get(arc.stage.value, "")
        cause_direction = self._cause_directions.get(arc.thought.cause.value, "")
        rows = [
            "[SELF-THOUGHT — lời tự nói, không phải trả lời một viewer cụ thể]",
            f"Chặng: {arc.stage.value}.",
            f"Nguyên nhân ý nghĩ: {arc.thought.cause.value}.",
            f"Mỏ neo đã biết: {arc.thought.anchor}",
            f"Ý định nhận thức: {arc.thought.intention}",
            "Hãy để câu nói nghe như một ý vừa nảy ra, có do dự hoặc sắc thái cá nhân vừa đủ; không giảng bài và không tự nhận mình đang thực hiện quy trình.",
            "Chỉ nói như nhận xét, sở thích, điều chưa chắc hoặc giả định. CẤM biến giả định thành sự kiện đã xảy ra.",
            "CẤM bịa người, game, vật thể, ký ức, hành động của operator/viewer hoặc dữ kiện ngoài mỏ neo.",
        ]
        if cause_direction:
            rows.append(cause_direction)
        if arc.previous_text:
            rows.append(f"Câu Mai vừa nói trong cùng mạch: {arc.previous_text}")
            rows.append(
                "Câu trước chỉ để hiểu mạch. CẤM chép lại hoặc diễn đạt lại; "
                "output chỉ gồm phần ý mới nối tiếp."
            )
        if arc.stage is SelfTalkStage.INVITE:
            rows.append("Khép ý bằng đúng một câu hỏi tự nhiên bám trực tiếp vào mỏ neo và ý vừa nói.")
        if direction:
            rows.append(direction)
        rows.extend(self._mood_rows(mood, tone_flags))
        rows.append("Chỉ viết đúng lời Mai sẽ nói, không giải thích.")
        return "\n".join(rows)

    def _grounded_prompt(
        self, thought: Thought, mood: MoodState, tone_flags: tuple[str, ...],
    ) -> str:
        # Autonomy render_prompt already applies MoodStyleTable to grounded material.
        # Do not inject it twice: repeated style directives cause overacting.
        del mood, tone_flags
        rows = [
            thought.anchor,
            "[RÀNG BUỘC SELF-TALK] Chỉ dùng dữ kiện xuất hiện trong context trên; thiếu dữ kiện thì nói chưa biết.",
            f"Ý định: {thought.intention}",
            "Mood chỉ điều chỉnh cách nói, tuyệt đối không được thêm dữ kiện.",
            "Nói như phản ứng vừa nảy ra, không tóm tắt máy móc và không nhắc đến prompt/context/quy trình.",
        ]
        return "\n".join(rows)

    def _compose_thought(
        self,
        ctx: SelfTalkContext,
        base_prompt: str | None,
        category: str | None,
    ) -> Thought | None:
        cause: ThoughtCause
        anchor: str
        evidence: tuple[str, ...]
        if base_prompt and base_prompt.strip():
            cause = ThoughtCause.GROUNDED
            anchor = _bounded(base_prompt, self._max_previous_chars)
            evidence = (f"category:{category or 'ambient'}",)
        elif ctx.environment_summary and ctx.environment_summary.strip():
            cause = ThoughtCause.ENVIRONMENT
            anchor = "Quan sát môi trường đã xác thực: " + _bounded(
                ctx.environment_summary, self._max_previous_chars,
            )
            evidence = ("runtime:environment",)
        elif ctx.recent_context and self._has_recent_material(ctx.recent_context[-1]):
            cause = ThoughtCause.RECENT_CONTEXT
            anchor = "Mạch gần đây đã có: " + _bounded(
                ctx.recent_context[-1], self._max_previous_chars,
            )
            evidence = ("runtime:recent_context",)
        elif ctx.silence_seconds >= self._min_silence_s and self._lore_material is not None and (
            lore := self._lore_material.reserve()
        ) is not None:
            cause = ThoughtCause.GROUNDED
            anchor = _bounded(lore.anchor, self._max_previous_chars)
            evidence = (lore.evidence_ref,)
        elif ctx.recent_context:
            self._metrics["recent_context_rejected_total"] += 1
            if ctx.silence_seconds < self._min_silence_s or self._silence_consumed:
                return None
            cause = ThoughtCause.SILENCE
            anchor = "Buổi live đang có một khoảng im lặng; ngoài điều đó không có sự kiện mới được xác thực."
            evidence = ("runtime:silence",)
        elif ctx.silence_seconds >= self._min_silence_s:
            if self._silence_consumed:
                return None
            cause = ThoughtCause.SILENCE
            anchor = "Buổi live đang có một khoảng im lặng; ngoài điều đó không có sự kiện mới được xác thực."
            evidence = ("runtime:silence",)
        else:
            return None
        move = self._silence_intention if cause is ThoughtCause.SILENCE else self._next_move()
        signature = _normalise(f"{cause.value}:{anchor}:{move}")
        return Thought(
            thought_id=f"thought_{uuid.uuid5(uuid.NAMESPACE_URL, signature).hex[:12]}",
            cause=cause,
            anchor=anchor,
            intention=move,
            evidence_refs=evidence,
        )

    def _make_grounded_thought(self, base_prompt: str, category: str) -> Thought:
        anchor = _bounded(base_prompt, self._max_previous_chars * 2)
        intention = "phản ứng vào chi tiết đáng chú ý nhất rồi để lại một nét nhìn riêng"
        signature = _normalise(f"grounded:{category}:{anchor}")
        return Thought(
            thought_id=f"thought_{uuid.uuid5(uuid.NAMESPACE_URL, signature).hex[:12]}",
            cause=ThoughtCause.GROUNDED,
            anchor=anchor,
            intention=intention,
            evidence_refs=(f"category:{category}",),
        )

    def _next_move(self) -> str:
        move = self._moves[self._move_cursor % len(self._moves)]
        self._move_cursor += 1
        return move

    @staticmethod
    def _lore_id(thought: Thought | None) -> str | None:
        if thought is None:
            return None
        ref = next(
            (item for item in thought.evidence_refs if item.startswith("lore:")),
            None,
        )
        return ref.removeprefix("lore:") if ref is not None else None

    def _commit_lore(self, thought: Thought | None) -> None:
        lore_id = self._lore_id(thought)
        if lore_id is not None and self._lore_material is not None:
            self._lore_material.commit(lore_id)

    def _ensure_lore_reservation(self, thought: Thought) -> bool:
        lore_id = self._lore_id(thought)
        if lore_id is None:
            return True
        if self._lore_material is None:
            return False
        if self._lore_material.has_reservation(lore_id):
            return True
        material = self._lore_material.reserve()
        return material is not None and material.material_id == lore_id

    def _release_lore(self, thought: Thought | None) -> None:
        lore_id = self._lore_id(thought)
        if lore_id is not None and self._lore_material is not None:
            self._lore_material.release(lore_id)

    def _has_recent_material(self, value: str) -> bool:
        without_emojis = re.sub(r":[^:\s]+:", " ", str(value))
        return len(_token_set(without_emojis)) >= self._recent_context_min_tokens

    def _limits_for(self, stage: SelfTalkStage) -> dict[str, Any]:
        default_questions = stage is SelfTalkStage.INVITE
        row = self._stage_limits.get(stage.value, {}) or {}
        return {
            "max_sentences": int(row.get("max_sentences", 2)),
            "allow_question": bool(row.get("allow_question", default_questions)),
        }

    def _is_repeated(self, thought: Thought) -> bool:
        signature = _token_set(f"{thought.anchor} {thought.intention}")
        return any(
            _jaccard(signature, set(item.get("thought_tokens", ()))) >= self._repeat_threshold
            for item in self._ledger
        )

    def _text_repeats_ledger(
        self,
        text: str,
        thought_id: str,
        *,
        exclude_same_thought: bool,
    ) -> bool:
        tokens = _token_set(text)
        if not tokens:
            return False
        return any(
            _jaccard(tokens, set(item.get("tokens", ()))) >= self._output_repeat_threshold
            for item in self._ledger
            if not exclude_same_thought or item.get("thought_id") != thought_id
        )

    def _repeats_previous_stage(self, text: str, plan: SelfTalkPlan) -> bool:
        arc = self._arc
        if (
            arc is None
            or arc.thought.thought_id != plan.thought_id
            or not arc.previous_text
        ):
            return False
        previous = _token_set(arc.previous_text)
        current = _token_set(text)
        if len(previous) < self._stage_repeat_min_tokens or not current:
            return False
        coverage = len(previous & current) / len(previous)
        return coverage >= self._stage_repeat_threshold

    def _mood_rows(self, mood: MoodState, tone_flags: tuple[str, ...]) -> list[str]:
        if self._mood_style is None:
            return []
        try:
            directive = self._mood_style.directive_for(mood, set(tone_flags))
        except Exception:
            directive = None
        return [f"Phong cách theo mood hiện tại (chỉ style): {directive}"] if directive else []

    @staticmethod
    def _new_id() -> str:
        return f"stp_{uuid.uuid4().hex[:12]}"


def _bounded(value: str, limit: int) -> str:
    return " ".join(str(value).split())[:max(1, int(limit))]


def _normalise(value: str) -> str:
    plain = "".join(
        char for char in unicodedata.normalize("NFD", value.lower())
        if unicodedata.category(char) != "Mn"
    )
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def _token_set(value: str) -> set[str]:
    return {token for token in _normalise(value).split() if len(token) > 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _looks_like_question(
    text: str,
    endings: tuple[str, ...],
    starters: tuple[str, ...],
    particles: tuple[str, ...] = (),
) -> bool:
    if "?" in text:
        return True
    normalised = _question_normalise(text)
    if not normalised:
        return False
    tokens = set(normalised.split())
    if any(item in tokens for item in particles):
        return True
    if any(normalised == item or normalised.endswith(f" {item}") for item in endings):
        return True
    return any(
        normalised == item or normalised.startswith(f"{item} ")
        for item in starters
    )


def _question_normalise(value: str) -> str:
    """Keep Vietnamese tone marks so declarative `sao á` is not question-ending `à`."""
    lowered = unicodedata.normalize("NFC", str(value).lower())
    return " ".join(re.findall(r"[^\W_]+", lowered, flags=re.UNICODE))
