# STATE — Mai project

**Phase hiện tại:** Phase 1 Core LLM — ĐÃ CODE XONG 6/6 milestone (1.A–1.F)
**Task đang làm:** ⛔ CHECKPOINT P1 — chờ user duyệt persona subjective (20 turn cli.py)
**Cập nhật:** 2026-07-30

## ✅ BLOCKER ĐÃ GIẢI HOÀN TOÀN (2026-07-30) — thủ phạm là httpx buffer, streaming vẫn NHANH
- Model: "Gemma 4 12B It Qat Uncensored Heretic" (uncensored Gemma 4 12B). Reasoning
  là NATIVE của Gemma 4 (Google build sẵn) → mọi bản Gemma 4 đều có, tắt bằng --reasoning off.
- Chẩn đoán cuối (đo thật, cùng prompt, first-CONTENT token):
  - **raw asyncio socket: 72ms** ✅ | curl -N: 283ms | **httpx stream: 2200ms** ✗
  - httpx buffer bất kể iter_lines/bytes/raw, trust_env, gzip. Server stream 69-72ms.
  - "2.4s" TỪ ĐẦU là do httpx buffer phía client, KHÔNG phải model/reasoning.
- Option C (--chat-template gemma) THẤT BẠI: output rác (khoá special token harmony).
- **CHỐT cho Phase 1 (streaming OK, giữ model uncensored):**
  - Server: `--flash-attn on --reasoning off` (bỏ --prompt-cache)
  - Endpoint `/v1/chat/completions`, persona = system message
  - **Streaming qua `asyncio.open_connection` (raw socket stdlib), KHÔNG httpx** → TTFT 72ms
  - httpx CHỈ cho non-stream (health/props)
  - Pipeline: LLM stream TTFT 72ms → tách câu → viXTTS inference_stream (TTFA 450ms)
    → first audio ~0.5s. Vượt target.
  - 1.B PHẢI viết lại: /v1/chat/completions, streaming qua asyncio socket, LLMRequest
    mang messages (system+history).

## Phase 1 milestone (6)
- [x] 1.A process_manager + fix config path — 14 unit + 1 live pass (start/healthy/stop server thật 22.8s)
  - Fix path: binary=E:\BAI_CUA_DUC\llama\llama-server.exe, model=gemma_4_12B_Q4.gguf
  - BỎ --prompt-cache (flag llama-cli, KHÔNG phải server; spec 10.3 nhầm) → dùng cache_prompt request param
  - flash-attn cần "on" (build mới cần [on|off|auto])
- [x] 1.B llama_cpp_llm streaming — VIẾT LẠI xong: /v1/chat/completions + raw
      asyncio socket (KHÔNG httpx) + --reasoning off. 63 unit + 2 live pass.
      Live: TTFT 204ms cold (warm ~72ms), decode 40.7tps, content sạch, cancel OK.
      Interface thêm ChatMessage + LLMRequest.messages + to_messages(). httpx chỉ health.
      config/models.yaml extra_flags thêm --reasoning off.
- [x] 1.C prompt_manager + persona (A+B+C) + prompt_cache — 20 unit pass
      - config/prompts/persona_system.txt (dựng từ persona.md A+B format+C ranh giới)
      - prompt_cache.PromptCache: load+freeze persona, version hash 12 ký tự, as_message()
        (vai trò: giữ prefix byte-ổn định cho KV cache reuse, KHÔNG file --prompt-cache)
      - prompt_manager.PromptManager: build_messages [system+history+user] thuần,
        commit_turn ghi history + trim theo max_history_turns, build_request → LLMRequest
      - models.yaml thêm: persona_prompt_path, max_history_turns=12, temperature=0.85
- [x] 1.D parser — 24 unit pass. services/llm/parser.py: parse_response(raw)->ParsedResponse
      (text + MoodState + reason + continuation + ok + raw). Fail-safe: sai format vẫn
      trả text, ok=False, không raise. Strip <think>/<|token|>. Key mood alternation
      (có/không dấu, space/underscore), clamp 0-10. Parse cả "còn nữa" (Phase 2 dùng sau).
      Né ngoặc vuông ngẫu nhiên trong text (chọn block nhiều mood key nhất).
- [x] 1.E CLI + LLM fallback 2-level — 19 unit pass + live smoke.
      - canned_response.CannedResponder: pick theo dominant mood (config models.yaml
        llm_canned.responses), fail-open pool "..."
      - llm_turn.LLMTurnRunner: đăng ký chain "llm" vào FallbackManager (0.D):
        L0 primary stream+parse, L1 canned; run_turn build→execute→commit history
        (lưu text ĐÃ tách mood block); update canned mood chỉ khi parse ok
      - scripts/cli.py: CLI input mode full stack (interactive + auto), on_token stream
      - models.yaml: llm_canned (timeout_primary_s 5.0, timeout_canned_s 0.1, responses)
      - live: primary stream OK, parse_ok=True, mood dominant hiển thị, TTFT 352ms warm
- [x] 1.F dashboard LLM metrics + integration — 11 unit/integration pass + visual check.
      - MetricsCollector: llm_ttft_seconds(hist), llm_decode_tps, llm_requests_total,
        llm_fallback_total, llm_parse_total{ok/fail}; record_llm_turn() + llm_snapshot()
      - LLMTurnRunner nhận metrics=, tự record sau mỗi turn (best-effort get_metrics)
      - dashboard build_snapshot thêm "llm"; frontend panel LLM (TTFT/decode/turns/
        parse%/fallback) + chart TTFT realtime — visual verify qua browser (parse 93.3%)
      - integration test_phase1_turns: 100 turn no crash + parse 100%, 96% với malformed,
        force-timeout → canned (level 1), history trim ổn định. Full suite 432 pass.

## Phase 0 — HOÀN THÀNH (báo cáo: docs/phase0_report.md)
- 0.A Config+Logger, 0.B Interfaces+Features, 0.C EventBus+StateMachine,
  0.D Trigger+Fallback, 0.E Migration, 0.F Metrics+Dashboard+EmergencyStop,
  0.G Health monitor + leak test
- 331 test pass. DoD 7/7 tick (leak test + live soak 60s RSS phẳng 60→61MB)

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
- [x] **0.D Trigger + Fallback skeleton** — 51 test pass (tổng 253)
  - `interfaces/trigger.py` — 4 TriggerType, Trigger/TriggerDecision/QueueStats
  - `orchestrator/trigger_manager.py` — classify, priority heap, spam, rate limit chat_normal, ambient 60s, TTL prune, overflow drop-lowest
  - `orchestrator/fallback_manager.py` — generic 2-level chain + timeout per level (N1 no circuit breaker)
  - `config/triggers.yaml` — 4 priority, rate limit, spam patterns, ambient threshold
  - Chưa làm (Phase 2): interrupt policy enforce, ambient content gen
- [x] **0.E SQLite migration** — 19 test pass (tổng 272)
  - `migrations/001_initial.sql` — turns, state_transitions, trigger_decisions (+index), IF NOT EXISTS
  - `orchestrator/migration_runner.py` — versioned SQL, numeric order, backup-before (shutil), idempotent, fail→success=0 + retry
  - Rule 8.8.4: chỉ THÊM, không auto-rollback (restore từ backup)
- [x] **0.F Metrics + Dashboard + Emergency stop** — 43 test pass (tổng 315)
  - `orchestrator/metrics_collector.py` — prometheus (TTFA/trigger/state + 3 fake gauge), CollectorRegistry riêng
  - `orchestrator/emergency_stop.py` — Ctrl+Shift+X (keyboard lib, degrade nếu không admin)
  - `dashboard/dashboard_server.py` — FastAPI + WS + REST (toggle/estop/resume/metrics)
  - `dashboard/templates/index.html` + `static/` — vanilla JS + canvas chart (không CDN, 100% local)
  - `orchestrator/main.py` — wiring toàn bộ + uvicorn
  - **Verified live:** server chạy localhost:7860, metric realtime qua WS, toggle OK,
    emergency→PAUSED→resume→IDLE, prometheus counter tăng đúng, hotkey bound (admin)

## ✅ DoD Phase 0 (ARCHITECTURE 11.1)
- [x] Dashboard mở ở localhost, toggle giả bật/tắt được
- [x] Metric giả cập nhật realtime trên chart (WS push mỗi 1s)
- [x] Emergency stop Ctrl+Shift+X → PAUSED từ mọi state (property test + live)
- [x] State transitions log được (structlog JSONL + event bus + SQLite table)
- [x] Config reload không cần restart (watchdog test)
- [x] Test phase 0 xanh — 315 passed
- [ ] Không memory leak sau 1h idle — CHƯA test (cần chạy dài, để user verify tuỳ chọn)

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
- ⭐ Phase 4 TTS BẮT BUỘC dùng `inference_stream()` (TTFA ~450ms đo thật), KHÔNG
  dùng `synthesize()` blocking (2.6s). End-to-end ~1s → đạt target. Xem day2_report.md.
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
