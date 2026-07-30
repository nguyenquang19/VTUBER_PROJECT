"""Memory leak test (DoD Phase 0: 'Không memory leak sau 1h idle').

1 giờ thật không khả thi trong CI. Thay vào đó: nén thời gian — chạy MẬT ĐỘ
CAO các thao tác idle (state churn, event bus pub/sub, metric tick, dashboard
snapshot, health check, TTL prune) trong hàng chục nghìn vòng, đo tăng trưởng
bộ nhớ bằng tracemalloc. Nếu có leak (giữ tham chiếu, buffer phình vô hạn),
nó lộ ra ở đây rõ hơn cả 1h idle thật vì mật độ thao tác cao hơn nhiều.

Marker `slow` để có thể skip khi cần chạy nhanh: pytest -m "not slow".
"""
from __future__ import annotations

import asyncio
import gc
import tracemalloc

import pytest

from orchestrator.logger import setup_logging
from orchestrator.event_bus import EventBus
from orchestrator.health_monitor import HealthMonitor
from orchestrator.metrics_collector import MetricsCollector
from orchestrator.state_machine import ConversationStateMachine
from orchestrator.trigger_manager import TriggerManager
from interfaces.base import HealthStatus
from interfaces.input import EventSource, InputEvent
from datetime import datetime, timezone


def make_triggers() -> TriggerManager:
    return TriggerManager(
        priorities={"operator_voice": 100, "chat_mention": 60, "chat_normal": 30, "ambient_talk": 10},
        keywords=["mai"],
        rate_window_s=10.0,
        rate_max=1000000,  # không rate-limit trong leak test
        queue_max_size=30,
        default_ttl_s=1,
        ambient_enabled=False,
        ambient_silence_s=60,
        spam_min_length=2,
        spam_patterns=["^k+$"],
    )


async def _idle_cycle(sm, bus, metrics, triggers, health, sub, i: int) -> None:
    """1 chu kỳ idle: đủ 1 turn + churn event/metric/health."""
    # full turn cycle
    await sm.trigger_received()
    await sm.first_token()
    await sm.tts_complete()
    await sm.cooldown_elapsed()

    # event bus churn (publish + drain)
    bus.publish("tick", {"i": i})
    with contextlib_suppress():
        sub_get_nowait(sub)

    # metrics
    metrics.tick_fake_metrics(t=float(i))

    # trigger queue churn (enqueue + drain + TTL)
    ev = InputEvent(
        event_id=f"e{i}", timestamp=datetime.now(timezone.utc),
        source=EventSource.CHAT_TWITCH, content=f"tin {i}",
    )
    await triggers.process_event(ev)
    await triggers.get_next_trigger()

    # health
    await health.check_once()


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception)


def sub_get_nowait(sub) -> None:
    # drain 1 item nếu có (không block)
    try:
        sub._queue.get_nowait()
    except Exception:
        pass


@pytest.mark.slow
async def test_no_leak_under_idle_churn(tmp_path) -> None:
    # Tắt console log: 1h idle thật chỉ ~vài chục transition/phút, nhưng leak
    # test nén hàng nghìn vòng → log I/O sẽ nhiễu phép đo + làm chậm vô ích.
    setup_logging(level="ERROR", console_enabled=False, jsonl_enabled=False, log_dir=tmp_path)

    sm = ConversationStateMachine(cooldown_ms=1, auto_cooldown=False, history_limit=50)
    bus = EventBus(max_queue_size=100)
    sub = bus.subscribe("tick")
    metrics = MetricsCollector()
    triggers = make_triggers()
    health = HealthMonitor()
    health.register("dummy", lambda: _ok())

    # warmup để loại chi phí khởi tạo lần đầu (import lazy, cache...)
    for i in range(500):
        await _idle_cycle(sm, bus, metrics, triggers, health, sub, i)
    gc.collect()

    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()

    N = 8000
    for i in range(N):
        await _idle_cycle(sm, bus, metrics, triggers, health, sub, i)

    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    stats = snap_after.compare_to(snap_before, "filename")
    total_growth = sum(s.size_diff for s in stats)
    growth_kb = total_growth / 1024

    # Bounded structures (history_limit=50, deque, heap drained) → tăng trưởng
    # phải nhỏ. Ngưỡng 512KB cho 8000 vòng rất rộng rãi; leak thật sẽ vượt xa.
    assert growth_kb < 512, (
        f"Nghi ngờ memory leak: tăng {growth_kb:.1f}KB sau {N} vòng idle.\n"
        + "\n".join(str(s) for s in stats[:5])
    )


async def _ok() -> HealthStatus:
    return HealthStatus.healthy("dummy")


@pytest.mark.slow
async def test_bounded_structures_stay_bounded(tmp_path) -> None:
    """History/queue/event có bound → size không tăng theo số vòng."""
    setup_logging(level="ERROR", console_enabled=False, jsonl_enabled=False, log_dir=tmp_path)
    sm = ConversationStateMachine(cooldown_ms=1, auto_cooldown=False, history_limit=50)
    bus = EventBus(max_queue_size=10)
    sub = bus.subscribe("t")
    metrics = MetricsCollector()
    triggers = make_triggers()
    health = HealthMonitor()
    health.register("d", _ok)

    for i in range(3000):
        await _idle_cycle(sm, bus, metrics, triggers, health, sub, i)

    assert len(sm.history()) <= 50           # history_limit
    assert sub.qsize() <= 10                 # event queue bound
    assert len(triggers._heap) <= 30         # trigger queue drained/bounded
    stats = await triggers.get_queue_stats()
    assert stats.size <= 30
