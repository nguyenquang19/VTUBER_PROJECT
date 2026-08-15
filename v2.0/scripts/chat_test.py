"""Công cụ test tay: chat với Mai qua ĐÚNG pipeline 1.B + 1.C.

Dogfood:
- PromptCache/PromptManager (1.C) dựng persona đầy đủ từ config/prompts/persona_system.txt
  + giữ history (cửa sổ max_history_turns).
- LlamaCppLLMService (1.B) stream qua raw asyncio socket (TTFT ~72ms), --reasoning off.

Cần server chạy trước:
    E:\\BAI_CUA_DUC\\llama\\llama-server.exe -m .\\models\\llm\\gemma_4_12B_Q4.gguf `
      --host 127.0.0.1 --port 8080 -c 4096 -ngl 999 -ctk q8_0 -ctv q8_0 -b 512 `
      --flash-attn on --reasoning off

Chạy:
    # tương tác:
    .\\venv\\Scripts\\python.exe scripts\\chat_test.py
    # auto (mỗi arg là 1 lượt, giữ ngữ cảnh giữa các lượt):
    .\\venv\\Scripts\\python.exe scripts\\chat_test.py "chào Mai" "mày có phải AI không"
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
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.prompt_manager import PromptManager  # noqa: E402


async def run_turn(svc: LlamaCppLLMService, pm: PromptManager, user: str) -> str:
    request = pm.build_request(f"cli-{time.time_ns()}", user)
    parts: list[str] = []
    print("Mai: ", end="", flush=True)
    async for tok in svc.generate_stream(request):
        if tok.token:
            print(tok.token, end="", flush=True)
            parts.append(tok.token)
    text = "".join(parts)
    pm.commit_turn(user, text)
    m = svc.get_metrics()
    ttft = m["llm_last_ttft_ms"] or 0.0
    tps = m["llm_last_decode_tps"] or 0.0
    print(f"\n   [TTFT={ttft:.0f}ms, {tps:.0f} tok/s, tokens={m['llm_last_tokens_out']}]")
    return text


async def main() -> None:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    pm = PromptManager.from_loader(loader)

    health = await svc.health_check()
    if not health.is_ok:
        print(f"❌ llama-server chưa chạy ({health.message}). Start server trước (xem docstring).")
        await svc.stop()
        return

    print("=" * 64)
    print(f"  CHAT với Mai — persona v{pm.version} — gõ 'quit' để thoát")
    print("=" * 64)

    auto_prompts = sys.argv[1:]
    try:
        if auto_prompts:
            for p in auto_prompts:
                print(f"\nBạn: {p}")
                await run_turn(svc, pm, p)
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
                await run_turn(svc, pm, user)
    finally:
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
