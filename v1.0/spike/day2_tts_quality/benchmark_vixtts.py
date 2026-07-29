"""Day 2 spike — viXTTS synthesize 10 câu VN (voice clone qua vi_sample.wav).

Cần các patch runtime (torchcodec bypass + vi tokenizer) do coqui-tts stock
không có VN support và torch 2.11 không tương thích torchcodec+FFmpeg 8.
"""
from __future__ import annotations

import sys
import time
import json
import wave
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --- Runtime patches ---
import torchaudio
import torch as _t
import soundfile as _sf


def _sf_load(path, *args, **kw):
    audio, sr = _sf.read(str(path), dtype="float32", always_2d=True)
    return _t.from_numpy(audio.T), sr


torchaudio.load = _sf_load

from TTS.tts.layers.xtts import tokenizer as _tk

_orig_preprocess = _tk.VoiceBpeTokenizer.preprocess_text


def _vi_preprocess(self, txt, lang):
    if lang == "vi":
        return _tk.multilingual_cleaners(txt, "en")
    return _orig_preprocess(self, txt, lang)


_tk.VoiceBpeTokenizer.preprocess_text = _vi_preprocess

# --- viXTTS load ---
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts

from sentences import SENTENCES

MODEL_DIR = Path("../../models/tts/xtts/vixtts")
SPEAKER_WAV = MODEL_DIR / "vi_sample.wav"
OUT_DIR = Path("samples/vixtts")


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading viXTTS...")
    t0 = time.perf_counter()
    config = XttsConfig()
    config.load_json(str(MODEL_DIR / "config.json"))
    model = Xtts.init_from_config(config)
    model.load_checkpoint(config, checkpoint_dir=str(MODEL_DIR), use_deepspeed=False, eval=True)
    model.cuda()
    load_ms = (time.perf_counter() - t0) * 1000
    vram_gb = _t.cuda.memory_allocated() / 1024**3
    print(f"loaded in {load_ms:.0f}ms  vram={vram_gb:.2f}GB")

    results = []
    for slug, text in SENTENCES:
        out_path = OUT_DIR / f"{slug}.wav"
        t0 = time.perf_counter()
        try:
            out = model.synthesize(
                text,
                config,
                speaker_wav=str(SPEAKER_WAV),
                gpt_cond_len=3,
                language="vi",
            )
            _sf.write(str(out_path), out["wav"], 24000)
        except Exception as e:
            print(f"    [{slug}] ERROR: {e}")
            results.append({"slug": slug, "error": str(e)})
            continue
        synth_ms = (time.perf_counter() - t0) * 1000
        dur = wav_duration(out_path)
        rtf = (synth_ms / 1000) / dur if dur > 0 else 0.0
        print(f"    [{slug}] synth={synth_ms:5.0f}ms  audio={dur:4.2f}s  RTF={rtf:.3f}")
        results.append({
            "slug": slug,
            "text": text,
            "synth_ms": synth_ms,
            "audio_duration_s": dur,
            "rtf": rtf,
            "wav_path": str(out_path),
        })

    Path("results_vixtts.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    valid = [r for r in results if "synth_ms" in r]
    if valid:
        avg = sum(r["synth_ms"] for r in valid) / len(valid)
        avg_rtf = sum(r["rtf"] for r in valid) / len(valid)
        print(f"\navg synth={avg:.0f}ms  avg RTF={avg_rtf:.3f}  ({len(valid)}/{len(results)} OK)")


if __name__ == "__main__":
    main()
