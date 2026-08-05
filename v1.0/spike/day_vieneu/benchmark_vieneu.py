"""Spike VieNeu-TTS v3 Turbo — đo TTFA/RTF/VRAM trên 10 câu VN.

Test 2 backend cùng model pnnbao-ump/VieNeu-TTS-v3-Turbo (48kHz output):
  1. GPU PyTorch  — auto khi có CUDA. Có streaming (infer_stream frame-level).
  2. CPU ONNX int8 — torch-free, dùng khi ép backend='onnx' hoặc không CUDA.

Voice: dùng preset đầu tiên (bỏ bước clone_voice, test quality baseline).

Chạy: python benchmark_vieneu.py
"""
from __future__ import annotations

import gc
import json
import sys
import time
import wave
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import soundfile as sf

try:
    import torch
    HAS_CUDA = torch.cuda.is_available()
except ImportError:
    torch = None
    HAS_CUDA = False

from vieneu import Vieneu

from sentences import SENTENCES

OUT_DIR = Path("samples")


def wav_dur_s(p: Path) -> float:
    with wave.open(str(p), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def cuda_vram_peak_gb() -> float:
    return torch.cuda.max_memory_allocated() / 1024**3 if HAS_CUDA else 0.0


def pick_voice(tts: Vieneu) -> str | None:
    """v3turbo: voice là string name (không phải dict). Lấy id đầu tiên."""
    try:
        voices = tts.list_preset_voices()
    except AttributeError:
        return None
    if not voices:
        return None
    # base.list_preset_voices trả list[(description, id)]
    if isinstance(voices[0], tuple):
        names = [vid for _, vid in voices]
    else:
        names = list(voices)
    print(f"    preset voices ({len(names)}): {names[:8]}{'…' if len(names) > 8 else ''}")
    print(f"    dùng voice: {names[0]!r}")
    return names[0]


def bench_blocking(tts: Vieneu, voice: str, out_prefix: str, sr: int) -> list[dict]:
    results = []
    for slug, text in SENTENCES:
        out = OUT_DIR / f"{out_prefix}_{slug}.wav"
        if HAS_CUDA:
            torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        try:
            audio = tts.infer(text=text, voice=voice)
        except Exception as e:
            print(f"    [{slug}] ERROR: {type(e).__name__}: {e}")
            results.append({"slug": slug, "text": text, "error": f"{type(e).__name__}: {e}"})
            continue
        synth_ms = (time.perf_counter() - t0) * 1000

        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        audio = np.asarray(audio, dtype=np.float32).squeeze()
        sf.write(str(out), audio, sr)
        dur = wav_dur_s(out)
        rtf = (synth_ms / 1000) / dur if dur > 0 else 0
        vram = cuda_vram_peak_gb()
        print(
            f"    [{slug}] synth={synth_ms:6.0f}ms  audio={dur:5.2f}s  "
            f"RTF={rtf:.3f}  vram_peak={vram:.2f}GB"
        )
        results.append({
            "slug": slug, "text": text,
            "ttfa_ms": synth_ms, "full_synth_ms": synth_ms,
            "audio_duration_s": dur, "rtf": rtf,
            "vram_peak_gb": vram, "wav_path": str(out),
        })
    return results


def bench_streaming(tts: Vieneu, voice: str, out_prefix: str, sr: int) -> list[dict]:
    results = []
    for slug, text in SENTENCES:
        out = OUT_DIR / f"{out_prefix}_{slug}.wav"
        chunks = []
        t0 = time.perf_counter()
        ttfa_ms = None
        try:
            for chunk in tts.infer_stream(text=text, voice=voice):
                if ttfa_ms is None:
                    ttfa_ms = (time.perf_counter() - t0) * 1000
                if hasattr(chunk, "detach"):
                    chunk = chunk.detach().cpu().numpy()
                chunks.append(np.asarray(chunk, dtype=np.float32).squeeze())
        except Exception as e:
            print(f"    [{slug}] ERROR: {type(e).__name__}: {e}")
            results.append({"slug": slug, "text": text, "error": f"{type(e).__name__}: {e}"})
            continue
        full_ms = (time.perf_counter() - t0) * 1000
        if not chunks:
            print(f"    [{slug}] ERROR: no chunks yielded")
            results.append({"slug": slug, "text": text, "error": "no chunks"})
            continue
        audio = np.concatenate(chunks)
        sf.write(str(out), audio, sr)
        dur = wav_dur_s(out)
        rtf = (full_ms / 1000) / dur if dur > 0 else 0
        print(
            f"    [{slug}] TTFA={ttfa_ms:5.0f}ms  full={full_ms:6.0f}ms  "
            f"audio={dur:5.2f}s  RTF={rtf:.3f}  chunks={len(chunks)}"
        )
        results.append({
            "slug": slug, "text": text,
            "ttfa_ms": ttfa_ms, "full_synth_ms": full_ms,
            "audio_duration_s": dur, "rtf": rtf,
            "num_chunks": len(chunks), "wav_path": str(out),
        })
    return results


def summary(name: str, results: list[dict]) -> None:
    ok = [r for r in results if "ttfa_ms" in r]
    err = [r for r in results if "error" in r]
    print(f"\n[{name}] {len(ok)}/{len(results)} OK, {len(err)} error")
    if ok:
        avg_ttfa = sum(r["ttfa_ms"] for r in ok) / len(ok)
        avg_full = sum(r["full_synth_ms"] for r in ok) / len(ok)
        avg_rtf = sum(r["rtf"] for r in ok) / len(ok)
        print(f"[{name}] avg TTFA={avg_ttfa:6.0f}ms  avg full={avg_full:6.0f}ms  avg RTF={avg_rtf:.3f}")
        print(f"[{name}] TTFA target <1000ms → {'PASS' if avg_ttfa < 1000 else 'FAIL'}")


def run_variant(tag: str, backend: str, streaming: bool) -> list[dict]:
    print(f"\n{'=' * 60}\n[{tag}] backend={backend} streaming={streaming}\n{'=' * 60}")
    if HAS_CUDA:
        torch.cuda.empty_cache()
    t0 = time.perf_counter()
    # mode="v3turbo" là default; ép backend qua tham số v3turbo
    kwargs = {"backend": backend}
    if backend == "onnx":
        kwargs["device"] = "cpu"
    tts = Vieneu(mode="v3turbo", **kwargs)
    load_ms = (time.perf_counter() - t0) * 1000
    sr = int(getattr(tts, "sample_rate", 48000))
    print(f"[{tag}] loaded in {load_ms:.0f}ms  sample_rate={sr}Hz  "
          f"vram_alloc={(torch.cuda.memory_allocated()/1024**3 if HAS_CUDA else 0):.2f}GB")

    voice = pick_voice(tts)
    if voice is None:
        print(f"[{tag}] không lấy được preset voice → skip")
        return []

    if streaming:
        results = bench_streaming(tts, voice, tag, sr)
    else:
        results = bench_blocking(tts, voice, tag, sr)
    summary(tag, results)

    del tts
    gc.collect()
    if HAS_CUDA:
        torch.cuda.empty_cache()
    return results


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[info] torch cuda available: {HAS_CUDA}")
    if HAS_CUDA:
        print(f"[info] device={torch.cuda.get_device_name(0)}")

    all_results = {}

    # Variant 1: GPU PyTorch — chất lượng tối đa 48kHz, có streaming
    if HAS_CUDA:
        all_results["gpu_pytorch_stream"] = run_variant(
            tag="gpu_pytorch_stream", backend="pytorch", streaming=True,
        )
        all_results["gpu_pytorch_block"] = run_variant(
            tag="gpu_pytorch_block", backend="pytorch", streaming=False,
        )
    else:
        print("\n[warn] không có CUDA → skip GPU variant")

    # Variant 2: CPU ONNX int8 — torch-free, so sánh baseline
    all_results["cpu_onnx_int8_stream"] = run_variant(
        tag="cpu_onnx_int8_stream", backend="onnx", streaming=True,
    )

    Path("results_vieneu.json").write_text(
        json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\n[done] results_vieneu.json + samples/*.wav")


if __name__ == "__main__":
    main()
