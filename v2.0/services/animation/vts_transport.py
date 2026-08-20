"""VTube Studio websocket transport — chỉ giao thức API, không business logic.

Config-injected (host/port/token/plugin từ animation.yaml, không hardcode).
Service layer (vts_service.py) mới gọi lớp này; tự nó không biết gì về mood/turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from orchestrator.credential_contract import (
    CredentialContractError,
    read_optional_secret_file,
    validate_secret_file_reference,
    validate_secret_value,
    write_secret_file_atomic,
)


API_NAME = "VTubeStudioPublicAPI"
API_VERSION = "1.0"


class VTSTransportError(Exception):
    pass


class VTSTransport:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        plugin_name: str,
        plugin_developer: str,
        token_file: str,
    ) -> None:
        token_file = validate_secret_file_reference(token_file, "token_file")
        self._url = f"ws://{host}:{port}"
        self._plugin_name = plugin_name
        self._plugin_developer = plugin_developer
        self._token_file = token_file
        self._ws: Any = None
        self._token: str | None = None
        self._hotkeys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def hotkeys(self) -> dict[str, str]:
        return dict(self._hotkeys)

    async def connect(self) -> None:
        try:
            import websockets
        except ImportError as e:
            raise VTSTransportError("websockets chưa cài; không thể kết nối VTS") from e
        try:
            self._ws = await websockets.connect(self._url)
        except Exception as e:
            raise VTSTransportError(f"không nối được VTS ở {self._url}: {e}") from e
        try:
            await self._authenticate()
            await self.reload_hotkeys()
        except BaseException:
            with contextlib.suppress(BaseException):
                await self.close()
            raise

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    async def _authenticate(self) -> None:
        try:
            self._token = read_optional_secret_file(self._token_file)
        except CredentialContractError as exc:
            raise VTSTransportError(exc.reason_code) from exc

        if not self._token:
            resp = await self._send("token", "AuthenticationTokenRequest", {
                "pluginName": self._plugin_name,
                "pluginDeveloper": self._plugin_developer,
            })
            try:
                self._token = validate_secret_value(
                    resp.get("data", {}).get("authenticationToken"),
                    source="vts_authentication_token",
                )
                write_secret_file_atomic(self._token_file, self._token)
            except CredentialContractError as exc:
                self._token = None
                raise VTSTransportError(exc.reason_code) from exc

        resp = await self._send("auth", "AuthenticationRequest", {
            "pluginName": self._plugin_name,
            "pluginDeveloper": self._plugin_developer,
            "authenticationToken": self._token,
        })
        if not resp.get("data", {}).get("authenticated"):
            self._token = None
            with contextlib.suppress(OSError):
                Path(self._token_file).unlink(missing_ok=True)
            raise VTSTransportError("vts_authentication_failed")

    async def reload_hotkeys(self) -> dict[str, str]:
        resp = await self._send("get_hotkeys", "HotkeysInCurrentModelRequest")
        available = resp.get("data", {}).get("availableHotkeys", [])
        self._hotkeys = {hk["name"]: hk["hotkeyID"] for hk in available}
        return dict(self._hotkeys)

    async def trigger(self, hotkey_name: str) -> bool:
        hotkey_id = self._hotkeys.get(hotkey_name)
        if not hotkey_id:
            return False
        await self._send("hotkey", "HotkeyTriggerRequest", {"hotkeyID": hotkey_id})
        return True

    async def _send(
        self, request_id: str, message_type: str, data: dict | None = None,
    ) -> dict:
        if self._ws is None:
            raise VTSTransportError("chưa connect()")
        payload = {
            "apiName": API_NAME,
            "apiVersion": API_VERSION,
            "requestID": request_id,
            "messageType": message_type,
        }
        if data is not None:
            payload["data"] = data
        try:
            async with self._lock:
                await self._ws.send(json.dumps(payload))
                raw = await self._ws.recv()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise VTSTransportError("vts_transport_error") from None
        try:
            resp = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise VTSTransportError("vts_invalid_response") from None
        if not isinstance(resp, dict):
            raise VTSTransportError("vts_invalid_response")
        if resp.get("messageType") == "APIError":
            raise VTSTransportError("vts_api_error")
        return resp
