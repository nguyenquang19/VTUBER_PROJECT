"""Base interface cho mọi service (ARCHITECTURE 7.1).

`HealthStatus` được spec reference ở 7.1 nhưng không định nghĩa — define ở đây
theo mức tối giản đủ dùng cho health_monitor + dashboard (P6 simplicity).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


class HealthStatus(BaseModel):
    """Kết quả health_check() của 1 service."""

    state: HealthState
    service_id: str
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.state is HealthState.HEALTHY

    @classmethod
    def healthy(cls, service_id: str, **details: Any) -> HealthStatus:
        return cls(state=HealthState.HEALTHY, service_id=service_id, details=details)

    @classmethod
    def degraded(cls, service_id: str, message: str, **details: Any) -> HealthStatus:
        return cls(
            state=HealthState.DEGRADED,
            service_id=service_id,
            message=message,
            details=details,
        )

    @classmethod
    def unhealthy(cls, service_id: str, message: str, **details: Any) -> HealthStatus:
        return cls(
            state=HealthState.UNHEALTHY,
            service_id=service_id,
            message=message,
            details=details,
        )

    @classmethod
    def stopped(cls, service_id: str) -> HealthStatus:
        return cls(state=HealthState.STOPPED, service_id=service_id)


class Service(ABC):
    """Base class cho tất cả service modules (ARCHITECTURE 7.1)."""

    #: id ổn định, dùng trong log / metric label / health report
    service_id: str = "unnamed"

    @abstractmethod
    async def start(self) -> None:
        """Initialize resources, load models."""

    @abstractmethod
    async def stop(self) -> None:
        """Cleanup resources."""

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Return current health."""

    @abstractmethod
    def get_metrics(self) -> dict[str, Any]:
        """Return current metrics."""
