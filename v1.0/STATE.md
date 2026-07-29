# STATE — Mai project

**Phase hiện tại:** -1 done → next: Pre-flight
**Task đang làm:** (none — chờ user "tiếp" để bắt đầu Pre-flight Day 1)
**Cập nhật:** 2026-07-29

## Tiến độ phase hiện tại
(none — chờ Pre-flight)

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
- Spike Day 2 chốt TTS: <điền sau Pre-flight>
- TTFT P50 thực đo: <điền sau Pre-flight Day 1>
- E4B model: **BỎ** (v2.3, Appendix C) — chỉ 1 instance 12B port 8080
- Nếu Pre-flight Day 1 tight VRAM → xem xét thêm E4B sau

## Next
Chờ user gõ "tiếp" → Pre-flight Day 1 (LLM latency benchmark, spike/day1_llm_latency/).
