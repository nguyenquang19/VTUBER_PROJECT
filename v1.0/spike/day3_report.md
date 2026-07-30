# Spike Day 3 Report — STT

**Status:** ⏸️ **SKIPPED / DEFERRED**
**Ngày quyết định:** 2026-07-29
**Người quyết định:** User (owner)

## Lý do skip

User re-prioritize scope MVP: trước mắt Mai chỉ cần **đọc chat + tương tác + TTS**.
Voice input (STT) không nằm trong luồng chính của MVP; sẽ làm gần cuối dự án.

Nguồn trigger thay thế cho input (KHÔNG STT):
- **Chat từ platform** (Twitch/YouTube chat) → xử lý ở Trigger Manager Phase 2 (spec `chat_normal` trigger type — có sẵn)
- **Operator text từ dashboard** → xử lý ở Trigger Manager Phase 2 (spec `operator_voice` sẽ đổi thành `operator_text` tạm; wire STT sau)
- **Ambient talk** (self-triggered) → thresholds sẵn ở Phase 2

## Deviation khỏi CLAUDE.md N3

**N3:** "Phase 0 chỉ bắt đầu SAU khi 3 spike report (day1/2/3) tồn tại trong `spike/`."

**Vi phạm được document:** Day 3 report này tồn tại nhưng ở dạng SKIPPED stub, không có số đo. Điều kiện N3 formal (3 report tồn tại) vẫn thoả về mặt file. Điều kiện semantic (validate STT khả thi trước khi commit architecture) bị hoãn.

**Rủi ro chấp nhận:**
- Đến khi làm Phase 5 (đã đẩy xuống sau Phase 8) mới biết STT tiếng Việt của whisper small có đủ ăn không → có thể cần replan Phase 5
- Nếu phát hiện STT không đủ chất lượng ở Phase 5 → có thể phải PTT (push-to-talk) hoặc bỏ voice input hoàn toàn
- KHÔNG ảnh hưởng Phase 0-4 (foundation → LLM → trigger → filter → TTS) vì các phase này không phụ thuộc STT

**Mitigation:**
- Giữ interface `STTService` trong `interfaces/stt.py` (ARCHITECTURE 7.3) — chỉ implement stub trả `""` ban đầu
- Phase 2 Trigger Manager thiết kế sao cho `operator_voice` là 1 trigger type có sẵn, chỉ chưa có source → wire STT sau không breaking
- Trước khi bắt đầu Phase 5 thực sự → chạy Day 3 spike thật (dùng template code viết sẵn)

## Kế hoạch làm STT sau

Khi nào chạy Day 3 spike thật:
- Trước Phase 5 (voice input) chính thức
- Có thể chạy ngay sau Phase 4 (TTS) khi cần validate end-to-end với voice
- Hoặc gần cuối, chỉ khi user quyết định add STT

Code sẽ viết:
- `spike/day3_stt_accuracy/benchmark.py` — chạy faster-whisper small, đo WER (dùng lib `jiwer`)
- `spike/day3_stt_accuracy/test_audio.py` — nguồn audio: (1) VIVOS test set 60 câu, (2) reuse viXTTS audio làm proxy, (3) user thu mic
- Report với WER per category + Go/No-go (No-go: WER >30% câu tự nhiên → cân nhắc PTT hoặc bỏ voice input)

## Adjusted phase order (từ PROCESS.md gốc)

Original: Bootstrap → Preflight(Day1-3) → Phase 0-9

**Adjusted:**
```
Bootstrap → Preflight(Day1 ✅, Day2 ✅, Day3 ⏸️) → Phase 0 Foundation
  → Phase 1 Core LLM (chat + operator text input)
  → Phase 2 Trigger + State (chat_normal, operator_text, ambient)
  → Phase 3 Filter
  → Phase 4 TTS (viXTTS)
  → Phase 6 Animation (skip Phase 5 STT)
  → Phase 7 Memory
  → Phase 8 QC + Data
  → Phase 9 Fine-tune (LLM + optionally viXTTS)
  → [Later] Phase 5 STT — chạy Day 3 spike trước, rồi implement
```

Update sẽ vào `STATE.md`. Không update PROCESS.md gốc (spec) để giữ nguyên intent, chỉ note deviation.
