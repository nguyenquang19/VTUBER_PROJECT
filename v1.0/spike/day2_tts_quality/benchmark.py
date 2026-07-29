"""Day 2 spike — Piper VN TTS benchmark (ARCHITECTURE Section 0.3).

Synthesize 10 câu tiếng Việt với các Piper voices đã tải, đo:
  - Latency per sentence
  - RTF (real-time factor = synth_time / audio_duration)
  - Xuất WAV vào samples/{voice_name}/ để nghe chấm quality subjective

USAGE (PowerShell, từ v1.0 root):
  .\venv\Scripts\Activate.ps1
  cd spike\day2_tts_quality
  python benchmark.py
"""
from __future__ import annotations

import json
import sys
import time
import wave
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from piper import PiperVoice

from sentences import SENTENCES


VOICES = [
    ("vais1000-medium", "../../models/tts/piper/vi_VN-vais1000-medium.onnx"),
    ("25hours_single-low", "../../models/tts/piper/vi_VN-25hours_single-low.onnx"),
    ("vivos-x_low", "../../models/tts/piper/vi_VN-vivos-x_low.onnx"),
]

SAMPLES_DIR = Path("samples")
RESULTS_PATH = Path("results.json")


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / rate if rate else 0.0


def run_voice(voice_name: str, model_path: str) -> list[dict]:
    print(f"\n=== {voice_name} ===")
    if not Path(model_path).exists():
        print(f"    [SKIP] model not found: {model_path}")
        return []

    t_load = time.perf_counter()
    v = PiperVoice.load(model_path)
    load_ms = (time.perf_counter() - t_load) * 1000
    print(f"    loaded in {load_ms:.0f}ms  sr={v.config.sample_rate}Hz")

    out_dir = SAMPLES_DIR / voice_name
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for slug, text in SENTENCES:
        wav_path = out_dir / f"{slug}.wav"
        t0 = time.perf_counter()
        try:
            with wave.open(str(wav_path), "wb") as wf:
                v.synthesize_wav(text, wf)
        except Exception as e:
            print(f"    [{slug}] ERROR: {e}")
            results.append({"slug": slug, "error": str(e), "text": text})
            continue
        synth_ms = (time.perf_counter() - t0) * 1000
        dur_s = wav_duration_sec(wav_path)
        rtf = (synth_ms / 1000) / dur_s if dur_s > 0 else 0.0
        print(f"    [{slug}] synth={synth_ms:5.0f}ms  audio={dur_s:4.2f}s  RTF={rtf:.3f}")
        results.append({
            "slug": slug,
            "text": text,
            "synth_ms": synth_ms,
            "audio_duration_s": dur_s,
            "rtf": rtf,
            "wav_path": str(wav_path),
        })
    return results


def print_summary(all_results: dict) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for voice, results in all_results.items():
        if not results:
            print(f"{voice}: SKIPPED")
            continue
        valid = [r for r in results if "synth_ms" in r]
        if not valid:
            continue
        avg_synth = sum(r["synth_ms"] for r in valid) / len(valid)
        avg_dur = sum(r["audio_duration_s"] for r in valid) / len(valid)
        avg_rtf = sum(r["rtf"] for r in valid) / len(valid)
        print(
            f"{voice:<25} avg synth={avg_synth:5.0f}ms  "
            f"avg audio={avg_dur:4.2f}s  avg RTF={avg_rtf:.3f}  "
            f"({len(valid)}/{len(results)} OK)"
        )
    print("\nGo criteria (ARCHITECTURE 0.3):")
    print("  - Ít nhất 1 voice ≥6/10 quality subjective + latency <800ms")
    print("\nNext: nghe file .wav trong samples/<voice>/ → chấm điểm vào day2_report.md")


def main() -> None:
    all_results: dict = {}
    for voice_name, model_path in VOICES:
        all_results[voice_name] = run_voice(voice_name, model_path)
    RESULTS_PATH.write_text(
        json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n[saved] {RESULTS_PATH.resolve()}")
    print(f"[wav files] {SAMPLES_DIR.resolve()}")
    print_summary(all_results)


if __name__ == "__main__":
    main()
