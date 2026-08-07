# Mai — AI VTuber

Một AI VTuber tiếng Việt **tự điều hành buổi stream**: không chỉ đáp chat mà **chủ động dẫn dắt** — nhặt tin đáng đáp, cưỡi sóng chat, tự nói khi nguội, chuyển "phân cảnh", và có cảm xúc phản ứng theo sự kiện thật (donation, troll, im lặng).

> Chạy **100% local** trên 1 GPU (RTX 5060 Ti). Không gọi API ngoài.

## Kiến trúc

```
Chat (YouTube/Discord) ─► SaliencePool (điểm + decay + cluster)
                          ChatPulse   (đo độ sôi nổi)
                              │
                          Director loop ─► quyết định: read_chat / self_talk /
                              │             ack_donation / transition
                              ▼
   Emotion (appraisal → mood engine) ─► mood_style ─► LLM (llama.cpp) ─► TTS (VieNeu)
```

- **Director** biến Mai từ *reactive chatbot* → *host* chủ động (không đáp mọi tin FIFO).
- **Emotion**: sự kiện → appraisal rule-based → mood engine (spring-damper) → chỉ dẫn giọng bằng chữ (bực nói cộc, vui nói lầy…).
- **Data pipeline**: mọi turn tự log → sẵn sàng fine-tune (SFT + DPO) khi đủ data.

## Stack

| Thành phần | Công nghệ |
|---|---|
| LLM | llama.cpp (llama-server) · Gemma 4 12B Q4_K_M |
| TTS | VieNeu-TTS v3 (giọng Việt, clone) |
| Memory | SQLite + sqlite-vec + bge-m3 |
| Backend | Python 3.11 async · FastAPI dashboard |
| Nền tảng | Windows 11 |

## Cấu trúc repo

```
v1.0/                    # codebase chính (versioned)
  orchestrator/          # Director, emotion, autonomy, state machine
  services/              # llm · tts · emotion · director · memory · filter · input
  dashboard/             # FastAPI + WebSocket UI (metrics, mood, Review)
  scripts/               # cli.py, stream_youtube.py, stream_discord.py, export_dataset.py
  config/                # *.yaml (config-over-code)
  tests/                 # ~1000+ unit + integration
  docs/dev_manual/       # tài liệu kỹ thuật canonical
```

## Chạy nhanh

```powershell
# 1. Bật llama-server (cửa sổ riêng, để yên)
llama-server.exe -m gemma_4_12B_Q4.gguf -c 4096 --port 8080 --flash-attn on --reasoning off

# 2. Cài + chạy
cd v1.0
python -m venv venv; .\venv\Scripts\Activate.ps1
pip install -r requirements.txt

# CLI test (gõ tay)
python scripts\cli.py --autonomy --tts --dashboard

# Live stream
python scripts\stream_youtube.py --video VIDEO_ID --tts --dashboard
```

Dashboard: http://127.0.0.1:7860

## Tài liệu

- `v1.0/docs/dev_manual/` — kiến trúc, modules, vận hành, mở rộng (canonical)
- `v1.0/STATE.md` — trạng thái phát triển hiện tại
- `v1.0/docs/ROADMAP_AUTONOMOUS_HOST.md` — lộ trình "reactive → host"

## Nguyên tắc

Config-over-code · interface-based · fail-safe · test theo phase · không hardcode magic number.

---
*Dự án cá nhân, đang phát triển.*
