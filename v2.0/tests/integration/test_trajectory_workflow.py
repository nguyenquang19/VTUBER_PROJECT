from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.dashboard_server import DashboardServer
from interfaces.director_v2 import (
    DirectorV2Candidate,
    DirectorV2Context,
    DirectorV2Proposal,
    DirectorV2TakeoverSelection,
)
from interfaces.trajectory import TrajectorySnapshotRefs
from services.execution.transaction import ActionTransactionManager
from services.director.decision_record import DecisionRecordManager
from services.director.director import DirectorAction
from services.director.trajectory import TrajectoryConfig, TrajectoryRecorder
from tests.integration.test_director_loop import _make


def _recorder() -> TrajectoryRecorder:
    return TrajectoryRecorder(
        TrajectoryConfig(1, 8, 8, 8, 8, 8, 120),
        snapshot_provider=lambda: TrajectorySnapshotRefs(
            "world-2", "self-2", "capabilities-2",
        ),
        clock=lambda: 2.0,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_accepted_v2_decision_records_complete_replayable_trajectory() -> None:
    context = DirectorV2Context(
        created_at=1.0,
        world_snapshot_id="world-1",
        self_snapshot_id="self-1",
        capability_snapshot_id="capabilities-1",
        candidates=(DirectorV2Candidate(
            "chat", "m1", "READ_CHAT", "READ_CHAT", 10.0, ("chat:m1",),
        ),),
    )
    proposal = DirectorV2Proposal(
        "p-read", 1.0, "READ_CHAT", "READ_CHAT", "m1",
        ("selected", "source_chat", "validated"), ("chat:m1",), 10.0,
    )

    class StaticShadow:
        @staticmethod
        def propose_current() -> DirectorV2Proposal:
            return proposal

        @staticmethod
        def trajectory_context(proposal_id: str) -> DirectorV2Context | None:
            return context if proposal_id == proposal.proposal_id else None

    class AcceptingSelector:
        @staticmethod
        def evaluate(**_kwargs: object) -> DirectorV2TakeoverSelection:
            return DirectorV2TakeoverSelection(
                True, "READ_CHAT", "accepted", "READ_CHAT", "p-read", "director_v2",
            )

    loop, _director, pool, _pulse, _runner, clock = _make()
    recorder = _recorder()
    loop._transactions = ActionTransactionManager(enabled=True)
    loop._decision_records = DecisionRecordManager(enabled=True, clock=lambda: 2.0)
    loop._trajectory_records = recorder
    loop.configure_director_v2_takeover(StaticShadow(), AcceptingSelector())
    pool.add("m1", "raw private chat", now=0.0, kind="mention")
    clock["t"] = 1.0

    assert await loop.tick_once() is DirectorAction.READ_CHAT
    current = recorder.snapshot()["current"]
    assert current["owner"] == "director_v2"
    assert current["lifecycle"] == "completed"
    assert current["action_request"]["action_type"] == "READ_CHAT"
    assert current["action_result"]["verified"] is True
    assert current["verification"]["reason_code"] == "committed"
    assert "raw private chat" not in str(current)
    assert recorder.replay("p-read", lambda _context: proposal).matched is True


@pytest.mark.asyncio
async def test_rejected_takeover_is_shadow_only_and_preserves_legacy_execution() -> None:
    context = DirectorV2Context(
        1.0, "world-1", "self-1", "capabilities-1",
        candidates=(DirectorV2Candidate(
            "chat", "m1", "READ_CHAT", "READ_CHAT", 10.0, ("chat:m1",),
        ),),
    )
    proposal = DirectorV2Proposal(
        "p-shadow", 1.0, "READ_CHAT", "READ_CHAT", "m1",
        ("selected", "validated"), ("chat:m1",), 10.0,
    )

    class StaticShadow:
        @staticmethod
        def propose_current() -> DirectorV2Proposal:
            return proposal

        @staticmethod
        def trajectory_context(_proposal_id: str) -> DirectorV2Context:
            return context

    class RejectingSelector:
        @staticmethod
        def evaluate(**_kwargs: object) -> DirectorV2TakeoverSelection:
            return DirectorV2TakeoverSelection(
                False, "READ_CHAT", "feature_disabled", "READ_CHAT", "p-shadow",
            )

    loop, _director, pool, _pulse, runner, clock = _make()
    recorder = _recorder()
    loop._trajectory_records = recorder
    loop.configure_director_v2_takeover(StaticShadow(), RejectingSelector())
    pool.add("m1", "hello", now=0.0, kind="mention")
    clock["t"] = 1.0

    assert await loop.tick_once() is DirectorAction.READ_CHAT
    assert runner.read_calls == ["hello"]
    current = recorder.snapshot()["current"]
    assert current["owner"] == "legacy"
    assert current["lifecycle"] == "shadow_only"
    assert current["action_request"] is None


def test_dashboard_exposes_only_the_sanitized_read_only_projection() -> None:
    recorder = _recorder()
    context = DirectorV2Context(
        1.0, "world-1", "self-1", "capabilities-1",
        candidates=(DirectorV2Candidate(
            "chat", "m1", "READ_CHAT", "READ_CHAT", 10.0, ("chat:m1",),
        ),),
    )
    proposal = DirectorV2Proposal(
        "p-dashboard", 1.0, "READ_CHAT", "READ_CHAT", "m1",
        ("selected", "validated"), ("chat:m1",), 10.0,
    )
    recorder.begin(context, proposal)
    recorder.mark_selection("p-dashboard", owner="legacy")
    client = TestClient(DashboardServer().app)
    assert recorder.snapshot()["recent"][0]["trajectory_id"] == "p-dashboard"
    assert not any(
        getattr(route, "path", "") == "/api/trajectories/replay"
        and "POST" in getattr(route, "methods", set())
        for route in client.app.routes
    )
