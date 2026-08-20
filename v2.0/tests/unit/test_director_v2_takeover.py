"""Strict safety and ownership gates for Director V2 controlled takeover."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from interfaces.base import HealthState
from interfaces.director_v2 import (
    DirectorV2Proposal,
    DirectorV2TakeoverSelection,
)
from orchestrator.config_loader import ConfigLoader
from orchestrator.features import FeatureManager
from orchestrator.runtime_feature_bindings import attach_set_enabled_feature
from services.director.v2_takeover import (
    DirectorV2Takeover,
    DirectorV2TakeoverConfig,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
STAGES = ("WAIT", "READ_CHAT", "SELF_TALK", "FOLLOW_UP", "SPEECH_SCHEDULING")
STAGE_ACTIONS = {
    "WAIT": frozenset({"WAIT"}),
    "READ_CHAT": frozenset({"WAIT", "READ_CHAT", "ACK_DONATION"}),
    "SELF_TALK": frozenset({"WAIT", "READ_CHAT", "ACK_DONATION", "SELF_TALK"}),
    "FOLLOW_UP": frozenset({
        "WAIT", "READ_CHAT", "ACK_DONATION", "SELF_TALK", "FOLLOW_UP",
        "CONTINUE_THREAD", "ASK_FOLLOW_UP", "SHARE_GOAL_PROGRESS",
    }),
    "SPEECH_SCHEDULING": frozenset({
        "WAIT", "READ_CHAT", "ACK_DONATION", "SELF_TALK", "FOLLOW_UP",
        "CONTINUE_THREAD", "ASK_FOLLOW_UP", "SHARE_GOAL_PROGRESS",
    }),
}
ALIASES = {
    "ACK_DONATION": "READ_CHAT",
    "CONTINUE_THREAD": "FOLLOW_UP",
    "ASK_FOLLOW_UP": "FOLLOW_UP",
    "SHARE_GOAL_PROGRESS": "FOLLOW_UP",
}


def _config(stage: str = "READ_CHAT", **overrides: object) -> DirectorV2TakeoverConfig:
    values: dict[str, object] = {
        "ownership_mode": "agreement",
        "stage": stage,
        "max_recent_decisions": 2,
        "max_reason_chars": 120,
        "max_evidence_ids": 4,
        "max_proposal_age_seconds": 2.0,
        "stage_order": STAGES,
        "stage_actions": STAGE_ACTIONS,
        "action_aliases": ALIASES,
    }
    values.update(overrides)
    return DirectorV2TakeoverConfig(**values)  # type: ignore[arg-type]


def _proposal(
    action: str,
    candidate: str = "chat-1",
    reasons: tuple[str, ...] = ("selected", "validated"),
    *,
    created_at: float = 10.0,
    capability: str | None = None,
) -> DirectorV2Proposal:
    return DirectorV2Proposal(
        "p1", created_at, action, capability or action, candidate, reasons,
    )


def _selector(
    stage: str = "READ_CHAT", *, ownership_mode: str = "agreement",
    **kwargs: object,
) -> DirectorV2Takeover:
    return DirectorV2Takeover(
        _config(stage, ownership_mode=ownership_mode),
        enabled=True, clock=lambda: 10.5, **kwargs,
    )


def test_selection_contract_is_strict_immutable_and_ownership_consistent() -> None:
    accepted = DirectorV2TakeoverSelection(
        True, "READ_CHAT", "accepted", "READ_CHAT", "p1", "director_v2",
    )
    assert accepted.decision_owner == "director_v2"
    with pytest.raises(FrozenInstanceError):
        accepted.reason_code = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        DirectorV2TakeoverSelection(
            1, "READ_CHAT", "accepted", "READ_CHAT", "p1", "director_v2",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        DirectorV2TakeoverSelection(
            True, "READ_CHAT", "accepted", "READ_CHAT", "p1", "legacy",
        )
    with pytest.raises(ValueError):
        DirectorV2TakeoverSelection(
            False, "READ_CHAT", "blocked", "READ_CHAT", "p1", "director_v2",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_recent_decisions", True),
        ("max_reason_chars", "120"),
        ("max_evidence_ids", 2.0),
        ("max_proposal_age_seconds", float("nan")),
        ("max_proposal_age_seconds", 0.0),
        ("stage_order", list(STAGES)),
        ("ownership_mode", True),
        ("ownership_mode", "future"),
    ],
)
def test_config_rejects_coercion_nonfinite_and_mutable_inventory(
    field: str, value: object,
) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_config_is_deep_immutable_and_requires_monotonic_locked_inventory() -> None:
    config = _config()
    with pytest.raises(TypeError):
        config.stage_actions["WAIT"] = frozenset({"WAIT"})  # type: ignore[index]
    with pytest.raises(TypeError):
        config.action_aliases["ACK_DONATION"] = "WAIT"  # type: ignore[index]
    bad = dict(STAGE_ACTIONS)
    bad["SELF_TALK"] = frozenset({"WAIT", "SELF_TALK"})
    with pytest.raises(ValueError, match="monotonically"):
        _config(stage_actions=bad)
    with pytest.raises(ValueError, match="aliases"):
        _config(action_aliases={"ACK_DONATION": "READ_CHAT"})


def test_real_yaml_loads_v2_test_cutover_and_rollback_contract() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    config = DirectorV2TakeoverConfig.from_loader(loader)
    assert config.ownership_mode == "primary"
    assert config.stage == "SPEECH_SCHEDULING"
    assert config.max_proposal_age_seconds == 2.0
    assert config.action_aliases["ACK_DONATION"] == "READ_CHAT"
    assert config.stage_actions["SPEECH_SCHEDULING"] == frozenset(
        action for actions in STAGE_ACTIONS.values() for action in actions
    )
    assert loader.get("features", "features.director_v2_takeover.enabled") is True


def test_disabled_takeover_is_side_effect_free_legacy_fallback() -> None:
    selector = DirectorV2Takeover(
        _config(), enabled=False, clock=lambda: 10.5,
    )
    result = selector.evaluate(
        legacy_action="read_chat",
        proposal=_proposal("READ_CHAT"),
        evidence_ids=("chat-1",),
    )
    assert result.accepted is False
    assert result.decision_owner == "legacy"
    assert result.reason_code == "feature_disabled"
    assert selector.snapshot()["recent"] == []


def test_read_chat_and_donation_require_agreement_and_same_tick_evidence() -> None:
    selector = _selector()
    accepted = selector.evaluate(
        legacy_action="read_chat",
        proposal=_proposal("READ_CHAT"),
        evidence_ids=("chat-1",),
    )
    donation = selector.evaluate(
        legacy_action="ack_donation",
        proposal=_proposal("READ_CHAT", "donation-1"),
        evidence_ids=("donation-1",),
    )
    missing = selector.evaluate(
        legacy_action="read_chat",
        proposal=_proposal("READ_CHAT", "other"),
        evidence_ids=("chat-1",),
    )
    mismatch = selector.evaluate(
        legacy_action="read_chat",
        proposal=_proposal("SELF_TALK"),
        evidence_ids=("chat-1",),
    )
    assert accepted.accepted is True and accepted.decision_owner == "director_v2"
    assert donation.accepted is True
    assert missing.reason_code == "chat_evidence_missing"
    assert mismatch.reason_code == "action_mismatch"


def test_primary_mode_selects_proposal_without_compatibility_agreement() -> None:
    selector = _selector("SELF_TALK", ownership_mode="primary")
    selected = selector.evaluate(
        legacy_action=None,
        proposal=_proposal("SELF_TALK", "urge"),
    )
    divergent = selector.evaluate(
        legacy_action="read_chat",
        proposal=_proposal("SELF_TALK", "urge"),
    )
    assert selected.accepted is True
    assert selected.action_type == "SELF_TALK"
    assert divergent.accepted is True
    assert divergent.action_type == "SELF_TALK"
    assert selector.snapshot()["ownership_mode"] == "primary"


def test_agreement_mode_requires_compatibility_action() -> None:
    with pytest.raises(ValueError, match="requires legacy_action"):
        _selector().evaluate(
            legacy_action=None,
            proposal=_proposal("READ_CHAT"),
            evidence_ids=("chat-1",),
        )


def test_stage_rollout_blocks_later_actions_and_follow_up_alias_requires_evidence() -> None:
    blocked = _selector("READ_CHAT").evaluate(
        legacy_action="self_talk", proposal=_proposal("SELF_TALK", "urge"),
    )
    follow = _selector("FOLLOW_UP").evaluate(
        legacy_action="continue_thread",
        proposal=_proposal("FOLLOW_UP", "thread-1"),
        evidence_ids=("thread-1",),
    )
    assert blocked.reason_code == "stage_blocked"
    assert follow.accepted is True


def test_self_talk_transfers_only_at_self_talk_or_later_stage() -> None:
    blocked = _selector("READ_CHAT").evaluate(
        legacy_action="self_talk", proposal=_proposal("SELF_TALK", "urge"),
    )
    accepted = _selector("SELF_TALK").evaluate(
        legacy_action="self_talk", proposal=_proposal("SELF_TALK", "urge"),
    )
    assert blocked.reason_code == "stage_blocked"
    assert accepted.accepted is True


@pytest.mark.parametrize(
    ("created_at", "reason"),
    [(7.0, "proposal_stale"), (11.0, "proposal_from_future")],
)
def test_stale_or_future_proposal_falls_back(created_at: float, reason: str) -> None:
    result = _selector().evaluate(
        legacy_action="read_chat",
        proposal=_proposal("READ_CHAT", created_at=created_at),
        evidence_ids=("chat-1",),
    )
    assert result.accepted is False
    assert result.reason_code == reason


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (("capability_permission_denied",), "capability_rejected"),
        (("safety_hold",), "hard_hold"),
        (("source_context_failed",), "proposal_rejected"),
        (("candidate_duplicate",), "proposal_rejected"),
    ],
)
def test_fail_closed_proposal_reasons_never_transfer_ownership(
    reasons: tuple[str, ...], expected: str,
) -> None:
    result = _selector().evaluate(
        legacy_action="read_chat",
        proposal=_proposal("READ_CHAT", reasons=reasons),
        evidence_ids=("chat-1",),
    )
    assert result.accepted is False
    assert result.reason_code == expected
    assert result.decision_owner == "legacy"


def test_evidence_contract_rejects_list_duplicate_and_overflow() -> None:
    selector = _selector()
    values = [
        ["chat-1"],
        ("chat-1", "chat-1"),
        ("a", "b", "c", "d", "e"),
    ]
    for evidence in values:
        result = selector.evaluate(
            legacy_action="read_chat",
            proposal=_proposal("READ_CHAT"),
            evidence_ids=evidence,  # type: ignore[arg-type]
        )
        assert result.reason_code == "evidence_invalid"


def test_wait_requires_canonical_wait_identity() -> None:
    selector = _selector("WAIT")
    accepted = selector.evaluate(
        legacy_action="wait",
        proposal=_proposal("WAIT", "wait", capability="WAIT"),
    )
    rejected = selector.evaluate(
        legacy_action="wait",
        proposal=_proposal("WAIT", "other", capability="WAIT"),
    )
    assert accepted.accepted is True
    assert rejected.reason_code == "wait_evidence_invalid"


def test_metrics_failure_isolated_and_records_remain_bounded() -> None:
    class BrokenMetrics:
        @staticmethod
        def record_director_v2_takeover(*_args: object) -> None:
            raise RuntimeError("metrics unavailable")

    selector = _selector(metrics=BrokenMetrics())
    for candidate in ("chat-1", "chat-2", "chat-3"):
        result = selector.evaluate(
            legacy_action="read_chat",
            proposal=_proposal("READ_CHAT", candidate),
            evidence_ids=(candidate,),
        )
        assert result.accepted is True
    snapshot = selector.snapshot()
    assert len(snapshot["recent"]) == 2  # type: ignore[arg-type]
    assert selector.get_metrics()["director_v2_takeover_outcomes"]["READ_CHAT:accepted"] == 3


def test_replay_same_inputs_produces_identical_ownership_selection() -> None:
    proposal = _proposal("READ_CHAT")
    first = _selector().evaluate(
        legacy_action="read_chat", proposal=proposal, evidence_ids=("chat-1",),
    )
    second = _selector().evaluate(
        legacy_action="read_chat", proposal=proposal, evidence_ids=("chat-1",),
    )
    assert first == second
    assert first.accepted is True
    assert first.proposal_id == "p1"


@pytest.mark.asyncio
async def test_lifecycle_health_and_strict_toggle_are_idempotent() -> None:
    selector = DirectorV2Takeover(
        _config(), enabled=False, clock=lambda: 10.5,
    )
    assert (await selector.health_check()).state is HealthState.STOPPED
    await selector.start()
    await selector.start()
    assert (await selector.health_check()).state is HealthState.DEGRADED
    selector.set_enabled(True)
    assert (await selector.health_check()).state is HealthState.HEALTHY
    with pytest.raises(ValueError):
        selector.set_enabled(1)  # type: ignore[arg-type]
    await selector.stop()
    await selector.stop()
    assert (await selector.health_check()).state is HealthState.STOPPED


@pytest.mark.asyncio
async def test_feature_manager_owns_runtime_enable_and_rollback_switch() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    manager = FeatureManager.from_config(loader)
    selector = DirectorV2Takeover.from_loader(
        loader, enabled=True, clock=lambda: 10.5,
    )
    attach_set_enabled_feature(manager, "director_v2_takeover", selector)

    disabled = await manager.disable("director_v2_takeover", user="test")
    assert disabled.ok is True
    assert selector.enabled is False
    enabled = await manager.enable("director_v2_takeover", user="test")
    assert enabled.ok is True
    assert selector.enabled is True
