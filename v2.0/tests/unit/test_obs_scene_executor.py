from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest

from interfaces.base import HealthStatus
from interfaces.compatibility import ActionRequest, ActionStatus
from interfaces.external_executor import OBSCommandAck, OBSSceneState, OBSSceneTransportService
from orchestrator.config_loader import ConfigLoader
from services.action.obs_scene import (
    OBSProtocolError,
    OBSSceneConfig,
    OBSSceneExecutor,
    OBSSceneVerifier,
    OBSWebSocketTransport,
)


NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


def _config(**changes: object) -> OBSSceneConfig:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 4455,
        "use_tls": False,
        "password_env": "OBS_WEBSOCKET_PASSWORD",
        "connect_timeout_s": 0.1,
        "request_timeout_s": 0.1,
        "health_timeout_s": 0.2,
        "health_ttl_s": 1.0,
        "max_attempts": 2,
        "retry_backoff_s": 0.0,
        "max_scene_name_chars": 32,
        "max_authority_records": 4,
        "max_evidence_refs": 4,
        "max_message_bytes": 4096,
    }
    values.update(changes)
    return OBSSceneConfig(**values)  # type: ignore[arg-type]


def _request(scene: str = "Main", *, action_id: str = "scene:1") -> ActionRequest:
    return ActionRequest(
        schema_version=1,
        action_id=action_id,
        capability_id="SWITCH_SCENE",
        action_type="SWITCH_SCENE",
        target=scene,
        arguments={"scene_name": scene},
        intention_id=None,
        evidence_refs=("operator:test",),
        idempotency_key=f"key:{action_id}",
        priority=0.0,
        requested_at=NOW,
        transaction_policy="verified",
    )


class FakeOBSTransport(OBSSceneTransportService):
    service_id = "fake_obs"

    def __init__(self, scene: str = "Starting") -> None:
        self.scene = scene
        self.running = False
        self.get_calls = 0
        self.set_calls: list[str] = []
        self.get_failures: deque[Exception] = deque()
        self.set_failure: Exception | None = None
        self.keep_scene_on_set = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id) if self.running else HealthStatus.stopped(self.service_id)

    def get_metrics(self) -> dict[str, int]:
        return {"get_calls": self.get_calls, "set_calls": len(self.set_calls)}

    async def get_current_program_scene(self) -> OBSSceneState:
        self.get_calls += 1
        if self.get_failures:
            raise self.get_failures.popleft()
        return OBSSceneState(self.scene, f"obs:get:{self.get_calls}")

    async def set_current_program_scene(self, scene_name: str) -> OBSCommandAck:
        self.set_calls.append(scene_name)
        if self.set_failure is not None:
            raise self.set_failure
        if not self.keep_scene_on_set:
            self.scene = scene_name
        return OBSCommandAck(
            request_id=f"set-{len(self.set_calls)}",
            accepted=True,
            evidence_ref=f"obs:set:{len(self.set_calls)}",
        )


def test_yaml_declares_disabled_obs_feature_and_strict_config() -> None:
    root = Path(__file__).resolve().parents[2]
    loader = ConfigLoader(root / "config")
    loader.load_all()
    config = OBSSceneConfig.from_loader(loader)
    assert config.port == 4455
    assert config.password_env == "OBS_WEBSOCKET_PASSWORD"
    assert loader.get("features", "features.obs_scene_executor.enabled") is False
    assert loader.get("capabilities", "capabilities.SWITCH_SCENE.mock_only") is False
    assert loader.get("capabilities", "capabilities.SWITCH_SCENE.parameter_schema") == {
        "scene_name": "string",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", True),
        ("use_tls", 1),
        ("max_attempts", 0),
        ("request_timeout_s", float("nan")),
        ("retry_backoff_s", -1),
        ("password_env", " OBS_PASSWORD "),
    ],
)
def test_obs_config_rejects_coercion_and_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


def test_executor_ack_is_unverified_until_independent_scene_query() -> None:
    transport = FakeOBSTransport()
    executor = OBSSceneExecutor(_config(), transport, enabled=True)
    verifier = OBSSceneVerifier(_config(), transport, enabled=True)

    async def scenario() -> None:
        await executor.start()
        await verifier.start()
        result = await executor.execute(_request())
        assert result.status is ActionStatus.SUCCESS
        assert result.verified is False
        assert transport.get_calls == 1
        verified = await verifier.verify(_request(), result)
        assert verified.verified is True
        assert verified.source == "obs_websocket"
        assert transport.get_calls == 2

    asyncio.run(scenario())


def test_verifier_rejects_ack_when_authoritative_scene_mismatches() -> None:
    transport = FakeOBSTransport()
    transport.keep_scene_on_set = True
    executor = OBSSceneExecutor(_config(), transport, enabled=True)
    verifier = OBSSceneVerifier(_config(), transport, enabled=True)

    async def scenario() -> None:
        await executor.start()
        await verifier.start()
        result = await executor.execute(_request())
        verified = await verifier.verify(_request(), result)
        assert result.status is ActionStatus.SUCCESS
        assert verified.verified is False
        assert verified.reason_code == "scene_mismatch"

    asyncio.run(scenario())


def test_executor_retries_only_retryable_read_with_same_action() -> None:
    transport = FakeOBSTransport()
    transport.get_failures.append(OBSProtocolError("obs_disconnected", retryable=True))
    executor = OBSSceneExecutor(_config(max_attempts=2), transport, enabled=True)

    async def scenario() -> None:
        await executor.start()
        result = await executor.execute(_request())
        assert result.status is ActionStatus.SUCCESS

    asyncio.run(scenario())
    assert transport.get_calls == 2
    assert transport.set_calls == ["Main"]
    assert executor.get_metrics()["obs_scene_executor_outcomes"]["retry"] == 1


def test_health_projection_requires_probe_and_expires() -> None:
    now = [10.0]
    transport = FakeOBSTransport()
    executor = OBSSceneExecutor(
        _config(health_ttl_s=2.0), transport, enabled=True, monotonic=lambda: now[0],
    )

    async def scenario() -> None:
        await executor.start()
        assert executor.public_health().is_ok is False
        assert (await executor.health_check()).is_ok is True
        assert executor.public_health().is_ok is True
        now[0] = 13.0
        assert executor.public_health().message == "health_stale"
        executor.set_enabled(False)
        assert executor.public_health().message == "feature_disabled"

    asyncio.run(scenario())


def test_obs_authentication_requires_environment_secret_without_exposing_it() -> None:
    transport = OBSWebSocketTransport(_config(), environ={})
    with pytest.raises(OBSProtocolError) as missing:
        transport._authentication({"challenge": "challenge", "salt": "salt"})
    assert missing.value.reason_code == "obs_credentials_missing"

    authenticated = OBSWebSocketTransport(
        _config(), environ={"OBS_WEBSOCKET_PASSWORD": "top-secret"},
    )._authentication({"challenge": "challenge", "salt": "salt"})
    assert authenticated
    assert "top-secret" not in authenticated


def test_scene_validation_rejects_control_char_before_obs_io() -> None:
    transport = FakeOBSTransport()
    executor = OBSSceneExecutor(_config(), transport, enabled=True)

    async def scenario() -> None:
        await executor.start()
        result = await executor.execute(_request("Main\nInjected"))
        assert result.status is ActionStatus.REJECTED
        assert result.error_code == "invalid_target"

    asyncio.run(scenario())
    assert transport.get_calls == 0
    assert transport.set_calls == []
