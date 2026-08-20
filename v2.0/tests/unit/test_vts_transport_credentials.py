"""Credential storage and disclosure boundaries for VTube Studio transport."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.animation.vts_transport import VTSTransport, VTSTransportError


def _transport(token_file: Path) -> VTSTransport:
    return VTSTransport(
        host="localhost",
        port=8001,
        plugin_name="Mai",
        plugin_developer="Duc",
        token_file=str(token_file),
    )


def test_vts_token_file_reference_is_strict() -> None:
    with pytest.raises(ValueError, match="token_file"):
        VTSTransport(
            host="localhost",
            port=8001,
            plugin_name="Mai",
            plugin_developer="Duc",
            token_file=" vts_token.txt ",
        )


async def test_vts_rejects_malformed_stored_token_without_exposing_it(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "vts_token.txt"
    secret = "raw-token\n"
    token_file.write_text(secret, encoding="utf-8")
    transport = _transport(token_file)

    with pytest.raises(VTSTransportError, match="credential_invalid") as raised:
        await transport._authenticate()

    assert secret.strip() not in str(raised.value)
    assert token_file.read_text(encoding="utf-8") == secret


async def test_vts_new_token_is_written_exactly_and_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "credentials" / "vts_token.txt"
    transport = _transport(token_file)
    calls: list[str] = []

    async def send(
        request_id: str, message_type: str, data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append(message_type)
        if message_type == "AuthenticationTokenRequest":
            return {"data": {"authenticationToken": "exact-vts-token"}}
        return {"data": {"authenticated": True}}

    monkeypatch.setattr(transport, "_send", send)
    await transport._authenticate()

    assert calls == ["AuthenticationTokenRequest", "AuthenticationRequest"]
    assert token_file.read_text(encoding="utf-8") == "exact-vts-token"
    assert not token_file.with_suffix(".txt.tmp").exists()


async def test_vts_auth_failure_deletes_rejected_token_with_sanitized_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "vts_token.txt"
    secret = "rejected-vts-token"
    token_file.write_text(secret, encoding="utf-8")
    transport = _transport(token_file)

    async def send(
        request_id: str, message_type: str, data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"data": {"authenticated": False, "debug": secret}}

    monkeypatch.setattr(transport, "_send", send)
    with pytest.raises(VTSTransportError, match="vts_authentication_failed") as raised:
        await transport._authenticate()

    assert secret not in str(raised.value)
    assert not token_file.exists()


async def test_vts_api_error_does_not_include_raw_response_data(tmp_path: Path) -> None:
    secret = "raw-api-secret"

    class _WebSocket:
        async def send(self, _payload: str) -> None:
            return None

        async def recv(self) -> str:
            return json.dumps({
                "messageType": "APIError",
                "data": {"debug": secret},
            })

    transport = _transport(tmp_path / "vts_token.txt")
    transport._ws = _WebSocket()

    with pytest.raises(VTSTransportError, match="vts_api_error") as raised:
        await transport._send("request", "Request")

    assert secret not in str(raised.value)


async def test_vts_send_failure_does_not_echo_authentication_payload(
    tmp_path: Path,
) -> None:
    secret = "vts-auth-secret"

    class _WebSocket:
        async def send(self, payload: str) -> None:
            raise RuntimeError(f"failed payload: {payload}")

        async def recv(self) -> str:
            raise AssertionError("recv must not run")

    transport = _transport(tmp_path / "vts_token.txt")
    transport._ws = _WebSocket()

    with pytest.raises(VTSTransportError, match="vts_transport_error") as raised:
        await transport._send(
            "auth",
            "AuthenticationRequest",
            {"authenticationToken": secret},
        )

    assert secret not in str(raised.value)


async def test_vts_connect_closes_socket_when_credential_gate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    websocket = _WebSocket()

    async def connect(_url: str) -> _WebSocket:
        return websocket

    transport = _transport(tmp_path / "vts_token.txt")

    async def fail_authentication() -> None:
        raise VTSTransportError("credential_invalid")

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    monkeypatch.setattr(transport, "_authenticate", fail_authentication)

    with pytest.raises(VTSTransportError, match="credential_invalid"):
        await transport.connect()

    assert websocket.closed is True
    assert transport.connected is False
