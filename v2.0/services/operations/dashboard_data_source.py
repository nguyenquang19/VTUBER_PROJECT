"""Independent dashboard source with deterministic live/history switching."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from interfaces.base import HealthStatus
from interfaces.operations import DashboardDataSourceService
from orchestrator.credential_contract import (
    require_dashboard_control_token,
    validate_dashboard_control_token,
)
from services.operations.standalone_snapshot import StandaloneSnapshotProvider


_SOURCE_MODES = frozenset({"auto", "live", "history"})
_COMMAND_PATTERNS = (
    re.compile(r"^/api/emergency_stop$"),
    re.compile(r"^/api/resume$"),
    re.compile(r"^/api/agent/(?:pause|resume)$"),
    re.compile(r"^/api/goals/pin$"),
    re.compile(r"^/api/goals/[^/]+/(?:complete|cancel)$"),
    re.compile(r"^/api/features/[^/]+/toggle$"),
)


class _UpstreamHttpError(OSError):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(f"upstream HTTP {status_code}")
        self.status_code = int(status_code)
        self.payload = payload


class DashboardDataSource(DashboardDataSourceService):
    """Read live loopback state when available and bounded local history otherwise."""

    service_id = "dashboard_data_source"

    def __init__(
        self,
        *,
        offline_provider: StandaloneSnapshotProvider,
        live_base_url: str,
        turns_path: str | Path,
        delivery_path: str | Path,
        request_timeout_s: float,
        max_files: int,
        max_records: int,
        default_limit: int,
        max_limit: int,
        control_token: str,
    ) -> None:
        parsed = urlparse(live_base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("dashboard live upstream must be an HTTP loopback URL")
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        for name, value in (
            ("max_files", max_files), ("max_records", max_records),
            ("default_limit", default_limit), ("max_limit", max_limit),
        ):
            if int(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if int(default_limit) > int(max_limit):
            raise ValueError("default_limit cannot exceed max_limit")

        self.offline_provider = offline_provider
        self.live_base_url = live_base_url.rstrip("/")
        self.turns_path = Path(turns_path)
        self.delivery_path = Path(delivery_path)
        self.request_timeout_s = float(request_timeout_s)
        self.max_files = int(max_files)
        self.max_records = int(max_records)
        self.default_limit = int(default_limit)
        self.max_limit = int(max_limit)
        self._control_token = validate_dashboard_control_token(
            control_token, source="dashboard_control_token",
        )
        self._running = False
        self._live_fetch_failures_total = 0
        self._history_queries_total = 0
        self._history_records_scanned = 0
        self._history_malformed_total = 0
        self._last_source_mode = "history"
        self._last_live_error = ""

    @classmethod
    def from_loader(cls, loader: Any) -> "DashboardDataSource":
        log_dir = Path(loader.get("logging", "jsonl.dir", "logs"))
        base = "dashboard_standalone"
        return cls(
            offline_provider=StandaloneSnapshotProvider.from_loader(loader),
            live_base_url=str(loader.get(
                "operations", f"{base}.live_base_url", "http://127.0.0.1:7860",
            )),
            turns_path=log_dir / str(loader.get(
                "logging", "jsonl.turns_file", "turns.jsonl",
            )),
            delivery_path=log_dir / str(loader.get(
                "logging", "jsonl.delivery_outcomes_file", "delivery_outcomes.jsonl",
            )),
            request_timeout_s=float(loader.get(
                "operations", f"{base}.request_timeout_s", 0.75,
            )),
            max_files=int(loader.get("operations", f"{base}.history.max_files", 6)),
            max_records=int(loader.get(
                "operations", f"{base}.history.max_records", 5000,
            )),
            default_limit=int(loader.get(
                "operations", f"{base}.history.default_limit", 100,
            )),
            max_limit=int(loader.get(
                "operations", f"{base}.history.max_limit", 500,
            )),
            control_token=require_dashboard_control_token(loader),
        )

    async def start(self) -> None:
        await self.offline_provider.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self.offline_provider.stop()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if self._last_live_error:
            return HealthStatus.degraded(
                self.service_id, "live upstream unavailable; history remains readable",
                source_mode=self._last_source_mode,
            )
        return HealthStatus.healthy(self.service_id, source_mode=self._last_source_mode)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "dashboard_source_mode": self._last_source_mode,
            "dashboard_live_fetch_failures_total": self._live_fetch_failures_total,
            "dashboard_history_queries_total": self._history_queries_total,
            "dashboard_history_records_scanned": self._history_records_scanned,
            "dashboard_history_malformed_total": self._history_malformed_total,
        }

    async def snapshot(self) -> dict[str, Any]:
        return await self.snapshot_for("auto")

    async def snapshot_for(self, source_mode: str) -> dict[str, Any]:
        requested = str(source_mode or "auto").strip().lower()
        if requested not in _SOURCE_MODES:
            requested = "auto"

        if requested in {"auto", "live"}:
            try:
                live = await asyncio.to_thread(self._request_json, "GET", "/api/snapshot", None)
                if not isinstance(live, dict):
                    raise ValueError("live snapshot is not an object")
                self._last_source_mode = "live"
                self._last_live_error = ""
                return self._with_source(live, requested=requested, actual="live", available=True)
            except (OSError, ValueError, TypeError) as exc:
                self._live_fetch_failures_total += 1
                self._last_live_error = "live_upstream_unavailable"
                if requested == "live":
                    self._last_source_mode = "live"
                    return self._with_source(
                        {"runtime": {"online": False, "controls_available": False}},
                        requested=requested, actual="live", available=False,
                        error="live_upstream_unavailable",
                    )

        offline = await self.offline_provider.snapshot()
        self._last_source_mode = "history"
        return self._with_source(
            offline, requested=requested, actual="history", available=True,
            error="live_upstream_unavailable" if requested == "auto" and self._last_live_error else "",
        )

    async def query_history(
        self,
        *,
        session_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        kind: str | None = None,
        delivered: bool | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self._history_queries_total += 1
        return await asyncio.to_thread(
            self._query_history_sync,
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
            kind=kind,
            delivered=delivered,
            limit=limit,
        )

    async def forward_command(
        self, path: str, payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        if not any(pattern.fullmatch(path) for pattern in _COMMAND_PATTERNS):
            return 403, {"ok": False, "reason": "command_not_allowlisted"}
        try:
            value = await asyncio.to_thread(self._request_json, "POST", path, payload)
        except _UpstreamHttpError as exc:
            return exc.status_code, exc.payload
        except (OSError, ValueError, TypeError):
            self._live_fetch_failures_total += 1
            self._last_live_error = "live command failed"
            return 503, {"ok": False, "reason": "live_upstream_unavailable"}
        if not isinstance(value, dict):
            return 502, {"ok": False, "reason": "invalid_upstream_response"}
        return 200, value

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "X-Mai-Operator-Token": self._control_token,
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.live_base_url}{path}", data=body, headers=headers, method=method,
        )
        try:
            with urlopen(request, timeout=self.request_timeout_s) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                value = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                value = {"ok": False, "reason": f"upstream_http_{exc.code}"}
            if isinstance(value, dict):
                raise _UpstreamHttpError(exc.code, value) from exc
            raise ValueError("invalid upstream error response") from exc
        except (URLError, TimeoutError) as exc:
            raise OSError(str(exc)) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("invalid upstream JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("upstream JSON must be an object")
        return value

    def _query_history_sync(
        self,
        *,
        session_id: str | None,
        started_at: str | None,
        ended_at: str | None,
        kind: str | None,
        delivered: bool | None,
        limit: int | None,
    ) -> dict[str, Any]:
        malformed = 0
        outcomes, malformed_outcomes = self._read_records(self.delivery_path)
        turns, malformed_turns = self._read_records(self.turns_path)
        malformed = malformed_outcomes + malformed_turns
        self._history_malformed_total += malformed
        self._history_records_scanned += len(outcomes) + len(turns)

        outcome_by_id: dict[tuple[str | None, str, int], dict[str, Any]] = {}
        for item in outcomes:
            identity = _identity(item)
            if identity is not None:
                outcome_by_id[identity] = item

        start_dt = _parse_time(started_at)
        end_dt = _parse_time(ended_at)
        wanted_session = str(session_id).strip() if session_id else None
        wanted_kind = str(kind).strip() if kind else None
        projected: list[dict[str, Any]] = []
        for turn in turns:
            if wanted_session and str(turn.get("session_id") or "") != wanted_session:
                continue
            if wanted_kind and str(turn.get("kind") or turn.get("trigger_type") or "") != wanted_kind:
                continue
            turn_time = _parse_time(turn.get("timestamp"))
            if start_dt is not None and (turn_time is None or turn_time < start_dt):
                continue
            if end_dt is not None and (turn_time is None or turn_time > end_dt):
                continue
            outcome = outcome_by_id.get(_identity(turn))
            delivery_value = outcome.get("delivered") if outcome is not None else None
            if delivered is not None and delivery_value is not delivered:
                continue
            item = dict(turn)
            item["delivered"] = delivery_value
            item["delivery_mode"] = outcome.get("mode") if outcome is not None else None
            item["delivery_timestamp"] = outcome.get("timestamp") if outcome is not None else None
            projected.append(item)

        projected.sort(key=lambda value: str(value.get("timestamp") or ""), reverse=True)
        safe_limit = self.default_limit if limit is None else max(1, int(limit))
        safe_limit = min(safe_limit, self.max_limit)
        return {
            "schema_version": 1,
            "read_only": True,
            "filters": {
                "session_id": wanted_session,
                "started_at": started_at,
                "ended_at": ended_at,
                "kind": wanted_kind,
                "delivered": delivered,
            },
            "total_matched": len(projected),
            "limit": safe_limit,
            "records": projected[:safe_limit],
            "malformed_skipped": malformed,
        }

    def _read_records(self, path: Path) -> tuple[list[dict[str, Any]], int]:
        records: deque[dict[str, Any]] = deque(maxlen=self.max_records)
        malformed = 0
        for item_path in _segments(path, self.max_files):
            try:
                lines = item_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    value = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if isinstance(value, dict):
                    records.append(value)
                else:
                    malformed += 1
        return list(records), malformed

    @staticmethod
    def _with_source(
        snapshot: dict[str, Any], *, requested: str, actual: str,
        available: bool, error: str = "",
    ) -> dict[str, Any]:
        value = dict(snapshot)
        runtime = dict(value.get("runtime") or {})
        if actual != "live" or not available:
            runtime.update({"online": False, "controls_available": False})
        value["runtime"] = runtime
        value["dashboard_source"] = {
            "schema_version": 1,
            "requested": requested,
            "actual": actual,
            "available": available,
            "read_only": actual != "live" or not available,
            "error": error,
            "sampled_at": (
                value.get("captured_at")
                or (value.get("mood") or {}).get("sampled_at")
                or datetime.now(timezone.utc).isoformat()
            ),
        }
        return value


def _segments(path: Path, max_files: int) -> list[Path]:
    rotated = [
        path.with_suffix(path.suffix + f".{index}")
        for index in range(max_files - 1, 0, -1)
    ]
    return [candidate for candidate in (*rotated, path) if candidate.exists()]


def _identity(value: dict[str, Any]) -> tuple[str | None, str, int] | None:
    try:
        return (
            value.get("session_id"), str(value["request_id"]), int(value["turn_id"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
