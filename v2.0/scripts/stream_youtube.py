"""Stream mode: YouTube primary. Optional gộp Discord qua --with-discord.

Chạy:
    # Chỉ YouTube:
    python scripts\\stream_youtube.py --video VIDEO_ID --tts

    # Gộp Discord:
    python scripts\\stream_youtube.py --video VIDEO_ID --tts --with-discord

    # Full: memory + dashboard:
    python scripts\\stream_youtube.py --video VIDEO_ID --tts --memory --dashboard --with-discord

Autonomy engine v2 luôn bật (Mai tự nói khi silence), tắt bằng --no-autonomy.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _print_token(t: str) -> None:
    print(t, end="", flush=True)


async def main() -> None:
    args = _parse_args()
    from orchestrator.config_loader import ConfigLoader
    from orchestrator.stream_runtime import (
        StreamRuntimeConfig,
        build_stream_runtime,
        run_stream_runtime,
    )
    from services.input.youtube_chat import YouTubeChatService

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    print(f"📺 YouTube live: {args.video}")
    sources = [YouTubeChatService(
        video_id=args.video,
        poll_interval_s=float(loader.get(
            "chat_sources", "youtube.poll_interval_s", 2.0,
        )),
    )]

    if args.with_discord:
        print("💬 + Discord bot")
        from services.input.discord_chat import DiscordChatService
        sources.append(DiscordChatService.from_loader(loader))

    cfg = StreamRuntimeConfig(
        enable_tts=args.tts,
        enable_memory=args.memory,
        enable_autonomy=not args.no_autonomy,
        enable_dashboard=args.dashboard,
        on_token=_print_token,
    )

    print("🧠 Nạp stack...")
    rt = await build_stream_runtime(loader=loader, sources=sources, cfg=cfg)
    print("=" * 60)
    print("Mai đang online. Ctrl+C để stop.")
    print("=" * 60 + "\n")

    try:
        await run_stream_runtime(rt)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n🛑 Đang tắt...")
    finally:
        print("👋 Bye.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mai stream — YouTube primary")
    p.add_argument("--video", required=True, metavar="VIDEO_ID",
                   help="YouTube live video ID")
    p.add_argument("--tts", action="store_true", help="Phát audio VieNeu")
    p.add_argument("--memory", action="store_true",
                   help="Bật semantic memory (nạp bge-m3, ~2GB CPU)")
    p.add_argument("--dashboard", action="store_true",
                   help="Dashboard http://127.0.0.1:7860")
    p.add_argument("--with-discord", action="store_true",
                   help="Gộp Discord bot (cần env DISCORD_BOT_TOKEN)")
    p.add_argument("--no-autonomy", action="store_true",
                   help="Tắt autonomy engine (Mai tự nói khi silence)")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye.")
