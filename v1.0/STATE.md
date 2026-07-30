# STATE — Mai project

**Phase hiện tại:** Phase 0 Foundation
**Task đang làm:** 0.C DONE → next 0.D (trigger + fallback skeleton)
**Cập nhật:** 2026-07-30

## Tiến độ Phase 0 (6 milestone)
- [x] **0.A Config + Logger** — 40 test pass
  - `config/system.yaml`, `models.yaml` (số Pre-flight đã điền), `logging.yaml`, `features.yaml`
  - `orchestrator/config_loader.py` — dotted access, atomic reload, watchdog hot-reload
  - `orchestrator/logger.py` — structlog + JSONL (events/turns) + rotation
  - `pytest.ini`
- [x] **0.B Interfaces + Feature registry** — 91 test pass (tổng 131)
  - `interfaces/base.py` (Service ABC + HealthStatus), `input.py`, `stt.py` (+NullSTTService stub),
    `llm.py`, `filter.py`, `tts.py`, `animation.py` (MoodState 5 mood), `memory.py`
  - `orchestrator/features.py` — FeatureManager: 6 toggle rule (atomic/log/dependency/conflict/resource/rollback)
  - `config/system.yaml` thêm `resources.*` (VRAM budget 5594MB) + `features.core` (7 core feature)
- [x] **0.C Event bus + State machine** — 71 test pass (tổng 202)
  - `orchestrator/event_bus.py` — asyncio pub/sub fan-out, bounded queue, drop_oldest/drop_newest, TOPIC_ALL
  - `orchestrator/state_machine.py` — AsyncMachine 5 state / 9 transition, action hook, cooldown timer
  - `config/state_machine.yaml` — cooldown 500ms, interrupt_policy + watchdog threshold (Phase 2 dùng)
  - 5 hypothesis property test: state luôn valid, emergency_stop từ mọi state → PAUSED,
    resume → IDLE, history liên tục, SPEAKING chỉ vào từ THINKING
- [ ] **0.D Trigger + Fallback skeleton** — 4 type, 2 level, `config/triggers.yaml`
- [ ] **0.E SQLite migration** — `migrations/001_initial.sql` + runner
- [ ] **0.F Metrics + Dashboard + Emergency stop** — FastAPI + WS + Chart.js, Ctrl+Shift+X

## Pre-flight (DONE)
- [x] Day 1 LLM latency — GO (TTFT cold 444ms, decode min 40tps, max temp 63°C)
- [x] Day 2 TTS — Piper REJECT, viXTTS GO (cond_len=30, VRAM 1.79GB, 2600ms avg)
- [x] Day 3 STT — SKIPPED (user scope decision, xem `spike/day3_report.md`)

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
