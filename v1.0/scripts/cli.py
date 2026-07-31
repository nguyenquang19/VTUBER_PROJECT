"""CLI input mode cho Mai (ARCHITECTURE 11.2, milestone 1.E).

Chạy full stack Phase 1: PromptManager (1.C) + LlamaCppLLMService (1.B) + parser
(1.D) + LLM fallback chain (canned theo mood, 1.E). Harness để duyệt persona.

Cần server chạy trước (--reasoning off). Xem scripts/chat_test.py docstring.

Chạy:
    # tương tác:
    .\\venv\\Scripts\\python.exe scripts\\cli.py
    # tương tác + dashboard realtime (TTFT/decode hiện lên khi chat):
    .\\venv\\Scripts\\python.exe scripts\\cli.py --dashboard
    # auto (mỗi arg 1 lượt, giữ ngữ cảnh):
    .\\venv\\Scripts\\python.exe scripts\\cli.py "chào Mai" "mày có phải AI không"

LƯU Ý: dashboard phải chạy CÙNG process với vòng chat thì mới thấy TTFT — vì metrics
là trong-process. Chạy `orchestrator.main` riêng sẽ KHÔNG thấy TTFT (metrics khác nhau).
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


async def _one_turn(runner: LLMTurnRunner, svc: LlamaCppLLMService, user: str) -> None:
    print("Mai: ", end="", flush=True)
    parsed, level = await runner.run_turn(f"cli-{time.time_ns()}", user)
    m = svc.get_metrics()
    tag = "canned" if level > 0 else "primary"
    ttft = m["llm_last_ttft_ms"] or 0.0
    tps = m["llm_last_decode_tps"] or 0.0
    print(f"\n   [{tag}] mood={parsed.mood.dominant()} parse_ok={parsed.ok} "
          f"TTFT={ttft:.0f}ms {tps:.0f}tok/s")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Mai CLI")
    ap.add_argument("prompts", nargs="*", help="câu hỏi auto (mỗi arg 1 lượt)")
    ap.add_argument("--dashboard", action="store_true", help="bật dashboard cùng process")
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
        print(f"❌ llama-server chưa chạy ({health.detail}). Start server trước.")
        await svc.stop()
        return

    dash_task = None
    dash_server = None
    if args.dashboard:
        import uvicorn

        host = loader.get("system", "dashboard.host", "127.0.0.1")
        port = int(loader.get("system", "dashboard.port", 7860))
        dash_server = DashboardServer(metrics=metrics, push_interval_s=0.5)
        dash_server.start_push_loop()
        uv = uvicorn.Server(uvicorn.Config(dash_server.app, host=host, port=port, log_level="warning"))
        dash_task = asyncio.create_task(uv.serve())
        print(f"📊 Dashboard: http://{host}:{port}  (TTFT hiện lên khi chat)")

    print("=" * 64)
    print(f"  Mai CLI — persona v{pm.version} — gõ 'quit' để thoát")
    print("=" * 64)

    try:
        if args.prompts:
            for p in args.prompts:
                print(f"\nBạn: {p}")
                await _one_turn(runner, svc, p)
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
                await _one_turn(runner, svc, user)
    finally:
        if dash_server is not None:
            await dash_server.stop_push_loop()
        if dash_task is not None:
            dash_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dash_task
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
