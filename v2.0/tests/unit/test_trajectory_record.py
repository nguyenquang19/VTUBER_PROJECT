from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from interfaces.action_execution import VerificationResult
from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from interfaces.director_v2 import DirectorV2Candidate, DirectorV2Context, DirectorV2Proposal
from interfaces.trajectory import TrajectorySnapshotRefs
from orchestrator.metrics_collector import MetricsCollector
from services.director.trajectory import TrajectoryConfig, TrajectoryRecorder
from services.director.v2_shadow import DirectorV2Shadow
from services.operations.turn_journal import TurnJournal, TurnJournalConfig
from tests.unit.test_director_v2_shadow import _Availability, _Registry, _config as _shadow_config


def _config(*, max_recent: int = 4) -> TrajectoryConfig:
    return TrajectoryConfig(
        schema_version=1,
        max_recent=max_recent,
        dashboard_recent=min(3, max_recent),
        max_candidates=4,
        max_evidence_refs=4,
        max_reason_codes=4,
        max_label_chars=120,
    )


def _refs(suffix: str = "next") -> TrajectorySnapshotRefs:
    return TrajectorySnapshotRefs(
        f"world-{suffix}", f"self-{suffix}", f"capabilities-{suffix}",
    )


def _context(created_at: float = 1.0) -> DirectorV2Context:
    return DirectorV2Context(
        created_at=created_at,
        world_snapshot_id="world-1",
        self_snapshot_id="self-1",
        capability_snapshot_id="capabilities-1",
        candidates=(DirectorV2Candidate(
            source="chat",
            candidate_id="chat-1",
            action_type="READ_CHAT",
            capability_id="READ_CHAT",
            score=10.0,
            evidence_refs=("chat:1",),
        ),),
    )


def _proposal(suffix: str = "1") -> DirectorV2Proposal:
    return DirectorV2Proposal(
        proposal_id=f"d2-{suffix}",
        created_at=1.0,
        action_type="READ_CHAT",
        capability_id="READ_CHAT",
        candidate_id="chat-1",
        reason_codes=("selected", "source_chat", "validated"),
        evidence_refs=("chat:1",),
        score=10.0,
    )


def _request(action_id: str = "action-1") -> ActionRequest:
    return ActionRequest(
        schema_version=1,
        action_id=action_id,
        capability_id="READ_CHAT",
        action_type="READ_CHAT",
        target="private-viewer",
        arguments={"text": "raw private chat secret"},
        intention_id="intent-1",
        evidence_refs=("chat:1",),
        idempotency_key="idem-1",
        priority=0.0,
        requested_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        transaction_policy="delivery_aware",
    )


def _result(action_id: str = "action-1") -> ActionResult:
    when = datetime(2026, 8, 20, tzinfo=timezone.utc)
    return ActionResult(
        schema_version=1,
        action_id=action_id,
        status=ActionStatus.SUCCESS,
        started_at=when,
        completed_at=when,
        verified=True,
        verification_source="director_delivery",
        result_data={"delivery_mode": "raw secret mode"},
    )


def test_complete_trajectory_is_versioned_bounded_and_value_redacted() -> None:
    metrics = MetricsCollector()
    recorder = TrajectoryRecorder(
        _config(), snapshot_provider=_refs, clock=lambda: 2.0,
        metrics=metrics, enabled=True,
    )
    assert recorder.begin(_context(), _proposal()) == "d2-1"
    recorder.mark_selection("d2-1", owner="director_v2")
    recorder.record_action("d2-1", _request())
    recorder.record_result(
        "d2-1",
        _result(),
        VerificationResult(True, "director_delivery", "committed", ("delivery:1",)),
    )
    snapshot = recorder.snapshot()
    current = snapshot["current"]
    assert current["schema_version"] == 1
    assert current["initial_snapshot"]["world_snapshot_id"] == "world-1"
    assert current["next_snapshot"]["world_snapshot_id"] == "world-next"
    assert current["lifecycle"] == "completed"
    assert current["action_request"]["argument_keys"] == ["text"]
    assert current["action_result"]["result_keys"] == ["delivery_mode"]
    rendered = json.dumps(snapshot)
    for secret in ("raw private chat secret", "private-viewer", "raw secret mode"):
        assert secret not in rendered
    assert current["chain_of_thought_included"] is False
    assert metrics.trajectory_snapshot()["completed"] == 1


def test_live_trajectory_projection_uses_canonical_journal_without_second_store() -> None:
    journal = TurnJournal(TurnJournalConfig(
        max_lineages=8,
        max_events_per_lineage=16,
        max_reason_codes=8,
        max_evidence_refs=8,
        max_label_chars=160,
        max_projection_bytes=65536,
    ))
    recorder = TrajectoryRecorder(
        _config(),
        snapshot_provider=_refs,
        clock=lambda: 2.0,
        turn_journal=journal,
        enabled=True,
    )
    recorder.begin(_context(), _proposal())
    recorder.mark_selection("d2-1", owner="director_v2")
    recorder.record_action("d2-1", _request())
    recorder.record_result(
        "d2-1",
        _result(),
        VerificationResult(True, "director_delivery", "committed", ("delivery:1",)),
    )
    assert recorder._items == {}
    assert journal.projection("d2-1", "trajectory_record") is not None
    assert recorder.snapshot()["current"]["lifecycle"] == "completed"
    assert recorder.replay("d2-1", lambda _context: _proposal()).matched is True


def test_wait_and_shadow_paths_never_invent_action_records() -> None:
    recorder = TrajectoryRecorder(
        _config(), snapshot_provider=_refs, clock=lambda: 2.0, enabled=True,
    )
    wait = DirectorV2Proposal(
        proposal_id="d2-wait",
        created_at=1.0,
        action_type="WAIT",
        capability_id="WAIT",
        candidate_id="wait",
        reason_codes=("operator_hold",),
    )
    recorder.begin(_context(), wait)
    recorder.mark_selection("d2-wait", owner="director_v2")
    recorder.record_no_action("d2-wait", reason_code="operator_hold")
    current = recorder.snapshot()["current"]
    assert current["lifecycle"] == "no_action"
    assert current["action_request"] is None
    assert current["action_result"] is None

    shadow = DirectorV2Proposal(**{**_proposal("shadow").__dict__, "created_at": 3.0})
    recorder.begin(_context(3.0), shadow)
    recorder.mark_selection("d2-shadow", owner="legacy")
    current = recorder.snapshot()["current"]
    assert current["lifecycle"] == "shadow_only"
    assert current["owner"] == "legacy"


def test_replay_compares_structured_decision_without_execution() -> None:
    recorder = TrajectoryRecorder(
        _config(), snapshot_provider=_refs, clock=lambda: 2.0, enabled=True,
    )
    expected = _proposal()
    recorder.begin(_context(), expected)
    recorder.mark_selection("d2-1", owner="legacy")
    calls = 0

    def same(context: DirectorV2Context) -> DirectorV2Proposal:
        nonlocal calls
        calls += 1
        assert context == _context()
        return expected

    matched = recorder.replay("d2-1", same)
    assert matched.matched is True and calls == 1
    changed = DirectorV2Proposal(
        **{**expected.__dict__, "action_type": "WAIT", "capability_id": "WAIT"},
    )
    mismatch = recorder.replay("d2-1", lambda _context: changed)
    assert mismatch.matched is False
    assert mismatch.mismatches == ("action_type", "capability_id")


def test_replay_with_real_director_v2_policy_is_deterministic() -> None:
    context = _context()
    registry = _Registry({"READ_CHAT": _Availability(True), "WAIT": _Availability(True)})
    shadow = DirectorV2Shadow(
        _shadow_config(),
        capability_registry=registry,
        context_provider=lambda: context,
        enabled=True,
    )
    proposal = shadow.propose(context)
    recorder = TrajectoryRecorder(
        _config(), snapshot_provider=_refs, clock=lambda: 2.0, enabled=True,
    )
    recorder.begin(context, proposal)
    recorder.mark_selection(proposal.proposal_id, owner="legacy")
    replay_shadow = DirectorV2Shadow(
        _shadow_config(),
        capability_registry=registry,
        context_provider=lambda: context,
        enabled=True,
    )
    assert recorder.replay(proposal.proposal_id, replay_shadow.propose).matched is True


@pytest.mark.asyncio
async def test_disable_and_stop_finalize_open_records_as_incomplete() -> None:
    recorder = TrajectoryRecorder(
        _config(), snapshot_provider=_refs, clock=lambda: 2.0, enabled=True,
    )
    await recorder.start()
    recorder.begin(_context(), _proposal())
    recorder.mark_selection("d2-1", owner="director_v2")
    recorder.set_enabled(False)
    assert recorder.snapshot()["current"]["lifecycle"] == "incomplete"
    assert recorder.begin(_context(3.0), _proposal("off")) is None
    await recorder.stop()


def test_retention_snapshot_copy_and_invalid_lifecycle_are_strict() -> None:
    recorder = TrajectoryRecorder(
        _config(max_recent=2), snapshot_provider=_refs, clock=lambda: 2.0, enabled=True,
    )
    for index in range(3):
        proposal = _proposal(str(index))
        context = _context(float(index + 1))
        proposal = DirectorV2Proposal(**{**proposal.__dict__, "created_at": context.created_at})
        recorder.begin(context, proposal)
        recorder.mark_selection(proposal.proposal_id, owner="legacy")
    first = recorder.snapshot()
    assert len(first["recent"]) == 2
    first["recent"].clear()
    assert len(recorder.snapshot()["recent"]) == 2
    with pytest.raises(ValueError, match="terminal"):
        recorder.mark_selection("d2-2", owner="legacy")


def test_strict_config_rejects_extra_keys_bool_and_invalid_dashboard_bound() -> None:
    raw = {
        "schema_version": 1,
        "max_recent": 4,
        "dashboard_recent": 2,
        "max_candidates": 4,
        "max_evidence_refs": 4,
        "max_reason_codes": 4,
        "max_label_chars": 120,
    }

    class Loader:
        def __init__(self, value: dict) -> None:
            self.value = value

        def get(self, *_args, **_kwargs):
            return self.value

    assert TrajectoryConfig.from_loader(Loader(raw)).max_recent == 4
    with pytest.raises(ValueError, match="keys"):
        TrajectoryConfig.from_loader(Loader({**raw, "extra": 1}))
    with pytest.raises(ValueError, match="positive integer"):
        TrajectoryConfig.from_loader(Loader({**raw, "max_recent": True}))
    with pytest.raises(ValueError, match="cannot exceed"):
        TrajectoryConfig.from_loader(Loader({**raw, "dashboard_recent": 5}))
