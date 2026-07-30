"""Fallback Manager skeleton (ARCHITECTURE 8.7.7, Phase 0 task 9).

N1: 2 level mỗi chain, KHÔNG circuit breaker (add sau khi thấy fail lặp lại).
N7 fail-safe: chạy hết chain, level cuối fail → raise AllFallbacksFailedError
để caller quyết (thường level cuối là canned/subtitle nên hiếm khi raise).

Phase 0 scope: cơ chế execute chain + timeout per level + log. Các service
level thật (PrimaryLLM, CannedResponse, ...) do Phase 1/4 đăng ký qua
register_chain(). Skeleton không hardcode service nào (N8).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from orchestrator.logger import get_logger

#: 1 level = coroutine nhận request, trả kết quả
LevelHandler = Callable[[Any], Awaitable[Any]]


class AllFallbacksFailedError(Exception):
    def __init__(self, module_id: str, errors: list[str]) -> None:
        self.module_id = module_id
        self.errors = errors
        super().__init__(f"Tất cả fallback level của '{module_id}' đều fail: {errors}")


class UnknownChainError(KeyError):
    pass


@dataclass
class FallbackResult:
    module_id: str
    value: Any
    level_used: int
    attempts: int


@dataclass
class _Chain:
    handlers: list[LevelHandler]
    timeouts: list[float]


class FallbackManager:
    def __init__(self, event_bus: Any = None) -> None:
        self._chains: dict[str, _Chain] = {}
        self._event_bus = event_bus
        self._log = get_logger("fallback_manager")
        self._fallback_counts: dict[str, int] = {}

    def register_chain(
        self, module_id: str, handlers: list[LevelHandler], timeouts: list[float]
    ) -> None:
        """Đăng ký chain. N1: đúng 2 level."""
        if len(handlers) != 2:
            raise ValueError(
                f"'{module_id}': chain phải có đúng 2 level (N1), nhận {len(handlers)}"
            )
        if len(timeouts) != len(handlers):
            raise ValueError(
                f"'{module_id}': số timeout ({len(timeouts)}) phải khớp số level ({len(handlers)})"
            )
        self._chains[module_id] = _Chain(handlers=list(handlers), timeouts=list(timeouts))

    def has_chain(self, module_id: str) -> bool:
        return module_id in self._chains

    async def execute(self, module_id: str, request: Any) -> FallbackResult:
        if module_id not in self._chains:
            raise UnknownChainError(module_id)
        chain = self._chains[module_id]

        errors: list[str] = []
        for level, (handler, timeout) in enumerate(zip(chain.handlers, chain.timeouts)):
            try:
                value = await asyncio.wait_for(handler(request), timeout=timeout)
                if level > 0:
                    self._log.info(
                        "fallback_recovered", module=module_id, level_used=level
                    )
                return FallbackResult(
                    module_id=module_id, value=value, level_used=level, attempts=level + 1
                )
            except Exception as e:
                reason = "timeout" if isinstance(e, asyncio.TimeoutError) else type(e).__name__
                errors.append(f"L{level}:{reason}")
                self._fallback_counts[module_id] = self._fallback_counts.get(module_id, 0) + 1
                self._log.warning(
                    "fallback_triggered",
                    module=module_id,
                    level=level,
                    error=str(e) or reason,
                )
                if self._event_bus is not None:
                    self._event_bus.publish(
                        "fallback_triggered",
                        {"module": module_id, "level": level, "reason": reason},
                    )
                continue

        raise AllFallbacksFailedError(module_id, errors)

    def get_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {"fallback_chains": len(self._chains)}
        for module_id, count in self._fallback_counts.items():
            metrics[f"fallback_triggered_total.{module_id}"] = count
        return metrics
