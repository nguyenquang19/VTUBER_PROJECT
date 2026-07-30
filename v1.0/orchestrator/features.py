"""Feature registry + toggle manager (ARCHITECTURE 4.2-4.5, Phase 0 task 2).

Toggle rules (4.4):
  1. atomic — enable/disable thành công hoàn toàn hoặc không đổi gì
  2. log timestamp + user
  3. dependency check — bật X cần depends_on của X đã bật
  4. conflict check — không bật X nếu conflicts_with đang bật
  5. resource check — VRAM budget từ config
  6. rollback nếu handler fail

`ToggleResult` / `ResourceCheck` / `DependencyGraph` được spec 4.4 reference
nhưng không định nghĩa — define ở đây mức tối giản (P6).

Persistence: state `enabled` ghi lại `config/features.yaml` (4.5).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from orchestrator.logger import get_logger

Handler = Callable[[], Awaitable[None]]
HealthCheck = Callable[[], Awaitable[bool]]


class FeatureStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass
class Feature:
    id: str
    name: str
    description: str
    category: str
    default_enabled: bool
    depends_on: list[str] = field(default_factory=list)
    conflicts_with: list[str] = field(default_factory=list)
    vram_cost_mb: int = 0
    latency_impact_ms: int = 0
    current_status: FeatureStatus = FeatureStatus.DISABLED
    enable_handler: Handler | None = None
    disable_handler: Handler | None = None
    health_check: HealthCheck | None = None

    @property
    def is_enabled(self) -> bool:
        return self.current_status in (FeatureStatus.ENABLED, FeatureStatus.DEGRADED)


@dataclass
class ToggleResult:
    ok: bool
    feature_id: str
    status: FeatureStatus
    reason: str = ""

    @classmethod
    def success(cls, feature_id: str, status: FeatureStatus) -> ToggleResult:
        return cls(ok=True, feature_id=feature_id, status=status)

    @classmethod
    def failure(cls, feature_id: str, status: FeatureStatus, reason: str) -> ToggleResult:
        return cls(ok=False, feature_id=feature_id, status=status, reason=reason)


@dataclass
class ResourceCheck:
    ok: bool
    requested_mb: int
    available_mb: int
    reason: str = ""


@dataclass
class DependencyGraph:
    feature_id: str
    #: feature mà `feature_id` cần (transitive)
    requires: list[str] = field(default_factory=list)
    #: feature đang cần `feature_id` (transitive) — tắt nó sẽ ảnh hưởng chúng
    required_by: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


class UnknownFeatureError(KeyError):
    """Feature id không có trong registry."""


class CoreFeatureError(Exception):
    """Feature core không toggle được (ARCHITECTURE 4.3)."""


class FeatureManager:
    """Registry + toggle. Không tự spawn service — handler do caller cung cấp."""

    def __init__(
        self,
        vram_budget_mb: int = 0,
        core_feature_ids: tuple[str, ...] = (),
        persist_path: Path | None = None,
    ) -> None:
        self._features: dict[str, Feature] = {}
        self._vram_budget_mb = vram_budget_mb
        self._core_ids = set(core_feature_ids)
        self._persist_path = persist_path
        self._lock = asyncio.Lock()
        self._log = get_logger("features")

    # ---------- construction ----------

    @classmethod
    def from_config(cls, loader) -> FeatureManager:
        """Build từ config/features.yaml + budget VRAM ở config/system.yaml."""
        total = int(loader.get("system", "resources.vram_total_mb", 0))
        reserved = int(loader.get("system", "resources.vram_reserved_mb", 0))
        buffer = int(loader.get("system", "resources.vram_buffer_mb", 0))
        budget = max(0, total - reserved - buffer)
        core_ids = tuple(loader.get("system", "features.core", []) or ())

        mgr = cls(vram_budget_mb=budget, core_feature_ids=core_ids)

        for fid, spec in (loader.section("features").get("features") or {}).items():
            spec = spec or {}
            mgr.register(
                Feature(
                    id=fid,
                    name=spec.get("name", fid),
                    description=spec.get("description", ""),
                    category=spec.get("category", "uncategorized"),
                    default_enabled=bool(spec.get("enabled", False)),
                    depends_on=list(spec.get("depends_on", []) or []),
                    conflicts_with=list(spec.get("conflicts_with", []) or []),
                    vram_cost_mb=int(spec.get("vram_cost_mb", 0)),
                    latency_impact_ms=int(spec.get("latency_impact_ms", 0)),
                    current_status=(
                        FeatureStatus.ENABLED
                        if spec.get("enabled", False)
                        else FeatureStatus.DISABLED
                    ),
                )
            )
        return mgr

    def register(self, feature: Feature) -> None:
        self._features[feature.id] = feature

    def attach_handlers(
        self,
        feature_id: str,
        *,
        enable: Handler | None = None,
        disable: Handler | None = None,
        health: HealthCheck | None = None,
    ) -> None:
        """Gắn handler sau khi register (service được tạo ở phase sau)."""
        f = self._get(feature_id)
        if enable is not None:
            f.enable_handler = enable
        if disable is not None:
            f.disable_handler = disable
        if health is not None:
            f.health_check = health

    def _get(self, feature_id: str) -> Feature:
        if feature_id not in self._features:
            raise UnknownFeatureError(feature_id)
        return self._features[feature_id]

    # ---------- query ----------

    async def get_status(self, feature_id: str) -> FeatureStatus:
        return self._get(feature_id).current_status

    async def list_features(self) -> list[Feature]:
        return [self._features[k] for k in sorted(self._features)]

    def is_core(self, feature_id: str) -> bool:
        return feature_id in self._core_ids

    def enabled_ids(self) -> list[str]:
        return sorted(f.id for f in self._features.values() if f.is_enabled)

    def used_vram_mb(self) -> int:
        """Tổng VRAM các feature đang bật (cost âm = tiết kiệm, vd kv_cache_q8)."""
        return sum(f.vram_cost_mb for f in self._features.values() if f.is_enabled)

    async def get_dependencies(self, feature_id: str) -> DependencyGraph:
        self._get(feature_id)

        requires: list[str] = []
        seen: set[str] = set()
        stack = list(self._get(feature_id).depends_on)
        while stack:
            dep = stack.pop()
            if dep in seen or dep == feature_id:
                continue
            seen.add(dep)
            requires.append(dep)
            if dep in self._features:
                stack.extend(self._features[dep].depends_on)

        required_by: list[str] = []
        for other in self._features.values():
            if other.id == feature_id:
                continue
            graph_deps: set[str] = set()
            stack = list(other.depends_on)
            while stack:
                d = stack.pop()
                if d in graph_deps:
                    continue
                graph_deps.add(d)
                if d in self._features:
                    stack.extend(self._features[d].depends_on)
            if feature_id in graph_deps:
                required_by.append(other.id)

        return DependencyGraph(
            feature_id=feature_id,
            requires=sorted(requires),
            required_by=sorted(required_by),
            conflicts=sorted(self._get(feature_id).conflicts_with),
        )

    async def check_resources(self, feature_id: str) -> ResourceCheck:
        f = self._get(feature_id)
        if f.is_enabled:
            return ResourceCheck(ok=True, requested_mb=0, available_mb=self._available_mb())
        available = self._available_mb()
        if f.vram_cost_mb <= 0:  # cost 0 hoặc âm (tiết kiệm) luôn OK
            return ResourceCheck(ok=True, requested_mb=f.vram_cost_mb, available_mb=available)
        if f.vram_cost_mb > available:
            return ResourceCheck(
                ok=False,
                requested_mb=f.vram_cost_mb,
                available_mb=available,
                reason=f"cần {f.vram_cost_mb}MB nhưng chỉ còn {available}MB",
            )
        return ResourceCheck(ok=True, requested_mb=f.vram_cost_mb, available_mb=available)

    def _available_mb(self) -> int:
        return self._vram_budget_mb - self.used_vram_mb()

    # ---------- toggle ----------

    async def enable(self, feature_id: str, user: str = "system") -> ToggleResult:
        async with self._lock:
            f = self._get(feature_id)

            if self.is_core(feature_id):
                raise CoreFeatureError(f"{feature_id} là core feature, không toggle được")

            if f.is_enabled:
                return ToggleResult.success(feature_id, f.current_status)

            # 3. dependency check
            missing = [
                d for d in f.depends_on
                if d not in self._features or not self._features[d].is_enabled
            ]
            if missing:
                return self._reject(f, user, "enable", f"thiếu dependency: {', '.join(missing)}")

            # 4. conflict check
            active_conflicts = [
                c for c in f.conflicts_with
                if c in self._features and self._features[c].is_enabled
            ]
            if active_conflicts:
                return self._reject(
                    f, user, "enable", f"xung đột với: {', '.join(active_conflicts)}"
                )

            # 5. resource check
            res = await self.check_resources(feature_id)
            if not res.ok:
                return self._reject(f, user, "enable", f"thiếu VRAM: {res.reason}")

            # 1 + 6. atomic + rollback
            previous = f.current_status
            f.current_status = FeatureStatus.ENABLED
            if f.enable_handler is not None:
                try:
                    await f.enable_handler()
                except Exception as e:
                    f.current_status = previous  # rollback
                    return self._reject(f, user, "enable", f"handler lỗi: {e}", FeatureStatus.ERROR)

            self._persist()
            self._log_toggle(f, user, "enable", ok=True)
            return ToggleResult.success(feature_id, f.current_status)

    async def disable(self, feature_id: str, user: str = "system") -> ToggleResult:
        async with self._lock:
            f = self._get(feature_id)

            if self.is_core(feature_id):
                raise CoreFeatureError(f"{feature_id} là core feature, không toggle được")

            if not f.is_enabled:
                return ToggleResult.success(feature_id, f.current_status)

            # dependency check ngược: feature khác đang bật mà cần cái này
            dependents = [
                other.id for other in self._features.values()
                if other.is_enabled and feature_id in other.depends_on
            ]
            if dependents:
                return self._reject(
                    f, user, "disable", f"đang được cần bởi: {', '.join(sorted(dependents))}"
                )

            previous = f.current_status
            f.current_status = FeatureStatus.DISABLED
            if f.disable_handler is not None:
                try:
                    await f.disable_handler()
                except Exception as e:
                    f.current_status = previous  # rollback
                    return self._reject(
                        f, user, "disable", f"handler lỗi: {e}", FeatureStatus.ERROR
                    )

            self._persist()
            self._log_toggle(f, user, "disable", ok=True)
            return ToggleResult.success(feature_id, f.current_status)

    def _reject(
        self,
        f: Feature,
        user: str,
        action: str,
        reason: str,
        status: FeatureStatus | None = None,
    ) -> ToggleResult:
        if status is not None:
            f.current_status = status
        self._log_toggle(f, user, action, ok=False, reason=reason)
        return ToggleResult.failure(f.id, f.current_status, reason)

    def _log_toggle(
        self, f: Feature, user: str, action: str, ok: bool, reason: str = ""
    ) -> None:
        """Rule 2: log timestamp + user."""
        self._log.info(
            "feature_toggle",
            feature_id=f.id,
            action=action,
            ok=ok,
            status=f.current_status.value,
            user=user,
            reason=reason,
            at=datetime.now(timezone.utc).isoformat(),
        )

    # ---------- health ----------

    async def refresh_health(self) -> dict[str, FeatureStatus]:
        """Chạy health_check của các feature đang bật; fail → DEGRADED."""
        out: dict[str, FeatureStatus] = {}
        for f in self._features.values():
            if not f.is_enabled or f.health_check is None:
                out[f.id] = f.current_status
                continue
            try:
                healthy = await f.health_check()
            except Exception:
                healthy = False
            f.current_status = FeatureStatus.ENABLED if healthy else FeatureStatus.DEGRADED
            out[f.id] = f.current_status
        return out

    # ---------- persistence (4.5) ----------

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        payload: dict[str, Any] = {"features": {}}
        for fid in sorted(self._features):
            f = self._features[fid]
            payload["features"][fid] = {
                "enabled": f.is_enabled,
                "vram_cost_mb": f.vram_cost_mb,
                "latency_impact_ms": f.latency_impact_ms,
                "category": f.category,
                "depends_on": f.depends_on,
                "conflicts_with": f.conflicts_with,
            }
        tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        tmp.replace(self._persist_path)  # atomic trên cùng volume
