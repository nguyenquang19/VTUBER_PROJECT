"""Spike VieNeu-TTS v3 Turbo — CLONE giọng vi_sample.wav (giống viXTTS).

TỐI ƯU: load model 1 lần, test cả streaming + blocking + 2 param preset.
Warmup không tính vào TTFA (chạy trước rồi mới đo).

Preset param:
  - default : temperature=0.8, top_k=25, max_new_frames=300 (mặc định VieNeu)
  - fast    : temperature=0.7, top_k=15, max_new_frames=150 (ép nhanh, có thể mất chất)
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
import torch

from vieneu import Vieneu

from sentences import SENTENCES

REF_WAV = Path("../../models/tts/xtts/vixtts/vi_sample.wav")
OUT_DIR = Path("samples")

HAS_CUDA = torch.cuda.is_available()

PARAM_PRESETS = {
    "default": dict(temperature=0.8, top_k=25, max_new_frames=300),
    "fast":    dict(temperature=0.7, top_k=15, max_new_frames=150),
}


def wav_dur_s(p: Path) -> float:
    with wave.open(str(p), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def cuda_vram_peak_gb() -> float:
    return torch.cuda.max_memory_allocated() / 1024**3 if HAS_CUDA else 0.0


def run_stream(tts: Vieneu, voice_name: str, tag: str, sr: int, params: dict) -> list[dict]:
    print(f"\n--- [{tag}] streaming (params={params}) ---")
    results = []
    for slug, text in SENTENCES:
        out = OUT_DIR / f"{tag}_{slug}.wav"
        chunks = []
        if HAS_CUDA:
            torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        ttfa_ms = None
        try:
            for chunk in tts.infer_stream(text=text, voice=voice_name, **params):
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
            print(f"    [{slug}] ERROR: no chunks")
            results.append({"slug": slug, "text": text, "error": "no chunks"})
            continue
        audio = np.concatenate(chunks)
        sf.write(str(out), audio, sr)
        dur = wav_dur_s(out)
        rtf = (full_ms / 1000) / dur if dur > 0 else 0
        vram = cuda_vram_peak_gb()
        print(
            f"    [{slug}] TTFA={ttfa_ms:5.0f}ms  full={full_ms:6.0f}ms  "
            f"audio={dur:5.2f}s  RTF={rtf:.3f}  chunks={len(chunks)}  vram={vram:.2f}GB"
        )
        results.append({
            "slug": slug, "text": text,
            "ttfa_ms": ttfa_ms, "full_synth_ms": full_ms,
            "audio_duration_s": dur, "rtf": rtf,
            "num_chunks": len(chunks), "vram_peak_gb": vram,
            "wav_path": str(out),
        })
    return results


def run_block(tts: Vieneu, voice_name: str, tag: str, sr: int, params: dict) -> list[dict]:
    print(f"\n--- [{tag}] blocking (params={params}) ---")
    results = []
    for slug, text in SENTENCES:
        out = OUT_DIR / f"{tag}_{slug}.wav"
        if HAS_CUDA:
            torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        try:
            audio = tts.infer(text=text, voice=voice_name, **params)
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
            f"RTF={rtf:.3f}  vram={vram:.2f}GB"
        )
        results.append({
            "slug": slug, "text": text,
            "ttfa_ms": synth_ms, "full_synth_ms": synth_ms,
            "audio_duration_s": dur, "rtf": rtf,
            "vram_peak_gb": vram, "wav_path": str(out),
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


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not REF_WAV.exists():
        print(f"[FATAL] ref wav không có: {REF_WAV.resolve()}")
        sys.exit(2)
    print(f"[info] ref_audio = {REF_WAV.resolve()}")
    print(f"[info] cuda={HAS_CUDA} device={torch.cuda.get_device_name(0) if HAS_CUDA else 'cpu'}")

    # === LOAD 1 LẦN ===
    print(f"\n{'=' * 60}\n[load] VieNeu v3 Turbo (GPU PyTorch)\n{'=' * 60}")
    t0 = time.perf_counter()
    tts = Vieneu(mode="v3turbo", backend="pytorch")
    load_ms = (time.perf_counter() - t0) * 1000
    sr = int(getattr(tts, "sample_rate", 48000))
    print(f"[load] {load_ms:.0f}ms  sample_rate={sr}Hz")

    # === ENROLL REF 1 LẦN — encode speaker_emb + ref_codes rồi cache thành voice preset ===
    # Nếu không làm bước này, MỖI infer() phải re-encode → chậm gấp 3-5 lần.
    print(f"\n[enroll] add_voice('mai_ref', denoise=True) từ vi_sample.wav…")
    t0 = time.perf_counter()
    tts.add_voice("mai_ref", str(REF_WAV), denoise=True)
    print(f"[enroll] xong sau {(time.perf_counter() - t0) * 1000:.0f}ms — sau đây dùng voice='mai_ref'")

    # === WARMUP (không đo) — làm nóng kernel + KV cache ===
    print("\n[warmup] chạy 1 câu dummy để nóng kernel (không tính vào TTFA)…")
    t0 = time.perf_counter()
    try:
        _ = tts.infer(text="Xin chào.", voice="mai_ref")
    except Exception as e:
        print(f"[warmup] lỗi (bỏ qua): {e}")
    print(f"[warmup] xong sau {(time.perf_counter() - t0) * 1000:.0f}ms")

    # === BENCHMARK 4 cấu hình (không reload, không re-encode) ===
    all_results = {}
    for preset_name, params in PARAM_PRESETS.items():
        stream_tag = f"stream_{preset_name}"
        block_tag = f"block_{preset_name}"
        all_results[stream_tag] = run_stream(tts, "mai_ref", stream_tag, sr, params)
        summary(stream_tag, all_results[stream_tag])
        all_results[block_tag] = run_block(tts, "mai_ref", block_tag, sr, params)
        summary(block_tag, all_results[block_tag])

    Path("results_clone.json").write_text(
        json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{'=' * 60}\n[TỔNG KẾT — so với viXTTS TTFA 450ms]\n{'=' * 60}")
    print(f"{'tag':<24} {'avg TTFA':>10} {'avg full':>10} {'RTF':>6} {'verdict':>15}")
    for tag, results in all_results.items():
        ok = [r for r in results if "ttfa_ms" in r]
        if not ok:
            continue
        avg_ttfa = sum(r["ttfa_ms"] for r in ok) / len(ok)
        avg_full = sum(r["full_synth_ms"] for r in ok) / len(ok)
        avg_rtf = sum(r["rtf"] for r in ok) / len(ok)
        v_ttfa = "PASS<450ms" if avg_ttfa < 450 else f"WORSE {avg_ttfa/450:.1f}x"
        v_rtf = "OK" if avg_rtf < 1.0 else "NO REAL-TIME"
        print(f"{tag:<24} {avg_ttfa:>8.0f}ms {avg_full:>8.0f}ms {avg_rtf:>6.3f} {v_ttfa:>15}  {v_rtf}")

    del tts
    gc.collect()
    if HAS_CUDA:
        torch.cuda.empty_cache()

    print(f"\n[done] results_clone.json + samples/*.wav")
    print(f"[so sánh] viXTTS: ../day2_tts_quality/samples/vixtts/*.wav")


if __name__ == "__main__":
    main()
