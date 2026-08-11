"""Config-driven, deterministic TurnAffect and SessionMood policy (M10.6)."""
from __future__ import annotations

import math
import re
import time
from typing import Any, Callable

from interfaces.affect import (
    AffectResponseMode,
    AffectService,
    AffectStyle,
    SessionMood,
    TurnAffect,
)
from interfaces.base import HealthStatus


_SAFE_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


class AffectV2(AffectService):
    service_id = "affect_v2"

    def __init__(
        self,
        *,
        mappings: dict[str, dict[str, Any]],
        tone_overrides: dict[str, dict[str, Any]],
        ttl_turns: int,
        session_half_life_s: float,
        session_blend: float,
        clock: Callable[[], float] | None = None,
        metrics: Any = None,
        enabled: bool = True,
    ) -> None:
        if ttl_turns <= 0 or session_half_life_s <= 0 or not 0 < session_blend <= 1:
            raise ValueError("affect v2 bounds are invalid")
        self._mappings = {str(k): dict(v or {}) for k, v in mappings.items()}
        self._tone_overrides = {str(k): dict(v or {}) for k, v in tone_overrides.items()}
        self.ttl_turns = int(ttl_turns)
        self.session_half_life_s = float(session_half_life_s)
        self.session_blend = float(session_blend)
        self._clock = clock or time.monotonic
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._running = False
        self._turn_index = 0
        now = float(self._clock())
        self._turn_affect: TurnAffect | None = None
        self._session = SessionMood(updated_at=now)
        self._events = 0

    @classmethod
    def from_loader(
        cls,
        loader: Any,
        *,
        clock: Callable[[], float] | None = None,
        metrics: Any = None,
        enabled: bool = True,
    ) -> "AffectV2":
        policy = loader.get("affect_v2", "policy", {}) or {}
        return cls(
            mappings=loader.get("affect_v2", "mappings", {}) or {},
            tone_overrides=loader.get("affect_v2", "tone_overrides", {}) or {},
            ttl_turns=int(policy.get("turn_ttl_turns", 1)),
            session_half_life_s=float(policy.get("session_half_life_s", 300)),
            session_blend=float(policy.get("session_blend", 0.25)),
            clock=clock, metrics=metrics, enabled=enabled,
        )

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id, enabled=self.enabled, events=self._events,
            turn_index=self._turn_index,
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "affect_v2_events_total": self._events,
            "affect_v2_turn_index": self._turn_index,
            "affect_v2_enabled": self.enabled,
        }

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def observe(
        self,
        category: str,
        *,
        targets: dict[str, float],
        tone_flag: str | None,
        cause_ref: str | None,
    ) -> TurnAffect:
        if not self.enabled:
            return TurnAffect(
                created_turn=self._turn_index,
                expires_at_turn=self._turn_index + self.ttl_turns,
            )
        spec = dict(self._mappings.get(str(category), self._mappings.get("default", {})))
        if tone_flag and tone_flag in self._tone_overrides:
            spec.update(self._tone_overrides[tone_flag])
        affect = TurnAffect(
            style=AffectStyle(str(spec.get("style", AffectStyle.NEUTRAL.value))),
            response_mode=AffectResponseMode(str(
                spec.get("mode", AffectResponseMode.NATURAL.value),
            )),
            energy=_unit(spec.get("energy", 0.5)),
            warmth=_unit(spec.get("warmth", 0.5)),
            urgency=_unit(spec.get("urgency", 0.0)),
            cause_ref=_cause_ref(cause_ref),
            created_turn=self._turn_index,
            expires_at_turn=self._turn_index + self.ttl_turns,
        )
        now = float(self._clock())
        current = self._decayed_session(now)
        impulse = dict(spec.get("session", {}) or {})
        self._session = SessionMood(
            valence=_axis(current.valence, impulse.get("valence", 0), self.session_blend),
            arousal=_axis(current.arousal, impulse.get("arousal", 0), self.session_blend),
            irritation=_axis(
                current.irritation, impulse.get("irritation", 0), self.session_blend,
            ),
            updated_at=now,
        )
        self._turn_affect = affect
        self._events += 1
        self._record(affect.style.value, "observed")
        return affect

    def current_turn_affect(self) -> TurnAffect | None:
        if not self.enabled or self._turn_affect is None:
            return None
        if self._turn_index >= self._turn_affect.expires_at_turn:
            return None
        return self._turn_affect

    def current_session_mood(self) -> SessionMood:
        if not self.enabled:
            return SessionMood(updated_at=float(self._clock()))
        return self._decayed_session(float(self._clock()))

    def advance_turn(self) -> None:
        self._turn_index += 1

    def reset_session(self) -> None:
        now = float(self._clock())
        self._turn_index = 0
        self._turn_affect = None
        self._session = SessionMood(updated_at=now)

    def snapshot(self) -> dict[str, Any]:
        turn = self.current_turn_affect()
        return {
            "schema_version": 1,
            "enabled": self.enabled,
            "mode": "shadow",
            "turn_index": self._turn_index,
            "turn_affect": turn.model_dump(mode="json") if turn else None,
            "session_mood": self.current_session_mood().model_dump(mode="json"),
            "events_total": self._events,
        }

    def _decayed_session(self, now: float) -> SessionMood:
        elapsed = max(0.0, now - self._session.updated_at)
        factor = math.pow(0.5, elapsed / self.session_half_life_s)
        return self._session.model_copy(update={
            "valence": self._session.valence * factor,
            "arousal": self._session.arousal * factor,
            "irritation": self._session.irritation * factor,
            "updated_at": now,
        })

    def _record(self, style: str, outcome: str) -> None:
        if self._metrics is not None and hasattr(self._metrics, "record_affect_v2_event"):
            try:
                self._metrics.record_affect_v2_event(style, outcome)
            except Exception:
                pass


def _unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def _axis(current: float, impulse: Any, blend: float) -> float:
    value = float(current) + float(impulse) * blend
    return max(-1.0, min(1.0, value))


def _cause_ref(value: str | None) -> str | None:
    clean = str(value or "").strip()
    return clean if _SAFE_REF.fullmatch(clean) else None
