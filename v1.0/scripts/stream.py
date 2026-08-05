"""Stream mode entry — Mai đọc chat YouTube + Discord + response qua TTS.

Wire full stack cho livestream:
- LLM (Gemma 12B qua llama-server) — user phải start trước
- Emotion (mood engine + appraisal + drift detector) — luôn bật
- TTS VieNeu (audio) — optional --tts
- Memory (semantic + working) — optional --memory (nạp bge-m3, ~2GB CPU RAM)
- YouTube chat scraper — optional --youtube VIDEO_ID
- Discord bot — optional --discord (cần env DISCORD_BOT_TOKEN)
- Dashboard realtime metrics — optional --dashboard

Chạy:
    # chỉ YouTube + TTS:
    .\\venv\\Scripts\\python.exe scripts\\stream.py --youtube abc123XYZ --tts

    # đủ bộ:
    .\\venv\\Scripts\\python.exe scripts\\stream.py \\
        --youtube abc123XYZ --discord --tts --memory --dashboard

Ctrl+C để stop gracefully (đóng bot, cancel task, save state).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    args = _parse_args()

    if not args.youtube and not args.discord:
        print("❌ Cần ít nhất 1 nguồn: --youtube VIDEO_ID hoặc --discord")
        sys.exit(2)

    from orchestrator.config_loader import ConfigLoader
    from orchestrator.fallback_manager import FallbackManager
    from orchestrator.metrics_collector import MetricsCollector

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    metrics = MetricsCollector()

    # ---------- LLM stack ----------
    print("🧠 Nạp LLM stack (kiểm tra llama-server 127.0.0.1:8080)...")
    from services.llm.canned_response import CannedResponder
    from services.llm.llama_cpp_llm import LlamaCppLLMService
    from services.llm.llm_turn import LLMTurnRunner
    from services.llm.prompt_manager import PromptManager

    llm_svc = LlamaCppLLMService.from_loader(loader)
    await llm_svc.start()
    health = await llm_svc.health_check()
    if not health.is_ok:
        print(f"❌ llama-server chưa chạy: {health.message}")
        print("   Start server trước (xem README).")
        await llm_svc.stop()
        sys.exit(3)
    print(f"✅ llama-server OK ({health.message})")

    pm = PromptManager.from_loader(loader)
    canned = CannedResponder.from_loader(loader)
    fb = FallbackManager()

    # ---------- Emotion (Phase 7.5) ----------
    print("🎭 Nạp Emotion engine...")
    from orchestrator.emotion_orchestrator import EmotionOrchestrator
    from services.qc.drift_detector import DriftDetector

    emotion = EmotionOrchestrator.from_loader(loader, memory=None)  # memory wire sau
    drift = DriftDetector.from_loader(loader)

    # ---------- Memory (optional) ----------
    memory = None
    memory_extractor = None
    if args.memory:
        print("📚 Nạp Memory (bge-m3 CPU, có thể mất 30-60s lần đầu)...")
        from orchestrator.migration_runner import MigrationRunner
        from services.memory.embedder import BgeM3Embedder
        from services.memory.extractor import MemoryExtractor
        from services.memory.memory_fallback import MemoryFallbackManager
        from services.memory.semantic_memory import SemanticMemoryService
        from services.memory.sqlite_vec_store import SqliteVecStore
        from services.memory.working_memory import WorkingMemoryService

        db_path = loader.get("system", "paths.db_file", "data/mai.db")
        MigrationRunner.from_config(loader).initialize()
        store = SqliteVecStore(db_path=db_path)
        embedder = BgeM3Embedder.from_loader(loader)
        semantic = SemanticMemoryService(store=store, embedder=embedder)
        working = WorkingMemoryService.from_loader(loader)
        memory = MemoryFallbackManager(primary=semantic, fallback=working)
        await memory.start()
        memory_extractor = MemoryExtractor()
        # Rewire emotion để modifier dùng được memory (repeated_shutdown, first_time)
        emotion._modifiers._memory = memory
        print("✅ Memory OK")

    # ---------- Runner (wire tất cả) ----------
    runner = LLMTurnRunner.from_loader(
        loader, llm_svc, pm, fb, canned,
        on_token=_print_token, metrics=metrics,
        memory=memory, memory_extractor=memory_extractor,
        emotion=emotion, drift_detector=drift,
    )

    # ---------- TTS (optional) ----------
    tts_svc = None
    audio_player = None
    tts_pipeline = None
    speak_callback = None
    if args.tts:
        print("🎙️ Nạp VieNeu-TTS (10-15s)...")
        from services.tts.audio_player import AudioPlayer
        from services.tts.subtitle_fallback import SubtitleFallbackService
        from services.tts.tts_pipeline import TTSPipeline
        from services.tts.vieneu_service import VieNeuTtsService

        tts_svc = VieNeuTtsService.from_loader(loader)
        try:
            await tts_svc.start()
        except Exception as e:
            print(f"❌ VieNeu-TTS load thất bại: {e}. Chạy tiếp KHÔNG TTS.")
            tts_svc = None
        else:
            audio_player = AudioPlayer(sample_rate=tts_svc.sample_rate)
            await audio_player.start()
            subtitle = SubtitleFallbackService(
                on_subtitle=lambda rid, txt: print(f"   [SUBTITLE] {txt}"),
            )
            tts_pipeline = TTSPipeline(
                primary=tts_svc, subtitle=subtitle, player=audio_player,
                fallback=FallbackManager(), metrics=metrics,
            )

            async def _speak(req_id: str, text: str) -> None:
                await tts_pipeline.speak(req_id, text)

            speak_callback = _speak
            print("✅ TTS OK")

    # ---------- Input sources ----------
    sources: list = []
    if args.youtube:
        print(f"📺 Kết nối YouTube live {args.youtube}...")
        from services.input.youtube_chat import YouTubeChatService
        sources.append(YouTubeChatService(
            video_id=args.youtube,
            poll_interval_s=float(loader.get(
                "chat_sources", "youtube.poll_interval_s", 2.0,
            )),
        ))
    if args.discord:
        print("💬 Kết nối Discord bot...")
        from services.input.discord_chat import DiscordChatService
        sources.append(DiscordChatService.from_loader(loader))

    # ---------- Router ----------
    from services.input.chat_router import ChatRouter
    router = ChatRouter(
        sources=sources, emotion=emotion, runner=runner,
        speak=speak_callback,
    )

    # ---------- Dashboard (optional) ----------
    dashboard_task = None
    if args.dashboard:
        from dashboard.dashboard_server import DashboardServer
        ds = DashboardServer(metrics=metrics)
        dashboard_task = asyncio.create_task(ds.serve(), name="dashboard")
        print("📊 Dashboard: http://127.0.0.1:7860")

    # ---------- Run ----------
    print("\n" + "=" * 60)
    print("Mai đang online. Ctrl+C để stop.")
    print("=" * 60 + "\n")
    await router.start()

    try:
        # Idle await forever cho tới Ctrl+C
        stop_event = asyncio.Event()
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n🛑 Đang tắt...")
    finally:
        await router.stop()
        if tts_pipeline is not None:
            try:
                await audio_player.stop()
                await tts_svc.stop()
            except Exception:
                pass
        if memory is not None:
            try:
                await memory.stop()
            except Exception:
                pass
        if dashboard_task is not None:
            dashboard_task.cancel()
            with contextlib.suppress(Exception):
                await dashboard_task
        await llm_svc.stop()
        print("👋 Bye.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mai stream mode (Platform.D)")
    p.add_argument("--youtube", metavar="VIDEO_ID",
                   help="Video ID YouTube live để scrape chat")
    p.add_argument("--discord", action="store_true",
                   help="Bật Discord bot (cần env DISCORD_BOT_TOKEN)")
    p.add_argument("--tts", action="store_true", help="Phát audio VieNeu")
    p.add_argument("--memory", action="store_true",
                   help="Bật semantic memory (nạp bge-m3, ~2GB CPU)")
    p.add_argument("--dashboard", action="store_true",
                   help="Chạy dashboard http://127.0.0.1:7860")
    return p.parse_args()


def _print_token(t: str) -> None:
    print(t, end="", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bye.")
