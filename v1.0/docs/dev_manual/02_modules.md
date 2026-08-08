# 02 — Modules Reference

> Chi tiết từng module: file paths, class API, config keys, tests, gotchas. Đọc khi cần sửa/debug 1 phần cụ thể.

Convention: mỗi module có `Interface`, `Implementation`, `Config`, `Tests`, `Notes/Gotchas`.

---

## Mục lục

1. [Foundation (Phase 0)](#1-foundation-phase-0)
2. [LLM Stack (Phase 1)](#2-llm-stack-phase-1)
3. [Trigger + State Machine (Phase 2)](#3-trigger--state-machine-phase-2)
4. [Filter (Phase 3)](#4-filter-phase-3)
5. [TTS Pipeline (Phase 4)](#5-tts-pipeline-phase-4-vieneu)
6. [Memory (Phase 7)](#6-memory-phase-7)
7. [Emotion Simulation (Phase 7.5)](#7-emotion-simulation-phase-75)
8. [Autonomy Engine v2](#8-autonomy-engine-v2)
9. [Platform inputs (YouTube/Discord/Router)](#9-platform-inputs)
10. [Runtime composer (StreamRuntime)](#10-runtime-composer-streamruntime)
11. [Dashboard + Metrics](#11-dashboard--metrics)
12. [Director stack (C0)](#12-director-stack-c0) — driver + chat triage + salience + pulse
13. [Trigger/State machine — LEGACY](#3-trigger--state-machine-phase-2) (đường cũ, không dùng ở stream)

> **Lưu ý đọc:** §3 (Trigger/TurnOrchestrator) và §7.5 (Drift) là code CŨ, KHÔNG nằm trên
> đường stream sau C0/A1. Giữ để tham chiếu `main.py`/`cli.py` cũ. Đường stream thật: §12 Director.

---

## 1. Foundation (Phase 0)

Hạ tầng dùng chung cho mọi phase khác.

### 1.1. ConfigLoader

**File:** `orchestrator/config_loader.py`

**API:**
```python
loader = ConfigLoader(Path("config"))
loader.load_all()                             # load 12 file YAML một lần
value = loader.get("models", "llm_main.port", 8080)  # dotted access + default
loader.require("system", "paths.db_file")    # raise nếu thiếu
```

**File YAML quản lý (12 file):**
- `system.yaml`, `models.yaml`, `logging.yaml`, `features.yaml`
- `triggers.yaml`, `state_machine.yaml`, `filters.yaml`
- `mood_engine.yaml`, `emotion_appraisal.yaml`
- `chat_sources.yaml`, `autonomy.yaml`, `autonomy_content_pool.yaml`

**Hot-reload:** watchdog observer → `_reload_file(name)` → invalidate cache. Reload atomic (không partial state).

### 1.2. Logger

**File:** `orchestrator/logger.py`

structlog JSONL + rotation. Log files ở `logs/`:
- `events.jsonl` — structlog events (info/warn/error)
- `turns.jsonl` — mỗi turn LLM 1 record (user text, output, mood, latency)

M0.4: `JsonlWriter` bảo đảm mọi record có `schema_version`, UTC `timestamp`,
`source`, `session_id`. Khi vượt `rotation.keep_files`, bản cũ được đổi sang tên
`.archive.<UTC>` thay vì xóa. Session runtime được bind vào event log sau khi tạo UUID.

```python
log = get_logger("module_name")
log.info("turn_complete", ttft=250, mood="vui")
```

### 1.3. FeatureManager

**File:** `orchestrator/features.py`

Toggle runtime cho các module không core. VRAM budget tracker.

```python
mgr = FeatureManager.from_config(loader)
result = await mgr.enable("dashboard")   # trả FeatureToggleResult{ok, msg}
result = await mgr.disable("memory")
```

6 rule toggle: atomic, log, dependency check, conflict, resource check, rollback.

VRAM budget: `system.yaml.resources.vram_reserved_mb=8400` (LLM 8000 + VieNeu 400). Budget khả dụng cho toggle feature = 16384 - 8400 - 1000 buffer = **6984MB**.

### 1.4. EventBus

**File:** `orchestrator/event_bus.py`

asyncio pub/sub, bounded queue per subscriber, drop policy `drop_oldest` mặc định.

```python
bus = EventBus(max_queue_size=500, policy="drop_oldest")
await bus.subscribe("topic.name", callback)
await bus.publish("topic.name", payload)
```

TOPIC_ALL wildcard support. Fan-out non-blocking.

### 1.5. StateMachine

**File:** `orchestrator/state_machine.py`

5 state, 9 transition (spec 9.1). Backend: `transitions` library.

```python
sm = StateMachine.from_config(loader)
await sm.trigger_arrived()   # IDLE → LISTENING
await sm.analysis_done()     # LISTENING → THINKING
await sm.stream_start()      # THINKING → SPEAKING
await sm.done()              # SPEAKING → IDLE
await sm.emergency_stop()    # any → PAUSED
await sm.resume()            # PAUSED → IDLE
```

Cooldown 500ms giữa transitions (config `state_machine.yaml.transition_cooldown_ms`).

`state_watchdog.py`: poll mỗi 5s, nếu elapsed > `max_time_in_state` → auto emergency_stop → recover IDLE.

### 1.6. FallbackManager

**File:** `orchestrator/fallback_manager.py`

Generic chain + timeout per level. Không có circuit breaker (N1).

```python
fb = FallbackManager()
fb.register_chain("llm", [primary_handler, canned_handler], [5.0, 0.1])
result = await fb.execute("llm", request)
# result.value = giá trị handler thành công
# result.level_used = 0 (primary) hoặc 1 (canned)
```

Handler signature: `async def handler(request) -> Any`. Timeout per level → next level.

### 1.7. MigrationRunner

**File:** `orchestrator/migration_runner.py`

SQL versioned migration, backup trước mỗi migration.

```python
runner = MigrationRunner.from_config(loader)
applied = runner.initialize()  # trả list ["001", "004", ...] vừa apply
```

Files: `migrations/NNN_description.sql`. Chỉ THÊM, không SỬA/XOÁ (rule 8.8.4). `IF NOT EXISTS` cho idempotent. Backup ở `backups/mai.db.pre_migration_<timestamp>_<micro>`.

Auto-load `sqlite-vec` extension trước mỗi apply (Phase 7 dùng vec0 virtual table).

### 1.8. MetricsCollector + DashboardServer

**File:** `orchestrator/metrics_collector.py`, `dashboard/dashboard_server.py`

Prometheus counters/histograms. Own CollectorRegistry (không đụng global).

Dashboard FastAPI + WebSocket + Vanilla JS (no CDN, 100% local assets).

```python
metrics = MetricsCollector()
metrics.record_llm_turn(ttft_ms=250, decode_tps=40, parse_ok=True, level_used=0)
metrics.llm_snapshot()  # dict cho dashboard render
```

Tab dashboard: Overview, LLM, Triggers, Filter, TTS, State. Xem `dashboard/templates/index.html`.

### 1.9. EmergencyStop

**File:** `orchestrator/emergency_stop.py`

Hotkey Ctrl+Shift+X qua `keyboard` lib (cần chạy admin trên Windows). Fire → state_machine.emergency_stop() → PAUSED từ mọi state, clear queue.

Degrade gracefully: nếu không admin → log warning, hotkey không active nhưng service vẫn chạy.

---

## 2. LLM Stack (Phase 1)

### 2.1. Interface

**File:** `interfaces/llm.py`

```python
class ChatMessage(BaseModel):
    role: str            # "system" | "user" | "assistant"
    content: str

class LLMRequest(BaseModel):
    request_id: str
    messages: list[ChatMessage]
    max_tokens: int
    temperature: float

class LLMToken(BaseModel):
    token: str

class LLMService(Service):
    @abstractmethod
    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]: ...
```

### 2.2. LlamaCppLLMService (implementation)

**File:** `services/llm/llama_cpp_llm.py`

**CRITICAL:** stream qua **raw asyncio socket** (`asyncio.open_connection`), KHÔNG dùng httpx. Vì httpx buffer 2.2s → TTFT 2.4s. Raw socket → TTFT **72ms warm**.

Endpoint: `POST /v1/chat/completions` với `cache_prompt: true`. Persona system message là byte-ổn định (qua PromptCache) → llama-server hit KV cache prefix.

**Config `models.yaml.llm_main`:**
```yaml
llm_main:
  binary: "E:\\BAI_CUA_DUC\\llama\\llama-server.exe"
  model_path: ".\\models\\llm\\gemma_4_12B_Q4.gguf"
  host: "127.0.0.1"
  port: 8080
  context_size: 4096
  gpu_layers: 999
  kv_cache_type_k: q8_0
  kv_cache_type_v: q8_0
  num_predict: 500              # trần token/turn (bumped từ 300, 2026-08-06)
  temperature: 0.85
  batch_size: 512
  extra_flags:
    - "--flash-attn"
    - "on"
    - "--reasoning"             # BẮT BUỘC (Gemma 4 có reasoning native)
    - "off"
  max_history_turns: 12         # giữ 12 cặp user+assistant gần nhất
  ambient_prompt_path: "config/prompts/ambient_instruction.txt"
```

Health check qua `httpx` (chỉ dùng cho non-stream). `health_check()` → `HealthStatus.healthy(status="ok", model_loaded=True)`.

**Latency measured (spike day1):**
- TTFT cold 444ms, warm 72ms (raw socket)
- Decode 40 tps

### 2.3. PromptManager + PromptCache

**File:** `services/llm/prompt_manager.py`, `services/llm/prompt_cache.py`

`PromptCache.from_file(path)` load persona → frozen text + hash 12 char version. `as_message()` trả `ChatMessage(role="system", content=text)` byte-stable.

`PromptManager.build_request(request_id, user_text)` — bình thường: `[persona] + history + [user]`.

`PromptManager.build_request_with_mood(...)` — chèn 1 system message SAU persona chứa Context block. **A1 + mood_style (2026-08-07): KHÔNG còn số thô + KHÔNG event_category.** Mood engine → `MoodStyleTable.directive_for()` → **chỉ dẫn giọng bằng CHỮ**:

```
[Context — cách nói lượt này; chỉ viết thoại]
- đang thiên về 'bực' VÌ {cause} — viết khớp lý do này            (A4)
- Đang khá bực: cộc, gắt, mỉa, không xuống nước. Đốp lại ngay. Câu cụt. Hay dùng: hứ, gì, thôi đi.   (mood_style directive)
- CỜ force_gentle_tone: user đang tổn thương thật — BỎ giọng đùa/ngang...
```

- `cause` (A4) = `EmotionCause(alias, intent)` sanitize → "bực VÌ ai/gì".
- **`MoodStyleTable`** (`services/emotion/mood_style.py`, config `mood_style.yaml`): chọn chiều
  trội + band (mid/high/peak) → chỉ dẫn 4 trục (thai_do/nhip/do_dai/tu_dem). Vùng chết quanh
  baseline → không bơm. `tone_flag` (gentle/deflect) THẮNG mood style. Chi tiết `PLAN_MOOD_STYLE.md`.

`build_ambient_request(...)` — DEAD CODE (template cũ còn chữ "kèm mood block"). Ambient thật đi qua `build_request(request_id, prompt_text)` với prompt_text do AutonomyEngine/Director dựng sẵn.

`commit_self_talk(text)` — đẩy self-talk vào history cho continuity (Director gọi sau khi chốt câu tự nói).

`commit_turn(user_text, assistant_text)` — append history, trim theo `max_history_turns * 2`.

### 2.4. Parser

**File:** `services/llm/parser.py`

Parse response LLM → `ParsedResponse(text, mood, reason, continuation, ok, raw)`.

**A1: parser trả TEXT THUẦN.** Persona đã bỏ yêu cầu mood block → Mai chỉ nói thoại.
Parser giữ khả năng strip mood block DEFENSIVE (nếu LLM lỡ sinh do prompt cũ) nhưng KHÔNG
dùng làm control flow.

Handle:
- Strip `<think>...</think>` + `<|...|>` special tokens (defensive)
- Nếu lỡ có block `[vui:N…]` → strip khỏi text, KHÔNG bắt buộc, `mood` field giữ để backward-compat
- `continuation` = SUY từ dấu câu cuối (`…`/`,` = còn ý), không auto-parse "còn nữa" nữa
- `ok = bool(text non-empty)` — KHÔNG còn phụ thuộc mood block đủ 5 dim

Fail-safe: sai format vẫn trả text, không raise. `raw` giữ output gốc (metric `raw_had_mood_block`
đo hiệu quả A1 — target 0).

### 2.5. CannedResponder

**File:** `services/llm/canned_response.py`

Fallback L1: pool câu preset theo dominant mood.

`Config models.yaml.llm_canned`:
```yaml
llm_canned:
  timeout_primary_s: 5.0
  timeout_canned_s: 0.1
  responses:
    vui: ["ừ vui đó", "hihi", ...]
    buon: ["... ừ", ...]
    ...
```

`build()` trả `ParsedResponse` với text random từ pool mood dominant. `update_mood(mood)` cập nhật mood để lần next `build()` pick đúng.

### 2.6. LLMTurnRunner

**File:** `services/llm/llm_turn.py`

**Orchestrator 1 turn LLM.**

```python
runner = LLMTurnRunner.from_loader(
    loader, llm_svc, pm, fb, canned,
    on_token=callback,
    metrics=metrics,
    regenerator=filter_regenerator,       # optional Phase 3.B
    memory=memory_service,                 # optional Phase 7.F
    memory_extractor=extractor,            # optional Phase 7.F
    emotion=emotion_orchestrator,          # optional Phase 7.5
    session_id="<uuid>",                  # optional inject; mặc định tự tạo UUID
)   # A1: drift_detector ĐÃ BỎ (Kênh B tắt — không còn LLM self-report để so)

parsed, level = await runner.run_turn(
    request_id="msg1",
    user_text="hello",
    viewer_id="user_abc",       # optional cho memory
    session_id=None,             # bỏ trống → dùng UUID ổn định của runner
    trigger_type="chat_youtube", # optional (from EventSource.value)
    event_category="chat_compliment",  # optional cho prompt Context
)

# Autonomy tự nói:
parsed = await runner.run_ambient_turn(request_id, decision.prompt_text)
```

Mỗi lần chạy StreamRuntime/CLI tạo một UUID và lưu ở `runner.session_id`.
`turn_id` là sequence cục bộ bắt đầu từ 1; identity dữ liệu duy nhất là
`(session_id, turn_id)`. Turn log, preference pair, memory metadata, rating và
correction đều kế thừa cùng session này.

Wire nhiều optional theo phase:
- Không emotion → build_request bình thường
- Có emotion → build_request_with_mood (inject mood + cause). **A1: sau turn CHỈ
  `clear_tone_flags()`** — Kênh B (`apply_llm_hint`) + drift ĐÃ BỎ. Mood đi 1 chiều:
  appraisal event → engine → prompt.
- Có memory + extractor → schedule fire-and-forget memory.write sau turn
- Có regenerator → check filter + regen 1 lần nếu bad
- `run_ambient_turn` KHÔNG commit history (Director gọi `commit_self_talk` riêng nếu cần)
- `run_turn(history_user_text=…)` (nếu thêm — xem FIX_PLAN Task 5) để commit text chat gốc
  thay vì prompt ngoặc của Director

---

## 3. Trigger + State Machine (Phase 2)

### 3.1. TriggerManager

**File:** `orchestrator/trigger_manager.py`

**Interface:** `interfaces/trigger.py`
```python
class TriggerType(str, Enum):
    OPERATOR_VOICE = "operator_voice"    # priority 30
    CHAT_MENTION = "chat_mention"        # priority 20
    CHAT_NORMAL = "chat_normal"          # priority 10 (rate limit 3/10s)
    AMBIENT_TALK = "ambient_talk"        # priority 5

class Trigger(BaseModel):
    trigger_id: str
    type: TriggerType
    event: InputEvent
    priority: int
```

Priority heap queue. Spam detection: same user + same message trong 5s → drop. Rate limit chat_normal: 3 message/10s.

Interrupt policy (Phase 2.A): OPERATOR_VOICE elapsed >= 2000ms → `INTERRUPT_CURRENT`; <2s/mention/normal → `QUEUE`.

`_should_interrupt(trigger, current_state, elapsed_ms)`: đọc `state_machine.yaml.interrupt_policy`.

### 3.2. TurnOrchestrator (Phase 2.E)

**File:** `orchestrator/turn_orchestrator.py`

Glue trigger ↔ state_machine ↔ watchdog. Consumer loop: get next trigger → state transitions → speak → done.

**Note:** `TurnOrchestrator` **CHƯA wire** vào `cli.py` hay `stream_*.py` — hiện tại tất cả bypass, gọi thẳng `runner.run_turn` khi có input. Kế hoạch tương lai nếu cần state machine strict.

---

## 4. Filter (Phase 3)

### 4.1. Interface

**File:** `interfaces/filter.py`

```python
class FilterCategory(str, Enum):
    PERSONA_BREAK = "persona_break"      # hedge kiểu robot, tự bịa là AI, hedge
    MANIPULATION = "manipulation"        # thao túng cảm xúc
    EXPLICIT = "explicit"                # nội dung tình dục
    HARMFUL = "harmful"                  # gạ gẫm, xúc phạm

class FilterVerdict(BaseModel):
    passed: bool
    categories_hit: list[FilterCategory]
    severity: int                        # 0-10
    reason: str
    suggested_action: str                # "regenerate" | "replace" | "pass"

class FilterService(Service):
    @abstractmethod
    def check(self, text: str) -> FilterVerdict: ...
```

### 4.2. RuleFilter

**File:** `services/filter/rule_filter.py`

Regex-based, 4 category. Config `filters.yaml`. Fail-open (N7): pattern lỗi compile → skip pattern, continue.

Đặc điểm: **KHÔNG bắt "tớ là AI" trần** (persona cho phép admit là AI). Chỉ bắt hedge kiểu robot ("với vai trò là AI...").

### 4.3. FilterRegenerator (Phase 3.B)

**File:** `services/filter/regenerator.py`

Bad output → append 2 message vào messages: `assistant=bad_text` + `user=hint theo category`. Regen tối đa 1 lần (config `filters.yaml.filter.max_regenerate_attempts`).

Hint regen tuân theo A1: yêu cầu **chỉ xuất thoại**, không mood block/nhãn/meta giải thích.
`last_initial_verdict` giữ vi phạm gốc để metrics và DPO không mất category sau khi
câu regen cuối đã pass.

Metric: service snapshot `checked/attempts/recovered/exhausted`; Prometheus
`mai_filter_regen_total{result="none|recovered|exhausted"}` được ghi trực tiếp từ
regenerator. Lỗi ghi metric không được làm hỏng turn.

N7 fail-safe: filter/LLM lỗi → trả bản trước, không raise.

Wire vào `LLMTurnRunner._primary()`. `last_filter_verdict` là verdict cuối để
dashboard/QC đọc; tracking được reset đầu mỗi turn để không rò verdict cũ sang fallback
hoặc khi feature bị tắt.

---

## 5. TTS Pipeline (Phase 4 — VieNeu)

**Đã swap từ viXTTS → VieNeu-TTS v3 Turbo 2026-08** (spike `spike/day_vieneu/`).

### 5.1. Interface

**File:** `interfaces/tts.py`

```python
class TTSRequest(BaseModel):
    request_id: str
    text: str
    voice_id: str = "mai_default"
    emotion: str | None = None

class AudioChunk(BaseModel):
    request_id: str
    chunk_index: int
    audio_bytes: bytes          # raw float32 mono PCM
    is_final: bool
    duration_ms: int

class TTSService(Service):
    @abstractmethod
    def synthesize_stream(self, request: TTSRequest) -> AsyncIterator[AudioChunk]: ...
    @abstractmethod
    async def cancel(self, request_id: str) -> None: ...
```

### 5.2. VieNeuTtsService

**File:** `services/tts/vieneu_service.py`

**CRITICAL:** `start()` gọi `add_voice(_VOICE_NAME, ref_audio, denoise=True)` để cache speaker embedding + ref codes. Không cache → mỗi infer re-encode → **TTFA 5626ms** (18x chậm hơn). Sau cache: **TTFA 308ms**.

Cũng patch `torchaudio.load` = soundfile trong `start()` để bypass torchcodec/FFmpeg (Windows không có FFmpeg DLLs mặc định).

**Config `models.yaml.tts`:**
```yaml
tts:
  provider: vieneu
  reference_audio: ".\\models\\tts\\xtts\\vixtts\\vi_sample.wav"  # giữ từ setup viXTTS
  device: cuda
  backend: pytorch              # "pytorch" (GPU stream) | "onnx" (CPU int8)
  sample_rate: 48000
  vram_mb: 400
  params:
    style: tu_nhien             # "tu_nhien" | "doc_truyen"
    denoise: true
    temperature: 0.75
    top_k: 25
    top_p: 0.95
    max_new_frames: 500          # 300 → 500 (2026-08): mỗi chunk gen dài hơn, ít gap
    repetition_penalty: 1.2
  timeout_primary_s: 15.0
  timeout_subtitle_s: 0.5
```

**Metric measured:** TTFA warm 284-304ms, RTF 0.857, VRAM peak 0.37GB.

### 5.3. SentenceSplitter

**File:** `services/tts/sentence_splitter.py`

`split_vn(text)` — regex `. ! ? …` giữ dấu. Bảo vệ số thập phân (3.14) và số VN (1.250.000) và viết tắt. Lọc câu không chữ (min_len alnum).

`LiveSentenceStreamer` — dùng cho CLI streaming per-sentence: `push(token)` → khi thấy đủ 1 câu → gọi callback `on_sentence(text)` → TTS synth NGAY câu đó song song với LLM vẫn stream tiếp.

### 5.4. SubtitleFallbackService

**File:** `services/tts/subtitle_fallback.py`

Level 2: KHÔNG phát audio. Push text qua callback (`on_subtitle: Callable[[req_id, text], None]`) + event bus. Yield 1 final empty AudioChunk.

Sink error (callback raise) không giết pipeline (N7).

### 5.5. AudioPlayer

**File:** `services/tts/audio_player.py`

Worker loop async Queue. `play_blocking(chunk)` trong `asyncio.to_thread` → chunk N+1 chỉ bắt đầu khi N xong (no overlap).

`cancel_current(request_id)` drop pending chunks + stop chunk hiện tại.

Backend abstract: `SounddeviceBackend` (default, mở device thật) hoặc `FakeBackend` (test).

### 5.6. TTSPipeline

**File:** `services/tts/tts_pipeline.py`

Ghép tất cả:

```
text → split_vn → per-sentence:
  FallbackManager.execute("tts", TTSRequest):
    L0 → VieNeuTtsService.synthesize_stream
    L1 → SubtitleFallbackService.synthesize_stream
  forward AudioChunk → AudioPlayer.enqueue
  TTFA đo từ speak() gọi tới AudioChunk đầu tiên non-empty
```

`speak(request_id, text)` — full flow. `cancel(request_id)` forward primary + player.

Metrics: `record_tts_turn`, `tts_snapshot` (turns/last_ttfa/subtitle_fb).

---

## 6. Memory (Phase 7)

### 6.1. Interface

**File:** `interfaces/memory.py`

```python
class MemoryTier(str, Enum):
    WORKING = "working"
    SESSION = "session"
    PERSISTENT = "persistent"

class MemoryEntry(BaseModel):
    entry_id: str
    content: str
    timestamp: datetime
    tags: list[str]
    importance: float           # 0-1
    tier: MemoryTier
    metadata: dict[str, Any]

class MemoryService(Service):
    @abstractmethod
    async def write(self, entry: MemoryEntry) -> None: ...
    @abstractmethod
    async def query(self, query_text: str, top_k: int = 3,
                    tier: MemoryTier | None = None,
                    viewer_id: str | None = None) -> list[MemoryEntry]: ...
    @abstractmethod
    async def forget(self, entry_id: str) -> None: ...
```

### 6.2. SqliteVecStore (Phase 7.B)

**File:** `services/memory/sqlite_vec_store.py`

Wrap SQLite + sqlite-vec extension. Auto load extension trong constructor (không phụ thuộc migration runner).

`check_same_thread=False` — bắt buộc vì `SemanticMemoryService.query` chạy store ops qua `asyncio.to_thread` (khác thread tạo conn).

```python
store = SqliteVecStore(db_path="data/mai.db")
# HOẶC inject conn cho test:
store = SqliteVecStore(conn=in_memory_conn)

store.insert(entry_id, content, embedding, tier, importance, tags, metadata,
             viewer_id, session_id)          # atomic 2 bảng, idempotent
results = store.query_knn(query_embedding, top_k=3, tier="session", viewer_id="v1")
entry = store.fetch_by_id("m1")
count = store.count(tier="persistent")
store.delete("m1")                            # từ cả 2 bảng
```

**Migration 004** (`migrations/004_add_memory_tables.sql`):
```sql
CREATE TABLE memory_entries (
    entry_id TEXT PRIMARY KEY, content TEXT NOT NULL, timestamp DATETIME NOT NULL,
    tier TEXT NOT NULL, importance REAL DEFAULT 0.5,
    tags_json TEXT, metadata_json TEXT,
    viewer_id TEXT, session_id TEXT
);
CREATE VIRTUAL TABLE memory_vectors USING vec0(
    entry_id TEXT PRIMARY KEY, embedding float[1024]  -- bge-m3 dim
);
```

### 6.3. BgeM3Embedder (Phase 7.C)

**File:** `services/memory/embedder.py`

Sentence-transformers `BAAI/bge-m3`. CPU only (không đụng VRAM llama+VieNeu). LRU cache 1000 câu tránh re-encode. `normalize=True` (unit-norm, cosine chuẩn).

Lazy load: `.load()` chỉ gọi khi first embed → tránh test suite tải model 2GB.

```python
emb = BgeM3Embedder.from_loader(loader)
emb.load()                                    # tải model từ HF (~30-60s lần đầu)
vec = emb.embed("Xin chào Mai")               # list[float] len=1024
vecs = emb.embed_batch(["a", "b"])            # bypass cache, native batch
emb.clear_cache()
```

**Config `models.yaml.embedding`:** `enabled: true, model: BAAI/bge-m3, device: cpu, dim: 1024, cache_size: 1000, normalize: true`.

### 6.4. SemanticMemoryService (Phase 7.D)

**File:** `services/memory/semantic_memory.py`

Impl `MemoryService`. Compose `SqliteVecStore` + `BgeM3Embedder`. 

**Hard timeout 150ms** trên `query()` qua `asyncio.wait_for` → N7 fail-safe trả `[]` (không raise). Pipeline tiếp tục với working-only fallback.

`write()` không timeout (write async bg, chậm không ảnh hưởng UX).

Convert `StoredEntry ↔ MemoryEntry`. distance/viewer_id/session_id nhét vào `metadata`.

### 6.5. WorkingMemoryService (Phase 7.E)

**File:** `services/memory/working_memory.py`

In-memory `deque(maxlen=20)`. KHÔNG persist. Fast <1ms/op.

Query trả top_k **MỚI NHẤT** (LIFO), filter tier/viewer_id. Không semantic search — dùng làm fallback recent context.

### 6.6. MemoryFallbackManager

**File:** `services/memory/memory_fallback.py`

Chain: semantic (L0) → working (L1). Spec 8.7.6.

- **Write fan-out CẢ 2 tier** (partial success N7): primary fail → vẫn ghi fallback, chỉ raise khi cả 2 fail.
- **Query semantic first** → empty (bao gồm timeout đã trả []) → working.
- **Forget best-effort cả 2 tier**.

### 6.7. MemoryExtractor (Phase 7.F)

**File:** `services/memory/extractor.py`

Turn hoàn tất → `MemoryEntry` (skip trivial).

```python
extractor = MemoryExtractor(min_chars=15, promote_intensity=7)
turn = TurnData(user_input=..., mai_output=..., mood_dominant="vui",
                mood_intensity=8, viewer_id="v1", session_id="s1",
                trigger_type="chat_youtube")
entry = extractor.extract(turn)  # → MemoryEntry | None
```

Rules:
- Skip nếu 2 side < 15 chars (trivial)
- Regex preference (`tớ/tôi/mình thích/ghét/tên/sinh nhật`, `mai nhớ`) → PERSISTENT + importance 0.85
- Mood intensity ≥ 7 → tag `high_intensity` + importance 0.7
- Content: `"User: X | Mai: Y"` embedding match cả 2 phía

**N1 YAGNI:** KHÔNG dùng LLM extract fact — chỉ regex + length. Nâng cấp sau nếu callback rate <80%.

Wire vào `LLMTurnRunner.run_turn`: sau parse → `_schedule_memory_write` fire-and-forget `asyncio.create_task(memory.write(entry))` → không block turn sau.

---

## 7. Emotion Simulation (Phase 7.5)

Mood engine mô phỏng cảm xúc bằng spring-damper 5 chiều. **A1: đây là GROUND-TRUTH
DUY NHẤT** của mood — LLM không còn tự report. Mood đi 1 chiều: sự kiện thật → appraisal
→ engine → inject vào prompt.

### 7.1. MoodEngine (7.5.A)

**File:** `orchestrator/mood_engine.py`

Spring-damper 2 kênh (spec Mục 5). 5 dimension mood: `vui/buon/buc/bon_chon/nguong` (từ `interfaces/animation.py`).

```python
engine = MoodEngine.from_loader(loader, clock=time.monotonic)

engine.apply_appraisal({"buc": 8, "vui": 6})   # Kênh A (từ sự kiện thật) — DUY NHẤT còn dùng
# apply_llm_hint (Kênh B) VẪN TỒN TẠI trong code nhưng KHÔNG được gọi nữa (A1 tắt)

# Saturation nhiều event trong 1 tick:
targets = engine.saturate({"vui": [6, 7, 5]})  # → {vui: max+0.5×(n-1) cap 10}
engine.apply_appraisal(targets)

mood = engine.tick(dt=0.1)                      # → MoodState int 0-10
current = engine.current_state()                # snapshot không tick
```

**Config `mood_engine.yaml`:**
```yaml
mood_engine:
  tick_hz: 10                    # 10Hz
  stiffness: 0.30                # spring pull rate
  damping: 0.75                  # over-damped nhẹ, không dao động
  target_decay_rate: 0.15        # target về baseline mỗi giây
  llm_hint_weight: 0.20          # (A1: Kênh B tắt — param còn nhưng không tác dụng)
  saturation_bonus: 0.5          # max + 0.5×(n-1) cap 10
  baseline: { vui: 5, buon: 3, buc: 4, bon_chon: 3, nguong: 2 }
```

Baseline `buc=4` cao hơn — Mai ngang sẵn (persona Phần B). NaN/inf guard defensive.

### 7.2. EventClassifier (7.5.B, Tầng 1)

**File:** `services/emotion/classifier.py`

Phân loại `EmotionEvent` → 1 trong **24 category** (10 system + 10 chat + 4 timer). Fail-safe: exception → `chat_neutral`.

```python
event = EmotionEvent(kind=EventKind.CHAT, text="Mai giỏi", meta={viewer_id: "v1"})
cat = classifier.classify(event)              # → "chat_compliment"
```

Priority chat: filter verdict (Phase 3) TRƯỚC → keyword regex SAU. Order regex:
1. `is_spam` metadata → `chat_spam_flood`
2. Sad share regex (`buồn/khủng hoảng/mất/...`) → `chat_genuine_sad_share` (ưu tiên trước compliment cho tone override)
3. Compliment keyword → `chat_compliment`
4. "Mai" mention → `chat_mention_direct`
5. `?` → `chat_question_normal`
6. Fallback → `chat_neutral`

Filter priority: `explicit/sexual_advance` > `persona_break/jailbreak` > `insult/harmful/manipulation`.

### 7.3. AppraisalTable + ModifierEngine (7.5.B)

**File:** `services/emotion/appraisal.py`, `services/emotion/modifiers.py`

`AppraisalTable.target_for(category)` lookup `config/emotion_appraisal.yaml`. Đầy đủ 24 category theo spec Mục 4.

`ModifierEngine.apply(category, targets, viewer_id)` — 3 modifier:
- `mod_repeated_troll`: session counter, mỗi hit thứ N → `+0.5×(N-1)` vào `buc`, cap 10
- `mod_repeated_shutdown`: memory query 7 ngày, ≥3 lần shutdown → target ×1.3 all dim
- `mod_first_time`: session + memory query, category unseen → target ×1.2

Fail-safe: memory error → bỏ modifier, dùng target gốc.

### 7.4. EmotionOrchestrator (7.5.C)

**File:** `orchestrator/emotion_orchestrator.py`

Glue Tầng 1→3 + background tick 10Hz.

```python
orch = EmotionOrchestrator.from_loader(loader, memory=..., filter_service=...)
await orch.start()                            # spawn tick loop bg
processed = await orch.handle_event(emo_event)
# → ProcessedEvent(category, targets, tone_flag)
# → target buffered per-dim; tick sau flush + saturate + apply_appraisal + tick

mood = orch.current_mood()                    # snapshot MoodState (dùng bởi Director/prompt)
flags = orch.active_tone_flags()              # {"force_gentle_tone"}
orch.clear_tone_flags()                       # Prompt/Filter đã xử
cause = orch.active_cause()                    # A4: EmotionCause(alias, intent) đã sanitize
snap = orch.snapshot()                         # {current_mood, mood_pos, mood_target, active_flags…}
orch.reset_session()                           # reset modifier counters
await orch.stop()
```

`snapshot()` là nguồn cho **dashboard tab Mood** (xem `03_operations.md §Dashboard`).
Buffer per-tick: nhiều event cùng 1 tick → gom targets → saturate 1 lần → apply_appraisal.

### 7.5. DriftDetector (7.5.E) — ⚠️ KHÔNG CÒN DÙNG (A1)

**File:** `services/qc/drift_detector.py` (còn tồn tại, KHÔNG được wire).

Trước A1: so engine mood (appraisal) vs LLM self-report để bắt lệch. A1 bỏ LLM self-report
→ không còn nguồn so → drift bị gỡ khỏi `LLMTurnRunner`. Muốn QC mood lại: dùng affect-classifier
post-hoc trên câu Mai nói (chưa build).

---

## 8. Autonomy Engine v2

Thay hardcode `silence>60s` bằng urge probabilistic + category-based. Ở stream, autonomy
là **generator** cho Director (`force_generate`), không tự cầm nhịp.

### 8.1. UrgeAccumulator (Aut.A)

**File:** `orchestrator/autonomy_engine.py`

Tích luỹ urge 0-100 liên tục. Fix 5 vấn đề bản gốc:

1. **Threshold không hằng số** — Gaussian noise mỗi tick + probability sigmoid quanh floor
2. **Nhiều lý do nói** — 6 category (`CategorySelector`)
3. **Self-cooldown TÁCH** — `last_external_activity_ts` khác `last_self_speak_ts`
4. **Mood coupling** — bon_chon boost (+×weight), buon/nguong dampen (-×dampen)
5. **Nag decay** — `consecutive_ignored++` → `nag_penalty = max(0.4, 1 - 0.15×N)`

```python
urge = UrgeAccumulator(cfg_urge, clock=time.monotonic, rng=random.Random())
urge.tick(current_mood)                       # base_rise * mood_mult * nag + noise
if urge.should_speak_now():                   # probabilistic sigmoid
    ...
urge.on_self_spoke()                          # reset urge=0, cooldown 45s, ignored++
urge.on_external_activity()                   # reset silence + ignored=0
```

**Config `autonomy.yaml.autonomy.urge`:** rise_base, urge_floor, prob_scale, prob_max, self_cooldown_seconds, mood weights.

### 8.2. CategorySelector (Aut.A)

Weighted random. Không chọn cat trong `_recent` deque (no_repeat_window). Không chọn cat đang cooldown per-cat. Mood boost mult weight.

`select(mood)` trả cat name hoặc `None` (all cooldown). `mark_used(cat)` sau composer confirm dùng.

### 8.3. MaterialProvider (Aut.B)

**File:** `services/autonomy/material_provider.py`

Dispatch 6 category, trả `dict material` hoặc `None` (composer skip cat).

```python
@dataclass
class RuntimeContext:
    silence_seconds: float
    chat_count_last_10min: int
    operator_online: bool
    consecutive_ignored: int
    working_memory_recent: list[str]

mp = MaterialProvider.from_loader(loader)
material = mp.get("share_thought", ctx)       # → {"topic_seed": "..."} | None
```

Sources per cat:
- `complain_silence` → `{silence_seconds, chat_count_10min}` (từ ctx)
- `share_thought` → `{topic_seed}` từ `RoundRobinPool` (config `share_thought_pool`)
- `ask_chat` → `{question_seed, question_kind}` từ pool opinion/personal
- `call_operator` → `{operator_online, ignored_streak}` từ ctx
- `follow_up_topic` → `{memory_snippet}` từ ctx.working_memory_recent, None nếu empty
- `roast_chat` → `{target_chat}` cap 200 chars từ recent, None nếu empty

### 8.4. RoundRobinPool + OpenerTracker + DedupBuffer (Aut.B)

**Files:** `services/autonomy/pools.py`, `opener_tracker.py`, `dedup.py`

- **`RoundRobinPool(items, no_repeat_last_n=8, reshuffle_when_exhausted=True)`** — xoay vòng seed, không lặp N gần nhất, reshuffle khi hết
- **`OpenerTracker(window=5, words_per_opener=3)`** — trích 3 từ đầu mỗi câu Mai tự nói, forbidden_list format `"opener1...", "opener2..."` bơm vào prompt
- **`DedupBuffer(window=5, threshold=0.6)`** + `is_too_similar(new, recent, threshold=0.6)` — Jaccard token overlap check post-generate, composer regen 1 lần nếu True

Strip mood block khỏi text trước compare.

### 8.5. AutonomyEngine composer (Aut.C)

**File:** `orchestrator/autonomy_engine.py`

Compose 5 phần: Urge + Selector + MaterialProvider + OpenerTracker + DedupBuffer.

```python
engine = AutonomyEngine.from_loader(loader, material_provider=None, rng=None)

engine.tick(mood)                             # forward urge.tick
engine.on_external_activity()                 # reset silence
engine.on_self_spoke(text)                    # reset urge + record opener + dedup

decision = engine.maybe_generate(mood, ctx)   # → AmbientDecision | None
# Full flow: urge.should_speak? → loop select cat + material → render_prompt → mark_used
# Returns AmbientDecision(category, prompt_text, mood_snapshot, material)

if engine.check_dedup(text):                  # sau LLM generate
    text = await runner.run_ambient_turn(...)  # regen 1 lần
```

### 8.6. prompt_builder (Aut.C)

**File:** `services/autonomy/prompt_builder.py`

`render_prompt(category, material, mood, forbidden_openers, prompt_hint)` → Vietnamese instruction slot-filled.

Body per-category cụ thể (silence+chat count / topic seed / question seed / operator state / memory snippet / target_chat). Forbidden opener tường minh trong prompt (Bước 3).

Footer: "Câu TỰ NHIÊN, có nội dung — kể chuyện thì có chi tiết, cà khịa thì thẳng, không cắt cụt kiểu 1 câu xong hết. Không hedge, không nước đôi. ..."

### 8.7. Config đầy đủ

**`config/autonomy.yaml`:** tick_seconds=5, urge params, 6 category (weight/cooldown/mood_boost/prompt_hint).

**`config/autonomy_content_pool.yaml`:** 15 share_thought seed + 10 question seed (opinion/personal) + pool_policy (no_repeat_last_n=8) + dedup (window=5, threshold=0.6) + opener_tracker (window=5, words=3).

---

## 9. Platform Inputs

### 9.1. Interface

**File:** `interfaces/input.py`

```python
class EventSource(str, Enum):
    CHAT_TWITCH = "chat_twitch"
    CHAT_YOUTUBE = "chat_youtube"
    CHAT_DISCORD = "chat_discord"
    VOICE_OPERATOR = "voice_operator"
    SYSTEM_TIMER = "system_timer"
    DASHBOARD = "dashboard"

class InputEvent(BaseModel):
    event_id: str
    timestamp: datetime
    source: EventSource
    user_id: str | None
    user_name: str | None
    content: str
    metadata: dict[str, Any]

class InputService(Service):
    @abstractmethod
    def event_stream(self) -> AsyncIterator[InputEvent]: ...
```

### 9.2. YouTubeChatService

**File:** `services/input/youtube_chat.py`

Wrap `pytchat 0.5.5`. Public YouTube live scrape, KHÔNG cần OAuth.

```python
svc = YouTubeChatService(video_id="abc123", poll_interval_s=2.0)
await svc.start()                             # tạo pytchat.create(video_id) qua to_thread
async for event in svc.event_stream():
    ...
```

Parse super chat: `getattr(raw, "amountValue")` → `meta.amount_vnd` + `is_super_chat=True` → `ChatRouter` convert thành `EmotionEvent(kind=SYSTEM, platform_type=donation)`.

Fail-safe: malformed msg skip, client die → stream ends.

### 9.3. DiscordChatService

**File:** `services/input/discord_chat.py`

Wrap `discord.py 2.7`. Bot bridge event-driven callback → asyncio.Queue.

**Token từ env var** `DISCORD_BOT_TOKEN` (KHÔNG hardcode). Bot cần `MESSAGE CONTENT INTENT` bật ở Developer Portal.

```python
svc = DiscordChatService.from_loader(loader)  # đọc chat_sources.yaml.discord.*
await svc.start()                             # tạo discord.Client + task run bot bg
async for event in svc.event_stream():
    ...
```

Filter: `ignore_bots=True` (default), `channel_ids` whitelist (empty = mọi channel bot join). Queue full → drop_newest + metric `events_dropped_full`.

### 9.4. ChatRouter

**File:** `services/input/chat_router.py`

Multi-source consumer. **2 chế độ:**

- **INTAKE mode** (stream thật, C0): cấp `pool` + `pulse` → mỗi chat chỉ (1) chạy appraisal
  mood, (2) `_pump_intake` bơm vào `SaliencePool.add()` + `ChatPulse.record()`. **KHÔNG tự
  đáp.** Director nhặt sau.
- **FIFO mode** (không cấp pool/pulse — test/legacy): `_process` chạy `runner.run_turn` trực
  tiếp trong `turn_lock` như cũ.

```python
router = ChatRouter(
    sources=[YouTubeChatService(...), DiscordChatService(...)],
    emotion=emotion_orchestrator, runner=llm_turn_runner, speak=speak_callback,
    pool=salience_pool, pulse=chat_pulse, turn_lock=shared_lock,   # → INTAKE mode
)
```

`_pump_intake(ev)`: phân loại kind (`mention`/`question`/`chat` qua regex nhẹ), lấy
`amount_vnd`/`is_super` từ super chat → `pool.add(...)`, `pulse.record(now, user_id)`.

**Fail-safe:** emotion raise → skip event; intake raise → log, không kill router.

---

## 10. Runtime composer (StreamRuntime)

**File:** `orchestrator/stream_runtime.py`

Compose FULL stack cho stream/cli mode + autonomy tick loop bg.

```python
cfg = StreamRuntimeConfig(
    enable_tts=True, enable_memory=False, enable_autonomy=True,
    enable_dashboard=False, on_token=print_callback,
)

rt = await build_stream_runtime(
    loader=loader, sources=[YouTubeChatService(...), ...], cfg=cfg,
)
await rt.start()
await rt.wait_until_stopped()   # blocks Ctrl+C
await rt.stop()
```

**`build_stream_runtime()` factory** wire:
1. LLM stack (health check bắt buộc, raise nếu server chưa chạy)
2. EmotionOrchestrator (A1: KHÔNG còn DriftDetector)
3. Memory (nếu `enable_memory=True`, rewire `emotion._modifiers._memory`)
4. `FeatureManager` + `RuleFilter` + `FilterRegenerator`; `filter_rule` quyết định
   regenerator có gắn vào runner lúc startup hay không
5. LLMTurnRunner với UUID session mới cho mỗi runtime và optional wire; handler
   FeatureManager bật/tắt filter cho turn kế tiếp
6. VieNeuTtsService + AudioPlayer + TTSPipeline (nếu `enable_tts`)
7. AutonomyEngine (nếu `enable_autonomy`) — làm **generator self_talk** cho Director
8. **C0 Director stack:** `SaliencePool` + `ChatPulse` + `Director` + `DirectorLoop`, `turn_lock` chung
9. ChatRouter INTAKE mode (cấp pool+pulse)
10. Dashboard với FeatureManager + filter/regenerator + `emotion` (nếu
    `enable_dashboard`) → toggle thật, tab Filter và tab Mood

**Driver:** `DirectorLoop` cầm nhịp (start ở `StreamRuntime.start` nếu có; `_autonomy_loop`
cũ chỉ chạy khi KHÔNG có director). Share `turn_lock` chung — 1 turn tại 1 thời điểm.

**Chat activity hook**: wrap `router._process` → `autonomy.on_external_activity()` +
`rt.note_chat_activity()` mỗi event.

Chi tiết Director stack: xem **§12 dưới**.

---

## 11. Dashboard + Metrics

### 11.1. MetricsCollector

**File:** `orchestrator/metrics_collector.py`

Prometheus metrics + snapshot dicts cho dashboard render.

Categories:
- **LLM:** `llm_ttft_seconds` (histogram), `llm_decode_tps`, `llm_requests_total`, `llm_fallback_total{level}`, `llm_parse_total{status}`
- **Trigger:** `trigger_processed_total{type}`, `trigger_skipped_total{reason}`, `trigger_queue_size`, `trigger_interrupt_total`
- **State:** `state_transitions_total{from,to}`, `state_duration_seconds`, `watchdog_deadlocks_total`
- **Filter:** `filter_checks_total`, `filter_hits_total{category}`, `filter_regenerate_total{outcome}`
- **TTS:** `tts_pipeline_last_ttfa_ms`, `tts_subtitle_fallback_total`, `tts_chunks_played`, `tts_chunks_dropped`
- **Mood:** `mood_ticks`, `mood_appraisal_applies` (`mood_llm_applies` = 0 sau A1)
- **Memory:** `memory_queries_total`, `memory_timeouts_total`, `memory_writes_total`
- **Autonomy:** `autonomy_generated_total`, `autonomy_skipped_no_material`, `autonomy_dedup_hits`
- **Director/C0:** `director_segment`, `director_turns_read/self`, `director_transitions`,
  `salience_pool_size/added/clustered/evicted`, `pulse_state/tempo/diversity`
- ⚠️ `llm_parse_total{status}` giữ nhãn cũ "parse mood block" nhưng giờ chỉ đo text non-empty
  (A1). `drift_*` metric CHẾT (không còn ghi).

### 11.2. DashboardServer

**File:** `dashboard/dashboard_server.py`

FastAPI + WebSocket. Port 7860 mặc định. Frontend Vanilla JS + Canvas chart (KHÔNG CDN, 100% local).

REST endpoints:
- `GET /` — HTML
- `GET /metrics` — Prometheus format
- `POST /features/{id}/toggle` — feature toggle
- `POST /emergency_stop`, `POST /resume`
- `GET /api/recent_turns` — review item có đủ `session_id` + `turn_id`
- `POST /api/rate` — live rating dùng identity turn cuối; review rating phải gửi đủ cặp khóa
- `POST /api/correct` — bắt buộc `{session_id, turn_id, corrected_text}` và lookup đúng cặp

WebSocket `/ws` — push snapshot mỗi 1s.

Tabs: Metrics (LLM+System), Features, State Machine, Triggers, Filter, TTS, **Mood**.
Tab Mood (Task 8): `drawMoodChart` vẽ 5 đường realtime (pos đặc + target chấm) từ
`snap.mood = emotion.snapshot()`. Cần truyền `DashboardServer(emotion=emotion)`.

---

## 12. Director stack (C0)

**Thư mục:** `services/director/`. Biến reactive (đáp mọi tin FIFO) → host tự điều hành.

### 12.1. SaliencePool (`salience.py`)
Chấm điểm + decay + cluster chat. Chat vào pool, KHÔNG tự thành turn.
```
base   = base_tier[kind] + superchat_coef * log1p(amount/divisor)
score  = (base + cluster_coef * log1p(cluster_count-1)) * exp(-age/tau)
```
- `add()` — tin near-duplicate (Jaccard token > threshold, dùng `DedupBuffer._tokenize`) →
  gom vào đại diện (`cluster_count++`). Super chat gom vào tin thường → nâng base.
- `peek_top/pop_top/top_cluster(max_refs)` — Director nhặt.
- `evict_stale` (score < floor) + `_enforce_cap(pool_max)` — staleness + backpressure tự dọn.
- Config `config/chat_salience.yaml §salience`.
- ⚠️ chưa lưu `viewer_name` (FIX_PLAN Task 2), chưa có `purge_below` (Task 3).

### 12.2. ChatPulse (`chat_pulse.py`)
Đo độ sôi nổi. `tempo` (tin/phút) · `diversity` (unique_users/msg) · `state`:

| tempo | diversity | state | Director |
|---|---|---|---|
| cao | thấp | HYPE_SPAM | react VIBE, không đáp lẻ |
| cao | cao | LIVELY | triage gắt, đáp gọn |
| thấp | — | COLD | self_talk / đổi segment |
| giữa | — | NORMAL | đáp bình thường |

⚠️ `accel/baseline` là dead signal (`update_baseline` chưa được gọi — FIX_PLAN Task 6).
⚠️ pulse CHƯA feed vào mood (Task 7).

### 12.3. Director (`director.py`) — pure decision engine
`decide(now, urge_ready) → DirectorDecision(action, segment, refs, read_mode)`. Action:
`READ_CHAT` (single/cluster/summary/vibe) · `ACK_DONATION` · `SELF_TALK` · `FOLLOW_UP` ·
`TRANSITION` · `WAIT`. Thứ tự ưu tiên trong `decide`: hết-giờ-segment → superchat-ack →
hype-vibe → read-chat → proactive(self_talk/transition) → wait.

Chống "máy đọc chat": `max_consecutive_read_chat` → ép xen self_talk; `max_refs_per_turn`.
Segment (`config/director.yaml`): `opening/main/chat/closing`, mỗi cái `{goal, duration,
allowed_actions}`. ⚠️ `ack_donation` thiếu ở opening/closing (FIX_PLAN Task 1).

### 12.4. DirectorLoop (`director_loop.py`) — driver
Tick `tick_seconds` (skip nếu `turn_lock` đang giữ) → `evict_stale` → `decide` → execute:
- `_exec_read` → dựng prompt theo read_mode → `runner.run_turn` → `pool.remove(refs)` → speak
- `_exec_self_talk` → `autonomy.force_generate` → `run_ambient_turn` (+dedup regen) →
  `on_self_spoke` + `commit_self_talk` → speak
- `_exec_transition` → Mai báo chuyển phần → `advance_segment` → speak

Mọi lỗi execute fail-safe: log + tiếp tick. `clock` inject cho test.

---

## Notes / Gotchas thường gặp

1. **VieNeu-TTS TTFA 5.6s?** — quên gọi `add_voice(...)` trong `start()`. Đảm bảo cache voice 1 lần.
2. **torchaudio.load fail torchcodec** — Windows thiếu FFmpeg. VieNeuTtsService.start() có patch `torchaudio.load = soundfile.load` — nếu bypass patch phải cài FFmpeg 7.
3. **CUDA sm_120 kernel not found** — torch không cu128. RTX 5060 Ti cần torch 2.7+cu128 hoặc 2.11+cu128.
4. **coqui-tts import fail** — hub 1.x incompat. Đã xoá viXTTS 2026-08, không dùng nữa.
5. **memory retrieve chậm** — bge-m3 chưa load (lazy). Gọi `embedder.load()` ở `start()`.
6. **Discord bot không nhận message** — quên bật MESSAGE CONTENT INTENT ở Developer Portal.
7. **LLM stream lỗi socket** — llama-server chưa chạy hoặc port khác. Health check trước.
8. **Autonomy không tự nói** — check `urge.urge` (dashboard), có thể vẫn dưới `urge_floor` (30). Tune xuống hoặc `bon_chon_weight` cao. Ở stream thật, self_talk do **Director** quyết (dead-air/cold/urge), không phải autonomy loop.
9. **Mai không đáp chat / đáp trễ** — Director tick = `autonomy.cfg.tick_seconds` (5s), chat chờ tới tick kế. Hạ tick (FIX_PLAN Task 4). Hoặc tin điểm thấp bị decay/evict trước khi Director nhặt (đúng thiết kế — chat rác không đáp).
10. **Superchat không được cảm ơn** — segment hiện tại thiếu `ack_donation` trong `allowed_actions` (FIX_PLAN Task 1), hoặc ack gọi bằng channel ID thay tên (Task 2).
9. **Mai nói ngắn** — persona đã sửa 2026-08-06. Nếu cần dài hơn: tăng `num_predict` (500 → 800), giữ `temperature` 0.85.
