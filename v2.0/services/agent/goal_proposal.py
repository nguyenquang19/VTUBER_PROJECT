"""Optional strict-schema LLM goal proposal service (Master Plan M2.5)."""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from interfaces.agent import GoalProposalService
from interfaces.base import HealthStatus
from interfaces.llm import ChatMessage, LLMRequest
from services.agent.goal_types import GoalKind
from services.agent.types import AgentStateSnapshot


class GoalProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: GoalKind
    reason: str
    success_condition: str
    source_event_id: str
    parent_thread_id: str | None = None

    @field_validator("reason", "success_condition", "source_event_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        compact = " ".join(value.split())
        if not compact:
            raise ValueError("must not be blank")
        return compact


class GoalProposalGenerator(GoalProposalService):
    service_id = "goal_proposals"

    def __init__(
        self,
        llm: Any,
        system_prompt: str,
        *,
        allowed_kinds: tuple[GoalKind, ...],
        evidence_max_items: int,
        max_tokens: int,
        temperature: float,
        max_reason_chars: int,
        metrics: Any = None,
        enabled: bool = False,
    ) -> None:
        if (
            not isinstance(system_prompt, str)
            or not system_prompt.strip()
            or not isinstance(allowed_kinds, tuple)
            or not allowed_kinds
            or not all(isinstance(item, GoalKind) for item in allowed_kinds)
            or len(set(allowed_kinds)) != len(allowed_kinds)
        ):
            raise ValueError("goal proposal prompt and allowed_kinds must be strict")
        for name, value in (
            ("evidence_max_items", evidence_max_items),
            ("max_tokens", max_tokens),
            ("max_reason_chars", max_reason_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"proposal.{name} must be a positive integer")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or not 0.0 <= float(temperature) <= 2.0
        ):
            raise ValueError("proposal.temperature must be finite and between zero and two")
        if not isinstance(enabled, bool):
            raise ValueError("goal proposal enabled must be boolean")
        self._llm = llm
        self._system_prompt = system_prompt.strip()
        self._allowed_kinds = allowed_kinds
        self._evidence_max_items = evidence_max_items
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_reason_chars = max_reason_chars
        self._metrics = metrics
        self._enabled = enabled
        self._running = False
        self._generated = 0
        self._rejected = 0
        self._errors = 0

    @classmethod
    def from_loader(
        cls, loader: Any, llm: Any, *, metrics: Any = None, enabled: bool = False,
    ) -> "GoalProposalGenerator":
        prompt_path = Path(__file__).resolve().parents[2] / "config" / "prompts" / "goal_proposal_system.txt"
        raw_kinds = loader.get("agent_goals", "proposal.allowed_kinds", None)
        if not isinstance(raw_kinds, list) or not raw_kinds or not all(
            isinstance(value, str) and value.strip() for value in raw_kinds
        ):
            raise ValueError("proposal.allowed_kinds must be a non-empty list of strings")
        try:
            allowed_kinds = tuple(GoalKind(value.strip()) for value in raw_kinds)
        except ValueError as exc:
            raise ValueError("proposal.allowed_kinds contains an unsupported kind") from exc
        return cls(
            llm,
            prompt_path.read_text(encoding="utf-8"),
            allowed_kinds=allowed_kinds,
            evidence_max_items=loader.get(
                "agent_goals", "proposal.evidence_max_items", None,
            ),
            max_tokens=loader.get("agent_goals", "proposal.max_tokens", None),
            temperature=loader.get("agent_goals", "proposal.temperature", None),
            max_reason_chars=loader.get(
                "agent_goals", "proposal.max_reason_chars", None,
            ),
            metrics=metrics,
            enabled=enabled,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("goal proposal enabled must be boolean")
        self._enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, enabled=self._enabled)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "goal_proposals_generated_total": self._generated,
            "goal_proposals_rejected_total": self._rejected,
            "goal_proposals_errors_total": self._errors,
            "goal_proposals_enabled": self._enabled,
        }

    async def propose(self, state: AgentStateSnapshot) -> GoalProposal | None:
        if not self._enabled:
            self._reject("feature_disabled")
            return None
        evidence = _render_evidence(state, self._evidence_max_items)
        if not evidence["events"]:
            self._reject("no_evidence")
            return None
        request_id = f"goal_proposal_{uuid.uuid4().hex[:12]}"
        request = LLMRequest(
            request_id=request_id,
            messages=[
                ChatMessage(role="system", content=self._system_prompt),
                ChatMessage(role="user", content=json.dumps(evidence, ensure_ascii=False)),
            ],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        chunks: list[str] = []
        try:
            async for token in self._llm.generate_stream(request):
                if token.token:
                    chunks.append(token.token)
            raw = "".join(chunks).strip()
            data = json.loads(raw)
            if data == {}:
                self._reject("empty")
                return None
            if not isinstance(data, dict):
                raise ValueError("proposal must be one JSON object")
            proposal = GoalProposal.model_validate(data)
            if proposal.kind not in self._allowed_kinds:
                self._reject("kind_not_allowed")
                return None
            if len(proposal.reason) > self._max_reason_chars:
                self._reject("reason_too_long")
                return None
            self._generated += 1
            self._record("proposal_generated", proposal.kind.value)
            return proposal
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            self._reject("invalid_schema")
            return None
        except Exception:
            self._errors += 1
            self._record("proposal_error", "llm")
            return None

    def _reject(self, reason: str) -> None:
        self._rejected += 1
        self._record("proposal_rejected", reason)

    def _record(self, outcome: str, reason: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_goal_event"):
            try:
                self._metrics.record_goal_event(outcome, reason)
            except Exception:
                pass


def _render_evidence(state: AgentStateSnapshot, max_items: int) -> dict[str, Any]:
    events = [
        {
            "event_id": event.event_id,
            "kind": event.kind.value,
            "source": event.source.value,
            "text": str(event.payload.get("text") or "")[:240],
        }
        for event in state.recent_events[-max(1, max_items):]
    ]
    return {
        "events": events,
        "open_threads": [
            {"thread_id": thread.thread_id, "summary": thread.summary[:240]}
            for thread in state.open_threads
        ],
        "active_goal_ref": state.active_goal_ref,
    }
