from __future__ import annotations

from datetime import datetime, timezone

from interfaces.operations import OperationsCommand
from services.operations.surface import OperationsSurface, OperationsSurfaceConfig


class _Metrics:
    def prometheus_text(self) -> bytes:
        return b"mai_test_total 1.0\n"


def _surface() -> OperationsSurface:
    return OperationsSurface(OperationsSurfaceConfig(
        max_snapshot_sections=4,
        max_commands=4,
        max_label_chars=40,
        max_payload_bytes=128,
    ), metrics=_Metrics())


def _command(name: str, payload: dict[str, object] | None = None) -> OperationsCommand:
    return OperationsCommand(
        command_id="command-1",
        name=name,
        issued_at=datetime.now(timezone.utc),
        payload=payload or {},
    )


async def test_snapshot_is_failure_isolated_and_metrics_are_canonical() -> None:
    surface = _surface()
    surface.register_snapshot_provider("healthy", lambda: {"value": 1})

    def broken() -> None:
        raise RuntimeError("private failure")

    surface.register_snapshot_provider("broken", broken)
    await surface.start()
    value = (await surface.snapshot()).to_dict()
    assert value["healthy"] == {"value": 1}
    assert value["operations_degraded"] == {
        "failed_sections": {"broken": "RuntimeError"},
    }
    assert surface.prometheus_text() == b"mai_test_total 1.0\n"
    assert surface.get_metrics()["operations_surface_snapshot_failed_broken_total"] == 1


async def test_commands_are_allowlisted_bounded_and_require_running_surface() -> None:
    surface = _surface()
    surface.register_command("agent.pause", lambda payload: {
        "ok": True, "reason": payload.get("reason"),
    })
    stopped = await surface.execute(_command("agent.pause"))
    assert stopped.status_code == 503
    await surface.start()
    missing = await surface.execute(_command("soft.choose"))
    assert missing.status_code == 404
    accepted = await surface.execute(_command("agent.pause", {"reason": "owner"}))
    assert accepted.accepted is True
    assert accepted.payload == {"ok": True, "reason": "owner"}
    oversized = await surface.execute(_command("agent.pause", {"reason": "x" * 200}))
    assert oversized.status_code == 400


async def test_handler_failure_does_not_escape_or_gain_authority() -> None:
    surface = _surface()

    def invalid(_payload: object) -> None:
        raise ValueError("invalid operator request")

    surface.register_command("goal.pin", invalid)
    await surface.start()
    result = await surface.execute(_command("goal.pin"))
    assert result.accepted is False
    assert result.status_code == 400
    assert result.payload["reason"] == "invalid operator request"
