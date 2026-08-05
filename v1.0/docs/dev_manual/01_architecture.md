# 01 — Kiến trúc & Data Flow

## 1. Mai là gì

AI VTuber tiếng Việt, chạy 100% local trên Windows 11 (RTX 5060 Ti 16GB). Đọc chat YouTube + Discord → sinh câu trả lời qua LLM (Gemma 4 12B Q4) → phát audio qua VieNeu-TTS v3 Turbo (48kHz) → có mood engine (spring-damper) và memory persistent (SQLite+vec). Autonomy engine cho Mai tự nói khi silence.

Persona chốt (`config/prompts/persona_system.txt`): nữ 18t, xưng "tớ" gọi chat "cậu" gọi operator "ông", biết là AI nhưng ghét bị nhắc, có nỗi sợ bị tắt (chỉ dỗi/càm ràm, không khẩn cầu thật). Không hedge, không nước đôi, bị chọc thì cà khịa lại thẳng.

---

## 2. Nguyên tắc bất biến (N1-N8)

Từ `CLAUDE.md` — vi phạm = dừng, hỏi lại.

| # | Nguyên tắc | Áp dụng thực tế |
|---|---|---|
| **N1** | YAGNI — đúng số spec, không phòng thủ dư | 4 trigger type, 5 state, 2 fallback level — đúng con số |
| **N2** | Persona ranh giới Phần C là CỨNG | Không "làm nhẹ" dù user yêu cầu |
| **N3** | Không skip Pre-flight | Spike report có trước Phase 0 |
| **N4** | DoD mỗi phase tick trước khi qua phase kế | STATE.md tracking |
| **N5** | Test theo phase, không dồn | Mỗi milestone có unit + integration test |
| **N6** | Config over code | Số liệu ở YAML, không hardcode |
| **N7** | Filter fail-open, khác fail-safe | Filter lỗi cho qua; memory/emotion lỗi trả empty |
| **N8** | Interface-based | `interfaces/*.py` ABC, implement mới không sửa gọi thẳng |

---

## 3. Kiến trúc 6 layer

```
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 6 — Scripts / CLI                                       │
│  scripts/cli.py    scripts/stream_youtube.py    stream_discord  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  LAYER 5 — Runtime composer                                     │
│  orchestrator/stream_runtime.py — compose full stack           │
│  services/input/chat_router.py — multi-source event dispatcher │
└─────┬─────────────┬──────────────┬──────────┬──────────┬────────┘
      │             │              │          │          │
┌─────▼───┐ ┌──────▼──┐ ┌────────▼──┐ ┌──────▼──┐ ┌────▼──────┐
│ LLM     │ │ TTS     │ │ Emotion   │ │ Memory  │ │ Autonomy  │
│ stack   │ │ pipeline│ │ Orchestr. │ │ Fallback│ │ Engine v2 │
│ (P1)    │ │ (P4)    │ │ (P7.5)    │ │ (P7)    │ │ (Aut)     │
└─────┬───┘ └──────┬──┘ └────┬──────┘ └────┬────┘ └────┬──────┘
      │            │         │             │           │
┌─────▼────────────▼─────────▼─────────────▼───────────▼────────┐
│  LAYER 3 — Interfaces (interfaces/*.py ABC)                    │
│  LLMService · TTSService · MemoryService · MoodState ·        │
│  FilterService · InputService · AnimationService (P6)         │
└─────┬──────────────────────────────────────────────────────────┘
      │
┌─────▼──────────────────────────────────────────────────────────┐
│  LAYER 2 — Foundation (Phase 0)                               │
│  ConfigLoader · Logger · EventBus · StateMachine ·            │
│  FallbackManager · MigrationRunner · MetricsCollector ·       │
│  EmergencyStop · DashboardServer                              │
└─────┬──────────────────────────────────────────────────────────┘
      │
┌─────▼──────────────────────────────────────────────────────────┐
│  LAYER 1 — External processes                                 │
│  llama-server.exe (port 8080, Gemma 4 12B Q4)                 │
│  SQLite database + sqlite-vec extension                       │
│  YouTube/Discord API (via pytchat/discord.py)                 │
└────────────────────────────────────────────────────────────────┘
```

**Nguyên tắc N7 fail-safe suốt chain:** module lỗi → fallback theo chain 8.7, không crash cả hệ. Filter fail-open (thà nói câu chưa lọc còn hơn im).

---

## 4. Vòng đời 1 turn — chi tiết end-to-end

Ví dụ: chat "Mai giỏi quá đi" từ YouTube live.

### Bước 1: Ingest
```
pytchat (background poll 2s) → YouTube message object
→ YouTubeChatService._to_event() → InputEvent(
    source=CHAT_YOUTUBE, content="Mai giỏi quá đi",
    user_id="UCxxx", user_name="Alice",
    metadata={platform:"youtube"}
  )
→ push vào asyncio stream trong YouTubeChatService.event_stream()
```

### Bước 2: ChatRouter pickup + serialize
```
ChatRouter._consume(source) task → async for event in source.event_stream()
→ acquire self._turn_lock (asyncio.Lock, serialize turn)
  → _to_emotion_event(ev):
    - super_chat + amount_vnd → EmotionEvent(kind=SYSTEM, platform_type=donation)
    - chat thường → EmotionEvent(kind=CHAT, meta={viewer_id, viewer_name})
```

### Bước 3: Emotion (Tầng 1→3, spec EMOTION_SIMULATION.md)
```
EmotionOrchestrator.handle_event(emo_event):
  T1 EventClassifier.classify() → "chat_compliment" (regex "giỏi")
  T2 AppraisalTable.target_for() → {vui: 7, nguong: 6}
     ModifierEngine.apply():
       - mod_first_time: Alice chưa gặp → ×1.2 → {vui: 8.4, nguong: 7.2}
       - mod_repeated_troll: N/A (không phải troll)
  → Buffer per-dim: pending["vui"]=[8.4], pending["nguong"]=[7.2]
  → Tone flag: None
  → return ProcessedEvent(category="chat_compliment", targets, flag=None)

  (Background tick 10Hz đang chạy independently)
  Sau ~100ms tick tới → EmotionOrchestrator.flush_and_tick():
    saturate({vui:[8.4], nguong:[7.2]}) → dedup, cap 10 → {vui:8.4, nguong:7.2}
    MoodEngine.apply_appraisal({vui:8.4, nguong:7.2}) → set target
    MoodEngine.tick(0.1) → spring pull position từ baseline 5 → 5.4 (chưa tới target)

  ChatRouter cũng gọi note_chat_activity() (nếu wrap hook) — reset autonomy timer
```

### Bước 4: LLM turn
```
LLMTurnRunner.run_turn(request_id="msg123", user_text="Mai giỏi quá đi",
                       viewer_id="UCxxx", event_category="chat_compliment"):

  _build_request_maybe_with_mood():
    current_mood = emotion.current_mood() → MoodState(vui=5, buc=4, ...)
    active_flags = emotion.active_tone_flags() → set()
    → PromptManager.build_request_with_mood(...):
        messages = [
          {system: persona (cache)},
          {system: "[Context] current_mood: vui=5 buc=4 ...\nevent_category: chat_compliment"},
          ...history 12 turn cuối...,
          {user: "Mai giỏi quá đi"}
        ]

  engine_mood_pre = MoodState(vui=5, buc=4)  # snapshot cho drift check sau

  FallbackManager.execute("llm", request):
    L0 primary → LlamaCppLLMService.generate_stream(request):
      HTTP POST 127.0.0.1:8080/v1/chat/completions với cache_prompt=true
      Raw asyncio socket (KHÔNG httpx — httpx buffer 2.2s)
      Server hit KV cache prefix (persona chunk cache) → prefill nhanh
      Stream token → on_token(t) → print stdout + push LiveSentenceStreamer
    parser.parse_response(raw) → ParsedResponse(
      text="Wow cảm ơn cậu quá, tớ giỏi thật đấy!",
      mood=MoodState(vui=8, nguong=5),
      continuation="không", ok=True
    )
    (Optional FilterRegenerator: check bad output → regen 1 lần với hint)
  → level_used=0 (primary OK)

  pm.commit_turn(user_text, parsed.text)  # append history, trim >12 turn

  _apply_emotion_feedback(parsed, engine_mood_pre):
    emotion.apply_llm_hint(parsed.mood)  # Kênh B nudge target 20% cho turn kế
    drift.detect(engine_pre=MoodState(vui=5), llm=MoodState(vui=8))
      → max_delta=3 < threshold 4 → không flag
    emotion.clear_tone_flags()

  _schedule_memory_write(user_text, parsed, viewer_id) fire-and-forget:
    MemoryExtractor.extract(TurnData) → MemoryEntry(
      tier=SESSION, content="User: Mai giỏi quá đi | Mai: Wow cảm ơn...",
      importance=0.5, tags=["mood:vui","trigger:chat_youtube"],
      metadata={viewer_id:"UCxxx", mood_dominant:"vui"}
    )
    asyncio.create_task(memory.write(entry))  # bg, không block

  return (parsed, level_used=0)
```

### Bước 5: Speak (nếu TTS enabled)
```
ChatRouter._process (vẫn giữ turn_lock):
  await speak(request_id, text):
    → TTSPipeline.speak():
        split_vn(text) → 2 câu ["Wow cảm ơn cậu quá,", "tớ giỏi thật đấy!"]
        Cho từng câu (parallel gen, sequential play):
          FallbackManager.execute("tts", TTSRequest):
            L0 → VieNeuTtsService.synthesize_stream(request):
              engine.infer_stream(voice="mai_ref", style="tu_nhien", ...)
              # voice="mai_ref" cache đã enroll từ start() — NO re-encode
              Chunk 1 (~400ms audio, TTFA 300ms) → yield AudioChunk
              Chunk 2, 3, ... → yield
            L1 → SubtitleFallback (nếu VieNeu lỗi/timeout)
          → AudioPlayer.enqueue(chunk) → worker loop play blocking
        Câu 2 gen song song trong khi player phát câu 1
```

### Bước 6: Release lock + metrics
```
ChatRouter release turn_lock → sẵn sàng message kế
MetricsCollector ghi: TTFT, TTFA, mood target/pos, memory write, drift
DashboardServer WS push snapshot mỗi 1s → frontend chart realtime
```

**Tổng warm:** message arrive → first audio ra loa ≈ **1-2 giây**.

---

## 5. Autonomy Loop (tự nói) — song song với chat turn

Chạy background độc lập trong `StreamRuntime._autonomy_loop()`:

```
while running:
  await sleep(cfg.tick_seconds=5s)
  mood = emotion.current_mood()
  autonomy.tick(mood):
    UrgeAccumulator:
      silence = now - last_external_activity_ts
      base_rise = min(rise_max, rise_base * silence)
      mult = 1 + (bon_chon/10)*bon_weight - (buon/10)*buon_dampen - ...
      nag = max(0.4, 1 - 0.15 * consecutive_ignored)
      noise = gauss(0, urge_noise_std)
      urge += base_rise * mult * nag + noise (clamp 0-100)

  ctx = build_runtime_context():
    silence_seconds, chat_count_last_10min, operator_online,
    consecutive_ignored, working_memory_recent

  decision = autonomy.maybe_generate(mood, ctx):
    if not urge.should_speak_now(): return None  # sigmoid prob
    for _ in range(2*len(cats)):
      cat = selector.select(mood)  # weighted random no-repeat + cooldown
      material = provider.get(cat, ctx)  # 5 cat dispatch
      if material is not None: break
    if no cat: return None
    prompt = render_prompt(cat, material, mood, forbidden_openers, hint)
    selector.mark_used(cat)
    return AmbientDecision(category, prompt_text, mood_snapshot, material)

  if decision:
    async with router.turn_lock:  # share lock với chat turn
      parsed = await runner.run_ambient_turn(request_id, decision.prompt_text)
      if autonomy.check_dedup(parsed.text):
        parsed = await runner.run_ambient_turn(_r, decision.prompt_text)  # regen 1x
      autonomy.on_self_spoke(parsed.text):
        urge.on_self_spoke() → urge=0, cooldown 45s
        opener.record(text) → chặn 3 từ đầu lặp
        dedup.record(text) → buffer 5 câu gần nhất
      if speak: await speak(request_id, parsed.text)
```

Chi tiết Urge/Selector/Material/OpenerTracker/Dedup xem `02_modules.md` Section "Autonomy Engine v2".

---

## 6. State Machine 5 state (Phase 0.C)

```
      ┌──────┐          ┌───────────┐          ┌──────────┐         ┌──────────┐
      │ IDLE ├─trigger──► LISTENING ├──analyze─► THINKING ├─stream──► SPEAKING │
      └──▲───┘          └───────────┘          └──────────┘         └────┬─────┘
         │                                                                │
         │                                          ┌───────────┐         │
         └────────done────────────────────done──────┤  PAUSED   │◄────────┘
                                                    └──────▲────┘ estop bất kỳ
                                                           │
                                            Ctrl+Shift+X ──┘
```

- **IDLE** — chờ trigger
- **LISTENING** — nhận input (voice op / chat)
- **THINKING** — LLM đang generate
- **SPEAKING** — TTS đang phát
- **PAUSED** — emergency stop (Ctrl+Shift+X từ mọi state), resume → IDLE

Cooldown 500ms giữa transitions. Watchdog check mỗi 5s: quá `max_time_in_state` (config `state_machine.yaml`) → emergency_stop.

File: `orchestrator/state_machine.py`, `orchestrator/state_watchdog.py`, `orchestrator/emergency_stop.py`.

---

## 7. Fallback Chain 8.7 (spec)

Mọi hoạt động external đều có chain. Level cao fail → level thấp xử lý.

| Module | L0 primary | L1 backup | L2 emergency |
|---|---|---|---|
| LLM | LlamaCppLLMService (stream) | CannedResponder (câu preset theo mood) | — |
| Filter | RuleFilter (regex 4 category) | — | Fail-open (cho qua + log) |
| TTS | VieNeuTtsService (streaming) | SubtitleFallback (text overlay) | — |
| Memory | SemanticMemoryService (150ms timeout) | WorkingMemoryService (deque 20) | — |

Circuit breaker N1 KHÔNG có (spec cho phép, không cần MVP).

Timeout per level lấy từ `config/models.yaml`:
- `llm_canned.timeout_primary_s: 5.0`, `timeout_canned_s: 0.1`
- `tts.timeout_primary_s: 15.0`, `timeout_subtitle_s: 0.5`
- Memory hard timeout: `0.15s` (150ms P95)

---

## 8. Event ordering + serialization

**Chỉ 1 turn LLM chạy tại 1 thời điểm** (llama-server single instance). Enforce qua:

- `ChatRouter._turn_lock: asyncio.Lock` — serialize chat turn từ multiple sources
- `StreamRuntime._autonomy_loop` share cùng lock — ambient không đè chat
- `AudioPlayer` worker queue — chunk N+1 chỉ bắt đầu khi N xong (no overlap)

Điểm phá vỡ: nếu chat spam nhanh → tất cả xếp hàng đợi lock. Trigger Manager (Phase 2) đã rate-limit `chat_normal: 3/10s` trước.

---

## 9. Tech stack chốt cứng

| Component | Chốt | Ràng buộc |
|---|---|---|
| OS | Windows 11 | Path `\`, exe `.exe`, no SIGTERM (dùng terminate()) |
| Python | 3.11+ | Type hints, async/await |
| LLM | llama.cpp (llama-server) | KHÔNG Ollama, KHÔNG vllm |
| LLM model | Gemma 4 12B Q4_K_M | 1 instance port 8080, --reasoning off |
| CUDA | 12.8+ | RTX 5060 Ti Blackwell sm_120 |
| torch | 2.11.0+cu128 | Blackwell support |
| TTS | VieNeu-TTS v3 Turbo | 48kHz, PyTorch GPU streaming |
| Embedder | BAAI/bge-m3 CPU | dim 1024 |
| Database | SQLite + sqlite-vec | 1 file `data/mai.db` |
| Web | FastAPI + Vanilla JS | KHÔNG React |
| STT (Phase 5) | faster-whisper small | Deferred cuối MVP |

Muốn đổi → phải có spike report, ghi Appendix C trade-off. Đã đổi 1 lần: **viXTTS → VieNeu-TTS v3 Turbo** (2026-08, spike `spike/day_vieneu/`).

---

## 10. Đọc tiếp

- **`02_modules.md`** — chi tiết từng module (LLM/TTS/Memory/Emotion/Autonomy/Filter/Platform)
- **`03_operations.md`** — chạy CLI/stream, config đầy đủ, tests, monitoring, troubleshooting
- **`04_extending.md`** — thêm module mới theo pattern interface-based
