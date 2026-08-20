"""Deterministic LOW/MID/HIGH embodiment arbitration."""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from interfaces.animation import (
    AnimationCommand,
    AnimationService,
    EmbodimentLevel,
    EmbodimentPolicyService,
    EmbodimentRecord,
    EmbodimentSnapshot,
    IntentionalGestureOutcome,
    MoodState,
)
from interfaces.base import HealthStatus


def _finite_number(value: Any, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        qualifier = "positive and finite" if positive else "non-negative and finite"
        raise ValueError(f"{field_name} must be {qualifier}")
    return result


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _bounded_label(value: Any, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a trimmed non-empty string")
    if len(value) > max_chars:
        raise ValueError(f"{field_name} exceeds configured bound")
    return value


@dataclass(frozen=True)
class EmbodimentPolicyConfig:
    mid_cooldown_s: float
    mid_timeout_s: float
    intentional_cooldown_s: float
    intentional_lease_ttl_s: float
    max_evidence_refs: int
    max_recent_records: int
    max_id_chars: int
    max_gesture_id_chars: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "mid_cooldown_s",
            _finite_number(self.mid_cooldown_s, "mid_cooldown_s"),
        )
        object.__setattr__(
            self, "mid_timeout_s",
            _finite_number(self.mid_timeout_s, "mid_timeout_s", positive=True),
        )
        object.__setattr__(
            self, "intentional_cooldown_s",
            _finite_number(self.intentional_cooldown_s, "intentional_cooldown_s"),
        )
        object.__setattr__(
            self, "intentional_lease_ttl_s",
            _finite_number(
                self.intentional_lease_ttl_s, "intentional_lease_ttl_s", positive=True,
            ),
        )
        for name in (
            "max_evidence_refs", "max_recent_records", "max_id_chars",
            "max_gesture_id_chars",
        ):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))

    @classmethod
    def from_loader(cls, loader: Any) -> "EmbodimentPolicyConfig":
        raw = loader.get("animation", "animation.embodiment", None)
        if not isinstance(raw, Mapping):
            raise ValueError("animation.embodiment must be a mapping")
        expected = {
            "mid_cooldown_s", "mid_timeout_s", "intentional_cooldown_s",
            "intentional_lease_ttl_s",
            "max_evidence_refs", "max_recent_records", "max_id_chars",
            "max_gesture_id_chars",
        }
        if set(raw) != expected:
            missing = sorted(expected - set(raw))
            extra = sorted(set(raw) - expected)
            raise ValueError(
                f"animation.embodiment keys invalid; missing={missing}, extra={extra}",
            )
        return cls(**{name: raw[name] for name in expected})


@dataclass(frozen=True)
class _HighLease:
    action_id: str
    gesture_id: str
    evidence_refs: tuple[str, ...]
    started_at: float


class EmbodimentPolicy(EmbodimentPolicyService):
    """No-fact, no-priority policy around the existing VTS animation adapter."""

    service_id = "embodiment_policy"

    def __init__(
        self,
        config: EmbodimentPolicyConfig,
        *,
        animation: AnimationService,
        metrics: Any = None,
        enabled: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(config, EmbodimentPolicyConfig):
            raise ValueError("config must be EmbodimentPolicyConfig")
        if not isinstance(animation, AnimationService):
            raise ValueError("animation must implement AnimationService")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        self._config = config
        self._animation = animation
        self._metrics = metrics
        self.enabled = enabled
        self._clock = clock or time.monotonic
        self._running = False
        self._lock = asyncio.Lock()
        self._active_high: _HighLease | None = None
        self._mid_active = False
        self._mid_attempt_sequence = 0
        self._active_mid_attempt: int | None = None
        self._last_mid_at: float | None = None
        self._last_high_at: float | None = None
        self._counts: dict[str, int] = {}
        self._records: deque[EmbodimentRecord] = deque(maxlen=config.max_recent_records)
        self._sequence = 0

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "EmbodimentPolicy":
        return cls(EmbodimentPolicyConfig.from_loader(loader), **kwargs)

    async def start(self) -> None:
        async with self._lock:
            self._running = True

    async def stop(self) -> None:
        async with self._lock:
            if self._active_high is not None:
                lease = self._active_high
                self._active_high = None
                self._last_high_at = self._now()
                self._record_locked(
                    "high_cancelled", level=EmbodimentLevel.HIGH,
                    action_id=lease.action_id, gesture_id=lease.gesture_id,
                    evidence_refs=lease.evidence_refs,
                )
            if self._mid_active:
                self._record_locked("mid_cancelled", level=EmbodimentLevel.MID)
            self._mid_active = False
            self._active_mid_attempt = None
            self._running = False

    async def health_check(self) -> HealthStatus:
        async with self._lock:
            self._expire_stale_locked(self._now())
            running = self._running
            enabled = self.enabled
            active = self._active_high is not None
        if not running:
            return HealthStatus.stopped(self.service_id)
        if not enabled:
            return HealthStatus.degraded(self.service_id, "embodiment policy disabled")
        if not await self._animation_usable():
            return HealthStatus.degraded(self.service_id, "animation adapter unavailable")
        return HealthStatus.healthy(self.service_id, active_high=active)

    async def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        async with self._lock:
            if self.enabled and not enabled and self._active_high is not None:
                lease = self._active_high
                self._active_high = None
                self._last_high_at = self._now()
                self._record_locked(
                    "high_cancelled", level=EmbodimentLevel.HIGH,
                    action_id=lease.action_id, gesture_id=lease.gesture_id,
                    evidence_refs=lease.evidence_refs,
                )
            if self.enabled and not enabled and self._mid_active:
                self._mid_active = False
                self._active_mid_attempt = None
                self._record_locked("mid_cancelled", level=EmbodimentLevel.MID)
            self.enabled = enabled

    async def apply_mid(self, delivery_id: str, mood: MoodState) -> bool:
        """Dispatch cosmetic expression only after confirmed speech delivery."""
        try:
            delivery = _bounded_label(
                delivery_id, "delivery_id", self._config.max_id_chars,
            )
            if not isinstance(mood, MoodState):
                raise ValueError("mood must be MoodState")
        except ValueError:
            async with self._lock:
                self._record_locked("mid_rejected_contract", level=EmbodimentLevel.MID)
            return False
        if not await self._animation_usable():
            async with self._lock:
                self._record_locked(
                    "mid_skipped_unavailable", level=EmbodimentLevel.MID,
                    delivery_id=delivery,
                )
            return False
        async with self._lock:
            now = self._now()
            self._expire_stale_locked(now)
            if not self.enabled or not self._running:
                self._record_locked(
                    "mid_skipped_disabled", level=EmbodimentLevel.MID,
                    delivery_id=delivery,
                )
                return False
            if self._active_high is not None or self._mid_active:
                self._record_locked(
                    "mid_skipped_conflict", level=EmbodimentLevel.MID,
                    delivery_id=delivery,
                )
                return False
            if self._in_cooldown(self._last_mid_at, self._config.mid_cooldown_s, now):
                self._record_locked(
                    "mid_skipped_cooldown", level=EmbodimentLevel.MID,
                    delivery_id=delivery,
                )
                return False
            self._mid_active = True
            self._mid_attempt_sequence += 1
            attempt = self._mid_attempt_sequence
            self._active_mid_attempt = attempt
        try:
            await asyncio.wait_for(
                self._animation.express(AnimationCommand(command_type="express", mood=mood)),
                timeout=self._config.mid_timeout_s,
            )
        except asyncio.CancelledError:
            async with self._lock:
                if self._active_mid_attempt == attempt:
                    self._mid_active = False
                    self._active_mid_attempt = None
                    self._record_locked(
                        "mid_cancelled", level=EmbodimentLevel.MID,
                        delivery_id=delivery,
                    )
            raise
        except asyncio.TimeoutError:
            async with self._lock:
                if self._active_mid_attempt == attempt:
                    self._mid_active = False
                    self._active_mid_attempt = None
                    self._record_locked(
                        "mid_timeout", level=EmbodimentLevel.MID, delivery_id=delivery,
                    )
            return False
        except Exception:
            async with self._lock:
                if self._active_mid_attempt == attempt:
                    self._mid_active = False
                    self._active_mid_attempt = None
                    self._record_locked(
                        "mid_failed", level=EmbodimentLevel.MID, delivery_id=delivery,
                    )
            return False
        async with self._lock:
            if self._active_mid_attempt != attempt:
                return False
            self._mid_active = False
            self._active_mid_attempt = None
            self._last_mid_at = self._now()
            self._record_locked(
                "mid_dispatched", level=EmbodimentLevel.MID, delivery_id=delivery,
            )
        return True

    async def begin_intentional(
        self, action_id: str, gesture_id: str, evidence_refs: tuple[str, ...],
    ) -> bool:
        try:
            action = _bounded_label(action_id, "action_id", self._config.max_id_chars)
            gesture = _bounded_label(
                gesture_id, "gesture_id", self._config.max_gesture_id_chars,
            )
            evidence = self._validate_evidence(evidence_refs)
        except ValueError:
            async with self._lock:
                self._record_locked("high_rejected_contract", level=EmbodimentLevel.HIGH)
            return False
        try:
            allowlisted = self._animation.is_intentional_gesture_allowed(gesture)
        except Exception:
            allowlisted = False
        if allowlisted is not True:
            async with self._lock:
                self._record_locked(
                    "high_rejected_allowlist", level=EmbodimentLevel.HIGH,
                    action_id=action, gesture_id=gesture, evidence_refs=evidence,
                )
            return False
        if not await self._animation_usable():
            async with self._lock:
                self._record_locked(
                    "high_rejected_unavailable", level=EmbodimentLevel.HIGH,
                    action_id=action, gesture_id=gesture, evidence_refs=evidence,
                )
            return False
        async with self._lock:
            now = self._now()
            self._expire_stale_locked(now)
            if not self.enabled or not self._running:
                self._record_locked(
                    "high_rejected_disabled", level=EmbodimentLevel.HIGH,
                    action_id=action, gesture_id=gesture, evidence_refs=evidence,
                )
                return False
            if self._active_high is not None or self._mid_active:
                self._record_locked(
                    "high_rejected_conflict", level=EmbodimentLevel.HIGH,
                    action_id=action, gesture_id=gesture, evidence_refs=evidence,
                )
                return False
            if self._in_cooldown(self._last_high_at, self._config.intentional_cooldown_s, now):
                self._record_locked(
                    "high_rejected_cooldown", level=EmbodimentLevel.HIGH,
                    action_id=action, gesture_id=gesture, evidence_refs=evidence,
                )
                return False
            self._active_high = _HighLease(action, gesture, evidence, now)
            self._record_locked(
                "high_started", level=EmbodimentLevel.HIGH,
                action_id=action, gesture_id=gesture, evidence_refs=evidence,
            )
            return True

    async def finish_intentional(
        self,
        action_id: str,
        outcome: IntentionalGestureOutcome,
        verification_source: str | None = None,
    ) -> bool:
        action = _bounded_label(action_id, "action_id", self._config.max_id_chars)
        if not isinstance(outcome, IntentionalGestureOutcome):
            raise ValueError("outcome must be IntentionalGestureOutcome")
        source: str | None = None
        if outcome is IntentionalGestureOutcome.VERIFIED:
            source = _bounded_label(
                verification_source, "verification_source", self._config.max_id_chars,
            )
        elif verification_source is not None:
            raise ValueError("verification_source is only valid for verified outcome")
        async with self._lock:
            now = self._now()
            self._expire_stale_locked(now)
            lease = self._active_high
            if lease is None or lease.action_id != action:
                self._record_locked(
                    "high_finish_ignored", level=EmbodimentLevel.HIGH,
                    action_id=action,
                )
                return False
            self._active_high = None
            self._last_high_at = now
            self._record_locked(
                f"high_{outcome.value}", level=EmbodimentLevel.HIGH,
                action_id=lease.action_id, gesture_id=lease.gesture_id,
                evidence_refs=lease.evidence_refs, verification_source=source,
            )
            return True

    def snapshot(self) -> EmbodimentSnapshot:
        lease = self._active_high
        if (
            lease is not None
            and self._now() - lease.started_at >= self._config.intentional_lease_ttl_s
        ):
            lease = None
        active_level = (
            EmbodimentLevel.HIGH if lease is not None
            else EmbodimentLevel.MID if self._mid_active
            else None
        )
        return EmbodimentSnapshot(
            running=self._running,
            enabled=self.enabled,
            active_level=active_level,
            active_action_id=lease.action_id if lease is not None else None,
            active_gesture_id=lease.gesture_id if lease is not None else None,
            counts=dict(sorted(self._counts.items())),
            recent=tuple(self._records),
        )

    def get_metrics(self) -> dict[str, object]:
        snapshot = self.snapshot()
        return {
            "embodiment_policy_running": snapshot.running,
            "embodiment_policy_enabled": snapshot.enabled,
            "embodiment_policy_active_high": int(
                snapshot.active_level is EmbodimentLevel.HIGH
            ),
            "embodiment_policy_active_mid": int(
                snapshot.active_level is EmbodimentLevel.MID
            ),
            "embodiment_policy_recent_records": len(snapshot.recent),
            **{
                f"embodiment_policy_{name}_total": count
                for name, count in sorted(snapshot.counts.items())
            },
        }

    async def _animation_usable(self) -> bool:
        if getattr(self._animation, "enabled", None) is not True:
            return False
        try:
            health = await self._animation.health_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return isinstance(health, HealthStatus) and health.is_ok

    def _validate_evidence(self, values: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(values, tuple) or not values:
            raise ValueError("evidence_refs must be a non-empty tuple")
        if len(values) > self._config.max_evidence_refs:
            raise ValueError("evidence_refs exceed configured bound")
        result = tuple(
            _bounded_label(value, "evidence_ref", self._config.max_id_chars)
            for value in values
        )
        if len(set(result)) != len(result):
            raise ValueError("evidence_refs must be unique")
        return result

    def _expire_stale_locked(self, now: float) -> None:
        lease = self._active_high
        if lease is None or now - lease.started_at < self._config.intentional_lease_ttl_s:
            return
        self._active_high = None
        self._last_high_at = now
        self._record_locked(
            "high_expired", level=EmbodimentLevel.HIGH,
            action_id=lease.action_id, gesture_id=lease.gesture_id,
            evidence_refs=lease.evidence_refs,
        )

    def _record_locked(
        self,
        outcome: str,
        *,
        level: EmbodimentLevel,
        delivery_id: str | None = None,
        action_id: str | None = None,
        gesture_id: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        verification_source: str | None = None,
    ) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        self._sequence += 1
        record = EmbodimentRecord(
            sequence=self._sequence,
            level=level,
            outcome=outcome,
            delivery_id=delivery_id,
            action_id=action_id,
            gesture_id=gesture_id,
            evidence_refs=evidence_refs,
            verification_source=verification_source,
        )
        self._records.append(record)
        callback = getattr(self._metrics, "record_embodiment_policy", None)
        if callable(callback):
            try:
                callback(level.value, outcome)
            except Exception:
                pass

    def _now(self) -> float:
        return _finite_number(self._clock(), "clock")

    @staticmethod
    def _in_cooldown(last_at: float | None, cooldown_s: float, now: float) -> bool:
        return last_at is not None and now - last_at < cooldown_s
