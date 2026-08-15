"""Phase 13 deterministic embodiment arbitration."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from interfaces.animation import (
    AnimationCommand, EmbodimentLevel, EmbodimentPolicyService, MoodState,
)
from interfaces.base import HealthStatus


@dataclass(frozen=True)
class EmbodimentPolicyConfig:
    mid_cooldown_s: float
    intentional_cooldown_s: float
    max_evidence_refs: int
    max_recent_records: int

    @classmethod
    def from_loader(cls, loader: Any) -> "EmbodimentPolicyConfig":
        raw = loader.get("animation", "animation.embodiment", {}) or {}
        if not isinstance(raw, Mapping):
            raise ValueError("animation.embodiment must be a mapping")
        config = cls(
            mid_cooldown_s=float(raw.get("mid_cooldown_s", 0)),
            intentional_cooldown_s=float(raw.get("intentional_cooldown_s", 0)),
            max_evidence_refs=int(raw.get("max_evidence_refs", 0)),
            max_recent_records=int(raw.get("max_recent_records", 0)),
        )
        if min(config.mid_cooldown_s, config.intentional_cooldown_s) < 0:
            raise ValueError("embodiment cooldowns must be non-negative")
        if min(config.max_evidence_refs, config.max_recent_records) <= 0:
            raise ValueError("embodiment limits must be positive")
        return config


class EmbodimentPolicy(EmbodimentPolicyService):
    """No-fact, no-priority policy around the existing VTS animation adapter."""

    service_id = "embodiment_policy"

    def __init__(
        self,
        config: EmbodimentPolicyConfig,
        *,
        animation: Any,
        metrics: Any = None,
        enabled: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._animation = animation
        self._metrics = metrics
        self.enabled = bool(enabled)
        self._clock = clock or time.monotonic
        self._running = False
        self._lock = asyncio.Lock()
        self._active_high: str | None = None
        self._mid_active = False
        self._last_mid_at: float | None = None
        self._last_high_at: float | None = None
        self._counts: dict[str, int] = {}
        self._records: deque[dict[str, object]] = deque(maxlen=config.max_recent_records)

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "EmbodimentPolicy":
        return cls(EmbodimentPolicyConfig.from_loader(loader), **kwargs)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            self._active_high = None
            self._mid_active = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self.enabled:
            return HealthStatus.degraded(self.service_id, "embodiment policy disabled")
        return HealthStatus.healthy(self.service_id, active_high=self._active_high is not None)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    async def apply_mid(self, delivery_id: str, mood: MoodState) -> bool:
        """Dispatch cosmetic expression only after the caller confirmed speech delivery."""
        async with self._lock:
            now = self._clock()
            if not self.enabled or not self._running:
                self._record("mid_skipped_disabled", delivery_id=delivery_id)
                return False
            if self._active_high is not None or self._mid_active:
                self._record("mid_skipped_conflict", delivery_id=delivery_id)
                return False
            if self._in_cooldown(self._last_mid_at, self._config.mid_cooldown_s, now):
                self._record("mid_skipped_cooldown", delivery_id=delivery_id)
                return False
            self._mid_active = True
        try:
            await self._animation.express(AnimationCommand(command_type="express", mood=mood))
        except Exception:
            async with self._lock:
                self._mid_active = False
                self._record("mid_failed", delivery_id=delivery_id)
            return False
        async with self._lock:
            self._mid_active = False
            self._last_mid_at = self._clock()
            self._record("mid_applied", delivery_id=delivery_id)
        return True

    async def begin_intentional(
        self, action_id: str, gesture_id: str, evidence_refs: tuple[str, ...],
    ) -> bool:
        evidence = tuple(str(item).strip() for item in evidence_refs if str(item).strip())
        async with self._lock:
            now = self._clock()
            if not self.enabled or not self._running:
                self._record("high_rejected_disabled", action_id=action_id, gesture_id=gesture_id)
                return False
            if not evidence:
                self._record("high_rejected_evidence", action_id=action_id, gesture_id=gesture_id)
                return False
            if self._active_high is not None or self._mid_active:
                self._record("high_rejected_conflict", action_id=action_id, gesture_id=gesture_id)
                return False
            if self._in_cooldown(self._last_high_at, self._config.intentional_cooldown_s, now):
                self._record("high_rejected_cooldown", action_id=action_id, gesture_id=gesture_id)
                return False
            self._active_high = str(action_id)
            self._record("high_started", action_id=action_id, gesture_id=gesture_id, evidence_refs=evidence)
            return True

    async def finish_intentional(self, action_id: str, succeeded: bool) -> None:
        async with self._lock:
            if self._active_high != str(action_id):
                self._record("high_finish_ignored", action_id=action_id)
                return
            self._active_high = None
            if succeeded:
                self._last_high_at = self._clock()
            self._record("high_verified" if succeeded else "high_failed", action_id=action_id)

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "active_high": self._active_high,
            "mid_active": self._mid_active,
            "counts": dict(sorted(self._counts.items())),
            "recent": list(self._records),
        }

    def get_metrics(self) -> dict[str, object]:
        return {
            "embodiment_policy_enabled": self.enabled,
            "embodiment_policy_active_high": int(self._active_high is not None),
            **{f"embodiment_policy_{name}_total": count for name, count in sorted(self._counts.items())},
        }

    def _record(self, outcome: str, **fields: object) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        record = {"level": EmbodimentLevel.HIGH.value if outcome.startswith("high_") else EmbodimentLevel.MID.value,
                  "outcome": outcome, **fields}
        if "evidence_refs" in record:
            record["evidence_refs"] = tuple(record["evidence_refs"])[ : self._config.max_evidence_refs]
        self._records.append(record)
        callback = getattr(self._metrics, "record_embodiment_policy", None)
        if callable(callback):
            callback(str(record["level"]), outcome)

    @staticmethod
    def _in_cooldown(last_at: float | None, cooldown_s: float, now: float) -> bool:
        return last_at is not None and now - last_at < cooldown_s