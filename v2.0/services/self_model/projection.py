"""Pure, read-only SelfSnapshot projection for Phase 3."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.compatibility import SelfSnapshot
from interfaces.self_model import SelfModelService


_ACTIVE_TRANSACTION_STATES = frozenset({"reserved", "generated", "delivering", "delivered"})
_TRANSACTION_STATES = _ACTIVE_TRANSACTION_STATES | frozenset({"committed", "released"})


@dataclass(frozen=True)
class SelfModelConfig:
    max_recent_action_ids: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_recent_action_ids, bool)
            or not isinstance(self.max_recent_action_ids, int)
            or self.max_recent_action_ids <= 0
        ):
            raise ValueError("self_model.max_recent_action_ids must be a positive integer")

    @classmethod
    def from_loader(cls, loader: Any) -> "SelfModelConfig":
        return cls(max_recent_action_ids=loader.get(
            "agent_state", "self_model.max_recent_action_ids", None,
        ))


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

        current_topic, focused_thread_id, failed = _agent_values(agent_snapshot)
        source_failed = source_failed or failed
        active_goal_id, failed = _goal_value(goal_snapshot)
        source_failed = source_failed or failed
        transactions, failed = _recent_transactions(transaction_snapshot)
        source_failed = source_failed or failed
        active = next((item for item in transactions if item["state"] in _ACTIVE_TRANSACTION_STATES), None)
        action_ids = tuple(
            item["transaction_id"] for item in transactions[:self._config.max_recent_action_ids]
        )
        current_action_id = active["transaction_id"] if active is not None else None
        degraded = bool(source_failed or avatar_degraded or health_degraded)
        busy = bool(speaking or active is not None)
        snapshot = SelfSnapshot(
            snapshot_id=_snapshot_id({
                "speaking": speaking,
                "busy": busy,
                "degraded": degraded,
                "current_action_id": current_action_id,
                "active_goal_id": active_goal_id,
                "focused_thread_id": focused_thread_id,
                "current_topic": current_topic,
                "avatar_state": avatar_state,
                "recent_action_ids": action_ids,
            }),
            created_at=now,
            speaking=speaking,
            busy=busy,
            degraded=degraded,
            current_action_id=current_action_id,
            current_intention_id=None,
            active_goal_id=active_goal_id,
            focused_thread_id=focused_thread_id,
            current_topic=current_topic,
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
            try:
                self._metrics.record_self_model_snapshot(
                    outcome, snapshot.degraded, len(snapshot.recent_action_ids),
                )
            except Exception:
                pass


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
        return None, True
    try:
        return source.snapshot(), False
    except Exception:
        return None, True


def _read_speaking(player: Any) -> tuple[bool, bool]:
    if player is None:
        return False, True
    try:
        value = player.is_playing
        if not isinstance(value, bool):
            return False, True
        return value, False
    except Exception:
        return False, True


def _read_avatar(animation: Any) -> tuple[dict[str, bool], bool, bool]:
    if animation is None:
        return {}, True, True
    try:
        enabled = getattr(animation, "enabled")
        metrics = animation.get_metrics()
        if not isinstance(enabled, bool) or not isinstance(metrics, Mapping):
            return {}, True, True
        connected = metrics.get("animation_connected")
        if not isinstance(connected, bool):
            return {}, True, True
        return {"enabled": enabled, "connected": connected}, bool(enabled and not connected), False
    except Exception:
        return {}, True, True


def _read_health(provider: Callable[[], Mapping[str, Any] | None] | None) -> tuple[bool, bool]:
    if provider is None:
        return True, True
    try:
        snapshot = provider()
        if not isinstance(snapshot, Mapping):
            return True, True
        targets = snapshot.get("targets")
        if not isinstance(targets, Mapping):
            return True, True
        degraded = not targets
        malformed = False
        for item in targets.values():
            if not isinstance(item, Mapping):
                malformed = True
                continue
            health = item.get("health")
            if not isinstance(health, str) or not health.strip():
                malformed = True
                continue
            if health.strip().lower() != "healthy":
                degraded = True
        return bool(degraded or malformed), malformed
    except Exception:
        return True, True


def _agent_values(snapshot: Any) -> tuple[str | None, str | None, bool]:
    topic, topic_present = _member(snapshot, "current_topic")
    threads, threads_present = _member(snapshot, "open_threads")
    if not topic_present or not threads_present or not isinstance(threads, (list, tuple)):
        return None, None, True
    current_topic, failed = _optional_member_text(topic, "summary")
    focused: tuple[float, str] | None = None
    for thread in threads:
        thread_id, thread_id_valid = _required_member_text(thread, "thread_id")
        updated_at, updated_present = _member(thread, "updated_at")
        if not thread_id_valid or not updated_present or not isinstance(updated_at, datetime):
            failed = True
            continue
        try:
            timestamp = _utc(updated_at).timestamp()
        except (TypeError, ValueError):
            failed = True
            continue
        candidate = (timestamp, thread_id)
        if focused is None or candidate > focused:
            focused = candidate
    return current_topic, focused[1] if focused is not None else None, failed


def _goal_value(snapshot: Any) -> tuple[str | None, bool]:
    active, active_present = _member(snapshot, "active")
    if not active_present:
        return None, True
    return _optional_member_text(active, "goal_id")


def _recent_transactions(snapshot: Any) -> tuple[tuple[dict[str, Any], ...], bool]:
    if not isinstance(snapshot, Mapping):
        return (), True
    recent = snapshot.get("recent")
    if not isinstance(recent, (list, tuple)):
        return (), True
    values: list[dict[str, Any]] = []
    failed = False
    for item in recent:
        if not isinstance(item, Mapping):
            failed = True
            continue
        transaction_id = item.get("transaction_id")
        state = item.get("state")
        updated_at = item.get("updated_at")
        if (
            not isinstance(transaction_id, str)
            or not transaction_id.strip()
            or not isinstance(state, str)
            or state not in _TRANSACTION_STATES
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
        ):
            failed = True
            continue
        values.append({
            "transaction_id": transaction_id.strip(),
            "state": state,
            "updated_at": float(updated_at),
        })
    values.sort(
        key=lambda item: (item["updated_at"], item["transaction_id"]),
        reverse=True,
    )
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        transaction_id = item["transaction_id"]
        if transaction_id in seen:
            failed = True
            continue
        seen.add(transaction_id)
        deduplicated.append(item)
    return tuple(deduplicated), failed


def _member(value: Any, name: str) -> tuple[Any, bool]:
    if isinstance(value, Mapping):
        return value.get(name), name in value
    if value is None or not hasattr(value, name):
        return None, False
    return getattr(value, name), True


def _required_member_text(value: Any, name: str) -> tuple[str, bool]:
    raw, present = _member(value, name)
    if not present or not isinstance(raw, str) or not raw.strip():
        return "", False
    return raw.strip(), True


def _optional_member_text(value: Any, name: str) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text, valid = _required_member_text(value, name)
    return (text if valid else None), not valid


def _snapshot_id(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "self-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("self model clock must be timezone-aware")
    return value.astimezone(timezone.utc)
