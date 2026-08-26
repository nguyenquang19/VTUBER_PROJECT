"""Regression coverage for runtime composition module boundaries."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.runtime_feature_bindings import attach_set_enabled_feature
from orchestrator.runtime_tts import TTSRuntimeStack, build_tts_runtime_stack
from orchestrator.stream_runtime import (
    _TTSRuntimeStack,
    _build_tts_runtime_stack,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FeatureManagerStub:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def attach_handlers(self, feature_id: str, **handlers: object) -> None:
        self.handlers = {"feature_id": feature_id, **handlers}


class ToggleTarget:
    def __init__(self) -> None:
        self.enabled = False

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled


def test_stream_runtime_keeps_legacy_tts_imports() -> None:
    assert _TTSRuntimeStack is TTSRuntimeStack
    assert _build_tts_runtime_stack is build_tts_runtime_stack


def test_composition_root_delegates_extracted_responsibilities() -> None:
    source = (REPO_ROOT / "orchestrator" / "stream_runtime.py").read_text(
        encoding="utf-8",
    )
    assert "class _TTSRuntimeStack" not in source
    assert "async def _build_tts_runtime_stack" not in source
    assert "RuntimeControlPlane(" not in source
    assert "DashboardServer(" not in source
    assert "EmergencyController(" not in source
    assert "ShutdownCoordinator.from_loader(" not in source
    assert "build_control_plane(" in source
    assert "start_dashboard(" in source
    assert "build_emergency_controller(" in source
    assert "configure_shutdown_coordinator(" in source
    assert "build_operations_surface(" in source


def test_live_dashboard_composition_receives_only_operations_surface() -> None:
    source = (REPO_ROOT / "orchestrator" / "runtime_operations.py").read_text(
        encoding="utf-8",
    )
    dashboard_call = source.split("server = DashboardServer(", 1)[1].split(")\n", 1)[0]
    assert "operations_surface=operations_surface" in dashboard_call
    assert "metrics=metrics" in dashboard_call
    for forbidden in (
        "goal_manager=", "relationship_manager=", "decision_records=",
        "closed_loop_canary=", "human_like_calibration=", "control_plane=",
        "runner=",
    ):
        assert forbidden not in dashboard_call


@pytest.mark.asyncio
async def test_set_enabled_feature_binding_is_symmetric() -> None:
    manager = FeatureManagerStub()
    target = ToggleTarget()
    attach_set_enabled_feature(manager, "sample", target)

    assert manager.handlers["feature_id"] == "sample"
    await manager.handlers["enable"]()  # type: ignore[operator]
    assert target.enabled is True
    assert await manager.handlers["health"]() is True  # type: ignore[operator]
    await manager.handlers["disable"]()  # type: ignore[operator]
    assert target.enabled is False
    assert await manager.handlers["health"]() is False  # type: ignore[operator]
