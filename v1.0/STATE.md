# STATE — Mai project

**Phase hiện tại:** Phase 4 TTS — P3 ĐÃ DUYỆT (2026-07-31)
**Task đang làm:** 4.E integration + dashboard TTS (kế tiếp) — 4.A–4.D xong

## Phase 4 milestone (5) — viXTTS streaming
- [x] 4.A VN cleaner + coqui-tts patches — 12 unit pass. services/tts/vixtts_patches.py:
      vi_expand_numbers (num2words), vi_clean (expand+strip+lowercase), apply_patches
      idempotent (torchaudio.load→soundfile, VoiceBpeTokenizer.preprocess_text vi hook).
      requirements.txt: coqui-tts + num2words + soundfile explicit; piper-tts REJECTED comment.
- [x] 4.B ViXttsService — 12 unit pass (fake model, no GPU). services/tts/vixtts_service.py:
      from_loader đọc models.yaml tts.*; start() apply patches → load Xtts (asyncio.to_thread)
      → cache gpt_cond_lat+spk_emb 1 lần; synthesize_stream chạy inference_stream trong
      executor, forward chunks qua asyncio.Queue → yield AudioChunk (float32 mono PCM
      @ sample_rate); cancel qua flag (check between chunks). Metrics TTFA/chunks/RTF.
- [x] 4.C sentence splitter + subtitle fallback — 18 unit pass.
      services/tts/sentence_splitter.py split_vn: regex . ! ? … giữ dấu, bảo vệ
      số thập phân/viết tắt (3.14, 1.250.000), lọc câu không chữ (min_len alnum).
      services/tts/subtitle_fallback.py SubtitleFallbackService(TTSService): Level 2
      (spec 8.7.3) — không phát audio, push text qua on_subtitle callback + event
      bus, yield 1 final empty chunk. Sink error không giết pipeline (N7).
- [x] 4.D audio player — 7 unit pass (FakeBackend, không mở device thật).
      services/tts/audio_player.py AudioPlayer: worker loop bên trong asyncio Queue,
      play_blocking trong asyncio.to_thread → CHUNK N+1 chỉ bắt đầu khi N xong
      (DoD no-overlap). cancel_current(request_id) drop pending + stop chunk hiện tại.
      Backend abstract (default SounddeviceBackend, test inject FakeBackend).
      is_playing property, chunks_played/dropped metrics.
- [ ] 4.E integration pipeline + dashboard TTS tab + DoD (TTFA P50 <1s)

## Phase 3 milestone (3)
- [x] 3.A RuleFilter — 13 unit pass. services/filter/rule_filter.py (FilterService):
      4 category, patterns config/filters.yaml (N6), severity/action max-priority,
      fail-open intrinsic (N7, không raise; bad pattern skip lúc compile). PERSONA_BREAK
      bắt hedge robot KHÔNG bắt "là AI" trần. config_loader thêm "filters".
- [x] 3.B regenerate-with-hint — 13 unit + 3 wire pass. services/filter/regenerator.py
      FilterRegenerator: build hint từ categories_hit (map VN), append 2 message
      (assistant=bad, user=hint) vào messages, re-generate + re-check tối đa
      max_regenerate_attempts (config filters.filter.max_regenerate_attempts=1).
      Metrics: checked/attempts/recovered/exhausted. N7 fail-open (filter/LLM lỗi →
      trả bản trước, không raise). Wire OPTIONAL vào LLMTurnRunner._primary +
      last_filter_verdict; backward-compat (no regen = behave như cũ).
- [x] 3.C dashboard filter tab + integration DoD — 12 unit/integration pass.
      MetricsCollector: record_filter_check + filter_snapshot (checks/hits/hit_rate/
      by_category/fail_open/recent). LLMTurnRunner._record_metrics forward verdict.
      DashboardServer: filter_svc + regenerator params, snapshot["filter"] merge
      check-level + regen counts + service fail-open. Frontend: tab Filter mới
      (cards + by-category + recent). test_phases.py thêm Phase 3.

## ✅ DoD Phase 3 (ARCHITECTURE 11.4)
- [x] 20 troll persona-break/manipulation/explicit/harmful: catch rate 100% (>80%)
- [x] 100 câu clean (gồm "tớ là AI" không hedge): false positive 0% (<5%)
- [x] Regenerate hoạt động khi persona_break detected (3.B unit test)
- [x] Filter fail-open khi regex/service error (3.A/3.B fail-safe test)
- Toàn suite: 515 pass (0 fail, 5 llm-live deselected).
**Cập nhật:** 2026-07-31

## ⚠️ Bug tiềm ẩn (flag, ngoài scope P2): migration backup filename chỉ có độ phân giải
giây → 2 migration cùng giây đè backup (test_migration_runner flaky). Đã spawn task riêng.

## 📌 ROADMAP đổi (2026-07-31): thêm PHASE 7.5 — Emotion Simulation (giữa P7 và P8)
- Spec: `docs/EMOTION_SIMULATION.md`. Mood đổi nguồn: appraisal rule-based (20 category
  + 4 timer + 3 modifier) làm CHÍNH; mood block LLM → Kênh B (format Phase 1 KHÔNG đổi).
- Đã cập nhật: ARCHITECTURE 11.8.5 + changelog v2.4, PROCESS Phase 7.5 + flow, persona.md Phần B.
- CHƯA code (đang ở Phase 2) — chỉ tích hợp tài liệu/roadmap. Code khi tới Phase 7.5.

## Phase 2 milestone (5) — delta, hạ tầng đã có từ P0
- [x] 2.A interrupt policy (7.9.3) — 11 unit pass. trigger_manager: set_speaking_context
      provider (N8), _should_interrupt đọc state_machine.yaml interrupt_policy. OPERATOR_VOICE
      elapsed>=2000ms → INTERRUPT_CURRENT (trigger vẫn enqueue để trả lời sau), <2s/mention/
      normal → QUEUE. fail-safe khi provider lỗi. metric trigger_interrupt_total.
- [x] 2.B deadlock watchdog (7.10.4) — 9 unit pass. state_watchdog.StateWatchdog:
      poll interval (config auto_recovery), elapsed>max_time_in_state → emergency_stop
      + auto recover→IDLE. IDLE/PAUSED (null) không giám sát. asyncio.Event stop (không
      hang). N8: chỉ dùng API state machine. metric watchdog_deadlocks_total.
- [x] 2.C ambient content gen (7.9.4) — 9 unit pass. PromptManager.build_ambient_request:
      persona + history + user turn "tự mở lời" (template config/prompts/ambient_instruction.txt,
      placeholder {silence} phút + {mood}). models.yaml ambient_prompt_path. Không mutate history.
- [x] 2.D dashboard tabs enrich — 4 unit pass. QueueStats +skipped_total/interrupt_total;
      trigger_manager populate; dashboard_server +watchdog param + snapshot["watchdog"];
      frontend: Triggers tab +Skipped/Interrupt cards, State tab +watchdog-info line.
- [x] 2.E integration 12.8 + DoD — 11 integration pass. turn_orchestrator.TurnOrchestrator:
      glue trigger↔state machine↔watchdog (consumer loop, interrupt cắt speak, emergency,
      resume). Test: priority operator>mention>normal, spam 60/phút→chỉ 3 lọt (rate),
      ambient>60s, trigger-during-thinking→queue, spam-during-speaking→drop, operator
      interrupt speaking, emergency từ speaking→PAUSED+clear queue, race 2 trigger, watchdog wiring.

## ✅ DoD Phase 2 (ARCHITECTURE 11.3)
- [x] Spam 60 tin/phút → không respond tất cả (rate limit 3/10s, test)
- [x] Priority operator_voice > mention > normal (test)
- [x] State transitions log đầy đủ (structlog + event bus + history, từ P0)
- [x] Ambient talk sau silence > 60s (test)
- [x] Watchdog detect deadlock khi force stuck (2.B unit + integration wiring)
- [x] Integration 12.8 xanh (11 test)

## ✅ CHECKPOINT P1 — user DUYỆT (2026-07-31)
- Soak 100 turn model thật: 0 crash ✅ | parse mood 100/100 = 100% ✅ | fallback 0
- Persona ổn định qua 100 turn: cà khịa, deflect kiến thức, nhận là AI khi hỏi thẳng,
  không khẩn cầu/không lộ system prompt — KHÔNG vi phạm ranh giới Phần C
- Dashboard TTFT/decode realtime OK (cli.py --dashboard / soak --dashboard, cùng process)
- ⚠️ TTFT p50=773ms > target 600ms — ĐẠT-CÓ-ĐIỀU-KIỆN: số 600 đo prompt ngắn
  Pre-flight; full history 12 cặp (~2000 tok) prefill nặng dần (min336→max1114). User
  chấp nhận (first-audio ~1.2s vẫn OK). SOI LẠI ở Phase 4 (TTS) nếu first-audio chậm.
  → Tùy chọn tối ưu sau: giảm max_history_turns / điều tra cache_prompt reuse.
- Công cụ: scripts/cli.py (--dashboard), scripts/soak_turns.py, config/prompts/soak_prompts.txt

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
