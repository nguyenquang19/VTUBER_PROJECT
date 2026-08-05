"""CLI input mode cho Mai (ARCHITECTURE 11.2, milestone 1.E + wire 4.E).

Chạy full stack:
- Phase 1: PromptManager + LlamaCppLLMService + parser + LLM fallback (canned)
- Phase 4 (khi --tts): VieNeuTtsService + AudioPlayer + SubtitleFallback + TTSPipeline
  → LiveSentenceStreamer: câu vừa hoàn tất trong lúc LLM còn stream → TTS synth
    NGAY câu đó (song song với LLM), player phát tuần tự → user nghe âm SỚM,
    không phải chờ LLM in xong hết rồi TTS mới nói.

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
from services.tts.sentence_splitter import LiveSentenceStreamer  # noqa: E402


class _TurnCtx:
    """Container cầu nối giữa on_token sync và pipeline async."""

    streamer: LiveSentenceStreamer | None = None
    loop: asyncio.AbstractEventLoop | None = None
    pipeline = None
    req_id: str = ""
    seq: int = 0
    tasks: list = []
    t_start: float = 0.0
    t_first_dispatch: float | None = None


def _on_token(t: str) -> None:
    """Streaming callback: in ra + đẩy vào LiveSentenceStreamer nếu TTS on."""
    print(t, end="", flush=True)
    if _TurnCtx.streamer is not None:
        _TurnCtx.streamer.push(t)


async def _speak_and_log(pipeline, req_id: str, sent: str) -> None:
    try:
        await pipeline.speak(req_id, sent)
    except Exception as e:
        # In lỗi thấy được — nếu không catch, asyncio nuốt vì task không được await
        import traceback
        print(f"\n   [TTS ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()


def _on_sentence(sent: str) -> None:
    """Callback từ streamer: gọi pipeline.speak cho câu vừa hoàn tất."""
    if _TurnCtx.pipeline is None or _TurnCtx.loop is None:
        return
    if _TurnCtx.t_first_dispatch is None:
        _TurnCtx.t_first_dispatch = time.perf_counter()
    _TurnCtx.seq += 1
    seq = _TurnCtx.seq
    # Pipeline.speak có async lock nội tại → task 2 chờ task 1 xong synth mới bắt
    # đầu. Player vẫn phát song song, nên câu 1 audio chảy trong khi câu 2 synth.
    task = _TurnCtx.loop.create_task(
        _speak_and_log(_TurnCtx.pipeline, f"{_TurnCtx.req_id}#s{seq}", sent)
    )
    _TurnCtx.tasks.append(task)


async def _one_turn(
    runner: LLMTurnRunner,
    svc: LlamaCppLLMService,
    user: str,
    pipeline=None,
    player=None,
):
    """Chạy 1 lượt. Trả ParsedResponse để caller check .continuation cho auto-continue."""
    print("Mai: ", end="", flush=True)
    req_id = f"cli-{time.time_ns()}"

    # Reset ngữ cảnh cho streamer
    _TurnCtx.streamer = LiveSentenceStreamer(_on_sentence) if pipeline is not None else None
    _TurnCtx.loop = asyncio.get_running_loop() if pipeline is not None else None
    _TurnCtx.pipeline = pipeline
    _TurnCtx.req_id = req_id
    _TurnCtx.seq = 0
    _TurnCtx.tasks = []
    _TurnCtx.t_start = time.perf_counter()
    _TurnCtx.t_first_dispatch = None

    parsed, level = await runner.run_turn(req_id, user)

    # Streamer close: flush câu chưa hoàn tất (nếu chưa cắt tại mood block)
    if _TurnCtx.streamer is not None:
        _TurnCtx.streamer.close()

    m = svc.get_metrics()
    tag = "canned" if level > 0 else "primary"
    ttft = m["llm_last_ttft_ms"] or 0.0
    tps = m["llm_last_decode_tps"] or 0.0
    print(f"\n   [{tag}] mood={parsed.mood.dominant()} parse_ok={parsed.ok} "
          f"TTFT={ttft:.0f}ms {tps:.0f}tok/s")

    # Đợi tất cả TTS task xong + player drain
    if pipeline is not None and _TurnCtx.tasks:
        with contextlib.suppress(Exception):
            await asyncio.gather(*_TurnCtx.tasks, return_exceptions=True)

        # Thống kê thời gian tới lần dispatch đầu (câu 1 sẵn sàng cho TTS)
        first_dispatch_ms = None
        if _TurnCtx.t_first_dispatch is not None:
            first_dispatch_ms = (_TurnCtx.t_first_dispatch - _TurnCtx.t_start) * 1000
        pipe_ttfa = pipeline.get_metrics().get("tts_pipeline_last_ttfa_ms")
        svc_ttfa = None
        primary = getattr(pipeline, "_primary", None)
        if primary is not None and hasattr(primary, "get_metrics"):
            svc_ttfa = primary.get_metrics().get("tts_last_ttfa_ms")

        parts = []
        if first_dispatch_ms is not None:
            parts.append(f"1st_sent@{first_dispatch_ms:.0f}ms")
        if pipe_ttfa is not None:
            parts.append(f"pipeline_TTFA={pipe_ttfa:.0f}ms")
        if svc_ttfa is not None:
            parts.append(f"svc_TTFA={svc_ttfa:.0f}ms")
        parts.append(f"sentences={_TurnCtx.seq}")
        print(f"   [TTS] {' '.join(parts)}")

        # Chờ player phát xong trước khi cho user gõ tiếp (tránh nói đè)
        if player is not None:
            deadline = asyncio.get_event_loop().time() + 60.0
            while player.is_playing or player.get_metrics()["audio_queue_size"] > 0:
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.05)

    return parsed


async def _turn_with_continue(
    runner: LLMTurnRunner,
    svc: LlamaCppLLMService,
    user: str,
    pipeline=None,
    player=None,
    max_continue: int = 0,
) -> None:
    """1 lượt + tự nói tiếp nếu Mai xuất `còn nữa: có` (tối đa max_continue lần).

    Prompt "nói tiếp" là user turn tổng hợp — Mai xem nó như tín hiệu "kể tiếp đi".
    History có ghi lại (Mai có thể nhìn lại như 1 turn thường).
    """
    parsed = await _one_turn(runner, svc, user, pipeline, player)
    count = 0
    while parsed is not None and parsed.continuation and count < max_continue:
        count += 1
        print(f"\n(auto-continue #{count})")
        parsed = await _one_turn(runner, svc, "(nói tiếp đi)", pipeline, player)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Mai CLI")
    ap.add_argument("prompts", nargs="*", help="câu hỏi auto (mỗi arg 1 lượt)")
    ap.add_argument("--dashboard", action="store_true", help="bật dashboard cùng process")
    ap.add_argument("--tts", action="store_true", help="bật TTS viXTTS (cần GPU)")
    args = ap.parse_args()

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    auto_continue_max = int(loader.get("system", "conversation.auto_continue_max", 0))

    metrics = MetricsCollector()
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    pm = PromptManager.from_loader(loader)
    fb = FallbackManager()
    canned = CannedResponder.from_loader(loader)
    runner = LLMTurnRunner.from_loader(
        loader, svc, pm, fb, canned, on_token=_on_token, metrics=metrics
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
        from services.tts.vieneu_service import VieNeuTtsService

        print("🎙️ Đang nạp VieNeu-TTS (10-15s)...")
        t0 = time.perf_counter()
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
          f"{' — TTS ON (streaming per-sentence)' if tts_pipeline else ''}")
    print("=" * 64)

    try:
        if args.prompts:
            for p in args.prompts:
                print(f"\nBạn: {p}")
                await _turn_with_continue(
                    runner, svc, p, tts_pipeline, audio_player,
                    max_continue=auto_continue_max,
                )
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
                await _turn_with_continue(
                    runner, svc, user, tts_pipeline, audio_player,
                    max_continue=auto_continue_max,
                )
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
