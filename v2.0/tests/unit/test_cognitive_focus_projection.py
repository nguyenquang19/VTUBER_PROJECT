"""Focus shadow is derived only from fresh authoritative delivery state."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from services.agent.types import (
    AgentStateSnapshot,
    OpenThread,
)
from tests.unit.test_cognitive_context_builder import (
    NOW,
    _builder,
    _config,
    _request,
    _sources,
)


@pytest.mark.asyncio
async def test_focus_contains_only_authoritatively_delivered_claims() -> None:
    config = _config()
    builder = _builder(config, _sources())
    await builder.start()
    context = await builder.build(_request(config))
    focus = builder.focus_snapshot()
    assert context is not None and focus is not None
    assert context.focus_snapshot_id == focus.focus_id
    assert focus.focus_id.startswith("focus:")
    assert focus.claims_delivered == ("Tớ thích cà phê rang đậm.",)
    assert "Câu mới chỉ được tạo" not in focus.claims_delivered
    assert focus.unresolved_items == ("Mai thích loại cà phê nào?",)
    assert focus.continuation_pressure == 1.0
    assert focus.saturation == 0.5


@pytest.mark.asyncio
async def test_same_focus_sources_produce_same_focus_id() -> None:
    config = _config()
    builder = _builder(config, _sources())
    await builder.start()
    assert await builder.build(_request(config)) is not None
    first = builder.focus_snapshot()
    assert await builder.build(_request(config)) is not None
    second = builder.focus_snapshot()
    assert first is not None and second is not None
    assert first.focus_id == second.focus_id


@pytest.mark.asyncio
async def test_missing_self_focus_is_absent_without_creating_state() -> None:
    config = _config()
    builder = _builder(config, _sources(focused_thread_id=None))
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert context.focus_snapshot_id is None
    assert builder.focus_snapshot() is None


@pytest.mark.asyncio
async def test_mismatched_focus_is_omitted_and_context_is_degraded() -> None:
    config = _config()
    builder = _builder(config, _sources(focused_thread_id="missing-thread"))
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert context.focus_snapshot_id is None
    assert "thread" in context.operator_state.source_failure_codes


@pytest.mark.asyncio
async def test_stale_focus_is_omitted() -> None:
    config = _config()
    sources = _sources()
    thread = sources["thread"][0]  # type: ignore[index]
    stale = replace(
        thread,
        updated_at=NOW - timedelta(minutes=20),
        expires_at=NOW - timedelta(seconds=1),
    )
    sources["thread"] = (stale,)
    sources["agent"] = replace(sources["agent"], open_threads=(stale,))  # type: ignore[arg-type]
    builder = _builder(config, sources)
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert context.focus_snapshot_id is None
    assert builder.focus_snapshot() is None


@pytest.mark.asyncio
async def test_unverified_origin_cannot_materialize_focus() -> None:
    config = _config()
    sources = _sources()
    thread: OpenThread = sources["thread"][0]  # type: ignore[index]
    invalid = replace(thread, origin_event_id="speech-final-1")
    sources["thread"] = (invalid,)
    agent: AgentStateSnapshot = sources["agent"]  # type: ignore[assignment]
    sources["agent"] = replace(agent, open_threads=(invalid,))
    builder = _builder(config, sources)
    await builder.start()
    context = await builder.build(_request(config))
    assert context is not None
    assert context.focus_snapshot_id is None
    assert "thread" in context.operator_state.source_failure_codes


@pytest.mark.asyncio
async def test_focus_change_evicts_prior_projection_metric() -> None:
    config = _config()
    sources = _sources()
    builder = _builder(config, sources, clock=NOW + timedelta(seconds=1))
    await builder.start()
    assert await builder.build(_request(config)) is not None
    first = builder.focus_snapshot()
    assert first is not None

    original: OpenThread = sources["thread"][0]  # type: ignore[index]
    changed = replace(
        original,
        move_count=3,
        updated_at=NOW + timedelta(milliseconds=500),
    )
    builder._thread_manager.value = (changed,)  # type: ignore[attr-defined]
    builder._agent_state.value = replace(  # type: ignore[attr-defined]
        sources["agent"], open_threads=(changed,),
    )
    context = await builder.build(_request(
        config, requested_at=NOW + timedelta(seconds=1),
    ))
    second = builder.focus_snapshot()
    assert context is not None and second is not None
    assert second.focus_id != first.focus_id
    assert builder.get_metrics()["cognitive_context_builder_evicted"] == {"focus": 1}
