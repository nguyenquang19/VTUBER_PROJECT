"""Feature registry, dependency validation and persistent toggle manager.

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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import yaml

from orchestrator.config_loader import ConfigError
from orchestrator.logger import get_logger

if TYPE_CHECKING:
    from services.operations.metrics import MetricsCollector

Handler = Callable[[], Awaitable[None]]
HealthCheck = Callable[[], Awaitable[bool]]

_FEATURE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FEATURE_HEADER_RE = re.compile(r"^  ([a-z][a-z0-9_]*):[ \t]*(?:#.*?)?(?:\r?\n)?$")
_ENABLED_LINE_RE = re.compile(
    r"^(    enabled:[ \t]*)(true|false)([ \t]*(?:#.*?)?)(\r?\n)?$",
)


def _strict_int(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} phải là integer thật")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{field_name} phải >= {minimum}")
    return value


def _strict_text(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} phải là string")
    if value != value.strip() or (not allow_empty and not value):
        raise ConfigError(f"{field_name} phải là string đã trim và không rỗng")
    return value


def _strict_feature_id(value: Any, field_name: str) -> str:
    feature_id = _strict_text(value, field_name)
    if _FEATURE_ID_RE.fullmatch(feature_id) is None:
        raise ConfigError(f"{field_name} không đúng feature id format")
    return feature_id


def _strict_id_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} phải là list")
    out = [
        _strict_feature_id(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    ]
    if len(set(out)) != len(out):
        raise ConfigError(f"{field_name} không được chứa id trùng")
    return out


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
    activation_allowed: bool = True
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
    """Raised when an operator attempts to toggle a core feature."""


class FeatureManager:
    """Registry + toggle. Không tự spawn service — handler do caller cung cấp."""

    def __init__(
        self,
        vram_budget_mb: int = 0,
        core_feature_ids: tuple[str, ...] = (),
        persist_path: Path | None = None,
        metrics: MetricsCollector | None = None,
        excluded_feature_ids: tuple[str, ...] = (),
    ) -> None:
        self._features: dict[str, Feature] = {}
        self._vram_budget_mb = vram_budget_mb
        self._core_ids = set(core_feature_ids)
        self._persist_path = persist_path
        self._metrics = metrics
        self._excluded_feature_ids = set(excluded_feature_ids)
        self._lock = asyncio.Lock()
        self._log = get_logger("features")

    # ---------- construction ----------

    @classmethod
    def from_config(
        cls, loader, *, persist: bool = False,
        metrics: MetricsCollector | None = None,
        excluded_categories: tuple[str, ...] = (),
        excluded_feature_ids: tuple[str, ...] = (),
    ) -> FeatureManager:
        """Build from strict YAML; production composition opts into persistence."""
        total = _strict_int(
            loader.get("system", "resources.vram_total_mb", 0),
            "system.resources.vram_total_mb",
            minimum=0,
        )
        reserved = _strict_int(
            loader.get("system", "resources.vram_reserved_mb", 0),
            "system.resources.vram_reserved_mb",
            minimum=0,
        )
        buffer = _strict_int(
            loader.get("system", "resources.vram_buffer_mb", 0),
            "system.resources.vram_buffer_mb",
            minimum=0,
        )
        if reserved + buffer > total:
            raise ConfigError("system.resources reserved + buffer vượt vram_total_mb")
        budget = total - reserved - buffer

        raw_core = loader.get("system", "features.core", [])
        core_ids = tuple(_strict_id_list(raw_core, "system.features.core"))

        section = loader.section("features")
        raw_features = section.get("features")
        if not isinstance(raw_features, Mapping):
            raise ConfigError("features.yaml::features phải là mapping")
        excluded = {
            _strict_text(value, "excluded_categories")
            for value in excluded_categories
        }
        excluded_ids = {
            _strict_feature_id(value, "excluded_feature_ids")
            for value in excluded_feature_ids
        }

        persist_path: Path | None = None
        if persist:
            path_for = getattr(loader, "path_for", None)
            if not callable(path_for):
                raise ConfigError("config loader không cung cấp path_for cho feature persistence")
            persist_path = path_for("features")

        mgr = cls(
            vram_budget_mb=budget,
            core_feature_ids=core_ids,
            persist_path=persist_path,
            metrics=metrics,
            excluded_feature_ids=tuple(
                sorted(excluded_ids & {str(value) for value in raw_features})
            ),
        )

        for raw_feature_id, raw_spec in raw_features.items():
            feature_id = _strict_feature_id(raw_feature_id, "features.<id>")
            if not isinstance(raw_spec, Mapping):
                raise ConfigError(f"features.{feature_id} phải là mapping")
            if "enabled" not in raw_spec or not isinstance(raw_spec["enabled"], bool):
                raise ConfigError(f"features.{feature_id}.enabled phải là boolean thật")
            enabled = raw_spec["enabled"]
            activation_allowed = raw_spec.get("activation_allowed", True)
            if not isinstance(activation_allowed, bool):
                raise ConfigError(
                    f"features.{feature_id}.activation_allowed phải là boolean thật"
                )
            name = _strict_text(
                raw_spec.get("name", feature_id),
                f"features.{feature_id}.name",
            )
            description = _strict_text(
                raw_spec.get("description", ""),
                f"features.{feature_id}.description",
                allow_empty=True,
            )
            category = _strict_text(
                raw_spec.get("category", "uncategorized"),
                f"features.{feature_id}.category",
            )
            if category in excluded or feature_id in excluded_ids:
                continue
            mgr.register(
                Feature(
                    id=feature_id,
                    name=name,
                    description=description,
                    category=category,
                    default_enabled=enabled,
                    activation_allowed=activation_allowed,
                    depends_on=_strict_id_list(
                        raw_spec.get("depends_on", []),
                        f"features.{feature_id}.depends_on",
                    ),
                    conflicts_with=_strict_id_list(
                        raw_spec.get("conflicts_with", []),
                        f"features.{feature_id}.conflicts_with",
                    ),
                    vram_cost_mb=_strict_int(
                        raw_spec.get("vram_cost_mb", 0),
                        f"features.{feature_id}.vram_cost_mb",
                    ),
                    latency_impact_ms=_strict_int(
                        raw_spec.get("latency_impact_ms", 0),
                        f"features.{feature_id}.latency_impact_ms",
                    ),
                    current_status=(
                        FeatureStatus.ENABLED if enabled else FeatureStatus.DISABLED
                    ),
                )
            )
        mgr._validate_initial_config()
        cognitive = mgr._features.get("cognitive_brain_shadow")
        if cognitive is not None and not cognitive.is_enabled:
            mgr._record_cognitive_feature_toggle("disabled")
        return mgr

    def register(self, feature: Feature) -> None:
        if feature.id in self._features:
            raise ValueError(f"feature id trùng: {feature.id}")
        self._features[feature.id] = feature

    def _validate_initial_config(self) -> None:
        known = set(self._features) | self._core_ids
        for feature in self._features.values():
            references = feature.depends_on + feature.conflicts_with
            unknown = sorted(set(references) - known)
            if unknown:
                raise ConfigError(
                    f"features.{feature.id} tham chiếu id không tồn tại: {', '.join(unknown)}",
                )
            if feature.id in references:
                raise ConfigError(f"features.{feature.id} không được tự tham chiếu")
            overlap = sorted(set(feature.depends_on) & set(feature.conflicts_with))
            if overlap:
                raise ConfigError(
                    f"features.{feature.id} vừa depend vừa conflict: {', '.join(overlap)}",
                )
            if feature.is_enabled and not feature.activation_allowed:
                raise ConfigError(
                    f"features.{feature.id} enabled nhưng activation_allowed=false"
                )
            if not feature.is_enabled:
                continue
            missing = sorted(
                dependency
                for dependency in feature.depends_on
                if dependency not in self._core_ids
                and not self._features[dependency].is_enabled
            )
            if missing:
                raise ConfigError(
                    f"features.{feature.id} enabled nhưng dependency tắt: {', '.join(missing)}",
                )
            active_conflicts = sorted(
                conflict
                for conflict in feature.conflicts_with
                if conflict in self._core_ids or self._features[conflict].is_enabled
            )
            if active_conflicts:
                raise ConfigError(
                    f"features.{feature.id} enabled nhưng conflict đang bật: "
                    f"{', '.join(active_conflicts)}",
                )
        if self.used_vram_mb() > self._vram_budget_mb:
            raise ConfigError(
                f"features enabled dùng {self.used_vram_mb()}MB vượt budget "
                f"{self._vram_budget_mb}MB",
            )

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

            if not f.activation_allowed:
                self._record_cognitive_feature_toggle("enable_rejected")
                return self._reject(
                    f, user, "enable", "activation bị khóa bởi contract hiện tại",
                )

            if f.is_enabled:
                return ToggleResult.success(feature_id, f.current_status)

            # 3. dependency check
            missing = [
                d for d in f.depends_on
                if d not in self._core_ids
                and (d not in self._features or not self._features[d].is_enabled)
            ]
            if missing:
                return self._reject(f, user, "enable", f"thiếu dependency: {', '.join(missing)}")

            # 4. conflict check
            active_conflicts = {
                c for c in f.conflicts_with
                if c in self._core_ids
                or (c in self._features and self._features[c].is_enabled)
            }
            active_conflicts.update(
                other.id
                for other in self._features.values()
                if other.is_enabled and feature_id in other.conflicts_with
            )
            if active_conflicts:
                return self._reject(
                    f, user, "enable",
                    f"xung đột với: {', '.join(sorted(active_conflicts))}",
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

            persist_failure = await self._persist_transition(
                f,
                previous=previous,
                user=user,
                action="enable",
                rollback_handler=f.disable_handler,
                side_effect_ran=f.enable_handler is not None,
            )
            if persist_failure is not None:
                return persist_failure
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

            persist_failure = await self._persist_transition(
                f,
                previous=previous,
                user=user,
                action="disable",
                rollback_handler=f.enable_handler,
                side_effect_ran=f.disable_handler is not None,
            )
            if persist_failure is not None:
                return persist_failure
            self._log_toggle(f, user, "disable", ok=True)
            return ToggleResult.success(feature_id, f.current_status)

    async def _persist_transition(
        self,
        f: Feature,
        *,
        previous: FeatureStatus,
        user: str,
        action: str,
        rollback_handler: Handler | None,
        side_effect_ran: bool,
    ) -> ToggleResult | None:
        try:
            self._persist()
        except Exception as exc:
            f.current_status = previous
            rollback_error = ""
            if side_effect_ran:
                if rollback_handler is None:
                    rollback_error = "missing inverse handler"
                else:
                    try:
                        await rollback_handler()
                    except Exception as rollback_exc:
                        rollback_error = str(rollback_exc)
            reason = f"persistence lỗi: {exc}"
            if rollback_error:
                f.current_status = FeatureStatus.ERROR
                reason = f"{reason}; rollback handler lỗi: {rollback_error}"
            return self._reject(f, user, action, reason)
        return None

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
        rendered = (
            self._render_existing_config(self._persist_path)
            if self._persist_path.exists()
            else self._render_new_config()
        )
        tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
        try:
            tmp.write_bytes(rendered.encode("utf-8"))
            tmp.replace(self._persist_path)  # atomic trên cùng volume
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _record_cognitive_feature_toggle(self, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.record_cognitive_feature_toggle(outcome)

    def _render_new_config(self) -> str:
        payload: dict[str, Any] = {"features": {}}
        for fid in sorted(self._features):
            f = self._features[fid]
            payload["features"][fid] = {
                "enabled": f.is_enabled,
                "activation_allowed": f.activation_allowed,
                "vram_cost_mb": f.vram_cost_mb,
                "latency_impact_ms": f.latency_impact_ms,
                "category": f.category,
                "depends_on": f.depends_on,
                "conflicts_with": f.conflicts_with,
                "name": f.name,
                "description": f.description,
            }
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    def _render_existing_config(self, path: Path) -> str:
        text = path.read_bytes().decode("utf-8")
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("features"), Mapping):
            raise ConfigError("features.yaml::features phải là mapping khi persist")
        disk_ids = set(parsed["features"])
        runtime_ids = set(self._features)
        expected_disk_ids = runtime_ids | self._excluded_feature_ids
        if disk_ids != expected_disk_ids:
            missing = sorted(expected_disk_ids - disk_ids)
            extra = sorted(disk_ids - expected_disk_ids)
            raise ConfigError(
                "feature inventory thay đổi trong lúc chạy; "
                f"missing={missing}, extra={extra}",
            )

        lines = text.splitlines(keepends=True)
        current_feature: str | None = None
        updated: set[str] = set()
        for index, line in enumerate(lines):
            header = _FEATURE_HEADER_RE.fullmatch(line)
            if header is not None:
                current_feature = header.group(1)
                continue
            enabled_line = _ENABLED_LINE_RE.fullmatch(line)
            if enabled_line is None or current_feature not in self._features:
                continue
            if current_feature in updated:
                raise ConfigError(
                    f"features.{current_feature}.enabled xuất hiện nhiều lần",
                )
            value = "true" if self._features[current_feature].is_enabled else "false"
            newline = enabled_line.group(4) or ""
            lines[index] = (
                f"{enabled_line.group(1)}{value}{enabled_line.group(3)}{newline}"
            )
            updated.add(current_feature)

        missing_enabled = sorted(runtime_ids - updated)
        if missing_enabled:
            raise ConfigError(
                "feature không có scalar enabled để persist: "
                f"{', '.join(missing_enabled)}",
            )
        rendered = "".join(lines)
        validated = yaml.safe_load(rendered)
        if not isinstance(validated, Mapping):
            raise ConfigError("features.yaml render không còn là mapping")
        return rendered
