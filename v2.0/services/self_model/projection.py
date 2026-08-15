"""Pure, read-only SelfSnapshot projection for Phase 3."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import SelfSnapshot
from interfaces.self_model import SelfModelService


_ACTIVE_TRANSACTION_STATES = frozenset({"reserved", "generated", "delivering"})
_DEGRADED_HEALTH_STATES = frozenset({"degraded", "unhealthy"})


@dataclass(frozen=True)
class SelfModelConfig:
    max_recent_action_ids: int

    @classmethod
    def from_loader(cls, loader: Any) -> "SelfModelConfig":
        config = cls(max_recent_action_ids=int(loader.get(
            "agent_state", "self_model.max_recent_action_ids", 0,
        )))
        if config.max_recent_action_ids <= 0:
            raise ValueError("self_model.max_recent_action_ids must be positive")
        return config


class SelfModelProjection(SelfModelService):
    """Assemble immutable self state from public, authoritative snapshots only."""

    service_id = "self_model_projection"

    def __init__(
        self,
        config: SelfModelConfig,
        *,
        agent_state: Any,
        goal_manager: Any,
        action_transactions: Any,
        audio_player: Any = None,
        animation: Any = None,
        health_snapshot_provider: Callable[[], Mapping[str, Any] | None] | None = None,
        metrics: Any = None,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._agent_state = agent_state
        self._goal_manager = goal_manager
        self._action_transactions = action_transactions
        self._audio_player = audio_player
        self._animation = animation
        self._health_snapshot_provider = health_snapshot_provider
        self._metrics = metrics
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False
        self._snapshots: dict[str, int] = {}

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "SelfModelProjection":
        return cls(SelfModelConfig.from_loader(loader), **kwargs)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "self model projection disabled")
        return HealthStatus.healthy(self.service_id)

    def snapshot(self) -> SelfSnapshot:
        now = _utc(self._clock())
        if not self._enabled:
            snapshot = _empty_snapshot(now, degraded=True)
            self._record("disabled", snapshot)
            return snapshot

        source_failed = False
        agent_snapshot, failed = _read_snapshot(self._agent_state)
        source_failed = source_failed or failed
        goal_snapshot, failed = _read_snapshot(self._goal_manager)
        source_failed = source_failed or failed
        transaction_snapshot, failed = _read_snapshot(self._action_transactions)
        source_failed = source_failed or failed
        speaking, failed = _read_speaking(self._audio_player)
        source_failed = source_failed or failed
        avatar_state, avatar_degraded, failed = _read_avatar(self._animation)
        source_failed = source_failed or failed
        health_degraded, failed = _read_health(self._health_snapshot_provider)
        source_failed = source_failed or failed

        transactions = _recent_transactions(transaction_snapshot)
        active = next((item for item in transactions if str(item.get("state")) in _ACTIVE_TRANSACTION_STATES), None)
        action_ids = tuple(
            str(item["transaction_id"])
            for item in transactions[:self._config.max_recent_action_ids]
            if str(item.get("transaction_id") or "").strip()
        )
        topic = getattr(agent_snapshot, "current_topic", None)
        threads = tuple(getattr(agent_snapshot, "open_threads", ()) or ())
        focused = max(
            threads,
            key=lambda item: (getattr(item, "updated_at", now), str(getattr(item, "thread_id", ""))),
            default=None,
        )
        active_goal = getattr(goal_snapshot, "active", None)
        snapshot = SelfSnapshot(
            snapshot_id=_snapshot_id({
                "speaking": speaking,
                "busy": bool(speaking or active is not None),
                "degraded": bool(source_failed or avatar_degraded or health_degraded),
                "current_action_id": _text(active, "transaction_id"),
                "active_goal_id": _text(active_goal, "goal_id"),
                "focused_thread_id": _text(focused, "thread_id"),
                "current_topic": _text(topic, "summary"),
                "avatar_state": avatar_state,
                "recent_action_ids": action_ids,
            }),
            created_at=now,
            speaking=speaking,
            busy=bool(speaking or active is not None),
            degraded=bool(source_failed or avatar_degraded or health_degraded),
            current_action_id=_text(active, "transaction_id"),
            current_intention_id=None,
            active_goal_id=_text(active_goal, "goal_id"),
            focused_thread_id=_text(focused, "thread_id"),
            current_topic=_text(topic, "summary"),
            attention_target=None,
            avatar_state=avatar_state,
            recent_action_ids=action_ids,
        )
        self._record("projected", snapshot)
        return snapshot

    def get_metrics(self) -> dict[str, Any]:
        return {
            "self_model_enabled": self._enabled,
            "self_model_snapshots": dict(sorted(self._snapshots.items())),
        }

    def _record(self, outcome: str, snapshot: SelfSnapshot) -> None:
        self._snapshots[outcome] = self._snapshots.get(outcome, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_self_model_snapshot"):
            self._metrics.record_self_model_snapshot(outcome, snapshot.degraded, len(snapshot.recent_action_ids))


def _empty_snapshot(now: datetime, *, degraded: bool) -> SelfSnapshot:
    return SelfSnapshot(
        snapshot_id="self-disabled",
        created_at=now,
        speaking=False,
        busy=False,
        degraded=degraded,
        current_action_id=None,
        current_intention_id=None,
        active_goal_id=None,
        focused_thread_id=None,
        current_topic=None,
        attention_target=None,
        avatar_state={},
        recent_action_ids=(),
    )


def _read_snapshot(source: Any) -> tuple[Any, bool]:
    if source is None or not hasattr(source, "snapshot"):
        return None, False
    try:
        return source.snapshot(), False
    except Exception:
        return None, True


def _read_speaking(player: Any) -> tuple[bool, bool]:
    if player is None:
        return False, False
    try:
        return bool(player.is_playing), False
    except Exception:
        return False, True


def _read_avatar(animation: Any) -> tuple[dict[str, bool], bool, bool]:
    if animation is None:
        return {}, False, False
    try:
        enabled = bool(getattr(animation, "enabled", False))
        metrics = animation.get_metrics() if hasattr(animation, "get_metrics") else {}
        connected = bool(metrics.get("animation_connected", False))
        return {"enabled": enabled, "connected": connected}, bool(enabled and not connected), False
    except Exception:
        return {}, True, True


def _read_health(provider: Callable[[], Mapping[str, Any] | None] | None) -> tuple[bool, bool]:
    if provider is None:
        return False, False
    try:
        snapshot = provider() or {}
        targets = snapshot.get("targets", {}) if isinstance(snapshot, Mapping) else {}
        if not isinstance(targets, Mapping):
            return False, True
        return any(
            str(item.get("health", "")).lower() in _DEGRADED_HEALTH_STATES
            for item in targets.values() if isinstance(item, Mapping)
        ), False
    except Exception:
        return False, True


def _recent_transactions(snapshot: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(snapshot, Mapping):
        return ()
    recent = snapshot.get("recent", ())
    if not isinstance(recent, (list, tuple)):
        return ()
    values = [item for item in recent if isinstance(item, Mapping)]
    values.sort(
        key=lambda item: (_timestamp(item.get("updated_at")), str(item.get("transaction_id", ""))),
        reverse=True,
    )
    return tuple(values)


def _timestamp(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any, name: str) -> str | None:
    if isinstance(value, Mapping):
        raw = value.get(name)
    else:
        raw = getattr(value, name, None)
    text = str(raw or "").strip()
    return text or None


def _snapshot_id(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "self-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("self model clock must be timezone-aware")
    return value.astimezone(timezone.utc)
