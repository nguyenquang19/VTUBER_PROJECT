# 🎀 AI VTuber (V1 — Discord)

AI VTuber tiếng Việt chạy local: nhận chat từ Discord, trả lời bằng **giọng nói** phát thẳng vào kênh voice Discord. Kiến trúc chia node, đo được độ trễ đầu-cuối ~1 giây trên GPU.

> **Trạng thái:** Pipeline chính chạy thật được (text → LLM → giọng nói → Discord voice). Độ trễ end-to-end ~1s. Đang chuẩn bị phần an toàn trước khi mở beta.

## ✨ Đặc điểm

- 🗣️ **Trả lời bằng giọng nói tiếng Việt** — TTS phát trực tiếp vào kênh voice Discord.
- ⚡ **Độ trễ thấp** — end-to-end ~1s (LLM chiếm ~98%), các khâu khác <25ms.
- 🧠 **Nhớ ngữ cảnh** — nhớ trong buổi (session) + hồ sơ người dùng lâu dài.
- 🚨 **Phát hiện khủng hoảng** — nhận diện tin nhắn nhạy cảm, ưu tiên xử lý, ngắt lời tức thì.
- 🩺 **Healthcheck** — phát hiện LLM chết trong ~5s, trả lời dự phòng thay vì treo.
- 📊 **Dashboard TUI** — theo dõi trạng thái, hàng đợi, timing; nút skip.

## 🏗️ Kiến trúc

Pipeline chia node, giao tiếp qua TCP:

```
Discord text
   ↓
node1_ingestion   nhận tin, rate-limit, chấm điểm, phát voice vào Discord
   ↓
node2_core        state machine, hàng đợi ưu tiên (crisis > chat > auto),
                  gọi LLM, kiểm duyệt, cắt câu
   ↓
node3_audio       TTS (edge-tts) → .wav + .vtt, báo node1 phát
   ↓
(node1 phát .wav vào kênh voice Discord qua FFmpeg)

node4_timing      gom timing từng chặng (t0→t9), ghi data/timing.jsonl
node4_dashboard   TUI hiển thị trạng thái + skip
node5_memory      hồ sơ người dùng lâu dài (SQLite)
shared/           contracts (payloads, timing, events) + utils
```

## 🧰 Công nghệ

- **LLM:** llama.cpp (`llama-server`) + Gemma 12B Q4 (GGUF), chạy GPU.
- **TTS:** edge-tts (giọng `vi-VN-HoaiMyNeural`); Piper offline làm dự phòng.
- **Voice:** discord.py + davey + FFmpeg (phát .wav vào kênh voice).
- **Memory:** SQLite (node5).

## 🚀 Chạy

Yêu cầu: Python 3.12, GPU (khuyến nghị), FFmpeg, `llama-server`, model GGUF trong `models/`.

```bash
# 1. môi trường
python -m venv env
./env/bin/pip install -r requirements.txt

# 2. cấu hình
cp .env.example .env        # điền DISCORD_TOKEN, DISCORD_VOICE_CHANNEL_ID, ...

# 3. chạy model (terminal riêng)
llama-server --model models/gemma_4_12B_Q4.gguf --port 8080 \
  -c 4096 --parallel 2 -ngl 99 --jinja

# 4. chạy hệ thống (terminal khác)
bash scripts/run_e2e.sh

# 5. dashboard (tuỳ chọn)
PYTHONPATH=. ./env/bin/python -m node4_dashboard.tui
```

Gõ tin trong kênh text Discord → nghe Mai trả lời trong kênh voice.

## ⚙️ Cấu hình chính (.env)

| Biến | Ý nghĩa |
| --- | --- |
| `DISCORD_TOKEN` | token bot Discord |
| `DISCORD_CHANNEL_ID` | ID kênh text |
| `DISCORD_VOICE_CHANNEL_ID` | ID kênh voice (phải khác kênh text) |
| `LLM_HOST` | địa chỉ llama-server (mặc định http://localhost:8080) |
| `LLM_N_PREDICT` | giới hạn token trả lời (80–128, giữ câu ngắn) |
| `TTS_VOICE` | giọng edge-tts (`vi-VN-HoaiMyNeural`) |
| `TIMING_COLLECTOR_PORT` | cổng collector timing (8810) |

## 📊 Timing

Sau khi chạy, xem độ trễ:

```bash
cat data/timing.jsonl
PYTHONPATH=. ./env/bin/python scripts/timing_summary.py
```

Ví dụ thực tế (GPU): end-to-end ~900–1150ms, trong đó LLM ~880–1120ms (~98%). Turn có câu trả lời dài sẽ chậm hơn — kiểm soát bằng `LLM_N_PREDICT` và prompt ép câu ngắn.

## ✅ Test

```bash
PYTHONPATH=. ./env/bin/python -m pytest tests/ -v
# hoặc
bash scripts/test_all.sh    # syntax check + import check + pytest
```

Test theo node: `test_node1_ingestion`, `test_node2_core`, `test_node3_audio`, `test_timing_merge`, `test_e2e_wiring`, `test_crisis`, `test_dashboard`, `test_health`, `test_session_memory`, `test_node5_memory`, `test_node5_integration`.

## ⚠️ Trước khi mở beta (an toàn)

Phần này **quan trọng hơn code**, cần hoàn thiện trước khi cho người ngoài dùng:

- Điền bộ từ khoá crisis thật trong `node2_core/crisis/keywords.py`.
- Mở rộng bộ test crisis (hiện quá ít cho self-harm).
- Điền danh sách chặn thật trong `node2_core/moderation.py` và `node5_memory/moderation.py`.
- Định nghĩa quy trình xử lý khi có cảnh báo crisis (ai theo dõi `data/crisis_review.jsonl`, Mai nói gì).

## 🗺️ Còn lại (nhánh phụ)

- Avatar / Live2D (frontend tách rời).
- Chuyển sang Twitch/YouTube.
- Semantic memory / skills.

## 📜 License

MIT (code). Model, giọng nói, asset bên thứ ba có license riêng.
