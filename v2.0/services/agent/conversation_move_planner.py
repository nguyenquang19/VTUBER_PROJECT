"""Pure, deterministic next-move policy for public conversation continuity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interfaces.agent import ConversationMovePlannerService
from interfaces.base import HealthStatus
from services.agent.types import ConversationMove, OpenThread, ThreadStatus


@dataclass(frozen=True)
class ConversationMoveConfig:
    summarize_after_moves: int = 4
    invite_after_moves: int = 2
    compare_after_viewer_contributions: int = 2

    @classmethod
    def from_loader(cls, loader: Any) -> "ConversationMoveConfig":
        prefix = "move_planner."
        value = cls(
            summarize_after_moves=int(
                loader.get("conversation", prefix + "summarize_after_moves", 4)
            ),
            invite_after_moves=int(
                loader.get("conversation", prefix + "invite_after_moves", 2)
            ),
            compare_after_viewer_contributions=int(loader.get(
                "conversation", prefix + "compare_after_viewer_contributions", 2,
            )),
        )
        if min(
            value.summarize_after_moves, value.invite_after_moves,
            value.compare_after_viewer_contributions,
        ) <= 0:
            raise ValueError("conversation move thresholds must be positive")
        return value


class ConversationMovePlanner(ConversationMovePlannerService):
    service_id = "conversation_move_planner"

    def __init__(self, config: ConversationMoveConfig, *, metrics: Any = None) -> None:
        self.config = config
        self._metrics = metrics
        self._running = False
        self._counts: dict[str, int] = {}

    @classmethod
    def from_loader(
        cls, loader: Any, *, metrics: Any = None,
    ) -> "ConversationMovePlanner":
        return cls(ConversationMoveConfig.from_loader(loader), metrics=metrics)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        return HealthStatus.healthy(self.service_id, choices=sum(self._counts.values()))

    def get_metrics(self) -> dict[str, Any]:
        return {
            f"conversation_move_{move}_total": count
            for move, count in sorted(self._counts.items())
        }

    def choose(self, thread: OpenThread) -> ConversationMove:
        if thread.status is ThreadStatus.PARKED:
            move = ConversationMove.RESUME
        elif thread.status is ThreadStatus.WAITING:
            move = ConversationMove.INVITE
        elif thread.last_move is ConversationMove.SUMMARIZE:
            move = ConversationMove.PARK
        elif thread.move_count >= self.config.summarize_after_moves:
            move = ConversationMove.SUMMARIZE
        elif len(thread.viewer_contributions) >= self.config.compare_after_viewer_contributions:
            move = ConversationMove.COMPARE
        elif thread.last_move is ConversationMove.DEEPEN:
            move = ConversationMove.CLARIFY
        elif thread.move_count >= self.config.invite_after_moves:
            move = ConversationMove.INVITE
        else:
            move = ConversationMove.DEEPEN
        self._counts[move.value] = self._counts.get(move.value, 0) + 1
        if self._metrics is not None and hasattr(self._metrics, "record_thread_event"):
            self._metrics.record_thread_event(f"move_{move.value}", thread.kind.value)
        return move
