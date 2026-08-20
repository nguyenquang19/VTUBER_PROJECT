"""Emergency stop hotkey Ctrl+Shift+X.

Windows: `keyboard` lib cần chạy Python với quyền Administrator để hook phím
toàn cục (CLAUDE.md Section 2). Nếu không có quyền / import fail → degrade:
hotkey không bind nhưng trigger vẫn gọi được qua dashboard / code (DoD vẫn đạt
vì "emergency stop → PAUSED" test được qua trigger programmatic).

Callback là async (gọi state_machine.emergency_stop()). Vì `keyboard` chạy
trên thread riêng, ta schedule callback vào event loop chính bằng
run_coroutine_threadsafe.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from orchestrator.logger import get_logger

StopCallback = Callable[[], Awaitable[None]]


class EmergencyStop:
    def __init__(
        self,
        callback: StopCallback,
        hotkey: str = "ctrl+shift+x",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._callback = callback
        self._hotkey = hotkey
        self._loop = loop
        self._log = get_logger("emergency_stop")
        self._bound = False
        self._trigger_count = 0
        self._keyboard = None  # module `keyboard`, import lazy

    @classmethod
    def from_config(cls, loader, callback: StopCallback, loop=None) -> EmergencyStop:
        return cls(
            callback=callback,
            hotkey=loader.get("system", "emergency_stop.hotkey", "ctrl+shift+x"),
            loop=loop,
        )

    async def trigger(self) -> None:
        """Kích hoạt emergency stop (nguồn: hotkey, dashboard, hoặc code)."""
        self._trigger_count += 1
        self._log.warning("emergency_stop_triggered", count=self._trigger_count)
        await self._callback()

    def _on_hotkey(self) -> None:
        """Chạy trên thread của `keyboard` → đẩy coroutine về loop chính."""
        loop = self._loop or asyncio.get_event_loop()
        try:
            asyncio.run_coroutine_threadsafe(self.trigger(), loop)
        except Exception as e:
            self._log.error("emergency_hotkey_dispatch_failed", error=str(e))

    def bind(self) -> bool:
        """Bind hotkey toàn cục. Trả True nếu bind được.

        Fail (không admin / lib thiếu / môi trường không có bàn phím) → False,
        log warning, KHÔNG raise — hệ thống vẫn chạy, dùng nút dashboard thay.
        """
        if self._bound:
            return True
        try:
            import keyboard  # import lazy: môi trường test/CI có thể không có
            self._keyboard = keyboard
            keyboard.add_hotkey(self._hotkey, self._on_hotkey)
            self._bound = True
            self._log.info("emergency_hotkey_bound", hotkey=self._hotkey)
            return True
        except Exception as e:
            self._log.warning(
                "emergency_hotkey_bind_failed",
                hotkey=self._hotkey,
                error=str(e),
                hint="cần chạy Python với quyền Administrator trên Windows",
            )
            return False

    def unbind(self) -> None:
        if self._bound and self._keyboard is not None:
            try:
                self._keyboard.remove_hotkey(self._hotkey)
            except Exception:
                pass
        self._bound = False

    @property
    def is_bound(self) -> bool:
        return self._bound

    @property
    def trigger_count(self) -> int:
        return self._trigger_count
