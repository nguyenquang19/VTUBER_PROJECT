# 🎀 AI VTuber (V1 — Discord)

AI VTuber tiếng Việt tên **Mai**, chạy local: nhận chat từ Discord, trả lời bằng **giọng nói** phát thẳng vào kênh voice Discord. Mai có **tâm trạng** riêng và biết **tự lên tiếng** khi phòng im. Kiến trúc chia node, đo được độ trễ đầu-cuối ~1 giây trên GPU.

> **Trạng thái:** Pipeline chính chạy thật được (text → LLM → giọng nói → Discord voice). Đã thêm hệ cảm xúc + luồng tự nói (node6). Phần an toàn (kiểm duyệt, crisis) **chưa hoàn thiện** — chưa mở cho người ngoài.

## ✨ Đặc điểm

- 🗣️ **Trả lời bằng giọng nói tiếng Việt** — TTS phát trực tiếp vào kênh voice Discord.
- 🎭 **Có tâm trạng** — 5 chiều (vui / buồn / bực / bồn_chồn / ngượng), model tự chấm, ảnh hưởng cách nói và nhịp nói. Nhớ qua buổi (SQLite).
- 💬 **Tự lên tiếng (node6)** — phòng im lâu, có người mới vào... Mai tự chen một câu. Nhịp nói do cảm xúc điều khiển, không theo luật cứng.
- ⚡ **Độ trễ thấp** — end-to-end ~1s (LLM chiếm ~98%), các khâu khác <25ms.
- 🧹 **Lọc nhiễu + đọc không khí** — bỏ qua emoji/"haha" đơn lẻ, nhưng vẫn đếm nhịp phòng để biết lúc nào nên nói.
- 🎯 **Hàng đợi ưu tiên theo điểm** — crisis > nhắc tên Mai > chat thường > tự nói; điểm đổi theo thời gian chờ.
- 🧠 **Nhớ ngữ cảnh** — lịch sử buổi + hồ sơ người dùng lâu dài, có quản lý ngân sách context để không tràn.
- 🚨 **Phát hiện khủng hoảng** — nhận diện tin nhạy cảm, ưu tiên xử lý, ngắt lời tức thì.
- 🩺 **Healthcheck** — phát hiện LLM chết trong ~5s, trả lời dự phòng thay vì treo.
- 📊 **Dashboard TUI + log lượt** — theo dõi trạng thái/hàng đợi/timing; ghi lại từng lượt (prompt + mood) để soi lại.

## 🏗️ Kiến trúc

Pipeline chia node, giao tiếp qua TCP:

```
Discord text
   ↓
node1_ingestion   nhận tin, lọc nhiễu, rate-limit (user + phòng), chấm điểm,
                  đếm nhịp phòng; phát .wav vào kênh voice Discord
   ↓
node2_core        state machine, hàng đợi ưu tiên theo ĐIỂM (crisis > chat > auto),
                  gọi LLM, TÁCH MOOD khỏi câu nói, kiểm duyệt, cắt câu,
                  quản lý ngân sách context
   ↓
node3_audio       TTS (edge-tts) → .wav + .vtt, báo node1 phát
   ↓
(node1 phát .wav vào kênh voice Discord qua FFmpeg)

node6_idle        TỰ NÓI: đọc mood + nhịp phòng, tính "mức muốn nói", đẩy lượt
                  AUTO vào node2. KHÔNG gọi LLM, KHÔNG tới node3 -> mọi câu vẫn
                  đi qua đúng một cửa kiểm duyệt ở node2.
node4_timing      gom timing từng chặng (t0→t9), ghi data/timing.jsonl
node4_dashboard   TUI hiển thị trạng thái + skip
node5_memory      hồ sơ người dùng lâu dài (SQLite)
shared/           contracts (payloads, timing, events) + utils
```

**Nguyên tắc cốt lõi:** code chỉ **đếm và tính** (dữ kiện, điểm, mức muốn nói); model **cảm nhận và quyết** (tâm trạng, lời nói). Mọi đường ra loa đi qua **một cửa** (node2 + moderation) — kể cả lượt tự nói, không có đường tắt.

## 🎭 Tâm trạng & tự nói

- **Mood** (`node2_core/mood.py`): model trả về khối `[vui:N buồn:N bực:N bồn_chồn:N ngượng:N]` cuối mỗi câu; code tách ra, cất vào RAM + SQLite (dự phòng) + jsonl (lịch sử). Không tự tăng/giảm — chỉ báo "cách đây bao lâu" để model tự quyết còn giữ bao nhiêu.
- **Tự nói** (`node6_idle/`): mỗi vài giây tính **urge** (mức muốn nói) từ dữ kiện (chat im bao lâu, có ai mới vào, chat có đang sôi...) so với **ngưỡng** do mood điều khiển (bồn_chồn/vui hạ ngưỡng, buồn/ngượng nâng ngưỡng). Vượt ngưỡng thì đẩy một lượt AUTO. Có **phanh cứng** `MAX_AUTO_STREAK` phòng khi hệ cảm xúc hỏng.

## 🧰 Công nghệ

- **LLM:** llama.cpp (`llama-server`) + Gemma 12B Q4 (GGUF), chạy GPU.
- **TTS:** edge-tts (giọng `vi-VN-HoaiMyNeural`); Piper offline làm dự phòng.
- **Voice:** discord.py + davey + FFmpeg (phát .wav vào kênh voice).
- **Memory:** SQLite (node5, mood).

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

Gõ tin trong kênh text Discord → nghe Mai trả lời trong kênh voice. Để yên một lúc → Mai tự chen chuyện.

> **Tắt tự nói:** `NODE6_ENABLED=0 bash scripts/run_e2e.sh`. Nên tắt khi mở cho người ngoài tới khi phần kiểm duyệt (Phần 7) hoàn thiện — model đang là bản uncensored, lớp kiểm duyệt là tất cả.

## ⚙️ Cấu hình chính (.env)

| Biến | Ý nghĩa |
| --- | --- |
| `DISCORD_TOKEN` | token bot Discord |
| `DISCORD_CHANNEL_ID` | ID kênh text |
| `DISCORD_VOICE_CHANNEL_ID` | ID kênh voice (phải khác kênh text) |
| `LLM_HOST` | địa chỉ llama-server (mặc định http://localhost:8080) |
| `LLM_N_PREDICT` | giới hạn token trả lời (~200, đủ cho cả câu nói + khối mood) |
| `TTS_VOICE` | giọng edge-tts (`vi-VN-HoaiMyNeural`) |
| `CTX_BUDGET_CHARS` | ngân sách ký tự toàn prompt (mặc định 10000, chỉnh nếu đổi `-c`) |
| `NODE6_ENABLED` | bật/tắt luồng tự nói (mặc định `1`) |
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

Hiện **98 pass / 1 fail có chủ đích** — `test_crisis_accuracy` còn đỏ vì bộ từ khoá crisis mới là khung mồi, cần con người điền (xem phần an toàn dưới).

Test theo mảng: `test_node1_ingestion`, `test_noise_filter`, `test_rate_limiter_room`, `test_node2_core`, `test_queue_scoring`, `test_mood`, `test_context_budget`, `test_urge`, `test_node6_loop`, `test_auto_turn`, `test_no_moderation_bypass`, `test_room_state`, `test_node3_audio`, `test_timing_merge`, `test_e2e_wiring`, `test_crisis`, `test_dashboard`, `test_health`, `test_session_memory`, `test_node5_memory`, `test_node5_integration`.

## ⚠️ Trước khi mở beta (an toàn — quan trọng hơn code)

Model đang dùng là **bản uncensored**, đã tháo hết phanh có sẵn — toàn bộ gánh nặng an toàn nằm ở lớp kiểm duyệt. **Không mở cho người lạ khi phần này chưa xong:**

- Điền bộ từ khoá crisis thật trong `node2_core/crisis/keywords.py`.
- Mở rộng bộ test crisis (`data/crisis_test/`) lên vài trăm câu (hiện quá ít cho self-harm).
- Điền danh sách chặn thật trong `node2_core/moderation.py` và `node5_memory/moderation.py`.
- Định nghĩa quy trình xử lý khi có cảnh báo crisis (ai theo dõi `data/crisis_review.jsonl`, Mai nói gì).
- Chạy thật vài buổi để chỉnh các con số của mood/urge (hiện là số khởi điểm).

## 🗺️ Còn lại

- **node6 hoàn thiện:** nối "còn nữa" (kể chuyện dài nhiều lượt), danh sách người trong voice (Mai đang "bị điếc" với ai nói bằng mồm).
- Avatar / Live2D (frontend tách rời).
- Chuyển sang Twitch/YouTube.
- Semantic memory / skills.

## 📜 License

MIT (code). Model, giọng nói, asset bên thứ ba có license riêng.
