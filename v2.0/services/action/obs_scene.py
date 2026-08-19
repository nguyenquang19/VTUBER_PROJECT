"""OBS WebSocket 5.x scene transport, executor and authoritative verifier."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from interfaces.action_execution import ActionVerifier, VerificationResult
from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionResult, ActionStatus
from interfaces.external_executor import (
    ExternalActionExecutor,
    OBSCommandAck,
    OBSSceneState,
    OBSSceneTransportService,
    RollbackResult,
    RollbackStatus,
)


T = TypeVar("T")


class OBSProtocolError(RuntimeError):
    """Sanitized OBS transport failure safe to convert into an action reason code."""

    def __init__(self, reason_code: str, *, retryable: bool = False) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    clean = value.strip()
    if clean != value:
        raise ValueError(f"{field_name} must be canonical")
    return clean


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive int")
    return value


def _positive_number(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a finite positive number")
    return float(value)


def _non_negative_number(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return float(value)


@dataclass(frozen=True)
class OBSSceneConfig:
    host: str
    port: int
    use_tls: bool
    password_env: str
    connect_timeout_s: float
    request_timeout_s: float
    health_timeout_s: float
    health_ttl_s: float
    max_attempts: int
    retry_backoff_s: float
    max_scene_name_chars: int
    max_authority_records: int
    max_evidence_refs: int
    max_message_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", _required_text(self.host, "host"))
        object.__setattr__(self, "password_env", _required_text(
            self.password_env, "password_env",
        ))
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be an int between 1 and 65535")
        if not isinstance(self.use_tls, bool):
            raise ValueError("use_tls must be a bool")
        for field_name in (
            "connect_timeout_s", "request_timeout_s", "health_timeout_s", "health_ttl_s",
        ):
            object.__setattr__(
                self, field_name, _positive_number(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "retry_backoff_s", _non_negative_number(
            self.retry_backoff_s, "retry_backoff_s",
        ))
        for field_name in (
            "max_attempts", "max_scene_name_chars", "max_authority_records",
            "max_evidence_refs", "max_message_bytes",
        ):
            object.__setattr__(
                self, field_name, _positive_int(getattr(self, field_name), field_name),
            )

    @classmethod
    def from_loader(cls, loader: Any) -> "OBSSceneConfig":
        raw = loader.get("capabilities", "external_actions.obs", None)
        if not isinstance(raw, Mapping):
            raise ValueError("external_actions.obs config must be a mapping")
        return cls(
            host=raw.get("host"),
            port=raw.get("port"),
            use_tls=raw.get("use_tls"),
            password_env=raw.get("password_env"),
            connect_timeout_s=raw.get("connect_timeout_s"),
            request_timeout_s=raw.get("request_timeout_s"),
            health_timeout_s=raw.get("health_timeout_s"),
            health_ttl_s=raw.get("health_ttl_s"),
            max_attempts=raw.get("max_attempts"),
            retry_backoff_s=raw.get("retry_backoff_s"),
            max_scene_name_chars=raw.get("max_scene_name_chars"),
            max_authority_records=raw.get("max_authority_records"),
            max_evidence_refs=raw.get("max_evidence_refs"),
            max_message_bytes=raw.get("max_message_bytes"),
        )


class OBSWebSocketTransport(OBSSceneTransportService):
    """One-request-per-connection OBS WebSocket 5.x client."""

    service_id = "obs_websocket"

    def __init__(
        self,
        config: OBSSceneConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(config, OBSSceneConfig):
            raise ValueError("config must be OBSSceneConfig")
        self._config = config
        self._environ = os.environ if environ is None else environ
        self._running = False
        self._request_sequence = 0
        self._outcomes: dict[str, int] = {}

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        try:
            await asyncio.wait_for(
                self.get_current_program_scene(), timeout=self._config.health_timeout_s,
            )
        except Exception as exc:
            return HealthStatus.unhealthy(
                self.service_id, _reason_from_exception(exc),
            )
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "obs_transport_running": int(self._running),
            "obs_transport_outcomes": dict(sorted(self._outcomes.items())),
        }

    async def get_current_program_scene(self) -> OBSSceneState:
        response, request_id = await self._request("GetCurrentProgramScene", {})
        scene_name = response.get("currentProgramSceneName")
        if not isinstance(scene_name, str) or not scene_name.strip() or scene_name != scene_name.strip():
            self._record("malformed_scene")
            raise OBSProtocolError("obs_malformed_scene")
        if len(scene_name) > self._config.max_scene_name_chars or _has_control(scene_name):
            self._record("invalid_scene")
            raise OBSProtocolError("obs_invalid_scene")
        return OBSSceneState(
            scene_name=scene_name,
            evidence_ref=f"obs:GetCurrentProgramScene:{request_id}",
        )

    async def set_current_program_scene(self, scene_name: str) -> OBSCommandAck:
        scene_name = _strict_scene(scene_name, self._config.max_scene_name_chars)
        _, request_id = await self._request(
            "SetCurrentProgramScene", {"sceneName": scene_name},
        )
        return OBSCommandAck(
            request_id=request_id,
            accepted=True,
            evidence_ref=f"obs:SetCurrentProgramScene:{request_id}",
        )

    async def _request(
        self, request_type: str, request_data: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], str]:
        if not self._running:
            raise OBSProtocolError("obs_transport_stopped")
        request_id = self._next_request_id(request_type)
        scheme = "wss" if self._config.use_tls else "ws"
        uri = f"{scheme}://{self._config.host}:{self._config.port}"
        try:
            from websockets.asyncio.client import connect

            async with connect(
                uri,
                open_timeout=self._config.connect_timeout_s,
                close_timeout=self._config.connect_timeout_s,
                max_size=self._config.max_message_bytes,
                ping_interval=None,
            ) as websocket:
                hello = await self._receive_json(websocket)
                if hello.get("op") != 0 or not isinstance(hello.get("d"), Mapping):
                    raise OBSProtocolError("obs_invalid_hello")
                hello_data = hello["d"]
                identify_data: dict[str, Any] = {"rpcVersion": 1}
                authentication = hello_data.get("authentication")
                if authentication is not None:
                    identify_data["authentication"] = self._authentication(authentication)
                await websocket.send(json.dumps({"op": 1, "d": identify_data}))
                identified = await self._receive_json(websocket)
                if identified.get("op") != 2:
                    raise OBSProtocolError("obs_identification_failed")
                await websocket.send(json.dumps({
                    "op": 6,
                    "d": {
                        "requestType": request_type,
                        "requestId": request_id,
                        "requestData": dict(request_data),
                    },
                }))
                response = await self._receive_json(websocket)
        except asyncio.CancelledError:
            raise
        except OBSProtocolError:
            raise
        except asyncio.TimeoutError as exc:
            self._record("timeout")
            raise OBSProtocolError("obs_timeout", retryable=True) from exc
        except (OSError, ConnectionError) as exc:
            self._record("disconnected")
            raise OBSProtocolError("obs_disconnected", retryable=True) from exc
        except Exception as exc:
            self._record("transport_error")
            raise OBSProtocolError("obs_transport_error", retryable=True) from exc
        if response.get("op") != 7 or not isinstance(response.get("d"), Mapping):
            raise OBSProtocolError("obs_invalid_response")
        data = response["d"]
        if data.get("requestType") != request_type or data.get("requestId") != request_id:
            raise OBSProtocolError("obs_request_mismatch")
        status = data.get("requestStatus")
        if not isinstance(status, Mapping) or not isinstance(status.get("result"), bool):
            raise OBSProtocolError("obs_invalid_request_status")
        if status["result"] is not True:
            self._record("request_rejected")
            raise OBSProtocolError("obs_request_rejected")
        response_data = data.get("responseData", {})
        if not isinstance(response_data, Mapping):
            raise OBSProtocolError("obs_invalid_response_data")
        self._record("request_success")
        return response_data, request_id

    async def _receive_json(self, websocket: Any) -> Mapping[str, Any]:
        raw = await asyncio.wait_for(
            websocket.recv(), timeout=self._config.request_timeout_s,
        )
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self._config.max_message_bytes:
            raise OBSProtocolError("obs_invalid_message")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OBSProtocolError("obs_invalid_json") from exc
        if not isinstance(payload, Mapping):
            raise OBSProtocolError("obs_invalid_message")
        return payload

    def _authentication(self, raw: object) -> str:
        if not isinstance(raw, Mapping):
            raise OBSProtocolError("obs_invalid_authentication")
        challenge = raw.get("challenge")
        salt = raw.get("salt")
        if not isinstance(challenge, str) or not challenge or not isinstance(salt, str) or not salt:
            raise OBSProtocolError("obs_invalid_authentication")
        password = self._environ.get(self._config.password_env)
        if not isinstance(password, str) or not password:
            raise OBSProtocolError("obs_credentials_missing")
        secret = base64.b64encode(
            hashlib.sha256((password + salt).encode("utf-8")).digest(),
        ).decode("ascii")
        return base64.b64encode(
            hashlib.sha256((secret + challenge).encode("utf-8")).digest(),
        ).decode("ascii")

    def _next_request_id(self, request_type: str) -> str:
        self._request_sequence += 1
        digest = hashlib.sha256(
            f"{request_type}:{self._request_sequence}".encode("utf-8"),
        ).hexdigest()[:16]
        return f"mai-{digest}"

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1


@dataclass(frozen=True)
class _OBSAttempt:
    target_scene: str
    previous_scene: str
    mutation_possible: bool


class _OBSAttemptAuthority:
    def __init__(self, max_records: int) -> None:
        self._max_records = _positive_int(max_records, "max_records")
        self._records: OrderedDict[str, _OBSAttempt] = OrderedDict()

    def put(self, action_id: str, attempt: _OBSAttempt) -> None:
        if not isinstance(attempt, _OBSAttempt):
            raise ValueError("attempt must be _OBSAttempt")
        self._records[action_id] = attempt
        self._records.move_to_end(action_id)
        while len(self._records) > self._max_records:
            self._records.popitem(last=False)

    def get(self, action_id: str) -> _OBSAttempt | None:
        value = self._records.get(action_id)
        if value is not None:
            self._records.move_to_end(action_id)
        return value

    def __len__(self) -> int:
        return len(self._records)


class OBSSceneExecutor(ExternalActionExecutor):
    service_id = "obs_scene"

    def __init__(
        self,
        config: OBSSceneConfig,
        transport: OBSSceneTransportService,
        *,
        enabled: bool = False,
        metrics: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, OBSSceneConfig):
            raise ValueError("config must be OBSSceneConfig")
        if not isinstance(transport, OBSSceneTransportService):
            raise ValueError("transport must implement OBSSceneTransportService")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._transport = transport
        self._enabled = enabled
        self._metrics = metrics
        self._monotonic = monotonic
        self._authority = _OBSAttemptAuthority(config.max_authority_records)
        self._running = False
        self._last_health_ok_at: float | None = None
        self._last_health_reason = "not_probed"
        self._outcomes: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._enabled = enabled
        if not enabled:
            self._last_health_ok_at = None
            self._last_health_reason = "feature_disabled"

    async def start(self) -> None:
        if self._running:
            return
        await self._transport.start()
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._last_health_ok_at = None
        await self._transport.stop()

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        try:
            await asyncio.wait_for(
                self._retry(self._transport.get_current_program_scene),
                timeout=self._config.health_timeout_s,
            )
        except Exception as exc:
            self._last_health_ok_at = None
            self._last_health_reason = _reason_from_exception(exc)
            self._record("health_failed")
            return HealthStatus.unhealthy(self.service_id, self._last_health_reason)
        self._last_health_ok_at = self._monotonic()
        self._last_health_reason = "healthy"
        self._record("health_success")
        return HealthStatus.healthy(self.service_id)

    def public_health(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        if self._last_health_ok_at is None:
            return HealthStatus.unhealthy(self.service_id, self._last_health_reason)
        if self._monotonic() - self._last_health_ok_at > self._config.health_ttl_s:
            return HealthStatus.unhealthy(self.service_id, "health_stale")
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "obs_scene_executor_enabled": self._enabled,
            "obs_scene_executor_running": self._running,
            "obs_scene_executor_authority_records": len(self._authority),
            "obs_scene_executor_outcomes": dict(sorted(self._outcomes.items())),
        }

    async def execute(self, request: ActionRequest) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ValueError("request must be ActionRequest")
        started = _now()
        error = _scene_request_error(request, self._config.max_scene_name_chars)
        if not self._running:
            error = "executor_stopped"
        elif not self._enabled:
            error = "feature_disabled"
        if error is not None:
            self._record(error)
            return _action_result(request, started, ActionStatus.REJECTED, error)
        target = request.target
        assert isinstance(target, str)
        try:
            previous = await self._retry(self._transport.get_current_program_scene)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = _reason_from_exception(exc)
            self._record(reason)
            return _action_result(request, started, ActionStatus.FAILED, reason)
        self._authority.put(request.action_id, _OBSAttempt(target, previous.scene_name, False))
        self._authority.put(request.action_id, _OBSAttempt(target, previous.scene_name, True))
        try:
            ack = await self._retry(lambda: self._transport.set_current_program_scene(target))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = _reason_from_exception(exc)
            self._record(reason)
            return _action_result(
                request, started, ActionStatus.UNKNOWN, reason,
                data={"target_scene": target, "previous_scene": previous.scene_name},
            )
        if not isinstance(ack, OBSCommandAck):
            self._record("invalid_ack")
            return _action_result(
                request, started, ActionStatus.UNKNOWN, "obs_invalid_ack",
                data={"target_scene": target, "previous_scene": previous.scene_name},
            )
        if not ack.accepted:
            reason = ack.error_code or "obs_request_rejected"
            self._record(reason)
            return _action_result(
                request, started, ActionStatus.FAILED, reason,
                data={"target_scene": target, "previous_scene": previous.scene_name},
            )
        self._record("command_acknowledged")
        return _action_result(
            request,
            started,
            ActionStatus.SUCCESS,
            None,
            data={
                "target_scene": target,
                "previous_scene": previous.scene_name,
                "command_acknowledged": True,
                "command_evidence_ref": ack.evidence_ref,
            },
        )

    async def rollback(
        self, request: ActionRequest, result: ActionResult,
    ) -> RollbackResult:
        attempt = self._authority.get(request.action_id)
        if attempt is None or not attempt.mutation_possible:
            return self._rollback(RollbackStatus.SKIPPED, "rollback_not_required")
        evidence: list[str] = []
        try:
            current = await self._retry(self._transport.get_current_program_scene)
            evidence.append(current.evidence_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._rollback(RollbackStatus.UNKNOWN, "rollback_state_unknown")
        if current.scene_name == attempt.previous_scene:
            return self._rollback(
                RollbackStatus.SKIPPED, "rollback_not_required", evidence,
            )
        if current.scene_name != attempt.target_scene:
            return self._rollback(
                RollbackStatus.SKIPPED, "rollback_operator_scene_changed", evidence,
            )
        try:
            ack = await self._retry(
                lambda: self._transport.set_current_program_scene(attempt.previous_scene),
            )
            if not isinstance(ack, OBSCommandAck) or not ack.accepted:
                return self._rollback(RollbackStatus.FAILED, "rollback_command_failed", evidence)
            evidence.append(ack.evidence_ref)
            verified = await self._retry(self._transport.get_current_program_scene)
            evidence.append(verified.evidence_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._rollback(RollbackStatus.UNKNOWN, "rollback_verification_unknown", evidence)
        if verified.scene_name != attempt.previous_scene:
            return self._rollback(RollbackStatus.FAILED, "rollback_scene_mismatch", evidence)
        return self._rollback(RollbackStatus.SUCCEEDED, "rollback_verified", evidence)

    async def _retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        last: Exception | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                return await asyncio.wait_for(
                    operation(), timeout=self._config.request_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                retryable = isinstance(exc, (asyncio.TimeoutError, OSError)) or (
                    isinstance(exc, OBSProtocolError) and exc.retryable
                )
                if not retryable or attempt >= self._config.max_attempts:
                    raise
                self._record("retry")
                if self._config.retry_backoff_s:
                    await asyncio.sleep(self._config.retry_backoff_s)
        assert last is not None
        raise last

    def _rollback(
        self,
        status: RollbackStatus,
        reason: str,
        evidence: list[str] | None = None,
    ) -> RollbackResult:
        self._record(f"rollback_{status.value}")
        return RollbackResult(
            status=status,
            reason_code=reason,
            evidence_refs=tuple((evidence or [])[:self._config.max_evidence_refs]),
        )

    def _record(self, outcome: str) -> None:
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_external_action", None)
        if callable(recorder):
            try:
                recorder("obs_scene", outcome)
            except Exception:
                pass


class OBSSceneVerifier(ActionVerifier):
    service_id = "obs_scene_state"

    def __init__(
        self,
        config: OBSSceneConfig,
        transport: OBSSceneTransportService,
        *,
        enabled: bool = False,
        metrics: Any = None,
    ) -> None:
        if not isinstance(config, OBSSceneConfig):
            raise ValueError("config must be OBSSceneConfig")
        if not isinstance(transport, OBSSceneTransportService):
            raise ValueError("transport must implement OBSSceneTransportService")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._config = config
        self._transport = transport
        self._enabled = enabled
        self._metrics = metrics
        self._running = False
        self._outcomes: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        self._enabled = enabled

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self._enabled:
            return HealthStatus.degraded(self.service_id, "feature_disabled")
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "obs_scene_verifier_enabled": self._enabled,
            "obs_scene_verifier_running": self._running,
            "obs_scene_verifier_outcomes": dict(sorted(self._outcomes.items())),
        }

    async def verify(
        self, request: ActionRequest, result: ActionResult,
    ) -> VerificationResult:
        if not isinstance(request, ActionRequest) or not isinstance(result, ActionResult):
            raise ValueError("verify requires typed request and result")
        if not self._running:
            return self._result(False, "verifier_stopped")
        if not self._enabled:
            return self._result(False, "feature_disabled")
        request_error = _scene_request_error(request, self._config.max_scene_name_chars)
        if request_error is not None:
            return self._result(False, request_error)
        if result.action_id != request.action_id or result.status is not ActionStatus.SUCCESS:
            return self._result(False, "executor_result_mismatch")
        if result.verified:
            return self._result(False, "executor_claimed_verification")
        try:
            state = await self._retry(self._transport.get_current_program_scene)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._result(False, _reason_from_exception(exc))
        if not isinstance(state, OBSSceneState):
            return self._result(False, "obs_invalid_scene_state")
        if state.scene_name != request.target:
            return self._result(False, "scene_mismatch", (state.evidence_ref,))
        return self._result(True, "scene_verified", (state.evidence_ref,))

    async def _retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                return await asyncio.wait_for(
                    operation(), timeout=self._config.request_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retryable = isinstance(exc, (asyncio.TimeoutError, OSError)) or (
                    isinstance(exc, OBSProtocolError) and exc.retryable
                )
                if not retryable or attempt >= self._config.max_attempts:
                    raise
                self._outcomes["retry"] = self._outcomes.get("retry", 0) + 1
                if self._config.retry_backoff_s:
                    await asyncio.sleep(self._config.retry_backoff_s)
        raise AssertionError("bounded verifier retry exhausted without outcome")

    def _result(
        self,
        verified: bool,
        reason: str,
        evidence: tuple[str, ...] = (),
    ) -> VerificationResult:
        outcome = "verified" if verified else reason
        self._outcomes[outcome] = self._outcomes.get(outcome, 0) + 1
        recorder = getattr(self._metrics, "record_external_action", None)
        if callable(recorder):
            try:
                recorder("obs_verifier", outcome)
            except Exception:
                pass
        return VerificationResult(
            verified=verified,
            source="obs_websocket",
            reason_code=reason,
            evidence_refs=evidence[:self._config.max_evidence_refs],
        )


def _scene_request_error(request: ActionRequest, max_chars: int) -> str | None:
    if request.schema_version != 1:
        return "unsupported_schema"
    if request.capability_id != "SWITCH_SCENE" or request.action_type != "SWITCH_SCENE":
        return "unsupported_action"
    if request.transaction_policy != "verified":
        return "transaction_policy_mismatch"
    if set(request.arguments) != {"scene_name"}:
        return "invalid_arguments"
    scene_name = request.arguments.get("scene_name")
    if not isinstance(request.target, str) or not isinstance(scene_name, str):
        return "invalid_target"
    if request.target != scene_name:
        return "target_mismatch"
    try:
        _strict_scene(request.target, max_chars)
    except ValueError:
        return "invalid_target"
    return None


def _strict_scene(value: object, max_chars: int) -> str:
    scene = _required_text(value, "scene_name")
    if len(scene) > max_chars or _has_control(scene):
        raise ValueError("scene_name is invalid")
    return scene


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _reason_from_exception(exc: BaseException) -> str:
    if isinstance(exc, OBSProtocolError):
        return exc.reason_code
    if isinstance(exc, asyncio.TimeoutError):
        return "obs_timeout"
    if isinstance(exc, (OSError, ConnectionError)):
        return "obs_disconnected"
    return "obs_unknown"


def _action_result(
    request: ActionRequest,
    started_at: datetime,
    status: ActionStatus,
    error_code: str | None,
    *,
    data: Mapping[str, Any] | None = None,
) -> ActionResult:
    return ActionResult(
        schema_version=1,
        action_id=request.action_id,
        status=status,
        started_at=started_at,
        completed_at=_now(),
        verified=False,
        verification_source=None,
        result_data=dict(data or {}),
        error_code=error_code,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
