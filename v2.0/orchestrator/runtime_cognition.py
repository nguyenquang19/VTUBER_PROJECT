"""Composition-only helper for the MCB-3 proposal observer stack."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interfaces.cognition import CognitionConfig, CognitiveHardState
from orchestrator.cognitive_observer import CognitiveDirectorObserver
from orchestrator.runtime_feature_bindings import attach_boolean_feature
from services.cognition.brain_shadow import CognitiveBrain
from services.cognition.context_builder import CognitiveContextBuilder
from services.cognition.shadow_scheduler import CognitiveOpportunityScheduler


@dataclass(frozen=True)
class CognitiveRuntimeStack:
    scheduler: CognitiveOpportunityScheduler
    observer: CognitiveDirectorObserver


def build_cognitive_runtime_stack(
    *,
    loader: Any,
    feature_manager: Any,
    llm: Any,
    world_model: Any,
    self_model: Any,
    capability_registry: Any,
    agent_state: Any,
    goal_manager: Any,
    thread_manager: Any,
    memory_service: Any,
    transactions: Any,
    control_plane: Any,
    emergency_controller: Any,
    metrics: Any,
    session_id: str,
) -> CognitiveRuntimeStack:
    """Build dormant services and attach lifecycle to the disabled feature."""
    config = CognitionConfig.from_mapping(loader.section("cognition"))
    context_builder = CognitiveContextBuilder(
        config,
        world_model=world_model,
        self_model=self_model,
        capability_registry=capability_registry,
        agent_state=agent_state,
        goal_manager=goal_manager,
        thread_manager=thread_manager,
        memory_service=memory_service,
        metrics=metrics,
    )
    brain = CognitiveBrain.from_loader(loader, llm=llm, config=config)
    scheduler = CognitiveOpportunityScheduler(
        config=config, context_builder=context_builder, brain=brain, metrics=metrics,
    )

    def hard_state(value: Any) -> CognitiveHardState:
        failures: list[str] = []
        emergency = False
        operator_hold = False
        transaction_conflict = False
        critical_state = False
        try:
            if emergency_controller is None:
                failures.append("emergency")
            else:
                emergency = emergency_controller.snapshot().get("latched")
                if not isinstance(emergency, bool):
                    raise ValueError("invalid emergency latch")
        except Exception:
            emergency = True
            failures.append("emergency")
        try:
            if control_plane is None:
                failures.append("operator")
            else:
                operator_hold = control_plane.paused
                if not isinstance(operator_hold, bool):
                    raise ValueError("invalid operator hold")
        except Exception:
            operator_hold = True
            failures.append("operator")
        try:
            recent = transactions.snapshot().get("recent")
            if not isinstance(recent, list):
                raise ValueError("invalid transaction snapshot")
            transaction_conflict = any(
                isinstance(item, dict) and item.get("state") in {
                    "reserved", "generated", "delivering", "delivered",
                }
                for item in recent
            )
        except Exception:
            transaction_conflict = True
            failures.append("transaction")
        try:
            degraded = self_model.snapshot().degraded
            if not isinstance(degraded, bool):
                raise ValueError("invalid Self Model degraded flag")
            critical_state = degraded
        except Exception:
            critical_state = True
            failures.append("self")
        return CognitiveHardState(
            config=config,
            schema_version=config.schema_version,
            emergency=emergency,
            operator_hold=operator_hold,
            safety_hold=bool(value.safety_hold),
            permission_hold=False,
            transaction_conflict=transaction_conflict,
            critical_state=critical_state,
            source_failure_codes=tuple(dict.fromkeys(failures)),
        )

    observer = CognitiveDirectorObserver(
        config=config, scheduler=scheduler, session_id=session_id,
        hard_state_provider=hard_state,
    )
    attach_boolean_feature(
        feature_manager,
        "cognitive_brain_shadow",
        set_enabled=lambda enabled: scheduler.start() if enabled else scheduler.stop(),
        is_enabled=lambda: (
            scheduler.snapshot().running and scheduler.snapshot().healthy
        ),
    )
    return CognitiveRuntimeStack(scheduler=scheduler, observer=observer)
