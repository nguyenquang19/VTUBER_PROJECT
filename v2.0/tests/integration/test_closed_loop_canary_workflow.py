from __future__ import annotations

import pytest

from interfaces.director_v2 import DirectorV2Candidate, DirectorV2Context, DirectorV2Proposal
from services.evaluation.closed_loop_canary import ClosedLoopCanary, ClosedLoopCanaryConfig
from services.evaluation.release_gate import SourceState
from tests.integration.test_external_action_transaction import (
    NOW,
    FakeOBS,
    _build,
    _request,
)


@pytest.mark.asyncio
async def test_closed_loop_canary_uses_verified_obs_transaction_and_world_projection() -> None:
    transport = FakeOBS()
    action_loop, world, transactions, _ = await _build(transport)

    def context() -> DirectorV2Context:
        snapshot = world.snapshot()
        return DirectorV2Context(
            created_at=1.0,
            world_snapshot_id=snapshot.snapshot_id,
            self_snapshot_id="self-canary",
            capability_snapshot_id="capabilities-rechecked",
            candidates=(DirectorV2Candidate(
                source="capability", candidate_id="cap:SWITCH_SCENE",
                action_type="SWITCH_SCENE", capability_id="SWITCH_SCENE",
                evidence_refs=("capability:SWITCH_SCENE",),
            ),),
        )

    def propose(value: DirectorV2Context) -> DirectorV2Proposal:
        before_action = value.world_snapshot_id == "world-0"
        return DirectorV2Proposal(
            proposal_id=f"proposal:{value.world_snapshot_id}", created_at=1.0,
            action_type="SWITCH_SCENE" if before_action else "WAIT",
            capability_id="SWITCH_SCENE" if before_action else "WAIT",
            candidate_id="cap:SWITCH_SCENE" if before_action else "wait",
            reason_codes=("selected",),
        )

    service = ClosedLoopCanary(
        ClosedLoopCanaryConfig(1, ("SWITCH_SCENE",), 1.0, 4, 160),
        current_product_version="1.4.3", target_product_version="2.0.0",
        context_provider=context, proposal_provider=propose,
        action_executor=action_loop.execute,
        source_state_provider=lambda: SourceState("a" * 40, True),
        clock=lambda: NOW, enabled=True,
    )
    await service.start()
    record = await service.run(_request(key="phase15-canary", scene="Canary Scene"))

    assert record.passed is True
    assert record.pre_snapshot.world_snapshot_id == "world-0"
    assert record.post_snapshot.world_snapshot_id == "world-1"
    assert world.query("stream.current_scene").value == "Canary Scene"
    assert transactions.snapshot()["recent"][-1]["state"] == "committed"
    assert transport.set_calls == ["Canary Scene"]
    assert record.to_dict()["next_decision"]["action_type"] == "WAIT"
