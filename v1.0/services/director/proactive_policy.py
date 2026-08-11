"""Grounded proactive candidate selection with silence as the final fallback (M5.2)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from services.director.action_types import DirectorInput
from services.director.director import DirectorAction


class ProactiveSource(str, Enum):
    OPEN_THREAD = "open_thread"
    ENVIRONMENT = "environment"
    SILENCE = "silence"


@dataclass(frozen=True)
class ProactiveChoice:
    source: ProactiveSource
    action: DirectorAction
    category: str
    reason: str
    source_id: str
    evidence_ids: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class ProactivePolicyConfig:
    source_cooldown_seconds: float = 90.0
    open_thread_category: str = "follow_up_topic"
    environment_category: str = "environment_reaction"
    silence_category: str = "complain_silence"
    environment_summary_max_chars: int = 220
    silence_cooldown_seconds: float = 45.0

    @classmethod
    def from_loader(cls, loader: Any) -> "ProactivePolicyConfig":
        prefix = "proactive_policy."
        value = cls(
            source_cooldown_seconds=float(
                loader.get("hosting", prefix + "source_cooldown_seconds", 90)
            ),
            open_thread_category=str(
                loader.get("hosting", prefix + "open_thread_category", "follow_up_topic")
            ),
            environment_category=str(
                loader.get("hosting", prefix + "environment_category", "environment_reaction")
            ),
            silence_category=str(
                loader.get("hosting", prefix + "silence_category", "complain_silence")
            ),
            environment_summary_max_chars=int(
                loader.get("hosting", prefix + "environment_summary_max_chars", 220)
            ),
            silence_cooldown_seconds=float(loader.get(
                "director", "director.self_talk_cooldown_seconds", 45.0,
            )),
        )
        if (
            value.source_cooldown_seconds < 0
            or value.silence_cooldown_seconds < 0
            or value.environment_summary_max_chars <= 0
        ):
            raise ValueError("invalid proactive policy limits")
        return value


class ProactiveHostingPolicy:
    def __init__(
        self, config: ProactivePolicyConfig, *, metrics: Any = None, enabled: bool = True,
    ) -> None:
        self.config = config
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._last_used: dict[str, float] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None, enabled: bool | None = None,
    ) -> "ProactiveHostingPolicy":
        configured = bool(loader.get("hosting", "proactive_policy.enabled", True))
        return cls(
            ProactivePolicyConfig.from_loader(loader), metrics=metrics,
            enabled=configured if enabled is None else enabled,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def choose(
        self, value: DirectorInput, *, allowed_actions: set[str], silence_ready: bool,
    ) -> ProactiveChoice | None:
        if not self.enabled or value.goals.active is not None:
            return None
        eligible_threads = tuple(
            thread for thread in value.agent_state.open_threads
            if getattr(thread.status, "value", "active") == "active"
        )
        if eligible_threads:
            thread = max(
                eligible_threads,
                key=lambda item: (item.updated_at, item.thread_id),
            )
            source_key = f"open_thread:{thread.thread_id}"
            action = (
                DirectorAction.FOLLOW_UP if "follow_up" in allowed_actions
                else DirectorAction.SELF_TALK
            )
            if action.value in allowed_actions and self._ready(source_key, value.now):
                evidence = tuple(item.source_event_id for item in thread.evidence)
                evidence_ids = evidence or ((thread.origin_event_id or thread.thread_id),)
                return self._selected(ProactiveChoice(
                    ProactiveSource.OPEN_THREAD, action, self.config.open_thread_category,
                    "grounded_open_thread", thread.thread_id, evidence_ids, thread.summary,
                ))
            if action.value in allowed_actions:
                self._record(ProactiveSource.OPEN_THREAD.value, "cooldown")
                return ProactiveChoice(
                    ProactiveSource.OPEN_THREAD, DirectorAction.WAIT, "",
                    "grounded_source_cooldown", thread.thread_id,
                )
        environment = value.agent_state.environment_summary or {}
        if bool(environment.get("salient")):
            source_id = str(environment.get("source_event_id") or "").strip()
            summary = _compact(
                environment.get("summary"), self.config.environment_summary_max_chars,
            )
            source_key = f"environment:{source_id}"
            if (
                source_id and summary and "self_talk" in allowed_actions
                and self._ready(source_key, value.now)
            ):
                return self._selected(ProactiveChoice(
                    ProactiveSource.ENVIRONMENT, DirectorAction.SELF_TALK,
                    self.config.environment_category, "salient_environment",
                    source_id, (source_id,), summary,
                ))
            if source_id and summary and "self_talk" in allowed_actions:
                self._record(ProactiveSource.ENVIRONMENT.value, "cooldown")
                return ProactiveChoice(
                    ProactiveSource.ENVIRONMENT, DirectorAction.WAIT, "",
                    "grounded_source_cooldown", source_id,
                )
        if silence_ready and "self_talk" in allowed_actions:
            source_key = f"{ProactiveSource.SILENCE.value}:silence"
            if not self._ready(
                source_key, value.now, self.config.silence_cooldown_seconds,
            ):
                self._record(ProactiveSource.SILENCE.value, "cooldown")
                return ProactiveChoice(
                    ProactiveSource.SILENCE, DirectorAction.WAIT, "",
                    "silence_cooldown", "silence",
                )
            return self._selected(ProactiveChoice(
                ProactiveSource.SILENCE, DirectorAction.SELF_TALK,
                self.config.silence_category, "silence_fallback", "silence",
            ))
        return None

    def mark_used(self, choice: ProactiveChoice, now: float) -> None:
        self._last_used[f"{choice.source.value}:{choice.source_id}"] = float(now)
        self._record(choice.source.value, "used")

    def mark_source_used(self, source: str, identifier: str, now: float) -> None:
        self._last_used[f"{source}:{identifier}"] = float(now)

    def _ready(
        self, source_key: str, now: float, cooldown_seconds: float | None = None,
    ) -> bool:
        used = self._last_used.get(source_key)
        cooldown = (
            self.config.source_cooldown_seconds
            if cooldown_seconds is None else max(0.0, float(cooldown_seconds))
        )
        return used is None or now - used >= cooldown

    def _selected(self, choice: ProactiveChoice) -> ProactiveChoice:
        self._record(choice.source.value, "selected")
        return choice

    def _record(self, source: str, outcome: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_proactive_candidate"):
            try:
                self._metrics.record_proactive_candidate(source, outcome)
            except Exception:
                pass


def _compact(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]
