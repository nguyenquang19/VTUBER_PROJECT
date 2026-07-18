"""
bus.py — EventBus: hệ thần kinh trung tâm (pub/sub) của hệ thống.

V1 dùng bản in-process bằng asyncio (đơn giản, không cần Redis).
Interface được thiết kế để sau này thay bằng RedisEventBus mà KHÔNG
phải đổi code các khối — chỉ đổi chỗ khởi tạo bus.

Nguyên tắc chống lỗi đã ghi trong tài liệu:
- Mỗi handler chạy trong task riêng -> một handler treo không kéo sập bus.
- Mọi lỗi trong handler được bắt & log, không làm chết vòng lặp.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from .events import Event, EventType

log = logging.getLogger("bus")

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[EventType, list[Handler]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def subscribe(self, etype: EventType, handler: Handler) -> None:
        """Đăng ký một handler async cho một loại event."""
        self._subs[etype].append(handler)

    def publish(self, event: Event) -> None:
        """
        Phát một event. KHÔNG chặn (fire-and-forget): mỗi handler chạy
        trong task riêng để một handler chậm/treo không cản các handler khác.
        """
        if self._closed:
            return
        for handler in self._subs.get(event.type, []):
            task = asyncio.create_task(self._run(handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:  # noqa: BLE001 - cố ý nuốt để bus không chết
            log.exception("Handler lỗi khi xử lý %s", event.type)

    async def close(self) -> None:
        self._closed = True
        # đợi các task đang chạy hoàn tất (có timeout để không kẹt)
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=2.0)
