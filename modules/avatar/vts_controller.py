"""
vts_controller.py — [BƯỚC D] Điều khiển VTube Studio (biểu cảm + lip-sync).

CHƯA HOÀN THIỆN — stub. Sẽ code đầy đủ ở Bước D.

Nhiệm vụ:
  - Subscribe AVATAR_EMOTION -> trigger hotkey biểu cảm tương ứng.
  - Lip-sync: route audio TTS -> VTS nhép miệng.

Ghi chú triển khai (Bước D) — LỖI HAY GẶP:
  - Bật API port 8001 trong VTS; xác thực token (popup cho phép lần đầu).
  - Map tên emotion (happy/laugh/pout...) -> tên hotkey đã đặt trong VTS.
  - Emotion lạ -> bỏ qua an toàn (không crash).
  - Watchdog: VTS treo -> tự reconnect; avatar về idle, không đứng hình.
  - Lip-sync đơn giản V1: route desktop audio vào VTS mic-lipsync.
"""
from __future__ import annotations
from core.bus import EventBus
from core.events import Event, EventType


class VTSController:
    def __init__(self, bus: EventBus, host: str = "localhost", port: int = 8001) -> None:
        self.bus = bus
        self.host = host
        self.port = port
        self._hotkeys: dict[str, str] = {}   # emotion -> hotkey id
        bus.subscribe(EventType.AVATAR_EMOTION, self._on_emotion)
        # TODO(BƯỚC D): pyvts connect + auth + lấy danh sách hotkey

    async def _on_emotion(self, event: Event) -> None:
        emotion = event.payload  # str: "happy" | "laugh" | ...
        # TODO(BƯỚC D): tra self._hotkeys[emotion] -> trigger; lạ thì bỏ qua
        raise NotImplementedError("Sẽ hoàn thiện ở Bước D")
