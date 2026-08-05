# Spike — VieNeu-TTS

**Mục tiêu:** đánh giá VieNeu-TTS v3 Turbo thay viXTTS cho Phase 4.

Test cùng model `pnnbao-ump/VieNeu-TTS-v3-Turbo` (48kHz), 3 setup:
1. **GPU PyTorch + streaming** — TTFA thấp nhất kỳ vọng (target <1s)
2. **GPU PyTorch + blocking** — full synth trên GPU (baseline latency)
3. **CPU ONNX int8 + streaming** — torch-free, dự phòng nếu VRAM đầy chung với llama-server

## Kiến trúc v3 Turbo
- Backbone: PyTorch (GPU) hoặc ONNX Runtime (CPU int8)
- Vocoder: MOSS-Audio-Tokenizer-Nano
- Sample rate: **48kHz** (v2 là 24kHz)
- License: Apache 2.0 (thương mại OK)
- Fine-tune LoRA support qua `/finetune` trong repo GitHub

## Setup — venv riêng

### Bước 1 — core (đã làm)

```powershell
python -m venv E:\BAI_CUA_DUC\AI_VTUBER\venv_vieneu
E:\BAI_CUA_DUC\AI_VTUBER\venv_vieneu\Scripts\Activate.ps1
pip install vieneu --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
pip install soundfile numpy
```

### Bước 2 — thêm PyTorch CUDA để test GPU (BẮT BUỘC nếu muốn GPU variant)

```powershell
E:\BAI_CUA_DUC\AI_VTUBER\venv_vieneu\Scripts\Activate.ps1
pip install torch==2.4.0+cu124 --extra-index-url https://download.pytorch.org/whl/cu124
```

Không có torch → benchmark tự skip GPU variant, chỉ chạy CPU ONNX.

## Chạy benchmark

```powershell
E:\BAI_CUA_DUC\AI_VTUBER\venv_vieneu\Scripts\Activate.ps1
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0\spike\day_vieneu
python benchmark_vieneu.py
```

Output:
- `results_vieneu.json` — số liệu 3 setup × 10 câu
- `samples/gpu_pytorch_stream_*.wav` — GPU stream 48kHz
- `samples/gpu_pytorch_block_*.wav` — GPU blocking 48kHz
- `samples/cpu_onnx_int8_stream_*.wav` — CPU ONNX stream 48kHz

## Cần đánh giá
1. **TTFA thực** (đặc biệt variant GGUF streaming — target <1s per DoD Phase 4)
2. **RTF** — <1.0 nghĩa gen kịp thời gian phát
3. **VRAM 0.5B GPU** — phải fit budget cùng llama-server (Gemma 4 12B ~7GB, GPU 16GB → còn ~9GB cho TTS + overhead)
4. **Chất lượng chủ quan** — user nghe 10 wav mỗi variant, so với `../day2_tts_quality/samples/vixtts/`

## Voice cloning
Spike này dùng preset voice đầu tiên (bỏ qua bước clone) để test quality baseline.
Nếu variant nào OK → viết tiếp voice clone với ref audio riêng cho Mai (`clone_voice(audio_path, text, name)`, cần 3-8s ref + transcript đúng).

## Kết quả
*Chưa chạy.*
