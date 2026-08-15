"""Smoke-test kết nối VTube Studio — chạy tay trước khi live.

Dùng đúng service production (services/animation), KHÔNG dựng lại logic riêng.
Kết nối VTS, liệt kê hotkey, trigger thử từng mood dominant qua mood_hotkeys.

Chạy:
    python scripts/vts_smoke.py

Yêu cầu: VTube Studio đang mở, Settings → Start API (port 8001).
Lần đầu sẽ hiện popup trong VTS để bấm Allow (token lưu vào vts_token.txt).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interfaces.animation import AnimationCommand, MoodState  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.animation.vts_service import VTSAnimationService  # noqa: E402


async def main() -> None:
    loader = ConfigLoader()
    svc = VTSAnimationService.from_loader(loader, enabled=True)
    await svc.start()

    health = await svc.health_check()
    print("health:", health.state.value, health.details)
    if not svc._transport.connected:
        print("VTS chưa kết nối — kiểm tra Start API + port trong config/animation.yaml")
        await svc.stop()
        return

    print("hotkeys model đang mở:", list(svc._transport.hotkeys))
    for dominant in ("vui", "buon", "buc", "bon_chon", "nguong"):
        mood = MoodState(**{dominant: 9})
        print(f"express dominant={dominant} ->", end=" ")
        await svc.express(AnimationCommand(command_type="express", mood=mood))
        # retrigger_on_same_mood=false: reset để thấy từng cái trigger
        svc._last_dominant = None
        print("ok")
        await asyncio.sleep(1.5)

    print("metrics:", svc.get_metrics())
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
