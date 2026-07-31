"""Soak test 100+ turn trên model thật (ARCHITECTURE 11.2 DoD, milestone 1.F).

Chạy N turn qua full stack Phase 1 (prompt→LLM→parse→fallback), giữ history, in
progress từng lượt + BÁO CÁO cuối: parse rate, TTFT p50/p95, decode, fallback, crash.

Tuỳ chọn --dashboard: serve dashboard CÙNG process (chung MetricsCollector) → xem
TTFT/decode chạy realtime khi soak. (Dashboard riêng process sẽ không thấy — metrics
là trong-process.)

Cần server chạy trước (--reasoning off).

Chạy:
    .\\venv\\Scripts\\python.exe scripts\\soak_turns.py --turns 100
    .\\venv\\Scripts\\python.exe scripts\\soak_turns.py --turns 120 --dashboard
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

from dashboard.dashboard_server import DashboardServer  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
from orchestrator.logger import get_logger  # noqa: E402
from orchestrator.metrics_collector import MetricsCollector  # noqa: E402
from services.llm.canned_response import CannedResponder  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.llm_turn import LLMTurnRunner  # noqa: E402
from services.llm.prompt_manager import PromptManager  # noqa: E402

_PROMPTS_FILE = REPO_ROOT / "config" / "prompts" / "soak_prompts.txt"
_log = get_logger("soak")


def load_prompts() -> list[str]:
    lines = _PROMPTS_FILE.read_text(encoding="utf-8").splitlines()
    prompts = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not prompts:
        raise SystemExit(f"Không có prompt nào trong {_PROMPTS_FILE}")
    return prompts


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[idx]


def _report(turns, parse_ok, fallback, errors, ttfts, decs, elapsed, target_p50) -> None:
    rate = 100.0 * parse_ok / turns if turns else 0.0
    print("\n" + "=" * 64)
    print("  SOAK REPORT")
    print("=" * 64)
    print(f"  Turns chạy       : {turns}")
    print(f"  Crash/exception  : {errors}   {'✅' if errors == 0 else '❌'}")
    print(f"  Parse mood ok    : {parse_ok}/{turns} = {rate:.1f}%   "
          f"{'✅' if rate > 95 else '❌ (<95%)'}")
    print(f"  Fallback (canned): {fallback}")
    if ttfts:
        p50, p95 = pct(ttfts, 50), pct(ttfts, 95)
        print(f"  TTFT primary     : p50={p50:.0f}ms  p95={p95:.0f}ms  "
              f"min={min(ttfts):.0f}  max={max(ttfts):.0f}  "
              f"{'✅' if p50 < target_p50 else f'❌ (>{target_p50}ms)'}")
    if decs:
        print(f"  Decode tps       : avg={sum(decs) / len(decs):.1f}  min={min(decs):.1f}")
    print(f"  Elapsed          : {elapsed:.1f}s  ({turns / elapsed:.2f} turn/s)")
    print("=" * 64)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Mai soak test")
    ap.add_argument("--turns", type=int, default=100)
    ap.add_argument("--dashboard", action="store_true")
    args = ap.parse_args()

    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    target_p50 = float(loader.get("models", "llm_main.latency_target.ttft_p50_ms", 600))

    metrics = MetricsCollector()
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    pm = PromptManager.from_loader(loader)
    fb = FallbackManager()
    canned = CannedResponder.from_loader(loader)
    runner = LLMTurnRunner.from_loader(loader, svc, pm, fb, canned, metrics=metrics)

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
        print(f"📊 Dashboard: http://{host}:{port}")

    prompts = load_prompts()
    print(f"Soak {args.turns} turn — persona v{pm.version} — pool {len(prompts)} câu\n")

    parse_ok = fallback = errors = 0
    ttfts: list[float] = []
    decs: list[float] = []
    t0 = time.perf_counter()

    try:
        for i in range(args.turns):
            prompt = prompts[i % len(prompts)]
            try:
                parsed, level = await runner.run_turn(f"soak-{i}", prompt)
            except Exception as e:  # no crash: đếm + tiếp
                errors += 1
                _log.error("soak_turn_error", turn=i, error=str(e))
                print(f"[{i + 1:3}/{args.turns}] ❌ EXCEPTION: {e}")
                continue

            m = svc.get_metrics()
            tag = "canned" if level > 0 else "primary"
            if level == 0:
                if m["llm_last_ttft_ms"]:
                    ttfts.append(m["llm_last_ttft_ms"])
                if m["llm_last_decode_tps"]:
                    decs.append(m["llm_last_decode_tps"])
            if parsed.ok:
                parse_ok += 1
            if level > 0:
                fallback += 1

            snippet = " ".join(parsed.text.split())[:60]
            print(f"[{i + 1:3}/{args.turns}] {tag:7} mood={parsed.mood.dominant():9} "
                  f"ok={str(parsed.ok):5} | {prompt[:22]:22} → {snippet}")
    finally:
        elapsed = time.perf_counter() - t0
        _report(args.turns, parse_ok, fallback, errors, ttfts, decs, elapsed, target_p50)
        if dash_server is not None and dash_task is not None:
            print("\n📊 Dashboard vẫn chạy — Ctrl+C để dừng.")
            with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
                await dash_task
            await dash_server.stop_push_loop()
            dash_task.cancel()
        await svc.stop()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
