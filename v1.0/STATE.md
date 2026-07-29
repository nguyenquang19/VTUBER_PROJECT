# STATE — Mai project

**Phase hiện tại:** Pre-flight
**Task đang làm:** Day 2 DONE (GO viXTTS) → next: Day 3 STT
**Cập nhật:** 2026-07-29 22:45

## Tiến độ phase hiện tại
- [x] Day 1 LLM latency — GO (TTFT cold 444ms, decode min 40tps, max temp 63°C)
- [x] Day 2 TTS — Piper 3 voice REJECT, viXTTS GO với baseline config
  - Primary: viXTTS (cond_len=30, num2words vi cleaner, VRAM 1.79GB, 2600ms avg)
  - Fallback: subtitle overlay
  - Known issues (train lại sau, không blocking): English words lai; ref audio cần thu riêng cho persona
- [ ] Day 3 STT (faster-whisper small)
- [ ] Update `config/models.yaml`, Section 1.1 latency, Appendix C

## Phase đã xong
- [x] Phase -1 Bootstrap
  - [x] `.gitignore` (plain UTF-8)
  - [x] `requirements.txt` (UTF-8, đủ package theo ARCHITECTURE 13.1)
  - [x] Cây thư mục khớp Appendix A
  - [x] venv activate được, pip install -r requirements.txt xong
  - [x] `STATE.md` tồn tại
  - [x] `.env.example`, `README.md`
  - [x] Commit "phase-1: bootstrap repo structure"

## Blocker / cần user verify trên máy thật (Appendix D)
- [ ] `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
- [ ] `LongPathsEnabled` = 1 trong registry
- [ ] `nvidia-smi` chạy được, VRAM idle < 500MB
- [ ] llama.cpp build với `GGML_CUDA=ON`, `llama-server.exe` chạy từ `.\build\bin\Release\`
- [ ] Model `gemma-4-12b-Q4_K_M.gguf` đã tải về `models\llm\`
- [ ] Windows Defender exception cho folder `llama.cpp\build\`
- [ ] Python chạy với quyền Administrator (cho `keyboard` hook toàn cục)

## Ghi chú
- Spike Day 2 chốt TTS: **viXTTS** (config trong `spike/day2_report.md`)
- TTFT P50 thực đo: <điền sau Pre-flight Day 1>
- E4B model: **BỎ** (v2.3, Appendix C) — chỉ 1 instance 12B port 8080
- Nếu Pre-flight Day 1 tight VRAM → xem xét thêm E4B sau
- ⚠️ **Day 1 finding:** llama-server đang stream `delta.reasoning_content`
  cho model Gemma 4 12B (không phải `delta.content`). Có thể do chat template
  GGUF hoặc llama-server version. Phase 1 parser cần handle: nếu output có
  reasoning tags, extract phần answer thật (bỏ reasoning) trước khi parse
  mood block. Chi tiết verify sau Day 1.

## Next
Chờ user gõ "tiếp" → Pre-flight Day 1 (LLM latency benchmark, spike/day1_llm_latency/).
