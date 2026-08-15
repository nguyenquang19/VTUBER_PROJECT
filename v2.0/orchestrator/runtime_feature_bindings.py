"""Reusable feature-toggle bindings for runtime-owned components."""
from __future__ import annotations

import inspect
from typing import Any, Callable

ToggleFn = Callable[[bool], Any]
HealthFn = Callable[[], Any]


def attach_boolean_feature(
    manager: Any,
    feature_id: str,
    *,
    set_enabled: ToggleFn,
    is_enabled: HealthFn,
) -> None:
    """Attach symmetric enable/disable handlers with an async health adapter."""

    async def _set(value: bool) -> None:
        result = set_enabled(value)
        if inspect.isawaitable(result):
            await result

    async def _enable() -> None:
        await _set(True)

    async def _disable() -> None:
        await _set(False)

    async def _health() -> bool:
        result = is_enabled()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    manager.attach_handlers(
        feature_id,
        enable=_enable,
        disable=_disable,
        health=_health,
    )


def attach_set_enabled_feature(manager: Any, feature_id: str, target: Any) -> None:
    """Attach a component exposing ``set_enabled`` and ``enabled``."""
    attach_boolean_feature(
        manager,
        feature_id,
        set_enabled=target.set_enabled,
        is_enabled=lambda: target.enabled,
    )
