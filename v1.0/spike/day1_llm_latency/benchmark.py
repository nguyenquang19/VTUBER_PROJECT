"""Day 1 spike — LLM latency benchmark (ARCHITECTURE Section 0.2).

Chạy 5 scenario chống llama-server (OpenAI-compatible endpoint), đo:
  - TTFT (time to first token, ms)
  - Decode speed (tokens/sec sau token đầu)
  - GPU temp + throttle ratio (scenario 5)

Kết quả xuất `results.json` (append sau mỗi scenario — resilient khi crash).

USAGE (PowerShell):
  # 1. Start llama-server với -c 4096 (theo Section 0.2)
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
from prompts import build_messages


async def stream_once(
    client: httpx.AsyncClient,
    endpoint: str,
    messages: list[dict],
    max_tokens: int,
) -> dict:
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

    try:
        async with client.stream(
            "POST",
            f"{endpoint}/v1/chat/completions",
            json=payload,
            timeout=180.0,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise RuntimeError(
                    f"HTTP {resp.status_code}: {body.decode('utf-8', errors='replace')[:500]}"
                )
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
    except Exception as e:
        return {"error": str(e), "ttft_ms": None, "decode_tps": None, "tokens_out": 0}

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


def save_checkpoint(results: dict, path: Path) -> None:
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")


async def scenario_cold(client: httpx.AsyncClient, endpoint: str) -> dict:
    print("\n>>> S1 Cold start")
    print("    Vui lòng RESTART llama-server (Ctrl+C rồi start lại),")
    print("    đợi 'HTTP server listening' → Enter để tiếp tục.")
    try:
        input("    [Enter khi ready] ")
    except EOFError:
        pass
    messages, ptok = await build_messages(client, endpoint, target_prompt_tokens=500)
    print(f"    prompt_tokens (measured): {ptok}")
    return await stream_once(client, endpoint, messages, max_tokens=100)


async def scenario_warm(
    client: httpx.AsyncClient,
    endpoint: str,
    target_tokens: int,
    label: str,
) -> dict:
    print(f"\n>>> {label} — build prompt...")
    messages, ptok = await build_messages(client, endpoint, target_prompt_tokens=target_tokens)
    print(f"    prompt_tokens (measured): {ptok}")
    print(f">>> {label} — warmup...")
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
    messages, ptok = await build_messages(client, endpoint, target_prompt_tokens=2000)
    print(f"    prompt_tokens (measured): {ptok}")
    monitor = GpuMonitor(poll_interval=10.0)
    monitor.start()
    samples: list[dict] = []
    t0 = time.perf_counter()
    try:
        while (time.perf_counter() - t0) < duration_sec:
            result = await stream_once(client, endpoint, messages, max_tokens=100)
            elapsed = time.perf_counter() - t0
            samples.append({"t_sec": elapsed, **result})
            print(
                f"    [{elapsed/60:5.1f}m] TTFT={result.get('ttft_ms') or 0:6.0f}ms "
                f"decode={result.get('decode_tps') or 0:5.1f}tok/s"
            )
            wait = interval_sec - (result.get("total_ms") or 0) / 1000
            if wait > 0:
                await asyncio.sleep(wait)
    finally:
        gpu = monitor.stop()
    return {"samples": samples, "gpu": gpu, "prompt_tokens": ptok}


async def health_check(client: httpx.AsyncClient, endpoint: str) -> bool:
    try:
        r = await client.get(f"{endpoint}/health", timeout=5)
        print(f"llama-server {endpoint}/health → {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"ERROR: không kết nối được {endpoint}: {e}")
        return False


def print_summary(results: dict) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY (target: ARCHITECTURE 0.2)")
    print("=" * 78)
    print(f"{'SCENARIO':<25} {'TTFT (ms)':>10} {'DECODE (tps)':>13} {'PROMPT_TOK':>11}   target")
    for key, target in (
        ("s1_cold", "TTFT<500 / dec>50"),
        ("s2_warm_short", "TTFT<300 / dec>60"),
        ("s3_warm_medium", "TTFT<800 / dec>45"),
        ("s4_warm_long", "TTFT<1500 / dec>35"),
    ):
        r = results.get(key)
        if not r:
            continue
        if r.get("error"):
            print(f"{key:<25} ERROR: {r['error'][:60]}")
            continue
        ttft = f"{r['ttft_ms']:.0f}" if r.get("ttft_ms") is not None else "n/a"
        tps = f"{r['decode_tps']:.1f}" if r.get("decode_tps") is not None else "n/a"
        pt = r.get("prompt_tokens") or "?"
        print(f"{key:<25} {ttft:>10} {tps:>13} {pt:>11}   {target}")

    s5 = results.get("s5_overheating")
    if s5 and s5.get("samples"):
        first = s5["samples"][:3]
        last = s5["samples"][-3:]

        def avg(items: list[dict], key: str) -> float:
            vals = [x[key] for x in items if x.get(key) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        print(f"\ns5_overheating: {len(s5['samples'])} samples, prompt_tokens={s5.get('prompt_tokens')}")
        print(f"  first 3: TTFT={avg(first, 'ttft_ms'):.0f}ms decode={avg(first, 'decode_tps'):.1f}tps")
        print(f"  last 3:  TTFT={avg(last, 'ttft_ms'):.0f}ms decode={avg(last, 'decode_tps'):.1f}tps")
        gpu = s5.get("gpu") or {}
        if gpu.get("num_samples"):
            print(
                f"  GPU: max={gpu.get('max_temp')}°C avg={gpu.get('avg_temp', 0):.1f}°C "
                f"throttle={gpu.get('throttle_ratio', 0)*100:.1f}% (target <30%)"
            )

    print("\nNo-go (ARCHITECTURE 0.2):")
    print("  - TTFT cold > 1000ms → re-architect")
    print("  - decode < 30 tok/s → cân nhắc model nhỏ hơn")
    print("  - throttle > 30% → cần thermal plan")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Day 1 LLM latency benchmark")
    parser.add_argument("--endpoint", default="http://localhost:8080")
    parser.add_argument(
        "--scenarios",
        default="1,2,3,4,5",
        help="1=cold 2=warm-short 3=med 4=long 5=overheat",
    )
    parser.add_argument("--output", default="results.json")
    parser.add_argument("--overheat-sec", type=int, default=1800)
    parser.add_argument("--overheat-interval", type=int, default=60)
    args = parser.parse_args()

    to_run = set(args.scenarios.split(","))
    output_path = Path(args.output)
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

        scenarios = [
            ("1", "s1_cold", lambda: scenario_cold(client, args.endpoint)),
            ("2", "s2_warm_short", lambda: scenario_warm(client, args.endpoint, 500, "S2 Warm short (~500 tok)")),
            ("3", "s3_warm_medium", lambda: scenario_warm(client, args.endpoint, 2000, "S3 Warm medium (~2K tok)")),
            ("4", "s4_warm_long", lambda: scenario_warm(client, args.endpoint, 4000, "S4 Warm long (~4K tok)")),
            ("5", "s5_overheating", lambda: scenario_overheating(client, args.endpoint, args.overheat_sec, args.overheat_interval)),
        ]
        for tag, key, coro in scenarios:
            if tag not in to_run:
                continue
            try:
                results[key] = await coro()
            except Exception as e:
                results[key] = {"error": f"scenario crashed: {e}"}
                print(f"    [ERROR] {key}: {e}")
            save_checkpoint(results, output_path)  # persist ngay sau mỗi scenario

    results["meta"]["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_checkpoint(results, output_path)
    print(f"\n[saved] {output_path.resolve()}")
    print_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
