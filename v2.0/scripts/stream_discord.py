"""Stream mode: Discord primary. Optional gộp YouTube qua --with-youtube VID.

Chạy:
    # Chỉ Discord (cần env DISCORD_BOT_TOKEN + channel_ids config):
    python scripts\\stream_discord.py --tts

    # Gộp YouTube:
    python scripts\\stream_discord.py --tts --with-youtube VIDEO_ID

    # Full:
    python scripts\\stream_discord.py --tts --memory --dashboard --with-youtube VIDEO_ID

Autonomy engine v2 luôn bật, tắt bằng --no-autonomy.
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
    from services.input.discord_chat import DiscordChatService

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    print("💬 Discord bot")
    sources = [DiscordChatService.from_loader(loader)]

    if args.with_youtube:
        print(f"📺 + YouTube live: {args.with_youtube}")
        from services.input.youtube_chat import YouTubeChatService
        sources.append(YouTubeChatService(
            video_id=args.with_youtube,
            poll_interval_s=float(loader.get(
                "chat_sources", "youtube.poll_interval_s", 2.0,
            )),
        ))

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
    p = argparse.ArgumentParser(description="Mai stream — Discord primary")
    p.add_argument("--tts", action="store_true", help="Phát audio VieNeu")
    p.add_argument(
        "--memory", action=argparse.BooleanOptionalAction, default=True,
        help="Bật semantic memory (mặc định; --no-memory dùng working-only)",
    )
    p.add_argument("--dashboard", action="store_true",
                   help="Dashboard http://127.0.0.1:7860")
    p.add_argument("--with-youtube", metavar="VIDEO_ID",
                   help="Gộp YouTube live chat")
    p.add_argument("--no-autonomy", action="store_true",
                   help="Tắt autonomy engine (Mai tự nói khi silence)")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye.")
