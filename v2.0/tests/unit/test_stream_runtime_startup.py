"""Failure-path contracts for transactional StreamRuntime startup."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import orchestrator.stream_runtime as stream_runtime_module
from orchestrator.stream_runtime import (
    StreamRuntime,
    StreamRuntimeConfig,
    _StartupRollback,
    _start_owned_resource,
    build_stream_runtime,
    run_stream_runtime,
)


class _Service:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.name = name
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.events.append(f"start:{self.name}")
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.stop_calls += 1
        self.events.append(f"stop:{self.name}")
        if self.stop_error is not None:
            raise self.stop_error


def _runtime(
    *,
    router: Any,
    llm: Any,
    director: Any = None,
    capability: Any = None,
    embodiment: Any = None,
    turn_journal: Any = None,
    operations_surface: Any = None,
    health_supervisor: Any = None,
    shutdown_coordinator: Any = None,
) -> StreamRuntime:
    return StreamRuntime(
        loader=object(),
        llm_svc=llm,
        runner=object(),
        emotion=object(),
        chat_router=router,
        autonomy=None,
        director_loop=director,
        capability_registry=capability,
        embodiment_policy=embodiment,
        turn_journal=turn_journal,
        operations_surface=operations_surface,
        health_supervisor=health_supervisor,
        shutdown_coordinator=shutdown_coordinator,
        metrics=object(),
        cfg=StreamRuntimeConfig(enable_autonomy=False),
    )


async def test_runtime_start_failure_cleans_started_components_and_resets_state() -> None:
    events: list[str] = []
    capability = _Service("capability", events)
    router = _Service("router", events)
    director = _Service("director", events, start_error=RuntimeError("director boom"))
    llm = _Service("llm", events)
    embodiment = _Service("embodiment", events)
    runtime = _runtime(
        router=router,
        llm=llm,
        director=director,
        capability=capability,
        embodiment=embodiment,
    )

    with pytest.raises(RuntimeError, match="director boom"):
        await runtime.start()

    assert runtime._running is False
    assert runtime._stop_event.is_set()
    assert router.stop_calls == 1
    assert director.stop_calls == 1
    assert capability.stop_calls == 1
    assert embodiment.stop_calls == 1
    assert llm.stop_calls == 1


async def test_runtime_owns_turn_journal_and_operations_surface_lifecycle() -> None:
    events: list[str] = []
    router = _Service("router", events)
    llm = _Service("llm", events)
    journal = _Service("turn_journal", events)
    surface = _Service("operations_surface", events)
    runtime = _runtime(
        router=router,
        llm=llm,
        turn_journal=journal,
        operations_surface=surface,
    )
    await runtime.start()
    await runtime.stop()
    assert journal.start_calls == 1
    assert journal.stop_calls == 1
    assert surface.start_calls == 1
    assert surface.stop_calls == 1


class _FailingRecovery(_Service):
    def pause_recovery(self, _reason: str) -> None:
        raise RuntimeError("cleanup boom")


class _Coordinator(_Service):
    def __init__(self, name: str, events: list[str], *, start_error: BaseException) -> None:
        super().__init__(name, events, start_error=start_error)
        self.shutdown_calls = 0

    async def shutdown(self) -> dict[str, Any]:
        self.shutdown_calls += 1
        return {}


async def test_runtime_start_preserves_root_error_when_cleanup_step_fails() -> None:
    events: list[str] = []
    router = _Service("router", events)
    llm = _Service("llm", events)
    health = _FailingRecovery("health", events)
    coordinator = _Coordinator(
        "coordinator",
        events,
        start_error=ValueError("startup root"),
    )
    runtime = _runtime(
        router=router,
        llm=llm,
        health_supervisor=health,
        shutdown_coordinator=coordinator,
    )

    with pytest.raises(ValueError, match="startup root"):
        await runtime.start()

    assert runtime._shutdown_coordinator_started is False
    assert router.stop_calls == 1
    assert llm.stop_calls == 1

    # Launcher cleanup must not invoke a coordinator that never finished start;
    # the startup path already performed the one allowed direct cleanup attempt.
    await runtime.stop()
    assert coordinator.stop_calls == 0
    assert coordinator.shutdown_calls == 0
    assert router.stop_calls == 1
    assert llm.stop_calls == 1

    await runtime.stop()
    assert coordinator.shutdown_calls == 0
    assert router.stop_calls == 1
    assert llm.stop_calls == 1


async def test_runtime_start_cancellation_still_cleans_owned_components() -> None:
    events: list[str] = []
    capability = _Service("capability", events)
    router = _Service("router", events, start_error=asyncio.CancelledError())
    llm = _Service("llm", events)
    runtime = _runtime(
        router=router,
        llm=llm,
        capability=capability,
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime.start()

    assert runtime._running is False
    assert capability.stop_calls == 1
    assert router.stop_calls == 1
    assert llm.stop_calls == 1


async def test_build_rollback_is_reverse_order_and_preserves_root_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    first = _Service("first", events)
    second = _Service("second", events, stop_error=RuntimeError("cleanup failed"))

    async def fail_composition(
        *, loader: Any, sources: list[Any], cfg: Any, rollback: _StartupRollback,
    ) -> StreamRuntime:
        await _start_owned_resource(rollback, "first", first)
        await _start_owned_resource(rollback, "second", second)
        raise LookupError("composition root")

    monkeypatch.setattr(stream_runtime_module, "_compose_stream_runtime", fail_composition)

    with pytest.raises(LookupError, match="composition root"):
        await build_stream_runtime(
            loader=object(),
            sources=[],
            cfg=StreamRuntimeConfig(),
        )

    assert events == [
        "start:first",
        "start:second",
        "stop:second",
        "stop:first",
    ]


async def test_partially_failed_owned_resource_is_cleaned_before_reraise() -> None:
    events: list[str] = []
    resource = _Service(
        "partial",
        events,
        start_error=RuntimeError("partial start"),
    )
    rollback = _StartupRollback()

    with pytest.raises(RuntimeError, match="partial start"):
        await _start_owned_resource(rollback, "partial", resource)

    assert resource.stop_calls == 1
    await rollback.rollback()
    assert resource.stop_calls == 1


async def test_launcher_cleanup_does_not_mask_start_failure() -> None:
    events: list[str] = []
    runtime = _Service(
        "runtime",
        events,
        start_error=RuntimeError("start root"),
        stop_error=ValueError("stop failure"),
    )

    with pytest.raises(RuntimeError, match="start root"):
        await run_stream_runtime(runtime)  # type: ignore[arg-type]

    assert runtime.stop_calls == 1
