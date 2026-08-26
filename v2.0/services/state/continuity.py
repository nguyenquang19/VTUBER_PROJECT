"""Canonical post-commit continuity owner for verified delivered turns."""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from interfaces.base import HealthStatus
from interfaces.events import CanonicalEvent, CanonicalEventRoute, EventProvenance
from interfaces.execution import OutcomeCommit, OutcomeDisposition
from interfaces.state import (
    AgentEventKind,
    AgentEventSource,
    ContinuityCommitDisposition,
    ContinuityCommitReceipt,
    ContinuityStateService,
    DeliveredTurnRecord,
)


@dataclass(frozen=True)
class ContinuityConfig:
    max_records: int
    dedup_ttl_s: float
    max_speech_age_s: float
    max_text_chars: int
    max_evidence_refs: int
    max_pending_memory_writes: int
    memory_write_timeout_s: float
    allowed_memory_scopes: tuple[str, ...]
    self_talk_history_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "max_records", "max_text_chars", "max_evidence_refs",
            "max_pending_memory_writes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"state.continuity.{name} must be a positive integer")
        for name in ("dedup_ttl_s", "max_speech_age_s", "memory_write_timeout_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) <= 0
            ):
                raise ValueError(f"state.continuity.{name} must be positive")
            object.__setattr__(self, name, float(value))
        for name in ("allowed_memory_scopes", "self_talk_history_actions"):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or not values
                or any(not isinstance(item, str) or not item.strip() for item in values)
            ):
                raise ValueError(f"state.continuity.{name} must contain canonical strings")
            normalized = tuple(item.strip() for item in values)
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"state.continuity.{name} must be unique")
            object.__setattr__(self, name, normalized)

    @classmethod
    def from_loader(cls, loader: Any) -> "ContinuityConfig":
        raw = loader.get("state", "continuity", None)
        if not isinstance(raw, Mapping):
            raise ValueError("state.continuity must be a mapping")
        expected = {
            "max_records", "dedup_ttl_s", "max_speech_age_s", "max_text_chars",
            "max_evidence_refs", "max_pending_memory_writes",
            "memory_write_timeout_s", "allowed_memory_scopes",
            "self_talk_history_actions",
        }
        if set(raw) != expected:
            raise ValueError("state.continuity config keys mismatch")
        return cls(
            max_records=raw["max_records"],
            dedup_ttl_s=raw["dedup_ttl_s"],
            max_speech_age_s=raw["max_speech_age_s"],
            max_text_chars=raw["max_text_chars"],
            max_evidence_refs=raw["max_evidence_refs"],
            max_pending_memory_writes=raw["max_pending_memory_writes"],
            memory_write_timeout_s=raw["memory_write_timeout_s"],
            allowed_memory_scopes=tuple(raw["allowed_memory_scopes"]),
            self_talk_history_actions=tuple(raw["self_talk_history_actions"]),
        )


class ContinuityCommitter(ContinuityStateService):
    """Commit exact delivered speech and every continuity projection once."""

    service_id = "continuity_state"

    def __init__(
        self,
        config: ContinuityConfig,
        *,
        authoritative_state: Any,
        prompt_history: Any,
        goal_manager: Any = None,
        memory: Any = None,
        memory_extractor: Any = None,
        metrics: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(config, ContinuityConfig):
            raise ValueError("config must be ContinuityConfig")
        if not hasattr(authoritative_state, "apply"):
            raise ValueError("authoritative_state must provide apply")
        if not hasattr(prompt_history, "commit_turn") or not hasattr(
            prompt_history, "commit_self_talk",
        ):
            raise ValueError("prompt_history has invalid commit boundary")
        self._config = config
        self._state = authoritative_state
        self._history = prompt_history
        self._goals = goal_manager
        self._memory = memory
        self._memory_extractor = memory_extractor
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: deque[DeliveredTurnRecord] = deque(maxlen=config.max_records)
        self._committed: OrderedDict[str, tuple[DeliveredTurnRecord, datetime]] = OrderedDict()
        self._memory_tasks: set[asyncio.Task[None]] = set()
        self._counts: dict[str, int] = {}
        self._memory_counts: dict[str, int] = {}
        self._running = False

    @classmethod
    def from_loader(cls, loader: Any, **kwargs: Any) -> "ContinuityCommitter":
        return cls(ContinuityConfig.from_loader(loader), **kwargs)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self.close_memory_writes()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(
            self.service_id,
            retained=len(self._records),
            memory_pending=len(self._memory_tasks),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "continuity_commit_total": self._counts.get("committed", 0),
            "continuity_duplicate_total": self._counts.get("duplicate", 0),
            "continuity_inconsistency_total": self._counts.get("inconsistent", 0),
            "continuity_memory_pending": len(self._memory_tasks),
            "continuity_memory_failed": self._memory_counts.get("failed", 0),
            "continuity_memory_completed": self._memory_counts.get("completed", 0),
            "continuity_memory_skipped": self._memory_counts.get("skipped", 0),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "retained": len(self._records),
            "memory_pending": len(self._memory_tasks),
            "counts": dict(sorted(self._counts.items())),
            "recent": [
                {
                    "continuity_id": item.continuity_id,
                    "outcome_ref": item.outcome_ref,
                    "transaction_id": item.transaction_id,
                    "delivery_id": item.delivery_id,
                    "source_mode": item.source_mode,
                    "action_type": item.action_type,
                    "delivered_at": item.delivered_at.isoformat(),
                }
                for item in self._records
            ],
        }

    def recent(self, limit: int | None = None) -> tuple[DeliveredTurnRecord, ...]:
        if limit is None:
            return tuple(self._records)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("continuity recent limit must be positive")
        return tuple(self._records)[-min(limit, self._config.max_records):]

    def commit_verified(
        self, outcome: OutcomeCommit, record: DeliveredTurnRecord,
    ) -> ContinuityCommitReceipt:
        now = _utc(self._clock())
        self._evict(now)
        self._validate_link(outcome, record, now)
        existing = self._committed.get(record.continuity_id)
        if existing is not None:
            if existing[0] != record:
                self._record("inconsistent")
                return self._receipt(
                    record, ContinuityCommitDisposition.INCONSISTENT, (),
                    ("idempotency_mismatch",), now,
                )
            self._record("duplicate")
            return self._receipt(
                record, ContinuityCommitDisposition.DUPLICATE, (), (), now,
            )

        committed: list[str] = ["record"]
        failed: list[str] = []
        self._records.append(record)
        self._committed[record.continuity_id] = (
            record, now + timedelta(seconds=self._config.dedup_ttl_s),
        )
        while len(self._committed) > self._config.max_records:
            self._committed.popitem(last=False)

        self._commit_history(record, committed, failed)
        self._publish_speech_final(record, committed, failed)
        self._publish_speech_completed(record, committed, failed)
        self._commit_focus(record, committed, failed)
        self._schedule_memory(record, committed, failed)

        disposition = (
            ContinuityCommitDisposition.INCONSISTENT
            if failed else ContinuityCommitDisposition.COMMITTED
        )
        self._record(disposition.value)
        return self._receipt(
            record, disposition, tuple(committed), tuple(failed), now,
        )

    def _validate_link(
        self, outcome: OutcomeCommit, record: DeliveredTurnRecord, now: datetime,
    ) -> None:
        if not isinstance(outcome, OutcomeCommit):
            raise ValueError("continuity requires OutcomeCommit")
        if outcome.disposition is not OutcomeDisposition.COMMITTED:
            raise ValueError("continuity requires committed outcome")
        if outcome.outcome_ref != record.outcome_ref:
            raise ValueError("continuity outcome_ref mismatch")
        if outcome.transaction_id != record.transaction_id:
            raise ValueError("continuity transaction_id mismatch")
        if len(record.speech_text) > self._config.max_text_chars:
            raise ValueError("continuity speech exceeds configured bound")
        if (
            record.history_input is not None
            and len(record.history_input) > self._config.max_text_chars
        ):
            raise ValueError("continuity history input exceeds configured bound")
        if len(record.evidence_refs) > self._config.max_evidence_refs:
            raise ValueError("continuity evidence exceeds configured bound")
        age = (now - record.delivered_at).total_seconds()
        if age < -5 or age > self._config.max_speech_age_s:
            raise ValueError("continuity delivered_at is stale or future")

    def _commit_history(
        self, record: DeliveredTurnRecord, committed: list[str], failed: list[str],
    ) -> None:
        try:
            if record.history_input is not None:
                self._history.commit_turn(record.history_input, record.speech_text)
                committed.append("history")
            elif record.action_type in self._config.self_talk_history_actions:
                self._history.commit_self_talk(record.speech_text)
                committed.append("self_talk_history")
        except Exception:
            failed.append("history")

    def _publish_speech_final(
        self, record: DeliveredTurnRecord, committed: list[str], failed: list[str],
    ) -> None:
        accepted = self._state.apply(self._speech_event(
            record,
            event_id=f"agent:speech:{record.session_id}:{record.delivery_id}",
            kind=AgentEventKind.SPEECH_FINAL,
            source=AgentEventSource.LLM,
            payload={
                "text": record.speech_text,
                "mode": record.source_mode,
                "trigger_type": record.trigger_type,
                "output_ok": record.output_ok,
            },
        ))
        (committed if accepted else failed).append("speech_final")

    def _publish_speech_completed(
        self, record: DeliveredTurnRecord, committed: list[str], failed: list[str],
    ) -> None:
        effective_goal_id = record.goal_id
        effective_intention_id = record.intention_id
        if effective_goal_id is None:
            try:
                snapshot = self._state.snapshot()
                effective_goal_id = snapshot.agent.active_goal_ref
                intention = (
                    snapshot.goals.current_intention
                    if snapshot.goals is not None else None
                )
                if (
                    intention is not None
                    and intention.goal_id == effective_goal_id
                ):
                    effective_intention_id = intention.intention_id
            except Exception:
                pass
        accepted = self._state.apply(self._speech_event(
            record,
            event_id=f"agent:speech_completed:{record.delivery_id}",
            kind=AgentEventKind.SPEECH_COMPLETED,
            source=AgentEventSource.DIRECTOR,
            payload={
                "action": record.action_type,
                "goal_id": effective_goal_id,
                "intention_id": effective_intention_id,
                "ref_event_ids": list(record.ref_event_ids),
                "text": record.speech_text,
                "thread_id": record.thread_id,
                "conversation_move": record.conversation_move,
                "expects_chat_answer": (
                    record.action_type == "ask_follow_up"
                    or record.conversation_move == "invite"
                ),
            },
        ))
        (committed if accepted else failed).append("speech_completed")

    @staticmethod
    def _speech_event(
        record: DeliveredTurnRecord,
        *,
        event_id: str,
        kind: AgentEventKind,
        source: AgentEventSource,
        payload: Mapping[str, Any],
    ) -> CanonicalEvent:
        return CanonicalEvent(
            schema_version=1,
            event_id=event_id,
            route=CanonicalEventRoute.AGENT,
            source=source.value,
            event_type=kind.value,
            timestamp=record.delivered_at,
            confidence=1.0 if record.output_ok else 0.7,
            payload=payload,
            provenance=EventProvenance(
                producer="continuity_state",
                source_event_id=record.outcome_ref,
                session_id=record.session_id,
            ),
            dedup_key=event_id,
        )

    def _commit_focus(
        self, record: DeliveredTurnRecord, committed: list[str], failed: list[str],
    ) -> None:
        if self._goals is None or not hasattr(self._goals, "focus_delivered_thread"):
            return
        if record.action_type != "read_chat":
            return
        try:
            if record.history_input is not None:
                raw_ids = {value for value in record.ref_event_ids if value}
                source_ids = {*raw_ids, *(f"agent:chat:{value}" for value in raw_ids)}
                parent_id = self._matching_thread(source_ids)
                self._goals.focus_delivered_thread(
                    parent_id, source_event_ids=source_ids,
                )
                committed.append("focus")
            else:
                self._goals.focus_delivered_thread(
                    None, reason="room_reaction_delivered",
                )
                committed.append("focus_clear")
        except Exception:
            failed.append("focus")

    def _matching_thread(self, source_ids: set[str]) -> str | None:
        snapshot = self._state.snapshot()
        matches = [
            thread for thread in snapshot.agent.open_threads
            if (
                thread.origin_event_id in source_ids
                or any(item.source_event_id in source_ids for item in thread.evidence)
            )
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (item.updated_at, item.thread_id)).thread_id

    def _schedule_memory(
        self, record: DeliveredTurnRecord, committed: list[str], failed: list[str],
    ) -> None:
        if record.history_input is None or self._memory is None or self._memory_extractor is None:
            return
        if record.viewer_ref is not None and "viewer" not in self._config.allowed_memory_scopes:
            failed.append("memory_privacy")
            return
        if record.viewer_ref is None and "session" not in self._config.allowed_memory_scopes:
            failed.append("memory_privacy")
            return
        from services.memory.extractor import TurnData

        try:
            entry = self._memory_extractor.extract(TurnData(
                user_input=record.history_input,
                mai_output=record.speech_text,
                mood_dominant=record.mood_dominant,
                mood_intensity=record.mood_intensity,
                viewer_id=record.viewer_ref,
                session_id=record.session_id,
                trigger_type=record.trigger_type,
                timestamp=record.delivered_at,
                delivery_verified=True,
                outcome_id=record.outcome_ref,
            ))
        except Exception:
            failed.append("memory_extract")
            return
        if entry is None:
            self._memory_record("skipped")
            return
        if len(self._memory_tasks) >= self._config.max_pending_memory_writes:
            self._memory_record("skipped")
            failed.append("memory_backpressure")
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._write_memory(entry),
                name=f"continuity_memory_{record.continuity_id[-8:]}",
            )
        except RuntimeError:
            self._memory_record("skipped")
            failed.append("memory_loop")
            return
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_done)
        committed.append("memory_enqueued")

    async def _write_memory(self, entry: Any) -> None:
        async with asyncio.timeout(self._config.memory_write_timeout_s):
            await self._memory.write(entry)

    def _memory_done(self, task: asyncio.Task[None]) -> None:
        self._memory_tasks.discard(task)
        if task.cancelled():
            self._memory_record("skipped")
            return
        try:
            task.result()
        except Exception:
            self._memory_record("failed")
        else:
            self._memory_record("completed")

    async def close_memory_writes(self) -> None:
        tasks = tuple(self._memory_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._memory_tasks.clear()

    def _evict(self, now: datetime) -> None:
        expired = [
            key for key, (_record, expires_at) in self._committed.items()
            if expires_at <= now
        ]
        for key in expired:
            del self._committed[key]

    def _receipt(
        self,
        record: DeliveredTurnRecord,
        disposition: ContinuityCommitDisposition,
        committed: tuple[str, ...],
        failed: tuple[str, ...],
        now: datetime,
    ) -> ContinuityCommitReceipt:
        return ContinuityCommitReceipt(
            schema_version=1,
            continuity_id=record.continuity_id,
            disposition=disposition,
            committed_facets=tuple(dict.fromkeys(committed)),
            failed_facets=tuple(dict.fromkeys(failed)),
            completed_at=now,
        )

    def _record(self, outcome: str) -> None:
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_continuity_commit", None)
        if callable(recorder):
            try:
                recorder(outcome)
            except Exception:
                pass

    def _memory_record(self, outcome: str) -> None:
        self._memory_counts[outcome] = self._memory_counts.get(outcome, 0) + 1


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("continuity clock must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = ["ContinuityCommitter", "ContinuityConfig"]
