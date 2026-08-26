"""Composition-only helper for the canonical Cognition stack."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from interfaces.cognition import CognitionConfig, CognitiveHardState
from interfaces.turn_kernel import KernelConfig, TurnRolloutMode
from orchestrator.runtime_feature_bindings import attach_boolean_feature
from services.cognition.brain import CognitiveBrain
from services.cognition.context_builder import CognitiveContextBuilder
from services.cognition.model_adapter import CognitiveModelAdapter
from services.cognition.scheduler import CognitiveOpportunityScheduler


@dataclass(frozen=True)
class CognitiveRuntimeStack:
    scheduler: CognitiveOpportunityScheduler
    config: CognitionConfig
    hard_state_provider: Callable[[Any], CognitiveHardState]


def build_cognitive_runtime_stack(
    *,
    loader: Any,
    feature_manager: Any,
    llm: Any,
    context_builder: CognitiveContextBuilder,
    self_model: Any,
    transactions: Any,
    control_plane: Any,
    emergency_controller: Any,
    metrics: Any,
    kernel_config: KernelConfig,
) -> CognitiveRuntimeStack:
    """Build dormant services and attach lifecycle to the disabled feature."""
    config = CognitionConfig.from_mapping(loader.section("cognition"))
    model_adapter = CognitiveModelAdapter.from_loader(
        loader, llm=llm, config=config,
    )
    brain = CognitiveBrain(
        config=config, model_adapter=model_adapter,
    )
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

    async def set_scheduler_enabled(enabled: bool) -> None:
        if enabled and kernel_config.rollout_mode is TurnRolloutMode.OFF:
            await scheduler.stop()
            raise RuntimeError("cognitive Brain cannot start while kernel rollout is OFF")
        if enabled:
            await scheduler.start()
        else:
            await scheduler.stop()

    attach_boolean_feature(
        feature_manager,
        "cognitive_brain_shadow",
        set_enabled=set_scheduler_enabled,
        is_enabled=lambda: (
            scheduler.snapshot().running and scheduler.snapshot().healthy
        ),
    )
    return CognitiveRuntimeStack(
        scheduler=scheduler,
        config=config,
        hard_state_provider=hard_state,
    )
