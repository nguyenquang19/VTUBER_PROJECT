# Cài đặt & Chạy

## Yêu cầu
- Máy A: Windows + WSL2 (Ubuntu), GPU NVIDIA (CUDA passthrough), 16GB VRAM.
- Máy B: Windows (VTube Studio + OBS).
- Python 3.10+ (khuyên 3.11).

## 1. Chạy khung với MOCK (không cần GPU/model)
```bash
pip install -r requirements.txt          # tối thiểu cần pyyaml
python main.py                            # demo tự nói + chat + barge-in
python -m tests.test_barge_in             # test ngắt lời
python -m tests.test_persona              # test persona + emotion parser
```

## 2. Bước A — LLM thật (vLLM + Qwen3.5-9B) trong WSL2
```bash
# Trong WSL2:
pip install vllm                          # cần CUDA; xem docs vLLM
vllm serve Qwen/Qwen3.5-9B \
  --port 8000 --max-model-len 16384 \
  --reasoning-parser qwen3
# rồi thay MockLLM -> VLLMClient trong main.py
```
> TẮT thinking mode khi live. Strip `<think>...</think>` nếu model lỡ sinh.

## 3. Bước B — TTS (GPT-SoVITS hoặc Kokoro)
Clone repo TTS riêng, load model, cắm vào `modules/outputs/tts_engine.py`.
Bắt buộc producer-consumer + text normalization + stop() cho barge-in.

## 4. Bước C — Discord
- Tạo bot ở Discord Developer Portal, **BẬT Message Content Intent**.
- Điền token vào `.env` (copy từ `.env.example`).
- Thay `fake_chat` -> `DiscordInput`.

## 5. Bước D — VTube Studio (máy B)
- Bật API port 8001. Đặt hotkey biểu cảm khớp tên trong `emotion.py`.

## 6. Bước E — Memory + Reflection
- Chạy Qdrant (docker). Cắm `MemoryStore` + `Reflection`.

## Bảo mật
- KHÔNG commit `.env`, token, model. `.gitignore` đã chặn sẵn.
```
