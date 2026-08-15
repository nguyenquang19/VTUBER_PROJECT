"""VTube Studio websocket transport — chỉ giao thức API, không business logic.

Config-injected (host/port/token/plugin từ animation.yaml, không hardcode).
Service layer (vts_service.py) mới gọi lớp này; tự nó không biết gì về mood/turn.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any


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
        await self._authenticate()
        await self.reload_hotkeys()

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    async def _authenticate(self) -> None:
        if os.path.exists(self._token_file):
            with open(self._token_file, "r", encoding="utf-8") as f:
                self._token = f.read().strip() or None

        if not self._token:
            resp = await self._send("token", "AuthenticationTokenRequest", {
                "pluginName": self._plugin_name,
                "pluginDeveloper": self._plugin_developer,
            })
            self._token = resp.get("data", {}).get("authenticationToken")
            if not self._token:
                raise VTSTransportError(f"VTS từ chối cấp token: {resp.get('data')}")
            with open(self._token_file, "w", encoding="utf-8") as f:
                f.write(self._token)

        resp = await self._send("auth", "AuthenticationRequest", {
            "pluginName": self._plugin_name,
            "pluginDeveloper": self._plugin_developer,
            "authenticationToken": self._token,
        })
        if not resp.get("data", {}).get("authenticated"):
            self._token = None
            if os.path.exists(self._token_file):
                os.remove(self._token_file)
            raise VTSTransportError(f"auth thất bại: {resp.get('data')}")

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
        async with self._lock:
            await self._ws.send(json.dumps(payload))
            raw = await self._ws.recv()
        resp = json.loads(raw)
        if resp.get("messageType") == "APIError":
            raise VTSTransportError(f"VTS APIError: {resp.get('data')}")
        return resp
