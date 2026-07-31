"""CLI input mode cho Mai (ARCHITECTURE 11.2, milestone 1.E + wire 4.E).

Chạy full stack:
- Phase 1: PromptManager + LlamaCppLLMService + parser + LLM fallback (canned)
- Phase 4 (khi --tts): ViXttsService + AudioPlayer + SubtitleFallback + TTSPipeline
  → sau mỗi câu Mai nói, đưa `parsed.text` (đã tách mood block) qua TTS pipeline.

Cần server LLM chạy trước (--reasoning off). Xem scripts/chat_test.py docstring.

Chạy:
    # chỉ chat text (không TTS):
    .\\venv\\Scripts\\python.exe scripts\\cli.py
    # + dashboard realtime cùng process:
    .\\venv\\Scripts\\python.exe scripts\\cli.py --dashboard
    # + TTS (viXTTS, cần GPU, nạp model ~10-15s):
    .\\venv\\Scripts\\python.exe scripts\\cli.py --tts
    # đủ bộ (chat + TTS + dashboard):
    .\\venv\\Scripts\\python.exe scripts\\cli.py --tts --dashboard
    # auto (mỗi arg 1 lượt):
    .\\venv\\Scripts\\python.exe scripts\\cli.py --tts "chào Mai"

LƯU Ý: dashboard phải chạy CÙNG process với chat/TTS thì mới thấy metrics —
metrics là trong-process (chạy `orchestrator.main` riêng sẽ KHÔNG thấy).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

from dashboard.dashboard_server import DashboardServer  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
from orchestrator.metrics_collector import MetricsCollector  # noqa: E402
from services.llm.canned_response import CannedResponder  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.llm_turn import LLMTurnRunner  # noqa: E402
from services.llm.prompt_manager import PromptManager  # noqa: E402


def _print_token(t: str) -> None:
    print(t, end="", flush=True)


async def _one_turn(
    runner: LLMTurnRunner,
    svc: LlamaCppLLMService,
    user: str,
    pipeline=None,
    player=None,
) -> None:
    print("Mai: ", end="", flush=True)
    req_id = f"cli-{time.time_ns()}"
    parsed, level = await runner.run_turn(req_id, user)
    m = svc.get_metrics()
    tag = "canned" if level > 0 else "primary"
    ttft = m["llm_last_ttft_ms"] or 0.0
    tps = m["llm_last_decode_tps"] or 0.0
    line = (f"\n   [{tag}] mood={parsed.mood.dominant()} parse_ok={parsed.ok} "
            f"TTFT={ttft:.0f}ms {tps:.0f}tok/s")
    print(line)

    if pipeline is not None and parsed.text.strip():
        # Chỉ đưa CÂU Mai nói (đã tách mood block ở parser 1.D) vào TTS
        t_speak = time.perf_counter()
        await pipeline.speak(f"tts-{req_id}", parsed.text)
        pipe_ttfa = pipeline.get_metrics().get("tts_pipeline_last_ttfa_ms")
        # svc-level TTFA (từ inference_stream tới first chunk yielded — spike day2 ~450ms)
        svc_ttfa = None
        primary = getattr(pipeline, "_primary", None)
        if primary is not None and hasattr(primary, "get_metrics"):
            svc_ttfa = primary.get_metrics().get("tts_last_ttfa_ms")
        enq_ms = (time.perf_counter() - t_speak) * 1000
        parts = []
        if pipe_ttfa is not None:
            parts.append(f"pipeline_TTFA={pipe_ttfa:.0f}ms")
        if svc_ttfa is not None:
            parts.append(f"svc_TTFA={svc_ttfa:.0f}ms")
        parts.append(f"total_enq={enq_ms:.0f}ms")
        print(f"   [TTS] {' '.join(parts)}")
        # Chờ player phát xong trước khi cho user gõ tiếp (tránh nói đè)
        if player is not None:
            deadline = asyncio.get_event_loop().time() + 30.0
            while player.is_playing or player.get_metrics()["audio_queue_size"] > 0:
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.05)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Mai CLI")
    ap.add_argument("prompts", nargs="*", help="câu hỏi auto (mỗi arg 1 lượt)")
    ap.add_argument("--dashboard", action="store_true", help="bật dashboard cùng process")
    ap.add_argument("--tts", action="store_true", help="bật TTS viXTTS (cần GPU)")
    args = ap.parse_args()

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    metrics = MetricsCollector()
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    pm = PromptManager.from_loader(loader)
    fb = FallbackManager()
    canned = CannedResponder.from_loader(loader)
    runner = LLMTurnRunner.from_loader(
        loader, svc, pm, fb, canned, on_token=_print_token, metrics=metrics
    )

    health = await svc.health_check()
    if not health.is_ok:
        print(f"❌ llama-server chưa chạy ({health.message}). Start server trước.")
        await svc.stop()
        return

    # ---------- TTS wiring (Phase 4) ----------
    tts_svc = None
    audio_player = None
    tts_pipeline = None
    if args.tts:
        from services.tts.audio_player import AudioPlayer
        from services.tts.subtitle_fallback import SubtitleFallbackService
        from services.tts.tts_pipeline import TTSPipeline
        from services.tts.vixtts_service import ViXttsService

        print("🎙️ Đang nạp viXTTS (10-15s)...")
        t0 = time.perf_counter()
        tts_svc = ViXttsService.from_loader(loader)
        try:
            await tts_svc.start()
        except Exception as e:
            print(f"❌ viXTTS load thất bại: {e}. Chạy tiếp KHÔNG TTS.")
            tts_svc = None
        else:
            audio_player = AudioPlayer(sample_rate=tts_svc.sample_rate)
            await audio_player.start()
            subtitle = SubtitleFallbackService(
                on_subtitle=lambda rid, txt: print(f"   [SUBTITLE] {txt}")
            )
            tts_pipeline = TTSPipeline(
                primary=tts_svc,
                subtitle=subtitle,
                player=audio_player,
                fallback=FallbackManager(),   # chain riêng cho TTS
                metrics=metrics,
            )
            print(f"🎙️ TTS sẵn sàng ({(time.perf_counter() - t0):.1f}s, "
                  f"sample_rate={tts_svc.sample_rate})")

    # ---------- dashboard ----------
    dash_task = None
    dash_server = None
    if args.dashboard:
        import uvicorn

        host = loader.get("system", "dashboard.host", "127.0.0.1")
        port = int(loader.get("system", "dashboard.port", 7860))
        dash_server = DashboardServer(
            metrics=metrics,
            tts_service=tts_svc,
            audio_player=audio_player,
            tts_pipeline=tts_pipeline,
            push_interval_s=0.5,
        )
        dash_server.start_push_loop()
        uv = uvicorn.Server(uvicorn.Config(dash_server.app, host=host, port=port, log_level="warning"))
        dash_task = asyncio.create_task(uv.serve())
        print(f"📊 Dashboard: http://{host}:{port}")

    print("=" * 64)
    print(f"  Mai CLI — persona v{pm.version} — gõ 'quit' để thoát"
          f"{' — TTS ON' if tts_pipeline else ''}")
    print("=" * 64)

    try:
        if args.prompts:
            for p in args.prompts:
                print(f"\nBạn: {p}")
                await _one_turn(runner, svc, p, tts_pipeline, audio_player)
        else:
            while True:
                try:
                    user = (await asyncio.to_thread(input, "\nBạn: ")).strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nBye!")
                    break
                if not user:
                    continue
                if user.lower() in ("quit", "exit", "thoat", "thoát"):
                    print("Bye!")
                    break
                await _one_turn(runner, svc, user, tts_pipeline, audio_player)
    finally:
        if dash_server is not None:
            await dash_server.stop_push_loop()
        if dash_task is not None:
            dash_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dash_task
        if audio_player is not None:
            await audio_player.stop()
        if tts_svc is not None:
            await tts_svc.stop()
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
