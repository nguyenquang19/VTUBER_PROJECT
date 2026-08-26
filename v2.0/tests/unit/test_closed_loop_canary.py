from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from interfaces.director_v2 import DirectorV2Candidate, DirectorV2Context, DirectorV2Proposal
from services.evaluation.closed_loop_canary import (
    ClosedLoopCanary,
    ClosedLoopCanaryConfig,
)
from services.evaluation.release_gate import SourceState


NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
REVISION = "a" * 40


def _config() -> ClosedLoopCanaryConfig:
    return ClosedLoopCanaryConfig(1, ("SWITCH_SCENE",), 1.0, 2, 160)


def _context(world: str, capability: str) -> DirectorV2Context:
    return DirectorV2Context(
        created_at=1.0,
        world_snapshot_id=world,
        self_snapshot_id="self-1",
        capability_snapshot_id=capability,
        candidates=(DirectorV2Candidate(
            source="capability", candidate_id="cap:SWITCH_SCENE",
            action_type="SWITCH_SCENE", capability_id="SWITCH_SCENE",
            evidence_refs=("capability:SWITCH_SCENE",),
        ),),
    )


def _proposal(context: DirectorV2Context) -> DirectorV2Proposal:
    changed = context.world_snapshot_id == "world-after"
    return DirectorV2Proposal(
        proposal_id="proposal-2" if changed else "proposal-1",
        created_at=1.0,
        action_type="WAIT" if changed else "SWITCH_SCENE",
        capability_id="WAIT" if changed else "SWITCH_SCENE",
        candidate_id="wait" if changed else "cap:SWITCH_SCENE",
        reason_codes=("selected",),
    )


def _request() -> ActionRequest:
    return ActionRequest(
        schema_version=1, action_id="action-1", capability_id="SWITCH_SCENE",
        action_type="SWITCH_SCENE", target="Canary Scene",
        arguments={"scene_name": "Canary Scene", "secret_token": "never-project"},
        intention_id=None, evidence_refs=("operator:canary",),
        idempotency_key="canary-action-1", priority=1.0, requested_at=NOW,
        transaction_policy="verified",
    )


def _result(*, projected: bool = True) -> ActionResult:
    return ActionResult(
        schema_version=1, action_id="action-1", status=ActionStatus.SUCCESS,
        started_at=NOW, completed_at=NOW, verified=True,
        verification_source="obs_current_scene",
        result_data={
            "world_projected": projected, "rollback_status": "not_required",
        },
        error_code=None if projected else "world_projection_rejected",
    )


def _service(
    *, contexts: list[DirectorV2Context], enabled: bool = True,
    executor=None, clean: bool = True,
) -> ClosedLoopCanary:
    queue = iter(contexts)

    async def execute(_request: ActionRequest) -> ActionResult:
        return _result()

    return ClosedLoopCanary(
        _config(), current_product_version="1.4.3", target_product_version="2.0.0",
        context_provider=lambda: next(queue), proposal_provider=_proposal,
        action_executor=executor or execute,
        source_state_provider=lambda: SourceState(REVISION, clean),
        clock=lambda: NOW, enabled=enabled,
    )


@pytest.mark.asyncio
async def test_operator_canary_closes_proposal_action_world_and_next_decision() -> None:
    service = _service(contexts=[_context("world-before", "cap-1"), _context("world-after", "cap-2")])
    await service.start()
    record = await service.run(_request())
    assert record.passed is True
    assert record.transaction_committed is True
    assert record.world_projected is True
    assert record.next_proposal_id == "proposal-2"
    snapshot = service.snapshot()
    assert snapshot["counts"] == {"passed": 1}
    rendered = str(snapshot)
    assert "Canary Scene" not in rendered
    assert "never-project" not in rendered


@pytest.mark.asyncio
async def test_canary_records_failed_projection_without_claiming_pass() -> None:
    async def execute(_request: ActionRequest) -> ActionResult:
        return _result(projected=False)

    service = _service(
        contexts=[_context("world-before", "cap-1"), _context("world-before", "cap-2")],
        executor=execute,
    )
    await service.start()
    record = await service.run(_request())
    assert record.passed is False
    assert record.outcome == "failed"
    assert record.reason_code == "world_projected"


@pytest.mark.asyncio
async def test_canary_requires_running_enabled_clean_source() -> None:
    stopped = _service(contexts=[])
    with pytest.raises(RuntimeError, match="stopped"):
        await stopped.run(_request())

    disabled = _service(contexts=[], enabled=False)
    await disabled.start()
    with pytest.raises(RuntimeError, match="disabled"):
        await disabled.run(_request())

    dirty = _service(contexts=[], clean=False)
    await dirty.start()
    with pytest.raises(RuntimeError, match="clean Git"):
        await dirty.run(_request())


@pytest.mark.asyncio
async def test_canary_refuses_request_not_owned_by_current_proposal() -> None:
    service = _service(contexts=[_context("world-after", "cap-1")])
    await service.start()
    with pytest.raises(RuntimeError, match="does not own"):
        await service.run(_request())
    assert service.snapshot()["counts"] == {"proposal_mismatch": 1}


def test_closed_loop_config_rejects_unknown_or_duplicate_values() -> None:
    class Loader:
        def get(self, _name, _key, _default=None):
            return {
                "schema_version": 1, "allowed_actions": ["SWITCH_SCENE", "SWITCH_SCENE"],
                "execution_timeout_s": 1.0, "max_recent": 2, "max_label_chars": 10,
            }

    with pytest.raises(ValueError, match="unique"):
        ClosedLoopCanaryConfig.from_loader(Loader())


@pytest.mark.asyncio
async def test_canary_execution_timeout_fails_without_a_success_record() -> None:
    contexts = iter([_context("world-before", "cap-1")])

    async def blocked(_request: ActionRequest) -> ActionResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service = ClosedLoopCanary(
        ClosedLoopCanaryConfig(1, ("SWITCH_SCENE",), 0.01, 2, 160),
        current_product_version="1.4.3", target_product_version="2.0.0",
        context_provider=lambda: next(contexts), proposal_provider=_proposal,
        action_executor=blocked,
        source_state_provider=lambda: SourceState(REVISION, True),
        clock=lambda: NOW, enabled=True,
    )
    await service.start()
    with pytest.raises(RuntimeError, match="timed out"):
        await service.run(_request())
    assert service.snapshot()["counts"] == {"timeout": 1}
    assert service.snapshot()["recent"] == []
