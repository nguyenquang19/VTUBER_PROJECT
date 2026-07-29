"""Day 1 spike — LLM latency benchmark (ARCHITECTURE Section 0.2).

Chạy 5 scenario chống llama-server (OpenAI-compatible endpoint), đo:
  - TTFT (time to first token, ms)
  - Decode speed (tokens/sec sau token đầu)
  - GPU temp + throttle ratio (scenario 5)

Kết quả xuất `results.json` để điền vào `spike/day1_report.md`.

USAGE (PowerShell):
  # 1. Start llama-server (theo Section 0.2)
  # 2. Ở terminal khác:
  #    cd spike\day1_llm_latency
  #    ..\..\venv\Scripts\python.exe benchmark.py --endpoint http://localhost:8080
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from gpu_monitor import GpuMonitor
from prompts import make_prompt


async def stream_once(
    client: httpx.AsyncClient,
    endpoint: str,
    messages: list[dict],
    max_tokens: int,
) -> dict:
    """Gọi 1 chat completion streaming. Return metrics."""
    payload = {
        "model": "gemma",
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "cache_prompt": True,
    }
    t_start = time.perf_counter()
    t_first: float | None = None
    tokens_out = 0
    prompt_tokens: int | None = None

    async with client.stream(
        "POST",
        f"{endpoint}/v1/chat/completions",
        json=payload,
        timeout=180.0,
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = chunk.get("usage")
            if usage and prompt_tokens is None:
                prompt_tokens = usage.get("prompt_tokens")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content:
                if t_first is None:
                    t_first = time.perf_counter()
                tokens_out += 1
    t_end = time.perf_counter()

    if t_first is None:
        return {
            "ttft_ms": None,
            "decode_tps": None,
            "tokens_out": 0,
            "total_ms": (t_end - t_start) * 1000,
            "prompt_tokens": prompt_tokens,
        }
    ttft_ms = (t_first - t_start) * 1000
    decode_seconds = t_end - t_first
    decode_tps = tokens_out / decode_seconds if decode_seconds > 0 else 0.0
    return {
        "ttft_ms": ttft_ms,
        "decode_tps": decode_tps,
        "tokens_out": tokens_out,
        "total_ms": (t_end - t_start) * 1000,
        "prompt_tokens": prompt_tokens,
    }


async def scenario_cold(client: httpx.AsyncClient, endpoint: str) -> dict:
    print("\n>>> S1 Cold start")
    print("    Vui lòng RESTART llama-server (Ctrl+C rồi start lại),")
    print("    đợi thấy 'HTTP server listening' → Enter để tiếp tục.")
    try:
        input("    [Enter khi ready] ")
    except EOFError:
        print("    (stdin closed — auto-continue)")
    messages = make_prompt(target_tokens=500)
    return await stream_once(client, endpoint, messages, max_tokens=100)


async def scenario_warm(
    client: httpx.AsyncClient,
    endpoint: str,
    target_tokens: int,
    label: str,
) -> dict:
    print(f"\n>>> {label} — warmup...")
    messages = make_prompt(target_tokens=target_tokens)
    await stream_once(client, endpoint, messages, max_tokens=30)
    print(f">>> {label} — measured run...")
    return await stream_once(client, endpoint, messages, max_tokens=100)


async def scenario_overheating(
    client: httpx.AsyncClient,
    endpoint: str,
    duration_sec: int,
    interval_sec: int,
) -> dict:
    print(f"\n>>> S5 Overheating — chạy {duration_sec/60:.0f} phút, sample mỗi {interval_sec}s")
    monitor = GpuMonitor(poll_interval=10.0)
    monitor.start()
    samples: list[dict] = []
    messages = make_prompt(target_tokens=2000)
    t0 = time.perf_counter()
    try:
        while (time.perf_counter() - t0) < duration_sec:
            result = await stream_once(client, endpoint, messages, max_tokens=100)
            elapsed = time.perf_counter() - t0
            samples.append({"t_sec": elapsed, **result})
            print(
                f"    [{elapsed/60:5.1f}m] TTFT={result.get('ttft_ms', 0):6.0f}ms "
                f"decode={result.get('decode_tps', 0):5.1f}tok/s"
            )
            wait = interval_sec - result.get("total_ms", 0) / 1000
            if wait > 0:
                await asyncio.sleep(wait)
    finally:
        gpu = monitor.stop()
    return {"samples": samples, "gpu": gpu}


async def health_check(client: httpx.AsyncClient, endpoint: str) -> bool:
    try:
        r = await client.get(f"{endpoint}/health", timeout=5)
        print(f"llama-server {endpoint}/health → {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"ERROR: không kết nối được {endpoint}: {e}")
        return False


def print_summary(results: dict) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY (target: ARCHITECTURE 0.2 table)")
    print("=" * 72)
    print(
        f"{'SCENARIO':<25} {'TTFT (ms)':>12} {'DECODE (tok/s)':>18} {'PROMPT_TOK':>12}"
    )
    for key, target in (
        ("s1_cold", "<500ms / >50tps"),
        ("s2_warm_short", "<300ms / >60tps"),
        ("s3_warm_medium", "<800ms / >45tps"),
        ("s4_warm_long", "<1500ms / >35tps"),
    ):
        r = results.get(key)
        if not r:
            continue
        ttft = f"{r['ttft_ms']:.0f}" if r.get("ttft_ms") is not None else "n/a"
        tps = f"{r['decode_tps']:.1f}" if r.get("decode_tps") is not None else "n/a"
        pt = r.get("prompt_tokens") or "?"
        print(f"{key:<25} {ttft:>12} {tps:>18} {pt:>12}   target: {target}")

    s5 = results.get("s5_overheating")
    if s5 and s5["samples"]:
        first = s5["samples"][:3]
        last = s5["samples"][-3:]

        def avg(items: list[dict], key: str) -> float:
            vals = [x[key] for x in items if x.get(key) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        print(f"\ns5_overheating: {len(s5['samples'])} samples")
        print(
            f"  first 3 avg: TTFT={avg(first, 'ttft_ms'):.0f}ms decode={avg(first, 'decode_tps'):.1f}tps"
        )
        print(
            f"  last 3 avg:  TTFT={avg(last, 'ttft_ms'):.0f}ms decode={avg(last, 'decode_tps'):.1f}tps"
        )
        gpu = s5.get("gpu") or {}
        if gpu.get("num_samples"):
            print(
                f"  GPU: max_temp={gpu.get('max_temp')}°C "
                f"avg_temp={gpu.get('avg_temp', 0):.1f}°C "
                f"throttle_ratio={gpu.get('throttle_ratio', 0)*100:.1f}% "
                f"(target <30%)"
            )

    print("\nNo-go criteria (ARCHITECTURE 0.2):")
    print("  - TTFT cold > 1000ms → re-architect")
    print("  - decode < 30 tok/s → cân nhắc model nhỏ hơn")
    print("  - overheating throttle > 30% → cần thermal plan")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Day 1 LLM latency benchmark")
    parser.add_argument("--endpoint", default="http://localhost:8080")
    parser.add_argument(
        "--scenarios",
        default="1,2,3,4,5",
        help="danh sách phân cách dấu phẩy: 1=cold 2=warm-short 3=med 4=long 5=overheat",
    )
    parser.add_argument("--output", default="results.json")
    parser.add_argument(
        "--overheat-sec",
        type=int,
        default=1800,
        help="thời lượng overheating (mặc định 1800s = 30 phút)",
    )
    parser.add_argument(
        "--overheat-interval",
        type=int,
        default=60,
        help="khoảng giữa mỗi request trong overheating (giây)",
    )
    args = parser.parse_args()

    to_run = set(args.scenarios.split(","))
    results: dict[str, Any] = {
        "meta": {
            "endpoint": args.endpoint,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scenarios": args.scenarios,
        }
    }

    async with httpx.AsyncClient() as client:
        if not await health_check(client, args.endpoint):
            sys.exit(1)

        if "1" in to_run:
            results["s1_cold"] = await scenario_cold(client, args.endpoint)
        if "2" in to_run:
            results["s2_warm_short"] = await scenario_warm(
                client, args.endpoint, 500, "S2 Warm short (~500 tok)"
            )
        if "3" in to_run:
            results["s3_warm_medium"] = await scenario_warm(
                client, args.endpoint, 2000, "S3 Warm medium (~2K tok)"
            )
        if "4" in to_run:
            results["s4_warm_long"] = await scenario_warm(
                client, args.endpoint, 4000, "S4 Warm long (~4K tok)"
            )
        if "5" in to_run:
            results["s5_overheating"] = await scenario_overheating(
                client, args.endpoint, args.overheat_sec, args.overheat_interval
            )

    results["meta"]["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    output_path = Path(args.output)
    output_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n[saved] {output_path.resolve()}")
    print_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
