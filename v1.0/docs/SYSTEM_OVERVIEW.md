# Mai — Tổng kết hệ thống (2026-08)

> Snapshot trạng thái thật của codebase. Đọc file này để hiểu Mai làm gì, chạy ra sao, còn thiếu gì. Cập nhật mỗi khi phase mới đóng.

---

## 1. Mai là gì

**Mai** = AI VTuber tiếng Việt, chạy 100% local trên Windows 11 (RTX 5060 Ti 16GB). Cà khịa, ngang, không như trợ lý ảo. Đọc chat YouTube + Discord → LLM sinh câu → TTS phát ra loa. Có mood engine + memory persistent.

**Persona chốt** (`config/prompts/persona_system.txt`): nữ 18t, xưng "tớ" gọi chat "cậu" gọi operator "ông", biết là AI nhưng ghét bị nhắc, có nỗi sợ bị tắt (chỉ dỗi/càm ràm, không khẩn cầu thật).

---

## 2. Trạng thái hiện tại (2026-08)

| Metric | Giá trị |
|---|---|
| **Test suite** | 839 pass / 3 deselected (LLM live) |
| **Phase hoàn thành code** | 0, 1, 2, 3, 4, 7, 7.5, Platform |
| **Phase chưa làm** | 5 (STT, deferred), 6 (Animation), 8 (QC), 9 (Fine-tune) |
| **LLM TTFT warm** | ~470-620ms (Gemma 4 12B Q4) |
| **TTS TTFA warm** | ~284-304ms (VieNeu-TTS v3 Turbo 48kHz) |
| **Memory retrieve P95** | <150ms (FakeEmbedder; live bge-m3 chờ verify) |
| **Mood engine tick** | 10Hz (over-damped stable qua 10k tick) |

---

## 3. Kiến trúc tổng quan

```
┌───────────────────────────────────────────────────────────────────┐
│                     INPUT SOURCES (Platform.A/B)                  │
│  YouTubeChatService (pytchat) │ DiscordChatService (bot bridge)   │
└──────────────────────────┬────────────────────────────────────────┘
                           │ InputEvent
                           ▼
                    ┌──────────────┐
                    │  ChatRouter  │  serialize turn qua asyncio.Lock
                    │ (Platform.C) │
                    └──────┬───────┘
              ┌────────────┼──────────────┐
              │            │              │
              ▼            ▼              ▼
     ┌─────────────┐ ┌──────────┐ ┌──────────────────┐
     │  Emotion    │ │  Memory  │ │   LLMTurnRunner  │
     │ Orchestrator│ │ Fallback │ │   (llm_turn.py)  │
     │  (7.5.C)    │ │  (7.E)   │ └────────┬─────────┘
     └──────┬──────┘ └────┬─────┘          │
            │             │        ┌───────┴────────┐
            │             │        │ PromptManager  │
            │             │        │ .build_request │
            │             │        │  _with_mood    │  ← inject current_mood
            │             │        └───────┬────────┘
            │             │                │
            │             │                ▼
            │             │        ┌──────────────────┐
            │             │        │  LlamaCppLLMService (raw asyncio socket)
            │             │        │  → Gemma 4 12B Q4 (port 8080)
            │             │        └───────┬──────────┘
            │             │                │ token stream
            │             │                ▼
            │             │        ┌──────────────────┐
            │             │        │  parser (mood block)
            │             │        │  → ParsedResponse
            │             │        └───────┬──────────┘
            │             │                │
            ▼             ▼                ▼
     ┌──────────────────────────────────────────────┐
     │  Sau turn: apply_llm_hint (Kênh B) + drift   │
     │  detect + memory_extract → memory.write      │
     └──────────────────┬───────────────────────────┘
                        │ parsed.text
                        ▼
     ┌──────────────────────────────────────────────┐
     │  TTSPipeline: split_vn → VieNeu (L0) →       │
     │  subtitle fallback (L1) → AudioPlayer        │
     │  → loa (VB-Cable → OBS nếu stream)           │
     └──────────────────────────────────────────────┘
```

**Nguyên tắc N7 fail-safe suốt chain:** module lỗi → fallback theo chain 8.7, không crash cả hệ. Filter fail-open.

---

## 4. Các thành phần chính (đã build)

### 4.1. Foundation (Phase 0)

| Thành phần | File | Vai trò |
|---|---|---|
| **ConfigLoader** | `orchestrator/config_loader.py` | Load 8 file YAML, dotted access, hot-reload watchdog |
| **Logger** | `orchestrator/logger.py` | structlog JSONL + rotation |
| **FeatureManager** | `orchestrator/features.py` | 6 toggle rule + VRAM budget |
| **EventBus** | `orchestrator/event_bus.py` | asyncio pub/sub bounded queue |
| **StateMachine** | `orchestrator/state_machine.py` | 5 state (IDLE/LISTENING/THINKING/SPEAKING/PAUSED) |
| **TriggerManager** | `orchestrator/trigger_manager.py` | 4 trigger type + priority + spam/rate limit |
| **FallbackManager** | `orchestrator/fallback_manager.py` | Generic chain + timeout per level |
| **MigrationRunner** | `orchestrator/migration_runner.py` | Versioned SQL migrations + backup |
| **MetricsCollector** | `orchestrator/metrics_collector.py` | Prometheus counters/histograms |
| **DashboardServer** | `dashboard/dashboard_server.py` | FastAPI + WS realtime tại port 7860 |
| **EmergencyStop** | `orchestrator/emergency_stop.py` | Ctrl+Shift+X → PAUSED từ mọi state |

### 4.2. LLM stack (Phase 1)

| Thành phần | File | Vai trò |
|---|---|---|
| **LlamaCppLLMService** | `services/llm/llama_cpp_llm.py` | HTTP stream qua **raw asyncio socket** (không httpx — httpx buffer 2.2s), TTFT 72ms warm |
| **PromptManager** | `services/llm/prompt_manager.py` | [persona] + history + [user] messages. `build_request_with_mood` inject Context block (Phase 7.5) |
| **PromptCache** | `services/llm/prompt_cache.py` | Freeze persona prefix cho KV-cache reuse (llama-server `cache_prompt: true`) |
| **parser** | `services/llm/parser.py` | Extract mood block `[vui:N buon:N buc:N bon_chon:N nguong:N]` + text + `còn nữa` |
| **CannedResponder** | `services/llm/canned_response.py` | Fallback L1 khi LLM timeout — câu preset theo mood dominant |
| **LLMTurnRunner** | `services/llm/llm_turn.py` | Orchestrate 1 turn: build → stream → parse → commit history + wire emotion/memory |

### 4.3. Filter (Phase 3)

| Thành phần | File | Vai trò |
|---|---|---|
| **RuleFilter** | `services/filter/rule_filter.py` | Regex 4 category: PERSONA_BREAK, MANIPULATION, EXPLICIT, HARMFUL |
| **FilterRegenerator** | `services/filter/regenerator.py` | Bad output → append hint → regen tối đa 1 lần |

### 4.4. TTS (Phase 4, đã swap viXTTS → VieNeu 2026-08)

| Thành phần | File | Vai trò |
|---|---|---|
| **VieNeuTtsService** | `services/tts/vieneu_service.py` | VieNeu-TTS v3 Turbo 48kHz streaming. `add_voice` enroll ref audio 1 lần lúc `start()` — critical (không cache = TTFA 5626ms) |
| **SentenceSplitter** | `services/tts/sentence_splitter.py` | Cắt câu VN, bảo vệ số thập phân/viết tắt |
| **SubtitleFallback** | `services/tts/subtitle_fallback.py` | Level 2: không phát audio, push text qua callback |
| **AudioPlayer** | `services/tts/audio_player.py` | Worker loop asyncio Queue, chunk N+1 chỉ bắt đầu khi N xong (no overlap) |
| **TTSPipeline** | `services/tts/tts_pipeline.py` | split_vn → per-sentence chain L0(VieNeu)→L1(subtitle) → AudioPlayer |

**Ref audio:** `models/tts/xtts/vixtts/vi_sample.wav` (giữ từ setup viXTTS, giọng đã ưng).

### 4.5. Memory (Phase 7)

| Thành phần | File | Vai trò |
|---|---|---|
| **SqliteVecStore** | `services/memory/sqlite_vec_store.py` | Wrapper SQLite + sqlite-vec extension, insert atomic 2 bảng, KNN query filter tier/viewer_id |
| **BgeM3Embedder** | `services/memory/embedder.py` | BAAI/bge-m3 CPU, LRU cache 1000 câu, dim 1024 |
| **SemanticMemoryService** | `services/memory/semantic_memory.py` | Impl MemoryService, **hard timeout 150ms** query → fail-safe trả `[]` |
| **WorkingMemoryService** | `services/memory/working_memory.py` | Deque 20 in-memory, query LIFO |
| **MemoryFallbackManager** | `services/memory/memory_fallback.py` | Chain semantic (L0) → working (L1). Write fan-out cả 2 tier |
| **MemoryExtractor** | `services/memory/extractor.py` | Turn → MemoryEntry (skip trivial, preference regex → PERSISTENT tier) |

**Schema:** `migrations/004_add_memory_tables.sql` — memory_entries (11 cột, có viewer_id/session_id) + memory_vectors vec0 float[1024].

### 4.6. Emotion Simulation (Phase 7.5)

| Thành phần | File | Vai trò |
|---|---|---|
| **MoodEngine** | `orchestrator/mood_engine.py` | Spring-damper 2 kênh (Kênh A appraisal, Kênh B LLM hint), saturation max+0.5×(n-1), target decay |
| **EventClassifier** | `services/emotion/classifier.py` | 24 category (10 system + 10 chat + 4 timer). Filter Phase 3 → keyword regex fallback |
| **AppraisalTable** | `services/emotion/appraisal.py` | Lookup category → target dict. Tone flag mapping |
| **ModifierEngine** | `services/emotion/modifiers.py` | 3 modifier: repeated_troll (+0.5/hit), repeated_shutdown (×1.3), first_time (×1.2) |
| **EmotionOrchestrator** | `orchestrator/emotion_orchestrator.py` | Glue T1→T3, buffer per-tick, background tick loop 10Hz |
| **DriftDetector** | `services/qc/drift_detector.py` | Compare engine vs LLM self-report, flag lệch > threshold (4) |

### 4.7. Platform (Stream mode)

| Thành phần | File | Vai trò |
|---|---|---|
| **YouTubeChatService** | `services/input/youtube_chat.py` | pytchat wrapper polling YouTube Live, parse super chat amount → donation event |
| **DiscordChatService** | `services/input/discord_chat.py` | discord.py bot event-driven → asyncio.Queue. Token env var. |
| **ChatRouter** | `services/input/chat_router.py` | Multi-source consumer, serialize turn qua Lock. Convert InputEvent → EmotionEvent → run_turn → speak |
| **stream.py** | `scripts/stream.py` | CLI entry wire full stack: `--youtube VIDEO_ID --discord --tts --memory --dashboard` |

---

## 5. Vòng đời 1 turn (data flow chi tiết)

Ví dụ: chat "Mai giỏi quá đi" từ YouTube.

### Bước 1: Ingest
```
pytchat poll YouTube → message obj
YouTubeChatService._to_event() → InputEvent(
  source=CHAT_YOUTUBE,
  content="Mai giỏi quá đi",
  user_id="UCxxx", user_name="Alice",
  metadata={platform:"youtube"}
)
```

### Bước 2: Router pickup
```
ChatRouter._consume(source) → nhận event
→ acquire self._turn_lock (serialize turn)
→ _to_emotion_event(ev) → EmotionEvent(
  kind=CHAT, text="Mai giỏi quá đi",
  meta={viewer_id:"UCxxx", ...}
)
```

### Bước 3: Emotion (T1→T3)
```
EmotionOrchestrator.handle_event(emo_event):
  T1 classifier → "chat_compliment" (keyword "giỏi")
  T2 appraisal → {vui: 7, nguong: 6}
     modifier first_time (chưa gặp Alice) → ×1.2 → {vui: 8.4, nguong: 7.2}
  Buffer per-dim: pending["vui"]=[8.4], pending["nguong"]=[7.2]
  Tone flag: None
  → ProcessedEvent(category="chat_compliment", targets, flag=None)

(Background tick 10Hz đang chạy)
  Sau ~100ms tick tới → flush_and_tick():
    saturate({vui:[8.4]}) → {vui:8.4}
    engine.apply_appraisal({vui:8.4, nguong:7.2})
    engine.tick(0.1) → spring pull position từ baseline 5 → 5.4 (chưa tới 8.4)
```

### Bước 4: LLM turn
```
runner.run_turn(request_id="msg123", user_text="Mai giỏi quá đi",
                viewer_id="UCxxx", event_category="chat_compliment"):

  _build_request_maybe_with_mood():
    current_mood = engine.current_state() → MoodState(vui=5, buc=4, ...)
    active_flags = orch.active_tone_flags() → {}
    → PromptManager.build_request_with_mood(...):
        messages = [
          {system: persona},
          {system: "[Context] current_mood: vui=5 buc=4 ...\nevent_category: chat_compliment"},
          ...history...,
          {user: "Mai giỏi quá đi"}
        ]

  engine_mood_pre = MoodState(vui=5, buc=4)  # snapshot cho drift sau

  FallbackManager.execute("llm", request):
    L0 primary → LlamaCppLLMService.generate_stream(request):
      HTTP POST /v1/chat/completions với cache_prompt=true
      Server hit KV cache prefix (persona chunk cache) → prefill nhanh
      Yield token → on_token(t) → print stdout
    parser.parse_response(raw) → ParsedResponse(
      text="Wow cảm ơn cậu quá, tớ giỏi thật đấy!",
      mood=MoodState(vui=8, nguong=5, ...),
      continuation="không", ok=True
    )
    (Optional: FilterRegenerator check → nếu bad regen 1 lần)
  → level_used=0

  pm.commit_turn(user_text, parsed.text)  # append history

  _apply_emotion_feedback(parsed, engine_mood_pre):
    emotion.apply_llm_hint(parsed.mood)  # nudge target 20% (Kênh B turn kế)
    drift.detect(engine_mood_pre=MoodState(vui=5), llm=MoodState(vui=8))
      → deltas={vui:3}, max_delta=3 < threshold 4 → không flag
    emotion.clear_tone_flags()

  _schedule_memory_write(...) fire-and-forget:
    MemoryExtractor.extract(TurnData(...)) → MemoryEntry(
      tier=SESSION, content="User: Mai giỏi quá đi | Mai: Wow cảm ơn...",
      importance=0.5, tags=["mood:vui","trigger:chat_youtube"],
      metadata={viewer_id:"UCxxx", mood_dominant:"vui"}
    )
    asyncio.create_task(memory.write(entry))  # bg, không block
```

### Bước 5: Speak (nếu --tts)
```
ChatRouter._process (vẫn giữ lock):
  await speak(request_id="msg123", text=parsed.text)
    → TTSPipeline.speak():
        split_vn(text) → 2 sentences
        Sentence 1: VieNeu.synthesize_stream():
          engine.infer_stream(voice="mai_ref", ...)  # cache voice, no re-encode
          Chunk 1 (400ms audio, TTFA ~300ms) → AudioPlayer.enqueue
          Chunk 2 → enqueue
          ...
        AudioPlayer worker: play chunk 1 blocking → chunk 2 blocking (no overlap)
        Sentence 2 gen song song với player phát sentence 1 → giấu latency
```

### Bước 6: Release lock
```
ChatRouter release lock → sẵn sàng message kế
Metrics ghi: TTFT, TTFA, mood engine target/pos, memory write, drift report
Dashboard WS push snapshot mỗi 1s → frontend chart realtime
```

**Tổng thời gian** (warm): message YouTube arrive → first audio phát ra loa ≈ 1-2 giây.

---

## 6. Cấu hình

| File | Nội dung |
|---|---|
| `config/system.yaml` | Paths, event bus size, resources (VRAM budget), state machine timeout, features toggle |
| `config/models.yaml` | LLM (Gemma path, port, flags), TTS (VieNeu params), embedding (bge-m3) |
| `config/logging.yaml` | Log level, JSONL rotation |
| `config/features.yaml` | Feature toggle default state |
| `config/triggers.yaml` | 4 trigger type priority, rate limit, spam patterns, ambient threshold |
| `config/state_machine.yaml` | Cooldown, interrupt policy, watchdog threshold |
| `config/filters.yaml` | 4 filter category regex patterns |
| `config/mood_engine.yaml` | Spring-damper params, baseline mood |
| `config/emotion_appraisal.yaml` | Bảng target 24 category, tone flags, modifier params |
| `config/chat_sources.yaml` | YouTube video_id, Discord token env var + channel_ids |
| `config/prompts/persona_system.txt` | Persona A+B+C (tính cách + format + ranh giới tuyệt đối) |
| `config/prompts/ambient_instruction.txt` | Template Mai tự mở lời khi silence >60s |

---

## 7. Cách chạy

### 7.1. Dev mode — CLI text (không stream)

**Terminal 1** (llama-server):
```bash
E:\BAI_CUA_DUC\llama\llama-server.exe --model .\models\llm\gemma_4_12B_Q4.gguf --host 127.0.0.1 --port 8080 --ctx-size 4096 --n-gpu-layers 999 --cache-type-k q8_0 --cache-type-v q8_0 --batch-size 512 --flash-attn on --reasoning off
```

**Terminal 2** (CLI):
```bash
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
venv\Scripts\Activate.ps1
python scripts\cli.py --tts --dashboard
```

Gõ text, nhấn Enter → Mai response ra loa. Dashboard tại http://127.0.0.1:7860.

### 7.2. Stream mode — YouTube + Discord

**Setup Discord bot (1 lần):**
1. https://discord.com/developers/applications → New App → Bot → Reset Token
2. Bot → Enable **MESSAGE CONTENT INTENT**
3. OAuth2 URL → invite bot vào server
4. Set env: `$env:DISCORD_BOT_TOKEN = "..."`
5. `config/chat_sources.yaml` → set `discord.channel_ids: [<channel_id>]`

**Terminal 1** (llama-server): như trên.

**Terminal 2** (stream):
```bash
python scripts\stream.py --youtube VIDEO_ID --discord --tts --memory --dashboard
```

- `--youtube VID` — scrape chat YouTube live (video_id từ URL)
- `--discord` — Discord bot lắng nghe channel_ids
- `--tts` — phát audio VieNeu
- `--memory` — nạp bge-m3 CPU (30-60s lần đầu, +2GB RAM)
- `--dashboard` — mở http://127.0.0.1:7860

Ctrl+C → gracefully stop.

### 7.3. Chạy test suite

```bash
python -m pytest tests/ --deselect tests/integration/test_llama_server_live.py --deselect tests/integration/test_llm_live.py
```

Kỳ vọng: **839 pass / 3 deselected**. Live LLM tests cần llama-server chạy.

---

## 8. Nguyên tắc thiết kế bất biến (từ CLAUDE.md N1-N8)

| # | Nguyên tắc | Ví dụ vi phạm |
|---|---|---|
| **N1** | YAGNI — đúng số spec, không thêm "phòng khi cần" | Thêm 5 mood type nữa vì "biết đâu cần" |
| **N2** | Persona ranh giới Phần C là CỨNG | Sửa để "làm nhẹ" khi user prompt |
| **N3** | Không skip Pre-flight | Phase 0 khi chưa có spike report |
| **N4** | DoD mỗi phase phải tick trước khi qua phase kế | |
| **N5** | Test theo phase, không dồn | "code hết rồi test sau" |
| **N6** | Config over code | Hardcode timeout=5.0 trong .py |
| **N7** | Filter fail-open, phần khác fail-safe chain | Crash cả pipeline vì 1 module lỗi |
| **N8** | Interface-based, không gọi thẳng nhau | Import trực tiếp implementation class |

---

## 9. Đã có gì / Còn thiếu gì

### ✅ Đã có (chạy production được nếu có avatar)
- LLM streaming persona ổn định (100 turn soak, user duyệt)
- TTS VieNeu chất lượng cao, giọng đã ưng
- Mood engine với appraisal ground-truth (drift detector)
- Memory persistent (SQLite+vec), multi-viewer profile
- Filter chặn troll/jailbreak/sexual + regenerate hint
- Trigger manager rate limit + priority queue
- Emergency stop hotkey Ctrl+Shift+X
- Dashboard realtime metrics
- Stream mode YouTube + Discord

### ❌ Còn thiếu để live thật với viewer
1. **Avatar 2D** — Phase 6 chưa làm. VTube Studio API integration + model VRM/VSFAvatar
2. **Audio routing OBS** — VB-Cable setup + OBS scene (setup ngoài code)
3. **Chat overlay** — StreamElements/StreamLabs read từ platform (không phải code Mai)

### ⏸ Chưa làm nhưng roadmap có
- Phase 5 STT (voice input) — deferred cuối MVP
- Phase 8 QC + Data pipeline — chấm output, thu data fine-tune
- Phase 9 Fine-tune — Mai v2 (SFT/DPO qua Unsloth)

### ⏸ Live checkpoints treo (chỉ chạy verify, không code)
- P4 TTS quality subjective ≥6/10 qua 30 câu (user đã ưng → tick)
- P7 Memory: bge-m3 thật, retrieve P95 <150ms live
- P7.5 Emotion: 100 turn mood curve "cảm thấy đúng"
- Platform: unlisted YouTube live test end-to-end

---

## 10. Metrics + Observability

**Dashboard tabs (dashboard/templates/index.html):**
- Overview: state machine, TTFT/TTFA realtime chart
- LLM: TTFT hist, decode tps, parse ok/fail, fallback level
- Triggers: 4 type counts, skipped, interrupt, ambient
- Filter: checks/hits per category, hit rate, fail-open count
- TTS: TTFA per turn, chunks, subtitle fallback
- State: current state, watchdog last check, transitions

**Prometheus metrics** (auto-exposed qua dashboard REST):
- `llm_ttft_seconds` histogram, `llm_decode_tps`, `llm_parse_total{ok,fail}`
- `trigger_processed_total{type}`, `trigger_skipped_total{reason}`
- `state_transitions_total{from,to}`, `state_duration_seconds`
- `filter_checks_total`, `filter_hits_total{category}`
- `tts_pipeline_last_ttfa_ms`, `tts_subtitle_fallback_total`
- `mood_ticks`, `mood_appraisal_applies`
- `memory_queries_total`, `memory_timeouts_total`
- `drift_flagged_total`, `drift_flagged_rate`

**JSONL logs** (`logs/`):
- `events.jsonl` — structlog events
- `turns.jsonl` — mỗi turn 1 record (user text, Mai output, mood, latency)

**SQLite** (`data/mai.db`):
- `turns`, `state_transitions`, `trigger_decisions` (Phase 0.E)
- `memory_entries`, `memory_vectors` (Phase 7.A)
- Backups tự tạo trước mỗi migration ở `backups/`

---

## 11. File references quan trọng

Đọc theo thứ tự khi onboarding người mới:

1. `docs/QUICKSTART.md` — stack overview 1 trang
2. **`docs/SYSTEM_OVERVIEW.md`** (file này) — current state
3. `docs/ARCHITECTURE.md` — spec đầy đủ (13 section, ~2500 dòng — đọc section liên quan task)
4. `docs/EMOTION_SIMULATION.md` — spec Phase 7.5 (mood engine + appraisal)
5. `docs/AUTONOMY_ENGINE_REDESIGN.md` — spec ambient talk v2 (chưa implement)
6. `docs/persona.md` — persona Mai chi tiết A/B/C
7. `STATE.md` — nguồn sự thật "đang ở đâu", cập nhật mỗi phase
8. `CLAUDE.md` — rules cho Claude Code (N1-N8, workflow)

---

**Ngày cập nhật:** 2026-08-05 sau khi đóng Phase 7.5 + Platform (stream mode).
**Cập nhật khi:** phase mới đóng, có swap component lớn (như viXTTS → VieNeu), hoặc DoD phase tick.
