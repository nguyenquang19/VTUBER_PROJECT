"""CLI input mode cho Mai (ARCHITECTURE 11.2, milestone 1.E).

Chạy full stack Phase 1: PromptManager (1.C) + LlamaCppLLMService (1.B) + parser
(1.D) + LLM fallback chain (canned theo mood, 1.E). Đây là harness để duyệt persona
20 turn (DoD P1) và chạy 100 turn (1.F).

Cần server chạy trước (--reasoning off). Xem scripts/chat_test.py docstring.

Chạy:
    # tương tác:
    .\\venv\\Scripts\\python.exe scripts\\cli.py
    # auto (mỗi arg 1 lượt, giữ ngữ cảnh):
    .\\venv\\Scripts\\python.exe scripts\\cli.py "chào Mai" "mày có phải AI không"
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
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
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    pm = PromptManager.from_loader(loader)
    fb = FallbackManager()
    canned = CannedResponder.from_loader(loader)
    runner = LLMTurnRunner.from_loader(loader, svc, pm, fb, canned, on_token=_print_token)

    health = await svc.health_check()
    if not health.is_ok:
        print(f"❌ llama-server chưa chạy ({health.detail}). Start server trước.")
        await svc.stop()
        return

    print("=" * 64)
    print(f"  Mai CLI — persona v{pm.version} — gõ 'quit' để thoát")
    print("=" * 64)

    auto = sys.argv[1:]
    try:
        if auto:
            for p in auto:
                print(f"\nBạn: {p}")
                await _one_turn(runner, svc, p)
        else:
            while True:
                try:
                    user = input("\nBạn: ").strip()
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
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
