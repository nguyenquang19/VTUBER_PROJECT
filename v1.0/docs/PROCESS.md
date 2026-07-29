# PROCESS.md — Quy trình xây dựng Mai từ đầu đến Phase 9

> Đây là kịch bản để Claude Code chạy **tự động, tuần tự**. Mỗi phase: Mục tiêu → Việc phải làm → Definition of Done → CHECKPOINT (điểm dừng bắt buộc để user duyệt).
>
> **Luật chạy autonomous:**
> - Làm tuần tự. KHÔNG nhảy phase.
> - Hết mỗi phase → dừng ở CHECKPOINT, cập nhật `STATE.md`, chờ user gõ "tiếp" mới sang phase sau.
> - Trong 1 phase: tự chạy hết các task, tự viết test, tự sửa đến khi test xanh.
> - Gặp CHECKPOINT ⛔ giữa phase (spike No-go, spec mâu thuẫn, đụng ranh giới persona) → dừng ngay, báo user.
> - Đọc `CLAUDE.md` để biết rules. Đọc `ARCHITECTURE.md` section tương ứng trước mỗi phase.

---

## PHASE -1: BOOTSTRAP (làm 1 lần, trước tất cả)

**Mục tiêu:** Có repo trống đúng cấu trúc + môi trường chạy được.

**Việc:**
1. `git init`, tạo `.gitignore` (models/*.gguf, .env, backups/, logs/, __pycache__/, venv/, *.db, cache/*.bin).
2. Tạo cây thư mục theo ARCHITECTURE Appendix A (config/, interfaces/, orchestrator/, services/, dashboard/, scripts/, tests/, spike/, migrations/, data/, logs/, models/, docs/).
3. `python -m venv venv`, viết `requirements.txt` (ARCHITECTURE 13.1), cài đặt.
4. Tạo `STATE.md` (format ở cuối file này) — ghi "Phase: -1 done, next: Pre-flight".
5. Copy `ARCHITECTURE.md`, `QUICKSTART.md`, `persona.md`, `CLAUDE.md`, `PROCESS.md` vào `docs/` (hoặc giữ ở root, tuỳ — miễn Claude Code đọc được).
6. Windows setup checklist (ARCHITECTURE Appendix D) — nếu chạy trên máy user: verify. Nếu không: ghi chú "cần user verify trên máy thật".

**DoD:**
- [ ] `venv` activate được, `pip list` có đủ package chính
- [ ] Cây thư mục khớp Appendix A
- [ ] `git log` có commit đầu "phase-1: bootstrap repo structure"
- [ ] `STATE.md` tồn tại

**⛔ CHECKPOINT B0:** Báo user "repo sẵn sàng, bắt đầu Pre-flight?". Chờ xác nhận.

---

## PRE-FLIGHT (Spike Day 1-3, bắt buộc — ARCHITECTURE Section 0)

> ⚠️ N3: Phase 0 KHÔNG được bắt đầu khi thiếu 3 spike report. Đây không phải formality — số từ spike quyết định config thật.

### Spike Day 1 — LLM Latency (ARCHITECTURE 0.2)
**Việc:** Viết `spike/day1_llm_latency/benchmark.py`, chạy 5 scenario (cold/warm short/medium/long/overheating) với llama-server + Gemma 12B. Ghi `spike/day1_report.md` (bảng số thực + Go/No-go).
**No-go ⛔:** TTFT cold >1s HOẶC decode <30 tok/s HOẶC throttle >30%. → DỪNG, báo user, không tự đổi model.

### Spike Day 2 — TTS Vietnamese (ARCHITECTURE 0.3)
**Việc:** Test Piper / XTTS v2 / viXTTS / F5-TTS với 10 câu mẫu. Điền decision matrix. Ghi `spike/day2_report.md` + chọn primary + fallback.
**No-go ⛔:** Tất cả <6/10 quality. → DỪNG, báo user (options: chấp nhận thấp / phá 100%-local dùng edge-tts / fine-tune TTS).

### Spike Day 3 — STT Accuracy (ARCHITECTURE 0.4)
**Việc:** Test faster-whisper small với 40 câu (script/natural/noisy/punctuation). Tính WER. Ghi `spike/day3_report.md`.
**No-go ⛔:** WER >30% câu tự nhiên. → DỪNG, báo user (options: PTT / restrict câu ngắn).

### Sau Pre-flight (ARCHITECTURE 0.6)
- Cập nhật `config/models.yaml` với model/TTS đã chọn thật.
- Cập nhật Section 1.1 latency target với số đo thật.
- Cập nhật Appendix C trade-off log (dòng TTS primary).

**DoD Pre-flight:**
- [ ] 3 report tồn tại trong `spike/`
- [ ] Tất cả 3 spike = Go (không có No-go chưa giải quyết)
- [ ] `models.yaml` có TTS provider thật (không còn "TBD")

**⛔ CHECKPOINT PF:** Báo user tổng hợp 3 spike + số thật. Chờ "tiếp" mới vào Phase 0.

---

## PHASE 0: Foundation (ARCHITECTURE 11.1)

**Mục tiêu:** Hạ tầng chạy được TRƯỚC mọi feature. Không có LLM thật ở phase này — toggle/metric/state đều dùng giả.

**Việc (theo thứ tự):**
1. `interfaces/base.py` — abstract `Service` (start/stop/health_check/get_metrics).
2. `orchestrator/features.py` — Feature registry (ARCHITECTURE 4.2).
3. `orchestrator/config_loader.py` — load + hot-reload yaml (watchdog).
4. Logger (structlog + JSONL) + `config/logging.yaml`.
5. `orchestrator/event_bus.py` — asyncio queue pub/sub.
6. SQLite schema init + `migrations/001_initial.sql` + `migration_runner.py` (ARCHITECTURE 8.8).
7. `orchestrator/state_machine.py` — 5 states, 9 transitions (ARCHITECTURE 7.10). Dùng lib `transitions`.
8. `orchestrator/trigger_manager.py` — skeleton (4 types, priority queue) (ARCHITECTURE 7.9.2).
9. `orchestrator/fallback_manager.py` — skeleton 2-level (ARCHITECTURE 8.7.7).
10. `orchestrator/metrics_collector.py` — prometheus_client, 3 metric giả.
11. `dashboard/` — FastAPI + WebSocket + HTML + Chart.js. Tabs: toggle giả, metric giả, state machine, triggers.
12. Emergency stop hotkey Ctrl+Shift+X (`keyboard` lib, admin).
13. Test: state machine property-based (hypothesis), config reload, emergency stop.

**DoD (ARCHITECTURE 11.1):**
- [ ] Dashboard mở ở localhost, toggle giả bật/tắt được
- [ ] Metric giả cập nhật realtime trên chart
- [ ] Emergency stop Ctrl+Shift+X → PAUSED từ mọi state
- [ ] State transitions log được
- [ ] Config reload không cần restart
- [ ] Không memory leak sau 1h idle
- [ ] Test phase 0 xanh

**⛔ CHECKPOINT P0.**

---

## PHASE 1: Core LLM (ARCHITECTURE 11.2)

**Mục tiêu:** Mai text-only qua CLI. Chưa TTS, chưa voice.

**Việc:**
1. `services/llm/process_manager.py` — spawn 2 llama-server (main 8080 + shared_e4b 8082), wait healthy.
2. `services/llm/llama_cpp_llm.py` — `LlamaCppLLMService` streaming qua httpx (ARCHITECTURE 8.2).
3. `services/llm/prompt_manager.py` + `prompt_cache.py` — persona prefix cache.
4. `config/prompts/` — system prompt từ `persona.md` Phần A+C.
5. Parser: regex + Pydantic, extract text + mood block. Chấp nhận key có/không dấu (persona.md ghi chú). Fallback nếu sai format.
6. CLI input mode.
7. LLM fallback chain 2-level (primary → canned response theo mood).
8. Dashboard hiện TTFT/decode realtime.
9. Test: parse >95% format đúng, fallback khi force timeout, 100 turn không crash.

**DoD (ARCHITECTURE 11.2):**
- [ ] Gõ CLI, Mai trả lời **đúng chất persona** qua 20 turn (subjective — user duyệt)
- [ ] Mood block parse thành công >95%
- [ ] TTFT P50 < target (số từ Pre-flight)
- [ ] Không crash sau 100 turn
- [ ] Fallback triggered khi force timeout
- [ ] Dashboard TTFT/decode realtime

> ⚠️ Pitfall (ARCHITECTURE 2.3): persona v1 SẼ tệ, đó là expected. KHÔNG tune persona ở đây — chạy đủ 100+ turn trước. Chỉ sửa nếu parse fail hoặc vi phạm ranh giới, không sửa vì "chưa hay".

**⛔ CHECKPOINT P1** (user duyệt persona subjective — quan trọng).

---

## PHASE 2: Trigger + State Machine (ARCHITECTURE 11.3)

**Mục tiêu:** Turn-taking logic hoàn chỉnh. 4 trigger types (N1 — KHÔNG 12).

**Việc:**
1. Trigger Manager full: classify 4 type, priority queue, spam detection, rate limit chat_normal.
2. State machine full + interrupt policy (ARCHITECTURE 7.9.3) + deadlock watchdog (7.10.4).
3. Ambient talk trigger (threshold cứng 60s, KHÔNG probability — N1).
4. Dashboard: Triggers tab + State Machine tab.
5. Test: Trigger Manager (12.7) + State Machine (12.6) + Integration (12.8 — quan trọng nhất, race/timing).

**DoD (ARCHITECTURE 11.3):**
- [ ] Spam 60 tin/phút → Mai không respond tất cả
- [ ] Priority đúng: operator_voice > mention > normal
- [ ] State transitions log đầy đủ
- [ ] Ambient talk sau silence >60s
- [ ] Watchdog detect deadlock khi force stuck
- [ ] Integration test 12.8 xanh hết

**⛔ CHECKPOINT P2.**

---

## PHASE 3: Filter (ARCHITECTURE 11.4)

**Mục tiêu:** Rule-based filter + regenerate. Fail-open (N7).

**Việc:**
1. `services/filter/rule_filter.py` — 4 category, patterns từ persona ranh giới (8.3A).
2. Regenerate với hint khi persona break.
3. Filter fallback chain fail-open (8.7.4): regex lỗi → allow + log warning.
4. Dashboard filter tab.
5. (Optional) `ai_filter.py` gọi shared_e4b port 8082 — chỉ nếu VRAM cho phép.
6. Test: 20 troll → catch >80%, 100 normal → false positive <5%.

**DoD (ARCHITECTURE 11.4):**
- [ ] Troll catch rate >80%
- [ ] False positive <5%
- [ ] Regenerate khi persona break
- [ ] Fail-open khi regex error (log nhưng allow)

**⛔ CHECKPOINT P3.**

---

## PHASE 4: TTS (ARCHITECTURE 11.5) — 4 tuần, rabbit hole

**Mục tiêu:** Text → audio streaming. TTFA là metric số 1.

**Việc:**
1. TTS module theo Pre-flight Day 2 decision (`services/tts/`).
2. Streaming pipeline: split câu → synthesize → play theo thứ tự, không overlap.
3. Interrupt handling (tích hợp state machine — SPEAKING → COOLDOWN với flag).
4. TTS fallback chain (8.7.3): primary → subtitle overlay.
5. Test: TTFA P50 <1s, không overlap giữa turns.

**DoD (ARCHITECTURE 11.5):**
- [ ] **Must:** Mai nói được, TTFA P50 <1s
- [ ] **Must:** Không audio overlap giữa turns
- [ ] **Stretch:** Interrupt mượt (fade 200ms)
- [ ] **Stretch:** Subtitle sync
- [ ] Quality subjective >6/10 qua 30 câu

> N4: Stretch goal chỉ làm khi Must xong. Không đánh đổi Must lấy Stretch.

**⛔ CHECKPOINT P4.**

---

## PHASE 5: Voice input (ARCHITECTURE 11.6)

**Mục tiêu:** Ông nói được với Mai.

**Việc:**
1. faster-whisper small streaming STT.
2. Echo cancellation (Mai không nghe chính mình — critical).
3. VAD (voice activity detection).
4. STT fallback chain (8.7.2): GPU fail → treat as silence + log.
5. Test: WER <20%, không loop, chunk <300ms delay.

**DoD (ARCHITECTURE 11.6):**
- [ ] Câu bình thường WER <20%
- [ ] Không loop
- [ ] VAD đúng lúc
- [ ] STT chunk → LLM <300ms

**⛔ CHECKPOINT P5.**

---

## PHASE 6: Animation (ARCHITECTURE 11.7)

**Mục tiêu:** Mai có biểu cảm mood. Dashboard chuyển sang Alpine.js.

**Việc:**
1. VTube Studio API integration (`services/animation/`).
2. Mood → expression mapping (5 mood chính).
3. Sync với TTS.
4. Animation fallback (8.7.5): VTS disconnect → auto-reconnect backoff → static image.
5. Dashboard: thêm Alpine.js cho review UI.
6. Test: 5 mood có expression, auto-reconnect.

**DoD (ARCHITECTURE 11.7):**
- [ ] **Must:** 5 mood có expression
- [ ] **Must:** Auto-reconnect khi VTS disconnect
- [ ] **Stretch:** Smooth interpolation
- [ ] **Stretch:** Lip-sync

**⛔ CHECKPOINT P6.**

---

## PHASE 7: Memory (ARCHITECTURE 11.8)

**Mục tiêu:** Mai nhớ dài hạn.

**Việc:**
1. `migrations/004_add_memory_tables.sql` (backup trước — 8.8.3).
2. SQLite + sqlite-vec + bge-m3 embedding (CPU).
3. Semantic memory service, 150ms timeout.
4. Working memory (deque 20).
5. Memory fallback (8.7.6): timeout → working-only (soft fail).
6. Manual inject UI + auto extraction + multi-viewer profile.
7. Test: retrieve <150ms P95, fallback working, callback >80%.

**DoD (ARCHITECTURE 11.8):**
- [ ] Retrieve <150ms P95
- [ ] Fallback working memory khi timeout
- [ ] Manual inject 10 → callback >80%
- [ ] Multi-viewer: nhớ 5 viewer qua sessions

**⛔ CHECKPOINT P7.**

---

## PHASE 8: QC + Data pipeline (ARCHITECTURE 11.9)

**Mục tiêu:** Thu + chấm data cho fine-tune. Rubric = persona.md Phần A-C.

**Việc:**
1. `migrations/005_add_qc_columns.sql`.
2. Persona QC async (chấm mỗi output theo rubric persona).
3. Data collector auto-scoring.
4. Review UI (Alpine.js): approve/reject/edit.
5. Export JSONL cho Unsloth.
6. Test: QC score correlate subjective (spot check 50).

**DoD (ARCHITECTURE 11.9):**
- [ ] Sau 1 tuần chạy: 500+ mẫu ranked
- [ ] Review UI approve/reject/edit hoạt động
- [ ] Export JSONL đúng format Unsloth
- [ ] QC score correlate subjective (50 samples)

**⛔ CHECKPOINT P8.**

---

## PHASE 9: Fine-tune (ARCHITECTURE 11.10)

**Mục tiêu:** Model v2 tốt hơn v1 đo được.

> ⚠️ N2: Toàn bộ training data phải align persona Phần A-C. Loại mọi mẫu vi phạm ranh giới, kể cả nếu "tự nhiên" hơn.

**Việc:**
1. Unsloth setup.
2. Training script (SFT hoặc DPO).
3. Eval script với hold-out test set.
4. Deploy fine-tuned model.
5. A/B test v1 vs v2.

**DoD (ARCHITECTURE 11.10):**
- [ ] Model v2 deploy được
- [ ] Persona QC score > v1 (delta >0.5)
- [ ] Không catastrophic forgetting (test general knowledge)
- [ ] Rollback plan working

**⛔ CHECKPOINT P9 — hoàn thành milestone chính. Sau đây là Phase 10+ iteration không deadline.**

---

## FORMAT STATE.md

Claude Code cập nhật file này sau MỖI task. Đây là nguồn sự thật "đang ở đâu".

```markdown
# STATE — Mai project

**Phase hiện tại:** Phase 1 (Core LLM)
**Task đang làm:** parser regex + pydantic
**Cập nhật:** 2026-07-29 14:30

## Tiến độ phase hiện tại
- [x] process_manager spawn 1 instance (main port 8080)
- [x] llama_cpp_llm streaming
- [ ] parser  ← đang làm
- [ ] CLI mode
- [ ] fallback chain
- [ ] test

## Phase đã xong
- [x] Phase -1 Bootstrap
- [x] Pre-flight (3 spike Go)
- [x] Phase 0 Foundation

## Blocker / cần user
(none)

## Ghi chú
- Spike Day 2 chốt TTS: <điền sau>
- TTFT P50 thực đo: <điền sau> ms
```

---

## TÓM TẮT LUỒNG AUTONOMOUS

```
Bootstrap → [CP B0] → Pre-flight 3 spike → [CP PF]
  → Phase 0 → [CP P0] → Phase 1 → [CP P1] → Phase 2 → [CP P2]
  → Phase 3 → [CP P3] → Phase 4 → [CP P4] → Phase 5 → [CP P5]
  → Phase 6 → [CP P6] → Phase 7 → [CP P7] → Phase 8 → [CP P8]
  → Phase 9 → [CP P9] → done (Phase 10+ iteration)

Tại mỗi [CP]: cập nhật STATE.md, báo user, chờ "tiếp".
Trong phase: tự chạy hết task + test, chỉ dừng nếu ⛔ (No-go / mâu thuẫn / đụng ranh giới).
```
