"""VTSAnimationService — implement interfaces/animation.py cho VTube Studio.

Ăn khớp Mai:
- Service contract: start/stop/health_check/get_metrics (interfaces/base.py).
- AnimationService: express(AnimationCommand), sync_with_audio(AudioChunk).
- Config-over-code: mọi tham số ở config/animation.yaml.
- Feature gate: compose sau `animation_smooth` (stream_runtime).
- Observable: get_metrics() có counter.
- Fail-safe: mọi lỗi VTS được nuốt + log; KHÔNG bao giờ làm chết turn chính
  (giống ràng buộc memory: lỗi phụ không giết delivery).

KHÔNG lấy logic ngoài: bỏ keyword-emotion, edge-tts, voicemeeter, retry-3 của
prototype. Nguồn cảm xúc duy nhất là MoodState.dominant() do Mai cấp.
"""
from __future__ import annotations

from typing import Any

from interfaces.animation import AnimationCommand, AnimationService, MoodState
from interfaces.base import HealthStatus
from interfaces.tts import AudioChunk
from orchestrator.logger import get_logger
from services.animation.vts_transport import VTSTransport, VTSTransportError


class VTSAnimationService(AnimationService):
    service_id = "animation_vts"

    def __init__(
        self,
        transport: VTSTransport,
        *,
        mood_hotkeys: dict[str, str],
        retrigger_on_same_mood: bool = False,
        enabled: bool = True,
    ) -> None:
        self._transport = transport
        self._mood_hotkeys = {str(k): str(v) for k, v in (mood_hotkeys or {}).items()}
        self._retrigger_same = bool(retrigger_on_same_mood)
        self.enabled = bool(enabled)
        self._running = False
        self._last_dominant: str | None = None
        self._log = get_logger("animation_vts")

        self._expressions_total = 0
        self._triggers_total = 0
        self._skipped_total = 0
        self._errors_total = 0

    @classmethod
    def from_loader(cls, loader: Any, *, enabled: bool = True) -> "VTSAnimationService":
        cfg = loader.get("animation", "animation", {}) or {}
        transport = VTSTransport(
            host=str(cfg.get("host", "localhost")),
            port=int(cfg.get("port", 8001)),
            plugin_name=str(cfg.get("plugin_name", "Mai")),
            plugin_developer=str(cfg.get("plugin_developer", "Duc")),
            token_file=str(cfg.get("token_file", "vts_token.txt")),
        )
        return cls(
            transport,
            mood_hotkeys=cfg.get("mood_hotkeys", {}) or {},
            retrigger_on_same_mood=bool(cfg.get("retrigger_on_same_mood", False)),
            enabled=enabled,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        self._running = True
        if not self.enabled:
            return
        try:
            await self._transport.connect()
            self._log.info("animation_vts_ready", hotkeys=list(self._transport.hotkeys))
        except VTSTransportError as e:
            # Fail-safe: VTS không mở/không nối được → chạy degraded, không chết runtime.
            self._errors_total += 1
            self._log.warning("animation_vts_connect_failed", error=str(e))

    async def stop(self) -> None:
        self._running = False
        try:
            await self._transport.close()
        except Exception as e:  # pragma: no cover - defensive
            self._log.warning("animation_vts_close_failed", error=str(e))

    async def health_check(self) -> HealthStatus:
        if not self._running:
            return HealthStatus.stopped(self.service_id)
        if not self.enabled:
            return HealthStatus.healthy(self.service_id, enabled=False)
        if not self._transport.connected:
            # Không nối được VTS = degraded, KHÔNG unhealthy (animation là phụ).
            return HealthStatus.degraded(
                self.service_id, "VTS chưa kết nối", triggers=self._triggers_total,
            )
        return HealthStatus.healthy(
            self.service_id, triggers=self._triggers_total,
            hotkeys=len(self._transport.hotkeys),
        )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "animation_expressions_total": self._expressions_total,
            "animation_triggers_total": self._triggers_total,
            "animation_skipped_total": self._skipped_total,
            "animation_errors_total": self._errors_total,
            "animation_connected": self._transport.connected,
        }

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    # ---------- AnimationService ----------

    async def express(self, command: AnimationCommand) -> None:
        """Map mood trội → hotkey. Gọi sau DELIVERED. Fail-safe tuyệt đối."""
        if not self.enabled or not self._running:
            return
        self._expressions_total += 1
        mood = command.mood or MoodState()
        dominant = mood.dominant()
        if dominant == "neutral":
            self._skipped_total += 1
            return
        if not self._retrigger_same and dominant == self._last_dominant:
            self._skipped_total += 1
            return
        hotkey = self._mood_hotkeys.get(dominant)
        if not hotkey:
            self._skipped_total += 1
            return
        try:
            if await self._transport.trigger(hotkey):
                self._triggers_total += 1
                self._last_dominant = dominant
            else:
                self._skipped_total += 1
                self._log.warning("animation_hotkey_missing", hotkey=hotkey)
        except Exception as e:
            self._errors_total += 1
            self._log.warning("animation_trigger_failed", error=str(e))

    async def sync_with_audio(self, audio_chunk: AudioChunk) -> None:
        """Lip-sync trong VTS lấy từ audio input (Voicemeeter), KHÔNG qua API.

        Contract yêu cầu method này tồn tại; ở đây là no-op có chủ đích. Nhép
        miệng do model tự xử theo tín hiệu âm thanh, không phải side-effect runtime.
        """
        return
